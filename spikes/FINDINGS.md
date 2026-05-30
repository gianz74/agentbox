# Spike findings — go/no-go on the bwrap + isolated-net + IDE bridge design

**Verdict: GO.** All hard checks `(a)/(b)/(c)/(crux)` pass on the bare host
(uid 1000). `bash spikes/bwrap_net_spike.sh` exits 0 deterministically. The
SSE-bridge fallback ladder was **not needed** — the primary `pasta -T` path
carries the SSE stream. These facts feed the sandbox argv builder (`sandbox.py`),
the pasta lifecycle (`net.py`), the run path, and the MCP bridge.

## Integration pattern (the load-bearing decision)

**pasta is the PARENT and spawns bwrap.** pasta creates the net+user namespace
and configures NAT/DNS; bwrap then shares that netns (it does **not**
`--unshare-net`) and unshares everything else (`--unshare-user/ipc/pid/uts/cgroup`).

This sidesteps the netns chicken-and-egg of the alternative ("bwrap
`--unshare-net`, attach pasta by PID"): no PID handshake, no `setns` race. The
nested user namespace (pasta's outer userns → bwrap's `--uid/--gid` map) works —
inside the sandbox `id` reports uid 1000 / `$USER` correctly.

Consequence for `net.py`/`sandbox.py`: the wrapper's top-level process is
`pasta … -- bwrap …`; `sandbox.py` renders the bwrap argv, `net.py` renders the
pasta argv that wraps it. `--die-with-parent` on bwrap + pasta foreground (`-f`)
means pasta exits when the sandbox does (the sandbox is ephemeral).

## Pinned pasta flags

| Need | Flag | Note |
|------|------|------|
| Create netns + run sandbox | `pasta --config-net -f -q -- bwrap …` | `--config-net` sets up the tap iface; `-f` foreground (die with sandbox); `-q` quiet |
| **Net isolation** | `--no-map-gw` | **MANDATORY.** Without it pasta maps the gateway address to the host, so the guest reaches *every* host-localhost port via `http://<gw>:<port>`. This was a real leak observed in the spike (`gw:blocked_port` returned the secret) and is the single most important hardening flag. |
| DNS forwarding | `--dns-forward <gateway>` | guest `/etc/resolv.conf` = `nameserver <gateway>`; pasta intercepts :53 to that addr and forwards to the host's real resolver (`--dns-host` default = host's first nameserver). Survives `--no-map-gw`. Lookup latency ≈ 0.01 s → **no "DNS wait" risk**. |
| **SSE bridge (crux)** | `-T <CLAUDE_CODE_SSE_PORT>` | forwards exactly that one port from the netns loopback out to the host init-ns loopback. Guest connects to `127.0.0.1:<port>`; round-trips a real `text/event-stream` (3/3 events). Only that one port is punched — every other host-loopback service stays blocked. |

`<gateway>` is derived at runtime from `ip route get 1.1.1.1` (→ `172.28.0.1`
here). `net.py` must discover it per-launch, not hardcode (it differs across
machines).

DNS-forward address choice: using the **gateway** works; a dedicated link-local
(`169.254.0.53`) also works equally well. Either is fine — gateway chosen for the
gate because it is guaranteed routable from the guest.

## Curated read-only `/etc` set that proved sufficient

`/etc/ssl` (TLS — essential), `/etc/ca-certificates*`, `/etc/alternatives`,
`/etc/localtime`, plus the **synthesized** `passwd`, `group`, `resolv.conf`, and a
trimmed `nsswitch.conf` (`passwd: files` / `group: files` / `hosts: files dns` —
drops the host's `mdns4`/`libvirt`/`myhostname` modules, which would otherwise
pull extra NSS libs). `/etc/hosts` was **not** required for the probes but should
be added for completeness. Everything else under `/etc` absent by default; expand
additively as real tooling complains (default-deny).

## seccomp / caps posture

**No restrictive `--seccomp`, no dropped caps, no apparmor confinement** — bwrap's
default. The native `claude` (Bun single-file ELF,
`~/.local/share/claude/versions/2.1.158`) runs `--version` cleanly inside the
sandbox (exit 0). **No SIGPWR/GC crash recurred.** No special knob needed; keep
the default permissive seccomp posture. (Heavier claude exercise — an actual
session — is deferred to the run-path and MCP-bridge work; `--version` is the
throwaway smoke probe.)

## Identity / ownership

Constructed minimal `/etc/passwd`+`/etc/group` (single user, uid/gid 1000) bound
read-only + `--uid/--gid` give correct `whoami`/`$HOME` with **no sssd/NSS, no
subuid range**. A file written into the rw-bound cwd from inside is owned by
`$USER` on the host side too (ownership parity holds through the single-uid map).

## Host tooling note

`socat` is installed on the host, so the SSE-relay fallback (a socat/unix-socket
relay across the mount namespace) needs no extra setup if it is ever required.
The primary `pasta -T` path works, so that fallback is **not** currently used.
`socat` is a host concern, not something this wrapper installs or manages.

## Throwaway status

`spikes/bwrap_net_spike.sh` is a hand-written bwrap invocation, **not** package
code. It may be discarded once the facts above are encoded in `sandbox.py` /
`net.py`. It is retained as executable, reproducible evidence of the go decision.
