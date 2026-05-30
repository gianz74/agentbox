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
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import net, sandbox
from .config import SCHEMA_VERSION, load_user_config
from .mounts import render, resolve, resolve_context
from .sandbox import DEFAULT_PATH, Bind, SandboxSpec, host_identity

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


# --- run path: store freshness, environment, launch --------------------------

# The sandbox identity (HOME/USER) and the launcher PATH are set explicitly from
# the resolved identity and store wiring; they are never carried in from the host
# environment or the config, so they are excluded everywhere the run-time
# environment is assembled.
_IDENTITY_ENV = ("HOME", "USER", "PATH")

# Host environment always forwarded into the sandbox, independent of config: just
# enough terminal/locale state for the CLI to render correctly, plus the
# Anthropic/Claude knobs the CLI itself reads. A name matches if it is listed
# here or begins with one of the prefixes below.
_BASELINE_ENV_NAMES = frozenset(
    {
        "TERM",
        "COLORTERM",
        "TERM_PROGRAM",
        "TERM_PROGRAM_VERSION",
        "LANG",
        "LANGUAGE",
        "TZ",
        "COLUMNS",
        "LINES",
        "NO_COLOR",
        "FORCE_COLOR",
        "CLICOLOR",
        "CLICOLOR_FORCE",
    }
)
_BASELINE_ENV_PREFIXES = ("LC_", "ANTHROPIC_", "CLAUDE_")


def _baseline_env(host_env) -> dict[str, str]:
    """The universal host baseline: terminal/locale plus the Anthropic/Claude
    knobs, never the identity/launcher keys."""
    out: dict[str, str] = {}
    for key, value in host_env.items():
        if key in _IDENTITY_ENV:
            continue
        if key in _BASELINE_ENV_NAMES or key.startswith(_BASELINE_ENV_PREFIXES):
            out[key] = value
    return out


def _apply_env_scope(env: dict[str, str], literals, forward, host_env) -> None:
    """Layer one ``[env]`` scope onto *env*: ``forward`` pulls host values (an
    unset host var is skipped), then literal pairs override them."""
    for name in forward:
        if name in host_env:
            env[name] = host_env[name]
    for key, value in literals.items():
        env[key] = value


def build_env(config, matched, host_env) -> dict[str, str]:
    """The environment applied to the sandboxed ``claude`` via ``--setenv``.

    Layered low-to-high: a universal host baseline, then the global ``[env]``,
    then the matched context's ``env`` -- each scope's ``forward`` list pulling
    host values and its literals overriding, so a context value wins over a global
    one and a literal wins over a forwarded value. The identity/launcher keys are
    excluded; the sandbox sets those itself.
    """
    env = _baseline_env(host_env)
    _apply_env_scope(env, dict(config.env), config.forward, host_env)
    if matched is not None:
        _apply_env_scope(env, dict(matched.env), matched.forward, host_env)
    for key in _IDENTITY_ENV:
        env.pop(key, None)
    return env


def store_matches(
    config,
    *,
    store: str | os.PathLike[str] | None = None,
    home: str | os.PathLike[str] | None = None,
) -> bool:
    """True if the frozen store is present and its identity stamp is current --
    the fast path that does no install work.

    A store needs rebuilding (returns False) when it is missing, carries no stamp,
    was built against a different config schema, or its recorded claude version has
    drifted from a ``[setup].claude_version`` pin. An unpinned config accepts
    whatever version the store holds.
    """
    s = Path(store) if store is not None else store_dir(home)
    if not store_present(s):
        return False
    stamp = read_stamp(s)
    if stamp is None or stamp.get("schema_version") != SCHEMA_VERSION:
        return False
    pin = config.setup.claude_version if config is not None else None
    return pin is None or stamp.get("version") == pin


def ensure_store(
    config,
    *,
    store: str | os.PathLike[str] | None = None,
    home: str | os.PathLike[str] | None = None,
    install=None,
) -> Path:
    """Ensure a present, current frozen store, building it on the spot when one is
    missing or its stamp has drifted (:func:`store_matches`); return its path.

    The fast path (a matching store) does no install work. *install*, when given,
    is a ``callable(store_path)`` that builds the store -- used to drive an
    offline or synthetic build; the default runs the native installer honoring any
    ``[setup].claude_version`` pin.
    """
    s = Path(store) if store is not None else store_dir(home)
    if store_matches(config, store=s, home=home):
        return s
    if install is not None:
        install(s)
    else:
        version = config.setup.claude_version if config is not None else None
        install_store(store=s, method="native", version=version)
    return s


def run(
    mounts=(),
    claude_args=(),
    *,
    config=None,
    cwd: str | None = None,
    home: str | None = None,
    env=None,
    store: str | os.PathLike[str] | None = None,
    install=None,
    sse_port: int | None = None,
    gateway: str | None = None,
) -> int:
    """The hot path: launch a sandboxed ``claude`` session for the current
    directory and return its exit code.

    Loads the config (unless one is supplied), resolves the cwd to its context and
    effective mount set, ensures the frozen store is present and current
    (auto-building it once on a missing/drifted stamp -- otherwise no install
    work), then renders the binds/masks and the merged environment and execs the
    store ``claude`` **by absolute path** inside a fresh bwrap sandbox fronted by
    pasta. The read-only store binds go on last so nothing configured can shadow
    the in-sandbox claude, and the launcher-prepended ``PATH`` keeps a bare
    ``claude`` resolving to the store too (the recursion guard).

    Each launch is its own mount and network namespace, so two directories never
    collide on a shared path; mounts and environment are read fresh per launch, so
    a ``config.toml`` edit takes effect on the next launch with no rebuild.

    *mounts* are ad-hoc per-session binds (objects with ``path``/``ro``) consumed
    ahead of the store binds. The remaining keyword arguments override the
    defaults derived from the host (config/cwd/identity/environment/store) and are
    primarily test seams.
    """
    if config is None:
        config = load_user_config()
    cwd = os.getcwd() if cwd is None else cwd
    host_env = os.environ if env is None else env

    ident = host_identity()
    h = ident.home if home is None else home

    s = ensure_store(config, store=store, home=h, install=install)

    resolution = resolve(config, cwd, home=h)
    matched = resolve_context(config, cwd)
    rendered = render(resolution.mounts)

    cli_binds = tuple(
        Bind(m.path, m.path, mode="ro" if m.ro else "rw", optional=True)
        for m in mounts
    )
    setenv = build_env(config, matched, host_env)
    if sse_port is None:
        sse_port = net.sse_port_from_env(host_env)

    # The launcher directory holds the private ``claude`` symlink and is bound
    # read-only into the sandbox; it must outlive the launch (the bind source is
    # read when the sandbox starts), so it wraps the whole boot.
    with tempfile.TemporaryDirectory(prefix="claude-sandbox-launcher.") as launcher_dir:
        sl = store_launch(h, launcher_dir, store=s)
        spec = SandboxSpec(
            identity=ident,
            argv=(sl.exec_path, *tuple(claude_args)),
            binds=(*rendered.binds, *cli_binds, *sl.binds),
            tmpfs=rendered.masks,
            setenv=setenv,
            path=sl.path,
            chdir=resolution.cwd,
        )
        return sandbox.run(spec, sse_port=sse_port, gateway=gateway)
