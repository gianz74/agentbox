#!/usr/bin/env bash
# De-risking spike gate (THROWAWAY — a hand-written bwrap launch, NOT package code).
#
# Proves the two riskiest unknowns before any package code is written:
#   - the IDE SSE bridge across `--unshare-net` (pasta `-T`)  ............... (crux)
#   - net isolation: an untrusted session cannot reach host-localhost  ...... (c)
# plus (a) identity/ownership parity and (b) outbound NAT+DNS+TLS, and a
# Bun-SIGPWR probe (real `claude --version` under bwrap, if present).
#
# Integration pattern (a spike finding): pasta is the PARENT and spawns bwrap.
# pasta creates the net+user namespace and configures NAT; bwrap shares that
# netns (it does NOT `--unshare-net`) and unshares everything else. This avoids
# the netns chicken-and-egg of attaching pasta to a bwrap-created namespace.
#
# Exits 0 only if all of (a)/(b)/(c)/(crux) pass; nonzero on any failure.
set -u

fail=0
check() { # $1 name  $2 PASS|FAIL|SKIP
  printf '  [%-4s] %s\n' "$2" "$1"
  [ "$2" = FAIL ] && fail=1
  return 0
}
fatal() { printf 'FATAL: %s\n' "$*" >&2; exit 2; }

# --- host facts -------------------------------------------------------------
: "${USER:=$(id -un)}"
: "${HOME:=$(getent passwd "$USER" | cut -d: -f6)}"
UID_N=$(id -u); GID_N=$(id -g)
GW=$(ip route get 1.1.1.1 2>/dev/null | grep -oP 'via \K[0-9.]+' | head -1)
[ -n "$GW" ] || fatal "no default gateway found (need outbound route)"
for b in bwrap pasta curl python3 ip; do command -v "$b" >/dev/null || fatal "missing host tool: $b"; done

WORK=$(mktemp -d); PROJ=$(mktemp -d)   # PROJ = real dir bound rw as cwd (ownership-parity test)
cleanup() {
  [ -n "${SRV_SSE:-}" ] && kill "$SRV_SSE" 2>/dev/null
  [ -n "${SRV_BLK:-}" ] && kill "$SRV_BLK" 2>/dev/null
  wait 2>/dev/null
  rm -rf "$WORK" "$PROJ"
}
trap cleanup EXIT

# --- constructed identity + curated /etc ------------------------------------
printf '%s:x:%s:%s:%s:%s:/bin/bash\n' "$USER" "$UID_N" "$GID_N" "$USER" "$HOME" > "$WORK/passwd"
printf '%s:x:%s:\n' "$USER" "$GID_N" > "$WORK/group"
printf 'nameserver %s\n' "$GW" > "$WORK/resolv.conf"          # points at pasta's --dns-forward
printf 'passwd: files\ngroup: files\nhosts: files dns\n' > "$WORK/nsswitch.conf"

# --- free loopback ports ----------------------------------------------------
read -r PORT_SSE PORT_BLK < <(python3 - <<'PY'
import socket
def free():
    s = socket.socket(); s.bind(('127.0.0.1', 0)); p = s.getsockname()[1]; s.close(); return p
print(free(), free())
PY
)
[ -n "$PORT_SSE" ] && [ -n "$PORT_BLK" ] || fatal "could not allocate test ports"

# --- host servers: one SSE stream (forwarded via -T), one plain (must stay blocked)
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

# native claude (Bun single-file ELF) for the SIGPWR probe, if installed
CLAUDE_BIN=""
[ -L "$HOME/.local/bin/claude" ] && CLAUDE_BIN=$(readlink -f "$HOME/.local/bin/claude" 2>/dev/null)
[ -n "$CLAUDE_BIN" ] && [ -x "$CLAUDE_BIN" ] || CLAUDE_BIN=""
CLAUDE_BIND=()
[ -n "$CLAUDE_BIN" ] && [ -e "$HOME/.local/share/claude" ] && \
  CLAUDE_BIND=(--ro-bind "$HOME/.local/share/claude" "$HOME/.local/share/claude")

printf '== spike: USER=%s uid=%s gw=%s sse=%s blk=%s claude=%s ==\n' \
  "$USER" "$UID_N" "$GW" "$PORT_SSE" "$PORT_BLK" "${CLAUDE_BIN:-<none>}"

# --- the sandbox: pasta(parent, isolated NAT) -> bwrap(everything-but-net) --
# Isolation: --no-map-gw closes pasta's default gateway->host-loopback mapping
# (without it the guest reaches every host-localhost port via the gateway addr).
# Bridge:    -T <PORT_SSE> forwards exactly that one port from the netns loopback
#            out to the host init-ns loopback. DNS: --dns-forward <gw>.
pasta --config-net -f -q --no-map-gw --dns-forward "$GW" -T "$PORT_SSE" -- \
  bwrap \
    --unshare-user --unshare-ipc --unshare-pid --unshare-uts --unshare-cgroup \
    --uid "$UID_N" --gid "$GID_N" \
    --ro-bind /usr /usr \
    --ro-bind-try /lib /lib --ro-bind-try /lib64 /lib64 \
    --ro-bind-try /bin /bin --ro-bind-try /sbin /sbin \
    --ro-bind /etc/ssl /etc/ssl \
    --ro-bind-try /etc/ca-certificates /etc/ca-certificates \
    --ro-bind-try /etc/ca-certificates.conf /etc/ca-certificates.conf \
    --ro-bind-try /etc/alternatives /etc/alternatives \
    --ro-bind-try /etc/localtime /etc/localtime \
    --ro-bind "$WORK/passwd" /etc/passwd \
    --ro-bind "$WORK/group" /etc/group \
    --ro-bind "$WORK/nsswitch.conf" /etc/nsswitch.conf \
    --ro-bind "$WORK/resolv.conf" /etc/resolv.conf \
    --proc /proc --dev /dev --tmpfs /tmp \
    --tmpfs "$HOME" \
    --bind "$PROJ" /work \
    "${CLAUDE_BIND[@]}" \
    --setenv HOME "$HOME" --setenv USER "$USER" --setenv PATH /usr/bin:/bin \
    --setenv EXP_USER "$USER" --setenv EXP_UID "$UID_N" --setenv EXP_HOME "$HOME" \
    --setenv GW "$GW" --setenv PORT_SSE "$PORT_SSE" --setenv PORT_BLK "$PORT_BLK" \
    --setenv CLAUDE_BIN "${CLAUDE_BIN:-}" \
    --die-with-parent --new-session --chdir /work \
    -- /bin/bash -c '
      set -u; R=/work/results; : > "$R"
      put() { printf "%s=%s\n" "$1" "$2" >> "$R"; }

      # (a) identity
      if [ "$(id -un)" = "$EXP_USER" ] && [ "$(id -u)" = "$EXP_UID" ] && [ "$HOME" = "$EXP_HOME" ]; then
        put ident PASS; else put ident "FAIL(got $(id -un)/$(id -u)/$HOME)"; fi

      # (a) ownership: write a file into the rw-bound cwd; host checks owner parity
      if echo "written-inside-by-$(id -un)" > /work/owned 2>/dev/null; then put ownwrite PASS; else put ownwrite FAIL; fi

      # (b) outbound NAT + DNS + TLS
      code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 12 https://api.anthropic.com/ 2>/dev/null)
      if [ -n "$code" ] && [ "$code" != 000 ]; then put outbound "PASS(http $code)"; else put outbound FAIL; fi

      # (c) isolation: host blocked server must be unreachable BOTH via guest
      #     loopback AND via the gateway address (the default pasta leak path).
      b1=$(curl -s --max-time 5 "http://127.0.0.1:$PORT_BLK/probe" 2>/dev/null)
      b2=$(curl -s --max-time 5 "http://$GW:$PORT_BLK/probe" 2>/dev/null)
      if printf "%s%s" "$b1" "$b2" | grep -q BLOCKED-SECRET; then put isolation "FAIL(leak)"; else put isolation PASS; fi

      # (crux) SSE bridge: read the forwarded stream over guest loopback, expect >=3 events
      ev=$(curl -sN --max-time 8 "http://127.0.0.1:$PORT_SSE/" 2>/dev/null | grep -c "^data:")
      if [ "${ev:-0}" -ge 3 ]; then put sse "PASS($ev events)"; else put sse "FAIL($ev events)"; fi

      # Bun SIGPWR/GC probe: real claude under bwrap (no restrictive seccomp/caps)
      if [ -n "${CLAUDE_BIN:-}" ]; then
        if v=$("$CLAUDE_BIN" --version 2>&1); then put bun "PASS($v)"; else put bun "FAIL(rc=$? $v)"; fi
      fi
    '
rc=$?
[ "$rc" -eq 0 ] || fatal "sandbox launch failed (pasta/bwrap rc=$rc)"

# --- aggregate results (outer side) ----------------------------------------
echo "-- checks --"
val() { sed -n "s/^$1=//p" "$PROJ/results" 2>/dev/null | head -1; }
status() { case "$1" in PASS*) echo PASS;; FAIL*|"") echo FAIL;; *) echo FAIL;; esac; }
for c in ident ownwrite outbound isolation sse; do
  v=$(val "$c"); check "$c ${v:+= $v}" "$(status "$v")"
done

# (a) ownership parity, observed from the HOST side
owner=$(stat -c %U "$PROJ/owned" 2>/dev/null)
if [ "$owner" = "$USER" ]; then check "ownership-parity (host sees owner=$USER)" PASS
else check "ownership-parity (host sees owner=${owner:-<missing>})" FAIL; fi

# Bun probe (soft: SKIP if native claude absent, FAIL only if present & crashed)
bun=$(val bun)
if [ -n "$bun" ]; then check "bun-no-sigpwr ${bun:+= $bun}" "$(status "$bun")"
elif [ -z "$CLAUDE_BIN" ]; then check "bun-no-sigpwr (native claude absent)" SKIP; fi

echo "-----------"
if [ "$fail" -eq 0 ]; then echo "SPIKE: PASS — bwrap+pasta isolation and the SSE bridge are GO."; exit 0
else echo "SPIKE: FAIL — see checks above."; exit 1; fi
