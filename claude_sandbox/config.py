"""Config loader and validation.

Loads ``config.toml`` and validates the schema: ``[setup]``, ``[[mounts]]``,
``[[contexts]]`` (name / when / mounts), ``[vars]``, ``[mount_groups]`` with
``include``, and ``[env]`` plus per-context ``env`` -- with reserved-key and
duplicate-name checks and ``${VAR}`` expansion.

Not yet implemented.
"""

from __future__ import annotations


def load(path=None):
    raise NotImplementedError("config loading is not implemented yet")
