"""Tests for the agent abstraction and the claude agent.

  * the registry exposes claude by name as a singleton :class:`Agent`,
  * claude's install recipe carries the right URL, redirect, layout and pin args,
  * its env surface and default auth/config mounts are as documented,
  * ``disable_self_update`` writes ``autoUpdates: false`` into the store's own
    ``.claude.json``, preserving any existing keys and surviving a corrupt file,
  * ``ClaudeLaunchHook.prepare`` composes the MCP/IDE bridge into a
    :class:`LaunchPlan`: plain exec with no IDE, the bootstrap wrapper + forwarded
    ports with one, and ``--mcp-config`` file staging.
"""

import json

from agentbox.agents import AGENTS
from agentbox.agents.base import Agent, InstallRecipe, LaunchHook, LaunchPlan
from agentbox.agents.claude import ClaudeAgent, ClaudeLaunchHook
from agentbox.config import MountSpec
from agentbox.sandbox import Bind


# --- registry -----------------------------------------------------------------

def test_registry_exposes_claude():
    assert "claude" in AGENTS
    agent = AGENTS["claude"]
    assert isinstance(agent, ClaudeAgent)
    assert isinstance(agent, Agent)
    assert agent.name == "claude"
    assert agent.command == "claude"


def test_registry_keys_match_agent_names():
    # Each agent is registered under its own ``name``; the keys are the selectors.
    assert all(name == agent.name for name, agent in AGENTS.items())


# --- install recipe shape -----------------------------------------------------

def test_claude_install_recipe():
    recipe = ClaudeAgent.install
    assert isinstance(recipe, InstallRecipe)
    assert recipe.url == "https://claude.ai/install.sh"
    assert recipe.redirect_env == "HOME"
    assert recipe.redirect_value == "{store}"
    assert recipe.binary_rel == (".local", "bin", "claude")
    assert recipe.payload_rel == (".local", "share", "claude")
    # The version pin maps to the installer's positional argv.
    assert recipe.version_args("2.1.150") == ["-s", "--", "2.1.150"]


def test_redirect_value_is_a_store_template():
    # The redirect value is a template over {store}: HOME points at the store root.
    assert ClaudeAgent.install.redirect_value.format(store="/x/store") == "/x/store"


# --- env surface & default mounts ---------------------------------------------

def test_claude_env_surface():
    assert ClaudeAgent.env_prefixes == ("ANTHROPIC_", "CLAUDE_")
    assert ClaudeAgent.env_names == ()


def test_claude_default_mounts():
    mounts = ClaudeAgent.default_mounts
    assert all(isinstance(m, MountSpec) for m in mounts)
    assert [m.path for m in mounts] == ["~/.claude", "~/.claude.json"]


# --- disable_self_update ------------------------------------------------------

def test_disable_self_update_writes_flag(tmp_path):
    AGENTS["claude"].disable_self_update(tmp_path)
    data = json.loads((tmp_path / ".claude.json").read_text())
    assert data == {"autoUpdates": False}


def test_disable_self_update_preserves_existing_keys(tmp_path):
    (tmp_path / ".claude.json").write_text(json.dumps({"keep": 1, "autoUpdates": True}))
    AGENTS["claude"].disable_self_update(tmp_path)
    data = json.loads((tmp_path / ".claude.json").read_text())
    assert data == {"keep": 1, "autoUpdates": False}


def test_disable_self_update_overwrites_corrupt_file(tmp_path):
    (tmp_path / ".claude.json").write_text("{not json")
    AGENTS["claude"].disable_self_update(tmp_path)
    data = json.loads((tmp_path / ".claude.json").read_text())
    assert data == {"autoUpdates": False}


# --- the launch hook ----------------------------------------------------------

def test_launch_hook_is_a_hook():
    hook = AGENTS["claude"].launch_hook
    assert isinstance(hook, LaunchHook)
    assert isinstance(hook, ClaudeLaunchHook)


def test_prepare_plain_without_ide(tmp_path):
    plan = ClaudeLaunchHook().prepare(
        exec_path="/h/.local/bin/claude",
        agent_args=["-p", "hi"],
        home="/h",
        host_env={"TERM": "xterm"},
        hook_stage=str(tmp_path),
    )
    assert isinstance(plan, LaunchPlan)
    assert plan.entry_argv == ("/h/.local/bin/claude", "-p", "hi")
    assert plan.binds == ()
    assert plan.ports == ()


def test_prepare_wraps_bootstrap_and_forwards_sse_port(tmp_path):
    plan = ClaudeLaunchHook().prepare(
        exec_path="/h/.local/bin/claude",
        agent_args=["-p", "hi"],
        home="/h",
        host_env={"CLAUDE_CODE_SSE_PORT": "4321"},
        hook_stage=str(tmp_path),
    )
    # The IDE SSE port is forwarded and the exec wraps the lockfile bootstrap.
    assert plan.ports == (4321,)
    assert plan.entry_argv[1] == "-c"
    assert plan.entry_argv[3] == "/h/.claude/ide/4321.lock"
    assert plan.entry_argv[4] == "/h/.local/bin/claude"


def test_prepare_stages_mcp_config_and_collects_ports(tmp_path):
    cfg = tmp_path / "servers.json"
    cfg.write_text(json.dumps({"mcpServers": {"t": {"url": "http://127.0.0.1:7000/mcp"}}}))
    stage = tmp_path / "stage"
    stage.mkdir()

    plan = ClaudeLaunchHook().prepare(
        exec_path="/h/.local/bin/claude",
        agent_args=["--mcp-config", str(cfg), "--print"],
        home="/h",
        host_env={"CLAUDE_CODE_SSE_PORT": "4321"},
        hook_stage=str(stage),
    )

    # The file is staged read-only and the operand rewritten to the staged path.
    assert plan.binds == (Bind(str(stage), "/run/box/mcp", mode="ro"),)
    assert "/run/box/mcp/servers.json" in plan.entry_argv
    # Both the SSE port and the MCP server's loopback port are forwarded, SSE first.
    assert plan.ports == (4321, 7000)
