"""Tests for cli dispatch and the run-path leading-block parse.

  * ``box <agent> setup|delete`` route to their handlers (with ``--from-host``
    parsed for setup); an unknown agent / missing subcommand errors,
  * an agent shim (``prog`` = the agent command) is pure passthrough to the run
    path: ``-p hi`` reaches ``run.run`` with that agent and ``['-p','hi']``,
  * the subcommand surface is exactly ``{setup, delete}``,
  * ``--mount /x -- --foo`` parses ``/x`` as a mount and ``--foo`` as passthrough.

The dispatch tests stub ``preflight.setup`` / ``store.delete`` / ``run.run`` to
recorders so routing is asserted without booting a real sandbox.
"""

import pytest

from agentbox import cli
from agentbox.agents import AGENTS
from agentbox.cli import Mount

CLAUDE = AGENTS["claude"]


# --- box <agent> management routing ------------------------------------------

def test_setup_routes_to_preflight(monkeypatch):
    calls = {}
    monkeypatch.setattr(cli, "load_user_config", lambda: "CFG")
    monkeypatch.setattr(
        cli.preflight, "setup",
        lambda agent, config, *, from_host=False: calls.update(
            agent=agent, config=config, from_host=from_host) or 0,
    )
    assert cli.dispatch(["claude", "setup"]) == 0
    assert calls == {"agent": CLAUDE, "config": "CFG", "from_host": False}


def test_setup_from_host_flag_is_parsed(monkeypatch):
    calls = {}
    monkeypatch.setattr(cli, "load_user_config", lambda: "CFG")
    monkeypatch.setattr(
        cli.preflight, "setup",
        lambda agent, config, *, from_host=False: calls.update(from_host=from_host) or 0,
    )
    assert cli.dispatch(["claude", "setup", "--from-host"]) == 0
    assert calls == {"from_host": True}


def test_setup_rejects_unexpected_arg(monkeypatch):
    monkeypatch.setattr(cli, "load_user_config", lambda: "CFG")
    monkeypatch.setattr(cli.preflight, "setup", lambda *a, **k: 0)
    assert cli.main(["claude", "setup", "--bogus"]) == 2


def test_delete_routes_to_store(monkeypatch):
    called = {}
    monkeypatch.setattr(cli.store, "delete", lambda agent: called.update(agent=agent) or 0)
    assert cli.dispatch(["claude", "delete"]) == 0
    assert called == {"agent": CLAUDE}


def test_unknown_agent_errors():
    assert cli.main(["bogus", "setup"]) == 2


def test_missing_subcommand_errors():
    assert cli.main(["claude"]) == 2


def test_box_with_no_args_errors():
    assert cli.main([]) == 2


def test_subcommand_surface_is_exactly_setup_and_delete():
    assert set(cli.SUBCOMMANDS) == {"setup", "delete"}


# --- agent-shim passthrough (prog = the agent command) -----------------------

def test_shim_passthrough_routes_to_run(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        cli.run, "run",
        lambda agent, mounts, agent_args: seen.update(
            agent=agent, mounts=mounts, args=agent_args) or 0,
    )
    assert cli.dispatch(["-p", "hi"], prog="claude") == 0
    assert seen == {"agent": CLAUDE, "mounts": [], "args": ["-p", "hi"]}


def test_shim_passthrough_forwards_mounts_and_propagates_rc(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        cli.run, "run",
        lambda agent, mounts, agent_args: seen.update(
            mounts=mounts, args=agent_args) or 7,
    )
    rc = cli.dispatch(["--mount", "/data:ro", "--", "-p", "hi"], prog="claude")
    assert rc == 7  # the run path's exit code is propagated
    assert seen == {"mounts": [Mount("/data", True)], "args": ["-p", "hi"]}


# --- run-path passthrough parse ----------------------------------------------

def test_passthrough_forwards_agent_args():
    mounts, agent_args = cli.parse_run_args(["-p", "hi"])
    assert mounts == []
    assert agent_args == ["-p", "hi"]


def test_no_args_is_empty_passthrough():
    mounts, agent_args = cli.parse_run_args([])
    assert mounts == []
    assert agent_args == []


# --- leading-block mount parse ------------------------------------------------

def test_mount_then_double_dash_terminator():
    mounts, agent_args = cli.parse_run_args(["--mount", "/x", "--", "--foo"])
    assert mounts == [Mount("/x", False)]
    assert agent_args == ["--foo"]  # the '--' itself is consumed, not forwarded


def test_mount_ro_suffix():
    mounts, agent_args = cli.parse_run_args(["--mount", "/data:ro", "claude-sub"])
    assert mounts == [Mount("/data", True)]
    assert agent_args == ["claude-sub"]


def test_multiple_mounts_then_first_non_wrapper_ends_block():
    mounts, agent_args = cli.parse_run_args(
        ["--mount", "/a", "--mount", "/b:ro", "-p", "hi"]
    )
    assert mounts == [Mount("/a", False), Mount("/b", True)]
    assert agent_args == ["-p", "hi"]


def test_mount_after_block_passes_through_verbatim():
    # First non-wrapper token ends the block; a later --mount is forwarded as-is.
    mounts, agent_args = cli.parse_run_args(["-p", "--mount", "/x"])
    assert mounts == []
    assert agent_args == ["-p", "--mount", "/x"]


def test_double_dash_with_no_mounts():
    mounts, agent_args = cli.parse_run_args(["--", "--mount", "/x"])
    assert mounts == []
    assert agent_args == ["--mount", "/x"]  # everything after '--' is verbatim


def test_mount_missing_operand_raises():
    with pytest.raises(cli.CliError):
        cli.parse_run_args(["--mount"])


def test_main_reports_cli_error(capsys):
    rc = cli.main(["claude", "--mount"])
    # `claude` is the agent shim only when prog is the command; via `box claude …`
    # `--mount` is an unknown subcommand, so this errors cleanly.
    assert rc == 2
    assert "box:" in capsys.readouterr().err
