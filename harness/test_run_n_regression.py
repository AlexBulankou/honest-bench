"""Offline tests for run.check_n_regression — no cluster, no I/O.

Run with bare python3 (no pytest, so the auto-refresh GH-runner needs nothing
extra):  python3 -m harness.test_run_n_regression
or directly:               python3 harness/test_run_n_regression.py

These assert the #198 property: a refresh whose fresh rows carry a lower
sample size than the committed measured rows of the same name is detected
(the caller then refuses the wholesale write unless BENCH_ALLOW_N_REGRESSION
is set). Prior `pending` placeholders never gate — they have no graduated n.
"""

from __future__ import annotations

# Make this file runnable BOTH as `python3 harness/test_x.py` and
# `python3 -m harness.test_x` by putting the repo root on sys.path before
# the absolute `from harness import ...` below (mirrors test_run_merge.py).
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from harness.run import check_n_regression


def _check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def test_regression_detected():
    # The hb#198 shape: graduated warm n=30 / cold n=30 cells, bare re-fire
    # produces n=5 / n=1 — both must be flagged.
    prior = [
        {"name": "warmpool_cold_start", "outcome": "PASS", "n": 30},
        {"name": "native_digest_cold", "outcome": "PASS", "n": 30},
        {"name": "burst_create", "outcome": "PASS", "n": 10},
    ]
    raw = [
        {"name": "warmpool_cold_start", "outcome": "PASS", "n": 5},
        {"name": "native_digest_cold", "outcome": "PASS", "n": 1},
        {"name": "burst_create", "outcome": "PASS", "n": 10},
    ]
    lines = check_n_regression(raw, prior)
    _check(len(lines) == 2, f"expected 2 regressions, got {lines!r}")
    _check(lines[0] == "warmpool_cold_start: fresh n=5 < committed n=30",
           f"unexpected line: {lines[0]!r}")
    _check(lines[1] == "native_digest_cold: fresh n=1 < committed n=30",
           f"unexpected line: {lines[1]!r}")


def test_equal_and_higher_n_clean():
    prior = [
        {"name": "warmpool_cold_start", "outcome": "PASS", "n": 30},
        {"name": "native_digest_cold", "outcome": "PASS", "n": 30},
    ]
    raw = [
        {"name": "warmpool_cold_start", "outcome": "PASS", "n": 30},
        {"name": "native_digest_cold", "outcome": "PASS", "n": 40},
    ]
    _check(check_n_regression(raw, prior) == [],
           "equal/higher n must not be flagged")


def test_prior_pending_never_gates():
    # A pending placeholder has no graduated n — even if it carries a
    # nonzero n field, it must not gate a measured refresh.
    prior = [{"name": "suspend_resume", "outcome": "pending", "n": 30}]
    raw = [{"name": "suspend_resume", "outcome": "PASS", "n": 1}]
    _check(check_n_regression(raw, prior) == [],
           "prior pending row must never gate")


def test_missing_prior_row_ignored():
    prior = [{"name": "burst_create", "outcome": "PASS", "n": 10}]
    raw = [{"name": "brand_new_cell", "outcome": "PASS", "n": 1}]
    _check(check_n_regression(raw, prior) == [],
           "fresh row with no committed counterpart must not gate")


def test_missing_or_none_n_ignored():
    prior = [
        {"name": "gvisor_canary", "outcome": "PASS", "n": None},
        {"name": "isolation", "outcome": "PASS"},
        {"name": "warmpool_cold_start", "outcome": "PASS", "n": 30},
    ]
    raw = [
        {"name": "gvisor_canary", "outcome": "PASS", "n": 1},
        {"name": "isolation", "outcome": "PASS", "n": 1},
        {"name": "warmpool_cold_start", "outcome": "PASS", "n": None},
    ]
    _check(check_n_regression(raw, prior) == [],
           "None/absent n on either side must not gate")


def test_bool_n_excluded():
    # bool is an int subclass — a stray True/False n must not be compared.
    prior = [{"name": "x", "outcome": "PASS", "n": 30}]
    raw = [{"name": "x", "outcome": "PASS", "n": False}]
    _check(check_n_regression(raw, prior) == [],
           "bool n must be excluded from comparison")


def test_malformed_inputs_tolerated():
    # _read_prior_scenarios is best-effort: it can hand back [] or rows of
    # unexpected shape. The guard must degrade to no-op, never raise.
    _check(check_n_regression([{"name": "x", "n": 1}], []) == [],
           "empty prior must be a no-op")
    _check(check_n_regression([{"name": "x", "n": 1}], None) == [],
           "non-list prior must be a no-op")
    prior = ["not-a-dict", {"name": 42, "outcome": "PASS", "n": 30},
             {"outcome": "PASS", "n": 30}]
    raw = ["not-a-dict", {"name": "x", "outcome": "PASS", "n": 1}]
    _check(check_n_regression(raw, prior) == [],
           "malformed rows on either side must not gate or raise")


def main() -> int:
    tests = [
        test_regression_detected,
        test_equal_and_higher_n_clean,
        test_prior_pending_never_gates,
        test_missing_prior_row_ignored,
        test_missing_or_none_n_ignored,
        test_bool_n_excluded,
        test_malformed_inputs_tolerated,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
    if failed:
        print(f"{failed}/{len(tests)} FAILED")
        return 1
    print(f"all {len(tests)} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
