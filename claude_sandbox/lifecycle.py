"""Frozen claude store and the run path.

``setup`` builds a wrapper-private **frozen claude store** -- a genuine native
``~/.local`` install of ``claude`` redirected into a private directory that the
host's own ``~/.local`` install is never touched by. The store is bound
**read-only** into every sandbox at the paths ``claude`` expects
(``~/.local/share/claude`` and ``~/.local/bin/claude``), so a session can run it
but never mutate it, and ``setup`` is the only thing that refreshes it.

Store layout (a faithful native install)::

    <store>/.local/bin/claude            -> .local/share/claude/versions/<v>
    <store>/.local/share/claude/versions/<v>   (a single self-contained binary)

A small identity stamp (schema version, claude version, install method) is
written alongside the store so a missing or drifted store can be rebuilt.

**Recursion guard.** ``claude`` is invoked by **absolute path**
(``$HOME/.local/bin/claude``), never through ``$PATH``; and a private launcher --
a ``claude`` symlink in a directory *outside* ``$HOME`` that the run path prepends
to ``PATH`` -- makes any bare ``claude`` (a child process, say) resolve to the
store binary too, even when ``~/.local`` is mounted over and even when an
unrelated ``claude`` shim sits on a shared ``PATH`` directory. The home is a
fresh skeleton with only whitelisted binds, so a shim living under the host
``$HOME`` is never carried in to begin with.

Install/freeze and the store-launch wiring live here; the full run path (config
-> mounts -> sandbox -> network) and ``delete`` arrive later.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import SCHEMA_VERSION
from .sandbox import DEFAULT_PATH, Bind

# Store location, relative to $HOME. A wrapper-private directory the native
# install is redirected into; the host's own ~/.local install is left alone.
STORE_DIR_REL = (".local", "share", "claude-sandbox", "store")

# The native install's layout, relative to the redirected HOME (the store root).
_BIN_CLAUDE_REL = (".local", "bin", "claude")
_SHARE_CLAUDE_REL = (".local", "share", "claude")

# The private claude launcher directory. It lives *outside* $HOME so no bind of a
# host ~/.local can shadow it; the run path prepends it to PATH ahead of
# ~/.local/bin, so a bare `claude` resolves to the store binary regardless of
# what is mounted over ~/.local or what `claude` shim sits further down PATH.
LAUNCHER_DIR = "/opt/claude-sandbox/bin"

# The native installer. Run with HOME redirected into the store so the install
# lands under <store>/.local rather than the real ~/.local.
NATIVE_INSTALL_URL = "https://claude.ai/install.sh"

# The store-identity stamp, written at the store root (outside the bound subtree).
STAMP_NAME = "stamp.json"


class LifecycleError(Exception):
    """A user-facing store/lifecycle error (no host claude to copy, etc.)."""


# --- store location & layout -------------------------------------------------


def store_dir(home: str | os.PathLike[str] | None = None) -> Path:
    """The wrapper-private store directory under *home* (default real ``$HOME``)."""
    base = Path(home) if home is not None else Path(os.path.expanduser("~"))
    return base.joinpath(*STORE_DIR_REL)


def _store_bin(store: str | os.PathLike[str]) -> Path:
    """``<store>/.local/bin/claude`` -- the store's ``claude`` link/binary."""
    return Path(store).joinpath(*_BIN_CLAUDE_REL)


def _store_share(store: str | os.PathLike[str]) -> Path:
    """``<store>/.local/share/claude`` -- the store's versioned payload tree."""
    return Path(store).joinpath(*_SHARE_CLAUDE_REL)


def sandbox_claude_bin(home: str) -> str:
    """The in-sandbox absolute path the store ``claude`` is bound at and exec'd by."""
    return f"{home}/.local/bin/claude"


def sandbox_claude_share(home: str) -> str:
    """The in-sandbox path the store's versioned payload is bound at."""
    return f"{home}/.local/share/claude"


def installed_version(store: str | os.PathLike[str]) -> str:
    """The version label of the store's ``claude`` (the ``bin/claude`` target's
    basename, falling back to the lone ``versions/`` entry), or ``"unknown"``."""
    binc = _store_bin(store)
    if binc.is_symlink():
        return os.path.basename(os.readlink(binc))
    versions = _store_share(store) / "versions"
    if versions.is_dir():
        names = sorted(p.name for p in versions.iterdir())
        if names:
            return names[-1]
    return "unknown"


def store_present(
    store: str | os.PathLike[str] | None = None,
    *,
    home: str | os.PathLike[str] | None = None,
) -> bool:
    """True if *store* holds a usable claude: the payload tree exists and
    ``bin/claude`` resolves to an executable."""
    s = Path(store) if store is not None else store_dir(home)
    binc = _store_bin(s)
    return (
        _store_share(s).is_dir()
        and binc.exists()
        and os.access(os.path.realpath(binc), os.X_OK)
    )


# --- store-identity stamp ----------------------------------------------------


def _stamp_path(store: str | os.PathLike[str]) -> Path:
    return Path(store) / STAMP_NAME


def store_stamp(*, version: str, method: str) -> dict:
    """The identity recorded for a freshly built store: the config schema version
    it was built against, the claude version, and how it was installed."""
    return {"schema_version": SCHEMA_VERSION, "version": version, "method": method}


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


# --- install / freeze --------------------------------------------------------


def _native_install_cmd(version: str | None) -> str:
    """The shell pipeline that installs claude into the redirected HOME, pinning
    *version* when given (else the latest at install time)."""
    cmd = f"curl -fsSL {shlex.quote(NATIVE_INSTALL_URL)} | bash"
    if version:
        cmd += " -s -- " + shlex.quote(version)
    return cmd


def _install_native(store: Path, version: str | None) -> None:
    """Run the native installer with HOME redirected into *store*."""
    store.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "HOME": str(store)}
    subprocess.run(["bash", "-c", _native_install_cmd(version)], env=env, check=True)
    if not _store_bin(store).exists():
        raise LifecycleError(
            f"native installer produced no {_store_bin(store)} -- install failed"
        )


def _install_copy(store: Path, source_home: str | os.PathLike[str] | None) -> None:
    """Build the store by copying an existing native ``~/.local`` claude.

    The active version (the one ``bin/claude`` points at) is copied into the store
    and a fresh ``bin/claude`` symlink is pointed at the store's own copy, so the
    result is self-contained and decoupled from the source install.
    """
    src_home = (
        Path(source_home)
        if source_home is not None
        else Path(os.path.expanduser("~"))
    )
    src_bin = src_home.joinpath(*_BIN_CLAUDE_REL)
    src_versions = src_home.joinpath(*_SHARE_CLAUDE_REL) / "versions"
    if not src_bin.exists():
        raise LifecycleError(f"no native claude to copy from: {src_bin} is missing")

    active = os.path.basename(os.readlink(src_bin)) if src_bin.is_symlink() else None
    if active is None or not (src_versions / active).exists():
        names = sorted(p.name for p in src_versions.iterdir()) if src_versions.is_dir() else []
        if len(names) != 1:
            raise LifecycleError(
                f"cannot determine the active claude version under {src_versions}"
            )
        active = names[0]

    dst_versions = _store_share(store) / "versions"
    dst_versions.mkdir(parents=True, exist_ok=True)
    src_payload, dst_payload = src_versions / active, dst_versions / active
    if src_payload.is_dir():
        shutil.copytree(src_payload, dst_payload, dirs_exist_ok=True)
    else:
        shutil.copy2(src_payload, dst_payload)

    dst_bin = _store_bin(store)
    dst_bin.parent.mkdir(parents=True, exist_ok=True)
    if dst_bin.exists() or dst_bin.is_symlink():
        dst_bin.unlink()
    os.symlink(str(dst_payload), dst_bin)


def freeze_store(store: str | os.PathLike[str]) -> None:
    """Freeze the store: disable claude's self-update in the store's own config.

    The store is bound read-only at runtime, so it cannot rewrite itself anyway;
    this also keeps the install-time copy from updating. It mirrors the host's
    frozen posture (the host install likewise disables auto-updates).
    """
    cfg = Path(store) / ".claude.json"
    data: dict = {}
    if cfg.exists():
        try:
            data = json.loads(cfg.read_text())
        except (ValueError, OSError):
            data = {}
    data["autoUpdates"] = False
    cfg.write_text(json.dumps(data, indent=2) + "\n")


def install_store(
    *,
    store: str | os.PathLike[str] | None = None,
    method: str = "native",
    version: str | None = None,
    source_home: str | os.PathLike[str] | None = None,
) -> Path:
    """Build, freeze and stamp the frozen claude store; return its path.

    *method* ``"native"`` runs the native installer (HOME redirected into the
    store), pinning *version* when given. *method* ``"copy"`` builds the store
    from an existing native ``~/.local`` claude under *source_home* -- an opt-in
    offline path, not the default.
    """
    s = Path(store) if store is not None else store_dir()
    if method == "native":
        _install_native(s, version)
    elif method == "copy":
        _install_copy(s, source_home)
    else:
        raise LifecycleError(f"unknown install method {method!r} (expected native/copy)")
    freeze_store(s)
    write_stamp(s, store_stamp(version=installed_version(s), method=method))
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


def store_binds(home: str, store: str | os.PathLike[str]) -> tuple[Bind, Bind]:
    """The two read-only binds that expose the store at the paths claude expects."""
    return (
        Bind(str(_store_share(store)), sandbox_claude_share(home), mode="ro"),
        Bind(str(_store_bin(store)), sandbox_claude_bin(home), mode="ro"),
    )


def store_launch(
    home: str,
    launcher_dir: str | os.PathLike[str],
    *,
    store: str | os.PathLike[str] | None = None,
    base_path: str = DEFAULT_PATH,
) -> StoreLaunch:
    """Assemble the store binds, private launcher and PATH for a sandbox launch.

    *launcher_dir* is a caller-owned host directory (kept alive until launch) the
    ``claude`` launcher symlink is written into; it is bound read-only at
    :data:`LAUNCHER_DIR` inside the sandbox. The launcher's target resolves
    *inside* the sandbox to the store binary, so a bare ``claude`` lands on the
    store even with ``~/.local`` mounted over and a ``claude`` shim elsewhere on
    PATH.
    """
    s = Path(store) if store is not None else store_dir(home)
    exec_path = sandbox_claude_bin(home)

    link = Path(launcher_dir) / "claude"
    if link.exists() or link.is_symlink():
        link.unlink()
    os.symlink(exec_path, link)

    binds = store_binds(home, s) + (Bind(str(launcher_dir), LAUNCHER_DIR, mode="ro"),)
    path = f"{LAUNCHER_DIR}:{home}/.local/bin:{base_path}"
    return StoreLaunch(binds=binds, path=path, exec_path=exec_path)


# --- commands ----------------------------------------------------------------


def setup(config=None, *, from_host: bool = False) -> int:
    """Build (or rebuild) the frozen claude store.

    Defaults to a native install, honoring an optional ``[setup].claude_version``
    pin from *config*. *from_host* takes the opt-in copy-from-host path instead.
    """
    version = config.setup.claude_version if config is not None else None
    store = install_store(
        method="copy" if from_host else "native", version=version
    )
    print(
        f"setup: frozen claude store ready at {store} "
        f"(claude {installed_version(store)})"
    )
    return 0


def delete():
    raise NotImplementedError("delete is not implemented yet")


def run(mounts, claude_args):
    raise NotImplementedError("the run path is not implemented yet")
