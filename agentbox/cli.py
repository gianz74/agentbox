"""Argv dispatch for box.

``box`` is a multi-call binary (like busybox/git): the invoked name selects the
agent. Two surfaces:

* **Shims** -- ``claude`` / ``copilot`` symlinks pointing at this entry. The
  invoked name (``basename(argv[0])``) names the agent; everything after is *pure
  passthrough* to that agent's run path (a *leading-block* parse of ad-hoc
  ``--mount PATH[:ro]`` modifiers, then the first non-wrapper token ends the block
  and the remainder is forwarded to the agent verbatim; an explicit ``--``
  force-terminates wrapper parsing and is itself consumed).
* **The explicit entry** -- ``box <agent> setup|delete`` for management. ``box``
  with no/unknown agent errors, listing the available agents.

The wrapper always operates on the current working directory.
"""

from __future__ import annotations

import os
import sys
from collections import namedtuple

from . import preflight, run, store
from .agents import AGENTS
from .config import ConfigError, load_user_config
from .mounts import MountError
from .net import NetworkError
from .sandbox import SandboxError

#: The console entry-point name. Not an agent command; selects the management
#: surface (``box <agent> setup|delete``).
WRAPPER_ENTRY = "box"

#: A leading ``--mount`` modifier. ``ro`` is True for a read-only bind.
Mount = namedtuple("Mount", ["path", "ro"])


class CliError(Exception):
    """A user-facing argument error (bad ``--mount`` operand, unknown agent, etc.)."""


def parse_mount(spec: str) -> Mount:
    """Parse a single ``PATH[:ro]`` mount spec.

    A trailing ``:ro`` marks the bind read-only; otherwise it is read-write.
    """
    if spec.endswith(":ro"):
        path = spec[:-3]
        if not path:
            raise CliError("--mount needs a PATH before ':ro'")
        return Mount(path, True)
    if not spec:
        raise CliError("--mount needs a non-empty PATH")
    return Mount(spec, False)


def parse_run_args(argv):
    """Split run-path argv into ``(mounts, agent_args)``.

    Consumes the leading block of ``--mount PATH[:ro]`` modifiers (and an optional
    terminating ``--``); the remainder is forwarded to the agent untouched.
    """
    mounts = []
    i, n = 0, len(argv)
    while i < n:
        tok = argv[i]
        if tok == "--mount":
            if i + 1 >= n:
                raise CliError("--mount requires a PATH[:ro] operand")
            mounts.append(parse_mount(argv[i + 1]))
            i += 2
            continue
        if tok == "--":
            # Explicit terminator: consume it, forward everything after.
            i += 1
            break
        # First non-wrapper token: the leading block ends here.
        break
    return mounts, list(argv[i:])


# --- agent selection ----------------------------------------------------------


def agent_for_command(name: str):
    """The agent whose shim ``command`` is *name*, or ``None`` (e.g. for ``box``)."""
    for agent in AGENTS.values():
        if agent.command == name:
            return agent
    return None


def _available_agents() -> str:
    return ", ".join(sorted(AGENTS))


# --- management subcommands (scoped to an agent) ------------------------------


def cmd_setup(agent, args) -> int:
    """``box <agent> setup`` -- build/refresh the agent's frozen store.

    Accepts the opt-in ``--from-host`` flag (build by copying the host's native
    install instead of a fresh native install); loads the user config so a version
    pin and the store-shadow guard apply, then defers to :func:`preflight.setup`.
    """
    from_host = False
    rest = []
    for tok in args:
        if tok == "--from-host":
            from_host = True
        else:
            rest.append(tok)
    if rest:
        raise CliError(f"{agent.command} setup: unexpected argument(s): {' '.join(rest)}")
    try:
        config = load_user_config()
    except ConfigError as exc:
        print(f"box: {exc}", file=sys.stderr)
        return 2
    return preflight.setup(agent, config, from_host=from_host)


def cmd_delete(agent, args) -> int:
    """``box <agent> delete`` -- remove the agent's frozen store after a confirm."""
    if args:
        raise CliError(f"{agent.command} delete: unexpected argument(s): {' '.join(args)}")
    return store.delete(agent)


#: The recognized management subcommands, keyed by name -> ``handler(agent, args)``.
SUBCOMMANDS = {
    "setup": cmd_setup,
    "delete": cmd_delete,
}


def run_passthrough(agent, mounts, agent_args) -> int:
    """Run path: launch a sandboxed *agent* session for the current directory.

    The parsed leading-block ``--mount`` modifiers and the verbatim agent args are
    forwarded to :func:`run.run`, which loads the config, resolves the cwd context,
    ensures the frozen store, and execs the store binary in a fresh sandbox.
    """
    return run.run(agent, mounts, agent_args)


# --- dispatch -----------------------------------------------------------------


def dispatch_box(argv) -> int:
    """The ``box`` management entry: ``box <agent> setup|delete``."""
    if not argv:
        raise CliError(
            f"usage: box <agent> <setup|delete> (agents: {_available_agents()})"
        )
    name, rest = argv[0], argv[1:]
    agent = AGENTS.get(name)
    if agent is None:
        raise CliError(f"unknown agent {name!r} (agents: {_available_agents()})")
    sub = rest[0] if rest else None
    if sub not in SUBCOMMANDS:
        got = repr(sub) if sub is not None else "nothing"
        raise CliError(
            f"box {name}: expected a subcommand (setup|delete), got {got}"
        )
    return SUBCOMMANDS[sub](agent, rest[1:])


def dispatch(argv, *, prog: str = WRAPPER_ENTRY) -> int:
    """Route a program argv (excluding argv[0]) and return an exit code.

    *prog* is the invoked name (``basename(argv[0])``): an agent command means a
    shim (pure passthrough to that agent's run path); anything else (notably
    ``box``) means the management entry.
    """
    agent = agent_for_command(prog)
    if agent is not None:
        mounts, agent_args = parse_run_args(argv)
        return run_passthrough(agent, mounts, agent_args)
    return dispatch_box(argv)


def main(argv=None) -> int:
    """Console entry point (``box`` and its agent shims)."""
    if argv is None:
        argv = sys.argv[1:]
    prog = os.path.basename(sys.argv[0]) if sys.argv and sys.argv[0] else WRAPPER_ENTRY
    try:
        return dispatch(argv, prog=prog)
    except CliError as exc:
        print(f"box: {exc}", file=sys.stderr)
        return 2
    except (store.StoreError, SandboxError, NetworkError, MountError) as exc:
        # The expected-failure domain errors of the run/setup paths: a store build
        # failing late (installer non-zero, copy unsupported, missing source), a
        # missing bwrap, a missing pasta or absent route, or a refused cwd. Surface
        # each as a clean non-zero exit, not an uncaught traceback.
        print(f"box: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
