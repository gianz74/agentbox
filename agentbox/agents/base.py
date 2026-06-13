"""The agent abstraction: a built-in, name-selected agent and its launch hook.

A *box* sandbox is generic; everything agent-specific -- how the tool installs,
which environment it reads, where its auth lives, and any editor/IDE bridge it
needs -- is carried by an :class:`Agent`. Agents are built-in modules registered
by name (see :mod:`agentbox.agents`) and selected by ``argv[0]``; this is
deliberately an internal abstraction, not a dynamic plugin system (YAGNI until
the contract has proven out across more than one agent).

Three small pieces make it up:

* :class:`InstallRecipe` -- install reduced to data: a single-binary native
  installer redirected into the wrapper-private store by one environment
  variable. The shared install *procedure* (run installer, verify binary,
  disable self-update, stamp) is agent-neutral and parameterized by this recipe;
  only the genuinely divergent bits are per-agent.
* :class:`LaunchHook` / :class:`LaunchPlan` -- an optional, per-agent hook into
  launch assembly. :meth:`LaunchHook.prepare` returns the command the sandbox
  execs plus any extra read-only binds and host-loopback ports the launch needs.
  claude's hook houses the whole MCP/IDE bridge; an agent with no editor
  integration has no hook.
* :class:`Agent` -- the declarative identity/data (name, command, recipe, env
  surface, default mounts) plus the two behavioral seams: ``disable_self_update``
  and the optional ``launch_hook``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..config import MountSpec
from ..sandbox import Bind


@dataclass(frozen=True)
class InstallRecipe:
    """A single-binary native install reduced to data.

    The installer is fetched from ``url`` and run with the environment variable
    ``redirect_env`` set to ``redirect_value`` (a template over ``{store}``) so
    the install lands inside the wrapper-private store rather than the real
    ``~/.local``. ``binary_rel`` locates the installed launcher under the store
    root; ``payload_rel`` the versioned payload tree (``None`` for a lone
    binary). ``version_args``, when set, maps a requested version string to the
    installer's extra argv that pins it.
    """

    url: str
    redirect_env: str
    redirect_value: str
    binary_rel: tuple[str, ...]
    payload_rel: tuple[str, ...] | None
    version_args: Callable[[str], list[str]] | None = None


@dataclass(frozen=True)
class LaunchPlan:
    """A launch hook's contribution to a sandbox launch.

    ``entry_argv`` is the command the sandbox execs (an agent that needs no
    wrapping returns the plain ``(exec_path, *args)``). ``binds`` are extra
    read-only binds the launch needs (e.g. staged config files); ``ports`` are
    the host-loopback ports pasta must forward in.
    """

    entry_argv: tuple[str, ...]
    binds: tuple[Bind, ...] = ()
    ports: tuple[int, ...] = ()


class LaunchHook(ABC):
    """An optional, per-agent hook into launch assembly.

    A single method, :meth:`prepare`, is called on the run path once the store
    binary's in-sandbox path is known; it returns a :class:`LaunchPlan`. The hook
    is the home for everything an agent weaves into the launch beyond plain
    mounts and environment -- notably an editor/IDE bridge.
    """

    @abstractmethod
    def prepare(
        self,
        *,
        exec_path: str,
        agent_args,
        home: str,
        host_env,
        hook_stage: str,
    ) -> LaunchPlan:
        """Assemble this agent's launch contribution.

        *exec_path* is the absolute in-sandbox path of the store binary;
        *agent_args* the user's CLI arguments; *home* the in-sandbox ``$HOME``;
        *host_env* the launcher's environment; *hook_stage* a writable host
        directory (bound into the sandbox by the returned binds) the hook may
        stage files into.
        """


class Agent(ABC):
    """A built-in, name-selected agent.

    Subclasses set the declarative attributes (identity, install recipe, env
    surface, default mounts) and implement :meth:`disable_self_update`; an agent
    with an editor bridge also overrides :attr:`launch_hook`. The shared install
    *procedure* is agent-neutral and driven by :attr:`install`; subclasses never
    reimplement it.
    """

    #: Registry key and per-agent store subdirectory, e.g. ``"claude"``.
    name: str
    #: The shim/launcher command name selection keys on (``argv[0]``).
    command: str
    #: How this agent installs into the wrapper-private store.
    install: InstallRecipe
    #: Host env names forwarded by prefix (e.g. ``("ANTHROPIC_", "CLAUDE_")``).
    env_prefixes: tuple[str, ...] = ()
    #: Host env names forwarded verbatim (e.g. ``("GITHUB_TOKEN",)``).
    env_names: tuple[str, ...] = ()
    #: Mounts every launch of this agent needs (its auth/config dirs).
    default_mounts: tuple[MountSpec, ...] = ()

    @abstractmethod
    def disable_self_update(self, store: Path) -> None:
        """Freeze the store's own copy so it cannot self-update at runtime."""

    @property
    def launch_hook(self) -> "LaunchHook | None":
        """The agent's launch hook, or ``None`` when it needs none (the default)."""
        return None
