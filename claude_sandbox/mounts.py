"""Mount-set resolution and guards -- pure logic.

Context resolution (longest-prefix over ``when``, OR), effective mount-set
assembly with the cwd bind (dropping a redundant cwd entry), masking
(``exclude`` -> ``--tmpfs``), and the refuse guards (alias ``from``, cwd
denylist, claude-shadow).

Not yet implemented.
"""

from __future__ import annotations


def resolve(config, cwd):
    raise NotImplementedError("mount resolution is not implemented yet")
