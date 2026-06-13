#!/usr/bin/env bash
# CLI wiring: the `box` console entry point really drives the store/preflight/run path.
# Earlier drivers exercised the run/setup/delete calls directly; this one goes
# through `cli.main`/`cli.dispatch` (the binary's own dispatch) and checks that
#   - `setup --from-host` runs the real setup path: it builds the frozen store
#     offline (the copy path), exits 0, prints the `frozen claude store ready`
#     line, and leaves a present, version-stamped, frozen store -- never the old
#     `not implemented` stub text,
#   - `delete` answering N keeps the store and exits 1; answering Y removes it and
#     exits 0 (real `input`-driven confirm, fed via a replaced stdin),
#   - an agent shim dispatches to `run.run` with the parsed leading-block
#     mounts and the verbatim claude args, propagating its exit code,
#   - the subcommand surface is still exactly {setup, delete}, and no stub text
#     ("not implemented") survives anywhere in cli.py.
# Everything runs under a redirected HOME holding a synthetic native install, so
# the real host store is never touched and no real claude install is needed. setup
# does only preflight + an offline copy + the shim report -- no sandbox launch --
# and the run-path check is seam-mocked, so this needs no default route/network.
# Exits 0 only if every check passes.
set -u

REPO=$(cd "$(dirname "$0")/.." && pwd)
export PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}"

fail=0
check() { printf '  [%-4s] %s\n' "$2" "$1"; [ "$2" = FAIL ] && fail=1; return 0; }
fatal() { printf 'FATAL: %s\n' "$*" >&2; exit 2; }

for b in bwrap pasta python3; do
  command -v "$b" >/dev/null || fatal "missing host tool: $b"
done

TMPHOME=$(mktemp -d)
trap 'rm -rf "$TMPHOME"' EXIT
echo "== cli driver: HOME=$TMPHOME (synthetic native install + redirected store) =="

# Redirect identity/config/store entirely under the temp HOME: setup derives both
# the store dest (store_dir(AG)) and the copy source (expanduser('~')) from $HOME,
# and config lands under $XDG_CONFIG_HOME.
export HOME="$TMPHOME"
export XDG_CONFIG_HOME="$TMPHOME/.config"

HOME="$TMPHOME" XDG_CONFIG_HOME="$TMPHOME/.config" python3 - "$TMPHOME" <<'PY'
import contextlib, io, json, os, sys
from pathlib import Path

from agentbox import cli, run as rmod, store as smod
from agentbox.agents import AGENTS
from agentbox.cli import Mount

AG = AGENTS["claude"]
home = sys.argv[1]
ver = "9.9.9"

res = []
def put(name, ok, detail=""):
    res.append((name, bool(ok)))
    print("  [%-4s] %s%s" % ("PASS" if ok else "FAIL", name,
                             "" if ok else " -- " + detail))

# A synthetic native ~/.local install under the redirected HOME: a self-contained
# "binary" plus the bin/claude -> versions/<ver> symlink (what the copy path reads).
vroot = os.path.join(home, ".local", "share", "claude", "versions")
os.makedirs(vroot)
payload = os.path.join(vroot, ver)
with open(payload, "w") as f:
    f.write("#!/bin/sh\necho STORE-9.9.9\n")
os.chmod(payload, 0o755)
binsrc = os.path.join(home, ".local", "bin")
os.makedirs(binsrc)
os.symlink(payload, os.path.join(binsrc, "claude"))

store = smod.store_dir(AG)  # = $HOME/.local/share/box/claude/store

# --- setup --from-host, through cli.main --------------------------------------
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc_setup = cli.main(["claude", "setup", "--from-host"])
out = buf.getvalue()
put("setup_rc", rc_setup == 0, "rc=%r" % rc_setup)
put("setup_ready_msg", "frozen claude store ready" in out, repr(out[-200:]))
put("setup_no_stub", "not implemented" not in out, repr(out))
put("setup_store_present", smod.store_present(AG, store),
    "present=%s at %s" % (smod.store_present(AG, store), store))
put("setup_version", smod.installed_version(AG, store) == ver,
    "ver=%r" % smod.installed_version(AG, store))
stamp = smod.read_stamp(store) or {}
put("setup_stamped_copy", stamp.get("method") == "copy", "stamp=%r" % stamp)
cfg = Path(store) / ".claude.json"
frozen = cfg.exists() and json.loads(cfg.read_text()).get("autoUpdates") is False
put("setup_frozen", frozen, "claude.json=%r" % (cfg.read_text() if cfg.exists() else None))

# --- delete N (keep) then Y (remove), through cli.main ------------------------
# cmd_delete -> store.delete(AG) with the default input-driven confirm; feed the
# answer via a replaced sys.stdin (input() reads a line from it when non-interactive).
real_stdin = sys.stdin
sys.stdin = io.StringIO("n\n")
with contextlib.redirect_stdout(io.StringIO()):
    rc_del_n = cli.main(["claude", "delete"])
put("delete_abort_rc", rc_del_n == 1, "rc=%r" % rc_del_n)
put("delete_abort_keeps_store", smod.store_present(AG, store))

sys.stdin = io.StringIO("y\n")
with contextlib.redirect_stdout(io.StringIO()):
    rc_del_y = cli.main(["claude", "delete"])
sys.stdin = real_stdin
put("delete_rc", rc_del_y == 0, "rc=%r" % rc_del_y)
put("delete_removes_store",
    not smod.store_present(AG, store) and not os.path.exists(store),
    "still=%s" % os.path.exists(store))

# --- agent shim dispatches to run.run (seam-mocked) ---------------------------
seen = {}
rmod.run = lambda agent, mounts, claude_args: (
    seen.update(agent=agent, mounts=list(mounts), args=list(claude_args)) or 7)
rc_run = cli.dispatch(["--mount", "/data:ro", "--", "-p", "hi"], prog="claude")
put("run_dispatches", seen.get("args") == ["-p", "hi"]
    and seen.get("mounts") == [Mount("/data", True)]
    and seen.get("agent") is AG, "seen=%r" % seen)
put("run_rc_propagated", rc_run == 7, "rc=%r" % rc_run)

# --- surface + no stub text ---------------------------------------------------
put("surface_setup_delete_only", set(cli.SUBCOMMANDS) == {"setup", "delete"},
    "%s" % sorted(cli.SUBCOMMANDS))
src = Path(cli.__file__).read_text()
put("no_stub_text_in_cli", "not implemented" not in src,
    "cli.py still carries stub text")

print("-----------")
ok = all(v for _, v in res)
print("CLI: %s -- box entry point drives the store/run path end to end."
      % ("PASS" if ok else "FAIL"))
sys.exit(0 if ok else 1)
PY
rc=$?
[ "$rc" -eq 0 ] || fail=1

echo "-----------"
if [ "$fail" -eq 0 ]; then
  echo "T12 CLI WIRING: PASS"
  exit 0
else
  echo "T12 CLI WIRING: FAIL"
  exit 1
fi
