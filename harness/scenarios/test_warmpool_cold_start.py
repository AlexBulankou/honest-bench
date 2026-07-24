"""Cluster-free tests for warmpool_cold_start's pure probe-result assembler.

Dependency-free: `python3 test_warmpool_cold_start.py` (exit 0 = pass). These
assert the locked histogram-input contract of `_assemble_probe_results` — the
pure flatten that replaced the serial `_probe_all_claims` when the TTFE probe
moved INTO each claim's watcher thread (concurrent per-claim, at each claim's own
bind). The I/O probe itself is exercised live against a cluster and unit-tested in
test_ttfe_probe.py; here we pin only the assembly, off fixtures.

The load-bearing contract: one exec_oks entry per claim FIRED, in claim order, so
n == len(exec_oks) == len(claim_names) regardless of how many claims bound or
probed — exec_success_rate's denominator is always the attempt total. A TTFE
sample is appended ONLY when the probe returned a latency (a failed exec drags the
rate but contributes no sample to the histogram).
"""

try:  # cwd == scenarios/ (dependency-free `python3 test_warmpool_cold_start.py`)
    import warmpool_cold_start as cell
except ModuleNotFoundError:  # repo-root pytest: scenarios/ is a package, not on sys.path
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import warmpool_cold_start as cell


def _names(n):
    return [f"claim{i:02d}" for i in range(n)]


# ---- _assemble_probe_results: the locked histogram-input contract ----

def test_all_bound_and_ok_every_sample_kept():
    names = _names(3)
    results = {
        "claim00": (300.0, True),
        "claim01": (420.5, True),
        "claim02": (510.0, True),
    }
    samples, oks = cell._assemble_probe_results(names, results)
    assert oks == [True, True, True]
    assert samples == [300.0, 420.5, 510.0]
    # locked: one exec_ok per claim fired
    assert len(oks) == len(names)


def test_never_bound_claim_is_false_with_no_sample():
    # claim01 never bound -> absent from ttfe_results entirely.
    names = _names(3)
    results = {
        "claim00": (300.0, True),
        "claim02": (510.0, True),
    }
    samples, oks = cell._assemble_probe_results(names, results)
    assert oks == [True, False, True]
    assert samples == [300.0, 510.0]
    assert len(oks) == len(names)


def test_failed_exec_drags_rate_but_drops_from_histogram():
    # claim01 bound but the probe failed: (None, False) -> exec_ok False, no sample.
    names = _names(3)
    results = {
        "claim00": (300.0, True),
        "claim01": (None, False),
        "claim02": (510.0, True),
    }
    samples, oks = cell._assemble_probe_results(names, results)
    assert oks == [True, False, True]
    assert samples == [300.0, 510.0]  # the None sample is dropped
    assert len(oks) == len(names)


def test_order_follows_claim_names_not_dict_insertion():
    # ttfe_results inserted out of order; output must follow claim_names order.
    names = _names(3)
    results = {
        "claim02": (3.0, True),
        "claim00": (1.0, True),
        "claim01": (2.0, True),
    }
    samples, oks = cell._assemble_probe_results(names, results)
    assert samples == [1.0, 2.0, 3.0]
    assert oks == [True, True, True]


def test_all_failed_zero_samples_full_false_vector():
    names = _names(4)
    results = {
        "claim00": (None, False),
        "claim01": (None, False),
        # claim02, claim03 never bound
    }
    samples, oks = cell._assemble_probe_results(names, results)
    assert samples == []
    assert oks == [False, False, False, False]
    assert len(oks) == len(names)


def test_empty_claim_list_yields_empty_pair():
    samples, oks = cell._assemble_probe_results([], {})
    assert samples == []
    assert oks == []


def test_zero_latency_sample_is_kept_not_treated_as_falsy():
    # A genuine 0.0ms TTFE (degenerate but valid) must NOT be dropped — the gate
    # is `is not None`, not truthiness.
    names = _names(1)
    results = {"claim00": (0.0, True)}
    samples, oks = cell._assemble_probe_results(names, results)
    assert samples == [0.0]
    assert oks == [True]


def test_n_equals_claim_count_across_mixed_outcomes():
    # The reserved-n invariant the harness lifts to "(n=N)": len(exec_oks) is the
    # attempt total no matter the mix of ok / failed / never-bound.
    names = _names(10)
    results = {
        "claim00": (100.0, True),
        "claim01": (None, False),     # bound, exec failed
        "claim03": (250.0, True),
        "claim07": (None, False),     # bound, exec failed
        "claim09": (180.0, True),
        # claim02,04,05,06,08 never bound
    }
    samples, oks = cell._assemble_probe_results(names, results)
    assert len(oks) == 10
    assert sum(1 for o in oks if o) == 3       # three genuine execs
    assert samples == [100.0, 250.0, 180.0]    # three samples, in claim order


# ---- warm-tier scoping: _classify publishes the single-source warm set ----
#
# The scenario OVERFLOWS on purpose (claim_count > pool_replicas) so the gate can
# prove a distinct fast tier — but the emitted TTFE row must describe the WARM-POOL
# HIT, not the warm+cold blend. These pin that _classify_latencies publishes the
# gate's warm set as claim NAMES (the single source of truth the emit path reuses),
# and that scoping the histogram to that set drops the cold overflow.

def test_classify_publishes_warm_names_as_single_source():
    # 3 warm (fast) + 2 cold overflow, pool_replicas=3.
    latencies = {"c0": 1.5, "c1": 0.8, "c2": 1.2, "c3": 5.0, "c4": 9.0}
    passed, bd = cell._classify_latencies(
        latencies, pool_replicas=3, abs_ceiling_s=2.5, separation_ratio=1.8,
    )
    assert passed  # warm_max 1.5 < 2.5 (absolute clause)
    # warm_names = the pool_replicas fastest-binding NAMES, ascending latency.
    assert bd["warm_names"] == ["c1", "c2", "c0"]
    # single source of truth: warm_max IS the last warm claim's latency.
    assert bd["warm_max_s"] == latencies[bd["warm_names"][-1]] == 1.5
    # the cold overflow is NOT in the warm set.
    assert "c3" not in bd["warm_names"] and "c4" not in bd["warm_names"]


def test_warm_scope_excludes_cold_overflow_from_histogram():
    # Scoping the histogram to warm_names drops the cold overflow samples — the
    # whole point of the honesty fix. Contrast against the all-claims blend.
    latencies = {"c0": 1.5, "c1": 0.8, "c2": 1.2, "c3": 5.0, "c4": 9.0}
    _, bd = cell._classify_latencies(
        latencies, pool_replicas=3, abs_ceiling_s=2.5, separation_ratio=1.8,
    )
    ttfe_results = {
        "c0": (1799.0, True), "c1": (1009.0, True), "c2": (1400.0, True),
        "c3": (5200.0, True), "c4": (9800.0, True),   # cold overflow
    }
    warm_samples, warm_oks = cell._assemble_probe_results(
        bd["warm_names"], ttfe_results,
    )
    assert sorted(warm_samples) == [1009.0, 1400.0, 1799.0]  # warm only
    assert len(warm_oks) == 3                                # uniform N=3
    # the all-claims blend WOULD carry the cold overflow (the mislabel we fix).
    blend_samples, _ = cell._assemble_probe_results(list(latencies), ttfe_results)
    assert 5200.0 in blend_samples and 9800.0 in blend_samples


def test_under_delivery_leaves_warm_names_empty():
    # Fewer completed than pool_replicas -> FAIL, warm_names stays the [] default
    # (no full warm cluster to scope to).
    latencies = {"c0": 1.5, "c1": None, "c2": None}
    passed, bd = cell._classify_latencies(
        latencies, pool_replicas=3, abs_ceiling_s=2.5, separation_ratio=1.8,
    )
    assert not passed
    assert bd["warm_names"] == []


# ---- _add_gate_diagnostic_metrics: hb#379 numeric gate-diagnostic keys ----
#
# The excerpt string naming WHY the gate passed/failed is deliberately never
# persisted (run.py deletes it, the raw-failure_excerpt public-safety rule), so a
# committed FAIL row previously carried no numeric trace of warm_max/cold_min/
# separation. These pin the three keys land correctly and the no-op cases stay
# no-ops (cold-baseline mode, under-delivery, no-cold-tier).

def test_gate_diagnostics_adds_all_three_keys_when_cold_tier_present():
    latencies = {"c0": 1.5, "c1": 0.8, "c2": 1.2, "c3": 5.0, "c4": 9.0}
    _, bd = cell._classify_latencies(
        latencies, pool_replicas=3, abs_ceiling_s=2.5, separation_ratio=1.8,
    )
    sla_metrics = cell._add_gate_diagnostic_metrics(
        {}, bd, warm_max=bd["warm_max_s"], pool_replicas=3,
    )
    assert sla_metrics["warmpool_gate_warm_max_ms"] == 1500.0
    assert sla_metrics["warmpool_gate_cold_min_ms"] == 5000.0
    assert sla_metrics["warmpool_gate_separation_ratio"] == bd["separation_observed"]


def test_gate_diagnostics_omits_cold_and_separation_keys_when_no_cold_tier():
    # claim_count == pool_replicas: no overflow claims, so no cold tier to
    # separate from. warm_max still reported; cold/separation keys absent.
    latencies = {"c0": 1.5, "c1": 0.8, "c2": 1.2}
    _, bd = cell._classify_latencies(
        latencies, pool_replicas=3, abs_ceiling_s=2.5, separation_ratio=1.8,
    )
    sla_metrics = cell._add_gate_diagnostic_metrics(
        {}, bd, warm_max=bd["warm_max_s"], pool_replicas=3,
    )
    assert sla_metrics["warmpool_gate_warm_max_ms"] == 1500.0
    assert "warmpool_gate_cold_min_ms" not in sla_metrics
    assert "warmpool_gate_separation_ratio" not in sla_metrics


def test_gate_diagnostics_noop_on_under_delivery():
    # warm_max is None (under-delivery) -> no-op, sla_metrics returned unchanged.
    latencies = {"c0": 1.5, "c1": None, "c2": None}
    _, bd = cell._classify_latencies(
        latencies, pool_replicas=3, abs_ceiling_s=2.5, separation_ratio=1.8,
    )
    sla_metrics = cell._add_gate_diagnostic_metrics(
        {}, bd, warm_max=bd["warm_max_s"], pool_replicas=3,
    )
    assert sla_metrics == {}


def test_gate_diagnostics_noop_in_cold_baseline_mode():
    # pool_replicas == 0 (cold-baseline mode) -> no gate, no-op regardless of
    # warm_max/sla_metrics contents.
    sla_metrics = cell._add_gate_diagnostic_metrics(
        {"some_key": 1.0}, {"cold_path_min_s": 1.0, "separation_observed": 2.0},
        warm_max=1.5, pool_replicas=0,
    )
    assert sla_metrics == {"some_key": 1.0}


def test_gate_diagnostics_noop_when_sla_metrics_not_a_dict():
    # The TTFE-off under-delivery path can hand back a non-dict sentinel in
    # theory; guard against mutating/crashing on it.
    result = cell._add_gate_diagnostic_metrics(
        None, {"cold_path_min_s": 1.0, "separation_observed": 2.0},
        warm_max=1.5, pool_replicas=3,
    )
    assert result is None


# ---- _under_delivery_outcome: honest FAIL row vs opaque assert crash (#4093) ----
#
# On the TTFE-on path, an under-delivered warm pool (warm_max_s is None) would hit
# the emit block's single-source assert (len(emit_names) == pool_replicas) on the
# empty warm set -> AssertionError -> opaque crash-caught 'fail' cell. The helper
# returns an explicit FAIL triple BEFORE the assert; run() returns it early. These
# pin: (a) an under-delivery breakdown yields a FAIL triple with empty sla_metrics
# and a shortfall-naming excerpt, (b) a delivered warm cluster returns None (fall
# through to the normal PASS/FAIL emit path), and (c) the cold-baseline mode
# (pool_replicas<=0) returns None (never short-circuits the neutral cold record).

def test_under_delivery_outcome_emits_honest_fail_triple():
    latencies = {"c0": 1.5, "c1": None, "c2": None}
    _, bd = cell._classify_latencies(
        latencies, pool_replicas=3, abs_ceiling_s=2.5, separation_ratio=1.8,
    )
    all_lat_str = ", ".join(f"{x:.3f}" for x in bd["all_latencies_s"])
    out = cell._under_delivery_outcome(
        bd, pool_replicas=3, claim_count=5, all_lat_str=all_lat_str,
    )
    assert out is not None
    outcome, excerpt, sla = out
    assert outcome == "FAIL"
    assert sla == {}                       # no isolated warm-tier measurement
    assert "1/3" in excerpt                # only 1 of 3 warm slots bound
    assert "claims fired=5" in excerpt
    assert "under-delivered" in excerpt


def test_under_delivery_outcome_none_when_warm_cluster_delivered():
    # A full warm cluster (warm_max_s set) -> None, so run() falls through to the
    # normal PASS/FAIL emit path and the assert stays reachable as a drift guard.
    latencies = {"c0": 1.5, "c1": 0.8, "c2": 1.2, "c3": 5.0, "c4": 9.0}
    passed, bd = cell._classify_latencies(
        latencies, pool_replicas=3, abs_ceiling_s=2.5, separation_ratio=1.8,
    )
    assert passed and bd["warm_max_s"] is not None
    out = cell._under_delivery_outcome(
        bd, pool_replicas=3, claim_count=5, all_lat_str="",
    )
    assert out is None


def test_under_delivery_outcome_none_in_cold_baseline_mode():
    # pool_replicas<=0 is the cold-baseline mode (no warm tier to under-deliver);
    # the helper must NOT short-circuit the neutral cold PASS record.
    latencies = {"c0": 1.5, "c1": 0.8, "c2": 1.2}
    _, bd = cell._classify_latencies(
        latencies, pool_replicas=0, abs_ceiling_s=2.5, separation_ratio=1.8,
    )
    out = cell._under_delivery_outcome(
        bd, pool_replicas=0, claim_count=3, all_lat_str="",
    )
    assert out is None


# ---- _build_template_manifest: the runtime-class pin wiring (#3942) ----
#
# The pure pin logic lives in test_runtime_class.py; these lock that the SCENARIO
# actually routes its template pod_spec through the shared helper, gated on the
# module-level _RUNTIME_CLASS knob. _RUNTIME_CLASS is read at import; monkeypatch the
# module attribute (not os.environ) to exercise each runtime in-process, restoring it.

def _pod_spec_with_runtime(value):
    saved = cell._RUNTIME_CLASS
    cell._RUNTIME_CLASS = value
    try:
        return cell._build_template_manifest("tmpl-test")["spec"]["podTemplate"]["spec"]
    finally:
        cell._RUNTIME_CLASS = saved


def test_template_default_off_is_byte_identical():
    # Unset knob -> the template is its pre-#3942 shape: no runtime fields added.
    spec = _pod_spec_with_runtime("")
    assert "runtimeClassName" not in spec
    assert "tolerations" not in spec
    assert "nodeSelector" not in spec
    assert spec["restartPolicy"] == "Never"
    assert spec["containers"][0]["name"] == "sandbox"


def test_template_gvisor_pins_class_and_toleration():
    spec = _pod_spec_with_runtime("gvisor")
    assert spec["runtimeClassName"] == "gvisor"
    assert "sandbox.gke.io/runtime" in {t["key"] for t in spec["tolerations"]}
    assert "nodeSelector" not in spec  # gVisor needs no node label


def test_template_kata_pins_class_toleration_and_selector():
    spec = _pod_spec_with_runtime("kata")
    assert spec["runtimeClassName"] == "kata"
    assert "sandbox.gke.io/kata" in {t["key"] for t in spec["tolerations"]}
    assert spec["nodeSelector"] == {"nested-virtualization": "enabled"}


# ---- hb#379: _sample_pool_ready / _run_pool_ready_sampler (continuous
# readyReplicas diagnostic sampler, mirrors the hb#319 node-count sampler) ----

class _FakeCustomOkOnce:
    """Returns a fixed readyReplicas value; raises if called more than `calls`."""

    def __init__(self, ready: int):
        self._ready = ready

    def get_namespaced_custom_object(self, group, version, namespace, plural, name):
        return {"status": {"readyReplicas": self._ready}}


class _FakeCustomRaises:
    def get_namespaced_custom_object(self, group, version, namespace, plural, name):
        raise RuntimeError("boom")


class _FakeCustomNoStatus:
    def get_namespaced_custom_object(self, group, version, namespace, plural, name):
        return {}


def test_sample_pool_ready_returns_ready_replicas():
    assert cell._sample_pool_ready(_FakeCustomOkOnce(7), "pool-x") == 7


def test_sample_pool_ready_missing_status_reads_as_zero():
    assert cell._sample_pool_ready(_FakeCustomNoStatus(), "pool-x") == 0


def test_sample_pool_ready_never_raises_returns_negative_one():
    assert cell._sample_pool_ready(_FakeCustomRaises(), "pool-x") == -1


def test_run_pool_ready_sampler_appends_until_stopped():
    import threading

    custom = _FakeCustomOkOnce(3)
    stop_event = threading.Event()
    samples: list = []

    # interval_s=0 -> the loop's stop_event.wait(0) returns immediately each
    # pass, so we stop it ourselves after it has had a chance to append.
    def _stop_after_first():
        while len(samples) < 3:
            pass
        stop_event.set()

    stopper = threading.Thread(target=_stop_after_first, daemon=True)
    stopper.start()
    cell._run_pool_ready_sampler(custom, "pool-x", stop_event, samples, 0.0)
    stopper.join(timeout=5.0)

    assert len(samples) >= 3
    assert all(ready == 3 for _, ready in samples)
    # timestamps are monotonically non-decreasing elapsed-since-start floats
    assert all(t >= 0 for t, _ in samples)


def test_run_pool_ready_sampler_stopped_immediately_yields_no_samples():
    import threading

    stop_event = threading.Event()
    stop_event.set()  # already stopped before the loop's first check
    samples: list = []
    cell._run_pool_ready_sampler(_FakeCustomRaises(), "pool-x", stop_event, samples, 0.0)
    assert samples == []


# ---- _min_ready_during_burst: hb#379 durable churn metric (promotes the sampler
# above from diagnostic-log-only to a published sla_metrics key) ----
#
# `samples` are (rel_t, readyReplicas) relative to `sampler_t0`; the helper
# converts each to an absolute monotonic timestamp and filters to
# `>= burst_start_abs` before taking the min, so pre-burst warm-up samples
# never dilute the churn signal.

def test_min_ready_during_burst_takes_min_of_in_burst_samples():
    # sampler_t0=100 -> absolute sample times are 100, 105, 110, 115.
    # burst starts at t=107 -> only the last two (110, 115) count.
    samples = [(0.0, 5), (5.0, 5), (10.0, 2), (15.0, 4)]
    result = cell._min_ready_during_burst(samples, sampler_t0=100.0, burst_start_abs=107.0)
    assert result == 2


def test_min_ready_during_burst_excludes_pre_burst_warmup_dip():
    # A dip during warm-up (before burst_start) must NOT be reported — only
    # samples from burst start onward matter.
    samples = [(0.0, 0), (1.0, 1), (2.0, 5), (3.0, 5), (4.0, 5)]
    result = cell._min_ready_during_burst(samples, sampler_t0=0.0, burst_start_abs=2.0)
    assert result == 5


def test_min_ready_during_burst_excludes_failed_polls():
    # -1 marks a failed poll (see _sample_pool_ready) -- never a real ready
    # count, must be excluded even though it's numerically the minimum.
    samples = [(0.0, 3), (1.0, -1), (2.0, 3)]
    result = cell._min_ready_during_burst(samples, sampler_t0=0.0, burst_start_abs=0.0)
    assert result == 3


def test_min_ready_during_burst_boundary_sample_included():
    # A sample exactly at burst_start_abs is IN-burst (>=, not >).
    samples = [(0.0, 4)]
    result = cell._min_ready_during_burst(samples, sampler_t0=10.0, burst_start_abs=10.0)
    assert result == 4


def test_min_ready_during_burst_no_in_burst_samples_returns_none():
    # All samples are before burst start -> no in-burst data -> None.
    samples = [(0.0, 5), (1.0, 5)]
    result = cell._min_ready_during_burst(samples, sampler_t0=0.0, burst_start_abs=100.0)
    assert result is None


def test_min_ready_during_burst_empty_samples_returns_none():
    assert cell._min_ready_during_burst([], sampler_t0=0.0, burst_start_abs=0.0) is None


def test_min_ready_during_burst_all_failed_polls_returns_none():
    samples = [(0.0, -1), (1.0, -1)]
    result = cell._min_ready_during_burst(samples, sampler_t0=0.0, burst_start_abs=0.0)
    assert result is None


# ---- hb#379: _wait_for_pool_warm stability-window (reject a single-tick flicker) ----

class _FakeCustomSequence:
    """Returns successive readyReplicas values from a fixed list, one per call.

    Holds the last value once the sequence is exhausted (mirrors a pool that
    settles at its final observed state rather than raising).
    """

    def __init__(self, ready_sequence: list[int]):
        self._seq = list(ready_sequence)
        self._i = 0

    def get_namespaced_custom_object(self, group, version, namespace, plural, name):
        ready = self._seq[min(self._i, len(self._seq) - 1)]
        self._i += 1
        return {"status": {"readyReplicas": ready}}


def _no_sleep(_seconds):
    pass


def test_wait_for_pool_warm_returns_on_first_poll_when_stability_polls_is_one():
    custom = _FakeCustomOkOnce(5)
    orig_sleep = cell.time.sleep
    cell.time.sleep = _no_sleep
    try:
        obj = cell._wait_for_pool_warm(
            custom, pool_name="pool-x", target_ready=5, timeout_s=10,
        )
    finally:
        cell.time.sleep = orig_sleep
    assert obj["status"]["readyReplicas"] == 5


def test_wait_for_pool_warm_requires_consecutive_polls_before_returning():
    # First poll hits target, second drops below (reset), third+fourth+fifth
    # hold at/above target -> must not return before the 3rd consecutive hit.
    custom = _FakeCustomSequence([5, 3, 5, 5, 5])
    orig_sleep = cell.time.sleep
    cell.time.sleep = _no_sleep
    try:
        obj = cell._wait_for_pool_warm(
            custom, pool_name="pool-x", target_ready=5, timeout_s=10,
            stability_polls=3,
        )
    finally:
        cell.time.sleep = orig_sleep
    assert obj["status"]["readyReplicas"] == 5
    # returned right after the 3rd consecutive at/above-target poll (index 4,
    # the 5th call: polls at indices 2,3,4 are the 3 consecutive hits)
    assert custom._i == 5


def test_wait_for_pool_warm_flicker_never_sustains_times_out():
    # Alternates below/at-target forever -> consecutive streak never reaches 2.
    calls = {"n": 0}

    class _Flicker:
        def get_namespaced_custom_object(self, group, version, namespace, plural, name):
            calls["n"] += 1
            ready = 5 if calls["n"] % 2 else 4
            return {"status": {"readyReplicas": ready}}

    fake_deadline = [0.0]

    def _fake_monotonic():
        # advance by 1s per call to time.monotonic() so the while-loop's
        # deadline check eventually trips without a real sleep.
        fake_deadline[0] += 1.0
        return fake_deadline[0]

    orig_sleep = cell.time.sleep
    orig_monotonic = cell.time.monotonic
    cell.time.sleep = _no_sleep
    cell.time.monotonic = _fake_monotonic
    try:
        try:
            cell._wait_for_pool_warm(
                _Flicker(), pool_name="pool-x", target_ready=5, timeout_s=5,
                stability_polls=2,
            )
            raised = False
        except RuntimeError as exc:
            raised = True
            msg = str(exc)
    finally:
        cell.time.sleep = orig_sleep
        cell.time.monotonic = orig_monotonic

    assert raised, "expected a flicker that never sustains 2 consecutive polls to time out"
    assert "did not sustain readyReplicas>=5" in msg
    assert "2 consecutive poll(s)" in msg


# ---- hb#411: _cleanup batch-retry rides out the IAM-strike window ----
# The finally-block cleanup can run inside the sub-92s a4-hb-refresh@ IAM strip,
# where every delete 403s. A single best-effort pass then leaks the pool + 30
# member sandboxes on the SHARED cluster (billed nodes). _cleanup now retries the
# WHOLE remaining set together (shared backoff, so object count doesn't blow up
# the wait) and returns the still-leaked descriptors (empty == clean) so a real
# leak is a loud, testable signal instead of swallowed WARNINGs. sleep + attempt
# budget are injected so these stay cluster-free and instant.

from kubernetes.client.exceptions import ApiException as _ApiException  # noqa: E402


class _FakeDeleteOK:
    """Every delete succeeds; records deleted names."""

    def __init__(self):
        self.deleted: list = []

    def delete_namespaced_custom_object(self, group, version, namespace, plural, name):
        self.deleted.append(name)
        return {}


class _FakeDeleteStatus:
    """Every delete raises ApiException with a fixed status (403 strip / 404 gone)."""

    def __init__(self, status):
        self._status = status

    def delete_namespaced_custom_object(self, group, version, namespace, plural, name):
        raise _ApiException(status=self._status)


class _FakeDeleteRecoversAfter:
    """403s until the injected sleep has fired `recover_after` times, then succeeds.

    Ties "which retry pass we're on" to the shared sleep counter, so a fixture can
    model 'IAM re-settles after N backoffs' without touching wall-clock time.
    """

    def __init__(self, recover_after, sleep_counter):
        self._recover_after = recover_after
        self._counter = sleep_counter  # 1-element list mutated by the fake sleep
        self.deleted: list = []

    def delete_namespaced_custom_object(self, group, version, namespace, plural, name):
        if self._counter[0] < self._recover_after:
            raise _ApiException(status=403)
        self.deleted.append(name)
        return {}


def _counting_sleep():
    calls = [0]

    def _sleep(_secs):
        calls[0] += 1

    return calls, _sleep


def test_cleanup_happy_path_deletes_all_and_never_sleeps():
    fake = _FakeDeleteOK()
    calls, sleep = _counting_sleep()
    leaked = cell._cleanup(
        fake, claim_names=_names(3), pool_name="pool-x", template_name="tmpl-x",
        sleep=sleep,
    )
    assert leaked == []
    # 3 claims + pool + template all deleted on the first pass
    assert len(fake.deleted) == 5
    assert "pool-x" in fake.deleted and "tmpl-x" in fake.deleted
    assert calls[0] == 0  # clean cleanup pays no backoff


def test_cleanup_retries_and_recovers_when_iam_resettles():
    calls, sleep = _counting_sleep()
    fake = _FakeDeleteRecoversAfter(recover_after=2, sleep_counter=calls)
    leaked = cell._cleanup(
        fake, claim_names=_names(3), pool_name="pool-x", template_name="tmpl-x",
        max_attempts=7, backoff_base_s=0.0, backoff_cap_s=0.0, sleep=sleep,
    )
    assert leaked == []  # recovered before the attempt budget ran out
    assert calls[0] == 2  # slept twice (passes 1+2 failed), succeeded on pass 3
    assert len(fake.deleted) == 5


def test_cleanup_leaks_loud_when_iam_never_resettles():
    calls, sleep = _counting_sleep()
    leaked = cell._cleanup(
        _FakeDeleteStatus(403), claim_names=_names(3),
        pool_name="pool-x", template_name="tmpl-x",
        max_attempts=3, backoff_base_s=0.0, backoff_cap_s=0.0, sleep=sleep,
    )
    # every object still leaked; descriptors carry label + name for the reaper
    assert len(leaked) == 5
    assert "warmpool/pool-x" in leaked
    assert "template/tmpl-x" in leaked
    assert "claim/claim00" in leaked
    # slept between each of the 3 attempts, but not after the last
    assert calls[0] == 2


def test_cleanup_treats_404_as_deleted_no_retry():
    calls, sleep = _counting_sleep()
    leaked = cell._cleanup(
        _FakeDeleteStatus(404), claim_names=_names(3),
        pool_name="pool-x", template_name="tmpl-x",
        max_attempts=7, backoff_base_s=0.0, backoff_cap_s=0.0, sleep=sleep,
    )
    assert leaked == []      # 404 == already gone
    assert calls[0] == 0     # nothing to retry, no backoff


def test_cleanup_empty_claim_list_still_deletes_pool_and_template():
    fake = _FakeDeleteOK()
    calls, sleep = _counting_sleep()
    leaked = cell._cleanup(
        fake, claim_names=[], pool_name="pool-x", template_name="tmpl-x",
        sleep=sleep,
    )
    assert leaked == []
    assert fake.deleted == ["pool-x", "tmpl-x"]
    assert calls[0] == 0


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok: {fn.__name__}")
    print(f"test_warmpool_cold_start: all {len(fns)} assertions passed")


if __name__ == "__main__":
    _run_all()
