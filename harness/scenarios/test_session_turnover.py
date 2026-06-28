"""Cluster-free tests for session_turnover's pure classifier + helpers.

Dependency-free: `python3 test_session_turnover.py` (exit 0 = pass). These assert
the load-bearing PASS/FAIL logic of the turnover cell — refill_latency_ms (the
median replenishment latency), the completed-cycle gate, and the
median-under-ceiling pass gate — off fixtures, so the metric is verified without
a cluster or the kubernetes client.
"""

import statistics

import session_turnover as cell

_KR = cell._KEY_REFILL          # "refill_latency_ms"
_KP90 = cell._KEY_REFILL_P90    # "refill_p90_ms"


def _refills(*vals):
    """Build a {cycle: refill_ms|None} map from positional values (None = miss)."""
    return {f"t{i}": v for i, v in enumerate(vals)}


# ---- _classify_turnover: median + pass gate ----

def test_all_under_ceiling_passes_and_medians():
    passed, bd, sla = cell._classify_turnover(
        _refills(400.0, 500.0, 600.0, 450.0, 550.0),
        cycle_count=5, refill_ceiling_ms=10000.0, min_completed_ratio=0.8,
    )
    assert passed is True
    assert bd["completed_count"] == 5
    assert bd["median_ms"] == statistics.median([400, 450, 500, 550, 600])
    assert sla[_KR] == 500.0
    # p90 tail surfaced alongside the median headline (nearest-rank of 5 sorted
    # values: idx = ceil(0.9*5)-1 = 4 -> 600.0).
    assert sla[_KP90] == 600.0
    assert sla["n"] == 5


def test_median_over_ceiling_fails_but_surfaces_real_median():
    # all cycles completed, but the median blew the ceiling: FAIL, real number kept.
    passed, bd, sla = cell._classify_turnover(
        _refills(12000.0, 13000.0, 11000.0, 14000.0, 15000.0),
        cycle_count=5, refill_ceiling_ms=10000.0, min_completed_ratio=0.8,
    )
    assert passed is False
    assert bd["completed_count"] == 5
    assert sla[_KR] == 13000.0          # median surfaced even on FAIL


def test_ceiling_is_strict_less_than():
    # median exactly at the ceiling does NOT clear it (median < ceiling, not <=).
    passed, bd, _ = cell._classify_turnover(
        _refills(10000.0, 10000.0, 10000.0),
        cycle_count=3, refill_ceiling_ms=10000.0, min_completed_ratio=0.8,
    )
    assert bd["median_ms"] == 10000.0
    assert passed is False


def test_too_few_completed_fails_even_if_fast():
    # only 2 of 5 cycles refilled; both fast, but below the completed threshold.
    # ceil(5 * 0.8) = 4 required; 2 completers -> FAIL regardless of the median.
    passed, bd, sla = cell._classify_turnover(
        _refills(300.0, None, None, 350.0, None),
        cycle_count=5, refill_ceiling_ms=10000.0, min_completed_ratio=0.8,
    )
    assert bd["completed_threshold"] == 4
    assert bd["completed_count"] == 2
    assert passed is False
    assert sla[_KR] == statistics.median([300.0, 350.0])  # real median still surfaced
    assert sorted(bd["timeouts"]) == ["t1", "t2", "t4"]


def test_completed_threshold_is_ceil_of_ratio():
    # N=5, ratio 0.8 -> ceil(4.0)=4. Exactly 4 completers (fast) passes.
    passed, bd, _ = cell._classify_turnover(
        _refills(300.0, 320.0, 310.0, 305.0, None),
        cycle_count=5, refill_ceiling_ms=10000.0, min_completed_ratio=0.8,
    )
    assert bd["completed_threshold"] == 4
    assert bd["completed_count"] == 4
    assert passed is True


def test_ceil_rounds_up_non_integer_threshold():
    # N=3, ratio 0.8 -> 2.4 -> ceil 3. So all 3 must complete; 2 is a FAIL.
    passed, bd, _ = cell._classify_turnover(
        _refills(300.0, 300.0, None),
        cycle_count=3, refill_ceiling_ms=10000.0, min_completed_ratio=0.8,
    )
    assert bd["completed_threshold"] == 3
    assert passed is False


# ---- zero-completion emission posture ----

def test_zero_completion_emits_empty_metrics():
    # a pool that never refilled publishes NO fabricated number (mirrors burst_create).
    passed, bd, sla = cell._classify_turnover(
        _refills(None, None, None),
        cycle_count=3, refill_ceiling_ms=10000.0, min_completed_ratio=0.8,
    )
    assert passed is False
    assert bd["completed_count"] == 0
    assert bd["median_ms"] is None
    assert sla == {}                    # no median AND no p90 fabricated


def test_single_completer_medians_to_itself():
    # one completer: median is that value; but completed threshold (ceil(3*0.8)=3)
    # not met -> FAIL, yet the real single measurement is surfaced.
    passed, bd, sla = cell._classify_turnover(
        _refills(742.0, None, None),
        cycle_count=3, refill_ceiling_ms=10000.0, min_completed_ratio=0.8,
    )
    assert bd["median_ms"] == 742.0
    assert sla[_KR] == 742.0
    assert sla[_KP90] == 742.0          # p90 of a single value is that value
    assert passed is False


def test_p90_emitted_alongside_median_and_distinct():
    # the tail key surfaces the slow end: p90 > median when the distribution skews.
    # nearest-rank p90 of 10 sorted values: idx = ceil(0.9*10)-1 = 8 -> 900.0.
    _, bd, sla = cell._classify_turnover(
        _refills(100.0, 200.0, 300.0, 400.0, 500.0,
                 600.0, 700.0, 800.0, 900.0, 1000.0),
        cycle_count=10, refill_ceiling_ms=10000.0, min_completed_ratio=0.8,
    )
    assert sla[_KR] == statistics.median(range(100, 1001, 100))  # 550.0
    assert sla[_KP90] == 900.0
    assert bd["p90_ms"] == 900.0
    assert sla[_KP90] > sla[_KR]


# ---- breakdown tail figures ----

def test_breakdown_min_max_p90():
    _, bd, _ = cell._classify_turnover(
        _refills(100.0, 200.0, 300.0, 400.0, 5000.0),
        cycle_count=5, refill_ceiling_ms=10000.0, min_completed_ratio=0.8,
    )
    assert bd["min_ms"] == 100.0
    assert bd["max_ms"] == 5000.0
    assert bd["all_refills_ms"] == [100.0, 200.0, 300.0, 400.0, 5000.0]
    # nearest-rank p90 of 5 sorted values: idx = ceil(0.9*5)-1 = 4 -> 5000.0
    assert bd["p90_ms"] == 5000.0


# ---- _percentile helper ----

def test_percentile_nearest_rank():
    xs = [10.0, 20.0, 30.0, 40.0]
    assert cell._percentile(xs, 0.0) == 10.0     # idx ceil(0)-1 -> clamp 0
    assert cell._percentile(xs, 0.5) == 20.0     # idx ceil(2)-1 = 1
    assert cell._percentile(xs, 0.9) == 40.0     # idx ceil(3.6)-1 = 3
    assert cell._percentile(xs, 1.0) == 40.0     # idx ceil(4)-1 = 3


def test_percentile_empty_is_none():
    assert cell._percentile([], 0.5) is None


# ---- _build_template_manifest: runtimeClassName knob ----

def test_template_omits_runtime_class_by_default():
    # default (kind/runc): no runtimeClassName AND no gVisor toleration — both
    # gated on the same knob, so a vanilla-kind run is unaffected.
    saved = cell._RUNTIME_CLASS
    try:
        cell._RUNTIME_CLASS = ""
        spec = cell._build_template_manifest("tmpl-x")["spec"]["podTemplate"]["spec"]
        assert "runtimeClassName" not in spec
        assert "tolerations" not in spec
    finally:
        cell._RUNTIME_CLASS = saved


def test_template_pins_runtime_class_when_set():
    # gVisor: runtimeClassName pinned AND the GKE-Sandbox taint toleration added so
    # the pod can land on the tainted gVisor node pool (else it stays Pending and
    # the warm pool never fills). operator=Exists keys on the taint key only.
    saved = cell._RUNTIME_CLASS
    try:
        cell._RUNTIME_CLASS = "gvisor"
        spec = cell._build_template_manifest("tmpl-x")["spec"]["podTemplate"]["spec"]
        assert spec["runtimeClassName"] == "gvisor"
        tol = spec["tolerations"]
        assert any(
            t["key"] == "sandbox.gke.io/runtime"
            and t["operator"] == "Exists"
            and t["effect"] == "NoSchedule"
            for t in tol
        )
    finally:
        cell._RUNTIME_CLASS = saved


# ---- manifest shapes (warmpool + claim) ----

def test_warmpool_manifest_references_template_and_replicas():
    m = cell._build_warmpool_manifest("pool-x", "tmpl-x", 4)
    assert m["kind"] == "SandboxWarmPool"
    assert m["spec"]["replicas"] == 4
    assert m["spec"]["sandboxTemplateRef"]["name"] == "tmpl-x"
    assert m["metadata"]["labels"] == {"honest-bench/scenario": "session-turnover"}


def test_claim_manifest_binds_via_warmpoolref():
    m = cell._build_claim_manifest("turn00-x", "pool-x")
    assert m["kind"] == "SandboxClaim"
    assert m["spec"]["warmPoolRef"]["name"] == "pool-x"


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok: {fn.__name__}")
    print(f"test_session_turnover: all {len(fns)} assertions passed")


if __name__ == "__main__":
    _run_all()
