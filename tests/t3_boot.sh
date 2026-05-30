#!/usr/bin/env bash
# Boot driver for the sandbox mechanism: drives the package's sandbox/net layers
# to launch a real bwrap sandbox (pasta as parent) on the host and checks that it
#   - has the right identity (whoami / $HOME / id) and ownership parity,
#   - execs an arbitrary command,
#   - has working outbound NAT + DNS + TLS,
#   - cannot reach a host-localhost service (via guest loopback OR the gateway),
#   - reaches one host port that is explicitly forwarded (the IDE SSE path), and
#   - reports a clear, package-naming error when bwrap is missing.
# Exits 0 only if all required checks pass. Throwaway: a hand-driven harness, not
# package code.
set -u

REPO=$(cd "$(dirname "$0")/.." && pwd)
export PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}"

fail=0
check() { printf '  [%-4s] %s\n' "$2" "$1"; [ "$2" = FAIL ] && fail=1; return 0; }
fatal() { printf 'FATAL: %s\n' "$*" >&2; exit 2; }

for b in bwrap pasta curl python3 ip; do
  command -v "$b" >/dev/null || fatal "missing host tool: $b"
done

WORK=$(mktemp -d); PROJ=$(mktemp -d)   # PROJ = real dir bound rw as cwd (parity test)
cleanup() {
  [ -n "${SRV_SSE:-}" ] && kill "$SRV_SSE" 2>/dev/null
  [ -n "${SRV_BLK:-}" ] && kill "$SRV_BLK" 2>/dev/null
  wait 2>/dev/null
  rm -rf "$WORK" "$PROJ"
}
trap cleanup EXIT

# --- free loopback ports ----------------------------------------------------
read -r PORT_SSE PORT_BLK < <(python3 - <<'PY'
import socket
def free():
    s = socket.socket(); s.bind(('127.0.0.1', 0)); p = s.getsockname()[1]; s.close(); return p
print(free(), free())
PY
)
[ -n "$PORT_SSE" ] && [ -n "$PORT_BLK" ] || fatal "could not allocate test ports"

# --- host servers: one SSE stream (forwarded), one plain (must stay blocked) -
cat > "$WORK/sse.py" <<'PY'
import sys, time, http.server, socketserver
PORT = int(sys.argv[1]); N = 3
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        for i in range(N):
            self.wfile.write(b'data: tick %d\n\n' % i); self.wfile.flush(); time.sleep(0.05)
class S(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
S(('127.0.0.1', PORT), H).serve_forever()
PY
python3 "$WORK/sse.py" "$PORT_SSE" >/dev/null 2>&1 & SRV_SSE=$!
mkdir -p "$WORK/blk"; printf 'BLOCKED-SECRET\n' > "$WORK/blk/probe"
( cd "$WORK/blk" && exec python3 -m http.server "$PORT_BLK" --bind 127.0.0.1 ) >/dev/null 2>&1 & SRV_BLK=$!

# wait for both host servers to accept
for _ in $(seq 1 20); do
  curl -s -o /dev/null "http://127.0.0.1:$PORT_BLK/probe" && \
  curl -s -o /dev/null "http://127.0.0.1:$PORT_SSE/" && break
  sleep 0.2
done

GW=$(ip route get 1.1.1.1 2>/dev/null | grep -oP 'via \K[0-9.]+' | head -1)
[ -n "$GW" ] || fatal "no default gateway found (need an outbound route)"

printf '== boot driver: PROJ=%s sse=%s blk=%s gw=%s ==\n' "$PROJ" "$PORT_SSE" "$PORT_BLK" "$GW"

# --- boot the sandbox through the package; probe writes results to the rw cwd -
python3 - "$PROJ" "$PORT_SSE" "$PORT_BLK" "$GW" <<'PY'
import sys
from claude_sandbox import sandbox
from claude_sandbox.sandbox import SandboxSpec, Bind, host_identity

proj, sse, blk, gw = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
ident = host_identity()

probe = r'''
set -u; R=/work/results; : > "$R"
put() { printf "%s=%s\n" "$1" "$2" >> "$R"; }

# identity
if [ "$(id -un)" = "$EXP_USER" ] && [ "$(id -u)" = "$EXP_UID" ] && [ "$HOME" = "$EXP_HOME" ]; then
  put ident PASS; else put ident "FAIL(got $(id -un)/$(id -u)/$HOME)"; fi

# arbitrary exec runs and produces expected output
if [ "$(echo hello-from-sandbox)" = "hello-from-sandbox" ]; then put exec PASS; else put exec FAIL; fi

# ownership parity: write into the rw-bound cwd; host checks owner
if echo "written-inside-by-$(id -un)" > /work/owned 2>/dev/null; then put ownwrite PASS; else put ownwrite FAIL; fi

# outbound NAT + DNS + TLS (any HTTP response proves reachability)
code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 12 https://api.anthropic.com/ 2>/dev/null)
if [ -n "$code" ] && [ "$code" != 000 ]; then put outbound "PASS(http $code)"; else put outbound FAIL; fi

# isolation: host blocked server unreachable via guest loopback AND gateway addr
b1=$(curl -s --max-time 5 "http://127.0.0.1:$PORT_BLK/probe" 2>/dev/null)
b2=$(curl -s --max-time 5 "http://$GW:$PORT_BLK/probe" 2>/dev/null)
if printf "%s%s" "$b1" "$b2" | grep -q BLOCKED-SECRET; then put isolation "FAIL(leak)"; else put isolation PASS; fi

# forwarded host port: read the SSE stream over guest loopback, expect >=3 events
ev=$(curl -sN --max-time 8 "http://127.0.0.1:$PORT_SSE/" 2>/dev/null | grep -c "^data:")
if [ "${ev:-0}" -ge 3 ]; then put sse "PASS($ev events)"; else put sse "FAIL($ev events)"; fi
'''

spec = SandboxSpec(
    identity=ident,
    argv=("/bin/bash", "-c", probe),
    binds=(Bind(proj, "/work", mode="rw"),),
    setenv={
        "EXP_USER": ident.user, "EXP_UID": str(ident.uid), "EXP_HOME": ident.home,
        "PORT_SSE": str(sse), "PORT_BLK": str(blk), "GW": gw,
    },
    chdir="/work",
)
sys.exit(sandbox.run(spec, sse_port=sse, gateway=gw))
PY
rc=$?
[ "$rc" -eq 0 ] || fatal "sandbox launch failed (pasta/bwrap rc=$rc)"

# --- aggregate the in-sandbox results (outer side) --------------------------
echo "-- checks --"
val() { sed -n "s/^$1=//p" "$PROJ/results" 2>/dev/null | head -1; }
status() { case "$1" in PASS*) echo PASS;; *) echo FAIL;; esac; }
for c in ident exec ownwrite outbound isolation sse; do
  v=$(val "$c"); check "$c ${v:+= $v}" "$(status "$v")"
done

# ownership parity, observed from the host side
owner=$(stat -c %U "$PROJ/owned" 2>/dev/null)
if [ "$owner" = "$USER" ]; then check "ownership-parity (host owner=$USER)" PASS
else check "ownership-parity (host owner=${owner:-<missing>})" FAIL; fi

# missing-bwrap diagnostic: a clear error naming the apt package
if msg=$(python3 -c '
import os, sys
os.environ["PATH"] = "/nonexistent-claude-sandbox"
from claude_sandbox import sandbox
try:
    sandbox.ensure_bwrap()
except sandbox.SandboxError as e:
    sys.stdout.write(str(e)); sys.exit(0)
sys.exit(1)
' 2>&1); then
  printf '%s' "$msg" | grep -q bubblewrap \
    && check "bwrap-missing -> names bubblewrap" PASS \
    || check "bwrap-missing error did not name the package ($msg)" FAIL
else
  check "bwrap-missing did not raise a clear error" FAIL
fi

echo "-----------"
if [ "$fail" -eq 0 ]; then echo "BOOT: PASS -- sandbox mechanism and net posture are correct."; exit 0
else echo "BOOT: FAIL -- see checks above."; exit 1; fi
