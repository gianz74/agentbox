"""pasta network lifecycle.

Starts ``pasta`` against the new network namespace for outbound NAT and DNS
forwarding; adds ``-T <CLAUDE_CODE_SSE_PORT>`` to forward exactly the one IDE SSE
port when an IDE session is detected; dies with the sandbox.

Not yet implemented.
"""

from __future__ import annotations


def start(netns, sse_port=None):
    raise NotImplementedError("pasta startup is not implemented yet")
