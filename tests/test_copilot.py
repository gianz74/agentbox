"""Tests for the copilot agent.

Locks in the facts verified against the real installer + a scratch install:

  * the registry exposes copilot by name as a singleton :class:`Agent`,
  * its install recipe carries the ``PREFIX``-redirected URL, the lone-binary
    layout (no payload tree) and no version pin (deferred -- the installer pins
    via a ``VERSION`` env var the argv-based recipe cannot express),
  * its env surface forwards ``GITHUB_TOKEN`` (not the ``COPILOT_*`` prefix),
  * ``~/.copilot`` is its single directory default mount,
  * the self-update freeze is the ``COPILOT_AUTO_UPDATE=false`` runtime-env knob,
    and ``disable_self_update`` writes nothing into the store,
  * it carries no launch hook.
"""

from agentbox.agents import AGENTS
from agentbox.agents.base import Agent, InstallRecipe
from agentbox.agents.copilot import CopilotAgent
from agentbox.config import MountSpec


# --- registry -----------------------------------------------------------------

def test_registry_exposes_copilot():
    agent = AGENTS["copilot"]
    assert isinstance(agent, CopilotAgent)
    assert isinstance(agent, Agent)
    assert agent.name == "copilot"
    assert agent.command == "copilot"


# --- install recipe shape -----------------------------------------------------

def test_copilot_install_recipe():
    recipe = CopilotAgent.install
    assert isinstance(recipe, InstallRecipe)
    assert recipe.url == "https://gh.io/copilot-install"
    # Redirected by PREFIX into the store's .local, binary lands at .local/bin.
    assert recipe.redirect_env == "PREFIX"
    assert recipe.redirect_value == "{store}/.local"
    assert recipe.redirect_value.format(store="/x/store") == "/x/store/.local"
    assert recipe.binary_rel == (".local", "bin", "copilot")
    # A lone binary -- no versioned payload tree (confirmed by the release tarball).
    assert recipe.payload_rel is None
    # Version pinning is deferred: the installer reads a VERSION env var, not argv.
    assert recipe.version_args is None


# --- env surface & default mounts ---------------------------------------------

def test_copilot_env_surface():
    # The three token vars copilot accepts for non-interactive auth are forwarded
    # by name; the COPILOT_* prefix is deliberately not (a host COPILOT_AUTO_UPDATE/
    # COPILOT_HOME must not override the freeze / config dir).
    assert CopilotAgent.env_prefixes == ()
    assert CopilotAgent.env_names == ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")


def test_copilot_default_mounts_are_one_directory():
    mounts = CopilotAgent.default_mounts
    assert all(isinstance(m, MountSpec) for m in mounts)
    assert [m.path for m in mounts] == ["~/.copilot"]


def test_copilot_runtime_env_freezes_self_update():
    assert CopilotAgent.runtime_env == (("COPILOT_AUTO_UPDATE", "false"),)


# --- self-update freeze is a no-op write --------------------------------------

def test_disable_self_update_writes_nothing_to_the_store(tmp_path):
    AGENTS["copilot"].disable_self_update(tmp_path)
    assert list(tmp_path.iterdir()) == []  # nothing written into the store


# --- no launch hook -----------------------------------------------------------

def test_copilot_has_no_launch_hook():
    assert AGENTS["copilot"].launch_hook is None
