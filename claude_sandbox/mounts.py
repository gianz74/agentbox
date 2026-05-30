"""Mount-set resolution and guards -- pure logic.

Given the loaded :class:`~claude_sandbox.config.Config` and the current working
directory, decide what a launch exposes:

* **Context resolution** -- a context matches when the cwd is at or under one of
  its ``when`` prefixes (the list is an implicit OR). Across all contexts the
  *longest* matching prefix wins; an exact-length tie falls back to config order
  (the earlier context). A cwd that matches nothing resolves to the reserved
  ``"default"`` context.
* **Effective mount set** -- the global ``[[mounts]]`` (present in every sandbox)
  followed by the matched context's own mounts, deduplicated by sandbox-side path
  with later entries winning, plus a read-write parity bind of the cwd itself. The
  cwd bind is dropped when an existing parity mount already exposes it. The set is
  ordered ancestors-before-descendants so a bind nested under another overlays it
  correctly; within equal depth the order is globals, then context mounts, then
  the cwd. The default context contributes no mounts of its own, so an unmatched
  cwd sees only the global baseline plus its own directory -- no per-context
  credential bundle.
* **Guards** -- refuse to launch when the cwd would sit on top of an aliased
  credential store or on a protected host location, and refuse a config whose
  mounts would replace the sandbox's own claude store.

Everything here is a pure function of the config and the cwd string -- no
filesystem access, and no bwrap argv (that rendering lives elsewhere).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .config import Config, Context, MountSpec

# The fallback context name for a cwd that matches no configured context.
DEFAULT_CONTEXT = "default"

# Host locations a session must never use as its working directory. ``/`` and
# ``$HOME`` are matched exactly (handled inline); the roots below are matched
# at-or-under -- nothing should run with one of these as its workspace.
_SYSTEM_ROOTS = (
    "/etc",
    "/usr",
    "/bin",
    "/boot",
    "/dev",
    "/proc",
    "/sys",
    "/run",
    "/var",
)


class MountError(Exception):
    """A user-facing refusal: an unsafe working directory, or a config whose
    mounts would replace the sandbox's own claude store."""


@dataclass(frozen=True)
class Resolution:
    """The resolved launch surface for one invocation.

    ``context`` is the matched context name (``"default"`` when none matched).
    ``cwd`` is the normalized working directory. ``mounts`` is the ordered,
    deduplicated effective bind set, including the cwd's own parity bind.
    """

    context: str
    cwd: str
    mounts: tuple[MountSpec, ...]


# --- path helpers ------------------------------------------------------------


def _norm(path: str) -> str:
    """``~``-expand and normalize a path for comparison (pure string work)."""
    return os.path.normpath(os.path.expanduser(path))


def _within(path: str, ancestor: str) -> bool:
    """True if *path* is *ancestor* itself or nested beneath it (both normalized)."""
    return path == ancestor or path.startswith(ancestor.rstrip("/") + "/")


def _home(home: str | None) -> str:
    return _norm(home) if home is not None else _norm("~")


def _claude_store_targets(home: str) -> tuple[str, ...]:
    """Sandbox-side paths the frozen claude store binds onto. A configured mount
    covering either would replace claude and break ``exec claude``."""
    return (
        os.path.join(home, ".local", "bin", "claude"),
        os.path.join(home, ".local", "share", "claude"),
    )


# --- context resolution ------------------------------------------------------


def resolve_context(config: Config, cwd: str) -> Context | None:
    """Return the context whose ``when`` has the longest prefix matching *cwd*, or
    ``None`` if none match. Ties on prefix length resolve to config order (the
    earlier context wins)."""
    cwd = _norm(cwd)
    best: tuple[int, Context] | None = None
    for ctx in config.contexts:
        for prefix in ctx.when:
            p = _norm(prefix)
            if _within(cwd, p) and (best is None or len(p) > best[0]):
                best = (len(p), ctx)
    return best[1] if best is not None else None


# --- guards ------------------------------------------------------------------


def guard_claude_shadow(config: Config, *, home: str | None = None) -> None:
    """Refuse a config whose mounts would cover the sandbox's claude store.

    Checks every configured mount, global and per-context: a mount whose
    sandbox-side ``path`` is an ancestor of -- or equal to -- a store target would
    mask the read-only claude bound there. cwd-independent, so it can run at
    setup time over the whole config."""
    h = _home(home)
    targets = tuple(_norm(t) for t in _claude_store_targets(h))
    all_mounts = (
        *config.mounts,
        *(m for ctx in config.contexts for m in ctx.mounts),
    )
    for m in all_mounts:
        mpath = _norm(m.path)
        if any(_within(t, mpath) for t in targets):
            raise MountError(
                f"mount {m.path!r} would shadow the sandbox's claude store; "
                "narrow or remove it"
            )


def _guard_cwd(cwd: str, mounts: tuple[MountSpec, ...], *, home: str) -> None:
    """Refuse an unsafe working directory: an aliased credential store, ``$HOME``
    itself, the filesystem root, a system root, or a claude store location."""
    # An aliased mount remaps a credential store onto a different host backing;
    # neither its target nor its source may double as a workspace.
    for m in mounts:
        if m.is_alias and (
            _within(cwd, _norm(m.path)) or _within(cwd, _norm(m.host_path))
        ):
            raise MountError(
                f"refusing to run in {cwd!r}: it is at or under the aliased mount "
                f"{m.path!r}; never use a remapped credential store as a workspace"
            )

    if cwd == home:
        raise MountError(
            f"refusing to run in {cwd!r}: it is $HOME itself -- run in a project "
            "directory beneath it"
        )
    if cwd == "/":
        raise MountError("refusing to run in '/': the filesystem root is off limits")
    for root in _SYSTEM_ROOTS:
        if _within(cwd, root):
            raise MountError(
                f"refusing to run in {cwd!r}: it is under the system path {root!r}"
            )

    local = os.path.join(home, ".local")
    share = os.path.join(local, "share")
    claude = os.path.join(share, "claude")
    if cwd in (local, share) or _within(cwd, claude):
        raise MountError(
            f"refusing to run in {cwd!r}: it is part of the sandbox's claude store"
        )


# --- mount-set assembly ------------------------------------------------------


def _merge(
    global_mounts: tuple[MountSpec, ...], ctx_mounts: tuple[MountSpec, ...]
) -> tuple[MountSpec, ...]:
    """Global mounts then context mounts, deduplicated by normalized sandbox-side
    path with later entries winning (a context mount overrides a global of the
    same path; first-seen position is kept)."""
    merged: dict[str, MountSpec] = {}
    for m in (*global_mounts, *ctx_mounts):
        merged[_norm(m.path)] = m
    return tuple(merged.values())


def _cwd_is_covered(cwd: str, mounts: tuple[MountSpec, ...]) -> bool:
    """True if a *parity* mount already exposes *cwd* (its sandbox path is the cwd
    or an ancestor, and its host backing is the same path, so the real cwd shows
    through). An aliased ancestor does not count -- that case is refused upstream,
    since the alias would show its backing instead of the cwd."""
    return any(m.from_ is None and _within(cwd, _norm(m.path)) for m in mounts)


def _ordered(mounts: tuple[MountSpec, ...]) -> tuple[MountSpec, ...]:
    """Stable-sort ancestors before descendants (by path depth) so a nested bind
    overlays the bind it sits inside; equal-depth order is preserved."""
    return tuple(sorted(mounts, key=lambda m: _norm(m.path).count("/")))


def resolve(config: Config, cwd: str, *, home: str | None = None) -> Resolution:
    """Resolve *cwd* against *config* into the effective launch surface.

    Raises :class:`MountError` if the config would shadow the claude store, or if
    the cwd is an unsafe place to run."""
    h = _home(home)
    cwd = _norm(cwd)

    guard_claude_shadow(config, home=h)

    matched = resolve_context(config, cwd)
    effective = _merge(config.mounts, matched.mounts if matched else ())

    _guard_cwd(cwd, effective, home=h)

    mounts = effective
    if not _cwd_is_covered(cwd, effective):
        mounts = (*effective, MountSpec(path=cwd, mode="rw"))

    return Resolution(
        context=matched.name if matched is not None else DEFAULT_CONTEXT,
        cwd=cwd,
        mounts=_ordered(mounts),
    )
