"""Tests for cli dispatch and the run-path leading-block parse.

  * ``setup`` / ``delete`` route to their ``lifecycle`` handlers (with the
    ``--from-host`` flag parsed for setup),
  * the subcommand surface is exactly ``{setup, delete}``,
  * ``-p hi`` routes to ``lifecycle.run`` with ``['-p', 'hi']`` forwarded,
  * ``--mount /x -- --foo`` parses ``/x`` as a mount and ``--foo`` as passthrough.

The dispatch tests stub ``lifecycle.setup/delete/run`` to recorders so routing is
asserted without booting a real sandbox.
"""

import pytest

from agentbox import cli
from agentbox.cli import Mount


# --- subcommand routing -------------------------------------------------------

def test_setup_routes_to_lifecycle(monkeypatch):
    calls = {}
    monkeypatch.setattr(cli, "load_user_config", lambda: "CFG")
    monkeypatch.setattr(
        cli.lifecycle, "setup",
        lambda config, *, from_host=False: calls.update(config=config, from_host=from_host) or 0,
    )
    assert cli.dispatch(["setup"]) == 0
    assert calls == {"config": "CFG", "from_host": False}


def test_setup_from_host_flag_is_parsed(monkeypatch):
    calls = {}
    monkeypatch.setattr(cli, "load_user_config", lambda: "CFG")
    monkeypatch.setattr(
        cli.lifecycle, "setup",
        lambda config, *, from_host=False: calls.update(from_host=from_host) or 0,
    )
    assert cli.dispatch(["setup", "--from-host"]) == 0
    assert calls == {"from_host": True}


def test_setup_rejects_unexpected_arg(monkeypatch):
    monkeypatch.setattr(cli, "load_user_config", lambda: "CFG")
    monkeypatch.setattr(cli.lifecycle, "setup", lambda *a, **k: 0)
    assert cli.main(["setup", "--bogus"]) == 2


def test_delete_routes_to_lifecycle(monkeypatch):
    called = {}
    monkeypatch.setattr(cli.lifecycle, "delete", lambda: called.update(hit=True) or 0)
    assert cli.dispatch(["delete"]) == 0
    assert called == {"hit": True}


def test_subcommand_surface_is_exactly_setup_and_delete():
    assert set(cli.SUBCOMMANDS) == {"setup", "delete"}


def test_unknown_first_token_routes_to_run(monkeypatch):
    # A bareword that is not a known subcommand falls through to the run path.
    seen = {}
    monkeypatch.setattr(
        cli.lifecycle, "run",
        lambda mounts, claude_args: seen.update(mounts=mounts, args=claude_args) or 0,
    )
    assert cli.dispatch(["resume"]) == 0
    assert seen == {"mounts": [], "args": ["resume"]}


# --- run-path passthrough -----------------------------------------------------

def test_passthrough_forwards_claude_args():
    mounts, claude_args = cli.parse_run_args(["-p", "hi"])
    assert mounts == []
    assert claude_args == ["-p", "hi"]


def test_passthrough_routes_to_run(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        cli.lifecycle, "run",
        lambda mounts, claude_args: seen.update(mounts=mounts, args=claude_args) or 0,
    )
    assert cli.dispatch(["-p", "hi"]) == 0
    assert seen == {"mounts": [], "args": ["-p", "hi"]}


def test_run_path_forwards_mounts_to_run(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        cli.lifecycle, "run",
        lambda mounts, claude_args: seen.update(mounts=mounts, args=claude_args) or 7,
    )
    rc = cli.dispatch(["--mount", "/data:ro", "--", "-p", "hi"])
    assert rc == 7  # the run path's exit code is propagated
    assert seen == {"mounts": [Mount("/data", True)], "args": ["-p", "hi"]}


def test_no_args_is_empty_passthrough():
    mounts, claude_args = cli.parse_run_args([])
    assert mounts == []
    assert claude_args == []


# --- leading-block mount parse ------------------------------------------------

def test_mount_then_double_dash_terminator():
    mounts, claude_args = cli.parse_run_args(["--mount", "/x", "--", "--foo"])
    assert mounts == [Mount("/x", False)]
    assert claude_args == ["--foo"]  # the '--' itself is consumed, not forwarded


def test_mount_ro_suffix():
    mounts, claude_args = cli.parse_run_args(["--mount", "/data:ro", "claude-sub"])
    assert mounts == [Mount("/data", True)]
    assert claude_args == ["claude-sub"]


def test_multiple_mounts_then_first_non_wrapper_ends_block():
    mounts, claude_args = cli.parse_run_args(
        ["--mount", "/a", "--mount", "/b:ro", "-p", "hi"]
    )
    assert mounts == [Mount("/a", False), Mount("/b", True)]
    assert claude_args == ["-p", "hi"]


def test_mount_after_block_passes_through_verbatim():
    # First non-wrapper token ends the block; a later --mount is forwarded as-is.
    mounts, claude_args = cli.parse_run_args(["-p", "--mount", "/x"])
    assert mounts == []
    assert claude_args == ["-p", "--mount", "/x"]


def test_double_dash_with_no_mounts():
    mounts, claude_args = cli.parse_run_args(["--", "--mount", "/x"])
    assert mounts == []
    assert claude_args == ["--mount", "/x"]  # everything after '--' is verbatim


def test_mount_missing_operand_raises():
    with pytest.raises(cli.CliError):
        cli.parse_run_args(["--mount"])


def test_main_reports_cli_error(capsys):
    rc = cli.main(["--mount"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "--mount" in err
