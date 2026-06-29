"""Offline tests for run.maybe_stepup / carry_prior_stepup — no cluster, no I/O
beyond a self-managed tempfile.

Run with bare python3 (no pytest, so the auto-refresh GH-runner needs nothing
extra):  python3 -m harness.test_run_stepup
or directly:               python3 harness/test_run_stepup.py

These assert the default-off, dual-gated INGESTION wiring of the step-up
throughput-saturation producer into the run loop (#3960). Unlike scale_proof
(which runs IN-PROCESS), the step-up sweep is produced out-of-process by an
INTERNAL CL2 orchestrator that writes a scrubbed nested file; maybe_stepup READS
that file (path in BENCH_STEPUP_RESULT), flattens it via stepup_adapter, and
enriches the cost axis. So the gate here is: the file path must be set AND
product=="sandbox", fail-closed otherwise; an unreadable/malformed/non-dict file
collapses to None so no stepup key is emitted (table absent, not a partial lie).
"""

from __future__ import annotations

import json
import os
import pathlib
import tempfile

from . import run


def _check(cond, msg):
    if not cond:
        raise AssertionError(msg)


# A minimal-but-realistic nested producer record (the make_sweep_record shape):
# one measured Pareto point with a ready-rate + the cluster shape so cost
# enrichment can stamp a real cost_usd_per_1k_ready.
_NESTED = {
    "method": "stepup-backfill",
    "params": {
        "sld_s": 20.0,
        "wpr": 0.75,
        "runtime_class": "gvisor",
        "north_star_p95_ms": 500.0,
        "collapse_p95_ms": 2000.0,
        "machine_type": "e2-standard-16",
        "cluster_nodes": 150,
    },
    "steps": [],
    "saturation": {
        "verdict": "flat-through-sweep",
        "north_star_breach_rate": 0.0,
        "saturation_rate": None,
        "max_flat_rate": 100,
    },
    "pareto": [
        {"offered_rate_per_s": 100, "ready_per_s": 95.0,
         "ttfe_p95_ms": 420.0, "ttfe_p50_ms": 210.0, "ttfe_p99_ms": 480.0},
    ],
    "controller_startup": {
        "lower_bound": True,
        "caveat": "controller-stamped lower bound; excludes admission->reconcile lag",
        "saturation": {"verdict": "flat-through-sweep"},
        "pareto": [
            {"offered_rate_per_s": 100, "ready_per_s": 95.0, "ttfe_p95_ms": 120.0},
        ],
    },
}


def _with(path_value, product, file_content):
    """Run maybe_stepup under a given env/product, with an optional temp result file.

    `path_value` controls BENCH_STEPUP_RESULT:
      - "<tempfile>"  -> write `file_content` to a tempfile and point the env at it
      - None          -> unset the env var
      - any other str -> set the env var to that literal (e.g. a nonexistent path)
    Returns the maybe_stepup result. Restores os.environ + removes any tempfile.
    """
    saved = os.environ.get("BENCH_STEPUP_RESULT")
    tmp_path = None
    try:
        if path_value == "<tempfile>":
            fd, tmp_path = tempfile.mkstemp(suffix=".json")
            with os.fdopen(fd, "w") as fh:
                if isinstance(file_content, str):
                    fh.write(file_content)
                else:
                    json.dump(file_content, fh)
            os.environ["BENCH_STEPUP_RESULT"] = tmp_path
        elif path_value is None:
            os.environ.pop("BENCH_STEPUP_RESULT", None)
        else:
            os.environ["BENCH_STEPUP_RESULT"] = path_value
        return run.maybe_stepup(product)
    finally:
        if saved is None:
            os.environ.pop("BENCH_STEPUP_RESULT", None)
        else:
            os.environ["BENCH_STEPUP_RESULT"] = saved
        if tmp_path is not None:
            pathlib.Path(tmp_path).unlink(missing_ok=True)


def test_no_path_sandbox_returns_none():
    _check(_with(None, "sandbox", None) is None, "path unset -> None")


def test_blank_path_returns_none():
    _check(_with("   ", "sandbox", None) is None, "blank/whitespace path -> None")


def test_non_sandbox_product_fail_closed():
    # Even with a valid file, a non-sandbox product is fail-closed (no render contract).
    result = _with("<tempfile>", "substrate", _NESTED)
    _check(result is None, f"product=substrate -> None (fail-closed), got {result!r}")


def test_missing_file_returns_none():
    result = _with("/nonexistent/stepup-result.json", "sandbox", None)
    _check(result is None, f"unreadable path -> None, got {result!r}")


def test_malformed_file_returns_none():
    result = _with("<tempfile>", "sandbox", "{not valid json")
    _check(result is None, f"malformed JSON -> None, got {result!r}")


def test_non_dict_record_returns_none():
    # Valid JSON but a list, not a dict -> flatten yields {} -> None.
    result = _with("<tempfile>", "sandbox", [1, 2, 3])
    _check(result is None, f"non-dict record -> None, got {result!r}")


def test_armed_sandbox_reads_flattens_enriches():
    result = _with("<tempfile>", "sandbox", _NESTED)
    _check(isinstance(result, dict) and result, f"armed -> flat dict, got {result!r}")
    # flatten: pareto -> pareto_points, saturation.verdict -> verdict, params lifted
    _check(result["verdict"] == "flat-through-sweep", f"verdict lifted, got {result.get('verdict')!r}")
    _check(result["node_count"] == 150, f"cluster_nodes -> node_count, got {result.get('node_count')!r}")
    _check(result["machine_type"] == "e2-standard-16", "machine_type lifted")
    _check(result["sld_s"] == 20.0 and result["wpr"] == 0.75, "Little's-law scalars lifted")
    pts = result["pareto_points"]
    _check(isinstance(pts, list) and len(pts) == 1, "one pareto point carried")
    # cost enrichment: a measured ready_per_s + known cluster shape -> a real cost
    cost = pts[0].get("cost_usd_per_1k_ready")
    _check(isinstance(cost, (int, float)) and cost > 0,
           f"cost enriched on the measured point, got {cost!r}")
    # controller-startup lower-bound proxy carried; caveat NOT lifted (render-owned)
    cs = result["controller_startup"]
    _check(cs["lower_bound"] is True, "controller lower_bound carried")
    _check(cs["verdict"] == "flat-through-sweep", "controller verdict lifted")
    _check("caveat" not in cs, "free-text caveat must NOT be lifted into the flat record")


def test_armed_controller_only_record():
    # True-TTFE pareto empty (the #3975 gap-open case) but the controller proxy is
    # present -> the flat record still carries the controller block; the closed
    # emitter (_coerce_stepup, not under test here) decides emission.
    rec = dict(_NESTED)
    rec["pareto"] = []
    result = _with("<tempfile>", "sandbox", rec)
    _check(isinstance(result, dict), f"controller-only -> flat dict, got {result!r}")
    _check(result["pareto_points"] == [], "empty true-TTFE pareto preserved")
    _check(result["controller_startup"]["lower_bound"] is True, "controller proxy carried")


# --- _stepup_usd_per_node_hour: optional explicit price override ---

def _with_price(value):
    saved = os.environ.get("BENCH_STEPUP_USD_PER_NODE_HOUR")
    try:
        if value is None:
            os.environ.pop("BENCH_STEPUP_USD_PER_NODE_HOUR", None)
        else:
            os.environ["BENCH_STEPUP_USD_PER_NODE_HOUR"] = value
        return run._stepup_usd_per_node_hour()
    finally:
        if saved is None:
            os.environ.pop("BENCH_STEPUP_USD_PER_NODE_HOUR", None)
        else:
            os.environ["BENCH_STEPUP_USD_PER_NODE_HOUR"] = saved


def test_price_unset_is_none():
    _check(_with_price(None) is None, "unset price -> None (list-price fallback)")


def test_price_blank_is_none():
    _check(_with_price("  ") is None, "blank price -> None")


def test_price_valid_parses():
    _check(_with_price("0.5363") == 0.5363, "valid positive float parsed")


def test_price_non_numeric_is_none():
    _check(_with_price("abc") is None, "non-numeric price -> None (no raise)")


def test_price_non_positive_is_none():
    _check(_with_price("0") is None, "zero price -> None")
    _check(_with_price("-1.5") is None, "negative price -> None")


# --- carry_prior_stepup (#3960): persist across the daily refresh ---

_GEN_AT = "2026-06-29T12:00:00Z"
_FRESH = {"pareto_points": [{"offered_rate_per_s": 100, "ttfe_p95_ms": 420.0}],
          "verdict": "flat-through-sweep"}
_PRIOR = {"pareto_points": [{"offered_rate_per_s": 300, "ttfe_p95_ms": 1800.0}],
          "verdict": "saturated", "measured_at": "2026-06-28T03:46:01Z"}


def test_carry_fresh_wins_and_is_stamped():
    out = run.carry_prior_stepup(dict(_FRESH), _PRIOR, generated_at=_GEN_AT)
    _check(out["verdict"] == "flat-through-sweep", "fresh wins")
    _check(out["measured_at"] == _GEN_AT,
           f"fresh stamps measured_at=generated_at, got {out.get('measured_at')!r}")
    _check("measured_at" not in _FRESH, "must not mutate the input fresh dict")


def test_carry_fresh_preexisting_measured_at_respected():
    fresh = dict(_FRESH)
    fresh["measured_at"] = "2025-01-01T00:00:00Z"
    out = run.carry_prior_stepup(fresh, _PRIOR, generated_at=_GEN_AT)
    _check(out["measured_at"] == "2025-01-01T00:00:00Z",
           "a fresh dict that already carries measured_at keeps it (setdefault)")


def test_carry_no_fresh_carries_prior_unchanged():
    out = run.carry_prior_stepup(None, _PRIOR, generated_at=_GEN_AT)
    _check(out == _PRIOR, f"no fresh -> carry prior verbatim, got {out!r}")
    _check(out["measured_at"] == "2026-06-28T03:46:01Z",
           "carried block keeps its ORIGINAL measured_at, not generated_at")


def test_carry_empty_fresh_carries_prior():
    out = run.carry_prior_stepup({}, _PRIOR, generated_at=_GEN_AT)
    _check(out == _PRIOR, f"empty fresh ({{}}) -> carry prior, got {out!r}")


def test_carry_no_fresh_no_prior_is_none():
    out = run.carry_prior_stepup(None, None, generated_at=_GEN_AT)
    _check(out is None, f"no fresh + no prior -> None (table absent), got {out!r}")


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
