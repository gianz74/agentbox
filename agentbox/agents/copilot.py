"""The copilot agent: GitHub's ``copilot`` CLI as a single-binary native install.

:class:`CopilotAgent` is the built-in agent for GitHub Copilot CLI. Its plumbing
was confirmed by inspecting the real installer (``gh.io/copilot-install``) and a
scratch install, not assumed:

* **Install.** A native installer redirected by the ``PREFIX`` env var into the
  wrapper-private store: ``PREFIX={store}/.local`` lands the binary at
  ``{store}/.local/bin/copilot``. The release tarball is a *lone binary* (no
  versioned payload tree), so :attr:`InstallRecipe.payload_rel` is ``None``.

* **Version pin -- unsupported** (``version_args=None``, see
  :attr:`InstallRecipe.version_args`): the installer takes a version via the
  ``VERSION`` env var, not argv, and the lone binary reports no version anyway.

* **Self-update freeze.** copilot's auto-update is gated by the
  ``COPILOT_AUTO_UPDATE`` runtime env (it updates *unless* the value is
  ``"false"``), loading a newer JS ``pkg`` from ``~/.copilot/pkg`` -- a writable
  default mount -- so a read-only store binary alone would not freeze it. The
  freeze is injected via :attr:`runtime_env`; nothing is written into the store
  (the inherited no-op :meth:`disable_self_update`).

* **Auth/config.** Everything copilot persists lives under ``~/.copilot``
  (``COPILOT_HOME``): ``config.json``, ``mcp-config.json``, the credential it
  stores after login, and the ``pkg`` cache. A single *directory* mount covers
  it, so the single-file atomic-replace trap (a ``rename()`` over a bind-mounted
  file → ``EBUSY``) does not apply here. The three token vars copilot accepts for
  non-interactive auth (``COPILOT_GITHUB_TOKEN``/``GH_TOKEN``/``GITHUB_TOKEN``) are
  forwarded by name; the ``COPILOT_*`` prefix is deliberately *not* forwarded so a
  host ``COPILOT_AUTO_UPDATE``/``COPILOT_HOME`` cannot defeat the freeze or
  re-point the config dir. (An interactive ``copilot login`` also works: it stores
  a token under ``~/.copilot``, which persists via the directory mount.)

copilot has no editor/IDE bridge wired yet, so it carries no ``launch_hook`` (the
default ``None``); it launches as the plain ``(exec_path, *args)``.
"""

from __future__ import annotations

from ..config import MountSpec
from .base import Agent, InstallRecipe


class CopilotAgent(Agent):
    """The built-in agent for GitHub's ``copilot`` CLI."""

    name = "copilot"
    command = "copilot"
    install = InstallRecipe(
        url="https://gh.io/copilot-install",
        redirect_env="PREFIX",
        redirect_value="{store}/.local",
        binary_rel=(".local", "bin", "copilot"),
        payload_rel=None,
        version_args=None,  # pin unsupported (see module docstring)
    )
    env_prefixes = ()
    # The three token vars copilot accepts for non-interactive auth (per its own
    # "No authentication information found" guidance). Listed by name, not via a
    # COPILOT_* prefix, so the freeze knob (COPILOT_AUTO_UPDATE) stays unforwarded.
    env_names = ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")
    default_mounts = (MountSpec(path="~/.copilot"),)
    runtime_env = (("COPILOT_AUTO_UPDATE", "false"),)
