"""bwrap mechanism layer.

A pure mechanism over the ``bwrap`` binary: builds the argv from a spec -- host
userspace read-only (``/usr``, ``/lib*``, a curated ``/etc``); synthesized
``/proc``, ``/dev``, tmpfs ``/tmp`` and ``/run``; a constructed
``passwd``/``group`` bound read-only; a single-uid ``--uid``/``--gid`` map; user
binds then claude binds last; ``--unshare-net`` and the other namespaces;
``--clearenv`` with ``--setenv``; ``--chdir``; ``--die-with-parent``;
``--new-session`` -- then ``exec``s it.

Not yet implemented.
"""

from __future__ import annotations


def build_argv(spec):
    raise NotImplementedError("the bwrap argv builder is not implemented yet")
