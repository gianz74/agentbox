"""box -- run an agent CLI (claude, …) inside an unprivileged bwrap sandbox.

Module layout:

* ``cli``        -- argv dispatch (argv[0]->agent shims; ``box <agent> setup|delete``).
* ``config``     -- config file load and validation.
* ``mounts``     -- context/mount-set resolution, masking, and guards (pure logic).
* ``sandbox``    -- builds and runs the bwrap argv from a spec.
* ``net``        -- pasta network lifecycle: NAT, DNS, and loopback port-forwards.
* ``agents``     -- built-in agents (claude, …) selected by name; their install
                    recipe, env surface, and launch hook.
* ``store``      -- the frozen, wrapper-private per-agent store: build/freeze/stamp,
                    launch wiring, and delete.
* ``preflight``  -- host-readiness checks, the shim report, and ``setup``.
* ``run``        -- the run path: config -> mounts -> env -> store -> hook -> sandbox.
"""

__all__ = [
    "cli",
    "config",
    "mounts",
    "sandbox",
    "net",
    "agents",
    "store",
    "preflight",
    "run",
]

__version__ = "0.0.1"
