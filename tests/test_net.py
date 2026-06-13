"""Tests for the pasta network plumbing (pure argv assembly).

  * ``sse_port_from_env``: reads ``CLAUDE_CODE_SSE_PORT``, rejecting unset/invalid,
  * ``wrap_argv``: the fixed pasta preamble, and one ``-T`` per distinct forwarded
    port -- in first-seen order, with ``None`` entries and duplicates dropped.
"""

from agentbox import net

GW = "10.0.2.2"
_PREAMBLE = [
    "pasta", "--config-net", "-f", "-q", "--no-map-gw", "--dns-forward", GW,
]


# --- sse_port_from_env --------------------------------------------------------

def test_sse_port_from_env_reads_value():
    assert net.sse_port_from_env({"CLAUDE_CODE_SSE_PORT": "54321"}) == 54321


def test_sse_port_from_env_absent_or_invalid():
    assert net.sse_port_from_env({}) is None
    assert net.sse_port_from_env({"CLAUDE_CODE_SSE_PORT": ""}) is None
    assert net.sse_port_from_env({"CLAUDE_CODE_SSE_PORT": "nope"}) is None
    assert net.sse_port_from_env({"CLAUDE_CODE_SSE_PORT": "0"}) is None
    assert net.sse_port_from_env({"CLAUDE_CODE_SSE_PORT": "70000"}) is None


# --- wrap_argv ----------------------------------------------------------------

def test_wrap_argv_no_ports_emits_no_forward():
    argv = net.wrap_argv(["bwrap", "true"], gateway=GW)
    assert argv == _PREAMBLE + ["--", "bwrap", "true"]


def test_wrap_argv_single_port():
    argv = net.wrap_argv(["bwrap", "true"], gateway=GW, ports=[4321])
    assert argv == _PREAMBLE + ["-T", "4321", "--", "bwrap", "true"]


def test_wrap_argv_one_T_per_distinct_port_in_order():
    argv = net.wrap_argv(["X"], gateway=GW, ports=[4321, 5050, 6060])
    assert argv == _PREAMBLE + [
        "-T", "4321", "-T", "5050", "-T", "6060", "--", "X",
    ]


def test_wrap_argv_dedupes_and_drops_none():
    # Duplicates collapse to a single -T; None entries (e.g. an unset SSE port
    # mixed with discovered MCP ports) are skipped; first-seen order is kept.
    argv = net.wrap_argv(["X"], gateway=GW, ports=[None, 4321, 5050, 4321, None])
    forwards = [argv[i + 1] for i, tok in enumerate(argv) if tok == "-T"]
    assert forwards == ["4321", "5050"]
    assert argv[-2:] == ["--", "X"]
