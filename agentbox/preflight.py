"""Host-readiness preflight, the user-facing shim resolution, and ``setup``.

``setup`` is the management entry that builds an agent's frozen store. It first
runs a host-readiness preflight (unprivileged user namespaces, ``bwrap``,
``pasta``) and, after building the store, prints how to point the user-facing
``<command>`` at this wrapper -- detect and instruct, never mutating the host.
The store build/freeze/stamp itself lives in :mod:`agentbox.store`; this module
is the setup-side: host checks, the shim report, and the orchestration.

The shim resolution is keyed on the agent's ``command`` (e.g. ``claude``): a
shim placed on PATH ahead of the real binary routes the command through the
sandbox; the absolute-path exec on the run path makes this robust wherever the
shim lives.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass

from . import net, sandbox, store
from .config import agent_version
from .mounts import MountError, guard_store_shadow

# The console entry point a shim must point at. A ``<command>`` shim placed on
# PATH ahead of the real binary routes it through the sandbox; the absolute-path
# exec on the run path makes this robust wherever the shim lives.
WRAPPER_ENTRY = "box"

# The shim's preferred parent directory, relative to $HOME. A directory under the
# home keeps the shim out of the shared read-only host userspace, so it is never
# carried into the sandbox.
SHIM_SLOT_DIR = "bin"

# apt packages backing the host-readiness binaries. Packages are a host concern:
# a tool missing inside the sandbox means a tool missing on the host.
_BWRAP_PACKAGE = "bubblewrap"
_PASTA_PACKAGE = "passt"


@dataclass(frozen=True)
class Check:
    """One host-readiness check. ``fix`` is the instruction printed when the
    check fails (``None`` when it passes -- a good host stays silent)."""

    name: str
    ok: bool
    fix: str | None = None


def check_bwrap(*, which=shutil.which) -> Check:
    """``bwrap`` must be installed for the sandbox to start."""
    if which(sandbox.BWRAP) is not None:
        return Check("bwrap", True)
    return Check(
        "bwrap",
        False,
        f"{sandbox.BWRAP} is not installed, so the sandbox cannot start. "
        f"Install it on the host: sudo apt install {_BWRAP_PACKAGE}",
    )


def check_pasta(*, which=shutil.which) -> Check:
    """``pasta`` must be installed for the sandbox to have a network."""
    if which(net.PASTA) is not None:
        return Check("pasta", True)
    return Check(
        "pasta",
        False,
        f"{net.PASTA} is not installed, so the sandbox has no network. "
        f"Install it on the host: sudo apt install {_PASTA_PACKAGE}",
    )


def probe_userns(*, run=subprocess.run) -> bool:
    """True if unprivileged user namespaces work.

    Runs ``bwrap --unshare-user --ro-bind / / /bin/true``: a host root bound
    read-only so a real binary is reachable, executed inside a fresh unprivileged
    user namespace. A clean exit means the namespace was created and entered; a
    restricted host (the sysctl unset, or no permitting AppArmor profile) makes
    bwrap fail to create it, which the caller turns into guidance rather than an
    opaque bwrap error. (A bare ``--unshare-user true`` would always fail -- with
    nothing bound there is no ``true`` to exec even when the namespace succeeds.)
    """
    try:
        proc = run(
            [sandbox.BWRAP, "--unshare-user", "--ro-bind", "/", "/", "/bin/true"],
            capture_output=True,
            text=True,
        )
    except OSError:
        return False
    return proc.returncode == 0


def check_userns(*, probe=probe_userns) -> Check:
    """Unprivileged user namespaces must be permitted for the sandbox to start."""
    if probe():
        return Check("userns", True)
    return Check(
        "userns",
        False,
        "unprivileged user namespaces are restricted on this host, so the "
        "sandbox cannot start. Enable them as root, for example:\n"
        "    sysctl -w kernel.unprivileged_userns_clone=1\n"
        "or permit your user in the host's AppArmor userns policy. box "
        "will not change the sysctl or the AppArmor policy for you.",
    )


def preflight(*, which=shutil.which, userns_probe=probe_userns) -> list[Check]:
    """Run the host-readiness checks in order. ``bwrap`` is checked first because
    the user-namespace probe needs it; when it is absent the probe is skipped (its
    result would be meaningless)."""
    bwrap = check_bwrap(which=which)
    checks = [bwrap]
    if bwrap.ok:
        checks.append(check_userns(probe=userns_probe))
    checks.append(check_pasta(which=which))
    return checks


def report_preflight(checks, *, out=print) -> bool:
    """Print the fix for every failed check; return True when the host is ready (a
    good host prints nothing)."""
    failures = [c for c in checks if not c.ok]
    for c in failures:
        out(c.fix)
    return not failures


@dataclass(frozen=True)
class ShimStatus:
    """How the user-facing ``<command>`` resolves on the user's ``PATH``.

    ``command`` is the agent's command name (``claude``). ``resolved`` is the
    absolute path a bare ``<command>`` runs (``None`` if none is found);
    ``is_wrapper`` is True when that is this wrapper's entry point. ``slot_path`` is
    the preferred shim location (``~/bin/<command>``); ``slot_taken`` flags a
    non-wrapper file already sitting there (e.g. a leftover shim). ``wrapper_entry``
    is where ``box`` itself lives, used in the suggested commands.
    """

    command: str
    resolved: str | None
    is_wrapper: bool
    slot_path: str
    slot_taken: bool
    wrapper_entry: str | None


def _resolves_to_wrapper(candidate, wrapper_entry, realpath) -> bool:
    """True if *candidate* (a path, or ``None``) is this wrapper: it resolves to a
    file named ``box`` or to the same file as *wrapper_entry*."""
    if candidate is None:
        return False
    real = realpath(candidate)
    if os.path.basename(real) == WRAPPER_ENTRY:
        return True
    return wrapper_entry is not None and real == realpath(wrapper_entry)


def resolve_shim(
    command: str,
    path: str,
    *,
    home: str,
    which=shutil.which,
    realpath=os.path.realpath,
    wrapper_entry: str | None = None,
) -> ShimStatus:
    """Resolve how *command* runs given the search *path* and the user's *home*.

    *path* is a ``PATH``-style string; *wrapper_entry* is where ``box`` lives
    (defaulting to a lookup on the real ``PATH``). Pure but for the injected
    lookups, so it is exercised with a temporary ``PATH`` and on-disk fixtures.
    """
    if wrapper_entry is None:
        wrapper_entry = which(WRAPPER_ENTRY)
    resolved = which(command, path=path)
    slot_path = os.path.join(home, SHIM_SLOT_DIR, command)
    slot_present = os.path.lexists(slot_path)
    slot_is_wrapper = _resolves_to_wrapper(
        slot_path if slot_present else None, wrapper_entry, realpath
    )
    return ShimStatus(
        command=command,
        resolved=resolved,
        is_wrapper=_resolves_to_wrapper(resolved, wrapper_entry, realpath),
        slot_path=slot_path,
        slot_taken=slot_present and not slot_is_wrapper,
        wrapper_entry=wrapper_entry,
    )


def shim_guidance(status: ShimStatus) -> list[str]:
    """The lines to print so ``<command>`` routes through this wrapper. Empty when
    it already does (a correctly shimmed host stays silent). Suggests the commands;
    setup never runs them."""
    if status.is_wrapper:
        return []

    command = status.command
    entry = status.wrapper_entry or WRAPPER_ENTRY
    slot = status.slot_path
    slot_dir = os.path.dirname(slot)
    lines: list[str] = []

    if status.resolved is None:
        lines.append(f"`{command}` is not on your PATH. Point it at this wrapper:")
    elif status.resolved == slot:
        lines.append(
            f"`{command}` resolves to {status.resolved}, which is not this "
            "wrapper. Repoint it:"
        )
    else:
        lines.append(
            f"`{command}` resolves to {status.resolved}, which is not this "
            "wrapper. Shadow it with a shim earlier on PATH:"
        )

    lines.append(f"    ln -sf {entry} {slot}")
    if status.resolved != slot:
        lines.append(
            f'    # keep {slot_dir} ahead on PATH, e.g. in ~/.profile: '
            f'export PATH="{slot_dir}:$PATH"'
        )
    if status.slot_taken and status.resolved != slot:
        lines.append(
            f"    # note: {slot} already exists and is not this wrapper; the "
            "command above replaces it"
        )
    lines.append(
        "box will not create the shim or edit your shell config -- run "
        "the command yourself."
    )
    return lines


def report_shim(status: ShimStatus, *, out=print) -> bool:
    """Print shim guidance (if any); return True when guidance was printed."""
    lines = shim_guidance(status)
    for line in lines:
        out(line)
    return bool(lines)


# --- the setup command -------------------------------------------------------


def setup(
    agent,
    config=None,
    *,
    from_host: bool = False,
    path: str | None = None,
    home: str | None = None,
) -> int:
    """Build (or rebuild) *agent*'s frozen store after a host-readiness check.

    Runs the preflight (unprivileged user namespaces, ``bwrap``, ``pasta``) and
    exits with guidance if the host is not ready; refuses a config whose mounts
    would shadow the store; then builds the store -- a native install honoring an
    optional version pin, or the opt-in copy-from-host path (*from_host*) -- and
    prints how to point the ``<command>`` at this wrapper if it does not already.
    Mutates nothing on the host but the private store.
    """
    h = os.path.expanduser("~") if home is None else home

    if not report_preflight(preflight()):
        return 1

    if config is not None:
        try:
            guard_store_shadow(config, agent, home=h)
        except MountError as exc:
            print(f"setup: {exc}", file=sys.stderr)
            return 1

    version = agent_version(config, agent.name)
    s = store.install_store(
        agent,
        store=store.store_dir(agent, home=h),
        method="copy" if from_host else "native",
        version=version,
    )
    print(
        f"setup: frozen {agent.command} store ready at {s} "
        f"({agent.command} {store.installed_version(agent, s)})"
    )

    search_path = os.environ.get("PATH", "") if path is None else path
    report_shim(resolve_shim(agent.command, search_path, home=h))
    return 0
