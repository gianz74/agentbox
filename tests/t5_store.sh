#!/usr/bin/env bash
# Frozen claude store + recursion guard. Builds a frozen store from a synthetic
# native install (the copy-from-host path), then boots a real bwrap launch and
# checks that:
#   - the store builds, freezes (autoUpdates:false), and carries an identity stamp,
#   - a bare `claude` resolves to the store binary even with a competing
#     `claude`->wrapper shim sitting on a system PATH dir outside $HOME
#     (the recursion guard, via the PATH-prepended private launcher),
#   - the store binary is also reachable by absolute path,
#   - ~/.claude and ~/.claude.json are present as shared host binds,
#   - the store is read-only from inside a session, and untouched after it.
# Store/recursion is independent of the network layer, so this drives bwrap
# directly (no pasta / default route needed). Exits 0 only if every check passes.
set -u

REPO=$(cd "$(dirname "$0")/.." && pwd)
export PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}"

fail=0
check() { printf '  [%-4s] %s\n' "$2" "$1"; [ "$2" = FAIL ] && fail=1; return 0; }
fatal() { printf 'FATAL: %s\n' "$*" >&2; exit 2; }

for b in bwrap python3 stat; do
  command -v "$b" >/dev/null || fatal "missing host tool: $b"
done
# shared host binds the run path expects; required for this check to be meaningful
[ -d "$HOME/.claude" ] && [ -e "$HOME/.claude.json" ] \
  || fatal "need ~/.claude and ~/.claude.json present (shared host binds)"

PROJ=$(mktemp -d)   # rw-bound cwd: the driver writes host-side results, the probe in-sandbox ones
cleanup() { rm -rf "$PROJ" "${TMP:-}"; }
trap cleanup EXIT

# The in-sandbox probe writes key=value results into the rw-bound cwd (/work).
PROBE=$(cat <<'PROBE'
set -u; R=/work/results; : > "$R"
put() { printf "%s=%s\n" "$1" "$2" >> "$R"; }

# bare `claude` resolves to the store binary (private launcher wins on PATH)
out=$(claude --version 2>/dev/null | head -1)
[ "$out" = "$EXP_STORE" ] && put store_bare PASS || put store_bare "FAIL(got '$out')"

# the store binary is also reachable by absolute path
outa=$("$HOME/.local/bin/claude" --version 2>/dev/null | head -1)
[ "$outa" = "$EXP_STORE" ] && put store_abs PASS || put store_abs "FAIL(got '$outa')"

# the competing claude->wrapper shim really is present further down PATH, so the
# bare-`claude` result above is a genuine recursion-guard win and not a no-op
outs=$(/shimbin/claude --version 2>/dev/null | head -1)
[ "$outs" = "$EXP_SHIM" ] && put shim_present PASS || put shim_present "FAIL(got '$outs')"

# ~/.claude + ~/.claude.json present as shared host binds
{ [ -d "$HOME/.claude" ] && [ -e "$HOME/.claude.json" ]; } && put shared_binds PASS || put shared_binds FAIL

# the store is read-only: neither the binary nor the payload tree accepts writes
if ( : > "$HOME/.local/bin/claude" ) 2>/dev/null \
   || ( : > "$HOME/.local/share/claude/INTRUDER" ) 2>/dev/null; then
  put store_ro "FAIL(store writable from inside)"; else put store_ro PASS; fi
PROBE
)

printf '== store driver: proj=%s ==\n' "$PROJ"

# Build the store and boot the sandbox through the package; host-side results land
# in $PROJ/host, in-sandbox results in $PROJ/results. Prints the temp root on
# stdout so the trap can clean it up.
TMP=$(python3 - "$PROJ" "$PROBE" <<'PY'
import hashlib, json, os, subprocess, sys, tempfile
from agentbox import config as cfgmod, lifecycle, sandbox
from agentbox.sandbox import Bind, SandboxSpec, host_identity

proj, probe = sys.argv[1], sys.argv[2]
host = os.path.join(proj, "host")
open(host, "w").close()
def put(k, v):
    with open(host, "a") as f:
        f.write(f"{k}={v}\n")

ident = host_identity()
home = ident.home
ver = "9.9.9"

tmp = tempfile.mkdtemp(prefix="box-t5.")
fakehost = os.path.join(tmp, "fakehost")
store = os.path.join(tmp, "store")
launcher = os.path.join(tmp, "launcher"); os.makedirs(launcher)
shimdir = os.path.join(tmp, "shimbin"); os.makedirs(shimdir)
etc = os.path.join(tmp, "etc"); os.makedirs(etc)

# A synthetic native ~/.local install: a single self-contained "binary" (a script
# that identifies itself) plus the bin/claude -> versions/<v> symlink.
vroot = os.path.join(fakehost, ".local", "share", "claude", "versions")
os.makedirs(vroot)
payload = os.path.join(vroot, ver)
with open(payload, "w") as f:
    f.write("#!/bin/sh\necho STORE-9.9.9\n")
os.chmod(payload, 0o755)
binsrc = os.path.join(fakehost, ".local", "bin"); os.makedirs(binsrc)
os.symlink(payload, os.path.join(binsrc, "claude"))

# Build the frozen store from that install (the opt-in offline copy path).
lifecycle.install_store(store=store, method="copy", source_home=fakehost)

# host-side: build / freeze / stamp
copied = os.path.join(store, ".local", "share", "claude", "versions", ver)
ok_build = (
    lifecycle.store_present(store)
    and lifecycle.installed_version(store) == ver
    and os.path.exists(copied)
)
put("build", "PASS" if ok_build else "FAIL")
try:
    fr = json.load(open(os.path.join(store, ".claude.json")))
    put("freeze", "PASS" if fr.get("autoUpdates") is False else f"FAIL({fr})")
except Exception as e:
    put("freeze", f"FAIL({e})")
st = lifecycle.read_stamp(store)
want = {"schema_version": cfgmod.SCHEMA_VERSION, "version": ver, "method": "copy"}
put("stamp", "PASS" if st == want else f"FAIL({st})")

# A competing claude->wrapper shim on a system PATH dir outside $HOME.
shim = os.path.join(shimdir, "claude")
with open(shim, "w") as f:
    f.write("#!/bin/sh\necho SHIM-WRAPPER\n")
os.chmod(shim, 0o755)

# Store binds + private launcher + PATH (launcher prepended ahead of the shim dir).
sl = lifecycle.store_launch(home, launcher, store=store, base_path="/shimbin:/usr/bin:/bin")

spec = SandboxSpec(
    identity=ident,
    argv=("/bin/bash", "-c", probe),
    binds=(
        *sl.binds,
        Bind(shimdir, "/shimbin", mode="ro"),
        Bind(f"{home}/.claude", f"{home}/.claude", mode="ro"),
        Bind(f"{home}/.claude.json", f"{home}/.claude.json", mode="ro"),
        Bind(proj, "/work", mode="rw"),
    ),
    setenv={
        "EXP_USER": ident.user, "EXP_UID": str(ident.uid), "EXP_HOME": home,
        "EXP_STORE": "STORE-9.9.9", "EXP_SHIM": "SHIM-WRAPPER",
    },
    path=sl.path,
    chdir="/work",
)

def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        h.update(f.read())
    return h.hexdigest()

before = sha(copied)
rc = subprocess.run(sandbox.build_argv(spec, etc_dir=etc)).returncode
put("frozen_ro", "PASS" if sha(copied) == before else "FAIL(store changed)")

print(tmp)
sys.exit(rc)
PY
)
rc=$?
[ "$rc" -eq 0 ] || fatal "store driver / sandbox launch failed (rc=$rc)"

echo "-- checks --"
val() { sed -n "s/^$1=//p" "$2" 2>/dev/null | head -1; }
status() { case "$1" in PASS*) echo PASS;; *) echo FAIL;; esac; }
for c in build freeze stamp frozen_ro; do
  v=$(val "$c" "$PROJ/host"); check "$c ${v:+= $v}" "$(status "$v")"
done
for c in store_bare store_abs shim_present shared_binds store_ro; do
  v=$(val "$c" "$PROJ/results"); check "$c ${v:+= $v}" "$(status "$v")"
done

echo "-----------"
if [ "$fail" -eq 0 ]; then echo "STORE: PASS -- frozen store + recursion guard hold end-to-end."; exit 0
else echo "STORE: FAIL -- see checks above."; exit 1; fi
