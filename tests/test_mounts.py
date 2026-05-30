"""Unit tests for claude_sandbox.mounts."""

from __future__ import annotations

import os

import pytest

from claude_sandbox.config import parse_config
from claude_sandbox.mounts import (
    DEFAULT_CONTEXT,
    MountError,
    Resolution,
    guard_claude_shadow,
    resolve,
    resolve_context,
)

HOME = os.path.expanduser("~")


def _h(*parts: str) -> str:
    return os.path.join(HOME, *parts)


def _paths(res: Resolution) -> list[str]:
    return [m.path for m in res.mounts]


# --- context resolution ------------------------------------------------------


def test_longest_prefix_wins():
    cfg = parse_config(
        {
            "contexts": [
                {"name": "outer", "when": ["~/work"]},
                {"name": "inner", "when": ["~/work/api"]},
            ]
        }
    )
    assert resolve_context(cfg, _h("work", "api", "sub")).name == "inner"
    assert resolve_context(cfg, _h("work", "other")).name == "outer"


def test_equal_length_tie_resolves_to_config_order():
    # The only way two distinct contexts can tie on prefix length is the same
    # prefix string; the earlier context in config order then wins.
    cfg = parse_config(
        {
            "contexts": [
                {"name": "first", "when": ["~/work"]},
                {"name": "second", "when": ["~/work"]},
            ]
        }
    )
    assert resolve_context(cfg, _h("work", "x")).name == "first"


def test_when_list_is_an_implicit_or():
    cfg = parse_config({"contexts": [{"name": "c", "when": ["~/a", "~/b"]}]})
    assert resolve_context(cfg, _h("b", "x")).name == "c"
    assert resolve_context(cfg, _h("c", "x")) is None


def test_prefix_match_respects_path_boundaries():
    # "~/work" must not match "~/workshop" (string-prefix but not a path prefix).
    cfg = parse_config({"contexts": [{"name": "c", "when": ["~/work"]}]})
    assert resolve_context(cfg, _h("workshop")) is None
    assert resolve_context(cfg, _h("work")).name == "c"


def test_unmatched_cwd_is_default_context_with_globals_and_cwd():
    cfg = parse_config(
        {
            "mounts": [{"path": "~/.claude"}],
            "contexts": [{"name": "api", "when": ["~/work/api"]}],
        }
    )
    res = resolve(cfg, _h("scratch", "thing"), home=HOME)
    assert isinstance(res, Resolution)
    assert res.context == DEFAULT_CONTEXT
    # global baseline still present; the cwd is bound; no context mounts added.
    assert _h(".claude") in _paths(res)
    assert _h("scratch", "thing") in _paths(res)


# --- effective mount set -----------------------------------------------------


def test_effective_set_is_global_plus_context_plus_cwd():
    cfg = parse_config(
        {
            "mounts": [{"path": "~/.claude"}],
            "contexts": [
                {
                    "name": "api",
                    "when": ["~/work/api"],
                    "mounts": [{"path": "~/.ssh", "mode": "ro"}],
                }
            ],
        }
    )
    res = resolve(cfg, _h("work", "api"), home=HOME)
    assert res.context == "api"
    paths = _paths(res)
    assert _h(".claude") in paths  # global
    assert _h(".ssh") in paths  # context
    assert _h("work", "api") in paths  # cwd


def test_cwd_bind_added_when_not_covered():
    cfg = parse_config({"mounts": [{"path": "~/.claude"}]})
    res = resolve(cfg, _h("proj"), home=HOME)
    cwd_bind = next(m for m in res.mounts if m.path == _h("proj"))
    assert cwd_bind.mode == "rw"
    assert cwd_bind.from_ is None  # parity bind of the cwd itself


def test_cwd_bind_dropped_when_covered_by_parity_mount():
    cfg = parse_config(
        {
            "contexts": [
                {
                    "name": "work",
                    "when": ["~/work"],
                    "mounts": [{"path": "~/work"}],  # parity mount of the tree
                }
            ]
        }
    )
    res = resolve(cfg, _h("work", "proj"), home=HOME)
    # ~/work/proj is already exposed by the parity mount ~/work -> no extra bind.
    assert _paths(res) == [_h("work")]


def test_cwd_normalized():
    res = resolve(parse_config({}), _h("proj") + "/", home=HOME)
    assert res.cwd == _h("proj")


def test_sibling_dir_never_auto_exposed():
    # whitelist / default-deny: a sibling of the workspace is absent from the set.
    cfg = parse_config(
        {
            "contexts": [
                {
                    "name": "a",
                    "when": ["~/work/a"],
                    "mounts": [{"path": "~/work/a"}],
                }
            ]
        }
    )
    res = resolve(cfg, _h("work", "a"), home=HOME)
    assert _h("work", "a") in _paths(res)
    assert _h("work", "b") not in _paths(res)


def test_context_mount_overrides_global_same_path():
    cfg = parse_config(
        {
            "mounts": [{"path": "~/.ssh", "from": "~/.ssh-global", "mode": "ro"}],
            "contexts": [
                {
                    "name": "c",
                    "when": ["~/work"],
                    "mounts": [{"path": "~/.ssh", "mode": "rw"}],
                }
            ],
        }
    )
    res = resolve(cfg, _h("work", "x"), home=HOME)
    ssh = [m for m in res.mounts if m.path == _h(".ssh")]
    assert len(ssh) == 1  # deduplicated, not bound twice
    assert ssh[0].mode == "rw"  # context wins
    assert ssh[0].from_ is None  # context parity overrides the global alias


def test_nested_bind_ordered_after_its_ancestor():
    cfg = parse_config(
        {
            "contexts": [
                {
                    "name": "c",
                    "when": ["~/work"],
                    "mounts": [
                        {"path": "~/work/proj/cache", "from": "~/caches/proj"},
                        {"path": "~/work/proj"},
                    ],
                }
            ]
        }
    )
    res = resolve(cfg, _h("work", "proj"), home=HOME)
    paths = _paths(res)
    # The ancestor must precede the descendant so the nested bind overlays it.
    assert paths.index(_h("work", "proj")) < paths.index(
        _h("work", "proj", "cache")
    )


# --- guards: unsafe working directory ----------------------------------------


def test_cwd_under_alias_target_refused():
    cfg = parse_config(
        {
            "contexts": [
                {
                    "name": "c",
                    "when": ["~/work"],
                    "mounts": [
                        {"path": "~/work", "from": "~/work-real", "mode": "ro"}
                    ],
                }
            ]
        }
    )
    with pytest.raises(MountError, match="aliased"):
        resolve(cfg, _h("work", "x"), home=HOME)


def test_cwd_under_alias_source_refused():
    cfg = parse_config(
        {"mounts": [{"path": "~/.ssh", "from": "~/.ssh-api", "mode": "ro"}]}
    )
    with pytest.raises(MountError, match="aliased"):
        resolve(cfg, _h(".ssh-api", "keys"), home=HOME)


def test_home_itself_refused():
    with pytest.raises(MountError, match=r"\$HOME"):
        resolve(parse_config({}), HOME, home=HOME)


def test_filesystem_root_refused():
    with pytest.raises(MountError, match="root"):
        resolve(parse_config({}), "/", home=HOME)


@pytest.mark.parametrize("cwd", ["/etc", "/usr/local/foo", "/var/tmp", "/proc"])
def test_system_roots_refused(cwd):
    with pytest.raises(MountError, match="system path"):
        resolve(parse_config({}), cwd, home=HOME)


@pytest.mark.parametrize(
    "rel",
    [".local", ".local/share", ".local/share/claude", ".local/share/claude/versions"],
)
def test_claude_store_cwd_refused(rel):
    with pytest.raises(MountError, match="claude store"):
        resolve(parse_config({}), _h(*rel.split("/")), home=HOME)


def test_local_bin_cwd_allowed():
    # ~/.local/bin is not a store location, so it is a usable workspace.
    res = resolve(parse_config({}), _h(".local", "bin"), home=HOME)
    assert _h(".local", "bin") in _paths(res)


# --- guards: claude-store shadowing ------------------------------------------


@pytest.mark.parametrize(
    "path", ["~/.local", "~/.local/share", "~/.local/share/claude", "~/.local/bin"]
)
def test_mount_shadowing_claude_store_refused(path):
    cfg = parse_config({"mounts": [{"path": path}]})
    with pytest.raises(MountError, match="shadow"):
        guard_claude_shadow(cfg, home=HOME)


def test_context_mount_shadowing_claude_store_refused():
    # Per-context mounts are checked too, not just the global ones.
    cfg = parse_config(
        {
            "contexts": [
                {
                    "name": "c",
                    "when": ["~/work"],
                    "mounts": [{"path": "~/.local/share/claude"}],
                }
            ]
        }
    )
    with pytest.raises(MountError, match="shadow"):
        guard_claude_shadow(cfg, home=HOME)


def test_mount_beside_claude_store_allowed():
    cfg = parse_config({"mounts": [{"path": "~/.local/share/other"}]})
    guard_claude_shadow(cfg, home=HOME)  # must not raise


def test_resolve_runs_the_shadow_guard():
    cfg = parse_config({"mounts": [{"path": "~/.local/share"}]})
    with pytest.raises(MountError, match="shadow"):
        resolve(cfg, _h("proj"), home=HOME)
