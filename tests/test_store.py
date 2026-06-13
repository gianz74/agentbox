"""Store-build boundary behavior.

A failed native installer must surface as a domain ``StoreError`` (which
``cli.main`` turns into a clean non-zero exit) rather than leaking the raw
``subprocess.CalledProcessError`` as an uncaught traceback.
"""

import subprocess

import pytest

from agentbox import store
from agentbox.agents import AGENTS

CLAUDE = AGENTS["claude"]


def test_install_native_wraps_installer_failure(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise subprocess.CalledProcessError(3, ["bash", "-c", "..."])

    monkeypatch.setattr(store.subprocess, "run", boom)
    with pytest.raises(store.StoreError, match="installer failed"):
        store._install_native(CLAUDE, tmp_path, None)
