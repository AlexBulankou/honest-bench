"""Offline tests for run.finalize_cluster_node_count (hb#214 part 2) — no
cluster, no I/O.

Run with bare python3 (no pytest, so the auto-refresh GH-runner needs nothing
extra):  python3 -m harness.test_run_finalize_node_count
or directly:               python3 harness/test_run_finalize_node_count.py

These assert the finalize-time encode-then-merge stamp for the per-cluster SLO
rate triple:

  - a cell carrying thpt_under_{5s,1s}_per_cluster without
    thpt_cluster_node_count gets the node count stamped from run provenance
    (BENCH_NODE_COUNT explicitly set to a valid int >= 1);
  - a leg-emitted node_count always wins — the stamp never overwrites it;
  - when neither source has it (env unset OR invalid), finalize returns
    problem lines so main() refuses the write — the gap fails loud at finalize
    instead of silently pending at render/publish time;
  - build_provenance's silent default of 1 is deliberately NOT a stamp source
    (env unset != env "1"): silently stamping 1 onto a multi-node fire would
    fabricate the measurement size.
"""

from __future__ import annotations

# Make this file runnable BOTH as `python3 harness/test_x.py` and
# `python3 -m harness.test_x` by putting the repo root on sys.path before
# the absolute `from harness import ...` below (mirrors test_run_slo_sweep.py).
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import os

from harness import run


def _check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _cell(name="warmpool_cold_start", sla_metrics=None):
    return {"name": name, "sla_metrics": {} if sla_metrics is None else sla_metrics}


def _with_env(value, raw):
    """Run finalize_cluster_node_count with BENCH_NODE_COUNT set/unset; restore after."""
    saved = os.environ.get("BENCH_NODE_COUNT")
    try:
        if value is None:
            os.environ.pop("BENCH_NODE_COUNT", None)
        else:
            os.environ["BENCH_NODE_COUNT"] = value
        return run.finalize_cluster_node_count(raw)
    finally:
        if saved is None:
            os.environ.pop("BENCH_NODE_COUNT", None)
        else:
            os.environ["BENCH_NODE_COUNT"] = saved


def test_no_rate_cells_is_noop_even_when_env_unset():
    raw = [
        _cell(sla_metrics={"ttfe_p50_ms": 1200.0}),
        _cell(name="native_digest_cold", sla_metrics={}),
    ]
    problems = _with_env(None, raw)
    _check(problems == [], f"no per-cluster rates -> no problems, got {problems}")
    _check("thpt_cluster_node_count" not in raw[0]["sla_metrics"],
           "no-op must not stamp cells without rate keys")


def test_leg_emitted_node_count_wins_over_env():
    raw = [_cell(sla_metrics={
        "thpt_under_5s_per_cluster": 28.4,
        "thpt_under_1s_per_cluster": 9.8,
        "thpt_cluster_node_count": 40,
    })]
    problems = _with_env("7", raw)
    _check(problems == [], f"leg-emitted node_count -> no problems, got {problems}")
    _check(raw[0]["sla_metrics"]["thpt_cluster_node_count"] == 40,
           "stamp must never overwrite a leg-emitted node_count")


def test_stamps_from_valid_env():
    raw = [_cell(sla_metrics={
        "thpt_under_5s_per_cluster": 28.4,
        "thpt_under_1s_per_cluster": 9.8,
    })]
    problems = _with_env("40", raw)
    _check(problems == [], f"valid env -> no problems, got {problems}")
    got = raw[0]["sla_metrics"].get("thpt_cluster_node_count")
    _check(got == 40, f"expected stamped 40, got {got!r}")
    _check(isinstance(got, int) and not isinstance(got, bool),
           f"stamped value must be an int, got {type(got).__name__}")


def test_stamps_single_5s_only_rate_key():
    # The literal-basis shape (hb#174): 5s cell only, 1s honest-empty. The
    # measurement-size requirement applies identically.
    raw = [_cell(sla_metrics={"thpt_under_5s_per_cluster": 0.07})]
    problems = _with_env("2", raw)
    _check(problems == [], f"valid env -> no problems, got {problems}")
    _check(raw[0]["sla_metrics"]["thpt_cluster_node_count"] == 2,
           "5s-only cell must be stamped too")


def test_env_unset_refuses_with_problem_line():
    raw = [_cell(sla_metrics={"thpt_under_5s_per_cluster": 28.4})]
    problems = _with_env(None, raw)
    _check(len(problems) == 1, f"expected 1 problem, got {problems}")
    _check("BENCH_NODE_COUNT is unset" in problems[0],
           f"problem line must name the missing provenance source: {problems[0]}")
    _check("thpt_cluster_node_count" not in raw[0]["sla_metrics"],
           "refusal path must not stamp anything")


def test_env_invalid_values_refuse():
    for bad in ("abc", "0", "-3", "1.5", ""):
        raw = [_cell(sla_metrics={"thpt_under_1s_per_cluster": 9.8})]
        problems = _with_env(bad, raw)
        _check(len(problems) == 1,
               f"BENCH_NODE_COUNT={bad!r} -> expected 1 problem, got {problems}")
        _check("thpt_cluster_node_count" not in raw[0]["sla_metrics"],
               f"BENCH_NODE_COUNT={bad!r} must not stamp")


def test_node_count_without_rates_is_noop():
    # A bare node_count with no rate keys is odd but not this guard's concern
    # (the render never reads it without a rate; nothing to verify here).
    raw = [_cell(sla_metrics={"thpt_cluster_node_count": 40})]
    problems = _with_env(None, raw)
    _check(problems == [], f"node_count-without-rates -> no problems, got {problems}")


def test_non_dict_shapes_skipped():
    raw = [
        "junk",
        {"name": "x"},
        _cell(sla_metrics="junk"),
        _cell(sla_metrics={"thpt_under_5s_per_cluster": 1.0}),
    ]
    problems = _with_env("3", raw)
    _check(problems == [], f"malformed cells skipped, got {problems}")
    _check(raw[3]["sla_metrics"]["thpt_cluster_node_count"] == 3,
           "well-formed cell after malformed ones must still be stamped")


def test_multiple_gap_cells_each_get_a_problem_line():
    raw = [
        _cell(name="warmpool_cold_start",
              sla_metrics={"thpt_under_5s_per_cluster": 28.4}),
        _cell(name="suspend_resume",
              sla_metrics={"thpt_under_1s_per_cluster": 9.8}),
    ]
    problems = _with_env(None, raw)
    _check(len(problems) == 2, f"expected 2 problems, got {problems}")
    _check("warmpool_cold_start" in problems[0] and "suspend_resume" in problems[1],
           f"each problem line names its cell: {problems}")


def _all_tests():
    return [v for k, v in sorted(globals().items())
            if k.startswith("test_") and callable(v)]


def main() -> int:
    failures = 0
    for t in _all_tests():
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {t.__name__}: {e}")
    print(f"\n{len(_all_tests()) - failures}/{len(_all_tests())} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
