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

from . import metrics as m
from . import results_schema as rs


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


def _run_all():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"ok - {len(tests)} metrics tests passed")


if __name__ == "__main__":
    _run_all()
