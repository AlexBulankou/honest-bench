"""Cluster-free tests for suspend_resume's pure helpers.

Dependency-free: `python3 test_suspend_resume.py` (exit 0 = pass). The
suspend/resume legs themselves are live-only (they drive a real Sandbox), but
the resume-cycle-count knob's env parsing (_env_int) is pure and gates how many
resume-activation TTFE samples a fire collects, so it is pinned here. The
N-sample aggregation it feeds (metrics.multi_sample_ttfe_point) is tested in
harness/test_metrics.py.
"""

import os

try:  # cwd == scenarios/ (dependency-free `python3 test_suspend_resume.py`)
    import suspend_resume as cell
except ModuleNotFoundError:  # repo-root pytest: scenarios/ is a package, not on sys.path
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import suspend_resume as cell


def _check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _with_env(key, value, fn):
    """Run fn() with os.environ[key]=value (or unset when value is None), restore."""
    prior = os.environ.get(key)
    if value is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = value
    try:
        return fn()
    finally:
        if prior is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = prior


def test_env_int_unset_returns_default():
    out = _with_env("BENCH_TEST_CYCLES", None,
                    lambda: cell._env_int("BENCH_TEST_CYCLES", 1))
    _check(out == 1, "unset env -> default")


def test_env_int_blank_returns_default():
    out = _with_env("BENCH_TEST_CYCLES", "   ",
                    lambda: cell._env_int("BENCH_TEST_CYCLES", 1))
    _check(out == 1, "blank/whitespace env -> default")


def test_env_int_parses_valid():
    out = _with_env("BENCH_TEST_CYCLES", "5",
                    lambda: cell._env_int("BENCH_TEST_CYCLES", 1))
    _check(out == 5, "valid int parsed")


def test_env_int_junk_returns_default():
    # a fat-fingered fire-time override degrades to the safe default, not a crash
    out = _with_env("BENCH_TEST_CYCLES", "abc",
                    lambda: cell._env_int("BENCH_TEST_CYCLES", 3))
    _check(out == 3, "non-numeric env -> default (no raise)")


def test_env_int_clamps_below_minimum():
    # cycle_count must be >= 1; 0 / negative clamp up to the minimum
    zero = _with_env("BENCH_TEST_CYCLES", "0",
                     lambda: cell._env_int("BENCH_TEST_CYCLES", 1))
    neg = _with_env("BENCH_TEST_CYCLES", "-4",
                    lambda: cell._env_int("BENCH_TEST_CYCLES", 1))
    _check(zero == 1, "0 clamps to minimum 1")
    _check(neg == 1, "negative clamps to minimum 1")


def test_env_int_custom_minimum():
    out = _with_env("BENCH_TEST_CYCLES", "2",
                    lambda: cell._env_int("BENCH_TEST_CYCLES", 5, minimum=5))
    _check(out == 5, "value below custom minimum clamps up")


def test_default_cycle_count_is_one():
    # The module-level knob defaults to 1 so the resume row is page-unchanged
    # unless a fire explicitly bumps SUSPEND_RESUME_CYCLE_COUNT.
    _check(cell._RESUME_CYCLE_COUNT >= 1, "default cycle count >= 1")


# ---- runtime-class pinning: the scenario routes its pod_spec through the helper ----
#
# The pure pin logic lives in test_runtime_class.py; these lock that the SCENARIO
# actually routes its bare-Sandbox pod_spec through the shared runtime_class helper,
# gated on the module-level _RUNTIME_CLASS knob. _RUNTIME_CLASS is read at import;
# monkeypatch the module attribute (not os.environ) to exercise each runtime
# in-process, restoring it. The pin must touch only podTemplate.spec — the
# podTemplate.metadata.labels (the _read_pod recreate-fallback selector) stays put.
# The default-off case is load-bearing: with the knob unset the manifest must be its
# pre-#3942 byte-identical shape so the resume cell renders nothing until a deliberate
# runtime-pinned fire emits a closed-schema-clean object.

def _pod_spec_with_runtime(value):
    saved = cell._RUNTIME_CLASS
    cell._RUNTIME_CLASS = value
    try:
        return cell._build_sandbox_manifest("rt-test")["spec"]["podTemplate"]["spec"]
    finally:
        cell._RUNTIME_CLASS = saved


def _pod_template_with_runtime(value):
    saved = cell._RUNTIME_CLASS
    cell._RUNTIME_CLASS = value
    try:
        return cell._build_sandbox_manifest("rt-test")["spec"]["podTemplate"]
    finally:
        cell._RUNTIME_CLASS = saved


def test_sandbox_default_off_is_byte_identical():
    # Unset knob -> the manifest is its pre-#3942 shape: no runtime fields added.
    spec = _pod_spec_with_runtime("")
    _check("runtimeClassName" not in spec, "no runtimeClassName when knob unset")
    _check("tolerations" not in spec, "no tolerations when knob unset")
    _check("nodeSelector" not in spec, "no nodeSelector when knob unset")
    _check(spec["restartPolicy"] == "Never", "restartPolicy preserved")
    _check(spec["containers"][0]["name"] == "sandbox", "container preserved")


def test_sandbox_gvisor_pins_class_and_toleration():
    spec = _pod_spec_with_runtime("gvisor")
    _check(spec["runtimeClassName"] == "gvisor", "gvisor runtimeClassName pinned")
    keys = {t["key"] for t in spec["tolerations"]}
    _check("sandbox.gke.io/runtime" in keys, "gvisor toleration pinned")
    _check("nodeSelector" not in spec, "gVisor needs no node label")


def test_sandbox_kata_pins_class_toleration_and_selector():
    spec = _pod_spec_with_runtime("kata")
    _check(spec["runtimeClassName"] == "kata", "kata runtimeClassName pinned")
    keys = {t["key"] for t in spec["tolerations"]}
    _check("sandbox.gke.io/kata" in keys, "kata toleration pinned")
    _check(spec["nodeSelector"] == {"nested-virtualization": "enabled"},
           "kata nodeSelector pinned")


def test_pod_template_metadata_label_survives_pin():
    # The runtime pin touches only podTemplate.spec; the harness-stamped pod label
    # (the _read_pod recreate-fallback selector) must survive every runtime.
    for value in ("", "gvisor", "kata"):
        tmpl = _pod_template_with_runtime(value)
        labels = tmpl["metadata"]["labels"]
        _check(labels[cell._POD_LABEL_KEY] == "rt-test",
               f"pod label preserved under runtime {value!r}")


def _run_all():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"ok: {t.__name__}")
    print(f"ok - {len(tests)} suspend_resume tests passed")


if __name__ == "__main__":
    _run_all()
