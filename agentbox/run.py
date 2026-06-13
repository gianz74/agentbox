"""The run path: config -> mounts -> env -> store -> launch hook -> sandbox.

The hot path launches a sandboxed agent session for the current directory. It
loads the config, resolves the cwd to its context and effective mount set,
ensures the frozen store is present and current, renders the binds/masks and the
merged environment, and execs the store binary **by absolute path** inside a
fresh bwrap sandbox fronted by pasta.

Everything agent-specific is carried by the :class:`~agentbox.agents.base.Agent`:
the environment surface it reads (``env_prefixes``/``env_names`` layered onto a
universal terminal/locale baseline), and -- through its optional
:class:`~agentbox.agents.base.LaunchHook` -- any editor/IDE bridge it weaves into
the launch (the command to exec, extra binds, and host-loopback ports pasta must
forward). An agent with no hook execs the plain ``(exec_path, *args)`` with no
extra binds or ports.
"""

from __future__ import annotations

import os
import tempfile

from . import sandbox
from .agents.base import LaunchPlan
from .config import load_user_config
from .mounts import render, resolve, resolve_context
from .sandbox import DEFAULT_PATH, Bind, SandboxSpec, host_identity
from .store import ensure_store, store_launch

# The sandbox identity (HOME/USER) and the launcher PATH are set explicitly from
# the resolved identity and store wiring; they are never carried in from the host
# environment or the config, so they are excluded everywhere the run-time
# environment is assembled.
_IDENTITY_ENV = ("HOME", "USER", "PATH")

# Host environment always forwarded into the sandbox, independent of config and of
# the agent: just enough terminal/locale state for a CLI to render correctly. The
# agent adds its own knobs on top (``env_prefixes``/``env_names``). A name matches
# if it is listed here, begins with one of the universal prefixes below, or is
# contributed by the agent.
_UNIVERSAL_ENV_NAMES = frozenset(
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
_UNIVERSAL_ENV_PREFIXES = ("LC_",)


def _baseline_env(agent, host_env) -> dict[str, str]:
    """The universal host baseline (terminal/locale) plus *agent*'s own env surface
    and its fixed ``runtime_env`` literals, never the identity/launcher keys."""
    names = _UNIVERSAL_ENV_NAMES.union(agent.env_names)
    prefixes = _UNIVERSAL_ENV_PREFIXES + tuple(agent.env_prefixes)
    out: dict[str, str] = {}
    for key, value in host_env.items():
        if key in _IDENTITY_ENV:
            continue
        if key in names or key.startswith(prefixes):
            out[key] = value
    # Agent-set literals (e.g. a self-update kill switch): part of the baseline, so
    # they override a forwarded host value of the same key but a [env] scope can
    # still override them in turn.
    for key, value in agent.runtime_env:
        if key not in _IDENTITY_ENV:
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


def build_env(agent, config, matched, host_env) -> dict[str, str]:
    """The environment applied to the sandboxed agent via ``--setenv``.

    Layered low-to-high: the agent's host baseline, then the global ``[env]``, then
    the matched context's ``env`` -- each scope's ``forward`` list pulling host
    values and its literals overriding, so a context value wins over a global one
    and a literal wins over a forwarded value. The identity/launcher keys are
    excluded; the sandbox sets those itself.
    """
    env = _baseline_env(agent, host_env)
    _apply_env_scope(env, dict(config.env), config.forward, host_env)
    if matched is not None:
        _apply_env_scope(env, dict(matched.env), matched.forward, host_env)
    for key in _IDENTITY_ENV:
        env.pop(key, None)
    return env


def _path_sources(literals, forward, host_env) -> list[str]:
    """One ``[env]`` scope's PATH fragments, highest priority first: a literal
    ``PATH`` (the config value) ahead of a forwarded host ``$PATH``. An unset or
    empty host PATH contributes nothing."""
    out: list[str] = []
    if "PATH" in literals:
        out.append(literals["PATH"])
    if "PATH" in forward and host_env.get("PATH"):
        out.append(host_env["PATH"])
    return out


def resolve_base_path(config, matched, host_env) -> str:
    """The PATH the launcher prefix is prepended to at launch.

    PATH is opt-in and treated like any other ``[env]`` key: a literal
    ``PATH = "..."`` contributes the config value and listing ``PATH`` under
    ``forward`` contributes the host ``$PATH``. Fragments are joined
    highest-priority-first -- the matched context ahead of the global scope and,
    within a scope, the literal ahead of the forwarded host value -- so the dedup
    in :func:`~agentbox.store.store_launch` keeps the winning copy of any repeated
    entry. With PATH mentioned nowhere, the sandbox default (:data:`DEFAULT_PATH`)
    stands.
    """
    fragments: list[str] = []
    if matched is not None:
        fragments += _path_sources(dict(matched.env), matched.forward, host_env)
    fragments += _path_sources(dict(config.env), config.forward, host_env)
    return ":".join(fragments) if fragments else DEFAULT_PATH


def ensure_default_mount_sources(agent, home: str) -> None:
    """Create the host source of each built-in default mount so its read-write bind
    has real backing and a fresh agent persists its first run.

    An absent source is silently skipped by bwrap's bind-try, so the agent's writes
    there (its auth/config) would land in the ephemeral tmpfs home and vanish on
    teardown.

    The file-vs-directory distinction is *declared* by each mount, not inferred:
    ``MountSpec.seed`` is ``None`` for a directory and the seed content for a file
    (an agent author knows which it is -- guessing from the path would misclassify a
    dotted directory like ``~/.config.d``). Created only when missing -- an existing
    source is never touched:

    * a directory mount (``seed is None``) -> ``mkdir -p``;
    * a file mount -> its parent dir plus the declared seed (e.g. ``{}\n`` for a JSON
      config, so the agent reads a clean empty config rather than treating a 0-byte
      file as corrupt -- claude logs a parse error and writes a junk backup).

    Aliased mounts (an explicit ``from``) and non-``~/`` paths are the user's to
    provide and are left alone.
    """
    for m in agent.default_mounts:
        if m.from_ is not None or not m.path.startswith("~/"):
            continue
        src = os.path.join(home, m.path[2:])
        if os.path.exists(src):
            continue
        if m.seed is None:  # directory-form
            os.makedirs(src, exist_ok=True)
        else:  # file-form, seeded with the declared content
            os.makedirs(os.path.dirname(src), exist_ok=True)
            with open(src, "w") as fh:
                fh.write(m.seed)


def run(
    agent,
    mounts=(),
    agent_args=(),
    *,
    config=None,
    cwd: str | None = None,
    home: str | None = None,
    env=None,
    store: str | os.PathLike[str] | None = None,
    install=None,
    gateway: str | None = None,
) -> int:
    """The hot path: launch a sandboxed *agent* session for the current directory
    and return its exit code.

    Loads the config (unless one is supplied), resolves the cwd to its context and
    effective mount set, ensures the frozen store is present and current
    (auto-building it once on a missing/drifted stamp -- otherwise no install
    work), then renders the binds/masks and the merged environment and execs the
    store binary **by absolute path** inside a fresh bwrap sandbox fronted by
    pasta. The read-only store binds go on last so nothing configured can shadow
    the in-sandbox binary, and the launcher-prepended ``PATH`` keeps a bare
    ``<command>`` resolving to the store too (the recursion guard).

    Each launch is its own mount and network namespace, so two directories never
    collide on a shared path; mounts and environment are read fresh per launch, so
    a ``config.toml`` edit takes effect on the next launch with no rebuild.

    When the agent has a launch hook, it composes the command to exec plus any
    extra read-only binds and host-loopback ports pasta must forward (e.g. claude's
    MCP/IDE bridge). Without one, the plain store binary and its args are exec'd.

    *mounts* are ad-hoc per-session binds (objects with ``path``/``ro``) consumed
    ahead of the store binds. The remaining keyword arguments override the defaults
    derived from the host (config/cwd/identity/environment/store) and are primarily
    test seams.
    """
    if config is None:
        config = load_user_config()
    cwd = os.getcwd() if cwd is None else cwd
    host_env = os.environ if env is None else env

    ident = host_identity()
    h = ident.home if home is None else home

    s = ensure_store(agent, config, store=store, home=h, install=install)
    ensure_default_mount_sources(agent, h)

    resolution = resolve(config, agent, cwd, home=h)
    matched = resolve_context(config, cwd)
    rendered = render(resolution.mounts)

    cli_binds = tuple(
        Bind(m.path, m.path, mode="ro" if m.ro else "rw", optional=True)
        for m in mounts
    )
    setenv = build_env(agent, config, matched, host_env)

    # The launcher and hook staging directories hold per-launch files bound into
    # the sandbox; both must outlive the launch (the bind sources are read when the
    # sandbox starts), so they wrap the whole boot.
    with tempfile.TemporaryDirectory(prefix="box-launcher.") as launcher_dir, \
            tempfile.TemporaryDirectory(prefix="box-hook.") as hook_stage:
        sl = store_launch(
            agent, h, launcher_dir, store=s,
            base_path=resolve_base_path(config, matched, host_env),
        )

        hook = agent.launch_hook
        if hook is not None:
            plan = hook.prepare(
                exec_path=sl.exec_path,
                agent_args=tuple(agent_args),
                home=h,
                host_env=host_env,
                hook_stage=hook_stage,
            )
        else:
            plan = LaunchPlan(entry_argv=(sl.exec_path, *agent_args))

        spec = SandboxSpec(
            identity=ident,
            argv=plan.entry_argv,
            binds=(*rendered.binds, *cli_binds, *plan.binds, *sl.binds),
            tmpfs=rendered.masks,
            setenv=setenv,
            path=sl.path,
            chdir=resolution.cwd,
        )
        return sandbox.run(spec, ports=list(plan.ports), gateway=gateway)
