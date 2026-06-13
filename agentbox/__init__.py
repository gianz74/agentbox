"""box -- run the ``claude`` CLI inside an unprivileged bwrap sandbox.

Module layout:

* ``cli``       -- argv dispatch and run-path argument parsing.
* ``config``    -- config file load and validation.
* ``mounts``    -- context/mount-set resolution, masking, and guards (pure logic).
* ``sandbox``   -- builds and runs the bwrap argv from a spec.
* ``net``       -- pasta network lifecycle: NAT, DNS, and the IDE SSE port-forward.
* ``mcp``       -- MCP config staging and IDE lockfile reconciliation.
* ``agents``    -- built-in agents (claude, …) selected by name; their install
                   recipe, env surface, and launch hook.
* ``lifecycle`` -- frozen store install/freeze, host preflight, and the run path.
"""

__all__ = ["cli", "config", "mounts", "sandbox", "net", "mcp", "agents", "lifecycle"]

__version__ = "0.0.1"
