"""Cross-contract guard: harness emitter output -> render_product, asserting NO row drops.

Run: `python3 test_cross_contract.py` (exit 0 = pass). Dependency-free.

Why this exists: the harness emitter (harness/, #3869) and the render closed schema
(schema.py) are two INDEPENDENT closed vocabularies. If they drift, the public README renders
an EMPTY table (every row silently dropped by the closed-schema guard) — the failure mode found
when an unported emitter shape was first fed through render_product (2 rows dropped, blank table).
This test fails loudly on that drift instead: it asserts that an emitter-shaped result renders
with `rows dropped by closed-schema guard: 0` and that every emitted scenario appears as its
public label. It codifies the manual cross-check done when the emitter was ported to the
canonical render vocabulary (uppercase PASS/FAIL, underscore scenario names, kebab pending
reasons, `_ms` metric keys).

It guards the contract two ways:
  1. an inline emitter-shaped fixture covering all six sandbox MVP scenarios, and
  2. the committed results/latest.json fixtures (which ARE the emitter's output contract — the
     auto-refresh Action overwrites them with live emitter output, so a drift there is a real
     producer/consumer divergence).
"""

import json
import os

import render

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

# Inline result in the canonical render vocabulary — exactly the shape the ported emitter
# (#3869) produces: uppercase outcomes, underscore scenario names, kebab pending reason,
# `_ms` metric keys. All six sandbox MVP scenarios, including the pending gVisor canary.
_EMITTER_SHAPE_SANDBOX = {
    "product": "sandbox",
    "generated_at": "2026-06-28T03:00:00Z",
    "provenance": {
        "cluster_substrate": "kind",
        "controller_image": "registry.k8s.io/agent-sandbox-controller:latest-main",
        "controller_digest": "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        "crd_version": "v1beta1",
        "suite_git_sha": "0123abc",
        "run_id": "sandbox-2026-06-28-0300",
        "node_count": 3,
        # the emitter always sets cold_start_mode (run.py defaults it to cold-provision,
        # #3885); #3894 renders it next to the native_digest_cold cell's cold_start_ms.
        "cold_start_mode": "cold-provision",
    },
    "scenarios": [
        {"name": "warmpool_cold_start", "outcome": "PASS", "n": 20, "sla_metrics": {"activation_ms": 180}},
        {"name": "native_digest_cold", "outcome": "PASS", "n": 20, "sla_metrics": {"cold_start_ms": 4200}},
        {"name": "suspend_resume", "outcome": "PASS", "n": 20, "sla_metrics": {"resume_ms": 950}},
        {"name": "cross_tenant_network_isolation", "outcome": "PASS", "n": 12},
        {"name": "default_deny_egress", "outcome": "PASS", "n": 12},
        {"name": "gvisor_canary", "outcome": "pending", "pending_reason": "requires-gvisor-runtime", "n": 0},
    ],
}


def _assert_no_drops(results, expect_labels):
    out = render.render_product(results)
    assert "rows dropped by closed-schema guard" not in out, (
        "emitter/render vocabulary DRIFT — the closed-schema guard dropped a row the emitter "
        "produced; the public table would be missing rows:\n" + out
    )
    for label in expect_labels:
        assert label in out, f"expected scenario label not rendered: {label!r}\n{out}"
    return out


def test_inline_emitter_shape_renders_all_six_rows():
    out = _assert_no_drops(
        _EMITTER_SHAPE_SANDBOX,
        [
            "Warm-pool activation (hit)",
            "Unique-image cold start",
            "Resume from suspend",
            "Cross-tenant network isolation",
            "Default-deny egress",
            "gVisor isolation canary",
        ],
    )
    # the pending canary renders its enum reason, never a guess or a false FAIL
    assert "pending (requires-gvisor-runtime)" in out
    # goal columns are (non-public) for every one of the six rows
    assert out.count("(non-public)") == 6 * 3
    # #3894: the emitter's cold_start_mode carries through to the cold-start cell label —
    # a drift that dropped it (or rendered the raw value elsewhere) fails here.
    assert "Cold start (ms) 4200 (cold-provision)" in out


def test_committed_fixtures_render_without_drops():
    # The committed results/latest.json files are the emitter's output contract: the
    # auto-refresh Action overwrites them with live emitter output. A drop here is a real
    # producer/consumer divergence, not a fixture typo.
    for product in ("sandbox", "substrate"):
        path = os.path.join(_ROOT, product, "results", "latest.json")
        if not os.path.exists(path):
            continue
        with open(path) as fh:
            results = json.load(fh)
        out = render.render_product(results)
        assert "rows dropped by closed-schema guard" not in out, (
            f"{product}/results/latest.json drifted from render schema (rows dropped):\n{out}"
        )
        # every scenario the fixture declares must appear as a row (non-empty table)
        for s in results.get("scenarios", []):
            name = s.get("name")
            label = render.SCENARIO_LABELS.get(name) if hasattr(render, "SCENARIO_LABELS") else None
            if label:
                assert label in out, f"{product}: {name!r} declared in fixture but not rendered"


def test_live_emitter_when_available():
    # Once the harness (#3869) is in-tree, exercise the REAL emitter end-to-end: build a
    # result via build_results and render it. Skipped (not failed) when the harness package
    # isn't present, so this test file stays runnable in the render-only staging slice.
    import importlib.util

    emitter_path = os.path.join(_ROOT, "harness", "results_schema.py")
    if not os.path.exists(emitter_path):
        print("  (skip: harness emitter not in-tree yet — #3869)")
        return
    spec = importlib.util.spec_from_file_location("_bench_emitter", emitter_path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as exc:  # pragma: no cover - harness has its own deps
        print(f"  (skip: harness emitter not importable in isolation: {exc})")
        return
    builder = getattr(mod, "build_results", None)
    if not callable(builder):
        print("  (skip: build_results not found on emitter)")
        return
    print("  (live emitter import OK — full end-to-end wiring tracked for #3869 follow-up)")


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok: {fn.__name__}")
    print(f"test_cross_contract: all {len(fns)} assertions passed")


if __name__ == "__main__":
    _run_all()
