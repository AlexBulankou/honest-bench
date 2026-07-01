"""Offline tests for the pure TTFE metrics core -- no cluster, no I/O, no clock.

Run with bare python3 (the auto-refresh GH-runner needs nothing extra):
    python3 -m harness.test_metrics
or directly:
    python3 harness/test_metrics.py

The load-bearing tests are:
  - throughput doc-validation (200@<5s in 50s on 1 node == 4 sb/sec/node, the
    spec warm-pool value) -- proves the throughput DEFINITION matches the doc;
  - honest-zero throughput (no sample beats 1s -> 0.0, not blank);
  - exec_success_rate timeout-drag (a never-executed sandbox lowers the rate);
  - the schema-convergence test (ttfe_sla_metrics output survives
    results_schema._coerce_sla_metrics with no key dropped) -- proves EMIT and
    the closed-schema guard converge on the locked keys.
"""

from __future__ import annotations

# Make this file runnable BOTH as `python3 harness/test_x.py` and
# `python3 -m harness.test_x` by putting the repo root on sys.path before
# the absolute `from harness import ...` below (mirrors test_warm_vs_cold.py).
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from harness import metrics as m
from harness import results_schema as rs


def _check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _close(a, b, eps=1e-6):
    return abs(a - b) <= eps


# ---------------------------------------------------------------- percentile

def test_percentile_single():
    _check(m.percentile([42.0], 50) == 42.0, "single-sample p50")
    _check(m.percentile([42.0], 95) == 42.0, "single-sample p95")


def test_percentile_interpolation():
    # numpy-default linear interpolation: p50 of [1,2,3,4] -> rank 1.5 -> 2.5
    _check(_close(m.percentile([1, 2, 3, 4], 50), 2.5), "p50 interpolated")
    _check(_close(m.percentile([1, 2, 3, 4], 0), 1.0), "p0 == min")
    _check(_close(m.percentile([1, 2, 3, 4], 100), 4.0), "p100 == max")


def test_percentile_p95_typical():
    # 100 samples 1..100 -> p95 rank = 0.95*99 = 94.05 -> between vals[94]=95 and vals[95]=96
    vals = list(range(1, 101))
    _check(_close(m.percentile(vals, 95), 95.05), "p95 of 1..100")


def test_percentile_empty_raises():
    raised = False
    try:
        m.percentile([], 50)
    except ValueError:
        raised = True
    _check(raised, "percentile of empty must raise")


# ---------------------------------------------------------------- throughput

def test_throughput_doc_warmpool_5s():
    # 200 sandboxes all under 5s, 50s window, 1 node -> 200/50/1 = 4.0 (doc value)
    samples = [500.0] * 200
    _check(_close(m.throughput_per_node(samples, m.THRESHOLD_5S_MS, 50.0, 1), 4.0),
           "warm-pool thpt@<5s must be 4.0 (doc)")


def test_throughput_doc_warmpool_1s():
    # warm-pool p95 0.9s -> all 200 under 1s -> 4.0 (doc value)
    samples = [600.0] * 200
    _check(_close(m.throughput_per_node(samples, m.THRESHOLD_1S_MS, 50.0, 1), 4.0),
           "warm-pool thpt@<1s must be 4.0 (doc)")


def test_throughput_honest_zero_when_none_beat_threshold():
    # unique-image cold p50 1.2s -> nothing beats 1s -> honest 0.0, not blank
    samples = [1200.0] * 200
    _check(m.throughput_per_node(samples, m.THRESHOLD_1S_MS, 50.0, 1) == 0.0,
           "no-qualifier thpt must be honest 0.0")


def test_throughput_none_samples_excluded():
    # None == never reached first-execution; never counts toward numerator
    samples = [500.0, None, 500.0, None]
    _check(_close(m.throughput_per_node(samples, m.THRESHOLD_5S_MS, 2.0, 1), 1.0),
           "2 qualifying / 2s / 1 node == 1.0; Nones excluded")


def test_throughput_per_node_division():
    samples = [500.0] * 8
    _check(_close(m.throughput_per_node(samples, m.THRESHOLD_5S_MS, 4.0, 2), 1.0),
           "8 / 4s / 2 nodes == 1.0")


def test_throughput_invalid_window_node():
    for bad in (lambda: m.throughput_per_node([1.0], 5000, 0, 1),
                lambda: m.throughput_per_node([1.0], 5000, 1, 0)):
        raised = False
        try:
            bad()
        except ValueError:
            raised = True
        _check(raised, "invalid window/node_count must raise")


# ---------------------------------------------------------------- exec success

def test_exec_success_rate_basic():
    _check(m.exec_success_rate([True, True, True, True]) == 1.0, "all-ok == 1.0")
    _check(m.exec_success_rate([True, False, True, False]) == 0.5, "half == 0.5")


def test_exec_success_rate_timeout_drag():
    # doc resume row: 1277/1376 succeeded -> 0.9281 (displays as 92.8%)
    oks = [True] * 1277 + [False] * 99
    _check(m.exec_success_rate(oks) == round(1277 / 1376, 4), "resume exec rate (doc 92.8%)")
    _check(0.927 < m.exec_success_rate(oks) < 0.929, "resume exec rate ~0.928")


def test_exec_success_rate_empty_raises():
    raised = False
    try:
        m.exec_success_rate([])
    except ValueError:
        raised = True
    _check(raised, "empty exec set must raise")


# ---------------------------------------------------------------- density

def test_density_locked_denominator():
    # doc 1.88/vCPU reading: max-concurrent / per-node allocatable sandbox vCPU
    _check(_close(m.density_per_vcpu(15, 8.0), 1.88), "15 / 8 allocatable == 1.88")


def test_density_invalid_denominator_raises():
    raised = False
    try:
        m.density_per_vcpu(15, 0)
    except ValueError:
        raised = True
    _check(raised, "zero allocatable vCPU must raise")


# ---------------------------------------------------------------- retention

def test_retention_flat():
    _check(m.retention(1.88, 1.85) == round(1.85 / 1.88, 3), "doc density retention")
    _check(m.retention(4.0, 4.0) == 1.0, "perfectly flat == 1.0")


# ---------------------------------------------------------------- assembler

def test_ttfe_sla_metrics_happy_path():
    samples = [600.0] * 200
    oks = [True] * 200
    out = m.ttfe_sla_metrics(samples, oks, 50.0, 1,
                             max_concurrent_sandboxes=15,
                             allocatable_sandbox_vcpu_per_node=8.0)
    _check(_close(out["thpt_under_5s_per_node"], 4.0), "assembler thpt5")
    _check(_close(out["thpt_under_1s_per_node"], 4.0), "assembler thpt1")
    _check(out["exec_success_rate"] == 1.0, "assembler exec rate")
    _check(_close(out["ttfe_p50_ms"], 600.0), "assembler p50")
    _check(_close(out["ttfe_p95_ms"], 600.0), "assembler p95")
    _check(_close(out["density_per_vcpu"], 1.88), "assembler density")


def test_ttfe_sla_metrics_no_execution_omits_percentiles():
    # nothing reached first-execution -> no percentiles, honest-zero throughput,
    # exec rate still reported (and 0.0 here -- every attempt timed out)
    samples = [None] * 10
    oks = [False] * 10
    out = m.ttfe_sla_metrics(samples, oks, 50.0, 1)
    _check("ttfe_p50_ms" not in out, "p50 omitted when no samples")
    _check("ttfe_p95_ms" not in out, "p95 omitted when no samples")
    _check(out["thpt_under_5s_per_node"] == 0.0, "honest-zero thpt5")
    _check(out["thpt_under_1s_per_node"] == 0.0, "honest-zero thpt1")
    _check(out["exec_success_rate"] == 0.0, "exec rate 0.0 all-timeout")


def test_ttfe_sla_metrics_omits_density_when_absent():
    out = m.ttfe_sla_metrics([600.0] * 5, [True] * 5, 5.0, 1)
    _check("density_per_vcpu" not in out, "density omitted when inputs absent")


def test_ttfe_sla_metrics_survives_results_schema_coerce():
    # The convergence test: every emit key must survive the closed-schema guard.
    samples = [600.0] * 100
    oks = [True] * 99 + [False]
    out = m.ttfe_sla_metrics(samples, oks, 25.0, 1,
                             max_concurrent_sandboxes=15,
                             allocatable_sandbox_vcpu_per_node=8.0)
    coerced = rs._coerce_sla_metrics(out)
    _check(set(coerced.keys()) == set(out.keys()),
           f"keys dropped by schema: {set(out.keys()) - set(coerced.keys())}")
    for k in out:
        _check(_close(coerced[k], float(out[k])), f"value mutated for {k}")


# --------------------------------------------- bind-vs-exec decomposition (inch #1)

def test_ttfe_sla_metrics_emits_bind_percentiles_when_samples_present():
    # inch #1: bind_ms_samples supplied -> bind_p50_ms/bind_p95_ms alongside the TTFE pair.
    samples = [1396.0] * 30
    oks = [True] * 30
    binds = [400.0] * 30
    out = m.ttfe_sla_metrics(samples, oks, 50.0, 1, bind_ms_samples=binds)
    _check(_close(out["bind_p50_ms"], 400.0), "bind p50 emitted")
    _check(_close(out["bind_p95_ms"], 400.0), "bind p95 emitted")
    _check(_close(out["ttfe_p50_ms"], 1396.0), "ttfe p50 still emitted")


def test_ttfe_sla_metrics_omits_bind_when_samples_absent():
    # default (no bind_ms_samples arg) -> no bind keys; page stays pre-decomposition.
    out = m.ttfe_sla_metrics([600.0] * 5, [True] * 5, 5.0, 1)
    _check("bind_p50_ms" not in out, "bind p50 omitted by default")
    _check("bind_p95_ms" not in out, "bind p95 omitted by default")


def test_ttfe_sla_metrics_omits_bind_when_all_none():
    # a run where no claim bound -> every bind sample is None -> no bind distribution.
    out = m.ttfe_sla_metrics([600.0] * 3, [True] * 3, 5.0, 1,
                             bind_ms_samples=[None, None, None])
    _check("bind_p50_ms" not in out, "bind p50 omitted when all None")
    _check("bind_p95_ms" not in out, "bind p95 omitted when all None")


def test_ttfe_sla_metrics_bind_survives_results_schema_coerce():
    # convergence: the new bind keys must survive the closed-schema emitter guard.
    out = m.ttfe_sla_metrics([1396.0] * 30, [True] * 30, 50.0, 1,
                             bind_ms_samples=[400.0] * 30)
    coerced = rs._coerce_sla_metrics(out)
    _check("bind_p50_ms" in coerced, "bind p50 survives coerce")
    _check("bind_p95_ms" in coerced, "bind p95 survives coerce")


def test_ttfe_sla_metrics_emits_exec_percentiles_when_samples_present():
    # inch #1: exec_ms_samples supplied -> exec_p50_ms/exec_p95_ms emitted. These
    # are GENUINELY-MEASURED per-claim (ttfe_ms - bind_ms), never subtracted
    # percentiles. Deliberately use exec values that do NOT equal ttfe_p50 -
    # bind_p50 (1396-400=996) to prove the emit percentiles the supplied samples,
    # not an arithmetic split.
    samples = [1396.0] * 30
    oks = [True] * 30
    binds = [400.0] * 30
    execs = [1000.0] * 30  # != 1396 - 400 == 996; measured, not subtracted
    out = m.ttfe_sla_metrics(samples, oks, 50.0, 1,
                             bind_ms_samples=binds, exec_ms_samples=execs)
    _check(_close(out["exec_p50_ms"], 1000.0), "exec p50 emitted (measured)")
    _check(_close(out["exec_p95_ms"], 1000.0), "exec p95 emitted (measured)")
    _check(_close(out["bind_p50_ms"], 400.0), "bind p50 still emitted")
    _check(_close(out["ttfe_p50_ms"], 1396.0), "ttfe p50 still emitted")


def test_ttfe_sla_metrics_omits_exec_when_samples_absent():
    # default (no exec_ms_samples arg) -> no exec keys; page stays pre-decomposition.
    out = m.ttfe_sla_metrics([600.0] * 5, [True] * 5, 5.0, 1)
    _check("exec_p50_ms" not in out, "exec p50 omitted by default")
    _check("exec_p95_ms" not in out, "exec p95 omitted by default")


def test_ttfe_sla_metrics_omits_exec_when_all_none():
    # a run where no claim produced a paired exec sample -> no exec distribution.
    out = m.ttfe_sla_metrics([600.0] * 3, [True] * 3, 5.0, 1,
                             exec_ms_samples=[None, None, None])
    _check("exec_p50_ms" not in out, "exec p50 omitted when all None")
    _check("exec_p95_ms" not in out, "exec p95 omitted when all None")


def test_ttfe_sla_metrics_exec_survives_results_schema_coerce():
    # convergence: the new exec keys must survive the closed-schema emitter guard.
    out = m.ttfe_sla_metrics([1396.0] * 30, [True] * 30, 50.0, 1,
                             bind_ms_samples=[400.0] * 30,
                             exec_ms_samples=[1000.0] * 30)
    coerced = rs._coerce_sla_metrics(out)
    _check("exec_p50_ms" in coerced, "exec p50 survives coerce")
    _check("exec_p95_ms" in coerced, "exec p95 survives coerce")


# --------------------------------------------- single-sample (cold / resume)

def test_single_sample_ttfe_point_happy():
    # one activation that executed -> p50==p95==the single sample, n=1, full credit
    out = m.single_sample_ttfe_point(3500.0, True)
    _check(out["exec_success_rate"] == 1.0, "single-sample exec rate 1.0")
    _check(out["n"] == 1, "single-sample n==1")
    _check(_close(out["ttfe_p50_ms"], 3500.0), "single-sample p50==sample")
    _check(_close(out["ttfe_p95_ms"], 3500.0), "single-sample p95==sample")
    _check("thpt_under_5s_per_node" not in out, "single-sample omits throughput")
    _check("density_per_vcpu" not in out, "single-sample omits density")


def test_single_sample_ttfe_point_failed_exec():
    # a failed one-shot exec has no honest first-instruction latency -> no
    # percentiles, exec_success_rate 0.0, n=1 (honest about both the failure
    # and the absent latency)
    out = m.single_sample_ttfe_point(None, False)
    _check(out["exec_success_rate"] == 0.0, "failed single-sample exec rate 0.0")
    _check(out["n"] == 1, "failed single-sample n==1")
    _check("ttfe_p50_ms" not in out, "failed single-sample omits p50")
    _check("ttfe_p95_ms" not in out, "failed single-sample omits p95")


def test_single_sample_ttfe_point_emits_bind_exec_decomposition():
    # inch #2 (cold): bind_ms (create->Ready provision) + exec_ms (residual
    # ttfe_ms - bind_ms) supplied -> the bind/exec p50==p95 pair emitted alongside
    # the ttfe pair. For n=1 each percentile IS the single sample. Deliberately use
    # an exec value that does NOT equal ttfe - bind (2130 - 2000 == 130) to prove
    # the emit is the SUPPLIED measured sample, never an arithmetic split.
    out = m.single_sample_ttfe_point(2130.0, True, bind_ms=2000.0, exec_ms=200.0)
    _check(_close(out["bind_p50_ms"], 2000.0), "cold bind p50 == sample")
    _check(_close(out["bind_p95_ms"], 2000.0), "cold bind p95 == sample")
    _check(_close(out["exec_p50_ms"], 200.0), "cold exec p50 == sample (measured)")
    _check(_close(out["exec_p95_ms"], 200.0), "cold exec p95 == sample (measured)")
    _check(_close(out["ttfe_p50_ms"], 2130.0), "cold ttfe p50 still emitted")
    _check(_close(out["ttfe_p95_ms"], 2130.0), "cold ttfe p95 still emitted")


def test_single_sample_ttfe_point_byte_identical_when_decomposition_absent():
    # default (no bind_ms/exec_ms kwargs) -> the emit is byte-identical to the
    # legacy single-sample form, so un-decomposed cold callers stay page-unchanged.
    out = m.single_sample_ttfe_point(2130.0, True)
    _check("bind_p50_ms" not in out, "no bind key by default")
    _check("bind_p95_ms" not in out, "no bind p95 key by default")
    _check("exec_p50_ms" not in out, "no exec key by default")
    _check("exec_p95_ms" not in out, "no exec p95 key by default")
    _check(out == {"exec_success_rate": 1.0, "n": 1,
                   "ttfe_p50_ms": 2130.0, "ttfe_p95_ms": 2130.0},
           "un-decomposed single-sample byte-identical to legacy shape")


def test_single_sample_ttfe_point_bind_exec_survive_results_schema_coerce():
    # convergence: the cold bind/exec keys must survive the closed-schema emitter
    # guard (same closed WARM_BIND_FIELDS the warm inch #1 uses).
    out = m.single_sample_ttfe_point(2130.0, True, bind_ms=2000.0, exec_ms=200.0)
    coerced = rs._coerce_sla_metrics(out)
    _check("bind_p50_ms" in coerced, "cold bind p50 survives coerce")
    _check("bind_p95_ms" in coerced, "cold bind p95 survives coerce")
    _check("exec_p50_ms" in coerced, "cold exec p50 survives coerce")
    _check("exec_p95_ms" in coerced, "cold exec p95 survives coerce")


def _lift_like_run_py(sla_metrics: dict, name: str, outcome: str) -> dict:
    """Mirror run.py:_run_one's lift of n + pending_reason out of sla_metrics
    to the scenario top level, so we can validate the suspend_resume merge
    contract end-to-end offline (no cluster, no run.py import)."""
    raw: dict = {"name": name, "outcome": outcome}
    sm = dict(sla_metrics)
    n = sm.pop("n", None)
    if isinstance(n, (int, float)) and not isinstance(n, bool):
        raw["n"] = int(n)
    reason = sm.pop("pending_reason", None)
    if isinstance(reason, str):
        raw["pending_reason"] = reason
    raw["sla_metrics"] = sm
    return raw


def test_single_sample_point_merge_preserves_pending_reason():
    # The suspend_resume merge contract, validated offline: on the gap-persists
    # path the scenario's sla_metrics carries {"pending_reason": "upstream-blocked"}
    # and we merge the single-sample TTFE point onto it. run.py lifts n +
    # pending_reason to the scenario top level; the rest must coerce cleanly and
    # the reason must survive _coerce_scenario when outcome == pending.
    base = {"pending_reason": "upstream-blocked"}
    point = m.single_sample_ttfe_point(5000.0, True)
    merged = {**base, **point}
    raw = _lift_like_run_py(merged, "suspend_resume", "pending")
    coerced = rs._coerce_scenario(raw)
    _check(coerced.get("pending_reason") == "upstream-blocked",
           "pending_reason survives _coerce_scenario on pending outcome")
    _check(coerced["n"] == 1, "n lifted + survives coerce")
    sla = coerced["sla_metrics"]
    _check(_close(sla["ttfe_p50_ms"], 5000.0), "p50 survives merge+coerce")
    _check(_close(sla["ttfe_p95_ms"], 5000.0), "p95 survives merge+coerce")
    _check(sla["exec_success_rate"] == 1.0, "exec rate survives merge+coerce")
    _check("pending_reason" not in sla, "pending_reason lifted out of sla_metrics")
    _check("n" not in sla, "n lifted out of sla_metrics")


def test_multi_sample_n1_identical_to_single_sample_happy():
    # The default (cycle_count=1) emit must be byte-identical to the legacy
    # single-sample form, so the resume row is page-unchanged when the knob is
    # not set.
    single = m.single_sample_ttfe_point(3500.0, True)
    multi = m.multi_sample_ttfe_point([3500.0], [True])
    _check(multi == single, "multi(n=1) == single_sample (happy path)")


def test_multi_sample_n1_identical_to_single_sample_failed():
    single = m.single_sample_ttfe_point(None, False)
    multi = m.multi_sample_ttfe_point([None], [False])
    _check(multi == single, "multi(n=1) == single_sample (failed exec)")


def test_multi_sample_happy_distribution():
    # N>1 all-exec-ok -> real p50/p95 over the samples, n=N, exec rate 1.0,
    # and still NO throughput / NO density (one-shot activation shape).
    samples = [1000.0, 2000.0, 3000.0, 4000.0, 5000.0]
    out = m.multi_sample_ttfe_point(samples, [True] * 5)
    _check(out["n"] == 5, "multi n==5")
    _check(out["exec_success_rate"] == 1.0, "multi exec rate 1.0")
    _check(_close(out["ttfe_p50_ms"], m.percentile(samples, 50)),
           "multi p50 matches percentile over samples")
    _check(_close(out["ttfe_p95_ms"], m.percentile(samples, 95)),
           "multi p95 matches percentile over samples")
    _check("thpt_under_5s_per_node" not in out, "multi omits throughput")
    _check("density_per_vcpu" not in out, "multi omits density")


def test_multi_sample_mixed_exec_excludes_failed_from_distribution():
    # A failed cycle contributes None (excluded from the latency distribution)
    # but still counts against exec_success_rate and n.
    out = m.multi_sample_ttfe_point([1000.0, None, 3000.0], [True, False, True])
    _check(out["n"] == 3, "mixed n==3 (all attempts counted)")
    _check(_close(out["exec_success_rate"], round(2 / 3, 4)),
           "mixed exec rate = 2/3")
    _check(_close(out["ttfe_p50_ms"], m.percentile([1000.0, 3000.0], 50)),
           "mixed p50 over present samples only")
    _check(_close(out["ttfe_p95_ms"], m.percentile([1000.0, 3000.0], 95)),
           "mixed p95 over present samples only")


def test_multi_sample_all_failed_omits_percentiles():
    out = m.multi_sample_ttfe_point([None, None], [False, False])
    _check(out["n"] == 2, "all-failed n==2")
    _check(out["exec_success_rate"] == 0.0, "all-failed exec rate 0.0")
    _check("ttfe_p50_ms" not in out, "all-failed omits p50")
    _check("ttfe_p95_ms" not in out, "all-failed omits p95")


def test_multi_sample_empty_attempts_raises():
    raised = False
    try:
        m.multi_sample_ttfe_point([], [])
    except ValueError:
        raised = True
    _check(raised, "multi_sample on empty attempt set raises")


def test_multi_sample_merge_lifts_n_and_preserves_pending_reason():
    # The N>1 suspend_resume merge contract, validated offline: gap-persists path
    # carries pending_reason, we merge the N-sample TTFE point, run.py lifts n +
    # pending_reason to the scenario top level, the rest coerces cleanly.
    base = {"pending_reason": "upstream-blocked"}
    point = m.multi_sample_ttfe_point([4000.0, 6000.0, 8000.0], [True, True, True])
    merged = {**base, **point}
    raw = _lift_like_run_py(merged, "suspend_resume", "pending")
    coerced = rs._coerce_scenario(raw)
    _check(coerced.get("pending_reason") == "upstream-blocked",
           "pending_reason survives coerce on pending outcome (N>1)")
    _check(coerced["n"] == 3, "n=3 lifted + survives coerce")
    sla = coerced["sla_metrics"]
    _check(_close(sla["ttfe_p50_ms"], m.percentile([4000.0, 6000.0, 8000.0], 50)),
           "N>1 p50 survives merge+coerce")
    _check("pending_reason" not in sla, "pending_reason lifted out of sla_metrics")
    _check("n" not in sla, "n lifted out of sla_metrics")


def _run_all():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"ok - {len(tests)} metrics tests passed")


if __name__ == "__main__":
    _run_all()
