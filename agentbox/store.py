"""The frozen, wrapper-private agent store: build, freeze, stamp, and launch wiring.

``setup`` builds a wrapper-private **frozen store** for an agent -- a genuine
native install of the agent's CLI redirected into a private directory that the
host's own ``~/.local`` install is never touched by. The store is bound
**read-only** into every sandbox at the paths the agent expects (under
``~/.local``), so a session can run the agent but never mutate it, and ``setup``
is the only thing that refreshes it. Everything agent-specific -- where the
installer lands, the binary/payload layout, how self-update is disabled -- is
carried by the :class:`~agentbox.agents.base.Agent` and its
:class:`~agentbox.agents.base.InstallRecipe`; this module is the agent-neutral
procedure parameterized by them.

Store layout (a faithful native install), per agent::

    <store>/.local/bin/<command>                 -> the agent's launcher/binary
    <store>/.local/share/<command>/...           -> versioned payload (if any)
    <store>/stamp.json   {schema_version, version, method, agent}

The per-agent store root is ``~/.local/share/box/<agent>/store`` so two agents
never share one store; a launch binds only the selected agent's store.

**Recursion guard.** The agent binary is invoked by **absolute path**
(``$HOME/.local/bin/<command>``), never through ``$PATH``; and a private launcher
-- a ``<command>`` symlink in a directory *outside* ``$HOME`` that the run path
prepends to ``PATH`` -- makes any bare ``<command>`` resolve to the store binary
too, even when ``~/.local`` is mounted over and even when an unrelated shim sits
on a shared ``PATH`` directory.

``delete`` removes the frozen store so the next launch rebuilds it. The store is
the only persistent artifact ``setup`` builds; warm toolchain caches persist
purely as ordinary ``[[mounts]]`` host directories, so nothing else is
wrapper-managed.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import SCHEMA_VERSION, agent_version
from .sandbox import DEFAULT_PATH, Bind

# Store location, relative to $HOME: a wrapper-private per-agent directory the
# native install is redirected into; the host's own ~/.local install is left
# alone. The agent name is interpolated between this root and "store".
STORE_ROOT_REL = (".local", "share", "box")

# The private launcher directory. It lives *outside* $HOME so no bind of a host
# ~/.local can shadow it; the run path prepends it to PATH ahead of the binary's
# own dir, so a bare ``<command>`` resolves to the store binary regardless of what
# is mounted over ~/.local or what shim sits further down PATH.
LAUNCHER_DIR = "/opt/box/bin"

# The store-identity stamp, written at the store root (outside the bound subtree).
STAMP_NAME = "stamp.json"


class StoreError(Exception):
    """A user-facing store error (no host install to copy, install failed, etc.)."""


# --- store location & layout -------------------------------------------------


def store_dir(agent, home: str | os.PathLike[str] | None = None) -> Path:
    """The wrapper-private store directory for *agent* under *home* (default real
    ``$HOME``): ``~/.local/share/box/<agent>/store``."""
    base = Path(home) if home is not None else Path(os.path.expanduser("~"))
    return base.joinpath(*STORE_ROOT_REL, agent.name, "store")


def _store_bin(agent, store: str | os.PathLike[str]) -> Path:
    """``<store>/<binary_rel>`` -- the store's launcher link/binary."""
    return Path(store).joinpath(*agent.install.binary_rel)


def _store_payload(agent, store: str | os.PathLike[str]) -> Path | None:
    """``<store>/<payload_rel>`` -- the store's versioned payload tree, or ``None``
    for a lone-binary agent (no payload tree)."""
    rel = agent.install.payload_rel
    return Path(store).joinpath(*rel) if rel is not None else None


def sandbox_bin(agent, home: str) -> str:
    """The in-sandbox absolute path the store binary is bound at and exec'd by."""
    return os.path.join(home, *agent.install.binary_rel)


def sandbox_payload(agent, home: str) -> str | None:
    """The in-sandbox path the store's versioned payload is bound at (``None`` for
    a lone-binary agent)."""
    rel = agent.install.payload_rel
    return os.path.join(home, *rel) if rel is not None else None


def installed_version(agent, store: str | os.PathLike[str]) -> str:
    """The version label of the store's binary (the ``bin`` link's target basename,
    falling back to the lone ``versions/`` entry of the payload), or ``"unknown"``."""
    binc = _store_bin(agent, store)
    if binc.is_symlink():
        return os.path.basename(os.readlink(binc))
    payload = _store_payload(agent, store)
    if payload is not None:
        versions = payload / "versions"
        if versions.is_dir():
            names = sorted(p.name for p in versions.iterdir())
            if names:
                return names[-1]
    return "unknown"


def store_present(
    agent,
    store: str | os.PathLike[str] | None = None,
    *,
    home: str | os.PathLike[str] | None = None,
) -> bool:
    """True if *store* holds a usable agent install: the payload tree exists (when
    the agent has one) and ``bin`` resolves to an executable."""
    s = Path(store) if store is not None else store_dir(agent, home)
    binc = _store_bin(agent, s)
    payload = _store_payload(agent, s)
    payload_ok = payload is None or payload.is_dir()
    return (
        payload_ok
        and binc.exists()
        and os.access(os.path.realpath(binc), os.X_OK)
    )


# --- store-identity stamp ----------------------------------------------------


def _stamp_path(store: str | os.PathLike[str]) -> Path:
    return Path(store) / STAMP_NAME


def store_stamp(*, version: str, method: str, agent) -> dict:
    """The identity recorded for a freshly built store: the config schema version
    it was built against, the agent version, how it was installed, and which agent
    it belongs to."""
    return {
        "schema_version": SCHEMA_VERSION,
        "version": version,
        "method": method,
        "agent": agent.name,
    }


def write_stamp(store: str | os.PathLike[str], stamp: dict) -> None:
    _stamp_path(store).write_text(json.dumps(stamp, indent=2) + "\n")


def read_stamp(store: str | os.PathLike[str]) -> dict | None:
    """The store's identity stamp, or ``None`` if absent/unreadable."""
    p = _stamp_path(store)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (ValueError, OSError):
        return None


# --- install -----------------------------------------------------------------


def _native_install_cmd(recipe, version: str | None) -> str:
    """The shell pipeline that installs the agent into the redirected store,
    pinning *version* (via the recipe's ``version_args``) when given."""
    cmd = f"curl -fsSL {shlex.quote(recipe.url)} | bash"
    if version and recipe.version_args is not None:
        cmd += " " + " ".join(shlex.quote(a) for a in recipe.version_args(version))
    return cmd


def _install_native(agent, store: Path, version: str | None) -> None:
    """Run the native installer with the recipe's redirect env pointed at *store*."""
    store.mkdir(parents=True, exist_ok=True)
    recipe = agent.install
    redirect = recipe.redirect_value.format(store=str(store))
    env = {**os.environ, recipe.redirect_env: redirect}
    subprocess.run(["bash", "-c", _native_install_cmd(recipe, version)], env=env, check=True)
    if not _store_bin(agent, store).exists():
        raise StoreError(
            f"native installer produced no {_store_bin(agent, store)} -- install failed"
        )


def _install_copy(agent, store: Path, source_home: str | os.PathLike[str] | None) -> None:
    """Build the store by copying an existing native ``~/.local`` install.

    The active version (the one ``bin`` points at) is copied into the store and a
    fresh ``bin`` symlink is pointed at the store's own copy, so the result is
    self-contained and decoupled from the source install. Assumes the agent's
    payload uses a ``versions/<v>`` layout (the native-installer shape).
    """
    recipe = agent.install
    if recipe.payload_rel is None:
        raise StoreError(f"copy install is not supported for {agent.command} (no payload tree)")
    src_home = (
        Path(source_home)
        if source_home is not None
        else Path(os.path.expanduser("~"))
    )
    src_bin = src_home.joinpath(*recipe.binary_rel)
    src_versions = src_home.joinpath(*recipe.payload_rel) / "versions"
    if not src_bin.exists():
        raise StoreError(f"no native {agent.command} to copy from: {src_bin} is missing")

    active = os.path.basename(os.readlink(src_bin)) if src_bin.is_symlink() else None
    if active is None or not (src_versions / active).exists():
        names = sorted(p.name for p in src_versions.iterdir()) if src_versions.is_dir() else []
        if len(names) != 1:
            raise StoreError(
                f"cannot determine the active {agent.command} version under {src_versions}"
            )
        active = names[0]

    dst_versions = _store_payload(agent, store) / "versions"
    dst_versions.mkdir(parents=True, exist_ok=True)
    src_payload, dst_payload = src_versions / active, dst_versions / active
    if src_payload.is_dir():
        shutil.copytree(src_payload, dst_payload, dirs_exist_ok=True)
    else:
        shutil.copy2(src_payload, dst_payload)

    dst_bin = _store_bin(agent, store)
    dst_bin.parent.mkdir(parents=True, exist_ok=True)
    if dst_bin.exists() or dst_bin.is_symlink():
        dst_bin.unlink()
    os.symlink(str(dst_payload), dst_bin)


def install_store(
    agent,
    *,
    store: str | os.PathLike[str] | None = None,
    method: str = "native",
    version: str | None = None,
    source_home: str | os.PathLike[str] | None = None,
) -> Path:
    """Build, freeze and stamp the frozen store for *agent*; return its path.

    *method* ``"native"`` runs the agent's native installer (redirected into the
    store), pinning *version* when given. *method* ``"copy"`` builds the store from
    an existing native ``~/.local`` install under *source_home* -- an opt-in
    offline path, not the default. The freeze step is the agent's own
    ``disable_self_update``.
    """
    s = Path(store) if store is not None else store_dir(agent)
    if method == "native":
        _install_native(agent, s, version)
    elif method == "copy":
        _install_copy(agent, s, source_home)
    else:
        raise StoreError(f"unknown install method {method!r} (expected native/copy)")
    agent.disable_self_update(s)
    write_stamp(s, store_stamp(version=installed_version(agent, s), method=method, agent=agent))
    return s


# --- run-path store wiring (binds + recursion guard) -------------------------


@dataclass(frozen=True)
class StoreLaunch:
    """The store's contribution to a sandbox launch.

    ``binds`` expose the store read-only plus the private launcher; ``path`` is
    the ``PATH`` value with the launcher prepended; ``exec_path`` is the absolute
    path the run path should exec (never resolved through ``PATH``).
    """

    binds: tuple[Bind, ...]
    path: str
    exec_path: str


def store_binds(agent, home: str, store: str | os.PathLike[str]) -> tuple[Bind, ...]:
    """The read-only binds that expose the store at the paths the agent expects:
    the versioned payload (when present) and the binary."""
    binds: list[Bind] = []
    payload = _store_payload(agent, store)
    if payload is not None:
        binds.append(Bind(str(payload), sandbox_payload(agent, home), mode="ro"))
    binds.append(Bind(str(_store_bin(agent, store)), sandbox_bin(agent, home), mode="ro"))
    return tuple(binds)


def _dedup_path(path: str) -> str:
    """Collapse a ``PATH`` to the first occurrence of each entry, dropping empties.

    Order is preserved, so the launcher prefix stays ahead of everything and any
    duplicate the host PATH carries (notably the binary's own dir) falls away.
    """
    seen: set[str] = set()
    out: list[str] = []
    for entry in path.split(":"):
        if entry and entry not in seen:
            seen.add(entry)
            out.append(entry)
    return ":".join(out)


def store_launch(
    agent,
    home: str,
    launcher_dir: str | os.PathLike[str],
    *,
    store: str | os.PathLike[str] | None = None,
    base_path: str = DEFAULT_PATH,
) -> StoreLaunch:
    """Assemble the store binds, private launcher and PATH for a sandbox launch.

    *launcher_dir* is a caller-owned host directory (kept alive until launch) the
    ``<command>`` launcher symlink is written into; it is bound read-only at
    :data:`LAUNCHER_DIR` inside the sandbox. The launcher's target resolves
    *inside* the sandbox to the store binary, so a bare ``<command>`` lands on the
    store even with ``~/.local`` mounted over and a shim elsewhere on PATH.

    *base_path* is the PATH the launcher prefix is prepended to; the prefix and a
    final dedup keep a bare ``<command>`` resolving to the store regardless of what
    the base contributes.
    """
    s = Path(store) if store is not None else store_dir(agent, home)
    exec_path = sandbox_bin(agent, home)
    bin_dir = os.path.dirname(exec_path)

    link = Path(launcher_dir) / agent.command
    if link.exists() or link.is_symlink():
        link.unlink()
    os.symlink(exec_path, link)

    binds = store_binds(agent, home, s) + (Bind(str(launcher_dir), LAUNCHER_DIR, mode="ro"),)
    path = _dedup_path(f"{LAUNCHER_DIR}:{bin_dir}:{base_path}")
    return StoreLaunch(binds=binds, path=path, exec_path=exec_path)


# --- store freshness (the run-path fast path) --------------------------------


def store_matches(
    agent,
    config,
    *,
    store: str | os.PathLike[str] | None = None,
    home: str | os.PathLike[str] | None = None,
) -> bool:
    """True if the frozen store is present and its identity stamp is current --
    the fast path that does no install work.

    A store needs rebuilding (returns False) when it is missing, carries no stamp,
    was built against a different config schema, or its recorded version has
    drifted from a configured version pin. An unpinned config accepts whatever
    version the store holds.
    """
    s = Path(store) if store is not None else store_dir(agent, home)
    if not store_present(agent, s):
        return False
    stamp = read_stamp(s)
    if stamp is None or stamp.get("schema_version") != SCHEMA_VERSION:
        return False
    pin = agent_version(config, agent.name)
    return pin is None or stamp.get("version") == pin


def ensure_store(
    agent,
    config,
    *,
    store: str | os.PathLike[str] | None = None,
    home: str | os.PathLike[str] | None = None,
    install=None,
) -> Path:
    """Ensure a present, current frozen store for *agent*, building it on the spot
    when one is missing or its stamp has drifted (:func:`store_matches`); return
    its path.

    The fast path (a matching store) does no install work. *install*, when given,
    is a ``callable(store_path)`` that builds the store -- used to drive an offline
    or synthetic build; the default runs the native installer honoring any
    configured version pin.
    """
    s = Path(store) if store is not None else store_dir(agent, home)
    if store_matches(agent, config, store=s, home=home):
        return s
    if install is not None:
        install(s)
    else:
        install_store(agent, store=s, method="native", version=agent_version(config, agent.name))
    return s


# --- delete ------------------------------------------------------------------


def delete(
    agent,
    *,
    store: str | os.PathLike[str] | None = None,
    home: str | os.PathLike[str] | None = None,
    confirm=input,
    out=print,
) -> int:
    """Remove *agent*'s frozen store after a ``[y/N]`` confirmation.

    The frozen store is the only persistent artifact ``setup`` builds; removing it
    forces the next launch to rebuild it from scratch (the run path auto-builds a
    missing store). Warm toolchain caches are ordinary host-backed ``[[mounts]]``
    owned by the host -- they live outside the store and are never touched here.

    *confirm* is the prompt callable (default :func:`input`); answering anything but
    ``y``/``yes`` aborts and leaves the store untouched. Returns ``0`` once the
    store is gone (including when there was none to begin with) and ``1`` on an
    aborted confirmation.
    """
    s = Path(store) if store is not None else store_dir(agent, home)
    label = f"frozen {agent.command} store"
    if not s.exists():
        out(f"delete: no {label} at {s}; nothing to remove.")
        return 0
    answer = confirm(f"delete: remove the {label} at {s}? [y/N] ")
    if answer.strip().lower() not in ("y", "yes"):
        out(f"delete: aborted; the {label} was left in place.")
        return 1
    shutil.rmtree(s)
    out(f"delete: removed the {label} at {s}; the next launch will rebuild it.")
    return 0
