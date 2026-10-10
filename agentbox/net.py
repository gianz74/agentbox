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
  It closes the *gateway* path only; namespace-loopback forwarding is a separate
  pasta mechanism (``-T``/``-U``), handled below.
* ``--dns-forward <gateway>`` makes pasta answer DNS on the gateway address and
  forward to the host's real resolver, which is what the synthesized
  ``/etc/resolv.conf`` points at. This works even when the host uses a loopback
  stub resolver (otherwise unreachable from inside the namespace).
* ``-T <port>`` forwards a single port from the namespace loopback out to the
  host's loopback, one per port the agent's launch hook names. With no ports,
  ``-T none`` closes TCP, because pasta's default ``auto`` would forward
  **every** host-loopback port.
* ``-4`` always: the sandbox network is IPv4-only. pasta's ``-T`` cannot bind a
  single family (it rejects a listen address), so without ``-4`` it listens on
  both ``127.0.0.1`` and ``::1`` for every forwarded port and splices each family
  to the host's same-family loopback. A host service bound to IPv4 only then
  has its ``::1`` connect *accepted* inside and reset once the host refuses --
  and clients that race both loopbacks for ``localhost`` (Bun, curl) take the
  accepted ``::1`` and never fall back. With ``-4`` that connect is refused, as
  on the host, and ``127.0.0.1`` wins. The cost: no IPv6 for the sandbox at all,
  so a ``[::1]`` URL fails inside however the host service binds -- which is why
  :data:`LOOPBACK_HOSTS` omits it.
* ``-U none`` always. Agents need no UDP to the host loopback (MCP and gateways are
  TCP); it also stops pasta mirroring UDP for each named TCP port.

``--die-with-parent`` on the sandbox plus pasta's foreground mode (``-f``) make
pasta exit when the (ephemeral) sandbox does.
"""

from __future__ import annotations

import shutil
import subprocess

PASTA = "pasta"
_PASTA_PACKAGE = "passt"

#: Host names that reach a forwarded port from inside the sandbox. No ``::1``:
#: under ``-4`` nothing listens there, so forwarding a ``[::1]`` URL's port is moot.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1"})


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


def wrap_argv(
    inner_argv: list[str], *, gateway: str, ports=()
) -> list[str]:
    """Wrap *inner_argv* (the sandbox command) so ``pasta`` is its parent.

    Builds ``pasta -4 --config-net -f -q --no-map-gw --dns-forward <gateway> -U none
    (-T none | -T <port>...) -- <inner_argv...>``: pasta sets up the isolated
    netns, then runs the sandbox inside it. *ports* is the iterable of
    host-loopback ports to forward (those the agent's launch hook names);
    one ``-T`` is emitted per distinct port, in first-seen order, with ``None``
    entries and duplicates dropped. pasta's ``-T``/``-U`` default is ``auto``,
    which forwards every host-loopback port, so when no port survives the
    absence is made explicit with ``-T none``.
    """
    argv = [
        PASTA,
        "-4",
        "--config-net",
        "-f",
        "-q",
        "--no-map-gw",
        "--dns-forward",
        gateway,
        "-U",
        "none",
    ]
    seen: set[int] = set()
    for port in ports:
        if port is None or port in seen:
            continue
        seen.add(port)
        argv += ["-T", str(port)]
    if not seen:
        argv += ["-T", "none"]
    argv += ["--", *inner_argv]
    return argv
