# claude-sandbox

Run the `claude` CLI inside an unprivileged [bubblewrap](https://github.com/containers/bubblewrap)
(`bwrap`) sandbox. Unrecognized arguments pass through to `claude`; management
lives in two subcommands, `setup` and `delete`.

`claude` is run against untrusted repository content, so a compromised session
must not reach sibling projects, private material, or host-local network
services. Each launch assembles a fresh, lightweight namespace sandbox from an
argv and tears it down on exit — no daemon, no image, no persistent container.

## Status

Early scaffolding. The package skeleton and CLI dispatch are in place; the
sandbox mechanism, config loader, store lifecycle, and IDE bridge are being
built up incrementally.

## Usage (intended)

```sh
claude-sandbox setup            # install/refresh the frozen claude store + host checks
claude-sandbox delete           # remove the store + persistent caches

claude --mount /some/dir -- -p "hello"   # run path: leading --mount binds, then claude args
```

The user-facing command is `claude` — a `$PATH` shim pointing at the
`claude-sandbox` entry point.

## Development

```sh
pipx install -e .             # or: pip install -e '.[test]'
pytest -q                     # unit tests for pure logic
```

Requires Python ≥ 3.11. The sandbox mechanism relies on the host's `bwrap` and
`pasta` binaries and an unprivileged-user-namespaces-enabled kernel — these are a
host concern, not Python dependencies.
