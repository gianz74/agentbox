#!/usr/bin/env bash
# Boot smoke test for the copilot agent: drives the real run path
# (agentbox.run.run) to launch the frozen copilot store inside a bwrap+pasta
# sandbox and checks that it
#   - ensures/builds the per-agent frozen store (native install, no payload tree),
#   - boots the sandbox and execs the store binary by absolute path through the
#     private launcher (recursion guard), and
#   - runs `copilot --version` to completion, printing its version banner.
#
# `copilot --version` needs neither auth nor network, so this isolates the launch
# mechanism. The self-update freeze (COPILOT_AUTO_UPDATE=false) is unit-tested in
# tests/test_run.py; the network posture (host-loopback blocked except forwarded
# ports) is agent-independent and covered by tests/t3_boot.sh -- run that too to
# confirm isolation holds under the copilot launch.
#
# Needs a host with bwrap + pasta + unprivileged userns (cannot run inside the box
# sandbox itself: pasta has no /dev/net/tun there). Throwaway hand-driven harness,
# not package code. Exits 0 only if all checks pass.
set -u

REPO=$(cd "$(dirname "$0")/.." && pwd)
export PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}"

fail=0
check() { printf '  [%-4s] %s\n' "$2" "$1"; [ "$2" = FAIL ] && fail=1; return 0; }
fatal() { printf 'FATAL: %s\n' "$*" >&2; exit 2; }

for b in bwrap pasta curl python3; do
  command -v "$b" >/dev/null || fatal "missing host tool: $b"
done

PROJ=$(mktemp -d)              # a safe rw cwd to launch in (not a store path / $HOME)
cleanup() { rm -rf "$PROJ"; }
trap cleanup EXIT

printf '== copilot boot smoke: proj=%s ==\n' "$PROJ"

# Drive the package run path: ensure the store, then boot the sandbox and exec
# `copilot --version`. Its stdout flows through to here; we capture and inspect it.
out=$(cd "$PROJ" && python3 - <<'PY' 2>&1
import sys
from agentbox.agents import AGENTS
from agentbox import run

rc = run.run(AGENTS["copilot"], agent_args=["--version"])
print(f"__rc__={rc}")
sys.exit(0)
PY
)
printf '%s\n' "$out" | sed 's/^/    /'

# the launcher boots and copilot runs to completion, printing its version banner
printf '%s' "$out" | grep -q "GitHub Copilot CLI" \
  && check "boot+exec: copilot --version printed its banner" PASS \
  || check "boot+exec: no copilot version banner (launch failed?)" FAIL

# the run path returned the agent's own exit code (0 for --version)
printf '%s' "$out" | grep -q "__rc__=0" \
  && check "exit-code propagated (rc=0)" PASS \
  || check "exit-code not 0 (see output above)" FAIL

echo "-----------"
if [ "$fail" -eq 0 ]; then
  echo "COPILOT BOOT: PASS -- store + launcher + sandboxed exec hold for copilot."
  echo "(run tests/t3_boot.sh for the agent-independent network-isolation posture.)"
  exit 0
else
  echo "COPILOT BOOT: FAIL -- see checks above."
  exit 1
fi
