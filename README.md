# box

A generic, agent-agnostic sandbox for **agentic CLIs**. `box` runs a coding
agent — `claude`, `copilot`, … — inside an unprivileged
[bubblewrap](https://github.com/containers/bubblewrap) (`bwrap`) filesystem +
namespace sandbox, fronted by [pasta](https://passt.top/) for network isolation.

An agent is typically run against untrusted repository content, so a compromised
session must not reach sibling projects, private material, or host-local network
services. Each launch assembles a fresh, lightweight namespace sandbox from an
argv and tears it down on exit — no daemon, no image, no persistent container.

The sandbox is generic; everything agent-specific — how the tool installs, which
environment it reads, where its auth lives, and any editor/IDE bridge it needs —
is carried by a built-in **agent**, selected by the invoked name (`argv[0]`).

## Agents

| | claude | copilot |
|---|---|---|
| CLI | Anthropic Claude Code | GitHub Copilot CLI |
| Installer | `claude.ai/install.sh` | `gh.io/copilot-install` |
| Auth/config mount | `~/.claude`, `~/.claude.json` | `~/.copilot` |
| Auth env | `ANTHROPIC_*`, `CLAUDE_*` | `COPILOT_GITHUB_TOKEN`, `GH_TOKEN`, `GITHUB_TOKEN` |
| Editor/IDE bridge | MCP/SSE + lockfile reconciliation | — (none yet) |

Each agent's auth/config directories are mounted automatically — you do **not**
list them under `[[mounts]]`.

## How it works

* **pasta is the parent and spawns bwrap.** pasta creates the network + user
  namespace and configures NAT/DNS; bwrap shares that netns and unshares
  everything else. The gateway address is *not* mapped to the host
  (`--no-map-gw`), so the guest cannot reach host-localhost services — except the
  exact loopback ports an editor bridge asks pasta to forward (`-T`).
* **Frozen store.** `box <agent> setup` performs a real native install of the
  agent's CLI, redirected into a wrapper-private store at
  `~/.local/share/box/<agent>/store`, with self-update disabled. The store is
  bound **read-only** into every sandbox, so a session can run the agent but
  never mutate it; `setup` is the only thing that refreshes it.
* **Recursion guard.** The store binary is exec'd by absolute path, and a private
  launcher (`/opt/box/bin/<command>`, prepended to `PATH`) makes a bare
  `<command>` resolve to the store binary too — so the shim never re-invokes
  itself.

## Install

```sh
pipx install -e .             # or: pip install -e '.[test]'
```

This installs the single console entry point, `box`. Python ≥ 3.11; the sandbox
mechanism relies on the host's `bwrap` and `pasta` binaries and an
unprivileged-user-namespaces-enabled kernel (host concerns, not Python deps).

## Usage

`box` is a multi-call binary (like busybox/git): the invoked name selects the
agent. There are two surfaces.

### Management — `box <agent> setup|delete`

```sh
box claude setup              # install/refresh the frozen claude store + host checks
box claude setup --from-host  # build by copying the host's existing native install
box claude delete             # remove the store (next launch rebuilds it)

box copilot setup
```

`setup` runs a host-readiness preflight (unprivileged user namespaces, `bwrap`,
`pasta`) and, once the store is built, prints how to point the user-facing
`<command>` at this wrapper. It mutates nothing on the host but the private
store — it never creates the shim or edits your shell config for you.

### Shim — point `<command>` at `box`

The user-facing command for each agent is its own name (`claude`, `copilot`) — a
`$PATH` shim that points at the `box` entry point:

```sh
mkdir -p ~/bin
ln -sf "$(command -v box)" ~/bin/claude     # repeat per agent, e.g. copilot
# keep ~/bin ahead on PATH, e.g. in ~/.profile:
export PATH="$HOME/bin:$PATH"
```

`box <agent> setup` prints the exact command for your machine when the shim is
missing or points elsewhere.

### Run — `<command> [--mount …] [--] <agent args>`

```sh
claude -p "hello"                          # run claude sandboxed in the cwd
claude --mount /data/refs:ro -- -p "…"     # bind an extra dir read-only, then pass through
copilot -p "say hi"
```

Everything after the leading block of `--mount PATH[:ro]` modifiers (and an
optional `--` terminator) is forwarded to the agent verbatim. The wrapper always
operates on the current working directory.

## Configuration

The first run writes a documented default to `~/.config/box/config.toml`. It is
**agent-neutral** — global `[[mounts]]`, `[env]`, cwd-prefix `[[contexts]]`,
reusable `[mount_groups]`, and a `[vars]` pre-pass apply to every agent. Per-agent
options live under `[agents.<name>]` (validated against the built-in registry);
v1 carries a `version` pin only:

```toml
[agents.claude]
version = "2.1.150"        # optional; unset = latest at setup time
```

> Do **not** pin `[agents.copilot].version`: copilot's installer takes a version
> via an env var the recipe can't express, so a pin would never converge and would
> rebuild the store on every launch. See `agentbox/agents/copilot.py`.

The shipped default file documents every table inline; read it after the first
run.

## Development

```sh
pip install -e '.[test]'
pytest -q                     # unit tests (pure logic, host-independent)
bash tests/t*.sh              # integration drivers (need bwrap + pasta + userns)
```

Requires Python ≥ 3.11, stdlib only.
