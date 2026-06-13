#!/usr/bin/env bash
# Mount-set rendering: masking, whitelist default-deny, absent-source skip, and
# claude-binds-last -- against a real bwrap launch. A context exposes a project
# directory (parity, read-write) with a `secrets` sub-path excluded, plus a mount
# pointing at a path that does not exist on this machine; a sibling directory is in
# no mount. With the read-only claude store binds appended last, the launch checks
# inside that:
#   - the excluded sub-path is an empty dir and the real secret is gone (masking),
#   - non-excluded content of the same bind shows through,
#   - the unmounted sibling is absent (whitelist default-deny),
#   - the absent-on-machine mount was silently skipped (no abort, path absent),
#   - bare and absolute-path `claude` resolve to the store binary (binds survived).
# Host-side it also confirms the mask was sandbox-only (the real secret is intact).
# This exercises render + bwrap directly (no pasta / network needed). Exits 0 only
# if every check passes.
set -u

REPO=$(cd "$(dirname "$0")/.." && pwd)
export PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}"

fail=0
check() { printf '  [%-4s] %s\n' "$2" "$1"; [ "$2" = FAIL ] && fail=1; return 0; }
fatal() { printf 'FATAL: %s\n' "$*" >&2; exit 2; }

for b in bwrap python3; do
  command -v "$b" >/dev/null || fatal "missing host tool: $b"
done

BASE=$(mktemp -d)
PROJ="$BASE/proj"
SIB="$BASE/sibling"
ABSENT="$BASE/not-on-this-machine"
mkdir -p "$PROJ/secrets" "$SIB"
printf 'TOPSECRET-CONTENT\n' > "$PROJ/secrets/topsecret"
printf 'public-content\n' > "$PROJ/public"
printf 'sibling-content\n' > "$SIB/file"
cleanup() { rm -rf "$BASE" "${TMP:-}"; }
trap cleanup EXIT

# The in-sandbox probe writes key=value results into the rw-bound project dir.
PROBE=$(cat <<'PROBE'
set -u; R="$PROJ/results"; : > "$R"
put() { printf "%s=%s\n" "$1" "$2" >> "$R"; }

# masking: the excluded sub-path is present but empty (an empty tmpfs overmount)
if [ -d "$PROJ/secrets" ] && [ -z "$(ls -A "$PROJ/secrets" 2>/dev/null)" ]; then
  put mask_empty PASS; else put mask_empty "FAIL($(ls -A "$PROJ/secrets" 2>/dev/null))"; fi

# masking: the real secret underneath is gone / unreadable from inside
[ -e "$PROJ/secrets/topsecret" ] && put mask_unreadable "FAIL(secret visible)" \
  || put mask_unreadable PASS

# non-excluded content of the same bind shows through
[ -f "$PROJ/public" ] && put public_visible PASS || put public_visible FAIL

# whitelist default-deny: a sibling in no mount is absent
[ -e "$SIB" ] && put sibling_absent "FAIL(sibling present)" || put sibling_absent PASS

# absent-on-machine mount silently skipped via -try (we run => no launch abort)
[ -e "$ABSENT" ] && put absent_skipped "FAIL(absent present)" || put absent_skipped PASS

# claude binds survive (emitted last): bare `claude` resolves to the store binary
out=$(claude --version 2>/dev/null | head -1)
[ "$out" = "$EXP_STORE" ] && put claude_present PASS || put claude_present "FAIL(got '$out')"

# and the store binary is reachable by absolute path too
outa=$("$HOME/.local/bin/claude" --version 2>/dev/null | head -1)
[ "$outa" = "$EXP_STORE" ] && put claude_abs PASS || put claude_abs "FAIL(got '$outa')"
PROBE
)

printf '== masking driver: proj=%s ==\n' "$PROJ"

# Render the resolved mount set and boot a real sandbox through the package;
# in-sandbox results land in $PROJ/results. Prints the temp root for cleanup.
TMP=$(python3 - "$PROJ" "$SIB" "$ABSENT" "$PROBE" <<'PY'
import os, subprocess, sys, tempfile
from agentbox import sandbox, store as smod
from agentbox.agents import AGENTS
from agentbox.config import parse_config
from agentbox.mounts import render, resolve
from agentbox.sandbox import SandboxSpec, host_identity

AG = AGENTS["claude"]
proj, sib, absent, probe = sys.argv[1:5]
ident = host_identity()
home = ident.home
ver = "9.9.9"

tmp = tempfile.mkdtemp(prefix="box-t7.")
fakehost = os.path.join(tmp, "fakehost")
store = os.path.join(tmp, "store")
launcher = os.path.join(tmp, "launcher"); os.makedirs(launcher)
etc = os.path.join(tmp, "etc"); os.makedirs(etc)

# A synthetic native ~/.local claude install, frozen into a private store, so the
# launch has a real claude to bind last (the offline copy path keeps this local).
vroot = os.path.join(fakehost, ".local", "share", "claude", "versions")
os.makedirs(vroot)
payload = os.path.join(vroot, ver)
with open(payload, "w") as f:
    f.write("#!/bin/sh\necho STORE-9.9.9\n")
os.chmod(payload, 0o755)
binsrc = os.path.join(fakehost, ".local", "bin"); os.makedirs(binsrc)
os.symlink(payload, os.path.join(binsrc, "claude"))
smod.install_store(AG, store=store, method="copy", source_home=fakehost)
sl = smod.store_launch(AG, home, launcher, store=store, base_path="/usr/bin:/bin")

# A context whose mount set is the project (parity, rw) with `secrets` masked, plus
# a mount at a path absent on this machine (rendered *-bind-try, so it is skipped).
# The sibling dir is in no mount, so default-deny leaves it absent.
cfg = parse_config(
    {
        "contexts": [
            {
                "name": "t",
                "when": [proj],
                "mounts": [
                    {"path": proj, "exclude": ["secrets"]},
                    {"path": absent},
                ],
            }
        ]
    }
)
res = resolve(cfg, proj, home=home)
rendered = render(res.mounts)

# User binds first; the read-only store binds appended last so nothing configured
# can shadow the in-sandbox claude. Masks ride in spec.tmpfs (emitted after binds).
spec = SandboxSpec(
    identity=ident,
    argv=("/bin/bash", "-c", probe),
    binds=(*rendered.binds, *sl.binds),
    tmpfs=rendered.masks,
    setenv={"PROJ": proj, "SIB": sib, "ABSENT": absent, "EXP_STORE": "STORE-9.9.9"},
    path=sl.path,
    chdir=proj,
)
rc = subprocess.run(sandbox.build_argv(spec, etc_dir=etc)).returncode
print(tmp)
sys.exit(rc)
PY
)
rc=$?
[ "$rc" -eq 0 ] || fatal "render / sandbox launch failed (rc=$rc)"

echo "-- checks --"
val() { sed -n "s/^$1=//p" "$2" 2>/dev/null | head -1; }
status() { case "$1" in PASS*) echo PASS;; *) echo FAIL;; esac; }
for c in mask_empty mask_unreadable public_visible sibling_absent absent_skipped \
         claude_present claude_abs; do
  v=$(val "$c" "$PROJ/results"); check "$c ${v:+= $v}" "$(status "$v")"
done

# Host-side: the mask was a sandbox-only overmount; the real secret is untouched.
if grep -q TOPSECRET-CONTENT "$PROJ/secrets/topsecret" 2>/dev/null; then
  check "mask_host_intact" PASS; else check "mask_host_intact" FAIL; fi

echo "-----------"
if [ "$fail" -eq 0 ]; then echo "MASKING: PASS -- render binds + masking + whitelist + claude-last hold end-to-end."; exit 0
else echo "MASKING: FAIL -- see checks above."; exit 1; fi
