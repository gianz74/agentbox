#!/usr/bin/env bash
# MCP / IDE bridge under the isolated network. Drives the package hot path
# (lifecycle.run) through real pasta+bwrap launches and checks everything the
# bridge can verify without a live editor:
#   - the SSE crux: a host text/event-stream on a loopback port is reachable from
#     inside the sandbox ONLY through the pasta `-T` forward of that one port,
#   - the IDE lockfile is reconciled with the sandbox: its pid is rewritten to a
#     live, uid-matched in-sandbox sentinel (what claude validates) and a
#     workspaceFolders trailing slash is stripped,
#   - --mcp-config files are staged and the operand rewritten so the host path
#     resolves inside,
#   - concurrency: two contexts launched at once with different ~/.ssh aliases
#     each see only their own — no collision across the per-launch namespaces.
# The pure logic (staging, lockfile patch, the bootstrap end to end) is covered
# first by the unit suite. The store is built offline (copy path), so nothing but
# the deliberate SSE round-trip touches the network; the hot path is pasta-fronted
# so this needs a default route. Exits 0 only if every check passes.
#
# The one thing that needs a human is a real editor session: from an Emacs project
# buffer claude-code-ide must connect (MCP tools available, diagnostics flow). That
# manual step is printed at the end; everything above stands in for it automatically.
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

echo "== unit suite: staging, lockfile patch, bootstrap =="
python3 -m pytest -q "$REPO/tests/test_mcp.py" || fatal "MCP unit suite failed"

RESULTS=$(mktemp)
cleanup() { rm -f "$RESULTS"; rm -rf "${TMP:-}"; }
trap cleanup EXIT

printf '== ide-bridge driver: gw=%s ==\n' "$GW"

# The driver runs every launch + assertion and writes `key=PASS|FAIL(...)` lines
# into $RESULTS; it prints the temp root it owns on stdout for cleanup.
TMP=$(python3 - "$RESULTS" "$GW" <<'PY'
import json, os, socket, sys, tempfile, threading
from agentbox import lifecycle, mcp
from agentbox.config import parse_config
from agentbox.sandbox import SANDBOX_UID, host_identity

results_path, gw = sys.argv[1], sys.argv[2]
checks = {}
def put(k, ok, detail=""):
    checks[k] = "PASS" if ok else ("FAIL(%s)" % detail if detail else "FAIL")

ident = host_identity()
tmp = tempfile.mkdtemp(prefix="box-t9.")
fakehost = os.path.join(tmp, "fakehost")
store = os.path.join(tmp, "store")

# A synthetic native claude whose "binary" is a probe: it records its args, the
# (rewritten) --mcp-config operand and its contents, the forwarded SSE event
# count, the reconciled lockfile's sentinel (alive + uid), and what it sees under
# ~/.ssh -- all into the rw-bound cwd -- then prints a version line.
PROBE = r'''#!/bin/sh
OUT="$(pwd)/claude_out"
mcp=""; prev=""
for a in "$@"; do
  [ "$prev" = "--mcp-config" ] && mcp="$a"
  case "$a" in --mcp-config=*) mcp="${a#--mcp-config=}";; esac
  prev="$a"
done
{
  echo "ARGS=$*"
  echo "MCP_ARG=${mcp:-<none>}"
  if [ -n "$mcp" ] && [ -f "$mcp" ]; then echo "MCP_READ=$(cat "$mcp")"; else echo "MCP_READ=<none>"; fi
  if [ -n "${CLAUDE_CODE_SSE_PORT:-}" ]; then
    ev=$(curl -sN --max-time 8 "http://127.0.0.1:$CLAUDE_CODE_SSE_PORT/" 2>/dev/null | grep -c "^data:")
    echo "SSE_EVENTS=${ev:-0}"
    LOCK="$HOME/.claude/ide/$CLAUDE_CODE_SSE_PORT.lock"
    pid=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("pid",""))' "$LOCK" 2>/dev/null)
    echo "SENTINEL_PID=${pid:-<none>}"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then echo "SENTINEL_ALIVE=yes"; else echo "SENTINEL_ALIVE=no"; fi
    if [ -n "$pid" ] && [ -r "/proc/$pid/status" ]; then
      echo "SENTINEL_UID=$(awk '/^Uid:/{print $2}' "/proc/$pid/status")"
    else echo "SENTINEL_UID=<none>"; fi
  fi
  if [ -r "$HOME/.ssh/marker" ]; then echo "SSH_SEES=$(cat "$HOME/.ssh/marker")"; else echo "SSH_SEES=<none>"; fi
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

# Build the frozen store once, offline; every launch then takes the fast path.
lifecycle.install_store(store=store, method="copy", source_home=fakehost)

def kv(path):
    out = {}
    if os.path.exists(path):
        for line in open(path):
            line = line.rstrip("\n")
            if "=" in line:
                k, v = line.split("=", 1); out[k] = v
    return out

# --- a host SSE server, reachable only through pasta -T -----------------------
def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

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
import subprocess
sse_port = free_port()
sse = subprocess.Popen([sys.executable, "-c", SSE_SRC, str(sse_port)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    # wait for the host server to accept
    for _ in range(40):
        try:
            socket.create_connection(("127.0.0.1", sse_port), timeout=0.2).close(); break
        except OSError:
            import time; time.sleep(0.1)

    # --- IDE launch: SSE bridge + lockfile reconciliation + mcp staging --------
    projIDE = os.path.join(tmp, "ide", "proj"); os.makedirs(projIDE)
    dotclaude = os.path.join(tmp, "ide", "dotclaude")
    ide_dir = os.path.join(dotclaude, "ide"); os.makedirs(ide_dir)
    lock_path = os.path.join(ide_dir, "%d.lock" % sse_port)
    BOGUS_PID = 999999
    json.dump({"pid": BOGUS_PID, "workspaceFolders": [projIDE + "/"], "transport": "ws"},
              open(lock_path, "w"))

    mcp_cfg = os.path.join(tmp, "servers.json")
    MCP_BODY = '{"mcpServers": {}}'
    open(mcp_cfg, "w").write(MCP_BODY)

    # Bind the fake ~/.claude (carrying the lockfile) at the real $HOME/.claude.
    home_claude = os.path.join(ident.home, ".claude")
    ide_config = parse_config({"mounts": [{"path": home_claude, "from": dotclaude, "mode": "rw"}]})
    # The editor exports the SSE port; the run path reads it from the launch env.
    host_env = {"TERM": "xterm-t9", "CLAUDE_CODE_SSE_PORT": str(sse_port)}

    out = os.path.join(projIDE, "claude_out")
    try: os.remove(out)
    except FileNotFoundError: pass
    rc = lifecycle.run([], ["--mcp-config", mcp_cfg, "--print"],
                       config=ide_config, cwd=projIDE, env=host_env,
                       store=store, gateway=gw)
    I = kv(out)
    put("ide_rc", rc == 0, "rc=%s" % rc)
    put("sse_bridge", I.get("SSE_EVENTS") not in (None, "0") and int(I.get("SSE_EVENTS", "0")) >= 3,
        "events=%s" % I.get("SSE_EVENTS"))
    put("mcp_arg_rewritten", (I.get("MCP_ARG") or "").startswith(mcp.MCP_STAGE_DIR), I.get("MCP_ARG"))
    put("mcp_read", I.get("MCP_READ") == MCP_BODY, I.get("MCP_READ"))
    put("sentinel_alive", I.get("SENTINEL_ALIVE") == "yes", I.get("SENTINEL_ALIVE"))
    put("sentinel_uid", I.get("SENTINEL_UID") == str(SANDBOX_UID), I.get("SENTINEL_UID"))

    # Lockfile, observed from the host side (the bind wrote through).
    patched = json.load(open(lock_path))
    put("lock_pid_rewritten", patched.get("pid") not in (None, BOGUS_PID),
        "pid=%s" % patched.get("pid"))
    put("lock_pid_matches_sentinel", str(patched.get("pid")) == I.get("SENTINEL_PID"),
        "host=%s probe=%s" % (patched.get("pid"), I.get("SENTINEL_PID")))
    put("lock_folder_normalized", patched.get("workspaceFolders") == [projIDE],
        "%s" % patched.get("workspaceFolders"))

    # --- staging, as a pure check -------------------------------------------
    with tempfile.TemporaryDirectory() as sd:
        args, binds = mcp.stage_mcp_configs(["--mcp-config", mcp_cfg], sd)
        staged_ok = (
            args == ("--mcp-config", "%s/servers.json" % mcp.MCP_STAGE_DIR)
            and len(binds) == 1
            and open(os.path.join(sd, "servers.json")).read() == MCP_BODY
        )
    put("stage_pure", staged_ok)

    # --- concurrency: two contexts, distinct ~/.ssh, launched at once ---------
    def conc(tag):
        proj = os.path.join(tmp, "conc", tag, "proj"); os.makedirs(proj)
        ssh = os.path.join(tmp, "conc", tag, "ssh"); os.makedirs(ssh)
        open(os.path.join(ssh, "marker"), "w").write("ssh-%s\n" % tag)
        home_ssh = os.path.join(ident.home, ".ssh")
        cfg = parse_config({"mounts": [{"path": home_ssh, "from": ssh, "mode": "ro"}]})
        o = os.path.join(proj, "claude_out")
        rc = lifecycle.run([], ["--marker", tag], config=cfg, cwd=proj,
                           env={"TERM": "xterm-t9"}, store=store, gateway=gw)
        return rc, kv(o)

    box = {}
    def runner(tag): box[tag] = conc(tag)
    ta = threading.Thread(target=runner, args=("a",))
    tb = threading.Thread(target=runner, args=("b",))
    ta.start(); tb.start(); ta.join(); tb.join()
    rcA, A = box.get("a", (1, {}))
    rcB, B = box.get("b", (1, {}))
    put("conc_rc", rcA == 0 and rcB == 0, "a=%s b=%s" % (rcA, rcB))
    put("conc_a_sees_a", A.get("SSH_SEES") == "ssh-a", A.get("SSH_SEES"))
    put("conc_b_sees_b", B.get("SSH_SEES") == "ssh-b", B.get("SSH_SEES"))
    put("conc_no_collision", A.get("SSH_SEES") != B.get("SSH_SEES"),
        "a=%s b=%s" % (A.get("SSH_SEES"), B.get("SSH_SEES")))
finally:
    sse.terminate()
    try: sse.wait(timeout=3)
    except Exception: sse.kill()

with open(results_path, "w") as f:
    for k in sorted(checks):
        f.write("%s=%s\n" % (k, checks[k]))
print(tmp)
PY
)
rc=$?
[ "$rc" -eq 0 ] || fatal "ide-bridge driver failed (rc=$rc)"

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
  wrapper: the IDE must connect (MCP tools listed, diagnostics flowing) over the
  forwarded SSE port, and two project buffers under different contexts must run
  side by side without a ~/.ssh collision. The automated checks above stand in
  for the bridge mechanics; this confirms the real editor handshake end to end.
MANUAL

if [ "$fail" -eq 0 ]; then echo "IDE-BRIDGE: PASS -- SSE forward, lockfile reconciliation, mcp staging and concurrency hold."; exit 0
else echo "IDE-BRIDGE: FAIL -- see checks above."; exit 1; fi
