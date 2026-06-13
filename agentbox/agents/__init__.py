"""Built-in agents, registered by name.

Agents are selected by ``argv[0]`` (and by ``box <agent> …``); :data:`AGENTS`
maps each agent's :attr:`~agentbox.agents.base.Agent.name` to its singleton
instance. This is a closed, in-package registry -- not a dynamic plugin system
(decided in PLAN.md §2): promotable to entry-point discovery later, once the
:class:`~agentbox.agents.base.Agent` contract has proven out across more agents.
"""

from __future__ import annotations

from .base import Agent
from .claude import ClaudeAgent
from .copilot import CopilotAgent

#: The agent registry: name -> singleton instance.
AGENTS: dict[str, Agent] = {
    "claude": ClaudeAgent(),
    "copilot": CopilotAgent(),
}

__all__ = ["AGENTS", "Agent"]
