"""Offline tests for the TTFE exec-probe pure core (bare python3, no deps).

Run from the repo root:

    python3 -m harness.test_ttfe_probe

Covers only the pure, cluster-free surface of ``ttfe_probe`` —
``first_instruction``, ``classify_exec``, ``ttfe_ms``, and
``resolve_probe_result``. The single I/O wrapper ``probe_first_instruction``
is exercised live by the scenarios against a cluster; here we assert its
contract indirectly (it returns whatever ``resolve_probe_result`` does), which
this suite pins exhaustively.
"""


# Make this file runnable BOTH as `python3 harness/test_x.py` and
# `python3 -m harness.test_x` by putting the repo root on sys.path before
# the absolute `from harness import ...` below (mirrors test_warm_vs_cold.py).
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from harness import ttfe_probe as tp


_failures = []


def _check(name, cond):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}")
        _failures.append(name)


def _expect_raises(name, fn, exc=Exception):
    try:
        fn()
    except exc:
        print(f"  ok   {name}")
    except Exception as e:  # wrong exception type
        print(f"  FAIL {name} (raised {type(e).__name__}, expected {exc.__name__})")
        _failures.append(name)
    else:
        print(f"  FAIL {name} (did not raise)")
        _failures.append(name)


def test_first_instruction_shape():
    argv, token = tp.first_instruction()
    _check("first_instruction returns (list, str)",
           isinstance(argv, list) and isinstance(token, str))
    _check("first_instruction argv non-empty", len(argv) > 0)
    _check("first_instruction token non-empty", len(token) > 0)
    # The instruction must actually emit the token it is checked against,
    # else every live probe would record a false failure.
    _check("first_instruction argv emits the token",
           any(token in part for part in argv))


def test_first_instruction_returns_fresh_copy():
    argv1, _ = tp.first_instruction()
    argv1.append("MUTATED")
    argv2, _ = tp.first_instruction()
    _check("first_instruction hands out an independent argv copy",
           "MUTATED" not in argv2)


def test_classify_exec_token_present():
    _, token = tp.first_instruction()
    _check("classify_exec true on exact token", tp.classify_exec(token, token))
    _check("classify_exec true on token with surrounding framing",
           tp.classify_exec(f"\n{token}\r\n", token))


def test_classify_exec_token_absent():
    _, token = tp.first_instruction()
    _check("classify_exec false on empty stdout", tp.classify_exec("", token) is False)
    _check("classify_exec false on wrong stdout",
           tp.classify_exec("some other output", token) is False)


def test_classify_exec_non_string_stdout():
    _, token = tp.first_instruction()
    _check("classify_exec false on None stdout", tp.classify_exec(None, token) is False)
    _check("classify_exec false on bytes stdout",
           tp.classify_exec(token.encode(), token) is False)


def test_classify_exec_empty_expected_token():
    # An empty expected token must not vacuously match every stdout.
    _check("classify_exec false when expected_token empty",
           tp.classify_exec("anything", "") is False)


def test_ttfe_ms_basic():
    _check("ttfe_ms 1.5s -> 1500ms", tp.ttfe_ms(10.0, 11.5) == 1500.0)
    _check("ttfe_ms zero span -> 0ms", tp.ttfe_ms(42.0, 42.0) == 0.0)


def test_ttfe_ms_negative_span_raises():
    _expect_raises("ttfe_ms raises on negative span",
                   lambda: tp.ttfe_ms(11.0, 10.0), ValueError)


def test_resolve_success():
    _, token = tp.first_instruction()
    ttfe, ok = tp.resolve_probe_result(token, token, 100.0, 101.0)
    _check("resolve exec_ok true on token present", ok is True)
    _check("resolve ttfe_ms 1000 on success", ttfe == 1000.0)


def test_resolve_failure_drops_latency():
    _, token = tp.first_instruction()
    ttfe, ok = tp.resolve_probe_result("garbled", token, 100.0, 100.9)
    _check("resolve exec_ok false on wrong stdout", ok is False)
    _check("resolve ttfe_ms None on failed exec (dropped from histogram)",
           ttfe is None)


def test_resolve_failure_still_validates_clock():
    # A failed exec must NOT mask a clock bug — the span arithmetic still runs.
    _, token = tp.first_instruction()
    _expect_raises("resolve raises on negative span even when exec failed",
                   lambda: tp.resolve_probe_result("garbled", token, 11.0, 10.0),
                   ValueError)


def test_resolve_non_string_stdout_failure():
    _, token = tp.first_instruction()
    ttfe, ok = tp.resolve_probe_result(None, token, 5.0, 5.5)
    _check("resolve exec_ok false on None stdout", ok is False)
    _check("resolve ttfe_ms None on None stdout", ttfe is None)


def main():
    tests = [
        test_first_instruction_shape,
        test_first_instruction_returns_fresh_copy,
        test_classify_exec_token_present,
        test_classify_exec_token_absent,
        test_classify_exec_non_string_stdout,
        test_classify_exec_empty_expected_token,
        test_ttfe_ms_basic,
        test_ttfe_ms_negative_span_raises,
        test_resolve_success,
        test_resolve_failure_drops_latency,
        test_resolve_failure_still_validates_clock,
        test_resolve_non_string_stdout_failure,
    ]
    print(f"running {len(tests)} ttfe_probe test groups\n")
    for t in tests:
        print(t.__name__)
        t()
    print()
    if _failures:
        print(f"FAILED: {len(_failures)} check(s): {', '.join(_failures)}")
        raise SystemExit(1)
    print("all ttfe_probe checks passed")


if __name__ == "__main__":
    main()
