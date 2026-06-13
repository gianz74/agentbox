"""Store-build boundary behavior.

A failed native installer must surface as a domain ``StoreError`` (which
``cli.main`` turns into a clean non-zero exit) rather than leaking the raw
``subprocess.CalledProcessError`` as an uncaught traceback.
"""

import subprocess

import pytest

from agentbox import store
from agentbox.agents import AGENTS
from agentbox.config import parse_config

CLAUDE = AGENTS["claude"]
COPILOT = AGENTS["copilot"]


def test_install_native_wraps_installer_failure(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise subprocess.CalledProcessError(3, ["bash", "-c", "..."])

    monkeypatch.setattr(store.subprocess, "run", boom)
    with pytest.raises(store.StoreError, match="installer failed"):
        store._install_native(CLAUDE, tmp_path, None)


def test_store_matches_requires_the_stamp_agent(tmp_path):
    # A present store whose binary name matches the queried agent but whose stamp
    # records a different agent (a mis-copied/mis-stamped store) is drift, not a
    # fast-path hit -- store_present's binary-name check alone wouldn't catch it.
    binp = tmp_path / ".local" / "bin" / "copilot"  # copilot is a lone binary
    binp.parent.mkdir(parents=True)
    binp.write_text("#!/bin/sh\n")
    binp.chmod(0o755)
    cfg = parse_config({})  # no version pin
    good = store.store_stamp(version="0.1", method="native", agent=COPILOT)

    store.write_stamp(tmp_path, good)
    assert store.store_matches(COPILOT, cfg, store=tmp_path) is True

    store.write_stamp(tmp_path, {**good, "agent": "claude"})  # wrong agent
    assert store.store_matches(COPILOT, cfg, store=tmp_path) is False

    missing = {k: v for k, v in good.items() if k != "agent"}  # field absent
    store.write_stamp(tmp_path, missing)
    assert store.store_matches(COPILOT, cfg, store=tmp_path) is False
