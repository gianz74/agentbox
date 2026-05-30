"""bwrap mechanism layer.

A pure mechanism over the ``bwrap`` binary: given a :class:`SandboxSpec` it
assembles a ``bwrap`` argv and runs it. It carries no mount-set policy (which host
paths to expose is decided elsewhere) and no store logic -- the caller supplies
the binds, tmpfs masks, env, working directory and the command to exec.

What the scaffold always provides, independent of the caller's binds:

* **Host userspace, read-only** -- ``/usr`` plus the usrmerge symlink roots
  ``/lib``, ``/lib64``, ``/bin``, ``/sbin``; synthesized ``/proc`` (``--proc``),
  ``/dev`` (``--dev``), and tmpfs ``/tmp`` and ``/run``.
* **Curated read-only ``/etc``** -- TLS material (``ssl``, ``ca-certificates*``),
  ``alternatives`` and ``localtime`` (many host tools resolve through them),
  ``hosts``, plus the synthesized ``passwd``/``group``/``nsswitch.conf`` and (when
  a nameserver is given) ``resolv.conf``. Everything else under ``/etc`` is absent
  by default; the set is additive.
* **Identity** -- a constructed minimal ``passwd``/``group`` (a single user at
  uid/gid 1000, home = the real ``$HOME``) bound read-only over ``/etc``, plus a
  single-uid ``--uid``/``--gid`` map. bwrap maps the host's real uid onto the one
  sandbox uid, so files written from inside land owned by ``$USER`` on the host
  (ownership parity) with no ``subuid`` range and no NSS/sssd dependency. The
  trimmed ``nsswitch.conf`` (``files`` only) makes the constructed passwd
  authoritative.
* **Namespaces** -- ``--unshare-user``/``ipc``/``pid``/``uts``/``cgroup``. The
  network namespace is *not* unshared here: the sandbox runs inside a network
  namespace its parent already set up (see :mod:`claude_sandbox.net`), so ``bwrap``
  inherits that isolated netns rather than creating its own.
* **Exec posture** -- ``--clearenv`` then explicit ``--setenv``; ``--chdir``;
  ``--die-with-parent`` (so the sandbox dies with its launcher); ``--new-session``.

No restrictive ``--seccomp`` and no capability drops -- bwrap's permissive
default.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

BWRAP = "bwrap"
_BWRAP_PACKAGE = "bubblewrap"

# The single uid/gid the sandbox process runs as. Constant across machines: bwrap
# maps the host's real uid (whatever it is, including a high sssd uid) onto this
# one id, so the constructed passwd is identical everywhere and ownership parity
# holds without an /etc/subuid range.
SANDBOX_UID = 1000
SANDBOX_GID = 1000

# Default search path inside the sandbox (host userspace is bound read-only).
DEFAULT_PATH = "/usr/bin:/bin"

# Host userspace roots. /usr is required; the usrmerge symlink roots are bound
# "try" since a fully merged host may not have them as real directories.
_USR_BIND = ("/usr",)
_USRMERGE_TRY = ("/lib", "/lib64", "/bin", "/sbin")

# Curated read-only /etc entries taken verbatim from the host. /etc/ssl is
# essential (TLS); the rest are bound "try" so a host missing one does not abort
# the launch. The synthesized passwd/group/nsswitch/resolv.conf are added
# separately by the builder.
_ETC_SSL = "/etc/ssl"
_ETC_TRY = (
    "/etc/ca-certificates",
    "/etc/ca-certificates.conf",
    "/etc/alternatives",
    "/etc/localtime",
    "/etc/hosts",
)


class SandboxError(Exception):
    """A user-facing sandbox error (missing ``bwrap``, etc.)."""


@dataclass(frozen=True)
class Identity:
    """The sandbox user. ``uid``/``gid`` are the in-sandbox ids (the single-uid
    map handles the host side); ``home`` is the real host ``$HOME`` reproduced
    byte-for-byte inside."""

    user: str
    home: str
    uid: int = SANDBOX_UID
    gid: int = SANDBOX_GID


@dataclass(frozen=True)
class Bind:
    """A bind mount of a host path into the sandbox.

    ``optional`` uses bwrap's ``*-bind-try`` form, which silently skips a source
    that is absent on this machine (per-machine configs that reference paths only
    some hosts have)."""

    src: str
    dest: str
    mode: str = "ro"  # "ro" | "rw"
    optional: bool = False


@dataclass(frozen=True)
class SandboxSpec:
    """Everything the builder needs that the scaffold does not fix.

    ``binds`` are applied in order *after* the host scaffold, so a later bind
    overlays an earlier path. ``tmpfs`` adds writable empty mounts (e.g. masking a
    sub-path of a bound tree). ``setenv`` is extra environment layered on top of
    the always-set ``HOME``/``USER``/``PATH``. ``nameserver``, when set, is the
    address written into the synthesized ``/etc/resolv.conf``.
    """

    identity: Identity
    argv: tuple[str, ...]  # the command to exec inside the sandbox
    binds: tuple[Bind, ...] = ()
    tmpfs: tuple[str, ...] = ()
    setenv: dict[str, str] = field(default_factory=dict)
    path: str = DEFAULT_PATH
    chdir: str | None = None
    nameserver: str | None = None


def host_identity() -> Identity:
    """The sandbox identity derived from the host: real ``$USER`` and ``$HOME``,
    reproduced at the constant sandbox uid/gid."""
    home = os.path.expanduser("~")
    user = os.environ.get("USER") or os.path.basename(home)
    return Identity(user=user, home=home)


def build_spec(
    argv: Sequence[str],
    *,
    binds: Sequence[Bind] = (),
    tmpfs: Sequence[str] = (),
    setenv: dict[str, str] | None = None,
    chdir: str | None = None,
    identity: Identity | None = None,
) -> SandboxSpec:
    """Assemble a :class:`SandboxSpec` for the command *argv*.

    The identity defaults to the host's real ``$USER``/``$HOME`` (see
    :func:`host_identity`). Pass *identity* to reproduce a different one -- e.g. a
    federated ``user@REALM`` name -- which flows verbatim into the constructed
    ``passwd``/``group``, so ``id``/``whoami`` inside resolve to it regardless of
    the host's own uid. *binds* overlay the host scaffold in order, *tmpfs* masks
    paths beneath it, and *setenv* layers on top of the baseline
    ``HOME``/``USER``/``PATH``.
    """
    return SandboxSpec(
        identity=identity if identity is not None else host_identity(),
        argv=tuple(argv),
        binds=tuple(binds),
        tmpfs=tuple(tmpfs),
        setenv=dict(setenv) if setenv else {},
        chdir=chdir,
    )


def ensure_bwrap() -> None:
    """Raise :class:`SandboxError` naming the apt package if ``bwrap`` is absent."""
    if shutil.which(BWRAP) is None:
        raise SandboxError(
            f"{BWRAP} not found on PATH -- install the {_BWRAP_PACKAGE!s} package "
            f"(e.g. `sudo apt install {_BWRAP_PACKAGE}`)"
        )


def _write_identity_files(spec: SandboxSpec, etc_dir: Path) -> dict[str, str]:
    """Synthesize the constructed ``/etc`` files into *etc_dir*.

    Returns a ``host_path -> sandbox_path`` map of read-only binds to add. A
    minimal ``passwd``/``group`` makes the single sandbox user resolvable without
    NSS; ``nsswitch.conf`` is trimmed to ``files`` (plus ``dns`` for hosts) so the
    constructed passwd is authoritative and no extra NSS modules are pulled in.
    """
    ident = spec.identity
    files = {
        "passwd": f"{ident.user}:x:{ident.uid}:{ident.gid}:{ident.user}:{ident.home}:/bin/bash\n",
        "group": f"{ident.user}:x:{ident.gid}:\n",
        "nsswitch.conf": "passwd: files\ngroup: files\nhosts: files dns\n",
    }
    if spec.nameserver is not None:
        files["resolv.conf"] = f"nameserver {spec.nameserver}\n"

    binds: dict[str, str] = {}
    for name, content in files.items():
        p = etc_dir / name
        p.write_text(content)
        binds[str(p)] = f"/etc/{name}"
    return binds


def build_argv(spec: SandboxSpec, *, etc_dir: str | os.PathLike[str]) -> list[str]:
    """Assemble the ``bwrap`` argv for *spec*.

    *etc_dir* is a caller-owned directory the synthesized ``/etc`` files are
    written into; it must stay alive until the launched process has started (the
    bind sources are read at launch). The command in ``spec.argv`` is placed last,
    after a ``--`` terminator.
    """
    etc = Path(etc_dir)
    ident = spec.identity
    argv: list[str] = [BWRAP]

    # Namespaces: everything but the network (the parent owns the netns).
    argv += [
        "--unshare-user",
        "--unshare-ipc",
        "--unshare-pid",
        "--unshare-uts",
        "--unshare-cgroup",
    ]
    argv += ["--uid", str(ident.uid), "--gid", str(ident.gid)]

    # Host userspace, read-only.
    for d in _USR_BIND:
        argv += ["--ro-bind", d, d]
    for d in _USRMERGE_TRY:
        argv += ["--ro-bind-try", d, d]

    # Curated /etc taken from the host.
    argv += ["--ro-bind", _ETC_SSL, _ETC_SSL]
    for d in _ETC_TRY:
        argv += ["--ro-bind-try", d, d]

    # Synthesized identity + resolver, bound read-only over /etc.
    for host_path, sandbox_path in _write_identity_files(spec, etc).items():
        argv += ["--ro-bind", host_path, sandbox_path]

    # Synthesized virtual filesystems.
    argv += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--tmpfs", "/run"]

    # A fresh writable home skeleton; caller binds may overlay paths beneath it.
    argv += ["--tmpfs", ident.home]

    # Caller binds, in order: a later bind overlays an earlier path, so the run
    # path places the read-only claude store binds last and nothing configured
    # ahead of them can shadow the in-sandbox claude.
    for b in spec.binds:
        if b.mode not in ("ro", "rw"):
            raise SandboxError(f"bind {b.dest}: invalid mode {b.mode!r}")
        flag = ("--ro-bind" if b.mode == "ro" else "--bind") + ("-try" if b.optional else "")
        argv += [flag, b.src, b.dest]

    # Masking: an empty tmpfs over a sub-path of a bind above fully shadows the
    # real contents. Emitted after the binds (argv order) so each mask lands on
    # top of the tree it sits inside.
    for t in spec.tmpfs:
        argv += ["--tmpfs", t]

    # Environment: a cleared slate plus the identity baseline, then caller extras.
    argv += ["--clearenv"]
    argv += ["--setenv", "HOME", ident.home]
    argv += ["--setenv", "USER", ident.user]
    argv += ["--setenv", "PATH", spec.path]
    for key, value in spec.setenv.items():
        argv += ["--setenv", key, value]

    if spec.chdir is not None:
        argv += ["--chdir", spec.chdir]

    argv += ["--die-with-parent", "--new-session"]
    argv += ["--", *spec.argv]
    return argv


def run(spec: SandboxSpec, *, ports=(), gateway: str | None = None) -> int:
    """Boot the sandbox for *spec* under an isolated network and wait for it.

    ``pasta`` is the parent: it creates the isolated network namespace (outbound
    NAT + DNS forwarding, forwarding each host-loopback port in *ports* -- the IDE
    SSE port plus any MCP server ports), then spawns the ``bwrap`` sandbox inside
    it. The synthesized ``resolv.conf`` is pointed at pasta's gateway. Returns the
    sandbox process's exit code.
    """
    from . import net

    ensure_bwrap()
    net.ensure_pasta()
    gw = gateway if gateway is not None else net.gateway()
    spec = replace(spec, nameserver=gw)

    # The synthesized /etc files must outlive the launch; the temp dir is removed
    # once pasta/bwrap have exited.
    import tempfile

    with tempfile.TemporaryDirectory(prefix="claude-sandbox-etc.") as etc_dir:
        inner = build_argv(spec, etc_dir=etc_dir)
        argv = net.wrap_argv(inner, gateway=gw, ports=ports)
        return subprocess.run(argv).returncode
