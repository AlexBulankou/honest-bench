"""Cluster-free tests for netpol_probe's pure classifier + command builders.

Dependency-free: `python3 test_netpol_probe.py` (exit 0 = pass). Everything that
decides the data-plane verdict is pure and pinned here — the env gate
(`dataplane_probe_enabled`), the connect/listener argv builders, the stdout
sentinel classifier (`classify_connection`), and the two-half verdict mapping
(`classify_dataplane`). The three I/O surfaces (`exec_connection`,
`start_listener`, `stop_listener`) touch a cluster and are exercised live by the
NetworkPolicy cells on a4s1's first armed fire; here we pin the contract they
feed/consume so a flaky exec can never manufacture an `enforced` badge or a false
FAIL — only a clean two-sided confirmation (-> enforced) or a clean breach
(-> FAIL) moves the badge.
"""

import os

try:  # cwd == scenarios/ (dependency-free `python3 test_netpol_probe.py`)
    import netpol_probe as np
except ModuleNotFoundError:  # repo-root pytest: scenarios/ is a package, not on sys.path
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import netpol_probe as np


_PROBE_ENV = "BENCH_NETPOL_DATAPLANE_PROBE"


def _with_env(key, value, fn):
    """Run fn() with os.environ[key]=value (or unset when value is None), restore."""
    prior = os.environ.get(key)
    if value is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = value
    try:
        return fn()
    finally:
        if prior is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = prior


# ---- dataplane_probe_enabled: the default-off flag gate ----

def test_probe_disabled_when_unset():
    assert _with_env(_PROBE_ENV, None, np.dataplane_probe_enabled) is False


def test_probe_disabled_on_blank_and_falsey():
    for v in ("", "   ", "0", "false", "no", "off", "nope"):
        assert _with_env(_PROBE_ENV, v, np.dataplane_probe_enabled) is False, v


def test_probe_enabled_on_truthy_case_insensitive():
    for v in ("1", "true", "TRUE", "Yes", "on", " on ", "ON"):
        assert _with_env(_PROBE_ENV, v, np.dataplane_probe_enabled) is True, v


# ---- connection_command: the connect-and-print-sentinel argv ----

def test_connection_command_shape():
    argv = np.connection_command("10.0.0.5", 8080, timeout_s=3)
    assert isinstance(argv, list)
    assert argv[0] == "sh" and argv[1] == "-c"
    script = argv[2]
    # host, port, and timeout all land in the connect line.
    assert "10.0.0.5" in script
    assert "8080" in script
    assert "-w 3" in script
    # both sentinels are emitted so the caller classifies from stdout, not exit code.
    assert np._CONN_OK in script
    assert np._CONN_REFUSED in script


def test_connection_command_coerces_numeric_args():
    # A float/str port or timeout must render as an int (no "8080.0", no "-w 3.0").
    argv = np.connection_command("h", 8080.0, timeout_s=3.0)
    script = argv[2]
    assert "8080.0" not in script and " 8080 " in script
    assert "-w 3 " in script and "-w 3.0" not in script


def test_connection_command_ok_on_success_refused_on_failure():
    # The OK sentinel is on the success branch, REFUSED on the else — so a blocked
    # (silently-dropped) connect prints REFUSED, never OK.
    script = np.connection_command("h", 1, timeout_s=1)[2]
    ok_at = script.find(np._CONN_OK)
    refused_at = script.find(np._CONN_REFUSED)
    assert ok_at != -1 and refused_at != -1
    assert ok_at < refused_at  # then-branch (OK) precedes else-branch (REFUSED)


# ---- listener_command: the repeat-survivable TCP listener argv ----

def test_listener_command_shape():
    argv = np.listener_command(8080)
    assert argv[0] == "sh" and argv[1] == "-c"
    script = argv[2]
    assert "nc -l -p 8080" in script
    # loops so it accepts more than one probe connection within a fire.
    assert "while" in script


def test_listener_command_coerces_port():
    assert "nc -l -p 8080" in np.listener_command(8080.0)[2]


# ---- classify_connection: stdout sentinel -> connected/refused/inconclusive ----

def test_classify_connection_ok():
    assert np.classify_connection(np._CONN_OK) is True
    assert np.classify_connection(f"\n{np._CONN_OK}\r\n") is True  # framing tolerated


def test_classify_connection_refused():
    assert np.classify_connection(np._CONN_REFUSED) is False


def test_classify_connection_neither_is_none():
    assert np.classify_connection("") is None
    assert np.classify_connection("garbled output") is None


def test_classify_connection_non_string_is_none():
    assert np.classify_connection(None) is None
    assert np.classify_connection(np._CONN_OK.encode()) is None  # bytes, not str


def test_classify_connection_connected_wins_if_both():
    # A real connection is the stronger signal if both sentinels somehow appear.
    assert np.classify_connection(f"{np._CONN_REFUSED} {np._CONN_OK}") is True


# ---- classify_dataplane: the (deny, control) -> (verdict, badge_scope) mapping ----

def test_classify_breach_when_deny_path_flowed():
    # deny_blocked False => policy-blocked traffic flowed => breach, no badge.
    assert np.classify_dataplane(False, True) == ("breach", None)
    assert np.classify_dataplane(False, None) == ("breach", None)
    # breach beats over-block: a flowed deny path is the loudest failure.
    assert np.classify_dataplane(False, False) == ("breach", None)


def test_classify_over_block_when_control_path_blocked():
    # control_allowed False (and deny not False) => over-restrictive => FAIL, no badge.
    assert np.classify_dataplane(True, False) == ("over-block", None)
    assert np.classify_dataplane(None, False) == ("over-block", None)


def test_classify_enforced_only_on_clean_two_sided_proof():
    assert np.classify_dataplane(True, True) == ("enforced", "enforced")


def test_classify_inconclusive_on_any_none_half():
    # Any remaining None (probe could not run cleanly) degrades to inconclusive,
    # badge_scope None => caller keeps the static control-plane badge.
    assert np.classify_dataplane(True, None) == ("inconclusive", None)
    assert np.classify_dataplane(None, True) == ("inconclusive", None)
    assert np.classify_dataplane(None, None) == ("inconclusive", None)


def test_classify_badge_scope_is_enforced_only_for_enforced():
    # The only verdict that returns a non-None badge_scope is enforced.
    verdicts = [
        np.classify_dataplane(False, True),
        np.classify_dataplane(True, False),
        np.classify_dataplane(True, None),
        np.classify_dataplane(None, None),
    ]
    assert all(scope is None for _, scope in verdicts)
    assert np.classify_dataplane(True, True)[1] == "enforced"


def _run_all():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok: {fn.__name__}")
    print(f"test_netpol_probe: all {len(fns)} test groups passed")


if __name__ == "__main__":
    _run_all()
