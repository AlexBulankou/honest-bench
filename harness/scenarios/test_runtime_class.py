"""Cluster-free tests for runtime_class's pure pin + verify core.

Dependency-free: `python3 test_runtime_class.py` (exit 0 = pass). Everything that
decides manifest shape or runtime-headline honesty is pure and pinned here — the
scheduling resolver (`resolve_scheduling`), the in-place pinner (`apply_runtime_class`,
including its default-off no-op and its merge-not-clobber semantics), the substrate
consistency guard (`assert_substrate_runtime_consistency`), and the bound-pod violation
classifier (`classify_runtime_violations` / `assert_no_runtime_violations`). The one
I/O surface (`verify_bound_pod_runtimes`) touches a cluster and is exercised live by
the matrix scenarios on a4s1's armed fire; here we pin the pure verdict it delegates to
so a runc Pod can never publish under a gVisor/Kata banner. Its owner-uid Sandbox->Pod
WALK (the logic between the two API reads — the part that decides which Pod backs which
Sandbox and can silently shrink the violation set) is itself covered offline with
in-memory fake clients, so a regression in the walk fails here rather than only on the
armed fire.
"""

try:  # cwd == scenarios/ (dependency-free `python3 test_runtime_class.py`)
    import runtime_class as rc
except ModuleNotFoundError:  # repo-root pytest: scenarios/ is a package, not on sys.path
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import runtime_class as rc


def _assert_raises(fn):
    try:
        fn()
    except RuntimeError:
        return
    raise AssertionError("expected RuntimeError, none raised")


# ---- resolve_scheduling: runtime_class -> (tolerations, node_selector) ----

def test_resolve_empty_is_noop():
    assert rc.resolve_scheduling("") == ([], {})


def test_resolve_gvisor_taint_toleration_no_selector():
    tols, sel = rc.resolve_scheduling(rc.GVISOR)
    assert sel == {}
    assert len(tols) == 1
    t = tols[0]
    assert t["key"] == "sandbox.gke.io/runtime"
    assert t["operator"] == "Exists"
    assert t["effect"] == "NoSchedule"


def test_resolve_kata_taint_toleration_and_node_selector():
    tols, sel = rc.resolve_scheduling(rc.KATA)
    assert sel == {"nested-virtualization": "enabled"}
    assert len(tols) == 1
    assert tols[0]["key"] == "sandbox.gke.io/kata"
    assert tols[0]["operator"] == "Exists"
    assert tols[0]["effect"] == "NoSchedule"


def test_resolve_unknown_is_noop():
    # An unknown non-empty class (no known family prefix) invents no scheduling —
    # runtimeClassName only. NB: "kata-*"/"gvisor-*" are NOT unknown (they normalize to a
    # family, covered below), so the unknown probe must be a genuinely-foreign runtime.
    assert rc.resolve_scheduling("firecracker") == ([], {})


# ---- runtime_family: concrete hypervisor variant -> scheduling/profile family ----

def test_family_bare_names_map_to_self():
    assert rc.runtime_family(rc.KATA) == "kata"
    assert rc.runtime_family(rc.GVISOR) == "gvisor"


def test_family_kata_hypervisor_variants_normalize_to_kata():
    # kata-deploy installs per-hypervisor RuntimeClasses; all share the nested-virt pool.
    assert rc.runtime_family("kata-clh") == "kata"
    assert rc.runtime_family("kata-qemu") == "kata"


def test_family_gvisor_variants_normalize_to_gvisor():
    assert rc.runtime_family("gvisor-experimental") == "gvisor"


def test_family_empty_is_empty():
    assert rc.runtime_family("") == ""


def test_family_unknown_is_itself():
    # A runtime with no known family prefix is its own family (no profile, no rule).
    assert rc.runtime_family("firecracker") == "firecracker"
    assert rc.runtime_family("runc") == "runc"


def test_resolve_kata_clh_resolves_kata_profile():
    # The load-bearing fix: a4s1 installs kata-clh, so resolve_scheduling("kata-clh")
    # MUST return the kata profile (toleration + nested-virt nodeSelector), not ([], {}).
    tols, sel = rc.resolve_scheduling("kata-clh")
    assert sel == {"nested-virtualization": "enabled"}
    assert len(tols) == 1 and tols[0]["key"] == "sandbox.gke.io/kata"


def test_resolve_kata_qemu_resolves_kata_profile():
    tols, sel = rc.resolve_scheduling("kata-qemu")
    assert sel == {"nested-virtualization": "enabled"}
    assert tols[0]["key"] == "sandbox.gke.io/kata"


def test_resolve_gvisor_experimental_resolves_gvisor_profile():
    tols, sel = rc.resolve_scheduling("gvisor-experimental")
    assert sel == {}
    assert len(tols) == 1 and tols[0]["key"] == "sandbox.gke.io/runtime"


def test_apply_kata_clh_pins_concrete_class_with_family_scheduling():
    # runtimeClassName is the CONCRETE hypervisor (kata-clh), but scheduling is the
    # kata family's — so the pod lands on the nested-virt pool AND the headline names
    # the exact hypervisor measured.
    out = rc.apply_runtime_class(_base_spec(), "kata-clh")
    assert out["runtimeClassName"] == "kata-clh"  # concrete, not normalized
    keys = {t["key"] for t in out["tolerations"]}
    assert "sandbox.gke.io/kata" in keys
    assert out["nodeSelector"] == {"nested-virtualization": "enabled"}


def test_resolve_returns_copies_not_shared_registry():
    # Mutating the result must not corrupt the shared profile.
    tols1, sel1 = rc.resolve_scheduling(rc.KATA)
    tols1[0]["key"] = "TAMPERED"
    sel1["nested-virtualization"] = "TAMPERED"
    tols2, sel2 = rc.resolve_scheduling(rc.KATA)
    assert tols2[0]["key"] == "sandbox.gke.io/kata"
    assert sel2["nested-virtualization"] == "enabled"


# ---- apply_runtime_class: default-off no-op + merge-not-clobber ----

def _base_spec():
    return {
        "containers": [{"name": "sandbox", "image": "busybox:1.36"}],
        "restartPolicy": "Never",
    }


def test_apply_empty_is_byte_identical():
    # The default-off contract: unset knob -> manifest unchanged (vanilla-kind safe).
    import copy
    spec = _base_spec()
    before = copy.deepcopy(spec)
    out = rc.apply_runtime_class(spec, "")
    assert out is spec  # in place
    assert out == before  # no runtimeClassName / tolerations / nodeSelector added
    assert "runtimeClassName" not in out
    assert "tolerations" not in out
    assert "nodeSelector" not in out


def test_apply_gvisor_sets_class_and_toleration_no_selector():
    out = rc.apply_runtime_class(_base_spec(), rc.GVISOR)
    assert out["runtimeClassName"] == "gvisor"
    keys = {t["key"] for t in out["tolerations"]}
    assert "sandbox.gke.io/runtime" in keys
    assert "nodeSelector" not in out  # gVisor needs no node label


def test_apply_kata_sets_class_toleration_and_selector():
    out = rc.apply_runtime_class(_base_spec(), rc.KATA)
    assert out["runtimeClassName"] == "kata"
    keys = {t["key"] for t in out["tolerations"]}
    assert "sandbox.gke.io/kata" in keys
    assert out["nodeSelector"] == {"nested-virtualization": "enabled"}


def test_apply_does_not_duplicate_existing_toleration_key():
    spec = _base_spec()
    spec["tolerations"] = [
        {"key": "sandbox.gke.io/runtime", "operator": "Exists", "effect": "NoSchedule"},
    ]
    out = rc.apply_runtime_class(spec, rc.GVISOR)
    keys = [t["key"] for t in out["tolerations"]]
    assert keys.count("sandbox.gke.io/runtime") == 1  # not duplicated


def test_apply_preserves_caller_tolerations_and_appends():
    spec = _base_spec()
    spec["tolerations"] = [{"key": "other", "operator": "Exists"}]
    out = rc.apply_runtime_class(spec, rc.KATA)
    keys = {t["key"] for t in out["tolerations"]}
    assert "other" in keys  # caller's toleration kept
    assert "sandbox.gke.io/kata" in keys  # runtime toleration appended


def test_apply_caller_node_selector_wins_on_conflict():
    spec = _base_spec()
    spec["nodeSelector"] = {"nested-virtualization": "caller-pin", "zone": "us"}
    out = rc.apply_runtime_class(spec, rc.KATA)
    # caller's explicit pin is not overwritten; unrelated keys preserved.
    assert out["nodeSelector"]["nested-virtualization"] == "caller-pin"
    assert out["nodeSelector"]["zone"] == "us"


# ---- assert_substrate_runtime_consistency: false-headline guard ----

def test_consistency_gke_sandbox_requires_gvisor_ok():
    rc.assert_substrate_runtime_consistency("gke-sandbox", rc.GVISOR)  # no raise


def test_consistency_gke_sandbox_unset_runtime_raises():
    _assert_raises(lambda: rc.assert_substrate_runtime_consistency("gke-sandbox", ""))


def test_consistency_gke_sandbox_kata_raises():
    # gke-sandbox banner claims gVisor; pinning kata under it is a false headline.
    _assert_raises(lambda: rc.assert_substrate_runtime_consistency("gke-sandbox", rc.KATA))


def test_consistency_unconstrained_substrates_never_raise():
    for sub in ("kind", "gke", "anything"):
        for cls in ("", rc.GVISOR, rc.KATA):
            rc.assert_substrate_runtime_consistency(sub, cls)  # no rule => no raise


def test_consistency_gke_kata_requires_kata_ok():
    rc.assert_substrate_runtime_consistency("gke-kata", rc.KATA)  # no raise


def test_consistency_gke_kata_unset_runtime_raises():
    _assert_raises(lambda: rc.assert_substrate_runtime_consistency("gke-kata", ""))


def test_consistency_gke_kata_gvisor_raises():
    # gke-kata banner claims Kata; pinning gvisor under it is a false headline.
    _assert_raises(lambda: rc.assert_substrate_runtime_consistency("gke-kata", rc.GVISOR))


def test_consistency_gke_kata_kata_clh_ok():
    # The isolation claim is the FAMILY: a gke-kata banner is satisfied by ANY kata
    # hypervisor pin (kata-clh / kata-qemu) — this is the a4s1-install case.
    rc.assert_substrate_runtime_consistency("gke-kata", "kata-clh")  # no raise
    rc.assert_substrate_runtime_consistency("gke-kata", "kata-qemu")  # no raise


def test_consistency_gke_sandbox_gvisor_experimental_ok():
    rc.assert_substrate_runtime_consistency("gke-sandbox", "gvisor-experimental")  # no raise


def test_consistency_gke_kata_gvisor_variant_raises():
    # A gvisor-family pin under a gke-kata banner is still a false headline.
    _assert_raises(
        lambda: rc.assert_substrate_runtime_consistency("gke-kata", "gvisor-experimental")
    )


# ---- required_runtime_for_substrate: the shared substrate->runtime source of truth ----

def test_required_runtime_gke_sandbox_is_gvisor():
    assert rc.required_runtime_for_substrate("gke-sandbox") == rc.GVISOR


def test_required_runtime_gke_kata_is_kata():
    assert rc.required_runtime_for_substrate("gke-kata") == rc.KATA


def test_required_runtime_unconstrained_is_none():
    # kind/gke/unknown make no isolation claim -> None -> producer skips the read-back.
    for sub in ("kind", "gke", "anything", ""):
        assert rc.required_runtime_for_substrate(sub) is None


def test_required_runtime_agrees_with_consistency_guard():
    # The verify-gate and the consistency guard share one source: for every ruled
    # substrate, pinning the required runtime is consistent and anything else raises.
    for sub in ("gke-sandbox", "gke-kata"):
        req = rc.required_runtime_for_substrate(sub)
        assert req is not None
        rc.assert_substrate_runtime_consistency(sub, req)  # required pin => no raise
        other = rc.KATA if req == rc.GVISOR else rc.GVISOR
        _assert_raises(lambda: rc.assert_substrate_runtime_consistency(sub, other))


# ---- classify_runtime_violations: observed pairs -> violations ----

def test_classify_all_match_is_clean():
    observed = [("sbx-a", "gvisor"), ("sbx-b", "gvisor")]
    assert rc.classify_runtime_violations(observed, "gvisor") == []


def test_classify_none_backing_is_violation():
    observed = [("sbx-a", "gvisor"), ("sbx-b", None)]
    v = rc.classify_runtime_violations(observed, "gvisor")
    assert len(v) == 1 and "sbx-b" in v[0]


def test_classify_mismatch_is_violation():
    observed = [("sbx-a", "runc")]
    v = rc.classify_runtime_violations(observed, "gvisor")
    assert len(v) == 1 and "runc" in v[0]


def test_classify_mixed_collects_all_violations():
    observed = [("a", "gvisor"), ("b", "runc"), ("c", None), ("d", "gvisor")]
    v = rc.classify_runtime_violations(observed, "gvisor")
    assert len(v) == 2  # b (runc) + c (None); a/d clean


def test_classify_kata_expected():
    observed = [("a", "kata"), ("b", "runc")]
    v = rc.classify_runtime_violations(observed, "kata")
    assert len(v) == 1 and "runc" in v[0]


def test_classify_verification_stays_exact_across_hypervisors():
    # THE honesty property: scheduling + the substrate claim normalize to the family, but
    # the bound-Pod read-back is EXACT. A Pod pinned kata-clh that fell back to kata-qemu
    # is the SAME family yet a DIFFERENT hypervisor — it MUST be a violation, because the
    # published headline names kata-clh and we measured kata-qemu.
    observed = [("a", "kata-clh"), ("b", "kata-qemu")]
    v = rc.classify_runtime_violations(observed, "kata-clh")
    assert len(v) == 1 and "kata-qemu" in v[0]  # b violates; a (exact match) clean


# ---- assert_no_runtime_violations: clean -> count; dirty -> raise ----

def test_assert_clean_returns_count():
    observed = [("a", "gvisor"), ("b", "gvisor"), ("c", "gvisor")]
    assert rc.assert_no_runtime_violations(observed, "gvisor") == 3


def test_assert_empty_returns_zero():
    assert rc.assert_no_runtime_violations([], "gvisor") == 0


def test_assert_dirty_raises():
    observed = [("a", "gvisor"), ("b", "runc")]
    _assert_raises(lambda: rc.assert_no_runtime_violations(observed, "gvisor"))


def test_assert_raise_message_names_runtime_and_counts():
    observed = [("a", "runc"), ("b", None)]
    try:
        rc.assert_no_runtime_violations(observed, "gvisor")
    except RuntimeError as exc:
        msg = str(exc)
        assert "gvisor" in msg
        assert "2/2" in msg  # both bound sandboxes violated
        return
    raise AssertionError("expected RuntimeError")


# ---- verify_bound_pod_runtimes: owner-uid Sandbox->Pod walk (fake clients) ----
# The two API reads are faked; what is under test is the pure walk between them:
# Sandbox uid -> backing Pod (by owner-uid) -> runtimeClassName, plus the guard that
# an unreadable Sandbox uid surfaces as an unresolved violation rather than vanishing
# from the count.


class _FakeOwnerRef:
    def __init__(self, uid):
        self.uid = uid


class _FakePodMeta:
    def __init__(self, owner_uids):
        self.owner_references = [_FakeOwnerRef(u) for u in owner_uids]


class _FakePodSpec:
    def __init__(self, runtime_class_name):
        self.runtime_class_name = runtime_class_name


class _FakePod:
    def __init__(self, owner_uids, runtime_class_name):
        self.metadata = _FakePodMeta(owner_uids)
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
    """sandbox name -> uid; a None uid simulates an unreadable Sandbox GET."""

    def __init__(self, name_to_uid):
        self._name_to_uid = name_to_uid

    def get_namespaced_custom_object(self, *, group, version, namespace, plural, name):
        uid = self._name_to_uid.get(name)
        return {"metadata": {"uid": uid}} if uid is not None else {"metadata": {}}


_GVR = ("agents.x-k8s.io", "v1beta1", "sandboxes")


def test_verify_all_pods_match_returns_count():
    custom = _FakeCustom({"sb-a": "uid-a", "sb-b": "uid-b"})
    core = _FakeCore([_FakePod(["uid-a"], "kata"), _FakePod(["uid-b"], "kata")])
    assert rc.verify_bound_pod_runtimes(
        custom, core, namespace="ns", sandbox_names=["sb-a", "sb-b"],
        sandbox_gvr=_GVR, expected_runtime_class="kata",
    ) == 2


def test_verify_backing_pod_wrong_runtime_raises():
    # The silent isolation drop: Sandbox Ready but its backing Pod ran runc.
    custom = _FakeCustom({"sb-a": "uid-a"})
    core = _FakeCore([_FakePod(["uid-a"], "runc")])
    _assert_raises(lambda: rc.verify_bound_pod_runtimes(
        custom, core, namespace="ns", sandbox_names=["sb-a"],
        sandbox_gvr=_GVR, expected_runtime_class="kata",
    ))


def test_verify_missing_backing_pod_is_unresolved_violation():
    # uid resolves but no Pod owns it -> None -> violation (cannot prove the runtime).
    # settle_retries=0 keeps the terminal-unresolved assertion fast (no settle sleep).
    custom = _FakeCustom({"sb-a": "uid-a"})
    core = _FakeCore([])
    _assert_raises(lambda: rc.verify_bound_pod_runtimes(
        custom, core, namespace="ns", sandbox_names=["sb-a"],
        sandbox_gvr=_GVR, expected_runtime_class="kata", settle_retries=0,
    ))


def test_verify_unreadable_sandbox_uid_cannot_silently_shrink_count():
    # sb-b's uid is unreadable: it must surface as an unresolved violation, NOT vanish
    # from the count (else the gate passes on a quietly-shrunk set).
    custom = _FakeCustom({"sb-a": "uid-a", "sb-b": None})
    core = _FakeCore([_FakePod(["uid-a"], "kata")])
    _assert_raises(lambda: rc.verify_bound_pod_runtimes(
        custom, core, namespace="ns", sandbox_names=["sb-a", "sb-b"],
        sandbox_gvr=_GVR, expected_runtime_class="kata", settle_retries=0,
    ))


class _FlakyCore:
    """Returns no pods until call N, then the real pods — simulates owner-ref
    propagation lag where a warm pod's re-parent to its Sandbox lands after the
    first read-back pass."""

    def __init__(self, pods, ready_on_call):
        self._pods = pods
        self._ready_on_call = ready_on_call
        self.calls = 0

    def list_namespaced_pod(self, namespace):
        self.calls += 1
        items = self._pods if self.calls >= self._ready_on_call else []
        return _FakePodList(items)


def test_verify_unresolved_clears_on_settle_retry():
    # The propagation-lag case: backing Pod not matchable on the first pass, then
    # resolvable on retry. With settle_retries>0 (zero sleep) the None settles to the
    # correct runtime and the gate PASSES — a transient race must not read as a
    # violation.
    custom = _FakeCustom({"sb-a": "uid-a"})
    core = _FlakyCore([_FakePod(["uid-a"], "kata")], ready_on_call=2)
    assert rc.verify_bound_pod_runtimes(
        custom, core, namespace="ns", sandbox_names=["sb-a"],
        sandbox_gvr=_GVR, expected_runtime_class="kata",
        settle_retries=3, settle_sleep_s=0.0,
    ) == 1
    assert core.calls >= 2  # proves it retried past the first empty pass


def test_verify_wrong_runtime_not_retried_fails_fast():
    # A runc fallback is observed_rc='runc' (not None) -> never enters the retry subset
    # -> fails immediately even with retries enabled. Settle is for None only.
    custom = _FakeCustom({"sb-a": "uid-a"})
    core = _FlakyCore([_FakePod(["uid-a"], "runc")], ready_on_call=1)
    _assert_raises(lambda: rc.verify_bound_pod_runtimes(
        custom, core, namespace="ns", sandbox_names=["sb-a"],
        sandbox_gvr=_GVR, expected_runtime_class="kata",
        settle_retries=3, settle_sleep_s=0.0,
    ))
    assert core.calls == 1  # wrong runtime is terminal — no settle retry burned


# ---- container_resources: runtime-family-aware pod resource floor (#3942) ----

def test_container_resources_kata_family_gets_guest_sane_floor():
    # kata sizes the microVM from Pod cpu+memory — a tiny box SIGKILLs (137) the
    # in-guest container despite Ready. Both kata dialects normalize to the kata floor.
    for rcls in ("kata-clh", "kata-qemu", "kata"):
        res = rc.container_resources(rcls)
        assert res == {
            "requests": {"cpu": "500m", "memory": "512Mi"},
            "limits": {"cpu": "1", "memory": "1Gi"},
        }, rcls


def test_container_resources_gvisor_and_unset_are_byte_identical_tiny():
    tiny = {
        "requests": {"cpu": "10m", "memory": "16Mi"},
        "limits": {"cpu": "100m", "memory": "64Mi"},
    }
    # gVisor, an unset class, and an unknown class all fall to the canonical tiny
    # footprint — a vanilla-kind / gVisor manifest is unchanged from its pre-#3942 shape.
    for rcls in ("gvisor", "gvisor-experimental", "", "runc", "bogus"):
        assert rc.container_resources(rcls) == tiny, rcls


def test_container_resources_per_field_override():
    # Each of the four fields overrides independently; None keeps the family default.
    res = rc.container_resources("kata-clh", cpu_request="2", mem_limit="4Gi")
    assert res["requests"]["cpu"] == "2"          # overridden
    assert res["requests"]["memory"] == "512Mi"   # kata default kept
    assert res["limits"]["cpu"] == "1"            # kata default kept
    assert res["limits"]["memory"] == "4Gi"       # overridden


def test_container_resources_returns_fresh_dict_no_registry_mutation():
    r1 = rc.container_resources("kata-clh")
    r1["requests"]["cpu"] = "999"
    r2 = rc.container_resources("kata-clh")
    assert r2["requests"]["cpu"] == "500m"  # registry not corrupted by caller mutation


def _with_env(overrides, fn):
    import os
    saved = {k: os.environ.get(k) for k in overrides}
    try:
        for k, v in overrides.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return fn()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_container_resources_from_env_unset_uses_family_default():
    res = _with_env(
        {"BENCH_POD_CPU_REQUEST": None, "BENCH_POD_MEM_REQUEST": None,
         "BENCH_POD_CPU_LIMIT": None, "BENCH_POD_MEM_LIMIT": None},
        lambda: rc.container_resources_from_env("kata-clh"),
    )
    assert res == {
        "requests": {"cpu": "500m", "memory": "512Mi"},
        "limits": {"cpu": "1", "memory": "1Gi"},
    }


def test_container_resources_from_env_knobs_override():
    res = _with_env(
        {"BENCH_POD_CPU_REQUEST": "250m", "BENCH_POD_MEM_REQUEST": "1Gi",
         "BENCH_POD_CPU_LIMIT": "2", "BENCH_POD_MEM_LIMIT": "2Gi"},
        lambda: rc.container_resources_from_env("gvisor"),
    )
    assert res == {
        "requests": {"cpu": "250m", "memory": "1Gi"},
        "limits": {"cpu": "2", "memory": "2Gi"},
    }


def test_container_resources_from_env_blank_knob_is_family_default():
    # A knob set to whitespace-only strips to None -> family default (not "").
    res = _with_env(
        {"BENCH_POD_CPU_REQUEST": "  ", "BENCH_POD_MEM_REQUEST": None,
         "BENCH_POD_CPU_LIMIT": None, "BENCH_POD_MEM_LIMIT": None},
        lambda: rc.container_resources_from_env("kata-clh"),
    )
    assert res["requests"]["cpu"] == "500m"


def _run_all():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok: {fn.__name__}")
    print(f"test_runtime_class: all {len(fns)} test groups passed")


if __name__ == "__main__":
    _run_all()
