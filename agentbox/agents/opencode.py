"""The opencode agent: the ``opencode`` CLI as a single-binary native install.

:class:`OpencodeAgent` is the built-in agent for `opencode <https://opencode.ai>`_
(the ``anomalyco/opencode`` terminal coding agent). Its plumbing was confirmed by
inspecting the real installer (``opencode.ai/install``) and the upstream source,
not assumed:

* **Install.** The installer hardcodes ``INSTALL_DIR=$HOME/.opencode/bin`` -- there
  is *no* install-dir override env var (unlike copilot's ``PREFIX``), so the only
  lever that redirects it into the wrapper-private store is ``HOME`` (exactly as
  for claude): ``HOME={store}`` lands the binary at ``{store}/.opencode/bin/opencode``.
  The Linux release tarball is a *lone binary* (the installer does ``mv .../opencode
  "$INSTALL_DIR"``), so :attr:`InstallRecipe.payload_rel` is ``None``. The installer
  also offers to edit a shell rc for ``PATH``, but with ``HOME`` pointed at a fresh
  store dir it finds no rc file and only prints guidance -- it mutates nothing.

* **Version pin -- unsupported** (``version_args=None``, see
  :attr:`InstallRecipe.version_args`): the installer *can* pin (``--version <v>``),
  but the lone binary reports no version for the freshness check to verify.

* **Self-update freeze.** opencode auto-updates on startup unless gated. The gate
  (confirmed in ``packages/opencode/src/cli/upgrade.ts``) is
  ``if (config.autoupdate === false || Flag.OPENCODE_DISABLE_AUTOUPDATE) return``,
  and ``Flag.OPENCODE_DISABLE_AUTOUPDATE`` reads the ``OPENCODE_DISABLE_AUTOUPDATE``
  env var as truthy (``"true"``/``"1"``). The freeze is injected via
  :attr:`runtime_env` (``OPENCODE_DISABLE_AUTOUPDATE=true``); the read-only store
  binds ``~/.opencode/bin/opencode`` read-only anyway, so even a stray update could
  not rewrite it. Nothing is written into the store (the inherited no-op
  :meth:`disable_self_update`): opencode reads its ``autoupdate`` config from
  ``~/.config/opencode``, a user-owned mount, never a store-side file.

* **Auth/config.** opencode follows XDG across three base dirs: config under
  ``~/.config/opencode`` (``opencode.json``/``tui.json`` plus ``agents/``,
  ``commands/``, ``plugins/``, ``themes/``, ...); data under
  ``~/.local/share/opencode`` (the credential ``opencode auth login`` writes,
  ``auth.json``, plus storage and logs); and *state* under
  ``~/.local/state/opencode`` (``model.json`` -- the last-used/recent/favorite
  model selection). All three are XDG-distinct (``XDG_CONFIG_HOME`` /
  ``XDG_DATA_HOME`` / ``XDG_STATE_HOME``), so three separate mounts are needed --
  in particular ``~/.local/state`` is *not* under ``~/.local/share``, and without
  the state mount opencode loses its persisted model selection every launch and
  falls back to provider auto-detection. Three *directory* mounts cover them, so
  the single-file atomic-replace trap (a ``rename()`` over a bind-mounted file →
  ``EBUSY``) does not apply. None sits under ``~/.opencode`` (which holds only the
  binary, i.e. the read-only store target), so there is no store-shadow conflict.

  The env surface is deliberately empty. opencode is provider-agnostic with no
  single auth prefix; its first-class credential path is ``opencode auth login``
  (persisted via the ``~/.local/share/opencode`` mount). The ``OPENCODE_*`` prefix
  is *not* forwarded -- a host ``OPENCODE_CONFIG_DIR``/``OPENCODE_CONFIG`` would
  re-point the config dir out of the mount, and a host ``OPENCODE_DISABLE_AUTOUPDATE``
  must not be able to defeat the freeze. A user who authenticates a provider by env
  var instead forwards exactly those keys via ``[env].forward`` in config.

opencode has no editor/IDE bridge wired yet, so it carries no ``launch_hook`` (the
default ``None``); it launches as the plain ``(exec_path, *args)``.
"""

from __future__ import annotations

from ..config import MountSpec
from .base import Agent, InstallRecipe


class OpencodeAgent(Agent):
    """The built-in agent for the ``opencode`` CLI."""

    name = "opencode"
    command = "opencode"
    install = InstallRecipe(
        url="https://opencode.ai/install",
        redirect_env="HOME",
        redirect_value="{store}",
        binary_rel=(".opencode", "bin", "opencode"),
        payload_rel=None,
        version_args=None,  # pin unsupported (see module docstring)
    )
    env_prefixes = ()  # deliberately empty (see module docstring)
    env_names = ()
    default_mounts = (
        MountSpec(path="~/.config/opencode"),
        MountSpec(path="~/.local/share/opencode"),
        MountSpec(path="~/.local/state/opencode"),
    )
    runtime_env = (("OPENCODE_DISABLE_AUTOUPDATE", "true"),)
