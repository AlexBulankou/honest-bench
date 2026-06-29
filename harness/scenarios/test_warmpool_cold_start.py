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


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok: {fn.__name__}")
    print(f"test_warmpool_cold_start: all {len(fns)} assertions passed")


if __name__ == "__main__":
    _run_all()
