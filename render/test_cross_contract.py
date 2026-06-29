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
        {"name": "cross_tenant_network_isolation", "outcome": "PASS", "badge_scope": "control-plane", "n": 12},
        {"name": "default_deny_egress", "outcome": "PASS", "badge_scope": "control-plane", "n": 12},
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
    # #3905: the isolation badge_scope rides the cell, data-driven, not the label
    assert "PASS (control-plane)" in out
    assert "Cross-tenant network isolation (control-plane)" not in out
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


def _load_metrics():
    """Import the harness metrics core (harness/metrics.py) in isolation, or None when the
    harness package isn't in-tree (keeps this file runnable in the render-only staging slice)."""
    import importlib.util

    path = os.path.join(_ROOT, "harness", "metrics.py")
    if not os.path.exists(path):
        return None
    spec = importlib.util.spec_from_file_location("_bench_metrics", path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception:  # pragma: no cover - harness has its own deps
        return None
    return mod


def test_emit_to_render_matrix_convergence_gvisor_doc_rows():
    """End-to-end convergence: REAL metrics.py producer -> render_matrix (the LIVE public path).

    This is the matrix-era successor to the render_product cross-contract guard above. The public
    page now renders via render_matrix (generate.build_readme switched to it in the 9-col migration,
    PR #28), so the contract that must not drift is metrics.ttfe_sla_metrics -> _clean_matrix_metrics
    (the closed MATRIX_METRIC_FIELDS guard) -> render_matrix. We feed synthetic raw samples through
    the ACTUAL producer functions, engineered to land on the spec doc's gVisor target values, and
    assert (a) every emitted key survives the closed guard with NO drop, and (b) the three gVisor
    activation rows render EXACTLY the doc numbers while the Kata rows stay honest-pending.

    Why a value-exact assertion (not just non-pending): a closed-schema key drop renders the cell
    `pending`, and a unit/format regression renders a wrong value -- both are silent on a
    non-pending check. Pinning the doc numbers catches either.
    """
    metrics = _load_metrics()
    if metrics is None:
        print("  (skip: harness metrics core not in-tree / not importable)")
        return

    # warm-pool: all TTFE <= 1s; p50=0.6s, p95=0.9s; 200/50s/1node = 4 sb/s/node at both bars.
    warm = metrics.ttfe_sla_metrics(
        [600.0] * 189 + [900.0] * 11, [True] * 200, window_s=50.0, node_count=1,
        max_concurrent_sandboxes=188, allocatable_sandbox_vcpu_per_node=100.0,
    )
    # unique-image cold: p50=1.2s, p95=1.56s; all >1s (<5s) so thpt@1s=0, thpt@5s=4.
    cold = metrics.ttfe_sla_metrics(
        [1200.0] * 189 + [1560.0] * 11, [True] * 200, window_s=50.0, node_count=1,
        max_concurrent_sandboxes=188, allocatable_sandbox_vcpu_per_node=100.0,
    )
    # resume-from-suspend: p50=3.5s, p95=5.0s; 1277/1376 exec-success (the doc's 92.8% honesty
    # flag); density N/A (omit the inputs). 1376/344s/1node = 4 @ <5s, 0 @ <1s.
    resume = metrics.ttfe_sla_metrics(
        [3500.0] * 1000 + [5000.0] * 376, [True] * 1277 + [False] * 99,
        window_s=344.0, node_count=1,
    )

    # (a) field-level convergence: the producer's keys all survive the closed render guard.
    for label, sla in (("warm", warm), ("cold", cold), ("resume", resume)):
        kept = render._clean_matrix_metrics(sla)
        assert kept == sla, (
            f"emit/render schema DRIFT on {label}: the closed MATRIX_METRIC_FIELDS guard dropped "
            f"producer keys {set(sla) - set(kept)} -- the matrix cell would render pending.\n"
            f"emitted={sla}\nkept={kept}"
        )

    results = {
        "product": "sandbox",
        "generated_at": "2026-06-29T03:00:00Z",
        "provenance": {"runtime": "gvisor", "cluster_substrate": "gke", "node_count": 1},
        "scenarios": [
            {"name": "warmpool_cold_start", "outcome": "PASS", "n": 200, "sla_metrics": warm},
            {"name": "native_digest_cold", "outcome": "PASS", "n": 200, "sla_metrics": cold},
            {"name": "suspend_resume", "outcome": "PASS", "n": 1376, "sla_metrics": resume},
        ],
    }
    out = render.render_matrix(results)

    # (b) the three gVisor rows render EXACTLY the spec doc's target numbers.
    assert "| gVisor | Warm-pool hit (Base image) | 4 | 4 | 0.6s | 0.9s | 200 | 1.88 | 100% |" in out
    assert "| gVisor | Unique-image cold (RL reality) | 4 | 0 | 1.2s | 1.56s | 200 | 1.88 | 100% |" in out
    assert (
        "| gVisor | Resume-from-suspend | 4 | 0 | 3.5s | 5s | 1376 | N/A | 92.8% (1277/1376) ⚠️ |"
        in out
    )
    # the unmeasured runtime stays honest-pending (never a guess), resume density always N/A.
    assert "| Kata + microVM | Warm-pool hit (Base image) | pending | pending | pending | pending | pending | pending | pending |" in out
    assert "| Kata + microVM | Resume-from-suspend | pending | pending | pending | pending | pending | N/A | pending |" in out


def test_emit_to_render_matrix_convergence_single_sample_ttfe_point():
    """Convergence guard for the SINGLE-SAMPLE shape (n=1): metrics.single_sample_ttfe_point ->
    _clean_matrix_metrics -> render_matrix. This is the shape the cold (native_digest_cold) and
    resume (suspend_resume) rows ACTUALLY emit (PR #31) -- a lone create->first-execution sample,
    not a burst. The sibling test above feeds the multi-sample BURST producer (ttfe_sla_metrics);
    this one pins the n=1 path, which differs in two honesty-load-bearing ways:

      1. p50 == p95 == the one sample (a single point has no distribution spread), and
      2. NO throughput key is emitted at all (one provision is not a per-second rate), so the two
         throughput columns MUST render `pending` -- the honest "not measured", NOT a false 0.

    The (2) distinction is the load-bearing one: a regression that defaulted an ABSENT throughput
    key to 0 in render would conflate it with metrics.throughput_per_node's honest-zero (samples
    exist but none beat the threshold). For n=1 there is no rate at all; pending is the only honest
    render. A naive "default missing numeric to 0" change would silently turn a single cold
    provision into a false "0 sb/s/node throughput" claim -- this test fails loudly on it,
    value-exact.
    """
    metrics = _load_metrics()
    if metrics is None:
        print("  (skip: harness metrics core not in-tree / not importable)")
        return

    cold = metrics.single_sample_ttfe_point(1200.0, True)
    resume = metrics.single_sample_ttfe_point(3500.0, True)
    cold_fail = metrics.single_sample_ttfe_point(None, False)

    # (a) field-level: every emitted key (minus the lifted-out n) survives the closed guard.
    for label, pt in (("cold", cold), ("resume", resume), ("cold_fail", cold_fail)):
        sla = {k: v for k, v in pt.items() if k != "n"}
        kept = render._clean_matrix_metrics(sla)
        assert kept == sla, (
            f"emit/render schema DRIFT on single-sample {label}: closed guard dropped "
            f"{set(sla) - set(kept)} -- the cell would render pending.\nemitted={sla}\nkept={kept}"
        )

    def _row(name, outcome, pt):
        # mirror run.py:_run_one -- n is lifted out of sla_metrics to the scenario top level.
        return {
            "name": name,
            "outcome": outcome,
            "n": pt.get("n", 1),
            "sla_metrics": {k: v for k, v in pt.items() if k != "n"},
        }

    results = {
        "product": "sandbox",
        "generated_at": "2026-06-29T03:00:00Z",
        "provenance": {"runtime": "gvisor", "cluster_substrate": "gke", "node_count": 1},
        "scenarios": [
            _row("native_digest_cold", "PASS", cold),
            _row("suspend_resume", "PASS", resume),
        ],
    }
    out = render.render_matrix(results)

    # (b) single-sample rows: p50 == p95 (one point), throughput columns PENDING (not a false 0),
    # density pending (cold) / N/A (resume), exec 100%.
    assert "| gVisor | Unique-image cold (RL reality) | pending | pending | 1.2s | 1.2s | 1 | pending | 100% |" in out
    assert "| gVisor | Resume-from-suspend | pending | pending | 3.5s | 3.5s | 1 | N/A | 100% |" in out

    # never-reached-first-execution cold provision: every measured column pending, exec honest 0%.
    fail_results = {
        "product": "sandbox",
        "generated_at": "2026-06-29T03:00:00Z",
        "provenance": {"runtime": "gvisor", "cluster_substrate": "gke", "node_count": 1},
        "scenarios": [_row("native_digest_cold", "FAIL", cold_fail)],
    }
    out_fail = render.render_matrix(fail_results)
    assert "| gVisor | Unique-image cold (RL reality) | pending | pending | pending | pending | 1 | pending | 0% (0/1) ⚠️ |" in out_fail


def test_emit_to_render_scale_proof_convergence_doc_linearity():
    """End-to-end convergence for the SECOND public table: the REAL metrics.py producers
    (density_per_vcpu + throughput_per_node + retention) -> _clean_scale_proof (the closed
    SCALE_PROOF_FIELDS guard) -> render_scale_proof (the LIVE public path — generate.build_readme
    renders this table right after the matrix, render/generate.py:111-112).

    The sibling of test_emit_to_render_matrix_convergence_* above, for the Scale Proof (Linearity
    Check) table. The matrix test guards the 9 metric cells; this guards the linearity table whose
    contract is metrics.{density_per_vcpu,throughput_per_node,retention} -> the closed scale_proof
    guard -> render_scale_proof. Without this, a key-name drift on the scale_proof object (e.g. a
    producer renaming density_retention/thpt_retention, or a scale_points shape change) would make
    _clean_scale_proof silently drop the ratio -> the cell renders `pending` (or the whole table
    vanishes) with NO test failing. We feed synthetic raw samples through the ACTUAL producers,
    engineered to land on the doc's flat-linearity claim (~1.88/vCPU held across 1->2->4 nodes,
    throughput held at 4 sb/s/node), and assert (a) every producer-emitted scale_proof key
    survives the closed guard with NO drop, and (b) the table renders EXACTLY the doc's linearity
    row with the asymmetric ✅ verdict.

    Why value-exact (not just non-pending): thpt_retention has NO per-point fallback (the locked
    contract — the producer MUST emit it), so a dropped key renders the throughput cell `pending`,
    and a unit/format regression renders a wrong ratio -- both silent on a non-pending check.
    """
    metrics = _load_metrics()
    if metrics is None:
        print("  (skip: harness metrics core not in-tree / not importable)")
        return

    # density via the REAL producer for nodes 1/2/4 — all land on the locked 1.88/vCPU basis
    # (per-node-allocatable denominator, NOT cluster-wide capacity), so density holds flat.
    densities = [
        metrics.density_per_vcpu(188, 100.0),   # 1 node
        metrics.density_per_vcpu(376, 200.0),   # 2 nodes
        metrics.density_per_vcpu(752, 400.0),   # 4 nodes
    ]
    # throughput via the REAL producer: 200 sb under 5s in a 50s window on 1 node = 4 sb/s/node;
    # 800 sb under 5s in the same window on 4 nodes = 4 sb/s/node — i.e. per-node thpt holds flat.
    thpt_1 = metrics.throughput_per_node([600.0] * 200, metrics.THRESHOLD_5S_MS, 50.0, 1)
    thpt_4 = metrics.throughput_per_node([600.0] * 800, metrics.THRESHOLD_5S_MS, 50.0, 4)
    density_retention = metrics.retention(densities[0], densities[-1])
    thpt_retention = metrics.retention(thpt_1, thpt_4)

    scale_proof = {
        "scale_points": [
            {"node_count": 1, "density": densities[0]},
            {"node_count": 2, "density": densities[1]},
            {"node_count": 4, "density": densities[2]},
        ],
        "density_retention": density_retention,
        "thpt_retention": thpt_retention,
    }
    results = {
        "product": "sandbox",
        "generated_at": "2026-06-29T03:00:00Z",
        "provenance": {"runtime": "gvisor", "cluster_substrate": "gke", "node_count": 4},
        "scenarios": [],
        "scale_proof": scale_proof,
    }

    # (a) field-level convergence: every producer-emitted ratio survives the closed guard. A drop
    # would null the ratio (thpt_retention has no per-point fallback) -> the cell renders pending.
    cleaned = render._clean_scale_proof(results)
    assert cleaned is not None, (
        "emit/render schema DRIFT: the closed scale_proof guard rejected a producer-shaped "
        f"object outright -- the whole Scale Proof table would vanish.\n{scale_proof}"
    )
    assert cleaned["density_retention"] == density_retention, (
        "emit/render DRIFT on density_retention: producer emitted "
        f"{density_retention}, guard kept {cleaned['density_retention']}"
    )
    assert cleaned["thpt_retention"] == thpt_retention, (
        "emit/render DRIFT on thpt_retention (NO per-point fallback): producer emitted "
        f"{thpt_retention}, guard kept {cleaned['thpt_retention']} -- the throughput cell "
        "would render pending."
    )
    assert [p["density"] for p in cleaned["points"]] == densities, (
        "emit/render DRIFT on scale_points: producer densities "
        f"{densities}, guard kept {[p['density'] for p in cleaned['points']]}"
    )

    # (b) the table renders EXACTLY the doc's flat-linearity row. retention 1.0 >= 0.9 reads ✅
    # (the asymmetric verdict); density verdict carries the per-node sequence inline.
    out = render.render_scale_proof(results)
    assert "## Scale Proof (Linearity Check)" in out
    assert "| 1 → 2 → 4 | ✅ Yes (1.88 → 1.88 → 1.88) | ✅ Yes |" in out, (
        "scale-proof linearity row drifted from the doc's flat claim:\n" + out
    )


def test_emit_to_render_stepup_pareto_convergence():
    """Convergence guard for the Step-Up Pareto object (a#3960 item 4) — INERT-render edition.

    The step-up sweep object is emitted by harness/results_schema.build_results(stepup=...) (via
    _coerce_stepup) and allow-listed on the render side by schema.STEPUP_PARETO_FIELDS. Unlike the
    matrix/scale-proof tables there is NO render consumer yet (render_stepup is the headline-page
    lane, #3954) — so this guards the contract the only way meaningful while the render is inert:
    every field the EMITTER keeps on a real step-up object must pass the INDEPENDENT render-side
    STEPUP_PARETO_FIELDS predicate. If the two closed vocabularies drift (emitter keeps a key/shape
    the render allow-list would reject, or vice versa), the future render_stepup wiring would
    silently drop it -> a blank Pareto table. This fails loudly first, exactly as the scale_proof
    convergence test does for the live table.
    """
    import importlib.util

    import schema

    emitter_path = os.path.join(_ROOT, "harness", "results_schema.py")
    if not os.path.exists(emitter_path):
        print("  (skip: harness emitter not in-tree yet)")
        return
    spec = importlib.util.spec_from_file_location("_bench_emitter_stepup", emitter_path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as exc:  # pragma: no cover - harness has its own deps
        print(f"  (skip: harness emitter not importable in isolation: {exc})")
        return

    # A full step-up sweep: every optional per-point + sweep-level field populated, so the
    # convergence check exercises the whole vocabulary, not just the required spine.
    stepup_in = {
        "pareto_points": [
            {"offered_rate_per_s": 10, "ttfe_p95_ms": 240.0, "ttfe_p50_ms": 120.0,
             "ttfe_p99_ms": 480.0, "ready_per_s": 9.6, "cost_usd_per_1k_ready": 0.42},
            {"offered_rate_per_s": 30, "ttfe_p95_ms": 380.0},
            {"offered_rate_per_s": 100, "ttfe_p95_ms": 2400.0},
        ],
        "verdict": "saturated",
        "north_star_breach_rate": 30,
        "saturation_rate": 100,
        "max_flat_rate": 10,
        "sld_s": 20.0,
        "wpr": 0.75,
        "node_count": 510,
        "machine_type": "e2-standard-16",
        "measured_at": "2026-06-29T07:40:00Z",
        # The controller-startup LOWER-BOUND proxy block (#3975) — exercised alongside a populated
        # true-TTFE pareto so the convergence check covers BOTH tables' vocabularies at once.
        "controller_startup": {
            "lower_bound": True,
            "pareto_points": [
                {"offered_rate_per_s": 10, "controller_startup_p95_ms": 180.0,
                 "controller_startup_p50_ms": 90.0, "controller_startup_p99_ms": 360.0,
                 "controller_ready_per_s": 9.8},
                {"offered_rate_per_s": 100, "controller_startup_p95_ms": 1900.0},
            ],
            "verdict": "saturated",
        },
    }
    results = mod.build_results(
        [], {"cluster_substrate": "gke", "node_count": 510},
        generated_at="2026-06-29T07:40:00Z", stepup=stepup_in,
    )
    assert "stepup" in results, (
        "emitter dropped the whole stepup object — _coerce_stepup rejected a valid full sweep:\n"
        f"{stepup_in}"
    )
    su = results["stepup"]

    # (a) every field the emitter KEPT passes the independent render allow-list predicate. A key
    # the render side doesn't know, or a value its predicate rejects, is the drift this catches.
    for key, val in su.items():
        assert key in schema.STEPUP_PARETO_FIELDS, (
            f"emit/render DRIFT: emitter kept key {key!r} absent from render STEPUP_PARETO_FIELDS "
            "— render_stepup would silently drop it."
        )
        assert schema.STEPUP_PARETO_FIELDS[key](val), (
            f"emit/render DRIFT: render STEPUP_PARETO_FIELDS[{key!r}] rejects emitter value "
            f"{val!r} — the cell/table would render nothing."
        )

    # (b) the required spine survives intact and full (no honest-partial drop on a clean sweep).
    assert len(su["pareto_points"]) == 3 and su["verdict"] == "saturated"
    assert su["pareto_points"][0]["ttfe_p99_ms"] == 480.0  # optional percentile carried through

    # (c) the controller_startup proxy block survives + its required lower_bound flag is True. The
    # render allow-list predicate already passed in loop (a); assert the shape the renderer reads.
    cs = su["controller_startup"]
    assert cs["lower_bound"] is True and len(cs["pareto_points"]) == 2
    assert cs["pareto_points"][0]["controller_startup_p95_ms"] == 180.0
    assert cs["pareto_points"][0]["controller_startup_p99_ms"] == 360.0  # optional carried through

    # (d) the #3975 gap shape: an EMPTY true-TTFE pareto + a valid proxy block still emits a stepup
    # object — pareto_points OMITTED (never empty []), proxy carries the only table, verdict honest.
    gap_in = {
        "pareto_points": [],
        "verdict": "no-measured-steps",
        "controller_startup": {
            "lower_bound": True,
            "pareto_points": [{"offered_rate_per_s": 10, "controller_startup_p95_ms": 180.0}],
        },
    }
    gap_results = mod.build_results(
        [], {"cluster_substrate": "gke", "node_count": 510},
        generated_at="2026-06-29T07:40:00Z", stepup=gap_in,
    )
    assert "stepup" in gap_results, "emitter dropped a valid #3975 gap sweep (empty TTFE + proxy)"
    gsu = gap_results["stepup"]
    assert "pareto_points" not in gsu, "empty true-TTFE pareto must be OMITTED, not emitted as []"
    assert gsu["verdict"] == "no-measured-steps"
    assert gsu["controller_startup"]["lower_bound"] is True
    for key, val in gsu.items():  # gap shape also passes the render allow-list
        assert key in schema.STEPUP_PARETO_FIELDS and schema.STEPUP_PARETO_FIELDS[key](val)

    # (e) an all-empty sweep (no TTFE points, no valid proxy) is honest "nothing" -> no stepup key.
    empty_results = mod.build_results(
        [], {"cluster_substrate": "gke", "node_count": 510},
        generated_at="2026-06-29T07:40:00Z",
        stepup={"pareto_points": [], "verdict": "no-measured-steps"},
    )
    assert "stepup" not in empty_results, "all-empty sweep must drop the stepup key entirely"


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok: {fn.__name__}")
    print(f"test_cross_contract: all {len(fns)} assertions passed")


if __name__ == "__main__":
    _run_all()
