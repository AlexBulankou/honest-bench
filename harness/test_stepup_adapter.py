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

# Make this file runnable BOTH as `python3 harness/test_x.py` and
# `python3 -m harness.test_x` by putting the repo root on sys.path before
# the absolute `from harness import ...` below (mirrors test_warm_vs_cold.py).
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from harness import results_schema as rs
from harness import stepup_adapter as a


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


# ----------------------------------- controller-startup LOWER-BOUND proxy (#3975)

def _nested_controller(over=None):
    # The #3975 producer shape: an EMPTY true-TTFE pareto (no upstream true-TTFE stamp) + a
    # SEPARATE nested controller_startup block carrying the controller-stamped lower-bound proxy.
    cs = {
        "lower_bound": True,
        "caveat": "LOWER BOUND -- excludes claim-admission->first-reconcile queueing lag (#3975)",
        "saturation": {"verdict": "flat-through-sweep", "max_flat_rate": 100},
        "pareto": [
            {"offered_rate_per_s": 10, "controller_ready_per_s": 10.0,
             "controller_startup_p95_ms": 180.0, "controller_startup_p50_ms": 90.0,
             "controller_startup_p99_ms": 360.0},
            {"offered_rate_per_s": 100, "controller_ready_per_s": 98.0,
             "controller_startup_p95_ms": 240.0, "controller_startup_p50_ms": 120.0,
             "controller_startup_p99_ms": 470.0},
        ],
    }
    rec = _nested()
    rec["pareto"] = []  # true-TTFE honestly empty while #3975 is open
    rec["saturation"]["verdict"] = "no-measured-steps"
    rec["controller_startup"] = cs
    if over:
        rec["controller_startup"].update(over)
    return rec


def test_flatten_lifts_controller_startup_block():
    flat = a.stepup_nested_to_flat(_nested_controller())
    cs = flat["controller_startup"]
    _check(cs["lower_bound"] is True, "lower_bound carried verbatim")
    _check(len(cs["pareto_points"]) == 2, "controller pareto -> pareto_points (2 points)")
    _check(cs["pareto_points"][1]["controller_startup_p95_ms"] == 240.0, "per-point proxy p95 lifted")
    _check(cs["verdict"] == "no-measured-steps" or cs["verdict"] == "flat-through-sweep",
           "saturation.verdict lifted to controller_startup.verdict")
    _check("caveat" not in cs, "free-text caveat DELIBERATELY NOT lifted (render-owned, public-safe)")


def test_convergence_controller_startup_round_trips():
    out = rs._coerce_stepup(a.stepup_nested_to_flat(_nested_controller()))
    _check(out is not None, "#3975 gap record (empty TTFE + proxy) coerces, not dropped")
    _check("pareto_points" not in out, "empty true-TTFE pareto omitted, not emitted as []")
    _check(out["verdict"] == "no-measured-steps", "honest no-measured-steps top-level verdict")
    cs = out["controller_startup"]
    _check(cs["lower_bound"] is True, "proxy lower_bound survives the round trip")
    _check(len(cs["pareto_points"]) == 2, "both proxy points survive")
    _check(cs["pareto_points"][0]["controller_startup_p99_ms"] == 360.0, "optional p99 carried")


def test_convergence_controller_startup_caveat_never_round_trips():
    # End-to-end public-safety: even with the producer's free-text caveat present in the nested
    # record, it never reaches the coerced public output (adapter drops it before the coercer).
    rec = _nested_controller()
    leak = rec["controller_startup"]["caveat"]
    out = rs._coerce_stepup(a.stepup_nested_to_flat(rec))
    _check("caveat" not in out["controller_startup"], "caveat absent from coerced proxy block")
    _check(leak not in repr(out), "no caveat free-text anywhere in coerced output")


def test_flatten_non_dict_controller_startup_omitted():
    rec = _nested_controller()
    rec["controller_startup"] = "not-a-dict"
    flat = a.stepup_nested_to_flat(rec)
    _check("controller_startup" not in flat, "non-dict controller_startup omitted from flat record")


# ----------------------------- true-TTFE webhook read-back count (hb#5396)

def test_flatten_lifts_webhook_stamped_claims_count():
    rec = _nested()
    rec["true_ttfe_webhook_stamped_claims"] = 7
    flat = a.stepup_nested_to_flat(rec)
    _check(flat["true_ttfe_webhook_stamped_claims"] == 7,
           "webhook-stamped-claims read-back count carried verbatim to flat")


def test_flatten_webhook_stamped_claims_absent_when_unstamped():
    flat = a.stepup_nested_to_flat(_nested())
    _check("true_ttfe_webhook_stamped_claims" not in flat,
           "unstamped record -> key absent (slo_rate then fails closed)")


# ----------------------------------- literal-TTFE UPPER-BOUND leg (hb#174)

def _nested_literal(over=None):
    # The hb#174 producer shape: an EMPTY true-TTFE pareto (#3975 stamper unlanded) + a
    # SEPARATE nested literal_ttfe block carrying the exec-probe UPPER-bound leg with the
    # two NAMESPACED measured-rate candidates. Rung rates are fractional on purpose —
    # kata rungs are 0.5/1.5 per_s and must be first-class through the whole bridge.
    lt = {
        "upper_bound": True,
        "includes_exec_setup_overhead": True,
        "caveat": "UPPER BOUND -- every sample includes exec websocket-setup overhead (hb#174)",
        "saturation": {"verdict": "degrading", "max_flat_rate": 1},
        "pareto": [
            {"offered_rate_per_s": 0.5, "literal_warm_p95_ms": 850.0,
             "literal_warm_p50_ms": 400.0, "literal_warm_p99_ms": 1200.0,
             "literal_cold_p95_ms": 4100.0,
             "acq_fulfilled_per_s": 0.49, "controller_completed_per_s": 0.47,
             "literal_every_n": 3, "literal_warm_n_exec_ok": 12},
            {"offered_rate_per_s": 1.5, "literal_warm_p95_ms": 3200.0,
             "acq_fulfilled_per_s": 1.42, "controller_completed_per_s": 1.38},
        ],
    }
    rec = _nested()
    rec["pareto"] = []  # true-TTFE honestly empty while #3975 is open
    rec["saturation"]["verdict"] = "no-measured-steps"
    rec["literal_ttfe"] = lt
    if over:
        rec["literal_ttfe"].update(over)
    return rec


def test_flatten_lifts_literal_ttfe_block():
    flat = a.stepup_nested_to_flat(_nested_literal())
    lt = flat["literal_ttfe"]
    _check(lt["upper_bound"] is True, "upper_bound carried verbatim")
    _check(lt["includes_exec_setup_overhead"] is True,
           "includes_exec_setup_overhead carried verbatim")
    _check(len(lt["pareto_points"]) == 2, "literal pareto -> pareto_points (2 points)")
    _check(lt["pareto_points"][0]["literal_warm_p95_ms"] == 850.0,
           "namespaced per-point warm p95 lifted (never aliased to ttfe_p95_ms)")
    _check(lt["verdict"] == "degrading", "saturation.verdict lifted to literal_ttfe.verdict")
    _check("caveat" not in lt, "free-text caveat DELIBERATELY NOT lifted (render-owned, public-safe)")


def test_convergence_literal_ttfe_round_trips():
    out = rs._coerce_stepup(a.stepup_nested_to_flat(_nested_literal()))
    _check(out is not None, "hb#174 gap record (empty TTFE + literal leg) coerces, not dropped")
    _check("pareto_points" not in out, "empty true-TTFE pareto omitted, not emitted as []")
    _check(out["verdict"] == "no-measured-steps", "honest no-measured-steps top-level verdict")
    lt = out["literal_ttfe"]
    _check(lt["upper_bound"] is True, "upper_bound survives the round trip")
    _check(lt["includes_exec_setup_overhead"] is True,
           "includes_exec_setup_overhead survives the round trip")
    _check(lt["verdict"] == "degrading", "literal verdict survives")
    _check(len(lt["pareto_points"]) == 2, "both literal points survive")
    p0 = lt["pareto_points"][0]
    _check(p0["offered_rate_per_s"] == 0.5,
           "FRACTIONAL rung rate survives (Real, not int-only like true-TTFE points)")
    _check(p0["acq_fulfilled_per_s"] == 0.49 and p0["controller_completed_per_s"] == 0.47,
           "both NAMESPACED measured-rate candidates carried")
    _check(p0["literal_every_n"] == 3 and p0["literal_warm_n_exec_ok"] == 12,
           "sampling-disclosure ints carried")
    _check(p0["literal_cold_p95_ms"] == 4100.0, "optional cold percentile carried")


def test_convergence_literal_ttfe_caveat_never_round_trips():
    # End-to-end public-safety, same contract as the controller leg: the producer's
    # free-text caveat never reaches the coerced public output.
    rec = _nested_literal()
    leak = rec["literal_ttfe"]["caveat"]
    out = rs._coerce_stepup(a.stepup_nested_to_flat(rec))
    _check("caveat" not in out["literal_ttfe"], "caveat absent from coerced literal block")
    _check(leak not in repr(out), "no caveat free-text anywhere in coerced output")


def test_convergence_literal_upper_bound_flag_gates_block():
    # An unflagged literal block must NOT survive coercion — an unmarked latency basis
    # could fabricate SLO compliance downstream. upper_bound must be exactly True.
    for flag in (False, None, "true", 1):
        rec = _nested_literal({"upper_bound": flag})
        out = rs._coerce_stepup(a.stepup_nested_to_flat(rec))
        _check(out is None, f"upper_bound={flag!r} -> whole stepup block dropped "
               "(empty true-TTFE + gated literal = nothing measured)")


def test_flatten_non_dict_literal_ttfe_omitted():
    rec = _nested_literal()
    rec["literal_ttfe"] = "not-a-dict"
    flat = a.stepup_nested_to_flat(rec)
    _check("literal_ttfe" not in flat, "non-dict literal_ttfe omitted from flat record")


def test_convergence_partial_measured_subset_only():
    # An UNMEASURED step (CL2/scrape gap) is already excluded from `pareto` by the
    # producer's assemble_pareto, so the adapter round-trips the MEASURED SUBSET only --
    # a partial shakeout yields a partial-but-honest curve, never a fabricated point for
    # the gapped step. Here 2 of 3 steps measured -> 2 pareto points -> degrading verdict.
    rec = _nested()
    rec["pareto"] = rec["pareto"][:2]
    rec["saturation"]["verdict"] = "degrading"
    rec["saturation"]["north_star_breach_rate"] = 100
    rec["saturation"]["measured_steps"] = 2
    rec["saturation"]["unmeasured_steps"] = 1
    out = rs._coerce_stepup(a.stepup_nested_to_flat(rec))
    _check(out is not None, "partial-measured record still coerces (the measured curve is real)")
    _check(len(out["pareto_points"]) == 2, "only the measured subset survives -- no gapped-step point")
    _check(out["verdict"] == "degrading", "verdict reflects the measured subset")
    _check(out["north_star_breach_rate"] == 100, "populated breach rate lifts to top level")


# ----------------------------------------------------- cost enrichment (item-4 axis)

def test_enrich_stamps_cost_on_every_measured_point():
    # node_count=150, machine_type=e2-standard-16 ($0.5363/node-hr list price), so each
    # point's cost = (150 * 0.5363) / (ready_per_s * 3600) * 1000, computed from the
    # point's OWN measured ready_per_s -- the cost axis is per-throughput, not per-record.
    flat = a.enrich_pareto_cost(a.stepup_nested_to_flat(_nested()))
    for pt in flat["pareto_points"]:
        want = (150 * 0.5363) / (pt["ready_per_s"] * 3600.0) * 1000.0
        _check(abs(pt["cost_usd_per_1k_ready"] - want) < 1e-9,
               f"per-point cost mismatch: {pt['cost_usd_per_1k_ready']} != {want}")
    # The cheapest cost is at the HIGHEST throughput (more ready/hr amortizes the same
    # cluster spend) -- a sanity check the per-point math is wired to the right rate.
    costs = [pt["cost_usd_per_1k_ready"] for pt in flat["pareto_points"]]
    _check(costs[0] > costs[-1], "cost should fall as measured throughput rises")


def test_enrich_explicit_rate_overrides_list_price():
    flat = a.enrich_pareto_cost(a.stepup_nested_to_flat(_nested()), usd_per_node_hour=0.30)
    pt = flat["pareto_points"][0]
    want = (150 * 0.30) / (pt["ready_per_s"] * 3600.0) * 1000.0
    _check(abs(pt["cost_usd_per_1k_ready"] - want) < 1e-9,
           f"explicit rate did not override list price: {pt['cost_usd_per_1k_ready']} != {want}")


def test_enrich_shakeout_null_node_count_omits_cost():
    # Shakeout fire: cluster_nodes None -> node_count None -> no honest cost to compute,
    # so the key stays ABSENT (never a fabricated 0), exactly like the schema omits the
    # scalar itself. The measured curve is otherwise unaffected.
    rec = _nested()
    rec["params"]["cluster_nodes"] = None
    flat = a.enrich_pareto_cost(a.stepup_nested_to_flat(rec))
    for pt in flat["pareto_points"]:
        _check("cost_usd_per_1k_ready" not in pt,
               "None node_count must omit cost, not fabricate one")


def test_enrich_unknown_machine_no_rate_omits_cost():
    rec = _nested()
    rec["params"]["machine_type"] = "totally-unknown-x99"
    flat = a.enrich_pareto_cost(a.stepup_nested_to_flat(rec))
    for pt in flat["pareto_points"]:
        _check("cost_usd_per_1k_ready" not in pt,
               "unknown machine_type with no explicit rate must omit cost (no guess)")


def test_enrich_nonpositive_ready_omits_that_point_only():
    # A step that produced no ready sandboxes has no honest unit cost -- that point's
    # cost is omitted while the genuinely-measured points still get one. Per-point honesty.
    rec = _nested()
    rec["pareto"][1]["ready_per_s"] = 0.0
    flat = a.enrich_pareto_cost(a.stepup_nested_to_flat(rec))
    _check("cost_usd_per_1k_ready" in flat["pareto_points"][0], "measured point keeps cost")
    _check("cost_usd_per_1k_ready" not in flat["pareto_points"][1],
           "zero-ready point omits cost (nothing to amortize over)")
    _check("cost_usd_per_1k_ready" in flat["pareto_points"][2], "other measured point keeps cost")


def test_enrich_non_list_pareto_is_noop():
    _check(a.enrich_pareto_cost({"pareto_points": None}) == {"pareto_points": None},
           "non-list pareto_points -> no-op (nothing-measured path)")
    _check(a.enrich_pareto_cost({}) == {}, "absent pareto_points -> no-op")


def test_enrich_then_schema_round_trips_cost():
    # End-to-end: nested -> flat -> enrich -> _coerce_stepup carries the cost through the
    # closed schema, so the public cost axis renders a real number, not pending.
    out = rs._coerce_stepup(a.enrich_pareto_cost(a.stepup_nested_to_flat(_nested())))
    _check(out is not None, "enriched record still coerces")
    for pt in out["pareto_points"]:
        _check(pt.get("cost_usd_per_1k_ready") is not None and pt["cost_usd_per_1k_ready"] > 0,
               "cost survives the closed-schema round trip as a positive number")


def _run_all():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"ok - {len(tests)} stepup_adapter tests passed")


if __name__ == "__main__":
    _run_all()
