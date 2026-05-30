"""pasta network lifecycle.

``pasta`` is the parent of the sandbox: it creates the isolated network namespace
(an unprivileged userspace tap), provides outbound NAT and DNS forwarding, and
then spawns the ``bwrap`` sandbox inside that namespace. Running pasta as the
parent -- rather than starting the sandbox first and attaching pasta to its
namespace by pid -- avoids a namespace setup race entirely.

Network posture:

* ``--no-map-gw`` is mandatory. Without it pasta maps the gateway address back to
  the host, so the sandbox could reach every host-localhost service via
  ``http://<gateway>:<port>`` -- the exact isolation the wrapper exists to prevent.
* ``--dns-forward <gateway>`` makes pasta answer DNS on the gateway address and
  forward to the host's real resolver, which is what the synthesized
  ``/etc/resolv.conf`` points at. This works even when the host uses a loopback
  stub resolver (otherwise unreachable from inside the namespace).
* ``-T <port>`` forwards a single port from the namespace loopback out to the
  host's loopback. One is emitted per host-loopback MCP port the editor named (the
  SSE/ws port plus any streamable-HTTP MCP servers), so the sandbox reaches the
  editor's local servers without exposing any other host service.

``--die-with-parent`` on the sandbox plus pasta's foreground mode (``-f``) make
pasta exit when the (ephemeral) sandbox does.
"""

from __future__ import annotations

import os
import shutil
import subprocess

PASTA = "pasta"
_PASTA_PACKAGE = "passt"

#: The IDE sets this when it spawns ``claude`` with a local MCP/SSE server.
SSE_PORT_ENV = "CLAUDE_CODE_SSE_PORT"


class NetworkError(Exception):
    """A user-facing networking error (missing ``pasta``, no route, etc.)."""


def ensure_pasta() -> None:
    """Raise :class:`NetworkError` naming the apt package if ``pasta`` is absent."""
    if shutil.which(PASTA) is None:
        raise NetworkError(
            f"{PASTA} not found on PATH -- install the {_PASTA_PACKAGE!s} package "
            f"(e.g. `sudo apt install {_PASTA_PACKAGE}`)"
        )


def gateway() -> str:
    """The default-route gateway address, discovered per launch.

    This is pasta's NAT/DNS-forward address and differs across machines, so it is
    never hardcoded. Raises :class:`NetworkError` if no outbound route exists.
    """
    try:
        out = subprocess.run(
            ["ip", "route", "get", "1.1.1.1"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as e:
        raise NetworkError(f"could not query the default route via `ip route`: {e}") from e

    tokens = out.split()
    if "via" in tokens:
        return tokens[tokens.index("via") + 1]
    raise NetworkError(
        "no default gateway found (need an outbound route for the sandbox network)"
    )


def sse_port_from_env(env: "os._Environ[str] | dict[str, str] | None" = None) -> int | None:
    """The IDE SSE port from ``CLAUDE_CODE_SSE_PORT``, or ``None`` if unset/invalid.

    The IDE exports it when launching ``claude``; the wrapper reads it from its own
    environment to forward exactly that one port into the sandbox.
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


def wrap_argv(
    inner_argv: list[str], *, gateway: str, ports=()
) -> list[str]:
    """Wrap *inner_argv* (the sandbox command) so ``pasta`` is its parent.

    Builds ``pasta --config-net -f -q --no-map-gw --dns-forward <gateway>
    [-T <port>]... -- <inner_argv...>``: pasta sets up the isolated netns, then
    runs the sandbox inside it. *ports* is the iterable of host-loopback ports to
    forward (the IDE SSE port plus any MCP server ports); one ``-T`` is emitted per
    distinct port, in first-seen order, with ``None`` entries and duplicates
    dropped.
    """
    argv = [
        PASTA,
        "--config-net",
        "-f",
        "-q",
        "--no-map-gw",
        "--dns-forward",
        gateway,
    ]
    seen: set[int] = set()
    for port in ports:
        if port is None or port in seen:
            continue
        seen.add(port)
        argv += ["-T", str(port)]
    argv += ["--", *inner_argv]
    return argv
