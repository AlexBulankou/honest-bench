"""Cluster-free tests for burst_create's pure classifier + manifest/vCPU helpers.

Dependency-free: `python3 test_burst_create.py` (exit 0 = pass). These assert the
load-bearing PASS/FAIL logic of the fleet headline cell — sandboxes_ready_under_1s
(the count), density_per_vcpu, and the ceil(K * ratio) pass gate — off fixtures,
so the headline metric is verified without a cluster or the kubernetes client.
"""

import math

try:  # cwd == scenarios/ (dependency-free `python3 test_burst_create.py`)
    import burst_create as cell
except ModuleNotFoundError:  # repo-root pytest: scenarios/ is a package, not on sys.path
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import burst_create as cell

_KC = cell._KEY_COUNT       # "sandboxes_ready_under_1s"
_KD = cell._KEY_DENSITY     # "density_per_vcpu"
_KEC = cell._KEY_EXEC_COUNT  # "sandboxes_exec_under_1s" (#3954 corroboration)
_KER = cell._KEY_EXEC_RATE   # "exec_success_rate" (#3954 corroboration)


def _ttfis(*vals):
    """Build a {claim: ttfi|None} map from positional values (None = timed out)."""
    return {f"c{i}": v for i, v in enumerate(vals)}


# ---- _classify_burst: the count + pass gate ----

def test_all_under_ceiling_passes_and_counts_all():
    passed, bd, sla = cell._classify_burst(
        _ttfis(0.3, 0.4, 0.5, 0.2),
        claim_count=4, ttfi_ceiling_s=1.0, total_vcpu=2.0, min_qualified_ratio=0.8,
    )
    assert passed is True
    assert bd["count_under"] == 4
    assert sla[_KC] == 4.0
    assert sla["n"] == 4


def test_ceiling_is_strict_less_than():
    # exactly 1.0s does NOT clear a 1.0s bar (TTFI < ceiling, not <=).
    _, bd, _ = cell._classify_burst(
        _ttfis(1.0, 0.9),
        claim_count=2, ttfi_ceiling_s=1.0, total_vcpu=2.0, min_qualified_ratio=0.5,
    )
    assert bd["count_under"] == 1


def test_pass_threshold_is_ceil_of_ratio():
    # K=10, ratio 0.8 -> need ceil(8.0)=8 under the bar. 8 passes, 7 fails.
    passed8, bd8, _ = cell._classify_burst(
        _ttfis(*([0.2] * 8 + [5.0] * 2)),
        claim_count=10, ttfi_ceiling_s=1.0, total_vcpu=4.0, min_qualified_ratio=0.8,
    )
    assert bd8["pass_threshold"] == 8
    assert passed8 is True
    passed7, _, _ = cell._classify_burst(
        _ttfis(*([0.2] * 7 + [5.0] * 3)),
        claim_count=10, ttfi_ceiling_s=1.0, total_vcpu=4.0, min_qualified_ratio=0.8,
    )
    assert passed7 is False


def test_ceil_rounds_up_non_integer_threshold():
    # K=3, ratio 0.8 -> 2.4 -> ceil 3. So all 3 must clear; 2 is a FAIL.
    _, bd, _ = cell._classify_burst(
        _ttfis(0.2, 0.2, 0.2),
        claim_count=3, ttfi_ceiling_s=1.0, total_vcpu=2.0, min_qualified_ratio=0.8,
    )
    assert bd["pass_threshold"] == math.ceil(3 * 0.8) == 3


def test_timeouts_counted_as_not_under_and_listed():
    passed, bd, sla = cell._classify_burst(
        _ttfis(0.3, None, 0.4, None),
        claim_count=4, ttfi_ceiling_s=1.0, total_vcpu=2.0, min_qualified_ratio=0.8,
    )
    assert bd["count_under"] == 2          # the two None never bound -> not under
    assert bd["completed_count"] == 2
    assert sorted(bd["timeouts"]) == ["c1", "c3"]
    assert passed is False                 # 2 < ceil(4*0.8)=4


# ---- density_per_vcpu ----

def test_density_is_count_over_total_vcpu():
    _, bd, sla = cell._classify_burst(
        _ttfis(*([0.2] * 8)),
        claim_count=8, ttfi_ceiling_s=1.0, total_vcpu=4.0, min_qualified_ratio=0.8,
    )
    assert bd["density_per_vcpu"] == 2.0    # 8 / 4
    assert sla[_KD] == 2.0


def test_density_zero_when_vcpu_unknown():
    # _sum_node_vcpu can return 0.0 on an odd capacity unit; never divide by zero.
    _, bd, sla = cell._classify_burst(
        _ttfis(0.2, 0.2),
        claim_count=2, ttfi_ceiling_s=1.0, total_vcpu=0.0, min_qualified_ratio=0.5,
    )
    assert bd["density_per_vcpu"] == 0.0
    assert sla[_KD] == 0.0                  # count>0 still emits, density 0.0


# ---- sla_metrics emission posture ----

def test_zero_under_emits_empty_metrics():
    # an all-cold burst publishes NO fabricated number (mirrors warm_max posture).
    passed, bd, sla = cell._classify_burst(
        _ttfis(5.0, 6.0, None),
        claim_count=3, ttfi_ceiling_s=1.0, total_vcpu=2.0, min_qualified_ratio=0.8,
    )
    assert passed is False
    assert bd["count_under"] == 0
    assert sla == {}


def test_partial_delivery_fails_but_surfaces_real_count():
    # some-but-not-enough: FAIL, yet the real count is still published (not hidden).
    passed, bd, sla = cell._classify_burst(
        _ttfis(*([0.2] * 5 + [9.0] * 5)),
        claim_count=10, ttfi_ceiling_s=1.0, total_vcpu=5.0, min_qualified_ratio=0.8,
    )
    assert passed is False
    assert sla[_KC] == 5.0
    assert sla[_KD] == 1.0                  # 5 / 5


# ---- _parse_cpu_quantity ----

def test_parse_cpu_integer_cores():
    assert cell._parse_cpu_quantity("8") == 8.0
    assert cell._parse_cpu_quantity(4) == 4.0


def test_parse_cpu_millicores():
    assert cell._parse_cpu_quantity("8000m") == 8.0
    assert cell._parse_cpu_quantity("500m") == 0.5


def test_parse_cpu_unparseable_is_zero():
    assert cell._parse_cpu_quantity("garbage") == 0.0
    assert cell._parse_cpu_quantity(None) == 0.0
    assert cell._parse_cpu_quantity("") == 0.0
    assert cell._parse_cpu_quantity(True) == 0.0   # bool excluded


# ---- _build_template_manifest: runtimeClassName knob ----

def test_template_omits_runtime_class_by_default():
    # default (kind/runc): no runtimeClassName AND no gVisor toleration — the
    # toleration is gated on the same knob, so a vanilla-kind run is unaffected.
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


# ---- _is_claim_ready_and_bound ----

def test_ready_and_bound_true_only_when_both():
    ready_bound = {"conditions": [{"type": "Ready", "status": "True"}],
                   "sandbox": {"name": "sbx-1"}}
    assert cell._is_claim_ready_and_bound(ready_bound) is True
    # Ready but not bound
    assert cell._is_claim_ready_and_bound(
        {"conditions": [{"type": "Ready", "status": "True"}]}) is False
    # bound but not Ready
    assert cell._is_claim_ready_and_bound(
        {"conditions": [{"type": "Ready", "status": "False"}],
         "sandbox": {"name": "sbx-1"}}) is False
    # empty / None safe
    assert cell._is_claim_ready_and_bound({}) is False
    assert cell._is_claim_ready_and_bound(None) is False


# ---- _assert_substrate_runtime_consistency (sub-gap 1: pure, no cluster) ----

def test_gke_sandbox_requires_gvisor_runtime_class():
    # gke-sandbox label + unset runtime class -> runc pods under a gVisor banner.
    for bad in ("", "runc", "gvisor-typo"):
        raised = False
        try:
            cell._assert_substrate_runtime_consistency("gke-sandbox", bad)
        except RuntimeError:
            raised = True
        assert raised, f"gke-sandbox + runtime_class={bad!r} must crash-FAIL"


def test_gke_sandbox_with_gvisor_is_consistent():
    # the honest pairing: gke-sandbox banner + gvisor runtime class -> no raise.
    cell._assert_substrate_runtime_consistency("gke-sandbox", "gvisor")


def test_kind_and_gke_impose_no_runtime_constraint():
    # kind/gke do not claim gVisor isolation, so any runtime class is fine
    # (including the default ""): the guard only gates the gke-sandbox banner.
    cell._assert_substrate_runtime_consistency("kind", "")
    cell._assert_substrate_runtime_consistency("gke", "")
    cell._assert_substrate_runtime_consistency("kind", "gvisor")


# ---- _verify_bound_pods_runtime (sub-gap 2: live read-back, via fakes) ----
#
# Cluster-free: fake the two API surfaces the helper touches — CustomObjectsApi
# (claim-GET -> bound Sandbox name; Sandbox-GET -> uid) and CoreV1Api
# (list_namespaced_pod -> backing Pods by owner uid) — so the read-back logic is
# asserted without the kubernetes client or a real cluster.

class _FakeOwner:
    def __init__(self, uid):
        self.uid = uid


class _FakeMeta:
    def __init__(self, name=None, owner_uids=()):
        self.name = name
        self.owner_references = [_FakeOwner(u) for u in owner_uids]


class _FakePodSpec:
    def __init__(self, runtime_class_name):
        self.runtime_class_name = runtime_class_name


class _FakePod:
    def __init__(self, name, owner_uid, runtime_class_name):
        self.metadata = _FakeMeta(name=name, owner_uids=(owner_uid,))
        self.spec = _FakePodSpec(runtime_class_name)


class _FakePodList:
    def __init__(self, pods):
        self.items = pods


class _FakeCore:
    def __init__(self, pods):
        self._pods = pods

    def list_namespaced_pod(self, namespace):
        return _FakePodList(self._pods)


class _FakeCustom:
    """Routes get_namespaced_custom_object by plural: claims vs sandboxes."""

    def __init__(self, claim_to_sbx, sbx_to_uid):
        self._claim_to_sbx = claim_to_sbx
        self._sbx_to_uid = sbx_to_uid

    def get_namespaced_custom_object(self, *, group, version, namespace, plural, name):
        if plural == cell._CLM_GVR[2]:
            sbx = self._claim_to_sbx.get(name)
            return {"status": {"sandbox": {"name": sbx}} if sbx else {}}
        if plural == cell._SBX_GVR[2]:
            return {"metadata": {"uid": self._sbx_to_uid.get(name)}}
        raise AssertionError(f"unexpected plural {plural!r}")


def test_verify_passes_when_all_backing_pods_are_gvisor():
    custom = _FakeCustom(
        claim_to_sbx={"c0": "sbx0", "c1": "sbx1"},
        sbx_to_uid={"sbx0": "u0", "sbx1": "u1"},
    )
    core = _FakeCore([
        _FakePod("pod0", "u0", "gvisor"),
        _FakePod("pod1", "u1", "gvisor"),
    ])
    verified = cell._verify_bound_pods_runtime(
        custom, core, bound_claim_names=["c0", "c1"],
        expected_runtime_class="gvisor",
    )
    assert verified == 2


def test_verify_raises_on_silent_runc_fallback():
    # a backing Pod with runtimeClassName None (node default = runc) -> crash-FAIL.
    custom = _FakeCustom(
        claim_to_sbx={"c0": "sbx0", "c1": "sbx1"},
        sbx_to_uid={"sbx0": "u0", "sbx1": "u1"},
    )
    core = _FakeCore([
        _FakePod("pod0", "u0", "gvisor"),
        _FakePod("pod1", "u1", None),   # silently fell back to runc
    ])
    raised = False
    try:
        cell._verify_bound_pods_runtime(
            custom, core, bound_claim_names=["c0", "c1"],
            expected_runtime_class="gvisor",
        )
    except RuntimeError as e:
        raised = True
        assert "sbx1" in str(e)
    assert raised


def test_verify_raises_when_backing_pod_unlocatable():
    # Sandbox has a uid but no Pod owns it -> isolation unverifiable -> crash-FAIL.
    custom = _FakeCustom(
        claim_to_sbx={"c0": "sbx0"},
        sbx_to_uid={"sbx0": "u0"},
    )
    core = _FakeCore([])   # no pods
    raised = False
    try:
        cell._verify_bound_pods_runtime(
            custom, core, bound_claim_names=["c0"],
            expected_runtime_class="gvisor",
        )
    except RuntimeError:
        raised = True
    assert raised


def test_verify_raises_when_claim_has_no_bound_sandbox():
    # a claim counted as bound but with no status.sandbox.name on re-read -> FAIL.
    custom = _FakeCustom(claim_to_sbx={"c0": None}, sbx_to_uid={})
    core = _FakeCore([])
    raised = False
    try:
        cell._verify_bound_pods_runtime(
            custom, core, bound_claim_names=["c0"],
            expected_runtime_class="gvisor",
        )
    except RuntimeError:
        raised = True
    assert raised


# ---- _assemble_probe_results (#3954: concurrent-probe flatten, pure) ----
#
# Walks the fired-claim list and flattens each claim's (ttfe_ms|None, exec_ok)
# into the two parallel lists the corroboration classifier consumes. One exec_oks
# entry PER CLAIM FIRED (attempt total). A latency sample is appended only when the
# probe returned a non-None ttfe_ms.

def test_assemble_absent_claim_counts_as_failed_attempt_no_sample():
    # a claim that never bound (absent from ttfe_results) drags exec_success_rate
    # as an attempted-never-executed False, and contributes NO latency sample.
    samples, oks = cell._assemble_probe_results(
        ["c0", "c1"], {"c0": (300.0, True)},
    )
    assert oks == [True, False]            # c1 absent -> False
    assert samples == [300.0]             # only c0 contributed a sample


def test_assemble_present_with_none_latency_is_failed_exec_no_sample():
    # bound but exec failed/blocked: (None, False) -> exec_ok False, no sample.
    samples, oks = cell._assemble_probe_results(
        ["c0", "c1"], {"c0": (300.0, True), "c1": (None, False)},
    )
    assert oks == [True, False]
    assert samples == [300.0]             # c1 has no honest latency


def test_assemble_present_with_latency_is_success_plus_sample():
    samples, oks = cell._assemble_probe_results(
        ["c0", "c1"], {"c0": (300.0, True), "c1": (700.0, True)},
    )
    assert oks == [True, True]
    assert sorted(samples) == [300.0, 700.0]


def test_assemble_empty_claim_list_is_empty():
    samples, oks = cell._assemble_probe_results([], {})
    assert oks == []
    assert samples == []


# ---- _classify_exec_corroboration (#3954: literal-TTFE, pure) ----
#
# ADDITIVE corroboration to the Ready+bound headline. Strictly-< the SAME sub-1s
# bar (create -> exec), plus exec_success_rate to disambiguate slow-vs-failed.

def test_corroboration_empty_attempts_emits_nothing():
    # no attempts -> nothing to corroborate -> {} (no fabricated number).
    assert cell._classify_exec_corroboration([], [], ttfi_ceiling_s=1.0) == {}


def test_corroboration_all_under_ceiling():
    corr = cell._classify_exec_corroboration(
        [300.0, 700.0], [True, True], ttfi_ceiling_s=1.0,
    )
    assert corr[_KEC] == 2.0
    assert corr[_KER] == 1.0


def test_corroboration_mixed_with_failed_exec():
    # 3 attempts, one exec failed (no sample); of the 2 samples, one is over 1s.
    corr = cell._classify_exec_corroboration(
        [300.0, 1500.0], [True, True, False], ttfi_ceiling_s=1.0,
    )
    assert corr[_KEC] == 1.0                       # only 300ms < 1000ms
    assert corr[_KER] == round(2 / 3, 4)          # 0.6667


def test_corroboration_ready_but_none_usable_still_emits_zero():
    # all samples over the bar: count 0 is a REAL "Ready but none usable" reading,
    # NOT empty -> the dict is still returned (attempts > 0).
    corr = cell._classify_exec_corroboration(
        [1200.0, 1800.0], [True, True], ttfi_ceiling_s=1.0,
    )
    assert corr[_KEC] == 0.0
    assert corr[_KER] == 1.0                       # both execs SUCCEEDED, just slow


def test_corroboration_ceiling_is_strict_less_than():
    # exactly 1000ms does NOT clear a 1.0s bar (mirrors the headline's strict <).
    corr = cell._classify_exec_corroboration(
        [1000.0, 999.0], [True, True], ttfi_ceiling_s=1.0,
    )
    assert corr[_KEC] == 1.0


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok: {fn.__name__}")
    print(f"test_burst_create: all {len(fns)} assertions passed")


if __name__ == "__main__":
    _run_all()
