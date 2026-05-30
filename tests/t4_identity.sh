#!/usr/bin/env bash
# Identity verification for the sandbox. Boots a real bwrap launch (built through
# the package's build_spec/build_argv) and checks that the constructed-passwd
# identity model holds end-to-end:
#   - whoami / uid / $HOME inside match the requested identity,
#   - the constructed passwd is resolvable by uid via files alone,
#   - a writable tmpfs $HOME skeleton is present,
#   - a file written into a rw bind is owned by the user on BOTH sides
#     (ownership parity via the single-uid map),
#   - the uid/gid maps are single-id ranges (no /etc/subuid range consulted),
#   - name resolution is files-only with no sssd/NSS dependency, and
#   - a federated `user@REALM` username flows through verbatim.
# Identity is independent of the network layer, so this runs bwrap directly
# (sharing the host network namespace) -- no pasta or default route needed.
# Exits 0 only if every check passes.
set -u

REPO=$(cd "$(dirname "$0")/.." && pwd)
export PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}"

fail=0
check() { printf '  [%-4s] %s\n' "$2" "$1"; [ "$2" = FAIL ] && fail=1; return 0; }
fatal() { printf 'FATAL: %s\n' "$*" >&2; exit 2; }

for b in bwrap python3 stat; do
  command -v "$b" >/dev/null || fatal "missing host tool: $b"
done

# in-sandbox probe: writes key=value results into the rw-bound cwd (/work)
PROBE=$(cat <<'PROBE'
set -u; R=/work/results; : > "$R"
put() { printf "%s=%s\n" "$1" "$2" >> "$R"; }

# identity: name / uid / home match what was requested
if [ "$(id -un)" = "$EXP_USER" ] && [ "$(id -u)" = "$EXP_UID" ] && [ "$HOME" = "$EXP_HOME" ]; then
  put ident PASS; else put ident "FAIL(got $(id -un)/$(id -u)/$HOME)"; fi

# the constructed passwd is resolvable by uid through files alone
if [ "$(getent passwd "$(id -u)" | cut -d: -f1)" = "$EXP_USER" ]; then
  put getent PASS; else put getent "FAIL($(getent passwd "$(id -u)"))"; fi

# writable tmpfs $HOME skeleton
if touch "$HOME/.probe" 2>/dev/null; then put home PASS; else put home FAIL; fi

# ownership parity (inside): a file in the rw bind is owned by the sandbox user
echo "written-by-$(id -un)" > /work/owned 2>/dev/null
if [ "$(stat -c %U /work/owned 2>/dev/null)" = "$(id -un)" ]; then
  put own_in PASS; else put own_in "FAIL($(stat -c %U /work/owned 2>/dev/null))"; fi

# single-id maps: one line each, range count 1 -> no subuid range is consulted
um=$(cat /proc/self/uid_map); gm=$(cat /proc/self/gid_map)
ulines=$(printf '%s\n' "$um" | wc -l); glines=$(printf '%s\n' "$gm" | wc -l)
set -- $um; ucount=${3:-}; set -- $gm; gcount=${3:-}
if [ "$ulines" -eq 1 ] && [ "$glines" -eq 1 ] && [ "$ucount" = 1 ] && [ "$gcount" = 1 ]; then
  put singlemap PASS; else put singlemap "FAIL(uid_map='$um' gid_map='$gm')"; fi

# files-only name resolution, no sssd: nsswitch trimmed and no sss runtime present
if grep -q '^passwd:[[:space:]]*files[[:space:]]*$' /etc/nsswitch.conf \
   && ! grep -qi 'sss' /etc/nsswitch.conf \
   && [ ! -e /var/lib/sss ] && [ ! -e /var/run/nscd/socket ]; then
  put nosssd PASS; else put nosssd FAIL; fi
PROBE
)

# launch the probe under a requested identity; leaves results in $1/results
launch() {  # $1=projdir  $2=mode(host|at)
  python3 - "$1" "$2" "$PROBE" <<'PY'
import sys, tempfile, subprocess
from claude_sandbox import sandbox
from claude_sandbox.sandbox import build_spec, Bind, Identity, host_identity

proj, mode, probe = sys.argv[1], sys.argv[2], sys.argv[3]
if mode == "at":
    ident = Identity(user="ci-user@EXAMPLE.test", home="/home/ci-user@EXAMPLE.test")
    override = ident                       # exercise the federated-username path
else:
    ident = host_identity()
    override = None                        # default build_spec to the host user

spec = build_spec(
    ("/bin/bash", "-c", probe),
    binds=(Bind(proj, "/work", mode="rw"),),
    setenv={"EXP_USER": ident.user, "EXP_UID": "1000", "EXP_HOME": ident.home},
    chdir="/work",
    identity=override,
)
with tempfile.TemporaryDirectory(prefix="claude-sandbox-etc.") as etc:
    sys.exit(subprocess.run(sandbox.build_argv(spec, etc_dir=etc)).returncode)
PY
}

aggregate() {  # $1=label  $2=projdir
  echo "-- identity: $1 --"
  for c in ident getent home own_in singlemap nosssd; do
    v=$(sed -n "s/^$c=//p" "$2/results" 2>/dev/null | head -1)
    case "$v" in PASS*) st=PASS;; *) st=FAIL;; esac
    check "$c ${v:+= $v}" "$st"
  done
  # ownership parity observed from the host side: the single-uid map means the
  # in-sandbox write lands owned by the real host user, whatever the in-ns name.
  owner=$(stat -c %U "$2/owned" 2>/dev/null)
  if [ "$owner" = "$USER" ]; then check "ownership-parity (host owner=$USER)" PASS
  else check "ownership-parity (host owner=${owner:-<missing>})" FAIL; fi
}

P_HOST=$(mktemp -d); P_AT=$(mktemp -d)
cleanup() { rm -rf "$P_HOST" "$P_AT"; }
trap cleanup EXIT

printf '== identity driver: host=%s @=%s ==\n' "$P_HOST" "$P_AT"

launch "$P_HOST" host || fatal "host-identity sandbox launch failed (rc=$?)"
aggregate "host user '$USER'" "$P_HOST"

launch "$P_AT" at || fatal "federated-username sandbox launch failed (rc=$?)"
aggregate "federated 'ci-user@EXAMPLE.test'" "$P_AT"

echo "-----------"
if [ "$fail" -eq 0 ]; then echo "IDENTITY: PASS -- constructed-passwd identity model holds end-to-end."; exit 0
else echo "IDENTITY: FAIL -- see checks above."; exit 1; fi
