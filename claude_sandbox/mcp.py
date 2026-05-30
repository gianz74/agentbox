"""MCP / IDE bridge plumbing.

The editor (e.g. Emacs) runs an MCP/SSE server on a host loopback port, writes a
lockfile under ``~/.claude/ide/<port>.lock``, and spawns ``claude`` with
``CLAUDE_CODE_SSE_PORT`` set to that port. Two things have to be reconciled for a
sandboxed ``claude`` to connect back:

* **``--mcp-config`` file staging.** A ``--mcp-config <file>`` operand names a host
  path that does not resolve inside the sandbox (its ``/tmp`` and ``$HOME`` are
  fresh). Each such *file* is copied into a private staging directory that is bound
  read-only at a fixed in-sandbox path, and the operand is rewritten to the staged
  path. Inline JSON (a value beginning with ``{``) and missing files pass through
  untouched.

* **The IDE lockfile.** ``claude`` discovers the editor by reading the lockfile,
  whose ``pid`` it validates as a live process owned by its own uid. The editor
  records *its own* (host) pid, which is meaningless inside the sandbox's pid
  namespace. So an in-sandbox bootstrap starts a long-lived uid-matched
  **sentinel**, rewrites the lockfile ``pid`` to the sentinel's
  pid-namespace-local pid, normalizes ``workspaceFolders`` (the editor stores them
  with a trailing slash that ``claude``'s exact ``getcwd()`` compare never has),
  and only then execs ``claude``. The sentinel dies with the ephemeral sandbox.

The cross-boundary network paths -- the SSE/ws port and any streamable-HTTP MCP
ports the editor names in ``--mcp-config`` -- are forwarded by
:mod:`claude_sandbox.net` with one ``pasta -T`` each;
:func:`loopback_mcp_ports` discovers the latter. Everything else here is the
staging and lockfile reconciliation around them.
"""

from __future__ import annotations

import json
import os
import shutil
from urllib.parse import urlparse

from .sandbox import Bind

#: In-sandbox directory the staged ``--mcp-config`` files are bound read-only at.
MCP_STAGE_DIR = "/run/claude-sandbox/mcp"

_MCP_FLAG = "--mcp-config"
_MCP_FLAG_EQ = "--mcp-config="

#: Hostnames whose MCP ports are safe to forward: the loopback the editor's own
#: servers bind. A non-loopback host (a LAN/public address) is never forwarded --
#: doing so would breach the host-localhost isolation the sandbox enforces.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

#: Port assumed for an MCP URL that names none, keyed by scheme.
_SCHEME_DEFAULT_PORTS = {"http": 80, "https": 443}


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
    *stage_dir* read-only at :data:`MCP_STAGE_DIR` (empty when nothing was staged).
    Inline JSON and missing paths are forwarded verbatim so ``claude`` surfaces its
    own errors. (Basenames are assumed unique within one invocation; a collision
    would clobber.)
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
    if os.path.isfile(value):
        try:
            with open(value) as fh:
                return fh.read()
        except OSError:
            return None
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
    ``mcpServers.<name>.url`` inspected; the port of each loopback URL is collected
    in first-seen order. Operands that do not parse, are not objects, or name a
    non-loopback host are skipped silently -- claude validates its own config, and
    forwarding a non-loopback port would breach host-localhost isolation.
    """
    ports: list[int] = []
    seen: set[int] = set()
    for value in _mcp_config_values(claude_args):
        text = _config_json_text(value)
        if text is None:
            continue
        try:
            data = json.loads(text)
        except ValueError:
            continue
        servers = data.get("mcpServers") if isinstance(data, dict) else None
        if not isinstance(servers, dict):
            continue
        for server in servers.values():
            if not isinstance(server, dict):
                continue
            port = _loopback_port(server.get("url"))
            if port is not None and port not in seen:
                seen.add(port)
                ports.append(port)
    return ports


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
