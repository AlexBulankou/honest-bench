"""Closed-schema render tests. Dependency-free: `python3 test_render.py` (exit 0 = pass).

These assert the Layer-1 PII guard holds: anything not declared in schema.py is dropped,
goal columns are always (non-public), and harness free-text can never reach the output.
"""

import render
from schema import NON_PUBLIC


def _render(results):
    return render.render_product(results)


def test_unknown_scenario_name_dropped_and_counted():
    out = _render(
        {
            "product": "sandbox",
            "scenarios": [
                {"name": "warmpool_cold_start", "outcome": "PASS", "n": 5},
                {"name": "exfil_secret_dump", "outcome": "PASS", "n": 5},
            ],
        }
    )
    assert "Warm-pool activation (hit)" in out
    assert "exfil_secret_dump" not in out
    assert "rows dropped by closed-schema guard: 1" in out


def test_unknown_metric_dropped():
    out = _render(
        {
            "product": "sandbox",
            "scenarios": [
                {
                    "name": "warmpool_cold_start",
                    "outcome": "PASS",
                    "n": 5,
                    "sla_metrics": {"activation_ms": 180, "internal_secret_ms": 7},
                }
            ],
        }
    )
    assert "Activation (ms) 180" in out
    assert "internal_secret_ms" not in out
    assert "7" not in out.split("Activation (ms) 180")[1].split("\n")[0]


def test_goal_columns_always_non_public():
    out = _render(
        {
            "product": "substrate",
            "scenarios": [{"name": "cold_reconcile", "outcome": "PASS", "n": 3}],
        }
    )
    # three goal cells per row, all (non-public)
    assert out.count(NON_PUBLIC) == 3


def test_bad_outcome_enum_dropped():
    out = _render(
        {
            "product": "sandbox",
            "scenarios": [
                {"name": "warmpool_cold_start", "outcome": "EXPLODED", "n": 5},
            ],
        }
    )
    assert "Warm-pool activation (hit)" not in out
    assert "rows dropped by closed-schema guard: 1" in out


def test_free_text_pending_reason_dropped_row_kept():
    out = _render(
        {
            "product": "sandbox",
            "scenarios": [
                {
                    "name": "gvisor_canary",
                    "outcome": "pending",
                    "pending_reason": "SYNTHETIC-FREETEXT-LEAK-MARKER",
                    "n": 0,
                }
            ],
        }
    )
    assert "gVisor isolation canary" in out  # row kept
    assert "SYNTHETIC-FREETEXT-LEAK-MARKER" not in out  # free-text scrubbed
    assert "pending (not-yet-measured)" in out  # fell back to enum default


def test_bad_controller_digest_rejected():
    out = _render(
        {
            "product": "sandbox",
            "provenance": {"controller_digest": "sha256:NOT-HEX-PROVENANCE-LEAK"},
            "scenarios": [{"name": "warmpool_cold_start", "outcome": "PASS", "n": 1}],
        }
    )
    assert "NOT-HEX-PROVENANCE-LEAK" not in out
    assert "controller_digest" not in out


def test_unknown_provenance_field_dropped():
    out = _render(
        {
            "product": "sandbox",
            "provenance": {
                "cluster_substrate": "gke-sandbox",
                "internal_cluster_name": "SYNTHETIC-INTERNAL-CLUSTER-NAME",
                # GCP project id is not allow-listed: infra-noise, off the public page by
                # construction even though the id itself is public (a4z1 corp-audit, #3876).
                "project": "SYNTHETIC-GCP-PROJECT-ID",
            },
            "scenarios": [{"name": "warmpool_cold_start", "outcome": "PASS", "n": 1}],
        }
    )
    assert "cluster_substrate=gke-sandbox" in out
    assert "SYNTHETIC-INTERNAL-CLUSTER-NAME" not in out
    assert "internal_cluster_name" not in out
    assert "SYNTHETIC-GCP-PROJECT-ID" not in out
    assert "project" not in out


def test_cold_start_mode_labeled_on_cold_start_cell():
    # #3894: a valid cold_start_mode in provenance renders next to the native_digest_cold
    # cell's cold_start_ms, so a cold-pull number (includes layer download) is not misread
    # as a warm-cached cold-provision one.
    out = _render(
        {
            "product": "sandbox",
            "provenance": {"cold_start_mode": "cold-pull"},
            "scenarios": [
                {
                    "name": "native_digest_cold",
                    "outcome": "PASS",
                    "n": 20,
                    "sla_metrics": {"cold_start_ms": 4200},
                }
            ],
        }
    )
    assert "Cold start (ms) 4200 (cold-pull)" in out
    # cold_start_mode renders ONLY on the cell, never in the build banner
    assert "cold_start_mode=" not in out


def test_cold_start_mode_absent_renders_no_label():
    # Absent ⇒ no label (graceful degradation on the empty-provenance seed): the cell
    # renders the bare metric, unchanged from pre-#3894 behavior.
    out = _render(
        {
            "product": "sandbox",
            "scenarios": [
                {
                    "name": "native_digest_cold",
                    "outcome": "PASS",
                    "n": 20,
                    "sla_metrics": {"cold_start_ms": 4200},
                }
            ],
        }
    )
    assert "Cold start (ms) 4200" in out
    assert "(cold-pull)" not in out
    assert "(cold-provision)" not in out


def test_cold_start_mode_invalid_dropped_no_label():
    # The render guard is SECONDARY (the harness emitter fail-closes on a typo); an
    # out-of-enum value is simply dropped here, so no bogus label leaks and the raw value
    # never reaches the page.
    out = _render(
        {
            "product": "sandbox",
            "provenance": {"cold_start_mode": "warm-cached-lie"},
            "scenarios": [
                {
                    "name": "native_digest_cold",
                    "outcome": "PASS",
                    "n": 20,
                    "sla_metrics": {"cold_start_ms": 4200},
                }
            ],
        }
    )
    assert "Cold start (ms) 4200" in out
    assert "warm-cached-lie" not in out


def test_cold_start_mode_no_label_when_no_cold_start_ms():
    # A valid mode but a non-cold-start cell (no cold_start_ms metric) ⇒ the label has
    # nothing to attach to and is not rendered: the mode describes the cold-start
    # measurement, not the run at large.
    out = _render(
        {
            "product": "sandbox",
            "provenance": {"cold_start_mode": "cold-pull"},
            "scenarios": [
                {
                    "name": "warmpool_cold_start",
                    "outcome": "PASS",
                    "n": 20,
                    "sla_metrics": {"activation_ms": 180},
                }
            ],
        }
    )
    assert "Activation (ms) 180" in out
    assert "(cold-pull)" not in out


def test_badge_scope_suffixes_isolation_pass_cell():
    # #3905: a valid badge_scope renders as a suffix on the scenario's PASS cell, so the
    # public badge says exactly what the PASS asserts (control-plane admission, not
    # data-plane enforcement) — data-driven, no hardcoded label.
    out = _render(
        {
            "product": "sandbox",
            "scenarios": [
                {
                    "name": "cross_tenant_network_isolation",
                    "outcome": "PASS",
                    "badge_scope": "control-plane",
                    "n": 12,
                }
            ],
        }
    )
    # label is now PLAIN; the qualifier rides the cell
    assert "Cross-tenant network isolation |" in out
    assert "Cross-tenant network isolation (control-plane)" not in out
    assert "PASS (control-plane) (n=12)" in out


def test_badge_scope_absent_renders_bare_pass():
    # Absent ⇒ no suffix (graceful degradation): an isolation cell with no badge_scope
    # renders a bare PASS, unchanged from pre-#3905 behavior.
    out = _render(
        {
            "product": "sandbox",
            "scenarios": [
                {"name": "gvisor_canary", "outcome": "PASS", "n": 0},
            ],
        }
    )
    assert "gVisor isolation canary | PASS (n=0)" in out
    assert "(control-plane)" not in out
    assert "(enforced)" not in out


def test_badge_scope_invalid_dropped_bare_pass():
    # The render guard is SECONDARY (the emitter fail-closes on a typo); an out-of-enum
    # value is simply dropped here, so no bogus scope leaks onto the public badge.
    out = _render(
        {
            "product": "sandbox",
            "scenarios": [
                {
                    "name": "default_deny_egress",
                    "outcome": "PASS",
                    "badge_scope": "fully-bulletproof-trust-me",
                    "n": 12,
                }
            ],
        }
    )
    assert "Default-deny egress | PASS (n=12)" in out
    assert "fully-bulletproof-trust-me" not in out


def test_badge_scope_enforced_value_renders():
    # The enum's second member: a cell that DID exercise data-plane enforcement can move
    # to "enforced" by emitting the new value — no label edit, the render follows the data.
    out = _render(
        {
            "product": "sandbox",
            "scenarios": [
                {
                    "name": "cross_tenant_network_isolation",
                    "outcome": "PASS",
                    "badge_scope": "enforced",
                    "n": 12,
                }
            ],
        }
    )
    assert "PASS (enforced) (n=12)" in out


def test_unknown_product_raises():
    try:
        _render({"product": "internal-prod", "scenarios": []})
    except ValueError:
        return
    raise AssertionError("expected ValueError for unknown product")


def test_bad_generated_at_dropped():
    out = _render(
        {
            "product": "sandbox",
            "generated_at": "yesterday-ish (ask alex)",
            "scenarios": [{"name": "warmpool_cold_start", "outcome": "PASS", "n": 1}],
        }
    )
    assert "generated-at" not in out
    assert "ask alex" not in out


def test_burst_throughput_headline_renders():
    # alex 2026-06-28 HB headline: whole-burst count + per-vCPU density (count, not latency).
    out = _render(
        {
            "product": "sandbox",
            "scenarios": [
                {
                    "name": "burst_create",
                    "outcome": "PASS",
                    "n": 200,
                    "sla_metrics": {"sandboxes_ready_under_1s": 4, "density_per_vcpu": 1.88},
                }
            ],
        }
    )
    assert "Burst create throughput" in out
    assert "Sandboxes ready <1s 4" in out
    assert "Density /vCPU 1.88" in out


def _hrow(**over):
    # A schema-valid history row; tests override individual fields.
    row = {
        "generated_at": "2026-06-28T14:42:40Z",
        "controller_digest": "sha256:6edaf7b6b22d9dfaf6ab077cd1c6517acf5fc6cf96b1ad58fe83bcfd477977ec",
        "suite_git_sha": "c88d857",
        "run_id": "a0e4f0ffae12440a826ac40a277f21f3",
        "cluster_substrate": "gke-sandbox",
        "sandboxes_ready_under_1s": 9,
        "density_per_vcpu": 0.45,
        "n": 10,
    }
    row.update(over)
    return row


def test_trend_empty_renders_nothing():
    # #3918: no history (empty seed) ⇒ no trend section (graceful degradation; the page
    # never half-renders an empty table).
    assert render.render_trend([]) == ""
    assert render.render_trend(None) == ""


def test_trend_single_build_is_baseline_no_delta():
    out = render.render_trend([_hrow()])
    assert "Throughput — build-over-build" in out
    assert "`sha256:6edaf7b6b22d…`" in out
    assert "2026-06-28" in out
    # baseline row: COUNT present, delta is the em-dash placeholder (no prior build)
    assert "| 9 | — | 0.45 | 10 |" in out


def test_trend_two_builds_show_signed_delta():
    # #3918 DoD: build-over-build deltas are REAL (computed from the two COUNTs), not asserted.
    rows = [
        _hrow(controller_digest="sha256:" + "a" * 64, generated_at="2026-06-27T10:00:00Z",
              sandboxes_ready_under_1s=9),
        _hrow(controller_digest="sha256:" + "b" * 64, generated_at="2026-06-28T10:00:00Z",
              sandboxes_ready_under_1s=14),
    ]
    out = render.render_trend(rows)
    # newer build shows +5 vs the prior build's 9
    assert "| 14 | +5 | " in out
    # baseline (older) build still has the em-dash
    assert "| 9 | — | " in out


def test_trend_negative_delta_signed():
    rows = [
        _hrow(controller_digest="sha256:" + "a" * 64, generated_at="2026-06-27T10:00:00Z",
              sandboxes_ready_under_1s=14),
        _hrow(controller_digest="sha256:" + "b" * 64, generated_at="2026-06-28T10:00:00Z",
              sandboxes_ready_under_1s=11),
    ]
    out = render.render_trend(rows)
    assert "| 11 | -3 | " in out


def test_trend_orders_oldest_first_regardless_of_input_order():
    rows = [
        _hrow(controller_digest="sha256:" + "b" * 64, generated_at="2026-06-28T10:00:00Z",
              sandboxes_ready_under_1s=14),
        _hrow(controller_digest="sha256:" + "a" * 64, generated_at="2026-06-27T10:00:00Z",
              sandboxes_ready_under_1s=9),
    ]
    out = render.render_trend(rows)
    # the 2026-06-27 (count 9, baseline) row must precede the 2026-06-28 (count 14) row
    assert out.index("2026-06-27") < out.index("2026-06-28")
    assert "| 9 | — | " in out
    assert "| 14 | +5 | " in out


def test_trend_malformed_row_dropped():
    # A row failing a closed-schema predicate (bad digest) is dropped entirely — same guard
    # as the per-product render; a malformed history degrades to fewer rows, never a leak.
    rows = [
        _hrow(controller_digest="sha256:NOT-HEX-LEAK-MARKER"),
        _hrow(controller_digest="sha256:" + "c" * 64),
    ]
    out = render.render_trend(rows)
    assert "NOT-HEX-LEAK-MARKER" not in out
    # exactly one data row survives (one digest cell rendered)
    assert out.count("`sha256:") == 1


def test_trend_missing_field_dropped():
    bad = _hrow()
    del bad["sandboxes_ready_under_1s"]
    out = render.render_trend([bad, _hrow(controller_digest="sha256:" + "d" * 64)])
    assert out.count("`sha256:") == 1


def test_trend_unknown_field_not_rendered():
    # An extra key beyond the closed schema does not reach the page (closed-schema project).
    out = render.render_trend([_hrow(internal_note="SYNTHETIC-HISTORY-LEAK")])
    assert "SYNTHETIC-HISTORY-LEAK" not in out


# --- Goal 2.1: Core Metrics matrix + Scale Proof render tests --------------------------------


def _matrix_results(scenarios, provenance=None, **top):
    r = {"product": "sandbox", "scenarios": scenarios}
    if provenance is not None:
        r["provenance"] = provenance
    r.update(top)
    return r


def _full_gvisor_scenarios():
    # doc target row values for the three gVisor activation modes (TTFE in ms; thpt per node).
    return [
        {
            "name": "warmpool_cold_start", "outcome": "PASS", "n": 200,
            "sla_metrics": {
                "thpt_under_5s_per_node": 4, "thpt_under_1s_per_node": 4,
                "ttfe_p50_ms": 600, "ttfe_p95_ms": 900,
                "exec_success_rate": 1.0, "density_per_vcpu": 1.88,
            },
        },
        {
            "name": "native_digest_cold", "outcome": "PASS", "n": 200,
            "sla_metrics": {
                "thpt_under_5s_per_node": 4, "thpt_under_1s_per_node": 0,
                "ttfe_p50_ms": 1200, "ttfe_p95_ms": 1560, "exec_success_rate": 1.0,
            },
        },
        {
            "name": "suspend_resume", "outcome": "PASS", "n": 1376,
            "sla_metrics": {
                "thpt_under_5s_per_node": 4, "thpt_under_1s_per_node": 0,
                "ttfe_p50_ms": 3500, "ttfe_p95_ms": 5000, "exec_success_rate": 0.9281,
            },
        },
    ]


def test_matrix_renders_doc_exact_gvisor_rows():
    out = render.render_matrix(_matrix_results(_full_gvisor_scenarios()))
    assert "| gVisor | Warm-pool hit (Base image) | 4 | 4 | 0.6s | 0.9s | 200 | 1.88 | 100% |" in out
    assert "| gVisor | Unique-image cold (RL reality) | 4 | 0 | 1.2s | 1.56s | 200 | 1.88 | 100% |" in out
    assert "| gVisor | Resume-from-suspend | 4 | 0 | 3.5s | 5s | 1376 | N/A | 92.8% (1277/1376) ⚠️ |" in out


def test_matrix_honest_zero_throughput_not_rounded():
    # the cold + resume rows print a literal 0 for throughput@<1s (p95 misses the bar).
    out = render.render_matrix(_matrix_results(_full_gvisor_scenarios()))
    # the <1s column on the cold row is exactly "0", never "pending" or a rounded-up value
    cold_line = [l for l in out.splitlines() if "Unique-image cold" in l][0]
    cells = [c.strip() for c in cold_line.strip("|").split("|")]
    assert cells[3] == "0"  # Throughput @ <1s TTFE column


def test_matrix_exec_success_n_emitted_preferred_over_derived():
    # when the harness DOES emit exec_success_n, the fraction uses it verbatim (not rate*N).
    scen = _full_gvisor_scenarios()
    scen[2]["sla_metrics"]["exec_success_n"] = 1300  # deliberately != round(rate*N)=1277
    out = render.render_matrix(_matrix_results(scen))
    assert "92.8% (1300/1376) ⚠️" in out
    assert "(1277/1376)" not in out


def test_matrix_resume_density_is_na():
    out = render.render_matrix(_matrix_results(_full_gvisor_scenarios()))
    resume_line = [l for l in out.splitlines() if "Resume-from-suspend" in l and "gVisor" in l][0]
    cells = [c.strip() for c in resume_line.strip("|").split("|")]
    assert cells[7] == "N/A"  # Max Density column is N/A for resume (no steady-state pool)


def test_matrix_kata_rows_all_pending():
    out = render.render_matrix(_matrix_results(_full_gvisor_scenarios()))
    kata_lines = [l for l in out.splitlines() if l.startswith("| Kata + microVM |")]
    assert len(kata_lines) == 3
    for l in kata_lines:
        cells = [c.strip() for c in l.strip("|").split("|")]
        # every measured column on an unmeasured runtime is pending; density warm/cold pending,
        # resume N/A
        assert cells[2] == "pending" and cells[3] == "pending"
        assert cells[4] == "pending" and cells[5] == "pending"
        assert cells[6] == "pending"


def test_matrix_unknown_metric_key_dropped():
    scen = _full_gvisor_scenarios()
    scen[0]["sla_metrics"]["internal_secret_ms"] = 7
    scen[0]["sla_metrics"]["SYNTHETIC_LEAK"] = "exfil"
    out = render.render_matrix(_matrix_results(scen))
    assert "internal_secret_ms" not in out
    assert "SYNTHETIC_LEAK" not in out
    assert "exfil" not in out


def test_matrix_unknown_scenario_does_not_pollute():
    # an unknown activation scenario name is simply not addressed by any matrix row.
    scen = _full_gvisor_scenarios() + [
        {"name": "exfil_secret_dump", "outcome": "PASS", "n": 5,
         "sla_metrics": {"ttfe_p50_ms": 1}},
    ]
    out = render.render_matrix(_matrix_results(scen))
    assert "exfil_secret_dump" not in out


def test_matrix_runtime_provenance_selects_measured_rows():
    # measured runtime rides provenance.runtime; kata-measured run fills Kata rows, gVisor pends.
    scen = [
        {
            "name": "warmpool_cold_start", "outcome": "PASS", "n": 50,
            "sla_metrics": {"ttfe_p50_ms": 700, "ttfe_p95_ms": 950, "exec_success_rate": 1.0},
        },
    ]
    out = render.render_matrix(_matrix_results(scen, provenance={"runtime": "kata-microvm"}))
    kata_warm = [l for l in out.splitlines()
                 if l.startswith("| Kata + microVM | Warm-pool hit")][0]
    assert "0.7s" in kata_warm and "0.95s" in kata_warm
    gvisor_warm = [l for l in out.splitlines()
                   if l.startswith("| gVisor | Warm-pool hit")][0]
    assert "0.7s" not in gvisor_warm  # gVisor un-measured this run


def test_matrix_invalid_runtime_provenance_dropped_defaults_gvisor():
    # an out-of-enum runtime fails the provenance predicate (dropped) ⇒ default gvisor measured.
    scen = _full_gvisor_scenarios()
    out = render.render_matrix(_matrix_results(scen, provenance={"runtime": "trust-me-vm"}))
    assert "trust-me-vm" not in out
    assert "| gVisor | Warm-pool hit (Base image) | 4 | 4 | 0.6s | 0.9s | 200 | 1.88 | 100% |" in out


def test_matrix_empty_metrics_renders_pending_skeleton():
    scen = [{"name": "warmpool_cold_start", "outcome": "PASS", "n": 5}]
    out = render.render_matrix(_matrix_results(scen))
    warm = [l for l in out.splitlines() if l.startswith("| gVisor | Warm-pool hit")][0]
    cells = [c.strip() for c in warm.strip("|").split("|")]
    assert cells[2] == "pending" and cells[4] == "pending" and cells[8] == "pending"
    # N is still shown from the scenario count
    assert cells[6] == "5"


def test_matrix_unknown_product_raises():
    try:
        render.render_matrix({"product": "internal-prod", "scenarios": []})
    except ValueError:
        return
    raise AssertionError("expected ValueError for unknown product")


def test_matrix_bad_generated_at_dropped():
    out = render.render_matrix(
        _matrix_results(_full_gvisor_scenarios(), generated_at="yesterday-ish (ask alex)")
    )
    assert "generated-at" not in out
    assert "ask alex" not in out


def test_scale_proof_renders_linearity_row():
    results = _matrix_results(
        _full_gvisor_scenarios(),
        scale_proof={
            "scale_points": [
                {"node_count": 1, "density": 1.88},
                {"node_count": 2, "density": 1.86},
                {"node_count": 4, "density": 1.85},
            ],
            "density_retention": 0.984,
            "thpt_retention": 0.99,
        },
    )
    out = render.render_scale_proof(results)
    assert "## Scale Proof (Linearity Check)" in out
    assert "| 1 → 2 → 4 | ✅ Yes (1.88 → 1.86 → 1.85) | ✅ Yes |" in out


def test_scale_proof_absent_renders_nothing():
    assert render.render_scale_proof(_matrix_results(_full_gvisor_scenarios())) == ""


def test_scale_proof_out_of_band_retention_flags_no():
    results = _matrix_results(
        _full_gvisor_scenarios(),
        scale_proof={
            "scale_points": [
                {"node_count": 1, "density": 1.88},
                {"node_count": 4, "density": 1.0},
            ],
            "thpt_retention": 0.5,
        },
    )
    out = render.render_scale_proof(results)
    # density_retention derives from points (1.0/1.88 ≈ 0.53 < 0.9) ⇒ ⚠️ No; thpt 0.5 ⇒ ⚠️ No
    assert "⚠️ No" in out
    assert "✅ Yes" not in out


def test_scale_proof_density_retention_derived_when_absent():
    results = _matrix_results(
        _full_gvisor_scenarios(),
        scale_proof={
            "scale_points": [
                {"node_count": 1, "density": 2.0},
                {"node_count": 2, "density": 1.9},
            ],
        },
    )
    out = render.render_scale_proof(results)
    # 1.9/2.0 = 0.95 within ±10% ⇒ ✅ Yes; thpt absent ⇒ pending
    assert "✅ Yes (2 → 1.9)" in out
    assert "pending" in out  # thpt_retention column


def test_scale_proof_orders_points_by_node_count():
    results = _matrix_results(
        _full_gvisor_scenarios(),
        scale_proof={
            "scale_points": [
                {"node_count": 4, "density": 1.85},
                {"node_count": 1, "density": 1.88},
                {"node_count": 2, "density": 1.86},
            ],
        },
    )
    out = render.render_scale_proof(results)
    assert "1 → 2 → 4" in out


def test_scale_proof_malformed_points_dropped():
    # a scale_points value failing the closed-schema predicate ⇒ no table (never a leak).
    results = _matrix_results(
        _full_gvisor_scenarios(),
        scale_proof={"scale_points": "SYNTHETIC-SCALE-LEAK"},
    )
    assert render.render_scale_proof(results) == ""


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok: {fn.__name__}")
    print(f"test_render: all {len(fns)} assertions passed")


if __name__ == "__main__":
    _run_all()
