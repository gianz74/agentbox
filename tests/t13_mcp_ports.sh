#!/usr/bin/env bash
# Forward EVERY host-loopback MCP port the editor names -- not just the SSE port.
# A modern editor drives two MCP channels: the `ide` WebSocket on
# CLAUDE_CODE_SSE_PORT and a streamable-HTTP server on a separate per-session port
# named inline in a `--mcp-config` operand. The wrapper must bridge both, and only
# those: a non-loopback URL in `--mcp-config` must stay unreachable (no
# host-localhost leak).
#
# This drives the package hot path (run.run) through real pasta+bwrap with
# three host-loopback servers up and checks, from inside the sandbox:
#   - the SSE/ws port round-trips (>=3 events) through its pasta `-T`,
#   - the `emacs-tools` HTTP URL (a loopback port the editor named) answers 200,
#   - a server whose `--mcp-config` URL is non-loopback is NOT forwarded: its
#     host-loopback port, live on the host, stays unreachable inside.
# The pure logic (port extraction across inline JSON / file operands /
# localhost|127.0.0.1|::1 / scheme-default ports / malformed+non-loopback skips,
# and wrap_argv's one-deduped-`-T`-per-port) is covered first by the unit suite.
# The store is built offline (copy path) so only the deliberate forwards touch the
# loopback; the hot path is pasta-fronted, so this needs a default route.
# Exits 0 only if every check passes.
set -u

REPO=$(cd "$(dirname "$0")/.." && pwd)
export PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}"

fail=0
check() { printf '  [%-4s] %s\n' "$2" "$1"; [ "$2" = FAIL ] && fail=1; return 0; }
fatal() { printf 'FATAL: %s\n' "$*" >&2; exit 2; }

for b in bwrap pasta python3 curl ip id; do
  command -v "$b" >/dev/null || fatal "missing host tool: $b"
done

GW=$(ip route get 1.1.1.1 2>/dev/null | grep -oP 'via \K[0-9.]+' | head -1)
[ -n "$GW" ] || fatal "no default gateway found (the hot path is pasta-fronted)"

echo "== unit suite: --mcp-config port extractor + wrap_argv multi-port =="
python3 -m pytest -q "$REPO/tests/test_claude.py" "$REPO/tests/test_net.py" \
  || fatal "MCP/net unit suite failed"

RESULTS=$(mktemp)
cleanup() { rm -f "$RESULTS"; rm -rf "${TMP:-}"; }
trap cleanup EXIT

printf '== mcp-ports driver: gw=%s ==\n' "$GW"

# The driver runs the launch + assertions and writes `key=PASS|FAIL(...)` lines
# into $RESULTS; it prints the temp root it owns on stdout for cleanup.
TMP=$(python3 - "$RESULTS" "$GW" <<'PY'
import json, os, socket, subprocess, sys, tempfile, time, urllib.request
from agentbox import run as rmod, store as smod
from agentbox.agents import AGENTS
from agentbox.config import parse_config
from agentbox.sandbox import host_identity

AG = AGENTS["claude"]
results_path, gw = sys.argv[1], sys.argv[2]
checks = {}
def put(k, ok, detail=""):
    checks[k] = "PASS" if ok else ("FAIL(%s)" % detail if detail else "FAIL")

ident = host_identity()
tmp = tempfile.mkdtemp(prefix="box-t13.")
fakehost = os.path.join(tmp, "fakehost")
store = os.path.join(tmp, "store")

# A synthetic native claude whose "binary" is a probe: from inside the sandbox it
# exercises all three channels and records the outcome into the rw-bound cwd.
#   - SSE/ws: count event lines off CLAUDE_CODE_SSE_PORT,
#   - emacs-tools: GET the loopback URL the editor named (forwarded -> 200),
#   - blocked: GET the host-loopback port of the non-loopback URL (never
#     forwarded -> connection refused inside).
# The inline --mcp-config JSON is forwarded verbatim, so the probe reads the URLs
# straight back out of its own argv.
PROBE = r'''#!/bin/sh
OUT="$(pwd)/claude_out"
mcp=""; prev=""
for a in "$@"; do
  [ "$prev" = "--mcp-config" ] && mcp="$a"
  case "$a" in --mcp-config=*) mcp="${a#--mcp-config=}";; esac
  prev="$a"
done
url() { printf '%s' "$mcp" | python3 -c 'import json,sys;print(json.load(sys.stdin)["mcpServers"][sys.argv[1]]["url"])' "$1" 2>/dev/null; }
port_of() { printf '%s' "$1" | python3 -c 'import sys;from urllib.parse import urlparse;print(urlparse(sys.stdin.read().strip()).port or "")' 2>/dev/null; }
{
  if [ -n "${CLAUDE_CODE_SSE_PORT:-}" ]; then
    ev=$(curl -sN --max-time 8 "http://127.0.0.1:$CLAUDE_CODE_SSE_PORT/" 2>/dev/null | grep -c "^data:")
    echo "SSE_EVENTS=${ev:-0}"
  fi
  good=$(url emacs-tools)
  echo "HTTP_URL=${good:-<none>}"
  echo "HTTP_CODE=$(curl -s --max-time 8 -o /dev/null -w '%{http_code}' "$good" 2>/dev/null)"
  bad=$(url blocked)
  bport=$(port_of "$bad")
  echo "BLK_PORT=${bport:-<none>}"
  if [ -n "$bport" ]; then
    echo "BLK_CODE=$(curl -s --max-time 5 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$bport/" 2>/dev/null)"
    echo "BLK_RC=$?"
  fi
} > "$OUT"
echo "STORE-9.9.9"
exit 0
'''
ver = "9.9.9"
vroot = os.path.join(fakehost, ".local", "share", "claude", "versions"); os.makedirs(vroot)
payload = os.path.join(vroot, ver)
open(payload, "w").write(PROBE); os.chmod(payload, 0o755)
binsrc = os.path.join(fakehost, ".local", "bin"); os.makedirs(binsrc)
os.symlink(payload, os.path.join(binsrc, "claude"))

# Build the frozen store once, offline; the launch then takes the fast path.
smod.install_store(AG, store=store, method="copy", source_home=fakehost)

def kv(path):
    out = {}
    if os.path.exists(path):
        for line in open(path):
            line = line.rstrip("\n")
            if "=" in line:
                k, v = line.split("=", 1); out[k] = v
    return out

def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

# A host SSE server (text/event-stream, 3 ticks) and a plain 200-OK HTTP server.
SSE_SRC = (
    "import sys,time,http.server,socketserver\n"
    "P=int(sys.argv[1])\n"
    "class H(http.server.BaseHTTPRequestHandler):\n"
    " def log_message(self,*a):pass\n"
    " def do_GET(self):\n"
    "  self.send_response(200);self.send_header('Content-Type','text/event-stream')\n"
    "  self.send_header('Cache-Control','no-cache');self.end_headers()\n"
    "  [ (self.wfile.write(b'data: tick %d\\n\\n'%i),self.wfile.flush(),time.sleep(0.05)) for i in range(3) ]\n"
    "class S(socketserver.ThreadingTCPServer):allow_reuse_address=True\n"
    "S(('127.0.0.1',P),H).serve_forever()\n"
)
# Dual-stack (V6ONLY off) so it answers on both loopback families. `localhost`
# resolves to ::1 or 127.0.0.1 depending on the host's gai ordering, and pasta
# forwards each family to the host's same-family loopback -- a real local MCP
# server likewise answers on whichever loopback the client picks.
HTTP_SRC = (
    "import sys,socket,http.server,socketserver\n"
    "P=int(sys.argv[1])\n"
    "class H(http.server.BaseHTTPRequestHandler):\n"
    " def log_message(self,*a):pass\n"
    " def do_GET(self):\n"
    "  self.send_response(200);self.end_headers();self.wfile.write(b'mcp-ok')\n"
    "class S(socketserver.ThreadingTCPServer):\n"
    " address_family=socket.AF_INET6\n"
    " allow_reuse_address=True\n"
    " def server_bind(self):\n"
    "  self.socket.setsockopt(socket.IPPROTO_IPV6,socket.IPV6_V6ONLY,0)\n"
    "  socketserver.ThreadingTCPServer.server_bind(self)\n"
    "S(('::',P),H).serve_forever()\n"
)

sse_port, http_port, blk_port = free_port(), free_port(), free_port()
procs = []
def spawn(src, port):
    procs.append(subprocess.Popen([sys.executable, "-c", src, str(port)],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
def wait_accept(port):
    for _ in range(40):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close(); return True
        except OSError:
            time.sleep(0.1)
    return False
def host_status(port):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/" % port, timeout=3) as r:
            return r.status
    except Exception:
        return None

spawn(SSE_SRC, sse_port); spawn(HTTP_SRC, http_port); spawn(HTTP_SRC, blk_port)
try:
    for p in (sse_port, http_port, blk_port):
        if not wait_accept(p):
            raise SystemExit("host server on %d never came up" % p)

    # The editor injects the second MCP server inline; one URL is loopback (must be
    # forwarded), one is non-loopback (must NOT be -- it would leak host localhost).
    mcp_json = json.dumps({"mcpServers": {
        "emacs-tools": {"type": "http", "url": "http://localhost:%d/mcp/abc" % http_port},
        "blocked": {"type": "http", "url": "http://10.255.255.1:%d/mcp/xyz" % blk_port},
    }})
    host_env = {"TERM": "xterm-t13", "CLAUDE_CODE_SSE_PORT": str(sse_port)}

    proj = os.path.join(tmp, "proj"); os.makedirs(proj)
    out = os.path.join(proj, "claude_out")
    rc = rmod.run(AG, [], ["--mcp-config", mcp_json, "--print"],
                       config=parse_config({}), cwd=proj, env=host_env,
                       store=store, gateway=gw)
    I = kv(out)

    put("run_rc", rc == 0, "rc=%s" % rc)
    put("sse_bridge", int(I.get("SSE_EVENTS", "0") or 0) >= 3,
        "events=%s" % I.get("SSE_EVENTS"))
    put("http_mcp_reachable", I.get("HTTP_CODE") == "200",
        "code=%s url=%s" % (I.get("HTTP_CODE"), I.get("HTTP_URL")))
    put("blocked_port_parsed", I.get("BLK_PORT") == str(blk_port),
        "got=%s want=%d" % (I.get("BLK_PORT"), blk_port))
    # The blocked server is live on the host loopback...
    put("blocked_live_on_host", host_status(blk_port) == 200,
        "status=%s" % host_status(blk_port))
    # ...yet its non-loopback URL was not forwarded, so it is refused inside.
    put("blocked_not_forwarded",
        I.get("BLK_CODE") in ("000", "", None),
        "code=%s rc=%s" % (I.get("BLK_CODE"), I.get("BLK_RC")))
finally:
    for p in procs:
        p.terminate()
        try: p.wait(timeout=3)
        except Exception: p.kill()

with open(results_path, "w") as f:
    for k in sorted(checks):
        f.write("%s=%s\n" % (k, checks[k]))
print(tmp)
PY
)
rc=$?
[ "$rc" -eq 0 ] || fatal "mcp-ports driver failed (rc=$rc)"

echo "-- checks --"
status() { case "$1" in PASS*) echo PASS;; *) echo FAIL;; esac; }
while IFS='=' read -r k v; do
  [ -n "$k" ] || continue
  check "$k = $v" "$(status "$v")"
done < "$RESULTS"

echo "-----------"
cat <<'MANUAL'
-- manual editor step (not automatable here) --
  From an Emacs project buffer with claude-code-ide, launch claude through the
  wrapper: BOTH MCP channels must connect -- the `ide` ws diagnostics/diff channel
  AND the `emacs-tools` HTTP tools (treesit-info, imenu, project-info, xref...) --
  with no ConnectionRefused in ~/.cache/claude-cli-nodejs/<cwd>/mcp-logs-*. The
  automated checks above stand in for the bridge mechanics; this confirms the full
  editor handshake end to end.
MANUAL

if [ "$fail" -eq 0 ]; then
  echo "MCP-PORTS: PASS -- SSE + emacs-tools HTTP both bridged; non-loopback URL not forwarded."
  exit 0
else
  echo "MCP-PORTS: FAIL -- see checks above."; exit 1
fi
