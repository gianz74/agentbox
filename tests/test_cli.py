"""Tests for cli dispatch and the run-path leading-block parse.

  * ``setup`` / ``delete`` route to their subcommand handlers,
  * the subcommand surface is exactly ``{setup, delete}``,
  * ``-p hi`` routes to the run path with ``['-p', 'hi']`` forwarded,
  * ``--mount /x -- --foo`` parses ``/x`` as a mount and ``--foo`` as passthrough.
"""

import pytest

from claude_sandbox import cli
from claude_sandbox.cli import Mount


# --- subcommand routing -------------------------------------------------------

def test_setup_routes_to_stub(capsys):
    rc = cli.dispatch(["setup"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "setup: not implemented" in out


def test_delete_routes_to_stub(capsys):
    rc = cli.dispatch(["delete"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "delete: not implemented" in out


def test_subcommand_surface_is_exactly_setup_and_delete():
    assert set(cli.SUBCOMMANDS) == {"setup", "delete"}


def test_unknown_first_token_routes_to_passthrough(capsys):
    # A bareword that is not a known subcommand falls through to the run path.
    rc = cli.dispatch(["resume"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "passthrough: not implemented" in out
    assert "['resume']" in out


# --- run-path passthrough -----------------------------------------------------

def test_passthrough_forwards_claude_args():
    mounts, claude_args = cli.parse_run_args(["-p", "hi"])
    assert mounts == []
    assert claude_args == ["-p", "hi"]


def test_passthrough_routes_through_dispatch(capsys):
    rc = cli.dispatch(["-p", "hi"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "passthrough: not implemented" in out
    assert "['-p', 'hi']" in out


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
