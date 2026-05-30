"""MCP / IDE bridge plumbing.

``--mcp-config`` file staging and read-only bind; the pasta SSE port-forward
wiring; the uid-1000 sentinel and lockfile pid patch (the recorded pid is wrong
once the sandbox has its own pid namespace); lockfile trailing-slash
normalization.

Not yet implemented.
"""

from __future__ import annotations


def stage(env):
    raise NotImplementedError("MCP staging is not implemented yet")
