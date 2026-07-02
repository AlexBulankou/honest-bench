"""Offline tests for density_probe (pure core only — no cluster, no kubernetes client).

Run from the scenarios/ dir: python3 test_density_probe.py
"""

try:  # package context
    from . import density_probe as dp
    from . import runtime_class as rc
    from ._apiversion import ext_api_version
except ImportError:  # standalone
    import density_probe as dp
    import runtime_class as rc
    from _apiversion import ext_api_version


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def _node(name, *, labels=None, cpu="15890m", pods="110", unschedulable=False):
    n = {
        "metadata": {"name": name, "labels": labels or {}},
        "status": {"allocatable": {"cpu": cpu, "pods": pods}},
    }
    if unschedulable:
        n["spec"] = {"unschedulable": True}
    return n


_GVISOR_LABELS = {"sandbox.gke.io/runtime": "gvisor"}
_KATA_LABELS = {"nested-virtualization": "enabled"}


def _pod(
    *,
    labeled=True,
    node_name="node-a",
    phase="Running",
    ready=True,
    runtime_class="gvisor",
    scheduled_false_msg=None,
    hostname_pin=None,
):
    pod = {
        "metadata": {"labels": ({dp._PROBE_LABEL_KEY: dp._PROBE_LABEL_VALUE}
                                if labeled else {})},
        "spec": {"nodeName": node_name, "runtimeClassName": runtime_class},
        "status": {"phase": phase, "conditions": []},
    }
    if hostname_pin:
        pod["spec"]["nodeSelector"] = {"kubernetes.io/hostname": hostname_pin}
    if ready:
        pod["status"]["conditions"].append({"type": "Ready", "status": "True"})
    if scheduled_false_msg is not None:
        pod["spec"].pop("nodeName", None)
        pod["status"]["conditions"].append({
            "type": "PodScheduled",
            "status": "False",
            "reason": "Unschedulable",
            "message": scheduled_false_msg,
        })
    return pod


# ---------------------------------------------------------------------------
# parse_cpu_quantity
# ---------------------------------------------------------------------------

def test_parse_cpu_quantity_millis():
    assert dp.parse_cpu_quantity("15890m") == 15.89


def test_parse_cpu_quantity_whole():
    assert dp.parse_cpu_quantity("16") == 16.0


def test_parse_cpu_quantity_nano_micro():
    assert abs(dp.parse_cpu_quantity("500000000n") - 0.5) < 1e-9
    assert abs(dp.parse_cpu_quantity("1500000u") - 1.5) < 1e-9


def test_parse_cpu_quantity_empty_raises():
    for bad in ("", "   ", None):
        try:
            dp.parse_cpu_quantity(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for {bad!r}")


def test_parse_cpu_quantity_garbage_raises():
    try:
        dp.parse_cpu_quantity("lots")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for 'lots'")


# ---------------------------------------------------------------------------
# select_target_node
# ---------------------------------------------------------------------------

def test_select_target_node_picks_capability_label():
    nodes = [
        _node("plain-a"),
        _node("gv-1", labels=_GVISOR_LABELS, cpu="3920m", pods="110"),
    ]
    got = dp.select_target_node(nodes, "gvisor")
    assert got["name"] == "gv-1"
    assert got["allocatable_vcpu"] == 3.92
    assert got["allocatable_pods"] == 110


def test_select_target_node_kata_family_resolution():
    # kata-clh resolves to the kata FAMILY marker (nested-virtualization=enabled)
    nodes = [_node("kata-1", labels=_KATA_LABELS)]
    got = dp.select_target_node(nodes, "kata-clh")
    assert got["name"] == "kata-1"


def test_select_target_node_skips_unschedulable():
    nodes = [
        _node("gv-cordoned", labels=_GVISOR_LABELS, unschedulable=True),
        _node("gv-ok", labels=_GVISOR_LABELS),
    ]
    assert dp.select_target_node(nodes, "gvisor")["name"] == "gv-ok"


def test_select_target_node_override_honored():
    nodes = [
        _node("gv-1", labels=_GVISOR_LABELS),
        _node("gv-2", labels=_GVISOR_LABELS),
    ]
    assert dp.select_target_node(nodes, "gvisor", "gv-2")["name"] == "gv-2"


def test_select_target_node_override_absent_raises():
    nodes = [_node("gv-1", labels=_GVISOR_LABELS)]
    try:
        dp.select_target_node(nodes, "gvisor", "no-such-node")
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected RuntimeError for absent override")


def test_select_target_node_no_capable_node_raises():
    try:
        dp.select_target_node([_node("plain-a")], "gvisor")
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected RuntimeError with no capable node")


def test_select_target_node_unknown_runtime_raises():
    try:
        dp.select_target_node([_node("n")], "runc-mystery")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for unmarked runtime")


# ---------------------------------------------------------------------------
# manifest builders
# ---------------------------------------------------------------------------

def test_template_manifest_hostname_pin_survives_kata_merge():
    # kata's scheduling profile carries its own nodeSelector; the caller's
    # hostname pin must survive the merge (setdefault contract).
    m = dp.build_probe_template_manifest(
        "t1", node_name="kata-1", runtime_class="kata", image="busybox:1.36",
    )
    spec = m["spec"]["podTemplate"]["spec"]
    assert spec["nodeSelector"]["kubernetes.io/hostname"] == "kata-1"
    assert spec["nodeSelector"]["nested-virtualization"] == "enabled"
    assert spec["runtimeClassName"] == "kata"


def test_template_manifest_gvisor_shape():
    m = dp.build_probe_template_manifest(
        "t1", node_name="gv-1", runtime_class="gvisor", image="busybox:1.36",
    )
    assert m["apiVersion"] == ext_api_version()
    assert m["kind"] == "SandboxTemplate"
    spec = m["spec"]["podTemplate"]["spec"]
    assert spec["nodeSelector"] == {"kubernetes.io/hostname": "gv-1"}
    assert spec["runtimeClassName"] == "gvisor"
    tol_keys = {t["key"] for t in spec["tolerations"]}
    assert "sandbox.gke.io/runtime" in tol_keys
    # resources come from the shared per-family env helper
    assert spec["containers"][0]["resources"] == rc.container_resources_from_env("gvisor")


def test_template_manifest_probe_label_on_pod_template():
    m = dp.build_probe_template_manifest(
        "t1", node_name="gv-1", runtime_class="gvisor", image="busybox:1.36",
    )
    tpl_labels = m["spec"]["podTemplate"]["metadata"]["labels"]
    assert tpl_labels[dp._PROBE_LABEL_KEY] == dp._PROBE_LABEL_VALUE
    assert m["metadata"]["labels"][dp._PROBE_LABEL_KEY] == dp._PROBE_LABEL_VALUE


def test_warmpool_manifest_shape():
    m = dp.build_probe_warmpool_manifest("p1", "t1", 120)
    assert m["kind"] == "SandboxWarmPool"
    assert m["apiVersion"] == ext_api_version()
    assert m["spec"] == {"replicas": 120, "sandboxTemplateRef": {"name": "t1"}}


# ---------------------------------------------------------------------------
# is_probe_pod
# ---------------------------------------------------------------------------

def test_is_probe_pod_label_primary():
    assert dp.is_probe_pod(_pod(labeled=True), node_name="node-a")


def test_is_probe_pod_hostname_fallback():
    pod = _pod(labeled=False, hostname_pin="node-a")
    assert dp.is_probe_pod(pod, node_name="node-a")


def test_is_probe_pod_rejects_unrelated():
    assert not dp.is_probe_pod(_pod(labeled=False), node_name="node-a")
    # hostname pin to a DIFFERENT node is not ours either
    assert not dp.is_probe_pod(
        _pod(labeled=False, hostname_pin="node-b"), node_name="node-a",
    )


# ---------------------------------------------------------------------------
# count_target_pods
# ---------------------------------------------------------------------------

def test_count_target_pods_ready_on_target():
    pods = [_pod(), _pod(), _pod()]
    got = dp.count_target_pods(pods, node_name="node-a", runtime_class="gvisor")
    assert got["ready_on_target"] == 3
    assert got["wrong_node"] == got["wrong_runtime"] == got["other"] == 0


def test_count_target_pods_wrong_node_and_runtime_bucketed():
    pods = [
        _pod(),
        _pod(node_name="node-b"),          # Ready but escaped the pin
        _pod(runtime_class="runc"),         # Ready but wrong runtime
    ]
    got = dp.count_target_pods(pods, node_name="node-a", runtime_class="gvisor")
    assert got["ready_on_target"] == 1
    assert got["wrong_node"] == 1
    assert got["wrong_runtime"] == 1


def test_count_target_pods_collects_unschedulable_messages():
    pods = [
        _pod(),
        _pod(phase="Pending", ready=False,
             scheduled_false_msg="0/3 nodes are available: 1 Too many pods, ..."),
    ]
    got = dp.count_target_pods(pods, node_name="node-a", runtime_class="gvisor")
    assert got["ready_on_target"] == 1
    assert len(got["pending_unschedulable_msgs"]) == 1
    assert "Too many pods" in got["pending_unschedulable_msgs"][0]


def test_count_target_pods_ignores_foreign_pods():
    pods = [_pod(labeled=False)]  # not ours (no label, no pin)
    got = dp.count_target_pods(pods, node_name="node-a", runtime_class="gvisor")
    assert got == {
        "ready_on_target": 0,
        "pending_unschedulable_msgs": [],
        "wrong_node": 0,
        "wrong_runtime": 0,
        "other": 0,
    }


def test_count_target_pods_running_not_ready_is_other():
    pods = [_pod(ready=False)]
    got = dp.count_target_pods(pods, node_name="node-a", runtime_class="gvisor")
    assert got["ready_on_target"] == 0
    assert got["other"] == 1


# ---------------------------------------------------------------------------
# classify_binding_constraints
# ---------------------------------------------------------------------------

def test_classify_positive_token_wins_over_selector_mismatch():
    # the real aggregate shape: the pinned node's true reason PLUS other
    # nodes' selector mismatches (the hostname pin working as designed)
    msgs = ["0/4 nodes are available: 1 Too many pods, "
            "3 node(s) didn't match Pod's node affinity/selector."]
    assert dp.classify_binding_constraints(msgs) == ["max-pods"]


def test_classify_multiple_distinct_classes():
    msgs = [
        "0/4 nodes: 1 Too many pods, 3 node(s) didn't match node selector",
        "0/4 nodes: 1 Insufficient cpu, 3 node(s) didn't match node selector",
    ]
    assert dp.classify_binding_constraints(msgs) == ["max-pods", "insufficient-cpu"]


def test_classify_selector_mismatch_only_when_no_positive():
    msgs = ["0/4 nodes are available: 4 node(s) didn't match Pod's node affinity/selector."]
    assert dp.classify_binding_constraints(msgs) == ["node-selector-mismatch"]


def test_classify_unknown_fallbacks():
    assert dp.classify_binding_constraints([]) == ["unknown"]
    assert dp.classify_binding_constraints(["something else entirely"]) == ["unknown"]


def test_classify_insufficient_memory():
    assert dp.classify_binding_constraints(
        ["0/4: 1 Insufficient memory"]) == ["insufficient-memory"]


# ---------------------------------------------------------------------------
# saturation_verdict
# ---------------------------------------------------------------------------

def test_verdict_all_ready_ceiling():
    assert dp.saturation_verdict(
        [50, 100, 120], hold_checks=3, pending_unschedulable=0, replicas=120,
    ) == "all-ready-ceiling"


def test_verdict_saturated():
    assert dp.saturation_verdict(
        [80, 104, 104, 104], hold_checks=3, pending_unschedulable=16, replicas=120,
    ) == "saturated"


def test_verdict_in_progress_while_climbing():
    assert dp.saturation_verdict(
        [10, 40, 80], hold_checks=3, pending_unschedulable=40, replicas=120,
    ) == "in-progress"


def test_verdict_in_progress_without_backlog():
    # plateau but NO unschedulable backlog -> demand not provably exceeded
    assert dp.saturation_verdict(
        [104, 104, 104], hold_checks=3, pending_unschedulable=0, replicas=120,
    ) == "in-progress"


def test_verdict_in_progress_zero_plateau():
    # a stuck-at-zero plateau is never saturation
    assert dp.saturation_verdict(
        [0, 0, 0, 0], hold_checks=3, pending_unschedulable=120, replicas=120,
    ) == "in-progress"


def test_verdict_in_progress_short_history():
    assert dp.saturation_verdict(
        [104, 104], hold_checks=3, pending_unschedulable=16, replicas=120,
    ) == "in-progress"


def test_verdict_empty_history():
    assert dp.saturation_verdict(
        [], hold_checks=2, pending_unschedulable=0, replicas=120,
    ) == "in-progress"


# ---------------------------------------------------------------------------
# assemble_density_report
# ---------------------------------------------------------------------------

_REPORT_KW = dict(
    max_concurrent=104,
    allocatable_vcpu=15.89,
    allocatable_pods=110,
    node_name="gv-1",
    runtime_class="gvisor",
    constraints=["max-pods"],
    pod_resources={"requests": {"cpu": "10m"}},
    counts={"ready_on_target": 104, "pending_unschedulable_msgs": 16,
            "wrong_node": 0, "wrong_runtime": 0, "other": 0},
    poll_history=[80, 104, 104, 104],
)


def test_report_saturated_exports_density_and_env():
    r = dp.assemble_density_report(verdict="saturated", **_REPORT_KW)
    assert r["max_concurrent"] == 104
    # LOCKED core: 104 / 15.89 rounded to 2 decimals
    assert r["density_per_vcpu"] == round(104 / 15.89, 2)
    assert r["canonical_fire_env"] == {
        "BENCH_DENSITY_MAX_CONCURRENT": "104",
        "BENCH_DENSITY_ALLOCATABLE_VCPU_PER_NODE": "15.89",
    }
    assert r["binding_constraints"] == ["max-pods"]


def test_report_non_saturated_never_leaks_density():
    for verdict in ("all-ready-ceiling", "timeout", "in-progress"):
        r = dp.assemble_density_report(verdict=verdict, **_REPORT_KW)
        assert "density_per_vcpu" not in r
        assert "canonical_fire_env" not in r
        assert "max_concurrent" not in r
        assert "binding_constraints" not in r
        assert r["verdict"] == verdict


def test_report_carries_reproducibility_context():
    r = dp.assemble_density_report(verdict="saturated", **_REPORT_KW)
    assert r["pod_resources"] == {"requests": {"cpu": "10m"}}
    assert r["allocatable_vcpu"] == 15.89
    assert r["allocatable_pods"] == 110
    assert r["node"] == "gv-1"
    assert r["runtime_class"] == "gvisor"
    assert r["poll_history_tail"] == [80, 104, 104, 104]


def test_report_poll_history_tail_capped_at_20():
    kw = dict(_REPORT_KW)
    kw["poll_history"] = list(range(50))
    r = dp.assemble_density_report(verdict="saturated", **kw)
    assert r["poll_history_tail"] == list(range(30, 50))


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------

def _run_all():
    import sys

    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok: {name}")
            except Exception as e:  # noqa: BLE001
                failures += 1
                print(f"FAIL: {name}: {e!r}")
    if failures:
        print(f"{failures} failure(s)")
        sys.exit(1)
    print("all density_probe tests passed")


if __name__ == "__main__":
    _run_all()
