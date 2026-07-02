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
import sys

import render

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

# Import the real harness emitter so the badge_construction test below exercises the TRUE
# emit -> render path (build_results -> render_product), not a hand-shaped dict — the whole
# point of the coupling guard is that it lives in the emitter.
sys.path.insert(0, os.path.join(_ROOT, "harness"))
import results_schema as _rs  # noqa: E402

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


def test_enforced_construction_emits_and_renders():
    # #4051: the harness emitter MUST be able to carry badge_construction end-to-end, so a
    # future enforced-flip renders `PASS (enforced, standard-np)` — never a bare
    # `PASS (enforced)` that misreads as a managed-gke-sandbox-NP guarantee (#3950/#2082).
    # This runs the REAL emitter (build_results, which holds the coupling guard) and feeds
    # its output through render_product, proving both vocabularies agree on the term.
    prov = {
        "cluster_substrate": "kind",
        "controller_image": "registry.k8s.io/agent-sandbox-controller:latest-main",
        "controller_digest": "sha256:" + "0" * 64,
        "crd_version": "v1beta1",
        "suite_git_sha": "0123abc",
        "run_id": "sandbox-2026-06-30-0647",
        "node_count": 3,
        "cold_start_mode": "cold-provision",
    }
    results = _rs.build_results(
        [
            {"name": "cross_tenant_network_isolation", "outcome": "pass",
             "badge_scope": "enforced", "badge_construction": "standard-np", "n": 12},
            {"name": "default_deny_egress", "outcome": "pass",
             "badge_scope": "enforced", "badge_construction": "standard-np", "n": 12},
        ],
        prov, "2026-06-30T06:47:00Z",
    )
    out = render.render_product(results)
    assert "rows dropped by closed-schema guard" not in out, (
        "emitter/render DRIFT on badge_construction — render dropped an enforced+construction "
        "row the emitter produced:\n" + out
    )
    # the construction renders as the SECOND suffix term, only alongside the scope
    assert "PASS (enforced, standard-np)" in out, (
        "enforced+construction did not render the coupled cell:\n" + out
    )
    # and never a bare enforced for these armed cells (the over-claim the term closes)
    assert "PASS (enforced)" not in out, (
        "bare `PASS (enforced)` leaked — the construction term must ride every enforced cell:\n"
        + out
    )


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

    # (b) the three gVisor rows render EXACTLY the spec doc's target numbers. hb#132: the
    # throughput cells are dual `<node> /node · <cluster>`; with no per-cluster field emitted the
    # cluster half pends `pending (cluster-fire)` while the per-node half matches the spec.
    cf = "pending (cluster-fire)"
    assert (
        f"| gVisor | Warm-pool hit (Base image) | 4 /node · {cf} | 4 /node · {cf} "
        "| 0.6s (count=200) | 0.9s (count=200) | 100% |"
    ) in out
    assert (
        f"| gVisor | Unique-image cold (RL reality) | 4 /node · {cf} | 0 /node · {cf} "
        "| 1.2s (count=200) | 1.56s (count=200) | 100% |"
    ) in out
    assert (
        f"| gVisor | Resume-from-suspend | 4 /node · {cf} | 0 /node · {cf} "
        "| 3.5s (count=1376) | 5s (count=1376) | 92.8% (1277/1376) ⚠️ |"
    ) in out
    # the unmeasured runtime stays honest-pending (never a guess) on its measurable rows.
    assert "| Kata + microVM | Warm-pool hit (Base image) | pending | pending | pending | pending | pending |" in out
    # resume × Kata is N/A-by-construction (CRIU does not transfer to the Kata VM model), never
    # pending — a pending cell would imply a future measurement that is structurally impossible.
    assert "| Kata + microVM | Resume-from-suspend | N/A | N/A | N/A | N/A | N/A |" in out


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

    # (b) single-sample rows: p50 == p95 (one point), exec 100%. The TTFE p50/p95 cells carry the
    # matched-N small-sample marker `†` (N=1 < TTFE_COMPARABILITY_MIN_N) so a cross-row read can't
    # rank a 1-sample point against a high-N row. (hb#134 dropped the N + Max-Density columns from
    # the headline matrix; Max-Density moved to render_density_detail / DETAILS.md.)
    #
    # Throughput columns (hb#142.1 derivable honest-0): the @<5s cell stays `pending` because the
    # measured p95 is WITHIN the 5s bar (1.2s / 3.5s) — some sandboxes may clear it, so we cannot
    # derive a 0 and no fire has run. The @<1s cell DERIVES `0 /node · 0 /cluster` because the
    # measured p95 EXCEEDS the 1s bar: that sample missed the bar, so 0 in-sample sandboxes cleared
    # <1s — the SAME honest-0 a real fire would emit, and a per-node 0 forces the per-cluster 0 (the
    # one exact case, not an extrapolation). This is NOT the guarded regression (defaulting an
    # ABSENT key to 0): the derivation is gated on p95 > bar; absent/within-bar p95 still pends —
    # see the FAIL row below, which pends both throughput columns because p95 is absent.
    assert "| gVisor | Unique-image cold (RL reality) | pending | 0 /node · 0 /cluster | 1.2s (count=1) † | 1.2s (count=1) † | 100% |" in out
    assert "| gVisor | Resume-from-suspend | pending | 0 /node · 0 /cluster | 3.5s (count=1) † | 3.5s (count=1) † | 100% |" in out

    # never-reached-first-execution cold provision: every measured column pending, exec honest 0%.
    fail_results = {
        "product": "sandbox",
        "generated_at": "2026-06-29T03:00:00Z",
        "provenance": {"runtime": "gvisor", "cluster_substrate": "gke", "node_count": 1},
        "scenarios": [_row("native_digest_cold", "FAIL", cold_fail)],
    }
    out_fail = render.render_matrix(fail_results)
    assert "| gVisor | Unique-image cold (RL reality) | pending | pending | pending | pending | 0% (0/1) ⚠️ |" in out_fail


def test_emit_to_render_cold_bind_decomposition_convergence():
    """Convergence guard for inch #2 (cold TTFE provision-vs-exec): the exact keys
    metrics.single_sample_ttfe_point emits on the DECOMPOSED cold path must survive the
    closed-schema guard AND render the cold decomposition block. This pins the emit↔render
    contract for native_digest_cold's bind/exec pairs the same way the warm inch #1 test
    pins the warm-pool path -- a schema drift that dropped a bind/exec key would silently
    return the block to INERT (page byte-unchanged), so this test fails loudly on it.

    The emit side is deliberately given an exec_ms that is NOT ttfe_ms - bind_ms
    (2130 - 2000 == 130, but exec_ms=200) so the rendered exec value proves the block shows
    the MEASURED residual carried through, never a render-side subtraction of percentiles.
    """
    metrics = _load_metrics()
    if metrics is None:
        print("  (skip: harness metrics core not in-tree / not importable)")
        return

    cold = metrics.single_sample_ttfe_point(2130.0, True, bind_ms=2000.0, exec_ms=200.0)

    # (a) field-level: the decomposition keys survive the closed results-schema coerce that
    # run.py writes to latest.json and that the render decomposition cleaner reads back. (The
    # MATRIX cleaner deliberately drops bind/exec -- they are not matrix columns -- so the
    # decomposition block reads from _coerce_sla_metrics, not _clean_matrix_metrics.)
    sla = {k: v for k, v in cold.items() if k != "n"}
    coerced = _rs._coerce_sla_metrics(sla)
    for k in ("bind_p50_ms", "bind_p95_ms", "exec_p50_ms", "exec_p95_ms",
              "ttfe_p50_ms", "ttfe_p95_ms"):
        assert k in coerced, (
            f"emit/render schema DRIFT on cold decomposition: closed guard dropped {k} -- "
            f"the block would go INERT.\nemitted={sla}\ncoerced={coerced}"
        )

    results = {
        "product": "sandbox",
        "generated_at": "2026-06-29T03:00:00Z",
        "provenance": {"runtime": "gvisor", "cluster_substrate": "gke", "node_count": 1},
        "scenarios": [
            {
                "name": "native_digest_cold",
                "outcome": "PASS",
                "n": cold.get("n", 1),
                "sla_metrics": sla,
            }
        ],
    }
    out = render.render_cold_bind_decomposition(results)
    assert "## Cold-Start TTFE — Provision vs Exec Decomposition" in out
    assert "| Provision (create → Ready) | 2s | 2s |" in out
    assert "| Exec (websocket + first-instruction) | 0.2s | 0.2s |" in out  # measured 200, not 130
    assert "| **TTFE (total)** | **2.13s** | **2.13s** |" in out


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


def test_emit_to_render_warm_vs_cold_convergence():
    """Convergence guard for the warm-vs-cold object (#3954 sibling) — INERT-render edition.

    The warm_vs_cold object is emitted by harness/results_schema.build_results(warm_vs_cold=...)
    (via _coerce_warm_vs_cold) and allow-listed on the render side by schema.WARM_VS_COLD_FIELDS.
    Like the step-up convergence test, this guards the contract while the render is inert (the
    headline-page render_warm_vs_cold wiring is a4s1's fire lane, #3954): every field the EMITTER
    keeps on a real warm-vs-cold object must pass the INDEPENDENT render-side WARM_VS_COLD_FIELDS
    predicate. A drift between the two closed vocabularies (the harness mirror enums vs render's
    RUNTIME_LABELS / semantic set) would make a future render silently drop the block -> a blank
    headline. This fails loudly first, exactly as the scale_proof / stepup convergence tests do.
    """
    import importlib.util

    import schema

    emitter_path = os.path.join(_ROOT, "harness", "results_schema.py")
    if not os.path.exists(emitter_path):
        print("  (skip: harness emitter not in-tree yet)")
        return
    spec = importlib.util.spec_from_file_location("_bench_emitter_wvc", emitter_path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as exc:  # pragma: no cover - harness has its own deps
        print(f"  (skip: harness emitter not importable in isolation: {exc})")
        return

    # Both enum vocabularies must agree value-for-value across the two independent closed sets,
    # or a future render drops a runtime/semantic the emitter happily emits.
    assert set(mod.WARM_VS_COLD_RUNTIME_CLASS_ENUM) == set(schema.RUNTIME_LABELS), (
        "emit/render DRIFT: harness WARM_VS_COLD_RUNTIME_CLASS_ENUM "
        f"{set(mod.WARM_VS_COLD_RUNTIME_CLASS_ENUM)} != render RUNTIME_LABELS keys "
        f"{set(schema.RUNTIME_LABELS)} — a runtime the emitter keeps would be dropped on render."
    )

    # A full warm-vs-cold object: every field populated (incl. the optional n_warm), exercised for
    # BOTH runtime classes so the convergence covers the whole runtime vocabulary, not just gVisor.
    for runtime_class in mod.WARM_VS_COLD_RUNTIME_CLASS_ENUM:
        wvc_in = {
            "warm_p50_ms": 420.0,
            "cold_ms": 4200.0,
            "speedup": 10.0,
            "semantic": "ttfe",
            "runtime_class": runtime_class,
            "n_warm": 200,
        }
        results = mod.build_results(
            [], {"cluster_substrate": "gke", "node_count": 1},
            generated_at="2026-06-29T07:40:00Z", warm_vs_cold=wvc_in,
        )
        assert "warm_vs_cold" in results, (
            "emitter dropped the whole warm_vs_cold object — _coerce_warm_vs_cold rejected a valid "
            f"full object:\n{wvc_in}"
        )
        wvc = results["warm_vs_cold"]

        # (a) every field the emitter KEPT passes the independent render allow-list predicate.
        for key, val in wvc.items():
            assert key in schema.WARM_VS_COLD_FIELDS, (
                f"emit/render DRIFT: emitter kept key {key!r} absent from render WARM_VS_COLD_FIELDS "
                "— render_warm_vs_cold would silently drop it."
            )
            assert schema.WARM_VS_COLD_FIELDS[key](val), (
                f"emit/render DRIFT: render WARM_VS_COLD_FIELDS[{key!r}] rejects emitter value "
                f"{val!r} — the headline would render nothing."
            )

        # (b) the required spine survives intact + full on a clean object.
        assert wvc["runtime_class"] == runtime_class and wvc["semantic"] == "ttfe"
        assert wvc["n_warm"] == 200  # optional sample count carried through

    # (c) an out-of-enum runtime drops the block entirely (the emitter's fail-closed PII guard),
    # so a free-text/internal runtime can never reach the render allow-list.
    leak_results = mod.build_results(
        [], {"cluster_substrate": "gke", "node_count": 1},
        generated_at="2026-06-29T07:40:00Z",
        warm_vs_cold={"warm_p50_ms": 420.0, "cold_ms": 4200.0, "speedup": 10.0,
                      "semantic": "ttfe", "runtime_class": "internal-pool-name"},
    )
    assert "warm_vs_cold" not in leak_results, (
        "out-of-enum runtime_class must drop the whole warm_vs_cold block — fail-closed PII guard"
    )


def test_emit_to_render_cold_start_mode_convergence():
    """Convergence guard for the OPTIONAL cold_start_mode field (#4024).

    cold_start_mode qualifies WHICH cold the cold leg measured so the public page never mislabels a
    warm-pool-overflow cold-provision (fresh node off the SHARED base image) as a true-cold
    unique-image pull. It is emitted by harness/results_schema._coerce_warm_vs_cold and allow-listed
    on the render side by schema.WARM_VS_COLD_FIELDS — two INDEPENDENT closed enums that must agree
    value-for-value, or a back-filled cold-provision object would be silently stripped (defeating the
    #4021 back-fill) or render the wrong phrasing. This fails loudly first.
    """
    import importlib.util

    import schema

    emitter_path = os.path.join(_ROOT, "harness", "results_schema.py")
    if not os.path.exists(emitter_path):
        print("  (skip: harness emitter not in-tree yet)")
        return
    spec = importlib.util.spec_from_file_location("_bench_emitter_csm", emitter_path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as exc:  # pragma: no cover - harness has its own deps
        print(f"  (skip: harness emitter not importable in isolation: {exc})")
        return

    # The two independent closed enums must agree value-for-value.
    assert set(mod.COLD_START_MODE_ENUM) == set(schema.COLD_START_MODES), (
        "emit/render DRIFT: harness COLD_START_MODE_ENUM "
        f"{set(mod.COLD_START_MODE_ENUM)} != render COLD_START_MODES "
        f"{set(schema.COLD_START_MODES)} — a back-filled mode would be dropped or mislabeled."
    )

    base = {
        "warm_p50_ms": 420.0, "cold_ms": 4200.0, "speedup": 10.0,
        "semantic": "ttfe", "runtime_class": "gvisor", "n_warm": 200,
    }

    # (a) every valid mode survives the emitter AND passes the render allow-list predicate.
    for mode in mod.COLD_START_MODE_ENUM:
        results = mod.build_results(
            [], {"cluster_substrate": "gke", "node_count": 1},
            generated_at="2026-06-29T07:40:00Z",
            warm_vs_cold={**base, "cold_start_mode": mode},
        )
        assert "warm_vs_cold" in results, f"emitter dropped a valid cold_start_mode={mode!r} object"
        wvc = results["warm_vs_cold"]
        assert wvc.get("cold_start_mode") == mode, (
            f"emitter stripped cold_start_mode={mode!r} — back-fill would not survive the PII guard"
        )
        assert schema.WARM_VS_COLD_FIELDS["cold_start_mode"](wvc["cold_start_mode"]), (
            f"render WARM_VS_COLD_FIELDS rejects emitter-kept cold_start_mode={mode!r}"
        )

    # (b) cold-provision renders the honest (non-unique-image) phrasing; never the true-cold leg.
    results = mod.build_results(
        [], {"cluster_substrate": "gke", "node_count": 1},
        generated_at="2026-06-29T07:40:00Z",
        warm_vs_cold={**base, "cold_start_mode": "cold-provision"},
    )
    out = render.render_warm_vs_cold(results)
    assert "Cold-provision (node overflow)" in out and "SHARED base image" in out, (
        "cold-provision must render the honest overflow phrasing"
    )
    assert "True-cold (unique-image)" not in out, (
        "cold-provision must NOT claim unique-image"
    )

    # (c) absent cold_start_mode survives + renders the locked true-cold default (byte-compat).
    results = mod.build_results(
        [], {"cluster_substrate": "gke", "node_count": 1},
        generated_at="2026-06-29T07:40:00Z", warm_vs_cold=dict(base),
    )
    assert "warm_vs_cold" in results and "cold_start_mode" not in results["warm_vs_cold"], (
        "absent cold_start_mode must stay absent (optional field), not be injected"
    )
    out = render.render_warm_vs_cold(results)
    assert "True-cold (unique-image)" in out, "absent mode must render the locked true-cold default"

    # (d) an invalid mode drops the whole block on the EMITTER side (fail-closed PII guard parity).
    leak = mod.build_results(
        [], {"cluster_substrate": "gke", "node_count": 1},
        generated_at="2026-06-29T07:40:00Z",
        warm_vs_cold={**base, "cold_start_mode": "cold-provison"},
    )
    assert "warm_vs_cold" not in leak, (
        "invalid cold_start_mode must drop the whole warm_vs_cold block (emitter fail-closed) — "
        "a typo must never publish as the true-cold default"
    )


def test_emit_to_render_kata_activation_convergence():
    """Convergence guard for the Kata+microVM activation object (#3942).

    The kata_activation object is emitted by harness/results_schema.build_results(kata_activation=...)
    (via _coerce_kata_activation) and allow-listed on the render side by schema.KATA_ACTIVATION_FIELDS.
    It publishes pod-Ready / microVM-activation latency — explicitly NOT TTFE (the matrix TTFE cells
    for Kata stay pending). Every field the EMITTER keeps on a real object must pass the INDEPENDENT
    render-side predicate, the two closed enum vocabularies (hypervisor, resume_status) must agree
    value-for-value, and the emitter's fail-closed PII guard must drop an out-of-enum hypervisor / a
    registry-path image / an internal node-name-shaped kernel. This fails loudly first.
    """
    import importlib.util

    import schema

    emitter_path = os.path.join(_ROOT, "harness", "results_schema.py")
    if not os.path.exists(emitter_path):
        print("  (skip: harness emitter not in-tree yet)")
        return
    spec = importlib.util.spec_from_file_location("_bench_emitter_kata", emitter_path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as exc:  # pragma: no cover - harness has its own deps
        print(f"  (skip: harness emitter not importable in isolation: {exc})")
        return

    # Both enum vocabularies must agree value-for-value across the two independent closed sets,
    # or a future render drops a hypervisor / resume-status the emitter happily emits.
    assert set(mod.KATA_HYPERVISOR_ENUM) == set(schema.KATA_HYPERVISORS), (
        "emit/render DRIFT: harness KATA_HYPERVISOR_ENUM "
        f"{set(mod.KATA_HYPERVISOR_ENUM)} != render KATA_HYPERVISORS {set(schema.KATA_HYPERVISORS)}"
    )
    assert set(mod.KATA_RESUME_STATUS_ENUM) == set(schema.KATA_RESUME_STATUSES), (
        "emit/render DRIFT: harness KATA_RESUME_STATUS_ENUM "
        f"{set(mod.KATA_RESUME_STATUS_ENUM)} != render KATA_RESUME_STATUSES "
        f"{set(schema.KATA_RESUME_STATUSES)}"
    )

    # A full kata_activation object: every field populated, exercised for each public hypervisor so
    # the convergence covers the whole hypervisor vocabulary, not just Cloud Hypervisor.
    for hypervisor in mod.KATA_HYPERVISOR_ENUM:
        ka_in = {
            "runtime_class": "kata-microvm",
            "microvm_activation_ms": 2000,
            "warm_ready_ms": 3000,
            "warm_image": "ubuntu:24.04",
            "cold_ready": [
                {"image": "debian:12", "ready_ms": 3000, "image_pull_ms": 900},
                {"image": "ubuntu:24.04", "ready_ms": 5000, "image_pull_ms": 887},
            ],
            "guest_kernel": "6.18.35",
            "host_kernel": "6.8.0-1054-gke",
            "hypervisor": hypervisor,
            "resume_status": "upstream-blocked",
            "kata_version": "3.32.0",
            "n": 3,
            "measured_at": "2026-06-30",
        }
        results = mod.build_results(
            [], {"cluster_substrate": "gke", "node_count": 1},
            generated_at="2026-06-30T07:40:00Z", kata_activation=ka_in,
        )
        assert "kata_activation" in results, (
            f"emitter dropped the whole kata_activation object — rejected a valid full object:\n{ka_in}"
        )
        ka = results["kata_activation"]

        # (a) every top-level field the emitter KEPT passes the independent render allow-list.
        for key, val in ka.items():
            assert key in schema.KATA_ACTIVATION_FIELDS, (
                f"emit/render DRIFT: emitter kept key {key!r} absent from render "
                "KATA_ACTIVATION_FIELDS — render_kata_activation would silently drop it."
            )
            assert schema.KATA_ACTIVATION_FIELDS[key](val), (
                f"emit/render DRIFT: render KATA_ACTIVATION_FIELDS[{key!r}] rejects emitter value "
                f"{val!r} — the block would render nothing."
            )

        # (b) the required spine survives intact on a clean object.
        assert ka["runtime_class"] == "kata-microvm" and ka["hypervisor"] == hypervisor
        assert ka["resume_status"] == "upstream-blocked" and len(ka["cold_ready"]) == 2

    # (c) an out-of-enum hypervisor drops the block entirely (emitter fail-closed PII guard).
    base = {
        "runtime_class": "kata-microvm", "microvm_activation_ms": 2000, "warm_ready_ms": 3000,
        "cold_ready": [{"image": "debian:12", "ready_ms": 3000}],
        "guest_kernel": "6.18.35", "host_kernel": "6.8.0-1054-gke",
    }
    leak_hv = mod.build_results(
        [], {"cluster_substrate": "gke", "node_count": 1},
        generated_at="2026-06-30T07:40:00Z",
        kata_activation={**base, "hypervisor": "internal-pool-name"},
    )
    assert "kata_activation" not in leak_hv, (
        "out-of-enum hypervisor must drop the whole kata_activation block — fail-closed PII guard"
    )

    # (d) a registry-path image (carries an internal project/registry path) drops the block.
    leak_img = mod.build_results(
        [], {"cluster_substrate": "gke", "node_count": 1},
        generated_at="2026-06-30T07:40:00Z",
        kata_activation={**base, "cold_ready": [
            {"image": "us-central1-docker.pkg.dev/proj/img:1", "ready_ms": 3000}]},
    )
    assert "kata_activation" not in leak_img, (
        "a registry-path image must drop the block — an internal registry path can never publish"
    )

    # (e) an internal node-name-shaped kernel value is rejected (the kernel regex is the guard
    # that keeps a node/pool name off a kernel field).
    leak_kern = mod.build_results(
        [], {"cluster_substrate": "gke", "node_count": 1},
        generated_at="2026-06-30T07:40:00Z",
        kata_activation={**base, "guest_kernel": "gke-sandbox-scenario-kata-microvm-poo-1e3d8e05"},
    )
    assert "kata_activation" not in leak_kern, (
        "an internal node-name-shaped kernel must drop the block — the kernel regex is the guard"
    )


def test_emit_to_render_concurrent_burst_convergence():
    """Convergence guard for the concurrent-burst block (#4021).

    concurrent_burst is emitted by harness/results_schema.build_results(concurrent_burst=...) (via
    _coerce_concurrent_burst) and allow-listed on the render side by schema.CONCURRENT_BURST_FIELDS.
    It reports a single ALL-AT-ONCE burst of N concurrent claims on the SAME TTFE spine as the Core
    Metrics matrix (warm-pool vs cold-provision). Every field the EMITTER keeps on a real object
    must pass the INDEPENDENT render-side predicate, the closed mode enum must agree value-for-value
    across the two independent sets, and the emitter's fail-closed guard must drop an out-of-enum
    mode / a negative ttfe / a registry-shaped machine_type. Fails loudly first.
    """
    import importlib.util

    import schema

    emitter_path = os.path.join(_ROOT, "harness", "results_schema.py")
    if not os.path.exists(emitter_path):
        print("  (skip: harness emitter not in-tree yet)")
        return
    spec = importlib.util.spec_from_file_location("_bench_emitter_cb", emitter_path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as exc:  # pragma: no cover - harness has its own deps
        print(f"  (skip: harness emitter not importable in isolation: {exc})")
        return

    # The closed mode vocabulary must agree value-for-value across the two independent sets,
    # or a future render drops a mode the emitter happily emits.
    assert set(mod.CONCURRENT_BURST_MODE_ENUM) == set(schema.CONCURRENT_BURST_MODES), (
        "emit/render DRIFT: harness CONCURRENT_BURST_MODE_ENUM "
        f"{set(mod.CONCURRENT_BURST_MODE_ENUM)} != render CONCURRENT_BURST_MODES "
        f"{set(schema.CONCURRENT_BURST_MODES)}"
    )

    # A full concurrent_burst object: every field populated, both modes exercised so the
    # convergence covers the whole mode vocabulary.
    cb_in = {
        "legs": [
            {"n": 300, "mode": "warm", "ttfe_p50_ms": 6874.3, "ttfe_p95_ms": 9393.0,
             "thpt_under_5s_per_node": 0.392, "thpt_under_1s_per_node": 0.0,
             "exec_success_rate": 1.0},
            {"n": 300, "mode": "cold", "ttfe_p50_ms": 56029.4, "ttfe_p95_ms": 58412.4,
             "thpt_under_5s_per_node": 0.0, "thpt_under_1s_per_node": 0.0,
             "exec_success_rate": 1.0},
        ],
        "node_count": 20,
        "machine_type": "e2-standard-16",
        "measured_at": "2026-06-30",
    }
    results = mod.build_results(
        [], {"cluster_substrate": "gke", "node_count": 20},
        generated_at="2026-06-30T21:50:00Z", concurrent_burst=cb_in,
    )
    assert "concurrent_burst" in results, (
        f"emitter dropped the whole concurrent_burst object — rejected a valid full object:\n{cb_in}"
    )
    cb = results["concurrent_burst"]

    # (a) every top-level field the emitter KEPT passes the independent render allow-list.
    for key, val in cb.items():
        assert key in schema.CONCURRENT_BURST_FIELDS, (
            f"emit/render DRIFT: emitter kept key {key!r} absent from render "
            "CONCURRENT_BURST_FIELDS — render_concurrent_burst would silently drop it."
        )
        assert schema.CONCURRENT_BURST_FIELDS[key](val), (
            f"emit/render DRIFT: render CONCURRENT_BURST_FIELDS[{key!r}] rejects emitter value "
            f"{val!r} — the block would render nothing."
        )

    # (b) the required spine survives intact on a clean object, both modes present.
    assert len(cb["legs"]) == 2
    assert {leg["mode"] for leg in cb["legs"]} == {"warm", "cold"}
    assert cb["legs"][0]["n"] == 300 and cb["legs"][0]["ttfe_p50_ms"] == 6874.3

    # (c) an out-of-enum mode drops the whole block (emitter fail-closed guard).
    leak_mode = mod.build_results(
        [], {"cluster_substrate": "gke", "node_count": 20},
        generated_at="2026-06-30T21:50:00Z",
        concurrent_burst={"legs": [
            {"n": 300, "mode": "lukewarm", "ttfe_p50_ms": 100.0, "ttfe_p95_ms": 200.0}]},
    )
    assert "concurrent_burst" not in leak_mode, (
        "out-of-enum mode must drop the whole concurrent_burst block — fail-closed guard"
    )

    # (d) a negative ttfe drops the whole block (no partial-lie table).
    leak_neg = mod.build_results(
        [], {"cluster_substrate": "gke", "node_count": 20},
        generated_at="2026-06-30T21:50:00Z",
        concurrent_burst={"legs": [
            {"n": 300, "mode": "warm", "ttfe_p50_ms": -1.0, "ttfe_p95_ms": 200.0}]},
    )
    assert "concurrent_burst" not in leak_neg, (
        "a negative ttfe must drop the whole block — closed-schema guard"
    )

    # (e) a registry-path-shaped machine_type is dropped (provenance scalar), but the block with
    # a valid spine still renders — provenance is best-effort, the spine is load-bearing.
    drop_mt = mod.build_results(
        [], {"cluster_substrate": "gke", "node_count": 20},
        generated_at="2026-06-30T21:50:00Z",
        concurrent_burst={"legs": [
            {"n": 300, "mode": "warm", "ttfe_p50_ms": 100.0, "ttfe_p95_ms": 200.0}],
            "machine_type": "us-central1-docker.pkg.dev/proj/img:1"},
    )
    assert "concurrent_burst" in drop_mt, "a valid spine must survive a dropped provenance scalar"
    assert "machine_type" not in drop_mt["concurrent_burst"], (
        "a registry-path-shaped machine_type must be dropped — bounded GCP-shape guard"
    )


def test_emit_to_render_warm_pool_acquisition_convergence():
    """Convergence guard for the warm-pool-acquisition block (#4083; carried across #112).

    warm_pool_acquisition is emitted by harness/results_schema.build_results(warm_pool_acquisition=...)
    (via _coerce_warm_pool_acquisition) and allow-listed on the render side by
    schema.WARM_POOL_ACQUISITION_FIELDS. It has NO in-process producer — the daily
    `harness.run --product sandbox` refresh carries the prior committed block forward verbatim
    (run.carry_prior_warm_pool_acquisition, #112), so this block MUST survive the emit→render
    round-trip or the daily refresh would carry a block the renderer silently treats as INERT
    (the #112 "other door" drop). Every field the EMITTER keeps on a real object must pass the
    INDEPENDENT render-side predicate, the runtime_class enum must agree value-for-value across the
    two independent sets, and the emitter's fail-closed guard must drop an out-of-enum runtime_class
    / a negative acq latency / a registry-shaped machine_type. Fails loudly first.
    """
    import importlib.util

    import schema

    emitter_path = os.path.join(_ROOT, "harness", "results_schema.py")
    if not os.path.exists(emitter_path):
        print("  (skip: harness emitter not in-tree yet)")
        return
    spec = importlib.util.spec_from_file_location("_bench_emitter_wpa", emitter_path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as exc:  # pragma: no cover - harness has its own deps
        print(f"  (skip: harness emitter not importable in isolation: {exc})")
        return

    # The closed runtime vocabulary must agree value-for-value across the two independent sets,
    # or a future render drops a runtime_class the emitter happily emits.
    assert set(mod.WARM_VS_COLD_RUNTIME_CLASS_ENUM) == set(schema.RUNTIME_LABELS), (
        "emit/render DRIFT: harness WARM_VS_COLD_RUNTIME_CLASS_ENUM "
        f"{set(mod.WARM_VS_COLD_RUNTIME_CLASS_ENUM)} != render RUNTIME_LABELS "
        f"{set(schema.RUNTIME_LABELS)}"
    )

    # A full warm_pool_acquisition object: every field populated so the convergence covers the
    # whole optional decomposition + provenance surface, not just the spine.
    wpa_in = {
        "runtime_class": "gvisor",
        "acq_p50_ms": 2939.65,
        "acq_p95_ms": 3878.44,
        "acq_p99_ms": 4009.62,
        "controller_startup_p95_ms": 1338.12,
        "n": 600,
        "offered_rate_per_s": 300,
        "warmpool_size": 600,
        "machine_type": "n2-standard-16",
        "node_count": 24,
        "measured_at": "2026-07-01T01:03:12Z",
    }
    results = mod.build_results(
        [], {"cluster_substrate": "gke-sandbox", "node_count": 24},
        generated_at="2026-07-01T12:00:00Z", warm_pool_acquisition=wpa_in,
    )
    assert "warm_pool_acquisition" in results, (
        f"emitter dropped the whole warm_pool_acquisition object — rejected a valid full object:\n{wpa_in}"
    )
    wpa = results["warm_pool_acquisition"]

    # (a) every top-level field the emitter KEPT passes the independent render allow-list.
    for key, val in wpa.items():
        assert key in schema.WARM_POOL_ACQUISITION_FIELDS, (
            f"emit/render DRIFT: emitter kept key {key!r} absent from render "
            "WARM_POOL_ACQUISITION_FIELDS — render_warm_pool_acquisition would silently drop it."
        )
        assert schema.WARM_POOL_ACQUISITION_FIELDS[key](val), (
            f"emit/render DRIFT: render WARM_POOL_ACQUISITION_FIELDS[{key!r}] rejects emitter value "
            f"{val!r} — the block would render nothing."
        )

    # (b) the required spine survives intact on a clean object.
    for spine in ("runtime_class", "acq_p50_ms", "acq_p95_ms", "n"):
        assert spine in wpa, f"required spine field {spine!r} must survive a clean object"
    assert wpa["runtime_class"] == "gvisor" and wpa["n"] == 600

    # (c) an out-of-enum runtime_class drops the whole block (emitter fail-closed guard).
    leak_rc = mod.build_results(
        [], {"cluster_substrate": "gke-sandbox", "node_count": 24},
        generated_at="2026-07-01T12:00:00Z",
        warm_pool_acquisition={"runtime_class": "firecracker", "acq_p50_ms": 100.0,
                               "acq_p95_ms": 200.0, "n": 10},
    )
    assert "warm_pool_acquisition" not in leak_rc, (
        "out-of-enum runtime_class must drop the whole warm_pool_acquisition block — fail-closed guard"
    )

    # (d) a negative acq latency drops the whole block (no partial-lie table).
    leak_neg = mod.build_results(
        [], {"cluster_substrate": "gke-sandbox", "node_count": 24},
        generated_at="2026-07-01T12:00:00Z",
        warm_pool_acquisition={"runtime_class": "gvisor", "acq_p50_ms": -1.0,
                               "acq_p95_ms": 200.0, "n": 10},
    )
    assert "warm_pool_acquisition" not in leak_neg, (
        "a negative acq_p50_ms must drop the whole block — closed-schema guard"
    )

    # (e) a registry-path-shaped machine_type is dropped (provenance scalar), but the block with
    # a valid spine still renders — provenance is best-effort, the spine is load-bearing.
    drop_mt = mod.build_results(
        [], {"cluster_substrate": "gke-sandbox", "node_count": 24},
        generated_at="2026-07-01T12:00:00Z",
        warm_pool_acquisition={"runtime_class": "gvisor", "acq_p50_ms": 100.0,
                               "acq_p95_ms": 200.0, "n": 10,
                               "machine_type": "us-central1-docker.pkg.dev/proj/img:1"},
    )
    assert "warm_pool_acquisition" in drop_mt, "a valid spine must survive a dropped provenance scalar"
    assert "machine_type" not in drop_mt["warm_pool_acquisition"], (
        "a registry-path-shaped machine_type must be dropped — bounded GCP-shape guard"
    )


def test_emit_to_render_at_scale_contention_convergence():
    """Convergence guard for the at-scale-contention RETRACTION block (#810; carried across #112).

    at_scale_contention is emitted by harness/results_schema.build_results(at_scale_contention=...)
    (via _coerce_at_scale_contention) and allow-listed on the render side by
    schema.AT_SCALE_CONTENTION_FIELDS. Like warm_pool_acquisition it has NO in-process producer —
    the daily `harness.run --product sandbox` refresh carries the prior committed block forward
    verbatim (run.carry_prior_at_scale_contention, #112), so this block MUST survive the emit→render
    round-trip or the daily refresh would carry a block the renderer silently treats as INERT (the
    #112 "other door" drop). It was the ONLY producer-less carry-forward block without a
    cross-contract convergence test — this closes that gap so a future vocab/enum edit on EITHER
    side fails loud, not silent-INERT. Every field the EMITTER keeps on a real object must pass the
    INDEPENDENT render-side predicate, the runtime_class enum must agree value-for-value across the
    two independent sets, the emitter's fail-closed guard must drop an out-of-enum runtime_class / a
    negative ttfe, a registry-shaped machine_type is dropped while a valid spine survives, and the
    DELIBERATE absence of any per-node throughput field holds on both sides (the #810 per-node-
    denominator trap: this point ran at node_count=1, so a per-node rate would invite a dishonest
    cross-block comparison against concurrent_burst's node_count=20 legs). Fails loudly first.
    """
    import importlib.util

    import schema

    emitter_path = os.path.join(_ROOT, "harness", "results_schema.py")
    if not os.path.exists(emitter_path):
        print("  (skip: harness emitter not in-tree yet)")
        return
    spec = importlib.util.spec_from_file_location("_bench_emitter_asc", emitter_path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as exc:  # pragma: no cover - harness has its own deps
        print(f"  (skip: harness emitter not importable in isolation: {exc})")
        return

    # The closed runtime vocabulary must agree value-for-value across the two independent sets,
    # or a future render drops a runtime_class the emitter happily emits.
    assert set(mod.WARM_VS_COLD_RUNTIME_CLASS_ENUM) == set(schema.RUNTIME_LABELS), (
        "emit/render DRIFT: harness WARM_VS_COLD_RUNTIME_CLASS_ENUM "
        f"{set(mod.WARM_VS_COLD_RUNTIME_CLASS_ENUM)} != render RUNTIME_LABELS "
        f"{set(schema.RUNTIME_LABELS)}"
    )

    # A full at_scale_contention object: every field populated so the convergence covers the
    # whole optional decomposition + provenance surface, not just the required spine.
    asc_in = {
        "runtime_class": "gvisor",
        "pool_size": 30,
        "claim_count": 60,
        "ttfe_p50_ms": 1658.9,
        "ttfe_p95_ms": 2016.9,
        "bind_p50_ms": 1384.0,
        "bind_p95_ms": 1700.1,
        "exec_p50_ms": 279.9,
        "exec_p95_ms": 323.7,
        "exec_success_rate": 1.0,
        "node_count": 1,
        "machine_type": "e2-standard-16",
        "measured_at": "2026-07-01",
    }
    results = mod.build_results(
        [], {"cluster_substrate": "gke-sandbox", "node_count": 1},
        generated_at="2026-07-01T12:00:00Z", at_scale_contention=asc_in,
    )
    assert "at_scale_contention" in results, (
        f"emitter dropped the whole at_scale_contention object — rejected a valid full object:\n{asc_in}"
    )
    asc = results["at_scale_contention"]

    # (a) every top-level field the emitter KEPT passes the independent render allow-list.
    for key, val in asc.items():
        assert key in schema.AT_SCALE_CONTENTION_FIELDS, (
            f"emit/render DRIFT: emitter kept key {key!r} absent from render "
            "AT_SCALE_CONTENTION_FIELDS — render_at_scale_contention would silently drop it."
        )
        assert schema.AT_SCALE_CONTENTION_FIELDS[key](val), (
            f"emit/render DRIFT: render AT_SCALE_CONTENTION_FIELDS[{key!r}] rejects emitter value "
            f"{val!r} — the block would render nothing."
        )

    # (b) the required spine survives intact on a clean object.
    for spine in ("runtime_class", "pool_size", "claim_count", "ttfe_p50_ms", "ttfe_p95_ms"):
        assert spine in asc, f"required spine field {spine!r} must survive a clean object"
    assert asc["runtime_class"] == "gvisor" and asc["pool_size"] == 30 and asc["claim_count"] == 60

    # (c) an out-of-enum runtime_class drops the whole block (emitter fail-closed guard).
    leak_rc = mod.build_results(
        [], {"cluster_substrate": "gke-sandbox", "node_count": 1},
        generated_at="2026-07-01T12:00:00Z",
        at_scale_contention={"runtime_class": "firecracker", "pool_size": 30, "claim_count": 60,
                             "ttfe_p50_ms": 100.0, "ttfe_p95_ms": 200.0},
    )
    assert "at_scale_contention" not in leak_rc, (
        "out-of-enum runtime_class must drop the whole at_scale_contention block — fail-closed guard"
    )

    # (d) a negative ttfe drops the whole block (no partial-lie table).
    leak_neg = mod.build_results(
        [], {"cluster_substrate": "gke-sandbox", "node_count": 1},
        generated_at="2026-07-01T12:00:00Z",
        at_scale_contention={"runtime_class": "gvisor", "pool_size": 30, "claim_count": 60,
                             "ttfe_p50_ms": -1.0, "ttfe_p95_ms": 200.0},
    )
    assert "at_scale_contention" not in leak_neg, (
        "a negative ttfe_p50_ms must drop the whole block — closed-schema guard"
    )

    # (e) a registry-path-shaped machine_type is dropped (provenance scalar), but the block with a
    # valid spine still renders — provenance is best-effort, the spine is load-bearing.
    drop_mt = mod.build_results(
        [], {"cluster_substrate": "gke-sandbox", "node_count": 1},
        generated_at="2026-07-01T12:00:00Z",
        at_scale_contention={"runtime_class": "gvisor", "pool_size": 30, "claim_count": 60,
                             "ttfe_p50_ms": 100.0, "ttfe_p95_ms": 200.0,
                             "machine_type": "us-central1-docker.pkg.dev/proj/img:1"},
    )
    assert "at_scale_contention" in drop_mt, "a valid spine must survive a dropped provenance scalar"
    assert "machine_type" not in drop_mt["at_scale_contention"], (
        "a registry-path-shaped machine_type must be dropped — bounded GCP-shape guard"
    )

    # (f) the DELIBERATE per-node-throughput omission holds on BOTH sides: a throughput-shaped key
    # is neither in the render allow-list nor kept by the emitter — so it can never sneak back in and
    # invite the #810 per-node-denominator cross-block comparison the block was designed to refuse.
    for banned in ("per_node_throughput", "throughput_per_node_per_s", "claims_per_node_per_s"):
        assert banned not in schema.AT_SCALE_CONTENTION_FIELDS, (
            f"render AT_SCALE_CONTENTION_FIELDS must NOT allow-list a per-node throughput field "
            f"({banned!r}) — #810 deliberately omits it (node_count=1, non-comparable)."
        )
    smuggle = mod.build_results(
        [], {"cluster_substrate": "gke-sandbox", "node_count": 1},
        generated_at="2026-07-01T12:00:00Z",
        at_scale_contention={"runtime_class": "gvisor", "pool_size": 30, "claim_count": 60,
                             "ttfe_p50_ms": 100.0, "ttfe_p95_ms": 200.0,
                             "per_node_throughput": 999.0},
    )
    assert "at_scale_contention" in smuggle, "a valid spine must survive an unknown extra key"
    assert "per_node_throughput" not in smuggle["at_scale_contention"], (
        "an unknown per-node throughput key must be dropped by the emitter's closed schema — "
        "the #810 per-node-denominator trap must stay closed on the emit side too"
    )


def test_emit_to_render_cluster_saturation_convergence():
    """Convergence guard for the cluster-scale SATURATION block (hb#132; carried across #112).

    cluster_saturation is emitted by harness/results_schema.build_results(cluster_saturation=...)
    (via _coerce_cluster_saturation) and allow-listed on the render side by
    schema.CLUSTER_SATURATION_FIELDS. Like at_scale_contention it has NO in-process producer — the
    daily `harness.run --product sandbox` refresh carries the prior committed block forward verbatim
    (run.carry_prior_cluster_saturation), so this block MUST survive the emit→render round-trip or
    the daily refresh would carry a block the renderer silently treats as INERT (the #112 "other
    door" drop). Every field the EMITTER keeps on a real object must pass the INDEPENDENT render-side
    predicate, the runtime_class enum must agree value-for-value across the two independent sets, the
    emitter's fail-closed guard must drop an out-of-enum runtime_class / a negative ttfe / a missing
    coupled-triple member, a registry-shaped machine_type is dropped while a valid spine survives,
    and the coupled per-cluster throughput triple (per-cluster figure + its node count) holds on both
    sides so a per-cluster rate never renders without the node count it was measured at. Loud first.
    """
    import importlib.util

    import schema

    emitter_path = os.path.join(_ROOT, "harness", "results_schema.py")
    if not os.path.exists(emitter_path):
        print("  (skip: harness emitter not in-tree yet)")
        return
    spec = importlib.util.spec_from_file_location("_bench_emitter_cs", emitter_path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as exc:  # pragma: no cover - harness has its own deps
        print(f"  (skip: harness emitter not importable in isolation: {exc})")
        return

    # The closed runtime vocabulary must agree value-for-value across the two independent sets.
    assert set(mod.WARM_VS_COLD_RUNTIME_CLASS_ENUM) == set(schema.RUNTIME_LABELS), (
        "emit/render DRIFT: harness WARM_VS_COLD_RUNTIME_CLASS_ENUM "
        f"{set(mod.WARM_VS_COLD_RUNTIME_CLASS_ENUM)} != render RUNTIME_LABELS "
        f"{set(schema.RUNTIME_LABELS)}"
    )

    # A full cluster_saturation object: every field populated so the convergence covers the whole
    # optional decomposition + provenance surface, not just the required spine.
    cs_in = {
        "runtime_class": "gvisor",
        "pool_size": 600,
        "claim_count": 600,
        "node_count": 40,
        "ttfe_p50_ms": 8630.8,
        "ttfe_p95_ms": 12610.3,
        "bind_p50_ms": 8191.6,
        "bind_p95_ms": 12137.2,
        "exec_p50_ms": 467.0,
        "exec_p95_ms": 608.0,
        "exec_success_rate": 1.0,
        "thpt_under_5s_per_node": 0.064,
        "thpt_under_1s_per_node": 0.0,
        "thpt_under_5s_per_cluster": 2.558,
        "thpt_under_1s_per_cluster": 0.0,
        "thpt_cluster_node_count": 40,
        "outcome": "FAIL",
        "run_id": "c50e1c51a4c441f3a0705a2426a9a93c",
        "machine_type": "n2-standard-16",
        "measured_at": "2026-07-02",
    }
    results = mod.build_results(
        [], {"cluster_substrate": "gke-sandbox", "node_count": 1},
        generated_at="2026-07-02T12:00:00Z", cluster_saturation=cs_in,
    )
    assert "cluster_saturation" in results, (
        f"emitter dropped the whole cluster_saturation object — rejected a valid full object:\n{cs_in}"
    )
    cs = results["cluster_saturation"]

    # (a) every top-level field the emitter KEPT passes the independent render allow-list.
    for key, val in cs.items():
        assert key in schema.CLUSTER_SATURATION_FIELDS, (
            f"emit/render DRIFT: emitter kept key {key!r} absent from render "
            "CLUSTER_SATURATION_FIELDS — render_cluster_saturation would silently drop it."
        )
        assert schema.CLUSTER_SATURATION_FIELDS[key](val), (
            f"emit/render DRIFT: render CLUSTER_SATURATION_FIELDS[{key!r}] rejects emitter value "
            f"{val!r} — the block would render nothing."
        )

    # (b) the required spine — incl. the coupled per-cluster triple — survives intact.
    for spine in ("runtime_class", "pool_size", "claim_count", "node_count", "ttfe_p50_ms",
                  "ttfe_p95_ms", "thpt_under_5s_per_cluster", "thpt_under_1s_per_cluster",
                  "thpt_cluster_node_count"):
        assert spine in cs, f"required spine field {spine!r} must survive a clean object"
    assert cs["runtime_class"] == "gvisor" and cs["pool_size"] == 600 and cs["node_count"] == 40

    # (c) an out-of-enum runtime_class drops the whole block (emitter fail-closed guard).
    leak_rc = mod.build_results(
        [], {"cluster_substrate": "gke-sandbox", "node_count": 1},
        generated_at="2026-07-02T12:00:00Z",
        cluster_saturation={"runtime_class": "firecracker", "pool_size": 600, "claim_count": 600,
                            "node_count": 40, "ttfe_p50_ms": 100.0, "ttfe_p95_ms": 200.0,
                            "thpt_under_5s_per_cluster": 1.0, "thpt_under_1s_per_cluster": 0.0,
                            "thpt_cluster_node_count": 40},
    )
    assert "cluster_saturation" not in leak_rc, (
        "out-of-enum runtime_class must drop the whole cluster_saturation block — fail-closed guard"
    )

    # (d) a negative ttfe drops the whole block (no partial-lie table).
    leak_neg = mod.build_results(
        [], {"cluster_substrate": "gke-sandbox", "node_count": 1},
        generated_at="2026-07-02T12:00:00Z",
        cluster_saturation={"runtime_class": "gvisor", "pool_size": 600, "claim_count": 600,
                            "node_count": 40, "ttfe_p50_ms": -1.0, "ttfe_p95_ms": 200.0,
                            "thpt_under_5s_per_cluster": 1.0, "thpt_under_1s_per_cluster": 0.0,
                            "thpt_cluster_node_count": 40},
    )
    assert "cluster_saturation" not in leak_neg, (
        "a negative ttfe_p50_ms must drop the whole block — closed-schema guard"
    )

    # (e) a missing coupled-triple member (the cluster node count) drops the whole block — a
    # per-cluster throughput must never render without the node count it was measured at.
    leak_triple = mod.build_results(
        [], {"cluster_substrate": "gke-sandbox", "node_count": 1},
        generated_at="2026-07-02T12:00:00Z",
        cluster_saturation={"runtime_class": "gvisor", "pool_size": 600, "claim_count": 600,
                            "node_count": 40, "ttfe_p50_ms": 100.0, "ttfe_p95_ms": 200.0,
                            "thpt_under_5s_per_cluster": 1.0, "thpt_under_1s_per_cluster": 0.0},
    )
    assert "cluster_saturation" not in leak_triple, (
        "a per-cluster throughput without thpt_cluster_node_count must drop the whole block — the "
        "coupled-triple rule (no per-cluster figure without its measured node count)"
    )

    # (f) a registry-path-shaped machine_type is dropped (provenance scalar), but the block with a
    # valid spine still renders — provenance is best-effort, the spine is load-bearing.
    drop_mt = mod.build_results(
        [], {"cluster_substrate": "gke-sandbox", "node_count": 1},
        generated_at="2026-07-02T12:00:00Z",
        cluster_saturation={"runtime_class": "gvisor", "pool_size": 600, "claim_count": 600,
                            "node_count": 40, "ttfe_p50_ms": 100.0, "ttfe_p95_ms": 200.0,
                            "thpt_under_5s_per_cluster": 1.0, "thpt_under_1s_per_cluster": 0.0,
                            "thpt_cluster_node_count": 40,
                            "machine_type": "us-central1-docker.pkg.dev/proj/img:1"},
    )
    assert "cluster_saturation" in drop_mt, "a valid spine must survive a dropped provenance scalar"
    assert "machine_type" not in drop_mt["cluster_saturation"], (
        "a registry-path-shaped machine_type must be dropped — bounded GCP-shape guard"
    )


def test_emit_to_render_provisioning_rate_sweep_convergence():
    """Convergence guard for the provisioning-rate-sweep block (#4086; carried across #112).

    provisioning_rate_sweep is emitted by harness/results_schema.build_results(
    provisioning_rate_sweep=...) (via _coerce_provisioning_rate_sweep) and allow-listed on the render
    side by schema.PROVISIONING_RATE_SWEEP_FIELDS. Like scale_proof it is a list-bearing top-level
    block with NO in-process producer — the daily `harness.run --product sandbox` refresh carries the
    prior committed block forward verbatim (run.carry_prior_provisioning_rate_sweep, #112), so it MUST
    survive the emit→render round-trip or the daily refresh would carry a block the renderer silently
    treats as INERT (the #112 "other door" drop). Every field the EMITTER keeps on a real object must
    pass the INDEPENDENT render-side predicate; the runtime_class enum must agree value-for-value
    across the two independent sets.

    The load-bearing DIFFERENCE from at_scale_contention (whose runtime_class is a REQUIRED, fail-
    closed spine): here `rate_points` is the ONLY required spine, and `runtime_class` is an OPTIONAL
    top-level color — an out-of-enum runtime_class is DROPPED PER-FIELD while the block SURVIVES, it
    does NOT fail the whole block closed. What DOES fail the whole block closed is a bad PER-POINT
    value (a negative ready_pct, an over-100 ready_pct, a non-int offered_rate): a bad point must
    never render as a partial-lie row. Fails loudly first.
    """
    import importlib.util

    import schema

    emitter_path = os.path.join(_ROOT, "harness", "results_schema.py")
    if not os.path.exists(emitter_path):
        print("  (skip: harness emitter not in-tree yet)")
        return
    spec = importlib.util.spec_from_file_location("_bench_emitter_prs", emitter_path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as exc:  # pragma: no cover - harness has its own deps
        print(f"  (skip: harness emitter not importable in isolation: {exc})")
        return

    # The closed runtime vocabulary must agree value-for-value across the two independent sets,
    # or a future render drops a runtime_class the emitter happily emits.
    assert set(mod.WARM_VS_COLD_RUNTIME_CLASS_ENUM) == set(schema.RUNTIME_LABELS), (
        "emit/render DRIFT: harness WARM_VS_COLD_RUNTIME_CLASS_ENUM "
        f"{set(mod.WARM_VS_COLD_RUNTIME_CLASS_ENUM)} != render RUNTIME_LABELS "
        f"{set(schema.RUNTIME_LABELS)}"
    )

    # A full provisioning_rate_sweep object: every top-level field + every optional per-point color
    # populated so the convergence covers the whole surface, not just the required spine.
    prs_in = {
        "runtime_class": "gvisor",
        "ceiling_low_per_s": 100,
        "ceiling_high_per_s": 150,
        "measured_at": "2026-07-01",
        "rate_points": [
            {"offered_rate_per_s": 100, "ready_pct": 100.0, "warmpool_size": 1500,
             "elapsed_s": 301.0, "converged": True},
            {"offered_rate_per_s": 150, "ready_pct": 42.0, "warmpool_size": 2250,
             "timeout_s": 1125.0, "converged": False},
            {"offered_rate_per_s": 200, "ready_pct": 21.0, "warmpool_size": 3000,
             "timeout_s": 1880.0, "converged": False},
        ],
    }
    results = mod.build_results(
        [], {"cluster_substrate": "gke-sandbox", "node_count": 1},
        generated_at="2026-07-01T12:00:00Z", provisioning_rate_sweep=prs_in,
    )
    assert "provisioning_rate_sweep" in results, (
        f"emitter dropped the whole provisioning_rate_sweep object — rejected a valid full object:\n{prs_in}"
    )
    prs = results["provisioning_rate_sweep"]

    # (a) every top-level field the emitter KEPT passes the independent render allow-list.
    for key, val in prs.items():
        assert key in schema.PROVISIONING_RATE_SWEEP_FIELDS, (
            f"emit/render DRIFT: emitter kept key {key!r} absent from render "
            "PROVISIONING_RATE_SWEEP_FIELDS — render_provisioning_rate_sweep would silently drop it."
        )
        assert schema.PROVISIONING_RATE_SWEEP_FIELDS[key](val), (
            f"emit/render DRIFT: render PROVISIONING_RATE_SWEEP_FIELDS[{key!r}] rejects emitter value "
            f"{val!r} — the block would render nothing."
        )

    # (b) the required spine (rate_points) survives intact, sorted, with per-point color preserved.
    assert "rate_points" in prs and len(prs["rate_points"]) == 3
    rates = [p["offered_rate_per_s"] for p in prs["rate_points"]]
    assert rates == [100, 150, 200], f"rate_points must survive all three points: {rates}"
    p0 = prs["rate_points"][0]
    assert p0["ready_pct"] == 100.0 and p0["warmpool_size"] == 1500 and p0["converged"] is True

    # (c) an out-of-enum runtime_class is DROPPED PER-FIELD but the block SURVIVES (the load-bearing
    # difference from at_scale_contention: runtime_class here is optional color, not a fail-closed spine).
    drop_rc = mod.build_results(
        [], {"cluster_substrate": "gke-sandbox", "node_count": 1},
        generated_at="2026-07-01T12:00:00Z",
        provisioning_rate_sweep={"runtime_class": "firecracker",
                                 "rate_points": [{"offered_rate_per_s": 100, "ready_pct": 100.0}]},
    )
    assert "provisioning_rate_sweep" in drop_rc, (
        "an out-of-enum runtime_class must NOT drop the whole block — it is optional color, not a "
        "fail-closed spine (the #4086 distinction from at_scale_contention)"
    )
    assert "runtime_class" not in drop_rc["provisioning_rate_sweep"], (
        "an out-of-enum runtime_class must be dropped per-field — closed-enum guard"
    )

    # (d) a bad PER-POINT value fails the WHOLE block closed (no partial-lie row).
    for bad_point in (
        {"offered_rate_per_s": 100, "ready_pct": -1.0},      # negative pct
        {"offered_rate_per_s": 100, "ready_pct": 101.0},     # over-100 pct
        {"offered_rate_per_s": 0, "ready_pct": 50.0},        # non-positive rate
        {"offered_rate_per_s": 100.5, "ready_pct": 50.0},    # non-int rate
    ):
        leak = mod.build_results(
            [], {"cluster_substrate": "gke-sandbox", "node_count": 1},
            generated_at="2026-07-01T12:00:00Z",
            provisioning_rate_sweep={"rate_points": [bad_point]},
        )
        assert "provisioning_rate_sweep" not in leak, (
            f"a bad per-point value {bad_point!r} must drop the WHOLE block — no partial-lie row"
        )

    # (e) a negative ceiling is dropped PER-FIELD, but a valid spine still renders.
    drop_ceil = mod.build_results(
        [], {"cluster_substrate": "gke-sandbox", "node_count": 1},
        generated_at="2026-07-01T12:00:00Z",
        provisioning_rate_sweep={"ceiling_low_per_s": -5,
                                 "rate_points": [{"offered_rate_per_s": 100, "ready_pct": 100.0}]},
    )
    assert "provisioning_rate_sweep" in drop_ceil, "a valid spine must survive a dropped ceiling"
    assert "ceiling_low_per_s" not in drop_ceil["provisioning_rate_sweep"], (
        "a negative ceiling_low_per_s must be dropped per-field — nonneg guard"
    )

    # (f) an empty / missing rate_points list drops the whole block (the required spine).
    for empty in ({"rate_points": []}, {"runtime_class": "gvisor"}):
        leak_empty = mod.build_results(
            [], {"cluster_substrate": "gke-sandbox", "node_count": 1},
            generated_at="2026-07-01T12:00:00Z", provisioning_rate_sweep=empty,
        )
        assert "provisioning_rate_sweep" not in leak_empty, (
            f"a missing/empty rate_points spine must drop the whole block: {empty!r}"
        )

    # (g) an unknown top-level key is dropped by the emitter's closed schema, spine survives.
    smuggle = mod.build_results(
        [], {"cluster_substrate": "gke-sandbox", "node_count": 1},
        generated_at="2026-07-01T12:00:00Z",
        provisioning_rate_sweep={"rate_points": [{"offered_rate_per_s": 100, "ready_pct": 100.0}],
                                 "internal_cluster_name": "leak-me"},
    )
    assert "provisioning_rate_sweep" in smuggle, "a valid spine must survive an unknown extra key"
    assert "internal_cluster_name" not in smuggle["provisioning_rate_sweep"], (
        "an unknown top-level key must be dropped by the emitter's closed schema — Layer-1 PII guard"
    )


def test_emit_to_render_slo_sweep_convergence():
    """Convergence guard for the per-mode SLO cluster-rate sweep leg (hb#132/#149).

    The harness ingestion (run.merge_slo_sweeps -> slo_rate.slo_sla_metrics_from_stepup) mirrors
    the render matrix's activation-mode rows as a LITERAL scenario tuple (the harness never
    imports render — offline-portability discipline), so the two vocabularies can drift silently:
    a renamed matrix row would leave its BENCH_SLO_SWEEP_* env var pointing at a scenario the
    render side no longer knows, and the derived triple would merge into a cell the matrix never
    renders. This test pins (a) name parity between run.SLO_SWEEP_SCENARIOS and
    schema.ACTIVATION_MODE_ROWS, and (b) the full emit->render round-trip for the INDEPENDENT
    per-bar fill — a sweep whose lowest rung clears only the 5s bar must fill the 5s cluster half
    (with the @X-nodes caption) while the 1s half keeps `pending (cluster-fire)`, straight through
    merge_slo_sweeps -> build_results (per-key sla_metrics coercion must keep the partial triple)
    -> render_matrix. Loud first.
    """
    import tempfile

    import schema

    sys.path.insert(0, _ROOT)
    try:
        from harness import run as _run
    except Exception as exc:  # pragma: no cover - harness has its own deps
        print(f"  (skip: harness run not importable: {exc})")
        return

    # (a) scenario-name parity — order-sensitive (both sides are ordered row lists).
    assert tuple(_run.SLO_SWEEP_SCENARIOS) == tuple(n for n, _ in schema.ACTIVATION_MODE_ROWS), (
        "emit/render DRIFT: harness run.SLO_SWEEP_SCENARIOS "
        f"{tuple(_run.SLO_SWEEP_SCENARIOS)} != render schema.ACTIVATION_MODE_ROWS names "
        f"{tuple(n for n, _ in schema.ACTIVATION_MODE_ROWS)} — a sweep env var would merge its "
        "derived triple into a scenario the matrix never renders (or vice versa)."
    )

    # (b) emit->render round-trip with an independent per-bar fill: the only compliant rung
    # clears the 5s bar (p95 3.2s) but not the 1s bar; the top rung overloads past both.
    nested = {
        "params": {"cluster_nodes": 40},
        "pareto": [
            {"offered_rate_per_s": 30, "ready_per_s": 28.4, "ttfe_p95_ms": 3200.0},
            {"offered_rate_per_s": 100, "ready_per_s": 41.0, "ttfe_p95_ms": 12610.3},
        ],
    }
    raw = [{
        "name": "warmpool_cold_start", "outcome": "PASS", "n": 200,
        "sla_metrics": {"ttfe_p50_ms": 600.0, "ttfe_p95_ms": 900.0,
                        "thpt_under_5s_per_node": 4.0, "thpt_under_1s_per_node": 4.0,
                        "exec_success_rate": 1.0},
    }]
    var = _run.slo_sweep_env_var("warmpool_cold_start")
    saved = os.environ.get(var)
    fd, tmp = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(nested, fh)
        os.environ[var] = tmp
        _run.merge_slo_sweeps(raw, "sandbox")
    finally:
        if saved is None:
            os.environ.pop(var, None)
        else:
            os.environ[var] = saved
        os.unlink(tmp)

    m = raw[0]["sla_metrics"]
    assert m.get("thpt_under_5s_per_cluster") == 28.4 and m.get("thpt_cluster_node_count") == 40, (
        f"merge_slo_sweeps must land the 5s bar + node_count, got {m!r}"
    )
    assert "thpt_under_1s_per_cluster" not in m, (
        "the non-compliant 1s bar must stay OMITTED (pend), never a fabricated 0"
    )

    # Through the REAL emitter: the per-key sla_metrics coercion must keep the partial triple.
    results = _run.results_schema.build_results(
        raw, {"cluster_substrate": "gke-sandbox", "node_count": 40, "runtime": "gvisor"},
        generated_at="2026-07-02T12:00:00Z",
    )
    sc = next(s for s in results["scenarios"] if s["name"] == "warmpool_cold_start")
    for key in ("thpt_under_5s_per_cluster", "thpt_cluster_node_count"):
        assert key in sc["sla_metrics"], (
            f"emitter coercion dropped {key!r} — the partial (landed-bar) triple must survive "
            "per-key; only the direct-emit metrics.py leg is all-or-nothing."
        )

    out = render.render_matrix(results)
    # 5s half fills (28.4 < the 300 cluster sizing target => ⚠️); 1s half keeps pending.
    assert (
        "| gVisor | Warm-pool hit (Base image) | 4 /node · 28.4 /cluster ⚠️ "
        "| 4 /node · pending (cluster-fire) | 0.6s (count=200) | 0.9s (count=200) | 100% |"
    ) in out, f"matrix row did not render the independent per-bar fill:\n{out}"
    # the caption pins the X the cluster figure was measured at.
    assert "cluster rate at 40 nodes" in out, (
        "the @X-nodes caption must resolve from the landed thpt_cluster_node_count"
    )


def test_emit_to_render_session_turnover_convergence():
    """Convergence guard for the session-turnover refill-latency block (#3868).

    session_turnover is a SCENARIO CELL (not a top-level carry_prior object): the scenario body
    (harness/scenarios/session_turnover.py) returns sla_metrics that run.py writes to
    results["scenarios"], and the render side allow-lists them via schema.SESSION_TURNOVER_FIELDS.
    The two vocabularies are INDEPENDENT, so a drift would silently return the block to INERT (page
    byte-unchanged). This test pins the contract three ways: (1) the scenario's own emit-key
    constants agree with the render allow-list, (2) the emitted sla keys survive the SAME
    _coerce_sla_metrics run.py applies before latest.json, and every survivor passes the independent
    render predicate, and (3) the full path renders the table with the top-level-`n` footnote —
    while the INERT gate (missing refill_latency_ms, or an empty {} from a pool that never refilled)
    renders "".

    The load-bearing subtlety: the scenario emits the reserved "n" key INSIDE sla_metrics, but
    run.py POPS it to a TOP-LEVEL scenario field before coercion (and _coerce_sla_metrics coerces
    every surviving value to float), so `n` is NEVER an sla_metrics key in results — the renderer
    reads it from the scenario's top-level "n". This test proves the block sources n from there
    (renders "(over N cycles)") and not from a float-coerced sla value.
    """
    import schema

    # (0) pin the scenario's OWN emit-key constants against the render allow-list — the TRUE emit
    # vocabulary, imported from the scenario module, not hand-shaped strings. The scenario has
    # sibling imports (harness/scenarios/_apiversion etc.), so its dir must be on sys.path for a
    # plain import to resolve them — spec_from_file_location alone would fail on the siblings.
    scen_dir = os.path.join(_ROOT, "harness", "scenarios")
    if not os.path.exists(os.path.join(scen_dir, "session_turnover.py")):
        print("  (skip: session_turnover scenario not in-tree yet)")
        return
    if scen_dir not in sys.path:
        sys.path.insert(0, scen_dir)
    try:
        import session_turnover as scen  # noqa: E402
    except Exception as exc:  # pragma: no cover - scenario has its own deps
        print(f"  (skip: session_turnover scenario not importable in isolation: {exc})")
        return
    for emit_key in (scen._KEY_REFILL, scen._KEY_REFILL_P90):
        assert emit_key in schema.SESSION_TURNOVER_FIELDS, (
            f"emit/render DRIFT: scenario emits {emit_key!r} but render SESSION_TURNOVER_FIELDS "
            f"does not allow-list it — render_session_turnover would silently drop it."
        )

    # (1) the emitted sla dict (as the scenario builds it on a completed run) survives the SAME
    # closed coercion run.py writes to latest.json; every survivor passes the render predicate.
    # `n` is stripped here exactly as run.py lifts the reserved key to a top-level scenario field.
    emitted_sla = {scen._KEY_REFILL: 4200.0, scen._KEY_REFILL_P90: 7800.0, "n": 5}
    sla = {k: v for k, v in emitted_sla.items() if k != "n"}
    coerced = _rs._coerce_sla_metrics(sla)
    for k in (scen._KEY_REFILL, scen._KEY_REFILL_P90):
        assert k in coerced, (
            f"emit/render schema DRIFT on session_turnover: closed coerce dropped {k!r} — "
            f"the block would go INERT.\nemitted={sla}\ncoerced={coerced}"
        )
        assert schema.SESSION_TURNOVER_FIELDS[k](coerced[k]), (
            f"render SESSION_TURNOVER_FIELDS[{k!r}] rejects coerced emitter value {coerced[k]!r} — "
            "the block would render nothing."
        )

    # (2) full path: the scenario cell (top-level n, coerced sla) renders the table + n-footnote.
    results = {
        "product": "sandbox",
        "generated_at": "2026-07-02T03:00:00Z",
        "provenance": {"runtime": "runc", "cluster_substrate": "kind", "node_count": 1},
        "scenarios": [
            {"name": "session_turnover", "outcome": "PASS", "n": 5, "sla_metrics": coerced},
        ],
    }
    out = render.render_session_turnover(results)
    assert "## Warm-Pool Turnover — Sustained-Churn Refill Latency" in out
    assert "| Median (p50) (over 5 cycles) | 4.2s |" in out, (
        f"median row must render the top-level-n footnote:\n{out}"
    )
    assert "| Tail (p90) | 7.8s |" in out, f"the optional p90 tail row must render:\n{out}"

    # (3) INERT gates: a cell whose pool never refilled ({}) and a missing spine both render "".
    for inert_metrics in ({}, {scen._KEY_REFILL_P90: 7800.0}):
        inert = {
            "product": "sandbox",
            "generated_at": "2026-07-02T03:00:00Z",
            "provenance": {"runtime": "runc", "cluster_substrate": "kind", "node_count": 1},
            "scenarios": [
                {"name": "session_turnover", "outcome": "FAIL", "n": 0,
                 "sla_metrics": inert_metrics},
            ],
        }
        assert render.render_session_turnover(inert) == "", (
            f"missing refill_latency_ms spine must render INERT (''): {inert_metrics!r}"
        )

    # (4) a completed cell with median but NO n still renders (footnote just omitted) — n is a
    # best-effort footnote, not a spine.
    no_n = {
        "product": "sandbox",
        "generated_at": "2026-07-02T03:00:00Z",
        "provenance": {"runtime": "runc", "cluster_substrate": "kind", "node_count": 1},
        "scenarios": [
            {"name": "session_turnover", "outcome": "PASS", "sla_metrics": {scen._KEY_REFILL: 4200.0}},
        ],
    }
    out_no_n = render.render_session_turnover(no_n)
    assert "| Median (p50) | 4.2s |" in out_no_n, (
        f"a median-only cell must render without the n-footnote:\n{out_no_n}"
    )
    assert "cycles)" not in out_no_n, "the '(over N cycles)' footnote must be absent when n is absent"


def test_emit_to_render_suspend_latency_convergence():
    """Convergence guard for the administrative-suspend-latency block (#3868).

    Like session_turnover this is a SCENARIO-CELL axis, but the emit is produced by a shared
    metrics helper (metrics.suspend_latency_point) whose output the suspend_resume scenario merges
    into its sla_metrics; run.py writes it to results["scenarios"], and the render side allow-lists
    it via schema.SUSPEND_LATENCY_FIELDS. The two vocabularies are INDEPENDENT — a drift would
    silently return the block to INERT (page byte-unchanged). This test pins the contract with the
    REAL producer: (1) the helper's emitted keys are allow-listed by render, (2) they survive the
    SAME _coerce_sla_metrics run.py applies and pass the independent render predicate, and (3) the
    full path renders the table while the INERT gate (missing suspend_latency_ms spine, empty {},
    or a suspend_resume cell carrying only resume-TTFE keys) renders "".
    """
    import schema

    m = _load_metrics()
    if m is None:
        print("  (skip: harness metrics core not in-tree / not importable)")
        return

    # (0) the REAL producer's emitted keys must be in the render allow-list (n>=2 -> both keys).
    emitted = m.suspend_latency_point([100.0, 300.0, 200.0])
    assert set(emitted) <= set(schema.SUSPEND_LATENCY_FIELDS), (
        f"emit/render DRIFT: helper emits {set(emitted)} but render SUSPEND_LATENCY_FIELDS "
        f"allow-lists {set(schema.SUSPEND_LATENCY_FIELDS)} — a stray key would be silently dropped."
    )
    for k in ("suspend_latency_ms", "suspend_p90_ms"):
        assert k in emitted, f"n>=2 producer must emit {k!r}: {emitted}"

    # (1) the emitted sla dict survives the SAME closed coercion run.py writes to latest.json,
    # and every survivor passes the independent render predicate.
    coerced = _rs._coerce_sla_metrics(emitted)
    for k in ("suspend_latency_ms", "suspend_p90_ms"):
        assert k in coerced, (
            f"emit/render schema DRIFT on suspend_latency: closed coerce dropped {k!r} — the block "
            f"would go INERT.\nemitted={emitted}\ncoerced={coerced}"
        )
        assert schema.SUSPEND_LATENCY_FIELDS[k](coerced[k]), (
            f"render SUSPEND_LATENCY_FIELDS[{k!r}] rejects coerced value {coerced[k]!r} — "
            "the block would render nothing."
        )

    # (2) full path: a suspend_resume cell that ALSO carries the resume-TTFE pair (the real shape)
    # renders the suspend table, and the closed schema drops the foreign resume keys.
    def _results(sla):
        return {
            "product": "sandbox",
            "generated_at": "2026-07-02T03:00:00Z",
            "provenance": {"runtime": "runc", "cluster_substrate": "kind", "node_count": 1},
            "scenarios": [{"name": "suspend_resume", "outcome": "PASS", "n": 3, "sla_metrics": sla}],
        }

    live_sla = {**coerced, "ttfe_p50_ms": 3500.0, "ttfe_p95_ms": 5000.0}
    out = render.render_suspend_latency(_results(live_sla))
    assert "## Administrative Suspend Latency" in out
    assert "| Median (p50) | 0.2s |" in out, f"median row must render:\n{out}"
    assert "| Tail (p90) | 0.28s |" in out, f"the optional p90 tail row must render:\n{out}"
    assert "3.5s" not in out and "5s" not in out, "resume-TTFE keys must not leak into this block"
    # the capability note guards the reader against inferring an auto/idle-suspend capability.
    assert "no idle-timeout, activity-reclaim, or auto-suspend" in out

    # (3) INERT gates: single-sample helper output (median only, no tail), an empty {}, and a cell
    # carrying only the resume-TTFE keys all behave correctly.
    single = m.suspend_latency_point([4200.0])
    assert "suspend_p90_ms" not in single, "n=1 emits no honest tail"
    out_single = render.render_suspend_latency(_results(_rs._coerce_sla_metrics(single)))
    assert "| Median (p50) | 4.2s |" in out_single and "Tail (p90)" not in out_single
    assert render.render_suspend_latency(_results({})) == "", "empty sla_metrics -> INERT"
    assert render.render_suspend_latency(
        _results({"ttfe_p50_ms": 3500.0, "ttfe_p95_ms": 5000.0})
    ) == "", "a cell with only resume-TTFE keys (no suspend spine) -> INERT"


def test_emit_to_render_vcpu_footprint_convergence():
    """Convergence guard for the vCPU-footprint provenance axis (#3868).

    Unlike session_turnover (a SCENARIO CELL), the footprint is a RUN-LEVEL PROVENANCE field:
    the emitter writes `sandbox_cpu_request_m` / `sandbox_mem_request_mib` in run.build_provenance
    (only for sandbox-family products, next to `runtime`), and the render side allow-lists them via
    schema.PROVENANCE_FIELDS + reads them in render.render_vcpu_footprint. The emit vocabulary
    (build_provenance keys) and the render vocabulary (PROVENANCE_FIELDS + _footprint_from_provenance)
    are INDEPENDENT closed sets, so a drift — a renamed key, a predicate that rejects the emitted
    int, or an emit that stops setting one leg — would silently return the block to INERT (page
    byte-unchanged). This pins the contract through the REAL emitter: build_provenance -> the SAME
    _clean_provenance the renderer applies -> render_vcpu_footprint, asserting the gVisor and Kata
    footprints survive the closed-schema round-trip and render, while an emit WITHOUT the footprint
    legs (substrate, which omits `runtime`) renders "".
    """
    import schema

    sys.path.insert(0, _ROOT)
    try:
        from harness import run as _run
    except Exception as exc:  # pragma: no cover - harness has its own deps
        print(f"  (skip: harness run not importable: {exc})")
        return

    # (0) pin the emit-key names against the render allow-list — the TRUE emit vocabulary is the
    # set of keys build_provenance writes on a sandbox-family run; each must be allow-listed by
    # PROVENANCE_FIELDS or _clean_provenance would drop it before the renderer ever sees it.
    for emit_key in ("sandbox_cpu_request_m", "sandbox_mem_request_mib"):
        assert emit_key in schema.PROVENANCE_FIELDS, (
            f"emit/render DRIFT: build_provenance emits {emit_key!r} but render PROVENANCE_FIELDS "
            f"does not allow-list it — _clean_provenance would silently drop it (block INERT)."
        )

    # (1) the REAL emitter output for both matrix runtimes survives the SAME _clean_provenance the
    # renderer applies, and every survivor passes the independent render predicate.
    gvisor_prov = _run.build_provenance("gke-sandbox", "sandbox")
    kata_prov = _run.build_provenance("gke-sandbox", "sandbox-kata")
    assert gvisor_prov.get("runtime") == "gvisor" and kata_prov.get("runtime") == "kata-microvm", (
        f"build_provenance runtime drift: gvisor={gvisor_prov.get('runtime')!r} "
        f"kata={kata_prov.get('runtime')!r}"
    )
    for prov, want in ((gvisor_prov, (10, 16)), (kata_prov, (500, 512))):
        cleaned = render._clean_provenance(prov)
        for k in ("sandbox_cpu_request_m", "sandbox_mem_request_mib"):
            assert k in cleaned, (
                f"emit/render schema DRIFT: _clean_provenance dropped {k!r} from a real "
                f"build_provenance emit — the footprint block would go INERT.\n"
                f"emitted={prov}\ncleaned={cleaned}"
            )
            assert schema.PROVENANCE_FIELDS[k](cleaned[k]), (
                f"render PROVENANCE_FIELDS[{k!r}] rejects emitted value {cleaned[k]!r} — INERT."
            )
        assert (cleaned["sandbox_cpu_request_m"], cleaned["sandbox_mem_request_mib"]) == want, (
            f"footprint drift: expected {want}, got "
            f"({cleaned['sandbox_cpu_request_m']}, {cleaned['sandbox_mem_request_mib']})"
        )

    # (2) full path: the gVisor primary emit renders its own row measured; the Kata companion
    # artifact fills the kata-microvm row.
    gvisor_results = {
        "product": "sandbox",
        "generated_at": "2026-07-02T03:00:00Z",
        "provenance": gvisor_prov,
        "scenarios": [],
    }
    kata_results = {
        "product": "sandbox-kata",
        "generated_at": "2026-07-02T03:00:00Z",
        "provenance": kata_prov,
        "scenarios": [],
    }
    out = render.render_vcpu_footprint(gvisor_results, kata_results=kata_results)
    assert "## Per-Sandbox Footprint (declared request)" in out
    assert "| gVisor | 10m | 16Mi |" in out, f"gVisor footprint row must render measured:\n{out}"
    assert "| Kata + microVM | 500m | 512Mi |" in out, (
        f"the Kata companion artifact must fill the kata-microvm row:\n{out}"
    )

    # (3) INERT gate: a REAL substrate emit omits `runtime`, so build_provenance never sets the
    # footprint legs — the block must render "".
    substrate_prov = _run.build_provenance("kind", "substrate")
    assert "sandbox_cpu_request_m" not in substrate_prov, (
        "substrate build_provenance must not emit a footprint (no `runtime`)"
    )
    inert = {
        "product": "substrate",
        "generated_at": "2026-07-02T03:00:00Z",
        "provenance": substrate_prov,
        "scenarios": [],
    }
    assert render.render_vcpu_footprint(inert) == "", (
        f"a provenance emit without footprint legs must render INERT (''): {substrate_prov!r}"
    )


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok: {fn.__name__}")
    print(f"test_cross_contract: all {len(fns)} assertions passed")


if __name__ == "__main__":
    _run_all()
