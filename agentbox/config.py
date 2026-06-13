"""Config loading and validation.

Loads ``~/.config/box/config.toml`` via :mod:`tomllib` and models its
tables into frozen dataclasses. All host paths are ``~``-expanded at load time so
downstream code never has to.

Tables:

* ``[agents.<name>]`` — per-agent options, keyed by an agent name validated
  against the built-in registry. Version-only in v1 (an optional ``version`` pin);
  unset = latest at setup time. Tooling inside the sandbox comes from the host
  read-only.
* ``[[mounts]]`` — global persistent binds, present in every sandbox.
* ``[[contexts]]`` — cwd-prefix-selected bundles (``name`` / ``when`` / their own
  ``mounts``), optionally splicing in reusable ``[mount_groups]`` via ``include``.
* ``[vars]`` — a flat ``name -> string`` table whose ``${NAME}`` references are
  substituted into every other string value as a verbatim pre-pass, run *before*
  ``~`` expansion, so per-machine configs can stop repeating long path prefixes.
  ``${HOME}`` and ``${USER}`` are always available (seeded from the host,
  overridable by an explicit ``[vars]`` entry) so a home-relative value can be
  written where the consuming tool won't ``~``-expand it — notably ``[env]``
  values, which are literal by design.
* ``[env]`` (and per-context ``env``) — literal ``KEY = "value"`` pairs plus a
  reserved ``forward`` list of host var names, applied at ``exec`` time.

``${...}`` interpolation is the loader's own sugar (TOML has none): consumed at
parse time and absent from the runtime :class:`Config`.

Public surface:

* :class:`Config` (+ :class:`AgentConfig`, :class:`MountSpec`, :class:`Context`)
  — the parsed model.
* :class:`ConfigError` — raised with a clear, user-facing message on any
  malformed/invalid config (including a malformed TOML file).
* :func:`parse_config` — pure ``dict -> Config`` (easy to unit-test).
* :func:`load_config` — read + parse a specific file.
* :func:`ensure_user_config` — write the documented default ``config.toml`` on
  first run if absent; returns the config path.
* :func:`load_user_config` — convenience: ``load_config(ensure_user_config())``.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

# Bumped when the config's *shape* changes incompatibly. Folded into the
# store-identity stamp so a schema change forces a re-`setup`.
SCHEMA_VERSION = 2

_VALID_MODES = ("ro", "rw")

# Brace form only — a bare ``$NAME`` is left literal so paths containing ``$``
# survive untouched. Names match a conventional identifier.
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# Environment-variable names (literals + ``forward`` entries) must be valid shell
# identifiers. ``HOME``/``USER`` are reserved — they carry the sandbox identity and
# are rejected in any ``[env]``. ``PATH`` is *not* reserved: it is opt-in (literal
# and/or forwarded) and resolved into the launcher-prefixed sandbox PATH at launch.
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RESERVED_ENV = ("HOME", "USER")

# The closed set of top-level tables. An unknown key is a typo or a setting that
# this tool does not honor; reject it loudly rather than silently ignore it.
_TOP_LEVEL_KEYS = ("agents", "mounts", "contexts", "vars", "mount_groups", "env")
# Per-agent keys under ``[agents.<name>]``. Version-only in v1.
_AGENT_KEYS = ("version",)


class ConfigError(Exception):
    """A user-facing configuration error (bad TOML, missing/invalid field)."""


# --- model -------------------------------------------------------------------


@dataclass(frozen=True)
class MountSpec:
    """A persistent bind mount (global ``[[mounts]]`` or ``[[contexts.mounts]]``).

    ``path`` is the sandbox-side location (and, with no ``from``, also the host
    backing — "parity"). ``from_`` is the host backing when *aliasing* (e.g.
    ``~/.ssh`` backed by ``~/.ssh-api``). ``exclude`` lists sub-paths (relative to
    ``path``) to mask with an empty read-only overmount.

    ``seed`` declares an agent ``default_mounts`` entry as a *file* (vs the default
    *directory*) and gives the content written when seeding its missing host source
    (see ``run.ensure_default_mount_sources``): ``None`` → a directory (``mkdir``);
    a string → a file holding exactly that text (e.g. ``"{}\n"`` for a JSON config).
    Only consulted for built-in default mounts; user ``[[mounts]]`` never set it.
    """

    path: str  # sandbox-side, ~-expanded
    from_: str | None = None  # host backing when aliasing, ~-expanded
    mode: str = "rw"  # "ro" | "rw"
    exclude: tuple[str, ...] = ()  # sub-paths relative to path
    seed: str | None = None  # default-mount file seed; None → directory

    @property
    def host_path(self) -> str:
        """Host-side backing path (the alias source, else ``path``)."""
        return self.from_ if self.from_ is not None else self.path

    @property
    def is_alias(self) -> bool:
        return self.from_ is not None


@dataclass(frozen=True)
class AgentConfig:
    """``[agents.<name>]`` — per-agent store-build options.

    ``version`` optionally pins the installed version; unset = latest at setup
    time. Version-only in v1 (agent-scoped env/mounts are deferred).
    """

    version: str | None = None


@dataclass(frozen=True)
class Context:
    name: str
    when: tuple[str, ...]  # host path prefixes (OR), ~-expanded
    mounts: tuple[MountSpec, ...] = ()
    # Per-context env: literal pairs + ``forward`` host var names. Applied at
    # ``exec claude``, never baked in.
    env: Mapping[str, str] = field(default_factory=dict)
    forward: tuple[str, ...] = ()


@dataclass(frozen=True)
class Config:
    # Per-agent options, keyed by agent name (validated against the registry).
    agents: Mapping[str, AgentConfig] = field(default_factory=dict)
    mounts: tuple[MountSpec, ...] = ()  # global, present in every sandbox
    contexts: tuple[Context, ...] = ()
    # Global env: merged broadest-first with each context's own env and applied
    # at ``exec`` time.
    env: Mapping[str, str] = field(default_factory=dict)
    forward: tuple[str, ...] = ()


def agent_version(config: "Config | None", agent_name: str) -> str | None:
    """The pinned version for *agent_name* from ``[agents.<name>].version``, or
    ``None`` when there is no config, no such ``[agents.<name>]`` table, or no
    pin (latest at setup time)."""
    if config is None:
        return None
    ac = config.agents.get(agent_name)
    return ac.version if ac is not None else None


# --- locations ---------------------------------------------------------------


def user_config_dir() -> Path:
    """``$XDG_CONFIG_HOME/box`` (falling back to ``~/.config``)."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )
    return Path(base) / "box"


# --- low-level coercion helpers ----------------------------------------------


def _expand(path: str) -> str:
    return os.path.expanduser(path)


def _require_str(value: object, where: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{where}: expected a string, got {type(value).__name__}")
    return value


def _opt_str(value: object, where: str) -> str | None:
    return None if value is None else _require_str(value, where)


def _str_list(value: object, where: str) -> tuple[str, ...]:
    """Accept a single string (coerced to one element) or a list of strings."""
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        return tuple(_require_str(v, f"{where}[{i}]") for i, v in enumerate(value))
    raise ConfigError(f"{where}: expected a string or list of strings")


def _reject_unknown_keys(raw: dict, allowed: tuple[str, ...], where: str) -> None:
    """Raise if *raw* carries any key outside *allowed* (a closed-schema table)."""
    unknown = [k for k in raw if k not in allowed]
    if unknown:
        names = ", ".join(repr(k) for k in sorted(unknown))
        allowed_names = ", ".join(sorted(allowed)) or "(none)"
        raise ConfigError(f"{where}: unknown key(s) {names} (allowed: {allowed_names})")


# --- variable expansion (`[vars]`) -------------------------------------------


def _implicit_vars() -> dict[str, str]:
    """Vars always available to the ``${NAME}`` pre-pass, overridable by an
    explicit ``[vars]`` entry of the same name.

    ``${HOME}`` / ``${USER}`` let a config express a home-relative value that the
    *consuming* tool won't ``~``-expand itself — e.g. ``GIT_CONFIG_GLOBAL`` in
    ``[env]``, whose values are literal. Because host HOME==sandbox HOME and host
    USER==sandbox USER, one value is correct on both sides of every mount.
    """
    home = os.path.expanduser("~")
    return {"HOME": home, "USER": os.environ.get("USER") or os.path.basename(home)}


def _parse_vars(raw: object) -> dict[str, str]:
    """Parse the ``[vars]`` table into a flat ``name -> str`` map.

    Values are used verbatim — a ``${...}`` inside a var value is *not* resolved
    (no recursion, vars cannot reference vars).
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError("[vars]: expected a table")
    variables: dict[str, str] = {}
    for name, value in raw.items():
        if not isinstance(value, str):
            raise ConfigError(
                f"[vars].{name}: expected a string, got {type(value).__name__}"
            )
        variables[name] = value
    return variables


def _substitute_vars(value: object, variables: dict[str, str], where: str) -> object:
    """Recursively replace ``${NAME}`` in every string under *value*.

    Brace form only; an undefined ``${NAME}`` raises :class:`ConfigError` naming
    the variable (and where it appeared). Non-string scalars pass through.
    """
    if isinstance(value, str):

        def _replace(m: re.Match[str]) -> str:
            name = m.group(1)
            if name not in variables:
                raise ConfigError(
                    f"{where}: undefined variable ${{{name}}} "
                    "(define it under [vars])"
                )
            return variables[name]

        return _VAR_RE.sub(_replace, value)
    if isinstance(value, dict):
        return {
            k: _substitute_vars(v, variables, f"{where}.{k}") for k, v in value.items()
        }
    if isinstance(value, list):
        return [
            _substitute_vars(v, variables, f"{where}[{i}]")
            for i, v in enumerate(value)
        ]
    return value


# --- section parsers ---------------------------------------------------------


def _parse_mount(raw: object, where: str) -> MountSpec:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: expected a table")
    if "path" not in raw:
        raise ConfigError(f"{where}: missing required 'path'")

    mode = raw.get("mode", "rw")
    mode = _require_str(mode, f"{where}.mode")
    if mode not in _VALID_MODES:
        raise ConfigError(
            f"{where}.mode: invalid mode {mode!r} (expected 'ro' or 'rw')"
        )

    from_raw = _opt_str(raw.get("from"), f"{where}.from")
    exclude = _str_list(raw["exclude"], f"{where}.exclude") if "exclude" in raw else ()

    return MountSpec(
        path=_expand(_require_str(raw["path"], f"{where}.path")),
        from_=_expand(from_raw) if from_raw is not None else None,
        mode=mode,
        exclude=exclude,
    )


def _parse_agents(raw: object) -> dict[str, AgentConfig]:
    """Parse ``[agents.<name>]`` tables into a ``name -> AgentConfig`` map.

    Each ``<name>`` must be a built-in agent (validated against the registry); an
    unknown name is a typo or an agent this build does not ship. Each body is a
    closed table carrying only ``version`` in v1.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError("[agents]: expected a table")
    # Imported lazily so the config module stays import-cycle-free (the registry's
    # agents import config); only reached when an [agents.*] table is present.
    from .agents import AGENTS

    out: dict[str, AgentConfig] = {}
    for name, body in raw.items():
        where = f"[agents.{name}]"
        if name not in AGENTS:
            known = ", ".join(sorted(AGENTS)) or "(none)"
            raise ConfigError(
                f"{where}: unknown agent {name!r} (known agents: {known})"
            )
        if not isinstance(body, dict):
            raise ConfigError(f"{where}: expected a table")
        _reject_unknown_keys(body, _AGENT_KEYS, where)
        version = _opt_str(body.get("version"), f"{where}.version")
        out[name] = AgentConfig(version=version)
    return out


# --- environment (`[env]` + context `env`) -----------------------------------


def _parse_env(raw: object, where: str) -> tuple[dict[str, str], tuple[str, ...]]:
    """Parse an ``[env]`` / per-context ``env`` table.

    Returns ``(literals, forward)``: the reserved lowercase key ``forward`` is a
    ``list[str]`` of host var names; every other pair is a literal
    ``KEY = "value"``. Validates env-name shape, string values, and rejects the
    reserved ``HOME``/``USER``. ``PATH`` is permitted (literal or forwarded) and
    folded into the sandbox PATH at launch. ``${VAR}`` is already expanded into
    literal values by the pre-pass; env values are *not* ``~``-expanded (they are
    not paths).
    """
    if raw is None:
        return {}, ()
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: expected a table")
    forward: tuple[str, ...] = ()
    literals: dict[str, str] = {}
    for key, value in raw.items():
        if key == "forward":
            forward = _str_list(value, f"{where}.forward")
            for name in forward:
                _check_env_name(name, f"{where}.forward")
            continue
        _check_env_name(key, where)
        if not isinstance(value, str):
            raise ConfigError(
                f"{where}.{key}: expected a string, got {type(value).__name__}"
            )
        literals[key] = value
    return literals, forward


def _check_env_name(name: str, where: str) -> None:
    if not _ENV_NAME_RE.match(name):
        raise ConfigError(
            f"{where}: invalid environment variable name {name!r} "
            "(expected letters, digits and underscores)"
        )
    if name in _RESERVED_ENV:
        raise ConfigError(
            f"{where}: {name} is reserved (sandbox identity) "
            "and may not be set in [env]"
        )


# --- mount groups (`[mount_groups]` + context `include`) ---------------------


def _parse_mount_groups(raw: object) -> dict[str, tuple[MountSpec, ...]]:
    """Parse ``[mount_groups.<name>]`` tables into a ``name -> mounts`` map.

    Each group's ``mounts`` array is parsed exactly like ``[[contexts.mounts]]``
    (inline or full tables). Parse-time-only — groups are flattened into the
    contexts that ``include`` them and never stored on :class:`Config`.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError("[mount_groups]: expected a table")
    groups: dict[str, tuple[MountSpec, ...]] = {}
    for name, body in raw.items():
        where = f"[mount_groups.{name}]"
        if not isinstance(body, dict):
            raise ConfigError(f"{where}: expected a table")
        raw_mounts = body.get("mounts", [])
        if not isinstance(raw_mounts, list):
            raise ConfigError(f"{where}.mounts: expected an array of tables")
        groups[name] = tuple(
            _parse_mount(m, f"{where}.mounts[{i}]") for i, m in enumerate(raw_mounts)
        )
    return groups


def _flatten_context_mounts(
    included: list[tuple[MountSpec, ...]], inline: tuple[MountSpec, ...]
) -> tuple[MountSpec, ...]:
    """Merge included-group mounts (in ``include`` order) then a context's own
    inline mounts, deduped by sandbox-side ``path`` with **later-wins** (inline
    overrides an included mount of the same ``path``; a later group overrides an
    earlier one). The flattened tuple is all downstream ever sees."""
    merged: dict[str, MountSpec] = {}
    for group_mounts in included:
        for m in group_mounts:
            merged[m.path] = m
    for m in inline:
        merged[m.path] = m
    return tuple(merged.values())


def _parse_context(
    raw: object, index: int, groups: dict[str, tuple[MountSpec, ...]]
) -> Context:
    where = f"[[contexts]][{index}]"
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: expected a table")
    if "name" not in raw:
        raise ConfigError(f"{where}: missing required 'name'")
    name = _require_str(raw["name"], f"{where}.name")
    if not name.strip():
        raise ConfigError(f"{where}.name: must not be empty")
    if "when" not in raw:
        raise ConfigError(f"context {name!r}: missing required 'when' (path prefixes)")
    when = tuple(_expand(p) for p in _str_list(raw["when"], f"context {name!r}.when"))
    if not when:
        raise ConfigError(f"context {name!r}: 'when' must list at least one path prefix")

    raw_mounts = raw.get("mounts", [])
    if not isinstance(raw_mounts, list):
        raise ConfigError(f"context {name!r}.mounts: expected an array of tables")
    inline_mounts = tuple(
        _parse_mount(m, f"context {name!r}.mounts[{i}]")
        for i, m in enumerate(raw_mounts)
    )

    include = (
        _str_list(raw["include"], f"context {name!r}.include")
        if "include" in raw
        else ()
    )
    included: list[tuple[MountSpec, ...]] = []
    for gname in include:
        if gname not in groups:
            raise ConfigError(
                f"context {name!r}: unknown mount group {gname!r} in 'include'"
            )
        included.append(groups[gname])

    env, forward = _parse_env(raw.get("env"), f"context {name!r}.env")

    return Context(
        name=name,
        when=when,
        mounts=_flatten_context_mounts(included, inline_mounts),
        env=env,
        forward=forward,
    )


def parse_config(data: dict, *, source: str = "<config>") -> Config:
    """Validate a parsed-TOML ``dict`` into a :class:`Config`.

    Raises :class:`ConfigError` with a clear message on any problem. ``source``
    is only used to make error messages locatable.
    """
    if not isinstance(data, dict):
        raise ConfigError(f"{source}: top level must be a table")

    _reject_unknown_keys(data, _TOP_LEVEL_KEYS, "config")

    # `${NAME}` pre-pass: substitute into every string value *except* the [vars]
    # table itself, before the section parsers (and their `~` expansion) run.
    # [vars] is then dropped — it has no runtime effect. Implicit ${HOME}/${USER}
    # are seeded first so an explicit [vars] entry of the same name still wins
    # (later key overrides in the merge).
    variables = {**_implicit_vars(), **_parse_vars(data.get("vars"))}
    data = {
        key: _substitute_vars(value, variables, key)
        for key, value in data.items()
        if key != "vars"
    }

    raw_mounts = data.get("mounts", [])
    if not isinstance(raw_mounts, list):
        raise ConfigError("[[mounts]]: expected an array of tables")
    global_mounts = tuple(
        _parse_mount(m, f"[[mounts]][{i}]") for i, m in enumerate(raw_mounts)
    )

    # Mount groups: parsed here so contexts can `include` them; flattened into
    # Context.mounts and never stored on Config.
    groups = _parse_mount_groups(data.get("mount_groups"))

    raw_contexts = data.get("contexts", [])
    if not isinstance(raw_contexts, list):
        raise ConfigError("[[contexts]]: expected an array of tables")
    contexts = tuple(
        _parse_context(c, i, groups) for i, c in enumerate(raw_contexts)
    )

    seen: set[str] = set()
    for ctx in contexts:
        if ctx.name in seen:
            raise ConfigError(f"duplicate context name {ctx.name!r}")
        seen.add(ctx.name)
    if "default" in seen:
        raise ConfigError(
            "context name 'default' is reserved for the no-context fallback"
        )

    env, forward = _parse_env(data.get("env"), "[env]")

    return Config(
        agents=_parse_agents(data.get("agents")),
        mounts=global_mounts,
        contexts=contexts,
        env=env,
        forward=forward,
    )


# --- file I/O ----------------------------------------------------------------


def load_config(path: str | os.PathLike[str]) -> Config:
    """Read, parse and validate a config file at *path*.

    Raises :class:`ConfigError` if the file is missing or malformed.
    """
    p = Path(path)
    try:
        raw = p.read_bytes()
    except FileNotFoundError as e:
        raise ConfigError(f"config file not found: {p}") from e
    except OSError as e:
        raise ConfigError(f"cannot read config file {p}: {e}") from e
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise ConfigError(f"malformed TOML in {p}: {e}") from e
    return parse_config(data, source=str(p))


def ensure_user_config(config_dir: str | os.PathLike[str] | None = None) -> Path:
    """Write the documented default ``config.toml`` if absent.

    Idempotent: never overwrites an existing file. Returns the config.toml path.
    """
    d = Path(config_dir) if config_dir is not None else user_config_dir()
    d.mkdir(parents=True, exist_ok=True)
    config_path = d / "config.toml"
    if not config_path.exists():
        config_path.write_text(_DEFAULT_CONFIG_TOML)
    return config_path


def load_user_config() -> Config:
    """Convenience: create defaults on first run, then load the user config."""
    return load_config(ensure_user_config())


# --- shipped defaults --------------------------------------------------------

_DEFAULT_CONFIG_TOML = """\
# box configuration.
# Host paths use ~ expansion. Paths absent on this machine are silently skipped.

# --- Variables ----------------------------------------------------------------
# ${NAME} is substituted into every string below *before* ~ expansion, to keep
# repeated path prefixes DRY. Brace form only (a bare $NAME is left literal).
# Vars cannot reference other vars (single level, no recursion).
# ${HOME} and ${USER} are always available (overridable by a [vars] entry) — use
# ${HOME} for a home-relative value the consuming tool won't ~-expand (e.g. an
# [env] value below).
# [vars]
# WM = "~/.config/box/work-mappings"

# --- Agents -------------------------------------------------------------------
# Per-agent options for `box <agent> setup`, which builds that agent's frozen
# store. The table name must be a built-in agent. Pin a specific version
# (default: latest at setup time). Auth/config mounts for each agent are built in
# and need not be listed under [[mounts]] below.
# [agents.claude]
# version = "2.1.150"

# --- Environment --------------------------------------------------------------
# Extra env passed into the sandbox at `exec <agent>`, on top of the always-
# forwarded universal baseline (terminal/locale, IDE hints, plus the selected
# agent's own env surface, e.g. claude's ANTHROPIC_*/CLAUDE_*).
# Applied at launch, never baked in. Literal KEY = "value" sets it verbatim
# (${VAR} from [vars] expands; no ~ expansion); the reserved `forward` key lists
# host var names passed through by value (an unset host var is skipped). A
# per-context `env` overrides the global one on a key collision.
# HOME/USER are reserved and rejected. PATH is special: it is opt-in and never
# replaces the sandbox PATH wholesale — a literal `PATH` and/or listing `PATH` in
# `forward` (the host PATH) become the *base*, onto which the private launcher
# prefix is prepended and the result deduped, so a bare `claude` still hits the
# store. With both, the literal is prepended ahead of the host PATH. Unmentioned,
# PATH stays the sandbox default (/usr/bin:/bin). Env values are literal — ~ is NOT
# expanded; for a home-relative path use ${HOME} (e.g. to point git at a config
# inside a *directory* mount, since a single-file ~/.gitconfig bind mount can't
# be rewritten atomically).
#
# List the proxy/cloud/cert vars THIS machine needs in `forward` below; a Bedrock
# host likewise adds its AWS_* creds by name.
# [env]
# EDITOR             = "vim"
# GIT_CONFIG_GLOBAL  = "${HOME}/.config/git/config"
# forward = [
#   "GH_TOKEN",
#   "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
#   "http_proxy", "https_proxy", "no_proxy",
#   "NODE_EXTRA_CA_CERTS", "CLOUD_ML_REGION", "GOOGLE_APPLICATION_CREDENTIALS",
# ]

# --- Global persistent mounts: present in every sandbox -----------------------
# `path` is the mount location (host & sandbox identical). `from` aliases a
# different host backing dir. Default mode is rw; mark credentials `ro`. The
# selected agent's own auth/config dirs are mounted automatically and need not be
# listed here.
# Example read-only credential mount:
# [[mounts]]
# path = "~/.aws"
# mode = "ro"

# --- Mount groups -------------------------------------------------------------
# A reusable, named bundle of mounts that several contexts can `include` — so a
# shared credential set isn't duplicated across contexts. A group is NOT a
# context: no `when`, never matched by resolution. Each entry under `mounts` is
# parsed exactly like a [[contexts.mounts]] table.
# [mount_groups.acme-creds]
# mounts = [
#   { path = "~/.ssh",       from = "${WM}/.ssh",       mode = "ro" },
#   { path = "~/.gnupg",     from = "${WM}/.gnupg",     mode = "ro" },
#   { path = "~/.gitconfig", from = "${WM}/.gitconfig", mode = "ro" },
# ]

# --- Contexts: cwd-prefix-selected bundles with their own mounts --------------
# `name` is required. `when` is a list of host path prefixes (OR); the longest
# matching prefix across all contexts wins. `include` splices in mount groups (a
# list, or a bare string for one); a context's own inline [[contexts.mounts]]
# override an included mount with the same `path` (later wins).
#
# [[contexts]]
# name    = "api"
# when    = ["~/work/acme-api"]
# include = ["acme-creds"]
# env     = { DEPLOY_ENV = "work", forward = ["WORK_TOKEN"] }  # overrides global [env]
#   [[contexts.mounts]]
#   path    = "~/work"  # whole-tree mount (broad)
#   exclude = ["secrets"]  # masked: appears as an empty read-only dir
"""
