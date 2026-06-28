"""Cluster-free tests for burst_create's pure classifier + manifest/vCPU helpers.

Dependency-free: `python3 test_burst_create.py` (exit 0 = pass). These assert the
load-bearing PASS/FAIL logic of the fleet headline cell — sandboxes_ready_under_1s
(the count), density_per_vcpu, and the ceil(K * ratio) pass gate — off fixtures,
so the headline metric is verified without a cluster or the kubernetes client.
"""

import math

import burst_create as cell

_KC = cell._KEY_COUNT       # "sandboxes_ready_under_1s"
_KD = cell._KEY_DENSITY     # "density_per_vcpu"


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


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok: {fn.__name__}")
    print(f"test_burst_create: all {len(fns)} assertions passed")


if __name__ == "__main__":
    _run_all()
