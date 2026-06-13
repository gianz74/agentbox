"""Run-path environment assembly (`build_env`): the agent's env surface layered
onto the universal baseline, scope precedence, and the identity/launcher drop.

(The launch itself is pasta-fronted and covered by the integration drivers; this
locks in the pure env logic, which is parameterized by the agent.)
"""

import os

from agentbox.agents import AGENTS
from agentbox.config import parse_config
from agentbox.run import build_env, ensure_default_mount_sources

CLAUDE = AGENTS["claude"]
COPILOT = AGENTS["copilot"]


def _cfg(env=None, contexts=None):
    data = {}
    if env is not None:
        data["env"] = env
    if contexts is not None:
        data["contexts"] = contexts
    return parse_config(data)


def test_universal_baseline_is_forwarded():
    host = {"TERM": "xterm", "LC_ALL": "C", "RANDOM_HOST_VAR": "x"}
    env = build_env(CLAUDE, _cfg(), None, host)
    assert env["TERM"] == "xterm"
    assert env["LC_ALL"] == "C"
    assert "RANDOM_HOST_VAR" not in env  # only the baseline + agent surface carries


def test_agent_env_surface_is_forwarded():
    # claude reads ANTHROPIC_*/CLAUDE_* (its env_prefixes); a stray var does not.
    host = {"ANTHROPIC_API_KEY": "sk", "CLAUDE_FOO": "1", "OPENAI_KEY": "no"}
    env = build_env(CLAUDE, _cfg(), None, host)
    assert env["ANTHROPIC_API_KEY"] == "sk"
    assert env["CLAUDE_FOO"] == "1"
    assert "OPENAI_KEY" not in env


def test_identity_and_launcher_keys_are_dropped():
    # HOME/USER/PATH on the host are never carried in -- the sandbox sets its own.
    host = {"HOME": "/wrong", "USER": "wrong", "PATH": "/wrong/bin", "TERM": "xterm"}
    env = build_env(CLAUDE, _cfg(), None, host)
    assert "HOME" not in env and "USER" not in env and "PATH" not in env
    assert env["TERM"] == "xterm"


def test_scope_precedence_context_over_global_over_baseline():
    host = {"H_FWD": "from-host"}
    cfg = _cfg(
        env={"SHARED": "global", "ONLY_GLOBAL": "g", "forward": ["H_FWD"]},
        contexts=[{"name": "c", "when": ["~/p"], "env": {"SHARED": "ctx"}}],
    )
    env = build_env(CLAUDE, cfg, cfg.contexts[0], host)
    assert env["SHARED"] == "ctx"  # context literal wins over global
    assert env["ONLY_GLOBAL"] == "g"
    assert env["H_FWD"] == "from-host"  # a forwarded host var is pulled in


def test_forward_skips_unset_host_var():
    cfg = _cfg(env={"forward": ["MISSING"]})
    env = build_env(CLAUDE, cfg, None, {"TERM": "xterm"})
    assert "MISSING" not in env


def test_literal_overrides_forwarded_value():
    host = {"X": "host"}
    cfg = _cfg(env={"X": "literal", "forward": ["X"]})
    env = build_env(CLAUDE, cfg, None, host)
    assert env["X"] == "literal"  # the literal wins over the forwarded host value


# --- agent runtime_env (fixed agent-set literals) -----------------------------


def test_agent_runtime_env_is_injected():
    # copilot freezes self-update via a fixed COPILOT_AUTO_UPDATE=false literal.
    env = build_env(COPILOT, _cfg(), None, {"TERM": "xterm"})
    assert env["COPILOT_AUTO_UPDATE"] == "false"


def test_no_runtime_env_for_an_agent_without_one():
    env = build_env(CLAUDE, _cfg(), None, {"TERM": "xterm"})
    assert "COPILOT_AUTO_UPDATE" not in env


def test_config_env_can_still_override_runtime_env():
    # runtime_env is part of the agent baseline, so an explicit [env] literal wins.
    cfg = _cfg(env={"COPILOT_AUTO_UPDATE": "true"})
    env = build_env(COPILOT, cfg, None, {})
    assert env["COPILOT_AUTO_UPDATE"] == "true"


# --- ensure_default_mount_sources ---------------------------------------------


def test_ensure_default_mount_sources_creates_directory_sources(tmp_path):
    home = str(tmp_path)
    ensure_default_mount_sources(COPILOT, home)
    assert os.path.isdir(os.path.join(home, ".copilot"))


def test_ensure_default_mount_sources_seeds_json_file_mounts(tmp_path):
    # claude has a ~/.claude dir and a ~/.claude.json file: the dir is created and
    # the json file is seeded with {} (a 0-byte file would read as corrupt).
    home = str(tmp_path)
    ensure_default_mount_sources(CLAUDE, home)
    assert os.path.isdir(os.path.join(home, ".claude"))
    cfg = os.path.join(home, ".claude.json")
    assert os.path.isfile(cfg)
    assert open(cfg).read().strip() == "{}"


def test_ensure_default_mount_sources_never_clobbers_existing(tmp_path):
    home = str(tmp_path)
    cfg = os.path.join(home, ".claude.json")
    open(cfg, "w").write('{"keep": 1}')
    ensure_default_mount_sources(CLAUDE, home)
    assert open(cfg).read() == '{"keep": 1}'  # existing config left untouched
