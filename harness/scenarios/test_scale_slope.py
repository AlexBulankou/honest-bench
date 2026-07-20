"""Cluster-free tests for scale_slope's pure linearity classifier + helpers.

Dependency-free: `python3 test_scale_slope.py` (exit 0 = pass). These assert the
load-bearing Scale Proof (Linearity Check) logic — density_retention / thpt_retention
emitted as a TOP-LEVEL scale_proof object — off fixtures, so the headline linearity
proof is verified without a cluster or the kubernetes client. Every number flows
through the LOCKED metrics.py functions, so these tests also pin that delegation.
"""

try:  # cwd == scenarios/ (dependency-free `python3 test_scale_slope.py`)
    import scale_slope as cell
except ModuleNotFoundError:  # repo-root pytest: scenarios/ is a package, not on sys.path
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import scale_slope as cell

_THR = 1000.0   # sub-1s ceiling (ms)
_WIN = 1.0      # 1.0s throughput window


def _point(node_count, max_concurrent, alloc_vcpu, ttfis):
    return {
        "node_count": node_count,
        "max_concurrent": max_concurrent,
        "allocatable_vcpu_per_node": alloc_vcpu,
        "ttfi_samples_ms": list(ttfis),
    }


# ---- flat sweep: both retentions 1.0 (the doc's "Holds Flat? Yes") ----

def test_flat_sweep_both_retentions_one():
    # density basis = total-ready / (vcpu_per_node × K). Linear: total-ready scales
    # with K, so per-vCPU density stays flat at 4/2.13 == 1.88 at every point; thpt
    # 4/1s/K stays flat per-node (8 under 1s on 2 nodes, 16 on 4 nodes -> 4/s/node).
    pts = [
        _point(1, 4, 2.13, [200.0, 300.0, 400.0, 500.0]),    # 4 / (2.13×1) = 1.88
        _point(2, 8, 2.13, [200.0] * 8),                     # 8 / (2.13×2) = 1.88
        _point(4, 16, 2.13, [200.0] * 16),                   # 16 / (2.13×4) = 1.88
    ]
    out = cell._classify_scale_slope(pts, threshold_ms=_THR, window_s=_WIN)
    assert out["density_retention"] == 1.0
    assert out["thpt_retention"] == 1.0
    assert [p["node_count"] for p in out["scale_points"]] == [1, 2, 4]
    assert out["scale_points"][0]["density"] == 1.88
    assert out["scale_points"][-1]["density"] == 1.88


# ---- declining sweep: retention < 1.0 (coordination overhead erodes density) ----

def test_declining_density_retention_below_one():
    # base K=1: 8 ready on (2.0×1) vCPU -> density 4.0/vCPU. A linear K=4 would put
    # 32 ready; coordination overhead delivers only 24 -> 24/(2.0×4) = 3.0/vCPU, so
    # per-node density eroded -> retention 3.0/4.0 = 0.75 (the doc's "⚠️ No").
    pts = [
        _point(1, 8, 2.0, [200.0] * 8),          # density 8/(2.0×1) = 4.0
        _point(4, 24, 2.0, [200.0] * 24),        # density 24/(2.0×4) = 3.0
    ]
    out = cell._classify_scale_slope(pts, threshold_ms=_THR, window_s=_WIN)
    assert out["scale_points"][0]["density"] == 4.0
    assert out["scale_points"][-1]["density"] == 3.0
    assert out["density_retention"] == 0.75


def test_declining_throughput_retention_below_one():
    # base K=1: all 8 under 1s -> thpt 8/1/1 = 8.0/node; max K=4: 32 ready (density
    # fine) but only 16 under the 1s bar (rest slow) -> thpt 16/1/4 = 4.0/node ->
    # thpt_retention 4.0/8.0 = 0.5.
    pts = [
        _point(1, 8, 2.0, [200.0] * 8),
        _point(4, 32, 2.0, [200.0] * 16 + [5000.0] * 16),
    ]
    out = cell._classify_scale_slope(pts, threshold_ms=_THR, window_s=_WIN)
    assert out["thpt_retention"] == cell.metrics.retention(8.0, 4.0)
    assert out["thpt_retention"] == 0.5
    # per-point throughput is emitted (not just the endpoint retention) so the render
    # side can draw a per-step throughput convergence subline.
    assert out["scale_points"][0]["throughput"] == 8.0
    assert out["scale_points"][-1]["throughput"] == 4.0


def test_scale_points_carry_per_point_throughput():
    # Each emitted point carries BOTH density and the per-node throughput it was measured
    # at, so a per-step throughput delta is reconstructable from scale_points alone.
    pts = [
        _point(1, 4, 2.13, [200.0, 300.0, 400.0, 500.0]),
        _point(2, 8, 2.13, [200.0] * 8),
        _point(4, 16, 2.13, [200.0] * 16),
    ]
    out = cell._classify_scale_slope(pts, threshold_ms=_THR, window_s=_WIN)
    for p in out["scale_points"]:
        assert "throughput" in p
        assert isinstance(p["throughput"], (int, float)) and p["throughput"] >= 0


# ---- emit-only-when-complete ----

def test_single_point_emits_empty():
    pts = [_point(1, 8, 2.0, [200.0] * 8)]
    assert cell._classify_scale_slope(pts, threshold_ms=_THR, window_s=_WIN) == {}


def test_empty_points_emits_empty():
    assert cell._classify_scale_slope([], threshold_ms=_THR, window_s=_WIN) == {}


def test_non_list_emits_empty():
    assert cell._classify_scale_slope(None, threshold_ms=_THR, window_s=_WIN) == {}
    assert cell._classify_scale_slope({"node_count": 1}, threshold_ms=_THR, window_s=_WIN) == {}


def test_degenerate_base_density_emits_empty():
    # base point delivered zero ready sandboxes -> density 0 -> retention undefined.
    pts = [
        _point(1, 0, 2.0, [None, None]),
        _point(4, 8, 2.0, [200.0] * 8),
    ]
    assert cell._classify_scale_slope(pts, threshold_ms=_THR, window_s=_WIN) == {}


# ---- honest-partial: thpt_retention omitted (not fabricated) on zero base thpt ----

def test_base_throughput_zero_omits_thpt_retention():
    # base point: 8 sandboxes Ready (density real) but NONE under the 1s bar ->
    # base throughput 0. density_retention is still emitted; thpt_retention is
    # omitted (render shows throughput pending, never a divide-by-zero lie).
    pts = [
        _point(1, 8, 2.0, [5000.0] * 8),          # all slow -> 0 under 1s
        _point(4, 16, 2.0, [200.0] * 16),
    ]
    out = cell._classify_scale_slope(pts, threshold_ms=_THR, window_s=_WIN)
    assert "scale_points" in out
    assert "density_retention" in out
    assert "thpt_retention" not in out
    assert out["scale_points"][0]["density"] == 4.0     # 8 / (2.0×1)
    assert out["scale_points"][-1]["density"] == 2.0    # 16 / (2.0×4)


# ---- ordering + dedup ----

def test_points_sorted_ascending_by_node_count():
    pts = [
        _point(4, 16, 2.0, [200.0] * 16),
        _point(1, 4, 2.0, [200.0] * 4),
        _point(2, 8, 2.0, [200.0] * 8),
    ]
    out = cell._classify_scale_slope(pts, threshold_ms=_THR, window_s=_WIN)
    assert [p["node_count"] for p in out["scale_points"]] == [1, 2, 4]


def test_duplicate_node_count_first_wins():
    pts = [
        _point(1, 4, 2.0, [200.0] * 4),
        _point(1, 99, 2.0, [200.0] * 99),     # dup K=1 -> ignored
        _point(2, 8, 2.0, [200.0] * 8),
    ]
    out = cell._classify_scale_slope(pts, threshold_ms=_THR, window_s=_WIN)
    assert [p["node_count"] for p in out["scale_points"]] == [1, 2]
    assert out["scale_points"][0]["density"] == 2.0   # 4/2.0, the first K=1 wins


# ---- malformed point filtering ----

def test_malformed_points_dropped_then_emit_check():
    # bool / non-int / <1 node_count and non-dict entries are dropped; if fewer than
    # 2 well-formed points survive, emit {}.
    pts = [
        _point(1, 4, 2.0, [200.0] * 4),
        "not-a-dict",
        _point(True, 8, 2.0, [200.0] * 8),    # bool node_count dropped
        _point(0, 8, 2.0, [200.0] * 8),       # node_count < 1 dropped
    ]
    # only the K=1 point survives -> {}
    assert cell._classify_scale_slope(pts, threshold_ms=_THR, window_s=_WIN) == {}


def test_two_wellformed_after_dropping_malformed():
    pts = [
        _point(1, 4, 2.0, [200.0] * 4),
        {"node_count": "x"},                  # non-int dropped
        _point(2, 8, 2.0, [200.0] * 8),
    ]
    out = cell._classify_scale_slope(pts, threshold_ms=_THR, window_s=_WIN)
    assert [p["node_count"] for p in out["scale_points"]] == [1, 2]


# ---- delegation to LOCKED metrics functions ----

def test_density_uses_locked_metric():
    pts = [
        _point(1, 9, 4.8, [200.0] * 9),       # 9/4.8 = 1.875 -> round 1.88
        _point(2, 18, 4.8, [200.0] * 18),
    ]
    out = cell._classify_scale_slope(pts, threshold_ms=_THR, window_s=_WIN)
    assert out["scale_points"][0]["density"] == cell.metrics.density_per_vcpu(9, 4.8)
    assert out["scale_points"][0]["density"] == 1.88


# ---- _build_template_manifest: runtimeClassName knob (mirror burst_create) ----

def test_template_omits_runtime_class_by_default():
    saved = cell._RUNTIME_CLASS
    try:
        cell._RUNTIME_CLASS = ""
        spec = cell._build_template_manifest("tmpl-x")["spec"]["podTemplate"]["spec"]
        assert "runtimeClassName" not in spec
        assert "tolerations" not in spec
    finally:
        cell._RUNTIME_CLASS = saved


def test_template_pins_runtime_class_when_set():
    saved = cell._RUNTIME_CLASS
    try:
        cell._RUNTIME_CLASS = "gvisor"
        spec = cell._build_template_manifest("tmpl-x")["spec"]["podTemplate"]["spec"]
        assert spec["runtimeClassName"] == "gvisor"
        assert any(
            t["key"] == "sandbox.gke.io/runtime"
            and t["operator"] == "Exists"
            and t["effect"] == "NoSchedule"
            for t in spec["tolerations"]
        )
    finally:
        cell._RUNTIME_CLASS = saved


# ---- _is_claim_ready_and_bound ----

def test_ready_and_bound_true_only_when_both():
    assert cell._is_claim_ready_and_bound(
        {"conditions": [{"type": "Ready", "status": "True"}],
         "sandbox": {"name": "sbx-1"}}) is True
    assert cell._is_claim_ready_and_bound(
        {"conditions": [{"type": "Ready", "status": "True"}]}) is False
    assert cell._is_claim_ready_and_bound(
        {"conditions": [{"type": "Ready", "status": "False"}],
         "sandbox": {"name": "sbx-1"}}) is False
    assert cell._is_claim_ready_and_bound({}) is False
    assert cell._is_claim_ready_and_bound(None) is False


# ---- gVisor-capable node counting (#3949) ----

def test_parse_label_selector_key_value_and_bare():
    assert cell._parse_label_selector("sandbox.gke.io/runtime=gvisor") == (
        "sandbox.gke.io/runtime", "gvisor")
    assert cell._parse_label_selector("sandbox.gke.io/runtime") == (
        "sandbox.gke.io/runtime", None)
    assert cell._parse_label_selector("  k = v  ") == ("k", "v")
    assert cell._parse_label_selector("") == (None, None)
    assert cell._parse_label_selector("   ") == (None, None)


def test_node_matches_presence_vs_exact():
    # value=None -> presence test (any value)
    assert cell._node_matches({"sandbox.gke.io/runtime": "gvisor"},
                              "sandbox.gke.io/runtime", None) is True
    assert cell._node_matches({"other": "x"}, "sandbox.gke.io/runtime", None) is False
    # value set -> exact match
    assert cell._node_matches({"sandbox.gke.io/runtime": "gvisor"},
                              "sandbox.gke.io/runtime", "gvisor") is True
    assert cell._node_matches({"sandbox.gke.io/runtime": "kata"},
                              "sandbox.gke.io/runtime", "gvisor") is False
    # missing key / empty key / None labels
    assert cell._node_matches({}, "sandbox.gke.io/runtime", "gvisor") is False
    assert cell._node_matches(None, "sandbox.gke.io/runtime", None) is False
    assert cell._node_matches({"a": "b"}, None, None) is False


# The exact sandbox-cluster shape the bug bit: 1 gVisor default-pool node + 2
# system-pool nodes. Counting total (3) lets k=2 pile 20 gVisor pods onto 1 node.
_ONE_GVISOR_TWO_SYSTEM = [
    {"sandbox.gke.io/runtime": "gvisor", "cloud.google.com/gke-nodepool": "default-pool"},
    {"cloud.google.com/gke-nodepool": "system-pool"},
    {"cloud.google.com/gke-nodepool": "system-pool"},
]


def test_count_capable_counts_only_gvisor_when_runtime_pinned():
    n = cell._count_capable_nodes(
        _ONE_GVISOR_TWO_SYSTEM, runtime_class="gvisor",
        gvisor_label="sandbox.gke.io/runtime=gvisor",
    )
    assert n == 1  # NOT 3 — only the default-pool node can host a gVisor sandbox


def test_count_capable_counts_all_when_no_runtime_class():
    # No runtimeClassName pinned -> every node can host the sandbox.
    n = cell._count_capable_nodes(
        _ONE_GVISOR_TWO_SYSTEM, runtime_class="",
        gvisor_label="sandbox.gke.io/runtime=gvisor",
    )
    assert n == 3


def test_count_capable_bare_key_presence_selector():
    n = cell._count_capable_nodes(
        _ONE_GVISOR_TWO_SYSTEM, runtime_class="gvisor",
        gvisor_label="sandbox.gke.io/runtime",  # bare key -> presence
    )
    assert n == 1


def test_count_capable_value_mismatch_excludes():
    # A node labeled with a DIFFERENT runtime value must not count toward gvisor.
    nodes = [
        {"sandbox.gke.io/runtime": "gvisor"},
        {"sandbox.gke.io/runtime": "kata"},
        {"sandbox.gke.io/runtime": "gvisor"},
    ]
    n = cell._count_capable_nodes(
        nodes, runtime_class="gvisor", gvisor_label="sandbox.gke.io/runtime=gvisor")
    assert n == 2


def test_count_capable_empty_and_none():
    assert cell._count_capable_nodes(
        [], runtime_class="gvisor", gvisor_label="sandbox.gke.io/runtime=gvisor") == 0
    assert cell._count_capable_nodes(
        None, runtime_class="gvisor", gvisor_label="sandbox.gke.io/runtime=gvisor") == 0
    assert cell._count_capable_nodes(
        None, runtime_class="", gvisor_label="x") == 0


# ---- density denominator: gVisor-capable subset, not the whole pool [#3949] ----
# The exact bug caught in cluster prep: min() over ALL nodes picks the 2-vCPU
# untainted system node, so density_per_vcpu = slots/(1.93*K) — ~8x inflated — even
# though sandboxes only ever land on the 16-vCPU gVisor default-pool node.
_GVISOR16_SYSTEM2 = [
    ({"sandbox.gke.io/runtime": "gvisor", "cloud.google.com/gke-nodepool": "default-pool"}, 16.0),
    ({"cloud.google.com/gke-nodepool": "system-pool"}, 2.0),
]


def test_min_capable_vcpu_picks_gvisor_node_when_runtime_pinned():
    v = cell._min_capable_vcpu(
        _GVISOR16_SYSTEM2, runtime_class="gvisor",
        gvisor_label="sandbox.gke.io/runtime=gvisor",
    )
    assert v == 16.0  # NOT 2.0 — the system node never hosts a gVisor sandbox


def test_min_capable_vcpu_min_over_all_when_no_runtime_class():
    # No runtimeClassName pinned -> every node is capable, basis is the smallest.
    v = cell._min_capable_vcpu(
        _GVISOR16_SYSTEM2, runtime_class="",
        gvisor_label="sandbox.gke.io/runtime=gvisor",
    )
    assert v == 2.0


def test_min_capable_vcpu_min_over_capable_subset():
    # Two gVisor nodes of differing size -> the smallest CAPABLE node bounds it.
    specs = [
        ({"sandbox.gke.io/runtime": "gvisor"}, 16.0),
        ({"sandbox.gke.io/runtime": "gvisor"}, 8.0),
        ({"cloud.google.com/gke-nodepool": "system-pool"}, 2.0),
    ]
    v = cell._min_capable_vcpu(
        specs, runtime_class="gvisor", gvisor_label="sandbox.gke.io/runtime=gvisor")
    assert v == 8.0


def test_min_capable_vcpu_value_mismatch_excluded():
    # A kata-labeled node must not bound a gvisor sweep's denominator.
    specs = [
        ({"sandbox.gke.io/runtime": "gvisor"}, 16.0),
        ({"sandbox.gke.io/runtime": "kata"}, 4.0),
    ]
    v = cell._min_capable_vcpu(
        specs, runtime_class="gvisor", gvisor_label="sandbox.gke.io/runtime=gvisor")
    assert v == 16.0


def test_min_capable_vcpu_zero_when_no_capable_node():
    # runtime pinned but no gVisor node -> 0.0 sentinel (caller falls back / omits).
    specs = [({"cloud.google.com/gke-nodepool": "system-pool"}, 2.0)]
    v = cell._min_capable_vcpu(
        specs, runtime_class="gvisor", gvisor_label="sandbox.gke.io/runtime=gvisor")
    assert v == 0.0


def test_min_capable_vcpu_zero_on_empty_and_none():
    assert cell._min_capable_vcpu(
        [], runtime_class="gvisor", gvisor_label="sandbox.gke.io/runtime=gvisor") == 0.0
    assert cell._min_capable_vcpu(
        None, runtime_class="", gvisor_label="x") == 0.0


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok: {fn.__name__}")
    print(f"test_scale_slope: all {len(fns)} assertions passed")


if __name__ == "__main__":
    _run_all()
