"""Offline tests for run.merge_slo_sweeps / carry_prior_cluster_triples — no
cluster, no I/O beyond self-managed tempfiles.

Run with bare python3 (no pytest, so the auto-refresh GH-runner needs nothing
extra):  python3 -m harness.test_run_slo_sweep
or directly:               python3 harness/test_run_slo_sweep.py

These assert the default-off ingestion wiring of the per-mode SLO cluster-rate
sweep (hb#132/#149) into the run loop:

  - merge_slo_sweeps: BENCH_SLO_SWEEP_<SCENARIO> -> nested record -> flatten ->
    slo_rate derivation -> merge into that scenario's sla_metrics. Fail-closed on
    non-sandbox product, unset env, unreadable/malformed file, underivable
    record. Sweep-derived triple overwrites a direct-emit triple (preferred
    producer). Partial per-bar fill merges only the landed bar + node_count.
  - carry_prior_cluster_triples: the scenario-level do-not-auto-decay carry.
    Fresh triple wins outright; a prior triple is carried all-together and only
    with node_count + >=1 rate (never a rate without its measurement size).
"""

from __future__ import annotations

# Make this file runnable BOTH as `python3 harness/test_x.py` and
# `python3 -m harness.test_x` by putting the repo root on sys.path before
# the absolute `from harness import ...` below (mirrors test_run_stepup.py).
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import json
import os
import pathlib
import tempfile

from harness import run


def _check(cond, msg):
    if not cond:
        raise AssertionError(msg)


# A minimal nested sweep record (the make_sweep_record shape maybe_stepup also
# reads): the 5s bar lands at ready 28.4, the 1s bar at 9.8, top rung overloads.
_NESTED = {
    "params": {"cluster_nodes": 40},
    "pareto": [
        {"offered_rate_per_s": 10, "ready_per_s": 9.8, "ttfe_p95_ms": 850.0},
        {"offered_rate_per_s": 30, "ready_per_s": 28.4, "ttfe_p95_ms": 3200.0},
        {"offered_rate_per_s": 100, "ready_per_s": 41.0, "ttfe_p95_ms": 12610.3},
    ],
}

_TRIPLE = {
    "thpt_under_5s_per_cluster": 28.4,
    "thpt_under_1s_per_cluster": 9.8,
    "thpt_cluster_node_count": 40,
}

_WARM = "warmpool_cold_start"


def _cell(name=_WARM, sla_metrics=None):
    return {"name": name, "sla_metrics": {} if sla_metrics is None else sla_metrics}


def _with_sweep(scenario, product, file_content, raw, *, path_value="<tempfile>"):
    """Run merge_slo_sweeps with one scenario's env armed; restore env after."""
    var = run.slo_sweep_env_var(scenario)
    saved = os.environ.get(var)
    tmp_path = None
    try:
        if path_value == "<tempfile>":
            fd, tmp_path = tempfile.mkstemp(suffix=".json")
            with os.fdopen(fd, "w") as fh:
                if isinstance(file_content, str):
                    fh.write(file_content)
                else:
                    json.dump(file_content, fh)
            os.environ[var] = tmp_path
        elif path_value is None:
            os.environ.pop(var, None)
        else:
            os.environ[var] = path_value
        run.merge_slo_sweeps(raw, product)
        return raw
    finally:
        if saved is None:
            os.environ.pop(var, None)
        else:
            os.environ[var] = saved
        if tmp_path is not None:
            pathlib.Path(tmp_path).unlink(missing_ok=True)


# --- merge_slo_sweeps ---

def test_env_unset_merges_nothing():
    raw = [_cell()]
    _with_sweep(_WARM, "sandbox", None, raw, path_value=None)
    _check(raw[0]["sla_metrics"] == {}, "unset env -> untouched")


def test_non_sandbox_product_fail_closed():
    raw = [_cell()]
    _with_sweep(_WARM, "substrate", _NESTED, raw)
    _check(raw[0]["sla_metrics"] == {}, "product=substrate -> untouched (fail-closed)")


def test_kata_product_merges_triple():
    # hb#149 / Path A kata half: a sandbox-kata run's warm-row sweep merges the
    # SAME triple into its scenario cell (render_matrix reads it via kata_results).
    # The one product-gate widening beyond gVisor — substrate stays fail-closed.
    raw = [_cell(sla_metrics={"ttfe_p95_ms": 640.0})]
    _with_sweep(_WARM, "sandbox-kata", _NESTED, raw)
    expected = dict(_TRIPLE)
    expected["ttfe_p95_ms"] = 640.0
    _check(raw[0]["sla_metrics"] == expected,
           f"kata sweep triple merged, got {raw[0]['sla_metrics']!r}")


def test_missing_file_merges_nothing():
    raw = [_cell()]
    _with_sweep(_WARM, "sandbox", None, raw, path_value="/nonexistent/sweep.json")
    _check(raw[0]["sla_metrics"] == {}, "unreadable path -> untouched")


def test_malformed_file_merges_nothing():
    raw = [_cell()]
    _with_sweep(_WARM, "sandbox", "{not valid json", raw)
    _check(raw[0]["sla_metrics"] == {}, "malformed JSON -> untouched")


def test_underivable_record_merges_nothing():
    # Rungs all overload both bars -> no compliant rung -> {} -> cell untouched,
    # never a fabricated 0.
    rec = {"params": {"cluster_nodes": 40},
           "pareto": [{"offered_rate_per_s": 100, "ready_per_s": 41.0,
                       "ttfe_p95_ms": 12610.3}]}
    raw = [_cell()]
    _with_sweep(_WARM, "sandbox", rec, raw)
    _check(raw[0]["sla_metrics"] == {}, "no compliant rung -> untouched (pend, not 0)")


def test_no_node_count_merges_nothing():
    rec = {"pareto": _NESTED["pareto"]}
    raw = [_cell()]
    _with_sweep(_WARM, "sandbox", rec, raw)
    _check(raw[0]["sla_metrics"] == {}, "no node_count -> untouched (whole-result pend)")


def test_armed_merges_triple_into_matching_cell():
    raw = [_cell(sla_metrics={"ttfe_p95_ms": 900.0}),
           _cell(name="native_digest_cold")]
    _with_sweep(_WARM, "sandbox", _NESTED, raw)
    expected = dict(_TRIPLE)
    expected["ttfe_p95_ms"] = 900.0
    _check(raw[0]["sla_metrics"] == expected,
           f"triple merged alongside existing keys, got {raw[0]['sla_metrics']!r}")
    _check(raw[1]["sla_metrics"] == {}, "other mode's cell untouched")


def test_sweep_overwrites_direct_emit_triple():
    direct = {"thpt_under_5s_per_cluster": 2.558, "thpt_under_1s_per_cluster": 0.0,
              "thpt_cluster_node_count": 40}
    raw = [_cell(sla_metrics=dict(direct))]
    _with_sweep(_WARM, "sandbox", _NESTED, raw)
    _check(raw[0]["sla_metrics"] == _TRIPLE,
           "sweep derivation (preferred producer) overwrites the direct-emit triple")


def test_partial_fill_merges_landed_bar_only():
    # Lowest rung clears 5s but not 1s: 5s half + node_count merge, no 1s key.
    rec = {"params": {"cluster_nodes": 40},
           "pareto": [{"offered_rate_per_s": 30, "ready_per_s": 28.4,
                       "ttfe_p95_ms": 3200.0}]}
    raw = [_cell()]
    _with_sweep(_WARM, "sandbox", rec, raw)
    _check(raw[0]["sla_metrics"] == {"thpt_under_5s_per_cluster": 28.4,
                                     "thpt_cluster_node_count": 40},
           f"only the landed bar merges, got {raw[0]['sla_metrics']!r}")


def test_scenario_absent_from_run_is_skipped():
    raw = [_cell(name="burst_create")]
    _with_sweep(_WARM, "sandbox", _NESTED, raw)  # must not raise
    _check(raw[0]["sla_metrics"] == {}, "unrelated cell untouched")


def test_non_dict_sla_metrics_skipped():
    raw = [{"name": _WARM, "sla_metrics": None}]
    _with_sweep(_WARM, "sandbox", _NESTED, raw)  # must not raise
    _check(raw[0]["sla_metrics"] is None, "non-dict sla_metrics left alone")


def test_env_var_naming():
    _check(run.slo_sweep_env_var(_WARM) == "BENCH_SLO_SWEEP_WARMPOOL_COLD_START",
           "env var is BENCH_SLO_SWEEP_<SCENARIO-UPPER>")


# --- hb#169 runtime-match gate ---
# The BENCH_SLO_SWEEP_* env namespace is shared across the gVisor (product
# `sandbox`) and Kata+microVM (product `sandbox-kata`) merges, so a stale env var
# left set across the OTHER product's run would cross-merge one runtime's rate
# into the other's matrix cell. The durable guard is a runtime stamp on the
# record (`params.runtime_class`, which the step-up producer already emits;
# `params.runtime` also accepted) checked against the merging run's product
# runtime (_matrix_runtime_for): tolerate-absent (back-compat), reject
# present-and-mismatched.

def _stamped(runtime, *, field="runtime_class"):
    """A copy of _NESTED with a runtime stamp in params under `field` (hb#169)."""
    return {"params": {"cluster_nodes": 40, field: runtime},
            "pareto": [dict(p) for p in _NESTED["pareto"]]}


def test_helper_reads_runtime_class():
    _check(run._sweep_record_runtime(_stamped("gvisor")) == "gvisor",
           "reads params.runtime_class")


def test_helper_prefers_runtime_over_runtime_class():
    rec = {"params": {"runtime": "kata-microvm", "runtime_class": "gvisor"}}
    _check(run._sweep_record_runtime(rec) == "kata-microvm",
           "params.runtime wins over params.runtime_class")


def test_helper_absent_stamp_is_empty():
    _check(run._sweep_record_runtime(_NESTED) == "", "no stamp -> ''")
    _check(run._sweep_record_runtime({"params": "junk"}) == "", "non-dict params -> ''")
    _check(run._sweep_record_runtime({}) == "", "no params -> ''")
    _check(run._sweep_record_runtime({"params": {"runtime": "  "}}) == "",
           "blank stamp -> ''")


def test_stamped_matching_runtime_merges_gvisor():
    raw = [_cell(sla_metrics={"ttfe_p95_ms": 900.0})]
    _with_sweep(_WARM, "sandbox", _stamped("gvisor"), raw)
    expected = dict(_TRIPLE)
    expected["ttfe_p95_ms"] = 900.0
    _check(raw[0]["sla_metrics"] == expected,
           f"gvisor stamp + sandbox run merges, got {raw[0]['sla_metrics']!r}")


def test_stamped_matching_runtime_merges_kata():
    raw = [_cell(sla_metrics={"ttfe_p95_ms": 640.0})]
    _with_sweep(_WARM, "sandbox-kata", _stamped("kata-microvm"), raw)
    expected = dict(_TRIPLE)
    expected["ttfe_p95_ms"] = 640.0
    _check(raw[0]["sla_metrics"] == expected,
           f"kata stamp + sandbox-kata run merges, got {raw[0]['sla_metrics']!r}")


def test_stamped_mismatch_skips_on_kata_run():
    # The core contamination case: a stale gVisor record env-pointed during a
    # sandbox-kata merge must NOT cross-merge into the Kata cell.
    raw = [_cell()]
    _with_sweep(_WARM, "sandbox-kata", _stamped("gvisor"), raw)
    _check(raw[0]["sla_metrics"] == {},
           "gvisor-stamped record skipped on a kata run (no cross-merge)")


def test_stamped_mismatch_skips_on_gvisor_run():
    raw = [_cell()]
    _with_sweep(_WARM, "sandbox", _stamped("kata-microvm"), raw)
    _check(raw[0]["sla_metrics"] == {},
           "kata-stamped record skipped on a gvisor run (no cross-merge)")


def test_unstamped_record_merges_backcompat():
    # A legacy/shakeout record with no stamp still merges (tolerate-absent).
    raw = [_cell()]
    _with_sweep(_WARM, "sandbox", _NESTED, raw)
    _check(raw[0]["sla_metrics"] == _TRIPLE,
           f"unstamped record merges (back-compat), got {raw[0]['sla_metrics']!r}")


def test_params_runtime_field_accepted():
    raw = [_cell()]
    _with_sweep(_WARM, "sandbox-kata", _stamped("kata-microvm", field="runtime"), raw)
    _check(raw[0]["sla_metrics"] == _TRIPLE,
           "params.runtime (hb#169 field name) accepted as the stamp")


def test_matrix_runtime_override_gates():
    # BENCH_MATRIX_RUNTIME overrides the product->runtime map for both directions.
    saved = os.environ.get("BENCH_MATRIX_RUNTIME")
    try:
        os.environ["BENCH_MATRIX_RUNTIME"] = "kata-microvm"
        raw = [_cell()]
        _with_sweep(_WARM, "sandbox", _stamped("kata-microvm"), raw)
        _check(raw[0]["sla_metrics"] == _TRIPLE,
               "override makes kata stamp match on the sandbox product")
        raw = [_cell()]
        _with_sweep(_WARM, "sandbox", _stamped("gvisor"), raw)
        _check(raw[0]["sla_metrics"] == {},
               "override makes gvisor stamp skip on the sandbox product")
    finally:
        if saved is None:
            os.environ.pop("BENCH_MATRIX_RUNTIME", None)
        else:
            os.environ["BENCH_MATRIX_RUNTIME"] = saved


# --- carry_prior_cluster_triples ---

def _prior(name=_WARM, sla_metrics=None):
    return {"name": name, "outcome": "PASS",
            "sla_metrics": {} if sla_metrics is None else sla_metrics}


def test_carry_full_triple_onto_bare_fresh_cell():
    raw = [_cell(sla_metrics={"ttfe_p50_ms": 400.0})]
    prior = [_prior(sla_metrics=dict(_TRIPLE, ttfe_p50_ms=380.0))]
    run.carry_prior_cluster_triples(raw, prior)
    m = raw[0]["sla_metrics"]
    _check(all(m.get(k) == _TRIPLE[k] for k in _TRIPLE),
           f"prior triple carried, got {m!r}")
    _check(m["ttfe_p50_ms"] == 400.0, "fresh non-triple keys untouched")


def test_fresh_triple_wins_over_prior():
    fresh_triple = {"thpt_under_5s_per_cluster": 33.0, "thpt_cluster_node_count": 80}
    raw = [_cell(sla_metrics=dict(fresh_triple))]
    prior = [_prior(sla_metrics=dict(_TRIPLE))]
    run.carry_prior_cluster_triples(raw, prior)
    _check(raw[0]["sla_metrics"] == fresh_triple,
           "any fresh triple key blocks the carry (never mix two fires)")


def test_partial_prior_triple_carries_as_is():
    # A sweep that only landed the 5s bar committed {5s, node_count}: carried as-is.
    part = {"thpt_under_5s_per_cluster": 28.4, "thpt_cluster_node_count": 40}
    raw = [_cell()]
    run.carry_prior_cluster_triples(raw, [_prior(sla_metrics=dict(part))])
    _check(raw[0]["sla_metrics"] == part, "partial (landed-bar) triple carried")


def test_node_count_alone_not_carried():
    raw = [_cell()]
    run.carry_prior_cluster_triples(
        raw, [_prior(sla_metrics={"thpt_cluster_node_count": 40})])
    _check(raw[0]["sla_metrics"] == {}, "bare node_count (no rate) never carried")


def test_rate_without_node_count_not_carried():
    raw = [_cell()]
    run.carry_prior_cluster_triples(
        raw, [_prior(sla_metrics={"thpt_under_5s_per_cluster": 28.4})])
    _check(raw[0]["sla_metrics"] == {},
           "rate without node_count never carried (render cannot caption X)")


def test_non_list_prior_is_noop():
    raw = [_cell()]
    run.carry_prior_cluster_triples(raw, None)
    run.carry_prior_cluster_triples(raw, {"name": _WARM})
    _check(raw[0]["sla_metrics"] == {}, "non-list prior -> no-op")


def test_prior_cell_missing_or_malformed_is_noop():
    raw = [_cell()]
    run.carry_prior_cluster_triples(raw, [_prior(name="burst_create",
                                                 sla_metrics=dict(_TRIPLE))])
    _check(raw[0]["sla_metrics"] == {}, "no prior cell for this name -> no-op")
    run.carry_prior_cluster_triples(raw, [{"name": _WARM, "sla_metrics": "junk"}])
    _check(raw[0]["sla_metrics"] == {}, "non-dict prior sla_metrics -> no-op")


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
