#!/usr/bin/env bash
# `delete` + persistent caches via ordinary mounts. Drives the package through real
# pasta+bwrap launches and the `delete` store call, and checks that
#   - each launch is an ephemeral sandbox: a write to a tmpfs surface (/tmp) is
#     gone next launch and never reaches the host,
#   - a configured rw `[[mounts]]` cache dir is an ordinary host-backed mount: a
#     file written inside one launch survives into the next and lands on the host,
#   - the store is frozen (autoUpdates:false) and built exactly once -- the second
#     launch takes the fast path with no install work (refreshed only by setup),
#   - `delete` aborts on `[y/N]`=N (store kept) and removes the store on y, leaving
#     the host-owned cache untouched, and is idempotent on an absent store,
#   - after a delete the next launch re-`setup`s the store (one fresh install),
#     while the cache dir outlives the whole delete+re-setup cycle, and
#   - the CLI surface is exactly {setup, delete} -- no `gc` anywhere.
# The store is built offline (the copy path) so nothing hits the network. The hot
# path is pasta-fronted, so this needs a default route like the run-path driver.
# Exits 0 only if every check passes.
set -u

REPO=$(cd "$(dirname "$0")/.." && pwd)
export PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}"

fail=0
check() { printf '  [%-4s] %s\n' "$2" "$1"; [ "$2" = FAIL ] && fail=1; return 0; }
fatal() { printf 'FATAL: %s\n' "$*" >&2; exit 2; }

for b in bwrap pasta python3 ip; do
  command -v "$b" >/dev/null || fatal "missing host tool: $b"
done

GW=$(ip route get 1.1.1.1 2>/dev/null | grep -oP 'via \K[0-9.]+' | head -1)
[ -n "$GW" ] || fatal "no default gateway found (the hot path is pasta-fronted)"

RESULTS=$(mktemp)
cleanup() { rm -f "$RESULTS" /tmp/box-t11-ephemeral; rm -rf "${TMP:-}"; }
trap cleanup EXIT

printf '== delete driver: gw=%s ==\n' "$GW"

# The driver runs every launch / delete / assertion and writes `key=PASS|FAIL(...)`
# lines into $RESULTS; it prints the temp root it owns on stdout for cleanup.
TMP=$(python3 - "$RESULTS" "$GW" <<'PY'
import json, os, sys, tempfile
from agentbox import cli, run as rmod, store as smod
from agentbox.agents import AGENTS
from agentbox.config import parse_config
from agentbox.sandbox import host_identity

results_path, gw = sys.argv[1], sys.argv[2]
checks = {}
def put(k, ok, detail=""):
    checks[k] = "PASS" if ok else ("FAIL(%s)" % detail if detail else "FAIL")

AG = AGENTS["claude"]
ident = host_identity()

tmp = tempfile.mkdtemp(prefix="box-t11.")
fakehost = os.path.join(tmp, "fakehost")
store = os.path.join(tmp, "store")          # intentionally absent at first
proj = os.path.join(tmp, "proj"); os.makedirs(proj)
cache = os.path.join(tmp, "cache"); os.makedirs(cache)   # an ordinary rw mount
cache_marker = os.path.join(cache, "marker")
eph_path = "/tmp/box-t11-ephemeral"  # a tmpfs surface inside the sandbox
try:
    os.remove(eph_path)                       # a write here must never reach the host
except FileNotFoundError:
    pass

# A synthetic native ~/.local claude install whose "binary" is a probe: it reports
# whether the ephemeral tmpfs marker and the cache marker are present (then writes
# both), so successive launches reveal what persists. Built into the store offline.
PROBE = r'''#!/bin/sh
R="$(pwd)/claude_out"
{
  if [ -e "$EPH_PATH" ]; then echo "EPH_SEEN=yes"; else echo "EPH_SEEN=no"; fi
  : > "$EPH_PATH"
  if [ -e "$CACHE_DIR/marker" ]; then echo "CACHE_SEEN=yes"; else echo "CACHE_SEEN=no"; fi
  echo launched >> "$CACHE_DIR/marker"
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

# HOME/USER/PATH here are deliberately wrong: the sandbox sets its own identity.
host_env = {"TERM": "xterm-t11", "HOME": "/wrong/home", "USER": "wrong", "PATH": "/wrong/bin"}

# A config with one rw `[[mounts]]` cache dir and the probe's two env vars. No
# `[cache]` table exists -- caches are just ordinary mounts.
def make_config():
    return parse_config({
        "env": {"EPH_PATH": eph_path, "CACHE_DIR": cache},
        "mounts": [{"path": cache, "mode": "rw"}],
    })

def kv(path):
    out = {}
    if os.path.exists(path):
        for line in open(path):
            line = line.rstrip("\n")
            if "=" in line:
                k, v = line.split("=", 1); out[k] = v
    return out

def launch():
    out = os.path.join(proj, "claude_out")
    try: os.remove(out)
    except FileNotFoundError: pass
    rc = rmod.run(
        AG, [], [],
        config=make_config(), cwd=proj, env=host_env,
        store=store, install=installer, gateway=gw,
    )
    return rc, kv(out)

# --- launch 1: missing store -> one auto-setup; fresh sandbox; empty cache -----
rc1, L1 = launch()
put("launch1_rc", rc1 == 0, "rc=%s" % rc1)
put("autosetup_built", smod.store_present(AG, store), "present=%s" % smod.store_present(AG, store))
put("autosetup_once", install_count[0] == 1, "installs=%s" % install_count[0])
put("eph_fresh_1", L1.get("EPH_SEEN") == "no", L1.get("EPH_SEEN"))
put("cache_empty_1", L1.get("CACHE_SEEN") == "no", L1.get("CACHE_SEEN"))
try:
    fr = json.load(open(os.path.join(store, ".claude.json")))
    put("frozen", fr.get("autoUpdates") is False, "%s" % fr)
except Exception as e:
    put("frozen", False, "%s" % e)
put("cache_on_host", os.path.exists(cache_marker))
put("eph_no_host_leak", not os.path.exists(eph_path))

# --- launch 2: store present -> fast path; cache survives; still ephemeral -----
rc2, L2 = launch()
put("launch2_rc", rc2 == 0, "rc=%s" % rc2)
put("fastpath_no_install", install_count[0] == 1, "installs=%s" % install_count[0])
put("cache_survives", L2.get("CACHE_SEEN") == "yes", L2.get("CACHE_SEEN"))
put("eph_fresh_2", L2.get("EPH_SEEN") == "no", L2.get("EPH_SEEN"))

# --- delete: `[y/N]`=N aborts and keeps the store ------------------------------
rc_abort = smod.delete(AG, store=store, confirm=lambda p: "n", out=lambda *a: None)
put("delete_abort_rc", rc_abort == 1, "rc=%s" % rc_abort)
put("delete_abort_keeps_store", smod.store_present(AG, store))

# --- delete: `[y/N]`=y removes the store, leaving the host cache untouched ------
rc_del = smod.delete(AG, store=store, confirm=lambda p: "y", out=lambda *a: None)
put("delete_rc", rc_del == 0, "rc=%s" % rc_del)
put("delete_removes_store", not smod.store_present(AG, store) and not os.path.exists(store))
put("delete_keeps_caches", os.path.exists(cache_marker))

# --- delete: idempotent on an already-absent store -----------------------------
rc_again = smod.delete(AG, store=store, confirm=lambda p: "y", out=lambda *a: None)
put("delete_idempotent_rc", rc_again == 0, "rc=%s" % rc_again)
put("delete_idempotent_no_store", not smod.store_present(AG, store))

# --- launch 3: deleted store -> the next launch re-setups it -------------------
rc3, L3 = launch()
put("launch3_rc", rc3 == 0, "rc=%s" % rc3)
put("resetup_after_delete",
    smod.store_present(AG, store) and install_count[0] == 2,
    "present=%s installs=%s" % (smod.store_present(AG, store), install_count[0]))
put("cache_outlives_delete", L3.get("CACHE_SEEN") == "yes", L3.get("CACHE_SEEN"))

# --- launch 4: the re-setup store is reused on the fast path -------------------
rc4, L4 = launch()
put("launch4_rc", rc4 == 0, "rc=%s" % rc4)
put("resetup_then_fastpath", install_count[0] == 2, "installs=%s" % install_count[0])

# --- the CLI surface is exactly {setup, delete}; no `gc` anywhere --------------
put("surface_setup_delete_only", set(cli.SUBCOMMANDS) == {"setup", "delete"},
    "%s" % sorted(cli.SUBCOMMANDS))
put("no_gc_subcommand", "gc" not in cli.SUBCOMMANDS)
put("no_gc_in_store_or_run", not hasattr(smod, "gc") and not hasattr(rmod, "gc"))

with open(results_path, "w") as f:
    for k in sorted(checks):
        f.write("%s=%s\n" % (k, checks[k]))

print(tmp)
PY
)
rc=$?
[ "$rc" -eq 0 ] || fatal "delete driver failed (rc=$rc)"

echo "-- checks --"
status() { case "$1" in PASS*) echo PASS;; *) echo FAIL;; esac; }
while IFS='=' read -r k v; do
  [ -n "$k" ] || continue
  check "$k = $v" "$(status "$v")"
done < "$RESULTS"

echo "-----------"
if [ "$fail" -eq 0 ]; then echo "DELETE: PASS -- ephemeral sandbox, cache-via-mounts, delete + re-setup, no gc."; exit 0
else echo "DELETE: FAIL -- see checks above."; exit 1; fi
