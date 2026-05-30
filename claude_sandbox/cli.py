"""Argv dispatch for claude-sandbox.

Two surfaces:

* Subcommands -- ``setup`` / ``delete``. Anything else is the run path.
* Run path -- a *leading-block* parse: zero or more leading ``--mount PATH[:ro]``
  modifiers (ad-hoc per-session binds consumed by the wrapper); the **first
  non-wrapper token ends the block** and everything from there is forwarded to
  ``claude`` verbatim. An explicit ``--`` force-terminates wrapper parsing and is
  itself consumed (not forwarded).

The wrapper always operates on the current working directory.
"""

from __future__ import annotations

import sys
from collections import namedtuple

#: A leading ``--mount`` modifier. ``ro`` is True for a read-only bind.
Mount = namedtuple("Mount", ["path", "ro"])


class CliError(Exception):
    """A user-facing argument error (bad/missing ``--mount`` operand, etc.)."""


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
    """Split run-path argv into ``(mounts, claude_args)``.

    Consumes the leading block of ``--mount PATH[:ro]`` modifiers (and an
    optional terminating ``--``); the remainder is forwarded to ``claude``
    untouched.
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


# --- subcommand stubs (real logic lands in later tasks) ----------------------

def cmd_setup(args) -> int:
    """``claude-sandbox setup`` management subcommand (not yet implemented)."""
    print("claude-sandbox setup: not implemented")
    return 0


def cmd_delete(args) -> int:
    """``claude-sandbox delete`` management subcommand (not yet implemented)."""
    print("claude-sandbox delete: not implemented")
    return 0


def run_passthrough(mounts, claude_args) -> int:
    """Run path: launch ``claude`` in the sandbox (not yet implemented).

    For now prints the parsed mounts and the verbatim ``claude`` args so the
    routing is observable until the launcher exists.
    """
    binds = ", ".join(f"{m.path}{':ro' if m.ro else ''}" for m in mounts) or "(none)"
    print("claude-sandbox passthrough: not implemented")
    print(f"  mounts: {binds}")
    print(f"  claude args: {claude_args}")
    return 0


#: The recognized management subcommands. Any other argv goes to the run path.
SUBCOMMANDS = {
    "setup": cmd_setup,
    "delete": cmd_delete,
}


def dispatch(argv) -> int:
    """Route a program argv (excluding argv[0]) and return an exit code."""
    if argv and argv[0] in SUBCOMMANDS:
        return SUBCOMMANDS[argv[0]](argv[1:])
    mounts, claude_args = parse_run_args(argv)
    return run_passthrough(mounts, claude_args)


def main(argv=None) -> int:
    """Console entry point (``claude-sandbox``)."""
    if argv is None:
        argv = sys.argv[1:]
    try:
        return dispatch(argv)
    except CliError as exc:
        print(f"claude-sandbox: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
