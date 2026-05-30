"""Tests for the MCP/IDE bridge plumbing (pure logic + the in-sandbox bootstrap).

  * ``--mcp-config`` staging: a real file is copied and its operand rewritten to
    the in-sandbox staged path with a single read-only bind; inline JSON and
    missing paths pass through; both the spaced and ``=`` operand forms work,
  * lockfile reconciliation: pid rewrite + ``workspaceFolders`` trailing-slash
    normalization, and the ``changed`` flag,
  * ``entry_argv``: plain ``claude`` without an SSE port, the bootstrap wrapper
    with one,
  * the bootstrap script itself, run as a subprocess, produces exactly what
    ``apply_lockfile_patch`` describes and execs the target command.
"""

import json
import os
import signal
import subprocess
import sys

import pytest

from claude_sandbox import mcp
from claude_sandbox.sandbox import Bind


# --- --mcp-config staging -----------------------------------------------------

def test_stage_rewrites_file_and_binds(tmp_path):
    cfg = tmp_path / "servers.json"
    cfg.write_text('{"mcpServers": {}}')
    stage = tmp_path / "stage"
    stage.mkdir()

    args, binds = mcp.stage_mcp_configs(
        ["--mcp-config", str(cfg), "--print"], str(stage)
    )

    assert args == ("--mcp-config", f"{mcp.MCP_STAGE_DIR}/servers.json", "--print")
    assert binds == (Bind(str(stage), mcp.MCP_STAGE_DIR, mode="ro"),)
    # The file was actually copied into the staging dir, byte-for-byte.
    assert (stage / "servers.json").read_text() == '{"mcpServers": {}}'


def test_stage_equals_form(tmp_path):
    cfg = tmp_path / "s.json"
    cfg.write_text("{}")
    stage = tmp_path / "stage"
    stage.mkdir()

    args, binds = mcp.stage_mcp_configs([f"--mcp-config={cfg}"], str(stage))

    assert args == (f"--mcp-config={mcp.MCP_STAGE_DIR}/s.json",)
    assert len(binds) == 1


def test_stage_passes_through_inline_json_and_missing(tmp_path):
    stage = tmp_path / "stage"
    stage.mkdir()
    inline = '{"mcpServers": {"x": {}}}'
    missing = str(tmp_path / "nope.json")

    args, binds = mcp.stage_mcp_configs(
        ["--mcp-config", inline, "--mcp-config", missing], str(stage)
    )

    # Nothing staged -> args unchanged, no bind, staging dir untouched.
    assert args == ("--mcp-config", inline, "--mcp-config", missing)
    assert binds == ()
    assert list(stage.iterdir()) == []


def test_stage_no_mcp_args_is_identity(tmp_path):
    stage = tmp_path / "stage"
    stage.mkdir()
    args, binds = mcp.stage_mcp_configs(["--print", "hello"], str(stage))
    assert args == ("--print", "hello")
    assert binds == ()


# --- lockfile reconciliation --------------------------------------------------

def test_lockfile_path():
    assert (
        mcp.lockfile_path("/home/u", 54321) == "/home/u/.claude/ide/54321.lock"
    )


def test_normalize_workspace_folders():
    assert mcp.normalize_workspace_folders(["/a/b/", "/c", "/"]) == [
        "/a/b",
        "/c",
        "/",
    ]


def test_apply_lockfile_patch_changes_pid_and_folders():
    data = {"pid": 999999, "workspaceFolders": ["/proj/"], "transport": "ws"}
    patched, changed = mcp.apply_lockfile_patch(data, 7)
    assert changed is True
    assert patched["pid"] == 7
    assert patched["workspaceFolders"] == ["/proj"]
    assert patched["transport"] == "ws"  # untouched fields preserved
    # The input is not mutated in place.
    assert data["pid"] == 999999


def test_apply_lockfile_patch_noop_when_already_correct():
    data = {"pid": 7, "workspaceFolders": ["/proj"]}
    patched, changed = mcp.apply_lockfile_patch(data, 7)
    assert changed is False
    assert patched == data


# --- entry_argv ---------------------------------------------------------------

def test_entry_argv_without_sse_is_plain_claude():
    argv = mcp.entry_argv("/h/.local/bin/claude", ["-p", "hi"], home="/h", sse_port=None)
    assert argv == ("/h/.local/bin/claude", "-p", "hi")


def test_entry_argv_with_sse_wraps_in_bootstrap():
    argv = mcp.entry_argv(
        "/h/.local/bin/claude", ["-p", "hi"], home="/h", sse_port=4321, python="/usr/bin/python3"
    )
    assert argv[0] == "/usr/bin/python3"
    assert argv[1] == "-c"
    assert argv[2] == mcp._BOOTSTRAP
    assert argv[3] == "/h/.claude/ide/4321.lock"
    assert argv[4] == "/h/.local/bin/claude"
    assert argv[5:] == ("-p", "hi")


# --- the bootstrap script, end to end -----------------------------------------

def test_bootstrap_patches_lockfile_and_execs(tmp_path):
    """Run the real bootstrap as a subprocess: it must spawn a live sentinel,
    rewrite the lockfile to that pid + normalized folders (matching
    apply_lockfile_patch), and exec the target command."""
    ide = tmp_path / ".claude" / "ide"
    ide.mkdir(parents=True)
    lock = ide / "4321.lock"
    original = {"pid": 11111, "workspaceFolders": [str(tmp_path) + "/"], "transport": "ws"}
    lock.write_text(json.dumps(original))

    # A stand-in "claude": records that it was exec'd (same process as the
    # bootstrap, args forwarded) and exits, leaving the sentinel orphaned.
    marker = tmp_path / "exec_marker"
    fake_claude = tmp_path / "fake-claude"
    fake_claude.write_text(
        "#!/bin/sh\n" f'printf "%s" "$*" > "{marker}"\n' "exit 0\n"
    )
    fake_claude.chmod(0o755)

    rc = subprocess.run(
        [sys.executable, "-c", mcp._BOOTSTRAP, str(lock), str(fake_claude), "--print", "x"]
    ).returncode
    assert rc == 0
    assert marker.read_text() == "--print x"  # claude was exec'd with its args

    patched = json.loads(lock.read_text())
    sentinel_pid = patched["pid"]
    try:
        # The pid was rewritten to the sentinel and the folder slash stripped --
        # exactly what apply_lockfile_patch describes for that pid.
        assert sentinel_pid != original["pid"]
        expected, _ = mcp.apply_lockfile_patch(original, sentinel_pid)
        assert patched == expected
        # The sentinel really runs (orphaned by the exec, alive here since there
        # is no sandbox to tear it down) and is owned by this uid.
        os.kill(sentinel_pid, 0)
        with open(f"/proc/{sentinel_pid}/status") as fh:
            uid_line = next(l for l in fh if l.startswith("Uid:"))
        assert int(uid_line.split()[1]) == os.getuid()
    finally:
        # Outside a sandbox the sentinel has no --die-with-parent; reap it.
        try:
            os.kill(sentinel_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
