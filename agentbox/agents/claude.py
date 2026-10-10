"""The claude agent: install recipe, env surface, and the MCP/IDE launch hook.

:class:`ClaudeAgent` is the built-in agent for Anthropic's ``claude`` CLI: a
single-binary native install (``claude.ai/install.sh`` redirected by ``HOME``),
the ``ANTHROPIC_*``/``CLAUDE_*`` env surface, the ``~/.claude`` auth/config
mounts, and a self-update freeze written into the store's own ``.claude.json``.

Everything claude needs woven into a launch beyond mounts and environment lives
in :class:`ClaudeLaunchHook`, which composes the MCP/IDE bridge plumbing in this
module (formerly ``agentbox.mcp`` plus the SSE bits of ``agentbox.net``):

* **``--mcp-config`` file staging.** A ``--mcp-config <file>`` operand names a
  host path that does not resolve inside the sandbox (its ``/tmp`` and ``$HOME``
  are fresh). Each such *file* is copied into a private staging directory bound
  read-only at a fixed in-sandbox path, and the operand rewritten to the staged
  path. Inline JSON (a value beginning with ``{``) and missing files pass
  through untouched.

* **The IDE lockfile.** ``claude`` discovers the editor by reading a lockfile
  whose ``pid`` it validates as a live process owned by its own uid. The editor
  records *its own* (host) pid, meaningless inside the sandbox's pid namespace.
  So an in-sandbox bootstrap starts a long-lived uid-matched **sentinel**,
  rewrites the lockfile ``pid`` to the sentinel's pid-namespace-local pid,
  normalizes ``workspaceFolders`` (the editor stores them with a trailing slash
  that ``claude``'s exact ``getcwd()`` compare never has), and only then execs
  ``claude``. The sentinel dies with the ephemeral sandbox.

The cross-boundary network paths -- the SSE/ws port, any streamable-HTTP MCP
ports the editor names in ``--mcp-config``, and a host-loopback
``ANTHROPIC_BASE_URL`` (a local Anthropic-compatible gateway) -- are returned by
the hook as the ports pasta must forward (one ``pasta -T`` each);
:func:`loopback_mcp_ports` and :func:`base_url_port` discover the latter two,
and :func:`configured_mcp_ports` finds loopback MCP servers the user registered
in claude's global config.
Everything else here is the staging and lockfile reconciliation around them.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from urllib.parse import urlparse

from ..config import MountSpec
from ..sandbox import Bind
from .base import Agent, InstallRecipe, LaunchHook, LaunchPlan

#: The IDE sets this when it spawns ``claude`` with a local MCP/SSE server.
SSE_PORT_ENV = "CLAUDE_CODE_SSE_PORT"

#: The Anthropic API base claude talks to. When it names a host-loopback address
#: (a local Anthropic-compatible gateway/proxy), its port is forwarded into the
#: sandbox so the agent can reach it; a remote base URL is left to outbound NAT.
BASE_URL_ENV = "ANTHROPIC_BASE_URL"

#: Overrides the directory holding claude's global ``.claude.json`` (default:
#: ``$HOME``); honoured when discovering the MCP servers the user configured.
CONFIG_DIR_ENV = "CLAUDE_CONFIG_DIR"

#: In-sandbox directory the staged ``--mcp-config`` files are bound read-only at.
MCP_STAGE_DIR = "/run/box/mcp"

_MCP_FLAG = "--mcp-config"
_MCP_FLAG_EQ = "--mcp-config="

#: Hostnames whose MCP ports are safe to forward: the loopback the editor's own
#: servers bind. A non-loopback host (a LAN/public address) is never forwarded --
#: doing so would breach the host-localhost isolation the sandbox enforces.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

#: Port assumed for an MCP URL that names none, keyed by scheme.
_SCHEME_DEFAULT_PORTS = {"http": 80, "https": 443}


# --- SSE port ----------------------------------------------------------------


def sse_port_from_env(env: os._Environ[str] | dict[str, str] | None = None) -> int | None:
    """The IDE SSE port from ``CLAUDE_CODE_SSE_PORT``, or ``None`` if unset/invalid.

    The IDE exports it when launching ``claude``; the wrapper reads it from its
    own environment to forward exactly that one port into the sandbox.
    """
    source = os.environ if env is None else env
    raw = source.get(SSE_PORT_ENV)
    if not raw:
        return None
    try:
        port = int(raw)
    except ValueError:
        return None
    return port if 0 < port < 65536 else None


# --- --mcp-config staging ----------------------------------------------------


def _is_inline_json(value: str) -> bool:
    """True if a ``--mcp-config`` operand is inline JSON rather than a file path."""
    return value.lstrip().startswith("{")


def _stage_one(value: str, stage_dir: str, staged: dict[str, str]) -> str:
    """Stage a single ``--mcp-config`` operand, returning the value to forward.

    Inline JSON and non-existent paths pass through unchanged; a real file is
    copied into *stage_dir* and the in-sandbox staged path returned. *staged*
    accumulates ``basename -> host source`` so the caller can bind the dir once.
    """
    if _is_inline_json(value) or not os.path.isfile(value):
        return value
    src = os.path.realpath(value)
    name = os.path.basename(src)
    shutil.copy(src, os.path.join(stage_dir, name))
    staged[name] = src
    return f"{MCP_STAGE_DIR}/{name}"


def stage_mcp_configs(
    claude_args, stage_dir: str
) -> tuple[tuple[str, ...], tuple[Bind, ...]]:
    """Stage any ``--mcp-config`` files referenced in *claude_args*.

    Returns ``(rewritten_args, binds)``: each ``--mcp-config <file>`` /
    ``--mcp-config=<file>`` operand naming a real file is copied into *stage_dir*
    and its operand rewritten to the in-sandbox staged path; the binds expose
    *stage_dir* read-only at :data:`MCP_STAGE_DIR` (empty when nothing was
    staged). Inline JSON and missing paths are forwarded verbatim so ``claude``
    surfaces its own errors. (Basenames are assumed unique within one invocation;
    a collision would clobber.)
    """
    out: list[str] = []
    staged: dict[str, str] = {}
    expect_value = False
    for arg in claude_args:
        if expect_value:
            out.append(_stage_one(arg, stage_dir, staged))
            expect_value = False
        elif arg == _MCP_FLAG:
            out.append(arg)
            expect_value = True
        elif arg.startswith(_MCP_FLAG_EQ):
            value = arg[len(_MCP_FLAG_EQ):]
            out.append(f"{_MCP_FLAG_EQ}{_stage_one(value, stage_dir, staged)}")
        else:
            out.append(arg)

    binds = (
        (Bind(stage_dir, MCP_STAGE_DIR, mode="ro"),) if staged else ()
    )
    return tuple(out), binds


# --- --mcp-config loopback-port discovery ------------------------------------

# The editor drives more than one MCP channel: besides the ``ide`` WebSocket on
# ``CLAUDE_CODE_SSE_PORT`` it injects per-session streamable-HTTP servers as
# ``--mcp-config`` operands, each a ``mcpServers.<name>.url`` on a fresh
# host-loopback port. Every such port the editor names has to be forwarded into
# the sandbox too, or the channel fails with ``ConnectionRefused``. Only the
# editor-supplied loopback ports are collected here -- the same trust boundary as
# the SSE port -- so a non-loopback URL stays unreachable.


def _mcp_config_values(claude_args):
    """Yield each ``--mcp-config`` operand value, spaced or ``=`` form alike."""
    expect_value = False
    for arg in claude_args:
        if expect_value:
            yield arg
            expect_value = False
        elif arg == _MCP_FLAG:
            expect_value = True
        elif arg.startswith(_MCP_FLAG_EQ):
            yield arg[len(_MCP_FLAG_EQ):]


def _config_json_text(value: str) -> str | None:
    """The JSON text of a ``--mcp-config`` operand: the inline value itself, or a
    real file's contents. ``None`` for a missing/unreadable path (claude surfaces
    its own error for that)."""
    if _is_inline_json(value):
        return value
    return _read_text(value) if os.path.isfile(value) else None


def _read_text(path: str) -> str | None:
    """*path*'s contents, or ``None`` when it can't be read or decoded."""
    try:
        with open(path) as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError):
        return None


def _loopback_port(url) -> int | None:
    """The port of *url* when its host is loopback, else ``None``.

    Falls back to the scheme's default port when the URL names none; ``None`` for
    a non-loopback host, an unparseable URL, or a port outside ``1..65535``.
    """
    if not isinstance(url, str):
        return None
    try:
        parsed = urlparse(url)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if host is None or host.lower() not in _LOOPBACK_HOSTS:
        return None
    if port is None:
        port = _SCHEME_DEFAULT_PORTS.get(parsed.scheme)
    if port is None or not 0 < port < 65536:
        return None
    return port


def loopback_mcp_ports(claude_args) -> list[int]:
    """The deduped host-loopback ports named in *claude_args*' ``--mcp-config``.

    Each operand (inline JSON or a real file's contents) is parsed and every
    ``mcpServers.<name>.url`` inspected; the port of each loopback URL is
    collected in first-seen order. Operands that do not parse, are not objects,
    or name a non-loopback host are skipped silently -- claude validates its own
    config, and forwarding a non-loopback port would breach host-localhost
    isolation.
    """
    objects = (_json_object(_config_json_text(v)) for v in _mcp_config_values(claude_args))
    return _mcp_ports(obj.get("mcpServers") for obj in objects if obj is not None)


def _json_object(text: str | None) -> dict | None:
    """*text* parsed as a JSON object, else ``None`` (absent, malformed, non-object)."""
    if text is None:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _mcp_ports(server_maps) -> list[int]:
    """The deduped loopback ports across ``mcpServers`` objects, first-seen order."""
    ports = (
        _loopback_port(server.get("url"))
        for servers in server_maps if isinstance(servers, dict)
        for server in servers.values() if isinstance(server, dict)
    )
    return list(dict.fromkeys(p for p in ports if p is not None))


# --- claude global-config loopback-port discovery ----------------------------

# Servers the user registered in claude's own config file. Only that
# user-authored file is read: a repo's ``.mcp.json`` is untrusted, and a forward
# opens the port to everything in the sandbox, not only to claude.


def _global_config_path(home: str, env) -> str:
    """claude's global config: ``$CLAUDE_CONFIG_DIR/.claude.json``, else ``<home>/.claude.json``."""
    base = env.get(CONFIG_DIR_ENV) or home
    return os.path.join(base, ".claude.json")


def _git_root(cwd: str) -> str | None:
    """The nearest ancestor-or-self of *cwd* holding a ``.git`` entry, else ``None``.

    ``.git`` is a file in worktrees and submodules, hence ``exists``, not ``isdir``.
    """
    path = cwd
    while True:
        if os.path.exists(os.path.join(path, ".git")):
            return path
        parent = os.path.dirname(path)
        if parent == path:
            return None
        path = parent


def _config_server_maps(config: dict, cwd: str):
    """Yield the ``mcpServers`` objects of *config* that apply in *cwd*: top-level,
    then ``projects[<cwd>]``, then ``projects[<git root of cwd>]``."""
    yield config.get("mcpServers")
    projects = config.get("projects")
    if not isinstance(projects, dict):
        return
    for key in (cwd, _git_root(cwd)):
        project = projects.get(key)
        if isinstance(project, dict):
            yield project.get("mcpServers")


def configured_mcp_ports(home: str, cwd: str, env) -> list[int]:
    """The deduped host-loopback MCP ports in claude's global config.

    The config is ``$CLAUDE_CONFIG_DIR/.claude.json`` when that is set in *env*,
    else ``<home>/.claude.json``. Ports are collected in first-seen order from
    its top-level ``mcpServers``, then ``projects[<cwd>].mcpServers``, then
    ``projects[<git root of cwd>].mcpServers``; *cwd* must be normalized, as it
    is matched verbatim against the ``projects`` keys. No other ancestor of
    *cwd* is consulted (a server registered for a parent directory does not
    apply in its subdirectories), and ``<cwd>/.mcp.json`` is never read. A
    missing, unreadable or malformed config, or any entry of the wrong shape,
    contributes no ports, silently -- claude reports its own config errors.
    """
    config = _json_object(_read_text(_global_config_path(home, env)))
    if config is None:
        return []
    return _mcp_ports(_config_server_maps(config, cwd))


# --- ANTHROPIC_BASE_URL loopback port ----------------------------------------


def base_url_port(env=None) -> int | None:
    """The host-loopback port named by ``ANTHROPIC_BASE_URL``, or ``None``.

    Forwarding it lets a sandboxed ``claude`` reach a local Anthropic-compatible
    gateway (e.g. a proxy on ``127.0.0.1`` fronting a local model) -- the same
    loopback trust boundary as the SSE/MCP ports. A non-loopback base URL (the
    default ``api.anthropic.com``, or any remote) yields ``None`` and is reached
    over normal outbound NAT, never a ``-T`` forward.
    """
    env = os.environ if env is None else env
    return _loopback_port(env.get(BASE_URL_ENV))


# --- IDE lockfile reconciliation ---------------------------------------------


def lockfile_path(home: str, port: int) -> str:
    """The IDE lockfile path for *port* under *home* (``.claude/ide/<port>.lock``)."""
    return os.path.join(home, ".claude", "ide", f"{port}.lock")


def normalize_workspace_folders(folders):
    """Strip a trailing slash from each workspace folder (the editor adds one; the
    sandbox ``claude``'s ``getcwd()`` compare never has it). ``"/"`` is left alone."""
    return [
        f.rstrip("/") if isinstance(f, str) and f != "/" else f for f in folders
    ]


def apply_lockfile_patch(data: dict, pid: int) -> tuple[dict, bool]:
    """Reconcile a parsed IDE lockfile with the sandbox: point ``pid`` at the
    in-sandbox sentinel and normalize ``workspaceFolders``.

    Returns ``(patched, changed)`` -- a shallow copy and whether anything moved.
    """
    patched = dict(data)
    changed = False
    if patched.get("pid") != pid:
        patched["pid"] = pid
        changed = True
    folders = patched.get("workspaceFolders")
    if isinstance(folders, list):
        normalized = normalize_workspace_folders(folders)
        if normalized != folders:
            patched["workspaceFolders"] = normalized
            changed = True
    return patched, changed


# The in-sandbox bootstrap, run as ``python3 -c <this> <lock> <claude> <args...>``
# when an SSE port is present. It reconciles the IDE lockfile with the sandbox's
# pid namespace (sentinel pid + trailing-slash normalization -- mirroring
# :func:`apply_lockfile_patch`), then execs ``claude`` by absolute path so the
# recursion guard holds. The sentinel is a long-lived uid-matched process whose
# pid is valid in *this* pid namespace; it is reaped when the ephemeral sandbox
# tears down. A missing or unreadable lockfile is not an error -- claude is exec'd
# regardless.
_BOOTSTRAP = r"""
import json, os, subprocess, sys

lock, claude = sys.argv[1], sys.argv[2]
args = sys.argv[3:]
try:
    with open(lock) as fh:
        data = json.load(fh)
except (OSError, ValueError):
    data = None
if isinstance(data, dict):
    sentinel = subprocess.Popen(
        ["sleep", "2147483647"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    data["pid"] = sentinel.pid
    folders = data.get("workspaceFolders")
    if isinstance(folders, list):
        data["workspaceFolders"] = [
            f.rstrip("/") if isinstance(f, str) and f != "/" else f for f in folders
        ]
    tmp = lock + ".bwrap-tmp"
    try:
        with open(tmp, "w") as fh:
            json.dump(data, fh)
        os.replace(tmp, lock)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
os.execv(claude, [claude] + args)
"""


def _python() -> str:
    """The host ``python3`` (absolute path); the same path resolves in-sandbox."""
    return shutil.which("python3") or "python3"


def entry_argv(
    claude_bin: str, claude_args, *, home: str, sse_port: int | None, python: str | None = None
) -> tuple[str, ...]:
    """The command the sandbox execs.

    Without an SSE port this is just ``claude`` (by absolute path) and its args.
    With one, it is the in-sandbox bootstrap (:data:`_BOOTSTRAP`) that reconciles
    the IDE lockfile for *home*/*sse_port* before exec'ing ``claude`` -- so the
    lockfile carries a sandbox-valid pid and clean workspace paths.
    """
    args = tuple(claude_args)
    if sse_port is None:
        return (claude_bin, *args)
    py = python if python is not None else _python()
    return (py, "-c", _BOOTSTRAP, lockfile_path(home, sse_port), claude_bin, *args)


# --- the agent ---------------------------------------------------------------


class ClaudeLaunchHook(LaunchHook):
    """claude's MCP/IDE bridge as a :class:`~agentbox.agents.base.LaunchHook`.

    :meth:`prepare` stages any ``--mcp-config`` files into *hook_stage*, wraps the
    exec in the lockfile-reconciling bootstrap when the editor exported an SSE
    port, and reports every host-loopback port pasta must forward (the SSE/ws port,
    any streamable-HTTP MCP server ports the ``--mcp-config`` operands name, and a
    loopback ``ANTHROPIC_BASE_URL`` port). A non-IDE launch with no local gateway
    returns the plain command with no extra binds or ports.
    """

    def prepare(
        self,
        *,
        exec_path: str,
        agent_args,
        home: str,
        host_env,
        hook_stage: str,
    ) -> LaunchPlan:
        sse_port = sse_port_from_env(host_env)
        staged_args, binds = stage_mcp_configs(agent_args, hook_stage)
        entry = entry_argv(exec_path, staged_args, home=home, sse_port=sse_port)
        ports = tuple(
            p
            for p in (sse_port, base_url_port(host_env), *loopback_mcp_ports(agent_args))
            if p is not None
        )
        return LaunchPlan(entry_argv=entry, binds=binds, ports=ports)


class ClaudeAgent(Agent):
    """The built-in agent for Anthropic's ``claude`` CLI."""

    name = "claude"
    command = "claude"
    install = InstallRecipe(
        url="https://claude.ai/install.sh",
        redirect_env="HOME",
        redirect_value="{store}",
        binary_rel=(".local", "bin", "claude"),
        payload_rel=(".local", "share", "claude"),
        version_args=lambda v: ["-s", "--", v],
    )
    env_prefixes = ("ANTHROPIC_", "CLAUDE_")
    env_names = ()
    default_mounts = (
        MountSpec(path="~/.claude"),
        # ~/.claude.json is a single-file bind, which is safe here: claude writes it
        # IN-PLACE (open+write, guarded by a separate .lock file), not via an atomic
        # temp+rename, so the write goes through the bind -- verified by writing to a
        # bind-mounted .claude.json (an atomic rename would hit EBUSY on the mount
        # point, as e.g. `git config` does on ~/.gitconfig). If a future claude
        # switches to atomic config writes, redirect it into a directory mount via
        # CLAUDE_CONFIG_DIR (which overrides the config base) instead.
        #
        # seed="{}\n": claude treats a 0-byte config as corrupt (logs a parse error
        # and writes a junk backup), so a fresh source must read as an empty object.
        MountSpec(path="~/.claude.json", seed="{}\n"),
    )

    def disable_self_update(self, store: Path) -> None:
        """Disable claude's self-update in the store's own ``.claude.json``.

        The store is bound read-only at runtime, so it cannot rewrite itself
        anyway; this also keeps the install-time copy from updating, mirroring the
        host's frozen posture. Existing keys in the file are preserved.
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

    @property
    def launch_hook(self) -> LaunchHook:
        return ClaudeLaunchHook()
