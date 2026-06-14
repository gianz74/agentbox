"""Tests for the claude agent's MCP/IDE bridge internals (pure logic + the
in-sandbox bootstrap). Relocated from ``test_mcp`` when the plumbing moved into
:mod:`agentbox.agents.claude`.

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

from agentbox.agents import claude
from agentbox.sandbox import Bind


# --- sse_port_from_env --------------------------------------------------------

def test_sse_port_from_env_reads_value():
    assert claude.sse_port_from_env({"CLAUDE_CODE_SSE_PORT": "54321"}) == 54321


def test_sse_port_from_env_absent_or_invalid():
    assert claude.sse_port_from_env({}) is None
    assert claude.sse_port_from_env({"CLAUDE_CODE_SSE_PORT": ""}) is None
    assert claude.sse_port_from_env({"CLAUDE_CODE_SSE_PORT": "nope"}) is None
    assert claude.sse_port_from_env({"CLAUDE_CODE_SSE_PORT": "0"}) is None
    assert claude.sse_port_from_env({"CLAUDE_CODE_SSE_PORT": "70000"}) is None


# --- --mcp-config staging -----------------------------------------------------

def test_stage_rewrites_file_and_binds(tmp_path):
    cfg = tmp_path / "servers.json"
    cfg.write_text('{"mcpServers": {}}')
    stage = tmp_path / "stage"
    stage.mkdir()

    args, binds = claude.stage_mcp_configs(
        ["--mcp-config", str(cfg), "--print"], str(stage)
    )

    assert args == ("--mcp-config", f"{claude.MCP_STAGE_DIR}/servers.json", "--print")
    assert binds == (Bind(str(stage), claude.MCP_STAGE_DIR, mode="ro"),)
    # The file was actually copied into the staging dir, byte-for-byte.
    assert (stage / "servers.json").read_text() == '{"mcpServers": {}}'


def test_stage_equals_form(tmp_path):
    cfg = tmp_path / "s.json"
    cfg.write_text("{}")
    stage = tmp_path / "stage"
    stage.mkdir()

    args, binds = claude.stage_mcp_configs([f"--mcp-config={cfg}"], str(stage))

    assert args == (f"--mcp-config={claude.MCP_STAGE_DIR}/s.json",)
    assert len(binds) == 1


def test_stage_passes_through_inline_json_and_missing(tmp_path):
    stage = tmp_path / "stage"
    stage.mkdir()
    inline = '{"mcpServers": {"x": {}}}'
    missing = str(tmp_path / "nope.json")

    args, binds = claude.stage_mcp_configs(
        ["--mcp-config", inline, "--mcp-config", missing], str(stage)
    )

    # Nothing staged -> args unchanged, no bind, staging dir untouched.
    assert args == ("--mcp-config", inline, "--mcp-config", missing)
    assert binds == ()
    assert list(stage.iterdir()) == []


def test_stage_no_mcp_args_is_identity(tmp_path):
    stage = tmp_path / "stage"
    stage.mkdir()
    args, binds = claude.stage_mcp_configs(["--print", "hello"], str(stage))
    assert args == ("--print", "hello")
    assert binds == ()


# --- --mcp-config loopback-port discovery ------------------------------------

def _inline(servers):
    return json.dumps({"mcpServers": servers})


def test_loopback_ports_from_inline_json():
    cfg = _inline({"emacs-tools": {"type": "http", "url": "http://localhost:43055/mcp/x"}})
    assert claude.loopback_mcp_ports(["--mcp-config", cfg, "--print"]) == [43055]


def test_loopback_ports_from_file(tmp_path):
    cfg = tmp_path / "servers.json"
    cfg.write_text(_inline({"t": {"url": "http://127.0.0.1:7000/mcp"}}))
    assert claude.loopback_mcp_ports([f"--mcp-config={cfg}"]) == [7000]


def test_loopback_ports_multiple_servers_deduped_in_order():
    cfg = _inline({
        "a": {"url": "http://localhost:5000/"},
        "b": {"url": "http://127.0.0.1:6000/"},
        "c": {"url": "http://localhost:5000/again"},  # duplicate port -> dropped
    })
    assert claude.loopback_mcp_ports(["--mcp-config", cfg]) == [5000, 6000]


def test_loopback_ports_accepts_all_loopback_spellings():
    cfg = _inline({
        "a": {"url": "http://localhost:5000/"},
        "b": {"url": "http://127.0.0.1:5001/"},
        "c": {"url": "http://[::1]:5002/"},
    })
    assert claude.loopback_mcp_ports(["--mcp-config", cfg]) == [5000, 5001, 5002]


def test_loopback_ports_scheme_default_when_absent():
    cfg = _inline({
        "h": {"url": "http://localhost/mcp"},     # -> 80
        "s": {"url": "https://127.0.0.1/mcp"},    # -> 443
    })
    assert claude.loopback_mcp_ports(["--mcp-config", cfg]) == [80, 443]


def test_loopback_ports_skips_non_loopback():
    cfg = _inline({
        "lan": {"url": "http://10.255.255.1:8888/mcp"},
        "pub": {"url": "https://example.com:9999/mcp"},
        "ok": {"url": "http://localhost:43055/mcp"},
    })
    # Only the loopback URL is forwarded; the LAN/public hosts must not leak.
    assert claude.loopback_mcp_ports(["--mcp-config", cfg]) == [43055]


def test_loopback_ports_skips_malformed_and_missing():
    missing = "/no/such/file.json"
    not_json = "{not json"
    no_url = _inline({"x": {"type": "stdio", "command": "foo"}})
    bad_shape = json.dumps({"mcpServers": "nope"})
    assert claude.loopback_mcp_ports([
        "--mcp-config", missing,
        "--mcp-config", not_json,
        "--mcp-config", no_url,
        "--mcp-config", bad_shape,
    ]) == []


def test_loopback_ports_none_without_mcp_config():
    assert claude.loopback_mcp_ports(["--print", "hi"]) == []


# --- base_url_port ------------------------------------------------------------

def test_base_url_port_reads_loopback_spellings():
    assert claude.base_url_port({"ANTHROPIC_BASE_URL": "http://127.0.0.1:4000"}) == 4000
    assert claude.base_url_port({"ANTHROPIC_BASE_URL": "http://localhost:4000/v1"}) == 4000
    assert claude.base_url_port({"ANTHROPIC_BASE_URL": "http://[::1]:4000"}) == 4000


def test_base_url_port_scheme_default_when_absent():
    assert claude.base_url_port({"ANTHROPIC_BASE_URL": "http://localhost/v1"}) == 80
    assert claude.base_url_port({"ANTHROPIC_BASE_URL": "https://127.0.0.1/v1"}) == 443


def test_base_url_port_none_for_remote_or_unset():
    # The default hosted endpoint and any non-loopback host are never forwarded.
    assert claude.base_url_port({"ANTHROPIC_BASE_URL": "https://api.anthropic.com"}) is None
    assert claude.base_url_port({"ANTHROPIC_BASE_URL": "http://192.168.1.10:4000"}) is None
    assert claude.base_url_port({}) is None
    assert claude.base_url_port({"ANTHROPIC_BASE_URL": "not a url"}) is None


# --- lockfile reconciliation --------------------------------------------------

def test_lockfile_path():
    assert (
        claude.lockfile_path("/home/u", 54321) == "/home/u/.claude/ide/54321.lock"
    )


def test_normalize_workspace_folders():
    assert claude.normalize_workspace_folders(["/a/b/", "/c", "/"]) == [
        "/a/b",
        "/c",
        "/",
    ]


def test_apply_lockfile_patch_changes_pid_and_folders():
    data = {"pid": 999999, "workspaceFolders": ["/proj/"], "transport": "ws"}
    patched, changed = claude.apply_lockfile_patch(data, 7)
    assert changed is True
    assert patched["pid"] == 7
    assert patched["workspaceFolders"] == ["/proj"]
    assert patched["transport"] == "ws"  # untouched fields preserved
    # The input is not mutated in place.
    assert data["pid"] == 999999


def test_apply_lockfile_patch_noop_when_already_correct():
    data = {"pid": 7, "workspaceFolders": ["/proj"]}
    patched, changed = claude.apply_lockfile_patch(data, 7)
    assert changed is False
    assert patched == data


# --- entry_argv ---------------------------------------------------------------

def test_entry_argv_without_sse_is_plain_claude():
    argv = claude.entry_argv("/h/.local/bin/claude", ["-p", "hi"], home="/h", sse_port=None)
    assert argv == ("/h/.local/bin/claude", "-p", "hi")


def test_entry_argv_with_sse_wraps_in_bootstrap():
    argv = claude.entry_argv(
        "/h/.local/bin/claude", ["-p", "hi"], home="/h", sse_port=4321, python="/usr/bin/python3"
    )
    assert argv[0] == "/usr/bin/python3"
    assert argv[1] == "-c"
    assert argv[2] == claude._BOOTSTRAP
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
        [sys.executable, "-c", claude._BOOTSTRAP, str(lock), str(fake_claude), "--print", "x"]
    ).returncode
    assert rc == 0
    assert marker.read_text() == "--print x"  # claude was exec'd with its args

    patched = json.loads(lock.read_text())
    sentinel_pid = patched["pid"]
    try:
        # The pid was rewritten to the sentinel and the folder slash stripped --
        # exactly what apply_lockfile_patch describes for that pid.
        assert sentinel_pid != original["pid"]
        expected, _ = claude.apply_lockfile_patch(original, sentinel_pid)
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
