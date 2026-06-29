"""Offline tests for the nested->flat step-up adapter -- no cluster, no I/O.

Run with bare python3 (the auto-refresh GH-runner needs nothing extra):
    python3 -m harness.test_stepup_adapter
or directly:
    python3 harness/test_stepup_adapter.py

The load-bearing tests are the CONVERGENCE GUARDS: a nested sweep record built to the
producer's real shape, run through `stepup_nested_to_flat` and then through the closed
schema's `_coerce_stepup`, must round-trip the measured data faithfully:
  - the Pareto curve survives intact (rename `pareto` -> `pareto_points`, per-point keys
    already match), with optional ready/p50/p99 carried when present;
  - the verdict (all four locked values) passes through unchanged -- no vocab remap;
  - the characteristic rates lift from `saturation.*` to the top level;
  - the Little's-law scalars lift from `params.*`, with `cluster_nodes` -> `node_count`;
  - the SHAKEOUT edge: `cluster_nodes=None` (and `machine_type=None`) flow through and the
    schema OMITS `node_count`/`machine_type` -- a missing scalar is never fabricated, and a
    `None` never trips the schema's optional-field type checks.
"""

from __future__ import annotations

from . import results_schema as rs
from . import stepup_adapter as a


def _check(cond, msg):
    if not cond:
        raise AssertionError(msg)


# A realistic nested sweep record matching the producer's make_sweep_record output:
# three measured steps, flat-through-sweep, with the Phase-1 scalars populated.
def _nested(**over):
    rec = {
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
        "steps": [],  # not read by the adapter (pareto carries the curve)
        "saturation": {
            "verdict": "flat-through-sweep",
            "north_star_breach_rate": None,
            "saturation_rate": None,
            "max_flat_rate": 300,
            "measured_steps": 3,
            "unmeasured_steps": 0,
            "north_star_ms": 500.0,
            "collapse_ms": 2000.0,
        },
        "pareto": [
            {"offered_rate_per_s": 10, "ready_per_s": 10.0,
             "ttfe_p95_ms": 220.0, "ttfe_p50_ms": 140.0, "ttfe_p99_ms": 380.0},
            {"offered_rate_per_s": 100, "ready_per_s": 98.0,
             "ttfe_p95_ms": 410.0, "ttfe_p50_ms": 210.0, "ttfe_p99_ms": 470.0},
            {"offered_rate_per_s": 300, "ready_per_s": 295.0,
             "ttfe_p95_ms": 480.0, "ttfe_p50_ms": 260.0, "ttfe_p99_ms": 495.0},
        ],
    }
    rec.update(over)
    return rec


# ------------------------------------------------------------------ pure flatten

def test_flatten_renames_and_lifts():
    flat = a.stepup_nested_to_flat(_nested())
    _check(flat["pareto_points"] is not None and len(flat["pareto_points"]) == 3,
           "pareto -> pareto_points (3 points)")
    _check(flat["verdict"] == "flat-through-sweep", "verdict lifted from saturation")
    _check(flat["max_flat_rate"] == 300, "max_flat_rate lifted from saturation")
    _check(flat["sld_s"] == 20.0 and flat["wpr"] == 0.75, "sld_s/wpr lifted from params")
    _check(flat["node_count"] == 150, "params.cluster_nodes -> node_count")
    _check(flat["machine_type"] == "e2-standard-16", "machine_type lifted from params")


def test_flatten_non_dict_is_empty():
    _check(a.stepup_nested_to_flat(None) == {}, "non-dict -> {} (schema rejects -> no block)")
    _check(a.stepup_nested_to_flat([]) == {}, "list -> {}")


def test_flatten_missing_subobjects_no_crash():
    flat = a.stepup_nested_to_flat({"pareto": []})
    _check(flat["verdict"] is None, "absent saturation -> verdict None")
    _check(flat["node_count"] is None, "absent params -> node_count None")


# ---------------------------------------------------- convergence: adapter -> schema

def test_convergence_full_record_round_trips():
    out = rs._coerce_stepup(a.stepup_nested_to_flat(_nested()))
    _check(out is not None, "full nested record coerces (not dropped)")
    _check(out["verdict"] == "flat-through-sweep", "verdict survives the round trip")
    _check(len(out["pareto_points"]) == 3, "all 3 pareto points survive")
    p0 = out["pareto_points"][0]
    _check(p0["offered_rate_per_s"] == 10 and p0["ttfe_p95_ms"] == 220.0,
           "required per-point fields survive")
    _check(p0["ready_per_s"] == 10.0 and p0["ttfe_p50_ms"] == 140.0
           and p0["ttfe_p99_ms"] == 380.0, "optional per-point fields carried when present")
    _check(out["max_flat_rate"] == 300, "max_flat_rate lifted to top level")
    _check(out["sld_s"] == 20.0 and out["wpr"] == 0.75, "Little's-law scalars survive")
    _check(out["node_count"] == 150, "node_count (renamed) survives")
    _check(out["machine_type"] == "e2-standard-16", "machine_type survives")
    # None characteristic rates must be OMITTED, not carried as null.
    _check("north_star_breach_rate" not in out, "None breach rate omitted, not null")
    _check("saturation_rate" not in out, "None saturation rate omitted, not null")


def test_convergence_shakeout_cluster_nodes_null_omitted():
    # The shakeout fire leaves cluster_nodes (and machine_type) None -- the adapter passes
    # the None through and the schema OMITS node_count/machine_type rather than fabricating.
    rec = _nested()
    rec["params"]["cluster_nodes"] = None
    rec["params"]["machine_type"] = None
    out = rs._coerce_stepup(a.stepup_nested_to_flat(rec))
    _check(out is not None, "shakeout record still coerces (the curve is real)")
    _check("node_count" not in out, "None cluster_nodes -> node_count omitted (never fabricated)")
    _check("machine_type" not in out, "None machine_type -> omitted")
    _check(len(out["pareto_points"]) == 3, "the measured curve is unaffected by null scalars")


def test_convergence_all_four_verdicts_pass_through():
    for v in rs.STEPUP_VERDICT_ENUM:
        rec = _nested()
        rec["saturation"]["verdict"] = v
        out = rs._coerce_stepup(a.stepup_nested_to_flat(rec))
        _check(out is not None and out["verdict"] == v,
               f"verdict {v!r} passes through unchanged (no remap)")


def test_convergence_unknown_verdict_drops_block():
    rec = _nested()
    rec["saturation"]["verdict"] = "totally-bogus"
    out = rs._coerce_stepup(a.stepup_nested_to_flat(rec))
    _check(out is None, "unknown verdict -> whole stepup block dropped (no partial lie)")


def test_convergence_empty_pareto_drops_block():
    rec = _nested()
    rec["pareto"] = []
    out = rs._coerce_stepup(a.stepup_nested_to_flat(rec))
    _check(out is None, "empty pareto -> block dropped (measured=False, not a fabricated 0)")


def _run_all():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"ok - {len(tests)} stepup_adapter tests passed")


if __name__ == "__main__":
    _run_all()
