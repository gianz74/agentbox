"""Store lifecycle and the run path.

Frozen-store install and freeze via the native installer with ``HOME``
redirected; the store-identity stamp (version, install method, and schema
version -- a missing or drifted stamp triggers an automatic ``setup``);
host-readiness checks (user namespaces, ``bwrap``, ``pasta``; ``claude`` shim
detection); store removal; and the run path that wires config, mounts, sandbox,
and network together.

Not yet implemented.
"""

from __future__ import annotations


def run(mounts, claude_args):
    raise NotImplementedError("the run path is not implemented yet")


def setup():
    raise NotImplementedError("setup is not implemented yet")


def delete():
    raise NotImplementedError("delete is not implemented yet")
