"""Offline tests for the pure warm-vs-cold speedup classifier -- no cluster, no clock.

Run with bare python3 (the auto-refresh GH-runner needs nothing extra):
    python3 -m harness.test_warm_vs_cold
or directly:
    python3 harness/test_warm_vs_cold.py

The load-bearing tests are:
  - happy path (warm p50 + single cold sample -> speedup = cold/warm, rounded);
  - rounding self-consistency (speedup re-derivable from the two published cells);
  - every honesty gate -> {} (semantic mismatch, runtime-class mismatch, empty warm,
    missing/non-positive cold, corrupt warm sample, falsy runtime_class) -- proves the
    classifier renders pending rather than fabricating an apples-to-oranges ratio.
"""

from __future__ import annotations

import os as _os
import sys as _sys

# Make both run-modes work: `python3 -m harness.test_warm_vs_cold` (repo root already
# on the path) AND `python3 harness/test_warm_vs_cold.py` (add the repo root so the
# `harness` package resolves). The path-insert is a no-op under the -m invocation.
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from harness import warm_vs_cold as wvc
from harness import metrics as m


def _check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _close(a, b, eps=1e-6):
    return abs(a - b) <= eps


# Canonical valid kwargs -- a single warm/cold pair both measured TTFI on gVisor.
_OK = dict(
    warm_semantic="ttfi",
    cold_semantic="ttfi",
    warm_runtime_class="gvisor",
    cold_runtime_class="gvisor",
)


# ----------------------------------------------------------------- happy path
def test_happy_path_basic():
    out = wvc.classify_warm_vs_cold([200.0, 400.0, 600.0], 3000.0, **_OK)
    _check(out["warm_p50_ms"] == 400.0, "p50 of 200/400/600 is 400")
    _check(out["cold_ms"] == 3000.0, "cold sample passes through rounded")
    _check(out["speedup"] == m.retention(400.0, 3000.0), "speedup = retention(warm, cold)")
    _check(_close(out["speedup"], 7.5), "3000/400 = 7.5x faster")
    _check(out["semantic"] == "ttfi", "shared semantic surfaced")
    _check(out["runtime_class"] == "gvisor", "shared runtime_class surfaced")
    _check(out["n_warm"] == 3, "n_warm = len(warm_samples)")


def test_happy_path_single_warm_sample():
    out = wvc.classify_warm_vs_cold([500.0], 2500.0, **_OK)
    _check(out["warm_p50_ms"] == 500.0, "p50 of a single sample is that sample")
    _check(_close(out["speedup"], 5.0), "2500/500 = 5.0x")
    _check(out["n_warm"] == 1, "n_warm=1")


def test_ttfe_mode_happy_path():
    ok = dict(_OK, warm_semantic="ttfe", cold_semantic="ttfe")
    out = wvc.classify_warm_vs_cold([300.0, 300.0], 1800.0, **ok)
    _check(out["semantic"] == "ttfe", "ttfe mode accepted")
    _check(_close(out["speedup"], 6.0), "1800/300 = 6.0x")


# --------------------------------------------------- rounding self-consistency
def test_speedup_is_self_consistent_with_published_cells():
    # Odd inputs so p50 interpolates and both legs round; the published speedup must
    # equal cold_ms / warm_p50_ms computed from the rounded cells, not the raw inputs.
    out = wvc.classify_warm_vs_cold([211.37, 433.91], 2999.96, **_OK)
    expected_p50 = round(m.percentile([211.37, 433.91], 50), 1)
    _check(out["warm_p50_ms"] == expected_p50, "warm_p50 rounded to 0.1")
    _check(out["cold_ms"] == round(2999.96, 1), "cold rounded to 0.1")
    _check(out["speedup"] == round(out["cold_ms"] / out["warm_p50_ms"], 3),
           "speedup re-derivable from the two published cells")


# ---------------------------------------------------------- semantic-parity gate
def test_semantic_mismatch_emits_nothing():
    bad = dict(_OK, warm_semantic="ttfi", cold_semantic="ttfe")
    _check(wvc.classify_warm_vs_cold([200.0], 3000.0, **bad) == {},
           "TTFI-warm vs TTFE-cold -> {}")


def test_unknown_semantic_emits_nothing():
    bad = dict(_OK, warm_semantic="bogus", cold_semantic="bogus")
    _check(wvc.classify_warm_vs_cold([200.0], 3000.0, **bad) == {},
           "semantic not in {ttfi,ttfe} -> {}")


# ------------------------------------------------------ runtime-class parity gate
def test_runtime_class_mismatch_emits_nothing():
    bad = dict(_OK, warm_runtime_class="gvisor", cold_runtime_class="runc")
    _check(wvc.classify_warm_vs_cold([200.0], 3000.0, **bad) == {},
           "gVisor-warm vs runc-cold -> {}")


def test_falsy_runtime_class_emits_nothing():
    bad = dict(_OK, warm_runtime_class="", cold_runtime_class="")
    _check(wvc.classify_warm_vs_cold([200.0], 3000.0, **bad) == {},
           "empty shared runtime_class -> {}")
    none_rc = dict(_OK, warm_runtime_class=None, cold_runtime_class=None)
    _check(wvc.classify_warm_vs_cold([200.0], 3000.0, **none_rc) == {},
           "None shared runtime_class -> {}")


# --------------------------------------------------------------- warm-leg gates
def test_empty_warm_emits_nothing():
    _check(wvc.classify_warm_vs_cold([], 3000.0, **_OK) == {}, "empty warm -> {}")


def test_corrupt_warm_sample_emits_nothing():
    _check(wvc.classify_warm_vs_cold([200.0, -5.0], 3000.0, **_OK) == {},
           "negative warm sample -> {}")
    _check(wvc.classify_warm_vs_cold([200.0, float("inf")], 3000.0, **_OK) == {},
           "non-finite warm sample -> {}")
    _check(wvc.classify_warm_vs_cold([200.0, None], 3000.0, **_OK) == {},
           "None warm sample -> {}")


# --------------------------------------------------------------- cold-leg gates
def test_missing_cold_emits_nothing():
    _check(wvc.classify_warm_vs_cold([200.0], None, **_OK) == {}, "None cold -> {}")


def test_nonpositive_cold_emits_nothing():
    _check(wvc.classify_warm_vs_cold([200.0], 0.0, **_OK) == {}, "cold==0 -> {}")
    _check(wvc.classify_warm_vs_cold([200.0], -1.0, **_OK) == {}, "cold<0 -> {}")


def test_nonfinite_cold_emits_nothing():
    _check(wvc.classify_warm_vs_cold([200.0], float("nan"), **_OK) == {}, "NaN cold -> {}")
    _check(wvc.classify_warm_vs_cold([200.0], float("inf"), **_OK) == {}, "inf cold -> {}")


# --------------------------------------------------- bool-is-not-a-number guard
def test_bool_samples_rejected():
    # Python bools are ints; a True/False slipping into a latency list is a defect.
    _check(wvc.classify_warm_vs_cold([True, 200.0], 3000.0, **_OK) == {},
           "bool warm sample -> {}")
    _check(wvc.classify_warm_vs_cold([200.0], True, **_OK) == {},
           "bool cold sample -> {}")


def _run_all():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"ok - {len(tests)} warm_vs_cold tests passed")


if __name__ == "__main__":
    _run_all()
