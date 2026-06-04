"""PATH handling: opt-in base resolution (`resolve_base_path`) and the
launcher-prefix prepend + dedup applied by `store_launch`."""

from claude_sandbox.config import parse_config
from claude_sandbox.lifecycle import (
    DEFAULT_PATH,
    LAUNCHER_DIR,
    _dedup_path,
    resolve_base_path,
    store_launch,
)

HOST = {"PATH": "/host/bin:/usr/bin:/bin"}


def _cfg(env=None, contexts=None):
    data = {}
    if env is not None:
        data["env"] = env
    if contexts is not None:
        data["contexts"] = contexts
    return parse_config(data)


# --- resolve_base_path: the opt-in base before the launcher prefix -----------


def test_unmentioned_falls_back_to_default():
    assert resolve_base_path(_cfg(), None, HOST) == DEFAULT_PATH


def test_literal_only_uses_config_value():
    cfg = _cfg(env={"PATH": "/opt/tools/bin"})
    assert resolve_base_path(cfg, None, HOST) == "/opt/tools/bin"


def test_forward_only_uses_host_path():
    cfg = _cfg(env={"forward": ["PATH"]})
    assert resolve_base_path(cfg, None, HOST) == HOST["PATH"]


def test_forward_with_unset_host_path_falls_back():
    cfg = _cfg(env={"forward": ["PATH"]})
    assert resolve_base_path(cfg, None, {}) == DEFAULT_PATH


def test_literal_and_forward_prepends_literal_to_host():
    cfg = _cfg(env={"PATH": "/opt/tools/bin", "forward": ["PATH"]})
    assert resolve_base_path(cfg, None, HOST) == "/opt/tools/bin:" + HOST["PATH"]


def test_context_fragments_precede_global():
    cfg = _cfg(
        env={"PATH": "/global/bin"},
        contexts=[{"name": "c", "when": ["~/p"], "env": {"PATH": "/ctx/bin"}}],
    )
    matched = cfg.contexts[0]
    assert resolve_base_path(cfg, matched, HOST) == "/ctx/bin:/global/bin"


# --- store_launch: launcher prefix prepended, whole thing deduped ------------


def test_store_launch_prepends_launcher_and_dedups(tmp_path):
    home = str(tmp_path / "home")
    launcher = tmp_path / "launcher"
    launcher.mkdir()
    # base repeats ~/.local/bin (the launcher prefix already adds it) and /usr/bin.
    base = f"{home}/.local/bin:/usr/bin:/host/bin:/usr/bin"
    sl = store_launch(home, launcher, store=tmp_path / "store", base_path=base)
    parts = sl.path.split(":")
    assert parts[0] == LAUNCHER_DIR
    assert parts[1] == f"{home}/.local/bin"
    assert len(parts) == len(set(parts))  # no duplicates
    assert "" not in parts  # no empties
    assert parts.count("/usr/bin") == 1


def test_dedup_keeps_first_occurrence_order():
    assert _dedup_path("a:b::a:c:b") == "a:b:c"
