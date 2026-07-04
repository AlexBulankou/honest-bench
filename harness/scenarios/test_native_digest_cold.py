"""Cluster-free tests for native_digest_cold: runtime-class pinning (#3942) and
the sample-count knob's mode-dependent honesty guard (NATIVE_DIGEST_COLD_SAMPLES,
hb#196).

Dependency-free: `python3 test_native_digest_cold.py` (exit 0 = pass). The pure
pin logic lives in test_runtime_class.py; these lock that the SCENARIO actually
routes its bare-Sandbox pod_spec through the shared runtime_class helper, gated on
the module-level _RUNTIME_CLASS knob.

_RUNTIME_CLASS is read at import; monkeypatch the module attribute (not
os.environ) to exercise each runtime in-process, restoring it. The default-off
case is the load-bearing one: with the knob unset the manifest must be exactly its
pre-#3942 byte-identical shape (no runtimeClassName / tolerations / nodeSelector),
so the unique-image-cold cell renders nothing until a deliberate runtime-pinned
fire emits a closed-schema-clean object.
"""

try:  # cwd == scenarios/ (dependency-free `python3 test_native_digest_cold.py`)
    import native_digest_cold as cell
except ModuleNotFoundError:  # repo-root pytest: scenarios/ is a package, not on sys.path
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import native_digest_cold as cell


# ---- runtime-class pinning: the scenario routes its pod_spec through the helper ----
#
# native_digest_cold builds a single bare Sandbox (a cold pull is cold once per
# node+image), so there is one pod_spec to pin — _build_sandbox_manifest's inner
# podTemplate.spec — unlike warmpool's template manifest. Same knob, same helper.

def _pod_spec_with_runtime(value):
    saved = cell._RUNTIME_CLASS
    cell._RUNTIME_CLASS = value
    try:
        return cell._build_sandbox_manifest("cold-test")["spec"]["podTemplate"]["spec"]
    finally:
        cell._RUNTIME_CLASS = saved


def test_sandbox_default_off_is_byte_identical():
    # Unset knob -> the manifest is its pre-#3942 shape: no runtime fields added.
    spec = _pod_spec_with_runtime("")
    assert "runtimeClassName" not in spec
    assert "tolerations" not in spec
    assert "nodeSelector" not in spec
    assert spec["restartPolicy"] == "Never"
    assert spec["containers"][0]["name"] == "sandbox"


def test_sandbox_gvisor_pins_class_and_toleration():
    spec = _pod_spec_with_runtime("gvisor")
    assert spec["runtimeClassName"] == "gvisor"
    assert "sandbox.gke.io/runtime" in {t["key"] for t in spec["tolerations"]}
    assert "nodeSelector" not in spec  # gVisor needs no node label


def test_sandbox_kata_pins_class_toleration_and_selector():
    spec = _pod_spec_with_runtime("kata")
    assert spec["runtimeClassName"] == "kata"
    assert "sandbox.gke.io/kata" in {t["key"] for t in spec["tolerations"]}
    assert spec["nodeSelector"] == {"nested-virtualization": "enabled"}


# ---- sample-count knob (NATIVE_DIGEST_COLD_SAMPLES, hb#196) ----
#
# The knob's honesty guard is mode-dependent: cold-provision has no cache
# confound (every bare create is an independent cold provision), so N passes
# through; cold-pull is cold exactly once per node+image, so N>1 is refused
# (fall back to 1) rather than publishing a caching measurement as cold pull.

def test_samples_default_is_one():
    # Env unset in the test environment -> byte-identical single-sample default.
    assert cell._SAMPLES == 1


def test_effective_samples_provision_passes_through():
    assert cell._effective_samples(1, "cold-provision") == 1
    assert cell._effective_samples(30, "cold-provision") == 30


def test_effective_samples_cold_pull_refuses_multi():
    assert cell._effective_samples(30, "cold-pull") == 1


def test_effective_samples_cold_pull_single_ok():
    assert cell._effective_samples(1, "cold-pull") == 1


def test_mode_env_validated_fail_fast():
    # An unknown BENCH_NATIVE_DIGEST_COLD_MODE must raise at import, not fail
    # open past the cold-pull refusal (which keys on the exact string). Re-exec
    # the module import in a subprocess so the module-level guard is exercised.
    import os
    import subprocess
    import sys

    env = dict(os.environ, BENCH_NATIVE_DIGEST_COLD_MODE="cold-pul")
    proc = subprocess.run(
        [sys.executable, "-c", "import native_digest_cold"],
        cwd=os.path.dirname(os.path.abspath(cell.__file__)),
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "BENCH_NATIVE_DIGEST_COLD_MODE must be one of" in proc.stderr


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok: {fn.__name__}")
    print(f"test_native_digest_cold: all {len(fns)} assertions passed")


if __name__ == "__main__":
    _run_all()
