"""Tests for the setup-side host preflight and the ``claude`` shim resolution.

Covers the pure pieces:

  * how a bare ``claude`` resolves against an injected ``PATH`` and on-disk
    fixtures -- the wrapper is found (silent), a non-wrapper shadows it (printed
    fix), nothing is on PATH (printed fix), and a leftover shim already in the
    ``~/bin`` slot is flagged,
  * the store-shadow refusal a config triggers (the guard setup runs over the
    whole config before building),
  * the host-readiness messaging -- missing ``bwrap``/``pasta`` and restricted
    user namespaces each instruct and signal a non-ready host, a ready host stays
    silent.
"""

from __future__ import annotations

import os

import pytest

from claude_sandbox import lifecycle
from claude_sandbox.config import parse_config
from claude_sandbox.mounts import MountError, guard_claude_shadow


# --- fixtures: a fake claude-sandbox entry and shim layout ----------------------


def _exe(path, target=None):
    """Create an executable at *path* (a symlink to *target* if given), making
    parents as needed; return its string path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if target is not None:
        path.symlink_to(target)
    else:
        path.write_text("#!/bin/sh\n")
        path.chmod(0o755)
    return str(path)


# --- shim resolution: the `claude`-resolves-to-wrapper PATH check -------------


def test_wrapper_on_path_is_recognized_and_silent(tmp_path):
    entry = _exe(tmp_path / "venv" / "bin" / "claude-sandbox")
    shim = tmp_path / "bin" / "claude"
    _exe(shim, target=entry)

    status = lifecycle.resolve_shim(
        str(shim.parent), home=str(tmp_path), wrapper_entry=entry
    )
    assert status.resolved == str(shim)
    assert status.is_wrapper is True
    # A correctly shimmed host produces no guidance.
    assert lifecycle.shim_guidance(status) == []


def test_wrapper_recognized_without_known_entry(tmp_path):
    # Even with no claude-sandbox on PATH to compare against, a `claude` that
    # resolves to a file named claude-sandbox is recognized as the wrapper.
    import shutil

    entry = _exe(tmp_path / "pkg" / "claude-sandbox")
    shim = tmp_path / "bin" / "claude"
    _exe(shim, target=entry)

    def which(cmd, mode=os.X_OK, path=None):
        # The default wrapper-entry lookup (no explicit path) finds nothing; the
        # shim lookup against the injected PATH resolves normally.
        return None if path is None else shutil.which(cmd, mode=mode, path=path)

    status = lifecycle.resolve_shim(
        str(shim.parent), home=str(tmp_path), wrapper_entry=None, which=which
    )
    assert status.wrapper_entry is None
    assert status.is_wrapper is True


def test_non_wrapper_claude_shadows_wrapper_and_gets_fix(tmp_path):
    entry = _exe(tmp_path / "venv" / "bin" / "claude-sandbox")
    # A real, non-wrapper `claude` sits earlier on PATH (outside ~/bin).
    other = _exe(tmp_path / "usr" / "bin" / "claude")

    status = lifecycle.resolve_shim(
        str(tmp_path / "usr" / "bin"), home=str(tmp_path), wrapper_entry=entry
    )
    assert status.resolved == other
    assert status.is_wrapper is False

    lines = lifecycle.shim_guidance(status)
    text = "\n".join(lines)
    assert other in text  # names what claude currently is
    assert f"ln -sf {entry} {status.slot_path}" in text  # the repoint command
    assert "not create the shim" in text  # never-mutate promise


def test_no_claude_on_path_gets_fix(tmp_path):
    entry = _exe(tmp_path / "venv" / "bin" / "claude-sandbox")
    empty = tmp_path / "empty"
    empty.mkdir()

    status = lifecycle.resolve_shim(str(empty), home=str(tmp_path), wrapper_entry=entry)
    assert status.resolved is None
    assert status.is_wrapper is False

    text = "\n".join(lifecycle.shim_guidance(status))
    assert "not on your PATH" in text
    assert f"ln -sf {entry} {status.slot_path}" in text


def test_leftover_shim_in_slot_is_flagged(tmp_path):
    entry = _exe(tmp_path / "venv" / "bin" / "claude-sandbox")
    # A leftover non-wrapper claude already occupies ~/bin/claude, but the one
    # that wins on PATH is elsewhere.
    _exe(tmp_path / "bin" / "claude")
    winner = _exe(tmp_path / "usr" / "bin" / "claude")

    status = lifecycle.resolve_shim(
        str(tmp_path / "usr" / "bin"), home=str(tmp_path), wrapper_entry=entry
    )
    assert status.resolved == winner
    assert status.slot_taken is True

    text = "\n".join(lifecycle.shim_guidance(status))
    assert status.slot_path in text
    assert "already exists" in text


def test_legacy_shim_occupies_the_slot_directly(tmp_path):
    # The winning `claude` *is* the ~/bin/claude slot, and it is not the wrapper
    # (the classic leftover-legacy-shim case): repoint that slot.
    entry = _exe(tmp_path / "venv" / "bin" / "claude-sandbox")
    slot = _exe(tmp_path / "bin" / "claude")

    status = lifecycle.resolve_shim(
        str(tmp_path / "bin"), home=str(tmp_path), wrapper_entry=entry
    )
    assert status.resolved == slot == status.slot_path
    assert status.is_wrapper is False

    text = "\n".join(lifecycle.shim_guidance(status))
    assert f"ln -sf {entry} {status.slot_path}" in text


def test_report_shim_prints_and_signals(tmp_path, capsys):
    entry = _exe(tmp_path / "venv" / "bin" / "claude-sandbox")
    shim = _exe(tmp_path / "bin" / "claude", target=entry)

    ok = lifecycle.resolve_shim(str(tmp_path / "bin"), home=str(tmp_path), wrapper_entry=entry)
    assert lifecycle.report_shim(ok) is False
    assert capsys.readouterr().out == ""

    other = _exe(tmp_path / "usr" / "bin" / "claude")
    bad = lifecycle.resolve_shim(
        str(tmp_path / "usr" / "bin"), home=str(tmp_path), wrapper_entry=entry
    )
    assert lifecycle.report_shim(bad) is True
    assert other in capsys.readouterr().out


# --- claude-shadow refusal (the guard setup runs over the config) ------------


@pytest.mark.parametrize(
    "path", ["~/.local/share/claude", "~/.local/bin", "~/.local"]
)
def test_config_shadowing_store_refused(path):
    cfg = parse_config({"mounts": [{"path": path}]})
    with pytest.raises(MountError, match="shadow"):
        guard_claude_shadow(cfg, home=os.path.expanduser("~"))


def test_config_beside_store_allowed():
    cfg = parse_config({"mounts": [{"path": "~/.local/share/other"}]})
    guard_claude_shadow(cfg, home=os.path.expanduser("~"))  # must not raise


# --- host preflight messaging ------------------------------------------------


def _which(present):
    """A ``shutil.which`` stand-in: resolves only the binaries named in *present*."""

    def which(cmd, mode=os.X_OK, path=None):
        return f"/usr/bin/{cmd}" if os.path.basename(cmd) in present else None

    return which


def test_good_host_is_silent(capsys):
    checks = lifecycle.preflight(
        which=_which({"bwrap", "pasta"}), userns_probe=lambda: True
    )
    assert all(c.ok for c in checks)
    assert lifecycle.report_preflight(checks) is True
    assert capsys.readouterr().out == ""


def test_missing_pasta_is_instructed(capsys):
    checks = lifecycle.preflight(
        which=_which({"bwrap"}), userns_probe=lambda: True
    )
    assert lifecycle.report_preflight(checks) is False
    out = capsys.readouterr().out
    assert "pasta" in out
    assert "passt" in out  # the apt package to install
    assert "bwrap" not in out  # bwrap is present, so it is not mentioned


def test_missing_bwrap_is_instructed_and_skips_userns(capsys):
    checks = lifecycle.preflight(
        which=_which({"pasta"}),
        userns_probe=lambda: pytest.fail("userns must not be probed without bwrap"),
    )
    # The userns check is skipped when bwrap is absent.
    assert [c.name for c in checks] == ["bwrap", "pasta"]
    assert lifecycle.report_preflight(checks) is False
    out = capsys.readouterr().out
    assert "bubblewrap" in out  # the apt package to install


def test_restricted_userns_is_instructed(capsys):
    checks = lifecycle.preflight(
        which=_which({"bwrap", "pasta"}), userns_probe=lambda: False
    )
    assert lifecycle.report_preflight(checks) is False
    out = capsys.readouterr().out
    assert "user namespaces" in out
    assert "unprivileged_userns_clone" in out
    assert "AppArmor" in out


def test_probe_userns_handles_missing_binary():
    # When bwrap cannot be exec'd at all, the probe reports "not working" rather
    # than raising.
    def boom(*args, **kwargs):
        raise FileNotFoundError("no bwrap")

    assert lifecycle.probe_userns(run=boom) is False


def test_probe_userns_reads_returncode():
    class _Proc:
        def __init__(self, rc):
            self.returncode = rc

    assert lifecycle.probe_userns(run=lambda *a, **k: _Proc(0)) is True
    assert lifecycle.probe_userns(run=lambda *a, **k: _Proc(1)) is False
