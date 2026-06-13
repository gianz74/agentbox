"""Transitional re-export of the MCP/IDE bridge plumbing.

The implementation moved into :mod:`agentbox.agents.claude` (its true home -- the
bridge is claude-specific). This shim keeps the run path's ``from . import mcp``
working unchanged until Phase 2 wires the launch through ``agent.launch_hook``;
it is deleted then.
"""

from __future__ import annotations

from .agents.claude import (  # noqa: F401  (re-exported for the run path)
    MCP_STAGE_DIR,
    apply_lockfile_patch,
    entry_argv,
    lockfile_path,
    loopback_mcp_ports,
    normalize_workspace_folders,
    stage_mcp_configs,
)
