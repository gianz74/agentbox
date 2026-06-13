"""Unit tests for agentbox.config."""

from __future__ import annotations

import os

import pytest

from agentbox.config import (
    Config,
    ConfigError,
    ensure_user_config,
    load_config,
    parse_config,
)

SAMPLE = """\
[agents.claude]
version = "2.1.150"

[[mounts]]
path = "~/.claude"
[[mounts]]
path = "~/.aws"
mode = "ro"

[[contexts]]
name = "api"
when = ["~/work/acme-api", "~/work/other"]
  [[contexts.mounts]]
  path = "~/.ssh"
  from = "~/.ssh-api"
  mode = "ro"
  [[contexts.mounts]]
  path    = "~/work"
  exclude = ["secrets", "secret"]

[[contexts]]
name = "catchall"
when = "~"
"""


def _write(tmp_path, text, name="config.toml"):
    p = tmp_path / name
    p.write_text(text)
    return p


def test_load_sample_config(tmp_path):
    cfg = load_config(_write(tmp_path, SAMPLE))
    assert isinstance(cfg, Config)

    # [agents.<name>]
    assert cfg.agents["claude"].version == "2.1.150"

    # global mounts, ~-expanded
    assert [m.path for m in cfg.mounts] == [
        os.path.expanduser("~/.claude"),
        os.path.expanduser("~/.aws"),
    ]
    assert cfg.mounts[0].mode == "rw"  # default
    assert cfg.mounts[1].mode == "ro"
    assert cfg.mounts[0].is_alias is False

    # contexts
    assert [c.name for c in cfg.contexts] == ["api", "catchall"]
    api = cfg.contexts[0]
    assert api.when == (
        os.path.expanduser("~/work/acme-api"),
        os.path.expanduser("~/work/other"),
    )
    # alias mount: path is sandbox-side, host_path is the `from` backing
    ssh = api.mounts[0]
    assert ssh.is_alias is True
    assert ssh.path == os.path.expanduser("~/.ssh")
    assert ssh.host_path == os.path.expanduser("~/.ssh-api")
    assert ssh.mode == "ro"
    # exclude preserved as relative sub-paths
    assert api.mounts[1].exclude == ("secrets", "secret")

    # `when` given as a bare string is coerced to a one-element list
    assert cfg.contexts[1].when == (os.path.expanduser("~"),)


def test_empty_config_uses_defaults(tmp_path):
    cfg = load_config(_write(tmp_path, ""))
    assert dict(cfg.agents) == {}
    assert cfg.mounts == ()
    assert cfg.contexts == ()
    assert dict(cfg.env) == {}
    assert cfg.forward == ()


def test_duplicate_context_name_rejected(tmp_path):
    text = """\
[[contexts]]
name = "dup"
when = ["~/a"]
[[contexts]]
name = "dup"
when = ["~/b"]
"""
    with pytest.raises(ConfigError, match="duplicate context name 'dup'"):
        load_config(_write(tmp_path, text))


def test_malformed_toml_rejected(tmp_path):
    with pytest.raises(ConfigError, match="malformed TOML"):
        load_config(_write(tmp_path, "this is = = not toml ["))


def test_missing_context_name_rejected(tmp_path):
    text = '[[contexts]]\nwhen = ["~/a"]\n'
    with pytest.raises(ConfigError, match="missing required 'name'"):
        load_config(_write(tmp_path, text))


def test_missing_when_rejected(tmp_path):
    text = '[[contexts]]\nname = "x"\n'
    with pytest.raises(ConfigError, match="missing required 'when'"):
        load_config(_write(tmp_path, text))


def test_reserved_default_name_rejected(tmp_path):
    text = '[[contexts]]\nname = "default"\nwhen = ["~/a"]\n'
    with pytest.raises(ConfigError, match="reserved"):
        load_config(_write(tmp_path, text))


def test_invalid_mount_mode_rejected(tmp_path):
    text = '[[mounts]]\npath = "~/x"\nmode = "rx"\n'
    with pytest.raises(ConfigError, match="invalid mode 'rx'"):
        load_config(_write(tmp_path, text))


def test_mount_missing_path_rejected(tmp_path):
    text = '[[mounts]]\nmode = "ro"\n'
    with pytest.raises(ConfigError, match="missing required 'path'"):
        load_config(_write(tmp_path, text))


def test_missing_file_rejected(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "does-not-exist.toml")


def test_parse_config_pure_dict():
    cfg = parse_config({"agents": {"claude": {"version": "2.1.150"}}})
    assert cfg.agents["claude"].version == "2.1.150"


# --- closed schema: only known tables / keys are accepted --------------------


def test_unknown_top_level_table_rejected(tmp_path):
    text = '[bogus]\nfoo = "bar"\n'
    with pytest.raises(ConfigError, match=r"unknown key\(s\) 'bogus'"):
        load_config(_write(tmp_path, text))


def test_agent_table_only_accepts_version(tmp_path):
    text = '[agents.claude]\nextra = "nope"\n'
    with pytest.raises(
        ConfigError, match=r"\[agents\.claude\]: unknown key\(s\) 'extra'"
    ):
        load_config(_write(tmp_path, text))


def test_unknown_agent_name_rejected(tmp_path):
    # [agents.<name>] is validated against the built-in registry.
    text = '[agents.bogus]\nversion = "1.0"\n'
    with pytest.raises(ConfigError, match=r"\[agents\.bogus\]: unknown agent 'bogus'"):
        load_config(_write(tmp_path, text))


def test_version_pin_rejected_for_agent_that_cannot_honor_it(tmp_path):
    # copilot's installer cannot select a version (version_args is None), so a pin
    # would never converge -> the run path would rebuild every launch. Reject it at
    # parse time rather than loop.
    text = '[agents.copilot]\nversion = "0.3.1"\n'
    with pytest.raises(
        ConfigError, match=r"\[agents\.copilot\]\.version: 'copilot' does not support"
    ):
        load_config(_write(tmp_path, text))


def test_version_pin_accepted_for_agent_that_supports_it(tmp_path):
    # claude's installer takes an argv version (version_args set), so a pin is fine.
    text = '[agents.claude]\nversion = "2.1.150"\n'
    cfg = load_config(_write(tmp_path, text))
    assert cfg.agents["claude"].version == "2.1.150"


# --- [vars] / ${NAME} expansion ----------------------------------------------


def test_vars_expansion_in_mount_from(tmp_path):
    text = """\
[vars]
WM = "~/x"

[[contexts]]
name = "v"
when = ["~/proj"]
  [[contexts.mounts]]
  path = "~/.gnupg"
  from = "${WM}/.gnupg"
"""
    cfg = load_config(_write(tmp_path, text))
    mount = cfg.contexts[0].mounts[0]
    # ${WM} substituted first, then ~ expanded -> /home/<user>/x/.gnupg
    assert mount.host_path == os.path.expanduser("~/x/.gnupg")
    assert mount.path == os.path.expanduser("~/.gnupg")


def test_undefined_var_rejected_naming_the_key(tmp_path):
    text = """\
[[mounts]]
path = "${NOPE}/data"
"""
    with pytest.raises(ConfigError, match=r"undefined variable \$\{NOPE\}"):
        load_config(_write(tmp_path, text))


def test_bare_dollar_name_left_literal(tmp_path):
    # No braces -> not a substitution target; the literal string survives.
    text = '[[mounts]]\npath = "/data/$HOME/x"\n'
    cfg = load_config(_write(tmp_path, text))
    assert cfg.mounts[0].path == "/data/$HOME/x"


def test_varless_config_parses_identically(tmp_path):
    # The pre-pass must be a no-op when there is no [vars] table / no ${...}.
    cfg = load_config(_write(tmp_path, SAMPLE))
    assert [m.path for m in cfg.mounts] == [
        os.path.expanduser("~/.claude"),
        os.path.expanduser("~/.aws"),
    ]
    assert cfg.contexts[0].mounts[0].host_path == os.path.expanduser("~/.ssh-api")


def test_vars_expansion_across_sections(tmp_path):
    # ${NAME} reaches every string: [agents.<name>], context `when`, and mounts.
    text = """\
[vars]
ROOT = "~/work"
VER = "2.1.150"

[agents.claude]
version = "${VER}"

[[contexts]]
name = "v"
when = ["${ROOT}/a"]
  [[contexts.mounts]]
  path = "${ROOT}/a"
"""
    cfg = load_config(_write(tmp_path, text))
    assert cfg.agents["claude"].version == "2.1.150"
    assert cfg.contexts[0].when == (os.path.expanduser("~/work/a"),)
    assert cfg.contexts[0].mounts[0].path == os.path.expanduser("~/work/a")


def test_vars_value_not_recursively_expanded(tmp_path):
    # A ${...} inside a [vars] value is inserted verbatim, never re-resolved.
    text = """\
[vars]
A = "${B}/x"

[[mounts]]
path = "/data/${A}"
"""
    cfg = load_config(_write(tmp_path, text))
    assert cfg.mounts[0].path == "/data/${B}/x"


def test_implicit_home_var_in_env_value(tmp_path):
    # ${HOME} is seeded implicitly so a home-relative [env] value works even
    # though env values are NOT ~-expanded.
    text = """\
[env]
GIT_CONFIG_GLOBAL = "${HOME}/.config/git/config"
"""
    cfg = load_config(_write(tmp_path, text))
    assert (
        cfg.env["GIT_CONFIG_GLOBAL"]
        == os.path.expanduser("~") + "/.config/git/config"
    )


def test_implicit_user_var(tmp_path):
    expected = os.environ.get("USER") or os.path.basename(os.path.expanduser("~"))
    text = '[[mounts]]\npath = "/srv/${USER}/data"\n'
    cfg = load_config(_write(tmp_path, text))
    assert cfg.mounts[0].path == f"/srv/{expected}/data"


def test_implicit_home_overridable_by_vars(tmp_path):
    # An explicit [vars] entry of the same name wins over the implicit seed.
    text = """\
[vars]
HOME = "/custom"

[[mounts]]
path = "${HOME}/x"
"""
    cfg = load_config(_write(tmp_path, text))
    assert cfg.mounts[0].path == "/custom/x"


def test_undefined_var_still_raises_with_implicit_seeds(tmp_path):
    # Seeding HOME/USER must not swallow a genuinely undefined ${NAME}.
    text = '[[mounts]]\npath = "${HOME}/${NOPE}"\n'
    with pytest.raises(ConfigError, match=r"undefined variable \$\{NOPE\}"):
        load_config(_write(tmp_path, text))


# --- mount groups + context `include` ----------------------------------------

_CREDS_GROUP = """\
[vars]
WM = "~/work-mappings"

[mount_groups.creds]
mounts = [
  { path = "~/.ssh",       from = "${WM}/.ssh",       mode = "ro" },
  { path = "~/.gnupg",     from = "${WM}/.gnupg",     mode = "ro" },
  { path = "~/.gitconfig", from = "${WM}/.gitconfig", mode = "ro" },
]
"""


def test_two_contexts_share_group_plus_own(tmp_path):
    text = _CREDS_GROUP + """\
[[contexts]]
name    = "api"
when    = ["~/work/api"]
include = ["creds"]
  [[contexts.mounts]]
  path = "~/work/api"

[[contexts]]
name    = "web"
when    = ["~/work/web"]
include = "creds"
"""
    cfg = load_config(_write(tmp_path, text))
    api, web = cfg.contexts

    # both contexts carry the three group mounts (~-expanded, ${WM} resolved)
    creds = {
        os.path.expanduser("~/.ssh"): os.path.expanduser("~/work-mappings/.ssh"),
        os.path.expanduser("~/.gnupg"): os.path.expanduser("~/work-mappings/.gnupg"),
        os.path.expanduser("~/.gitconfig"): os.path.expanduser(
            "~/work-mappings/.gitconfig"
        ),
    }
    for ctx in (api, web):
        by_path = {m.path: m for m in ctx.mounts}
        for path, host in creds.items():
            assert path in by_path, ctx.name
            assert by_path[path].host_path == host
            assert by_path[path].mode == "ro"

    # api has its own extra mount; web (bare-string include) has only the creds
    assert os.path.expanduser("~/work/api") in {m.path for m in api.mounts}
    assert len(web.mounts) == 3


def test_include_order_then_inline(tmp_path):
    # included groups (in `include` order) come before the context's own inline
    # mounts; first-seen position is stable.
    text = """\
[mount_groups.a]
mounts = [{ path = "~/a1" }, { path = "~/a2" }]
[mount_groups.b]
mounts = [{ path = "~/b1" }]

[[contexts]]
name    = "x"
when    = ["~/x"]
include = ["a", "b"]
  [[contexts.mounts]]
  path = "~/own"
"""
    cfg = load_config(_write(tmp_path, text))
    paths = [m.path for m in cfg.contexts[0].mounts]
    assert paths == [os.path.expanduser(p) for p in ("~/a1", "~/a2", "~/b1", "~/own")]


def test_inline_overrides_group_mount(tmp_path):
    # an inline mount with the same sandbox-side `path` as a group mount wins
    # (later-wins): asserted on mode + from_.
    text = """\
[mount_groups.creds]
mounts = [{ path = "~/.ssh", from = "~/work-mappings/.ssh", mode = "ro" }]

[[contexts]]
name    = "x"
when    = ["~/x"]
include = ["creds"]
  [[contexts.mounts]]
  path = "~/.ssh"
  mode = "rw"
"""
    cfg = load_config(_write(tmp_path, text))
    by_path = {m.path: m for m in cfg.contexts[0].mounts}
    ssh = by_path[os.path.expanduser("~/.ssh")]
    assert ssh.mode == "rw"            # inline mode wins
    assert ssh.from_ is None           # inline is parity, overriding the alias
    # deduped: only one ~/.ssh entry
    assert [m.path for m in cfg.contexts[0].mounts].count(
        os.path.expanduser("~/.ssh")
    ) == 1


def test_later_group_overrides_earlier(tmp_path):
    text = """\
[mount_groups.a]
mounts = [{ path = "~/.ssh", mode = "ro" }]
[mount_groups.b]
mounts = [{ path = "~/.ssh", mode = "rw" }]

[[contexts]]
name    = "x"
when    = ["~/x"]
include = ["a", "b"]
"""
    cfg = load_config(_write(tmp_path, text))
    by_path = {m.path: m for m in cfg.contexts[0].mounts}
    assert by_path[os.path.expanduser("~/.ssh")].mode == "rw"  # later group wins


def test_unknown_include_rejected(tmp_path):
    text = """\
[[contexts]]
name    = "x"
when    = ["~/x"]
include = ["nope"]
"""
    with pytest.raises(
        ConfigError, match=r"context 'x': unknown mount group 'nope'"
    ):
        load_config(_write(tmp_path, text))


# --- environment (`[env]` + context `env`) -----------------------------------


def test_env_global_literal_and_forward_parsed(tmp_path):
    text = """\
[env]
EDITOR  = "vim"
forward = ["GH_TOKEN"]
"""
    cfg = load_config(_write(tmp_path, text))
    assert dict(cfg.env) == {"EDITOR": "vim"}
    assert cfg.forward == ("GH_TOKEN",)


def test_env_per_context_inline_table(tmp_path):
    text = """\
[[contexts]]
name = "c"
when = ["~/p"]
env  = { DEPLOY_ENV = "work", forward = ["WORK_TOKEN"] }
"""
    ctx = load_config(_write(tmp_path, text)).contexts[0]
    assert dict(ctx.env) == {"DEPLOY_ENV": "work"}
    assert ctx.forward == ("WORK_TOKEN",)


def test_env_per_context_subtable_form(tmp_path):
    # The `[contexts.env]` sub-table form parses to the same dict as the inline
    # form.
    text = """\
[[contexts]]
name = "c"
when = ["~/p"]
  [contexts.env]
  DEPLOY_ENV = "work"
"""
    ctx = load_config(_write(tmp_path, text)).contexts[0]
    assert dict(ctx.env) == {"DEPLOY_ENV": "work"}


def test_env_invalid_name_rejected(tmp_path):
    text = '[env]\n"bad-name" = "x"\n'
    with pytest.raises(ConfigError, match="invalid environment variable name"):
        load_config(_write(tmp_path, text))


def test_env_non_string_value_rejected(tmp_path):
    text = "[env]\nFOO = 123\n"
    with pytest.raises(ConfigError, match="expected a string"):
        load_config(_write(tmp_path, text))


def test_env_forward_elements_must_be_strings(tmp_path):
    text = "[env]\nforward = [1, 2]\n"
    with pytest.raises(ConfigError, match="expected a string"):
        load_config(_write(tmp_path, text))


def test_env_forward_wrong_type_rejected(tmp_path):
    text = "[env]\nforward = 5\n"
    with pytest.raises(ConfigError, match="expected a string or list"):
        load_config(_write(tmp_path, text))


@pytest.mark.parametrize("name", ["HOME", "USER"])
def test_env_reserved_key_rejected(tmp_path, name):
    text = f'[env]\n{name} = "x"\n'
    with pytest.raises(ConfigError, match="reserved"):
        load_config(_write(tmp_path, text))


@pytest.mark.parametrize("name", ["HOME", "USER"])
def test_env_reserved_key_rejected_in_context(tmp_path, name):
    text = f"""\
[[contexts]]
name = "c"
when = ["~/p"]
env  = {{ {name} = "/x" }}
"""
    with pytest.raises(ConfigError, match="reserved"):
        load_config(_write(tmp_path, text))


def test_env_path_allowed_literal_and_forward(tmp_path):
    # PATH is no longer reserved: it parses both as a literal and in `forward`,
    # to be folded into the sandbox PATH at launch.
    text = """\
[env]
PATH    = "/opt/tools/bin"
forward = ["PATH"]
"""
    cfg = load_config(_write(tmp_path, text))
    assert cfg.env["PATH"] == "/opt/tools/bin"
    assert "PATH" in cfg.forward


def test_env_var_expansion_in_value_no_tilde(tmp_path):
    text = """\
[vars]
WM = "~/proj"

[env]
PATH_EXTRA    = "${WM}/bin"
HOME_LITERAL  = "~/x"
"""
    cfg = load_config(_write(tmp_path, text))
    # ${WM} expands to the var value verbatim ("~/proj"); env values are NOT
    # ~-expanded (env != path) -- both the substituted and literal ~ survive.
    assert cfg.env["PATH_EXTRA"] == "~/proj/bin"
    assert cfg.env["HOME_LITERAL"] == "~/x"


def test_env_absent_defaults_empty(tmp_path):
    cfg = load_config(_write(tmp_path, SAMPLE))
    assert dict(cfg.env) == {}
    assert cfg.forward == ()
    assert dict(cfg.contexts[0].env) == {}
    assert cfg.contexts[0].forward == ()


# --- shipped default config --------------------------------------------------


def test_ensure_user_config_writes_defaults(tmp_path):
    d = tmp_path / "cfgdir"
    path = ensure_user_config(d)
    assert path == d / "config.toml"
    assert path.exists()

    # the shipped default must itself parse + validate cleanly
    cfg = load_config(path)
    assert isinstance(cfg, Config)
    # the default config is agent-neutral: no active mounts (an agent's own
    # auth/config dirs come from its built-in default_mounts, not from here)
    assert cfg.mounts == ()
    assert os.path.expanduser("~/.claude") not in [m.path for m in cfg.mounts]


def test_ensure_user_config_idempotent_no_overwrite(tmp_path):
    d = tmp_path / "cfgdir"
    path = ensure_user_config(d)
    path.write_text('[[mounts]]\npath = "~/custom"\n')
    again = ensure_user_config(d)  # must not clobber the user's edits
    assert again == path
    cfg = load_config(path)
    assert [m.path for m in cfg.mounts] == [os.path.expanduser("~/custom")]
