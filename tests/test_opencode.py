"""Tests for the opencode agent: its declared facts, as verified against the real
installer and upstream source (rationale in :mod:`agentbox.agents.opencode`)."""

from agentbox.agents import AGENTS
from agentbox.agents.base import InstallRecipe
from agentbox.agents.opencode import OpencodeAgent
from agentbox.config import MountSpec


def test_registry_exposes_opencode():
    agent = AGENTS["opencode"]
    assert isinstance(agent, OpencodeAgent)
    assert (agent.name, agent.command) == ("opencode", "opencode")


def test_opencode_install_recipe():
    assert OpencodeAgent.install == InstallRecipe(
        url="https://opencode.ai/install",
        redirect_env="HOME",
        redirect_value="{store}",
        binary_rel=(".opencode", "bin", "opencode"),
        payload_rel=None,
        version_args=None,
    )


def test_opencode_env_surface_is_empty():
    assert (OpencodeAgent.env_prefixes, OpencodeAgent.env_names) == ((), ())


def test_opencode_default_mounts():
    assert OpencodeAgent.default_mounts == (
        MountSpec(path="~/.config/opencode"),
        MountSpec(path="~/.local/share/opencode"),
        MountSpec(path="~/.local/state/opencode"),
    )


def test_opencode_runtime_env_freezes_self_update():
    assert OpencodeAgent.runtime_env == (("OPENCODE_DISABLE_AUTOUPDATE", "true"),)
