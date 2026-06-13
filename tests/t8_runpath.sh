#!/usr/bin/env bash
# Run path end-to-end: drives the package's hot path (run.run) through real
# pasta+bwrap launches and checks that
#   - a missing store triggers exactly ONE auto-setup, and later launches take the
#     fast path with no install work (the store-identity stamp),
#   - a stamp that has drifted (version pin / schema / missing) is detected, and a
#     pinned-version drift rebuilds,
#   - a real claude session launches in the right context (cwd, args, identity,
#     launcher PATH),
#   - environment precedence holds: a context env overrides the global one, a
#     `forward` pulls a host var, an un-forwarded host var does NOT leak, and the
#     universal terminal/Anthropic baseline is carried in,
#   - two different cwds yield isolated sandboxes (neither sees the other), and
#   - a config edit (env + an added mount) changes the next launch with no rebuild.
# The store is built offline (the copy path) so nothing hits the network. The hot
# path is pasta-fronted, so this needs a default route like the boot driver.
# Exits 0 only if every check passes.
set -u

REPO=$(cd "$(dirname "$0")/.." && pwd)
export PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}"

fail=0
check() { printf '  [%-4s] %s\n' "$2" "$1"; [ "$2" = FAIL ] && fail=1; return 0; }
fatal() { printf 'FATAL: %s\n' "$*" >&2; exit 2; }

for b in bwrap pasta python3 ip id; do
  command -v "$b" >/dev/null || fatal "missing host tool: $b"
done

GW=$(ip route get 1.1.1.1 2>/dev/null | grep -oP 'via \K[0-9.]+' | head -1)
[ -n "$GW" ] || fatal "no default gateway found (the hot path is pasta-fronted)"

RESULTS=$(mktemp)
cleanup() { rm -f "$RESULTS"; rm -rf "${TMP:-}"; }
trap cleanup EXIT

printf '== run-path driver: gw=%s ==\n' "$GW"

# The driver performs every launch and assertion and writes `key=PASS|FAIL(...)`
# lines into $RESULTS; it prints the temp root it owns on stdout for cleanup.
TMP=$(python3 - "$RESULTS" "$GW" <<'PY'
import os, shutil, sys, tempfile
from agentbox import run as rmod, store as smod
from agentbox.agents import AGENTS
from agentbox.config import parse_config, SCHEMA_VERSION
from agentbox.sandbox import host_identity

AG = AGENTS["claude"]
results_path, gw = sys.argv[1], sys.argv[2]
checks = {}
def put(k, ok, detail=""):
    checks[k] = "PASS" if ok else ("FAIL(%s)" % detail if detail else "FAIL")

ident = host_identity()

tmp = tempfile.mkdtemp(prefix="box-t8.")
fakehost = os.path.join(tmp, "fakehost")
store = os.path.join(tmp, "store")            # intentionally absent at first
projA = os.path.join(tmp, "A", "proj"); os.makedirs(projA)
projB = os.path.join(tmp, "B", "proj"); os.makedirs(projB)
extra = os.path.join(tmp, "extra"); os.makedirs(extra)
open(os.path.join(extra, "marker"), "w").write("extra-content\n")

# A synthetic native ~/.local claude install whose "binary" is a probe: it records
# its args, cwd, identity, environment and a couple of path-visibility answers into
# the rw-bound cwd, then prints a version line. Built into the frozen store offline.
PROBE = r'''#!/bin/sh
R="$(pwd)/claude_out"
{
  echo "ARGS=$*"
  echo "CWD=$(pwd)"
  echo "WHOAMI=$(id -un)"
  echo "HOMEVAL=$HOME"
  echo "PATHVAL=$PATH"
  echo "SHARED=${SHARED-<unset>}"
  echo "ONLY_GLOBAL=${ONLY_GLOBAL-<unset>}"
  echo "ONLY_CTX=${ONLY_CTX-<unset>}"
  echo "T8_FWD=${T8_FWD-<unset>}"
  echo "T8_CTX_FWD=${T8_CTX_FWD-<unset>}"
  echo "T8_SECRET=${T8_SECRET-<unset>}"
  echo "T8_NEW=${T8_NEW-<unset>}"
  echo "TERMVAL=${TERM-<unset>}"
  echo "ANTH=${ANTHROPIC_TEST_T8-<unset>}"
  if [ -n "${OTHER_PROJ:-}" ] && [ -e "$OTHER_PROJ" ]; then echo "SEE_OTHER=yes"; else echo "SEE_OTHER=no"; fi
  if [ -n "${EXTRA_DIR:-}" ] && [ -e "$EXTRA_DIR" ]; then echo "SEE_EXTRA=yes"; else echo "SEE_EXTRA=no"; fi
} > "$R"
echo "STORE-9.9.9"
exit 0
'''
ver = "9.9.9"
vroot = os.path.join(fakehost, ".local", "share", "claude", "versions"); os.makedirs(vroot)
payload = os.path.join(vroot, ver)
open(payload, "w").write(PROBE); os.chmod(payload, 0o755)
binsrc = os.path.join(fakehost, ".local", "bin"); os.makedirs(binsrc)
os.symlink(payload, os.path.join(binsrc, "claude"))

# Offline auto-setup: the run path's store-build seam, copy-from-synthetic, counted.
install_count = [0]
def installer(s):
    install_count[0] += 1
    smod.install_store(AG, store=s, method="copy", source_home=fakehost)

# Host environment the run path reads for baseline + `forward` + (unused) SSE port.
# HOME/USER here are deliberately wrong: build_env must drop them so the sandbox
# identity wins. PATH is opt-in: the config below both sets a literal and forwards
# it, so the sandbox PATH becomes launcher + ~/.local/bin + literal + host PATH
# (deduped), with the launcher prefix still leading so a bare claude hits the store.
host_env = {
    "TERM": "xterm-t8",
    "ANTHROPIC_TEST_T8": "anth",
    "T8_FWD": "fwd-val",
    "T8_CTX_FWD": "ctxfwd-val",
    "T8_SECRET": "leak",
    # A realistic host PATH: a host-only dir plus the system dirs the probe's `id`
    # needs. Forwarding replaces the sandbox base wholesale, so dropping /usr/bin
    # here would leave coreutils off PATH.
    "HOME": "/wrong/home", "USER": "wrong-user",
    "PATH": "/host/only/bin:/usr/bin:/bin",
}

def make_config(other_proj, *, edited=False):
    glob_env = {
        "SHARED": "global", "ONLY_GLOBAL": "g",
        "OTHER_PROJ": other_proj, "EXTRA_DIR": extra,
        "PATH": "/literal/bin",
        "forward": ["T8_FWD", "PATH"],
    }
    ctx_env = {"SHARED": "ctx", "ONLY_CTX": "c", "forward": ["T8_CTX_FWD"]}
    data = {"env": dict(glob_env), "contexts": [
        {"name": "a", "when": [projA], "env": dict(ctx_env)}
    ]}
    if edited:
        data["env"]["T8_NEW"] = "new"                 # an added global env var
        data["contexts"][0]["env"]["SHARED"] = "ctx2"  # an edited context env var
        data["mounts"] = [{"path": extra, "mode": "ro"}]  # an added mount
    return parse_config(data)

def kv(path):
    out = {}
    if os.path.exists(path):
        for line in open(path):
            line = line.rstrip("\n")
            if "=" in line:
                k, v = line.split("=", 1); out[k] = v
    return out

def launch(label, cwd, config):
    out = os.path.join(cwd, "claude_out")
    try: os.remove(out)
    except FileNotFoundError: pass
    rc = rmod.run(
        AG, [], ["--marker", label],
        config=config, cwd=cwd, env=host_env,
        store=store, install=installer, gateway=gw,
    )
    return rc, kv(out)

# --- launch A: missing store -> one auto-setup; right context; env precedence ---
rcA, A = launch("A", projA, make_config(projB))
put("launch_a_rc", rcA == 0, "rc=%s" % rcA)
put("autosetup_built", smod.store_present(AG, store) and smod.installed_version(AG, store) == ver,
    "present=%s ver=%s" % (smod.store_present(AG, store), smod.installed_version(AG, store)))
put("ident", A.get("WHOAMI") == ident.user and A.get("HOMEVAL") == ident.home,
    "whoami=%s home=%s" % (A.get("WHOAMI"), A.get("HOMEVAL")))
put("args", A.get("ARGS") == "--marker A", A.get("ARGS"))
put("cwd", A.get("CWD") == projA, A.get("CWD"))
_pathv = (A.get("PATHVAL") or "").split(":")
put("launcher_path", _pathv[:2] == [smod.LAUNCHER_DIR, "%s/.local/bin" % ident.home], A.get("PATHVAL"))
# Opt-in PATH: literal prepended ahead of the forwarded host PATH, both present.
put("path_literal_before_host",
    "/literal/bin" in _pathv and "/host/only/bin" in _pathv
    and _pathv.index("/literal/bin") < _pathv.index("/host/only/bin"),
    A.get("PATHVAL"))
put("env_ctx_wins", A.get("SHARED") == "ctx", A.get("SHARED"))
put("env_global", A.get("ONLY_GLOBAL") == "g", A.get("ONLY_GLOBAL"))
put("env_ctx_only", A.get("ONLY_CTX") == "c", A.get("ONLY_CTX"))
put("env_fwd_global", A.get("T8_FWD") == "fwd-val", A.get("T8_FWD"))
put("env_fwd_ctx", A.get("T8_CTX_FWD") == "ctxfwd-val", A.get("T8_CTX_FWD"))
put("env_no_leak", A.get("T8_SECRET") == "<unset>", A.get("T8_SECRET"))
put("env_baseline_term", A.get("TERMVAL") == "xterm-t8", A.get("TERMVAL"))
put("env_baseline_prefix", A.get("ANTH") == "anth", A.get("ANTH"))
put("iso_a_no_other", A.get("SEE_OTHER") == "no", A.get("SEE_OTHER"))
put("pre_edit_no_extra", A.get("SEE_EXTRA") == "no", A.get("SEE_EXTRA"))

# --- launch B: store present -> fast path; default context; isolation ----------
rcB, B = launch("B", projB, make_config(projA))
put("launch_b_rc", rcB == 0, "rc=%s" % rcB)
put("fastpath_no_install", install_count[0] == 1, "installs=%s" % install_count[0])
put("env_default_shared", B.get("SHARED") == "global", B.get("SHARED"))
put("env_default_no_ctx", B.get("ONLY_CTX") == "<unset>", B.get("ONLY_CTX"))
put("env_default_no_ctx_fwd", B.get("T8_CTX_FWD") == "<unset>", B.get("T8_CTX_FWD"))
put("iso_b_no_other", B.get("SEE_OTHER") == "no", B.get("SEE_OTHER"))

# --- launch C: edited config -> change takes effect, still no rebuild ----------
rcC, C = launch("C", projA, make_config(projB, edited=True))
put("launch_c_rc", rcC == 0, "rc=%s" % rcC)
put("edit_env_ctx", C.get("SHARED") == "ctx2", C.get("SHARED"))
put("edit_env_added", C.get("T8_NEW") == "new", C.get("T8_NEW"))
put("edit_mount_added", C.get("SEE_EXTRA") == "yes", C.get("SEE_EXTRA"))
put("no_rebuild_after_edit", install_count[0] == 1, "installs=%s" % install_count[0])

# --- pure stamp-freshness checks (no launch) -----------------------------------
nopin = parse_config({})
pin_ok = parse_config({"agents": {"claude": {"version": ver}}})
pin_bad = parse_config({"agents": {"claude": {"version": "1.2.3"}}})
put("match_nopin", smod.store_matches(AG, nopin, store=store) is True)
put("match_pin_ok", smod.store_matches(AG, pin_ok, store=store) is True)
put("drift_pin", smod.store_matches(AG, pin_bad, store=store) is False)

drift_count = [0]
def installer2(s):
    drift_count[0] += 1
    smod.install_store(AG, store=s, method="copy", source_home=fakehost)
smod.ensure_store(AG, pin_bad, store=store, install=installer2)
put("drift_rebuilds", drift_count[0] == 1, "installs=%s" % drift_count[0])

os.remove(os.path.join(store, smod.STAMP_NAME))
put("drift_unstamped", smod.store_matches(AG, nopin, store=store) is False)
smod.write_stamp(store, {"schema_version": SCHEMA_VERSION + 1, "version": ver, "method": "copy"})
put("drift_schema", smod.store_matches(AG, nopin, store=store) is False)

with open(results_path, "w") as f:
    for k in sorted(checks):
        f.write("%s=%s\n" % (k, checks[k]))

print(tmp)
PY
)
rc=$?
[ "$rc" -eq 0 ] || fatal "run-path driver failed (rc=$rc)"

echo "-- checks --"
status() { case "$1" in PASS*) echo PASS;; *) echo FAIL;; esac; }
while IFS='=' read -r k v; do
  [ -n "$k" ] || continue
  check "$k = $v" "$(status "$v")"
done < "$RESULTS"

echo "-----------"
if [ "$fail" -eq 0 ]; then echo "RUNPATH: PASS -- hot path, stamp/auto-setup, env precedence and isolation hold."; exit 0
else echo "RUNPATH: FAIL -- see checks above."; exit 1; fi
