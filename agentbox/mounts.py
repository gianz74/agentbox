"""Mount-set resolution and guards -- pure logic.

Given the loaded :class:`~agentbox.config.Config` and the current working
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
  mounts would replace the sandbox's own frozen agent store.
* **Rendering** -- translate the resolved set into the concrete ``bwrap`` binds
  and tmpfs masks a launch needs: parity vs alias, read-only vs read-write,
  absent sources skipped natively, and each ``exclude`` sub-path turned into an
  empty overmount (:func:`render`).

Everything here is a pure function of the config and the cwd string -- no
filesystem access. Turning the rendered binds and masks into the final ``bwrap``
argv stays in :mod:`agentbox.sandbox`.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass

from .config import Config, Context, MountSpec
from .sandbox import Bind

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
    mounts would replace the sandbox's own frozen agent store."""


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


def _store_targets(agent, home: str) -> tuple[str, ...]:
    """Sandbox-side paths the frozen store binds onto -- the agent's binary, plus
    its payload tree when it has one (derived from the agent's install recipe). A
    configured mount covering any of these would replace the store and break
    ``exec <command>``."""
    recipe = agent.install
    rels = [recipe.binary_rel]
    if recipe.payload_rel is not None:
        rels.append(recipe.payload_rel)
    return tuple(os.path.join(home, *rel) for rel in rels)


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


def guard_store_shadow(config: Config, agent, *, home: str | None = None) -> None:
    """Refuse a config whose mounts would cover *agent*'s frozen store.

    Checks every configured mount, global and per-context: a mount whose
    sandbox-side ``path`` is an ancestor of -- or equal to -- a store target (the
    agent's binary, and its payload tree when it has one) would mask the read-only
    store bound there. cwd-independent, so it can run at setup time over the whole
    config."""
    h = _home(home)
    targets = tuple(_norm(t) for t in _store_targets(agent, h))
    all_mounts = (
        *config.mounts,
        *(m for ctx in config.contexts for m in ctx.mounts),
    )
    for m in all_mounts:
        mpath = _norm(m.path)
        if any(_within(t, mpath) for t in targets):
            raise MountError(
                f"mount {m.path!r} would shadow the sandbox's {agent.command} "
                "store; narrow or remove it"
            )


def _guard_cwd(cwd: str, mounts: tuple[MountSpec, ...], *, agent, home: str) -> None:
    """Refuse an unsafe working directory: an aliased credential store, ``$HOME``
    itself, the filesystem root, a system root, or part of the agent's store."""
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

    # The store's payload tree -- and the intermediate directories under $HOME that
    # lead to it -- are off limits as a workspace: the read-only store binds there.
    # A lone-binary agent (no payload tree) has no such subtree to protect.
    payload_rel = agent.install.payload_rel
    if payload_rel is not None:
        payload = _norm(os.path.join(home, *payload_rel))
        if _within(cwd, payload) or (_within(payload, cwd) and _within(cwd, home)):
            raise MountError(
                f"refusing to run in {cwd!r}: it is part of the sandbox's "
                f"{agent.command} store"
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


def resolve(config: Config, agent, cwd: str, *, home: str | None = None) -> Resolution:
    """Resolve *cwd* against *config* into the effective launch surface for *agent*.

    Raises :class:`MountError` if the config would shadow the agent's store, or if
    the cwd is an unsafe place to run."""
    h = _home(home)
    cwd = _norm(cwd)

    guard_store_shadow(config, agent, home=h)

    matched = resolve_context(config, cwd)
    effective = _merge(config.mounts, matched.mounts if matched else ())

    _guard_cwd(cwd, effective, agent=agent, home=h)

    mounts = effective
    if not _cwd_is_covered(cwd, effective):
        mounts = (*effective, MountSpec(path=cwd, mode="rw"))

    return Resolution(
        context=matched.name if matched is not None else DEFAULT_CONTEXT,
        cwd=cwd,
        mounts=_ordered(mounts),
    )


# --- rendering to bwrap binds ------------------------------------------------


@dataclass(frozen=True)
class RenderedMounts:
    """A resolved mount set translated for the sandbox.

    ``binds`` is one :class:`~agentbox.sandbox.Bind` per mount, carrying its
    parity/alias backing and read-only/read-write mode. ``masks`` are the empty
    tmpfs overmounts for ``exclude`` sub-paths. The sandbox emits ``masks`` after
    ``binds`` so each mask lands on top of the bound tree it shadows.
    """

    binds: tuple[Bind, ...]
    masks: tuple[str, ...]


def render(mounts: Sequence[MountSpec]) -> RenderedMounts:
    """Translate a resolved mount set into ``bwrap`` binds and tmpfs masks.

    Each mount yields one bind: a parity mount binds its path onto itself, an
    alias binds its host backing onto the sandbox-side path, and ``mode="ro"``
    makes it read-only. Every bind is *optional* (bwrap's ``*-bind-try``), so a
    source that does not exist on this machine is skipped at launch rather than
    rejected by a host-side existence check. Each ``exclude`` sub-path becomes a
    tmpfs mask at ``<path>/<sub>`` -- an empty overmount that fully shadows the
    real contents underneath.
    """
    binds: list[Bind] = []
    masks: list[str] = []
    for m in mounts:
        binds.append(Bind(src=m.host_path, dest=m.path, mode=m.mode, optional=True))
        for sub in m.exclude:
            masks.append(os.path.normpath(os.path.join(m.path, sub)))
    return RenderedMounts(binds=tuple(binds), masks=tuple(masks))
