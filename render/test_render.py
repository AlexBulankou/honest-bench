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


def test_badge_construction_renders_as_second_term_with_scope():
    # #3950: badge_construction is an ORTHOGONAL second term naming WHICH NP mechanism was
    # measured. With a scope present it renders "PASS (<scope>, <construction>)" so an
    # `enforced` flip discloses the standard-NP-with-label-propagation mechanism and can
    # never be read as a managed-gke-sandbox-NP guarantee.
    out = _render(
        {
            "product": "sandbox",
            "scenarios": [
                {
                    "name": "cross_tenant_network_isolation",
                    "outcome": "PASS",
                    "badge_scope": "enforced",
                    "badge_construction": "standard-np",
                    "n": 12,
                }
            ],
        }
    )
    assert "PASS (enforced, standard-np) (n=12)" in out


def test_badge_construction_without_scope_renders_nothing():
    # construction qualifies the enforcement claim and is meaningless alone, so a cell that
    # carries a construction but NO scope renders a bare PASS (the construction is suppressed,
    # not promoted to a lone suffix that would assert nothing).
    out = _render(
        {
            "product": "sandbox",
            "scenarios": [
                {
                    "name": "default_deny_egress",
                    "outcome": "PASS",
                    "badge_construction": "standard-np",
                    "n": 12,
                }
            ],
        }
    )
    assert "Default-deny egress | PASS (n=12)" in out
    assert "standard-np" not in out


def test_badge_construction_invalid_dropped_scope_survives():
    # The render guard is SECONDARY (the emitter fail-closes); an out-of-enum construction is
    # dropped while a valid scope still renders — no free-text leaks onto the public badge.
    out = _render(
        {
            "product": "sandbox",
            "scenarios": [
                {
                    "name": "cross_tenant_network_isolation",
                    "outcome": "PASS",
                    "badge_scope": "control-plane",
                    "badge_construction": "magic-firewall-9000",
                    "n": 12,
                }
            ],
        }
    )
    assert "PASS (control-plane) (n=12)" in out
    assert "magic-firewall-9000" not in out


def test_badge_construction_managed_np_value_renders():
    # The enum's second member: a cell measured against the managed gke-sandbox NP discloses
    # "managed-np" so the reader knows a control-plane PASS rode the inert-podSelector path.
    out = _render(
        {
            "product": "sandbox",
            "scenarios": [
                {
                    "name": "default_deny_egress",
                    "outcome": "PASS",
                    "badge_scope": "control-plane",
                    "badge_construction": "managed-np",
                    "n": 12,
                }
            ],
        }
    )
    assert "PASS (control-plane, managed-np) (n=12)" in out


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
    # hb#132: throughput cells are dual `<node> /node · <cluster>`; with no per-cluster field
    # landed, the cluster half pends `pending (cluster-fire)` while the per-node half is exact.
    cf = f"pending ({render._CLUSTER_FIRE})"
    # hb#142: each TTFE cell carries its sample count inline as `value (count=N)`.
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


def test_matrix_low_n_ttfe_cells_marked():
    # a4z1 footgun: a low-N row's TTFE must not read as comparable to a high-N row. A row whose
    # N is below TTFE_COMPARABILITY_MIN_N gets the small-sample dagger on BOTH TTFE cells; the
    # high-N warm-pool row stays unmarked.
    scen = _full_gvisor_scenarios()
    scen[1]["n"] = 1  # cold row: single sample (the inverting case a4z1 flagged)
    out = render.render_matrix(_matrix_results(scen))
    cold_line = [l for l in out.splitlines() if "Unique-image cold" in l][0]
    cells = [c.strip() for c in cold_line.strip("|").split("|")]
    # hb#142: inline count precedes the low-N dagger — `value (count=N) †`.
    assert cells[4] == f"1.2s (count=1) {render._LOW_N_MARK}"  # TTFE p50 marked
    assert cells[5] == f"1.56s (count=1) {render._LOW_N_MARK}"  # TTFE p95 marked
    # the high-N warm-pool row is NOT marked
    warm_line = [l for l in out.splitlines() if "Warm-pool hit" in l][0]
    assert render._LOW_N_MARK not in warm_line


def test_matrix_low_n_marker_caveat_and_footnote_present():
    # the cross-row caveat + the low-N marker gloss render whenever the matrix renders — they are
    # static honesty scaffolding (now carried once in the "How to read the cells" legend, hb#134
    # nit-1 dedup), not data-gated.
    out = render.render_matrix(_matrix_results(_full_gvisor_scenarios()))
    assert "Read TTFE *down* a column, not across rows" in out
    assert render._LOW_N_MARK in out  # caveat + footnote reference the marker glyph
    assert f"N={render.TTFE_COMPARABILITY_MIN_N}" in out


def test_matrix_at_floor_n_not_marked():
    # boundary: N exactly at the floor is comparable (the marker fires strictly below the floor).
    scen = _full_gvisor_scenarios()
    scen[1]["n"] = render.TTFE_COMPARABILITY_MIN_N  # exactly at floor
    out = render.render_matrix(_matrix_results(scen))
    cold_line = [l for l in out.splitlines() if "Unique-image cold" in l][0]
    cells = [c.strip() for c in cold_line.strip("|").split("|")]
    assert cells[4] == "1.2s (count=30)"  # inline count, no marker at the floor
    assert cells[5] == "1.56s (count=30)"


def test_matrix_pending_ttfe_never_marked_even_low_n():
    # a low-N row whose TTFE metric is ABSENT renders `pending`, never `pending †` — the marker
    # qualifies a measurement, not a missing cell.
    scen = [
        {"name": "native_digest_cold", "outcome": "PASS", "n": 2,
         "sla_metrics": {"thpt_under_5s_per_node": 4}},
    ]
    out = render.render_matrix(_matrix_results(scen))
    cold_line = [l for l in out.splitlines() if "Unique-image cold" in l][0]
    cells = [c.strip() for c in cold_line.strip("|").split("|")]
    assert cells[4] == "pending"  # TTFE p50 absent → pending, unmarked
    assert cells[5] == "pending"


def test_matrix_honest_zero_throughput_not_rounded():
    # the cold + resume rows print a literal 0 for throughput@<1s (p95 misses the bar).
    out = render.render_matrix(_matrix_results(_full_gvisor_scenarios()))
    # the per-node half of the <1s cell is exactly "0", never "pending" or a rounded-up value.
    cold_line = [l for l in out.splitlines() if "Unique-image cold" in l][0]
    cells = [c.strip() for c in cold_line.strip("|").split("|")]
    assert cells[3] == f"0 /node · pending ({render._CLUSTER_FIRE})"  # Throughput @ <1s TTFE


def test_matrix_dual_throughput_cluster_figure_above_target_clean():
    # hb#132: when OUR schema-validated fire lands per-cluster figures at/above the sizing target,
    # the cluster half renders `<X> /cluster` with NO ⚠️, and the @X-nodes caption resolves.
    scen = _full_gvisor_scenarios()
    scen[0]["sla_metrics"].update(
        {
            "thpt_under_5s_per_cluster": 350,
            "thpt_under_1s_per_cluster": 320,
            "thpt_cluster_node_count": 40,
        }
    )
    out = render.render_matrix(_matrix_results(scen))
    warm_line = [l for l in out.splitlines() if "Warm-pool hit" in l][0]
    cells = [c.strip() for c in warm_line.strip("|").split("|")]
    assert cells[2] == "4 /node · 350 /cluster"
    assert cells[3] == "4 /node · 320 /cluster"
    assert "⚠️" not in cells[2] and "⚠️" not in cells[3]
    assert "at 40 nodes" in out  # X caption resolved from thpt_cluster_node_count


def test_matrix_dual_throughput_cluster_figure_below_target_flagged():
    # hb#132: a landed per-cluster figure BELOW the sizing target is printed REAL (never zeroed or
    # hidden) and carries ⚠️ — the honest under-target signal for the Phase-1 controller-limited cut.
    scen = _full_gvisor_scenarios()
    scen[0]["sla_metrics"].update(
        {
            "thpt_under_5s_per_cluster": 148,
            "thpt_under_1s_per_cluster": 148,
            "thpt_cluster_node_count": 40,
        }
    )
    out = render.render_matrix(_matrix_results(scen))
    warm_line = [l for l in out.splitlines() if "Warm-pool hit" in l][0]
    cells = [c.strip() for c in warm_line.strip("|").split("|")]
    assert cells[2] == "4 /node · 148 /cluster ⚠️"
    assert cells[3] == "4 /node · 148 /cluster ⚠️"


def test_matrix_dual_throughput_at_target_not_flagged():
    # boundary: a per-cluster figure EXACTLY at the target is clean (⚠️ fires strictly below).
    scen = _full_gvisor_scenarios()
    scen[0]["sla_metrics"].update(
        {"thpt_under_5s_per_cluster": render.CLUSTER_THROUGHPUT_TARGET, "thpt_cluster_node_count": 40}
    )
    out = render.render_matrix(_matrix_results(scen))
    warm_line = [l for l in out.splitlines() if "Warm-pool hit" in l][0]
    cells = [c.strip() for c in warm_line.strip("|").split("|")]
    assert cells[2] == f"4 /node · {render.CLUSTER_THROUGHPUT_TARGET} /cluster"
    assert "⚠️" not in cells[2]


def test_matrix_dual_throughput_caption_pending_without_cluster_fire():
    # with no per-cluster field landed, the dual caption renders the pending-branch text and the
    # cluster halves pend — no @X-nodes figure is invented.
    out = render.render_matrix(_matrix_results(_full_gvisor_scenarios()))
    assert "Throughput is dual" in out
    assert f"pending ({render._CLUSTER_FIRE})" in out
    assert "at 40 nodes" not in out  # no fabricated X without a landed node count


def test_matrix_cluster_half_gated_on_node_count_presence():
    # hb#132 render gate (defense-in-depth; the emit side couples the triple all-or-nothing):
    # a per_cluster figure WITHOUT thpt_cluster_node_count in the same metrics dict has no X to
    # disclose, so the cluster half pends rather than rendering a real rate under a caption
    # stuck on the pending-branch text (the X-less-per_cluster ambiguity).
    scen = _full_gvisor_scenarios()
    scen[0]["sla_metrics"].update(
        {"thpt_under_5s_per_cluster": 350, "thpt_under_1s_per_cluster": 320}
        # deliberately NO thpt_cluster_node_count
    )
    out = render.render_matrix(_matrix_results(scen))
    warm_line = [l for l in out.splitlines() if "Warm-pool hit" in l][0]
    cells = [c.strip() for c in warm_line.strip("|").split("|")]
    assert cells[2] == f"4 /node · pending ({render._CLUSTER_FIRE})"
    assert cells[3] == f"4 /node · pending ({render._CLUSTER_FIRE})"
    assert "/cluster" not in cells[2] and "/cluster" not in cells[3]
    # caption stays on the pending branch — no X was landed anywhere
    assert "at 40 nodes" not in out
    assert "until a schema-validated per-mode cluster-throughput fire lands them" in out
    # the Saturation-ceiling distinction is carried once in the legend (hb#134 nit-1 dedup),
    # not repeated in the caption above the table.
    assert "distinct from the whole-cluster Saturation ceiling in DETAILS" in out


def test_matrix_mixed_x_caption_names_each_runtime():
    # hb#132 mixed-X: gVisor's cluster leg at X=40 and kata's at X=20 must NOT share a single
    # first-match X — the caption names each runtime's X and flags non-comparability.
    scen = _full_gvisor_scenarios()
    scen[0]["sla_metrics"].update(
        {
            "thpt_under_5s_per_cluster": 350,
            "thpt_under_1s_per_cluster": 320,
            "thpt_cluster_node_count": 40,
        }
    )
    kata_scen = [
        {
            "name": "warmpool_cold_start", "outcome": "PASS", "n": 30,
            "sla_metrics": {
                "thpt_under_5s_per_node": 16.8,
                "thpt_under_5s_per_cluster": 310,
                "thpt_under_1s_per_cluster": 305,
                "thpt_cluster_node_count": 20,
                "ttfe_p50_ms": 630, "ttfe_p95_ms": 987, "exec_success_rate": 1.0,
            },
        },
    ]
    out = render.render_matrix(
        _matrix_results(scen), kata_results=_kata_results(scenarios=kata_scen)
    )
    assert "DIFFERENT node counts" in out
    assert "gVisor at 40 nodes; Kata + microVM at 20 nodes" in out
    assert "NOT comparable across runtimes here (different X)" in out
    # neither runtime's X is presented as THE table-wide X
    assert "cluster rate at 40 nodes" not in out
    assert "cluster rate at 20 nodes" not in out
    # both cluster halves still render their real figures (above target, no ⚠️)
    assert "350 /cluster" in out and "310 /cluster" in out


def test_matrix_same_x_two_runtimes_single_caption():
    # two runtimes' cluster legs at the SAME X keep the single-figure caption (no mixed-X note).
    scen = _full_gvisor_scenarios()
    scen[0]["sla_metrics"].update(
        {"thpt_under_5s_per_cluster": 350, "thpt_cluster_node_count": 40}
    )
    kata_scen = [
        {
            "name": "warmpool_cold_start", "outcome": "PASS", "n": 30,
            "sla_metrics": {
                "thpt_under_5s_per_node": 16.8,
                "thpt_under_5s_per_cluster": 305,
                "thpt_cluster_node_count": 40,
            },
        },
    ]
    out = render.render_matrix(
        _matrix_results(scen), kata_results=_kata_results(scenarios=kata_scen)
    )
    assert "cluster rate at 40 nodes" in out
    assert "DIFFERENT node counts" not in out


def test_matrix_exec_success_n_emitted_preferred_over_derived():
    # when the harness DOES emit exec_success_n, the fraction uses it verbatim (not rate*N).
    scen = _full_gvisor_scenarios()
    scen[2]["sla_metrics"]["exec_success_n"] = 1300  # deliberately != round(rate*N)=1277
    out = render.render_matrix(_matrix_results(scen))
    assert "92.8% (1300/1376) ⚠️" in out
    assert "(1277/1376)" not in out


def test_matrix_pending_scenario_suppresses_leaked_metrics():
    # A scenario whose OUTCOME is `pending` carries provisional sla_metrics that are NOT a
    # publishable measurement — the upstream-blocked resume probe records its timeout
    # CEILING (the wall-clock waiting out a never-clearing Suspended condition), not a real
    # resume TTFE. Those values must NOT leak onto the public page: the whole row renders
    # `pending (upstream-blocked)` across every metric column (matching the throughput
    # columns, which already go pending when their keys are absent), never a misleading
    # number a reader would rank against a real distribution. Regression guard for the
    # live-page defect where the gVisor Resume-from-suspend row showed `34.8414s †` (the
    # gap-probe ceiling). The `(upstream-blocked)` qualifier is the honesty distinction: this
    # run DID land but is held by an upstream controller gap — see
    # test_matrix_gvisor_resume_pending_carries_upstream_blocked_reason below.
    scen = _full_gvisor_scenarios()
    scen[2] = {
        "name": "suspend_resume", "outcome": "pending",
        "pending_reason": "upstream-blocked", "n": 1,
        "sla_metrics": {
            "thpt_under_5s_per_node": 4, "thpt_under_1s_per_node": 0,
            "ttfe_p50_ms": 34841.4, "ttfe_p95_ms": 34841.4, "exec_success_rate": 1.0,
        },
    }
    out = render.render_matrix(_matrix_results(scen))
    resume_line = [l for l in out.splitlines() if "Resume-from-suspend" in l and "gVisor" in l][0]
    cells = [c.strip() for c in resume_line.strip("|").split("|")]
    # columns 2..6 (thpt5, thpt1, p50, p95, exec) all pending — none of the provisional
    # values survive. Each pending cell carries the upstream-blocked reason.
    pend = "pending (upstream-blocked)"
    assert cells[2] == pend and cells[3] == pend
    assert cells[4] == pend and cells[5] == pend
    assert cells[6] == pend
    # the leaked ceiling never appears anywhere in the row
    assert "34.8414" not in resume_line and "34841" not in resume_line


def test_matrix_pass_scenario_still_shows_metrics_after_pending_guard():
    # The pending-suppression must not touch a PASS scenario: a graduated resume row (the
    # post-#4099 state) still renders its real TTFE + N, so graduation is a clean
    # pending -> real flip with no further render change.
    scen = _full_gvisor_scenarios()  # scen[2] suspend_resume is PASS n=1376 here
    out = render.render_matrix(_matrix_results(scen))
    resume_line = [l for l in out.splitlines() if "Resume-from-suspend" in l and "gVisor" in l][0]
    cells = [c.strip() for c in resume_line.strip("|").split("|")]
    # hb#142: real TTFE p50/p95 survive, now with inline count.
    assert cells[4] == "3.5s (count=1376)" and cells[5] == "5s (count=1376)"
    # N=1376 (>= floor) means no low-N dagger on the TTFE cells
    assert render._LOW_N_MARK not in resume_line
    assert "pending" not in [cells[4], cells[5]]


def test_matrix_density_sourced_from_warmpool_not_stale_burst_create():
    # a4s2 Q3 lock (PR #28): DENSITY_SOURCE_SCENARIOS = (warmpool_cold_start,). A stale
    # burst_create row carrying the OLD cluster-wide-capacity 0.45 must NOT shadow warmpool's
    # corrected per-node-allocatable 1.88. hb#134 relocated Max Density off the headline
    # matrix into render_density_detail (DETAILS.md), so the sourcing guard now asserts
    # against that block: the gVisor row sources 1.88, never 0.45.
    scen = _full_gvisor_scenarios() + [
        {
            "name": "burst_create", "outcome": "PASS", "n": 10,
            "sla_metrics": {"density_per_vcpu": 0.45},
        }
    ]
    detail = render.render_density_detail(_matrix_results(scen))
    assert "| gVisor | 1.88 |" in detail
    assert "0.45" not in detail
    # and the headline matrix no longer carries any density cell at all
    out = render.render_matrix(_matrix_results(scen))
    assert "1.88" not in out and "0.45" not in out and "Max Density" not in out


def test_matrix_kata_warm_cold_rows_pending():
    # on an unmeasured kata runtime, the warm-pool + cold rows render pending (not-yet-measured);
    # the resume row is N/A-by-design and is asserted separately below.
    out = render.render_matrix(_matrix_results(_full_gvisor_scenarios()))
    kata_measurable = [
        l for l in out.splitlines()
        if l.startswith("| Kata + microVM |") and "Resume-from-suspend" not in l
    ]
    assert len(kata_measurable) == 2
    for l in kata_measurable:
        cells = [c.strip() for c in l.strip("|").split("|")]
        assert cells[2] == "pending" and cells[3] == "pending"
        assert cells[4] == "pending" and cells[5] == "pending"
        assert cells[6] == "pending"


def test_matrix_resume_kata_is_na_by_design_not_pending():
    # Resume-from-suspend × Kata+microVM can NEVER be measured (CRIU does not transfer to the
    # Kata VM model), so every cell renders N/A — never `pending` (which would imply a future
    # measurement). Holds even when kata is the MEASURED runtime.
    for prov in (None, {"runtime": "kata-microvm"}):
        out = render.render_matrix(_matrix_results(_full_gvisor_scenarios(), provenance=prov))
        resume_kata = [
            l for l in out.splitlines()
            if l.startswith("| Kata + microVM | Resume-from-suspend")
        ]
        assert len(resume_kata) == 1
        cells = [c.strip() for c in resume_kata[0].strip("|").split("|")]
        # columns 2..6 are the 5 metric cells (thpt5, thpt1, p50, p95, exec)
        assert all(c == "N/A" for c in cells[2:7]), cells
        assert "pending" not in resume_kata[0]
    assert "N/A` by construction" in out


def test_matrix_gvisor_resume_pending_carries_upstream_blocked_reason():
    # The honesty distinction (positive assertion): a gVisor resume cell whose run DID land
    # but is held by an upstream controller gap renders `pending (upstream-blocked)` in EVERY
    # metric column — NOT a bare `pending` (which reads as a not-yet-run cell). This is the
    # gap fix: previously _matrix_scenarios dropped pending_reason, so the upstream-blocked
    # resume row was indistinguishable from an unmeasured one. Density stays N/A-by-design.
    scen = _full_gvisor_scenarios()
    scen[2] = {
        "name": "suspend_resume", "outcome": "pending",
        "pending_reason": "upstream-blocked", "n": 1,
    }
    out = render.render_matrix(_matrix_results(scen))
    resume_line = [l for l in out.splitlines() if "Resume-from-suspend" in l and "gVisor" in l][0]
    cells = [c.strip() for c in resume_line.strip("|").split("|")]
    pend = "pending (upstream-blocked)"
    # thpt5, thpt1, p50, p95, exec all carry the reason.
    assert cells[2] == pend and cells[3] == pend
    assert cells[4] == pend and cells[5] == pend
    assert cells[6] == pend
    # a bare `pending` never appears in this row (every pending cell is qualified)
    for c in (cells[2], cells[3], cells[4], cells[5], cells[6]):
        assert c == pend


def test_matrix_bare_pending_no_reason_stays_bare():
    # A genuinely not-yet-run cell (outcome pending, NO pending_reason) renders bare `pending`
    # — the qualifier only appears when there is a real, enum-valid reason. This is the other
    # half of the distinction: a bare `pending` means "awaits a run", `pending (<reason>)`
    # means "ran, but held".
    scen = _full_gvisor_scenarios()
    scen[2] = {"name": "suspend_resume", "outcome": "pending", "n": 0}
    out = render.render_matrix(_matrix_results(scen))
    resume_line = [l for l in out.splitlines() if "Resume-from-suspend" in l and "gVisor" in l][0]
    cells = [c.strip() for c in resume_line.strip("|").split("|")]
    assert cells[2] == "pending" and cells[4] == "pending" and cells[6] == "pending"
    assert "(" not in resume_line.split("Resume-from-suspend")[1]  # no qualifier anywhere


def test_matrix_free_text_pending_reason_dropped_renders_bare_pending():
    # PII/leak guard on the matrix path (mirrors the non-matrix test_free_text_pending_reason
    # _dropped_row_kept): a pending_reason outside the closed enum is dropped, the cell falls
    # back to bare `pending`, and the free-text NEVER reaches the public page.
    scen = _full_gvisor_scenarios()
    scen[2] = {
        "name": "suspend_resume", "outcome": "pending",
        "pending_reason": "SYNTHETIC-MATRIX-FREETEXT-LEAK", "n": 0,
    }
    out = render.render_matrix(_matrix_results(scen))
    assert "SYNTHETIC-MATRIX-FREETEXT-LEAK" not in out
    resume_line = [l for l in out.splitlines() if "Resume-from-suspend" in l and "gVisor" in l][0]
    cells = [c.strip() for c in resume_line.strip("|").split("|")]
    assert cells[2] == "pending" and cells[4] == "pending"  # bare, no leaked qualifier


def test_matrix_upstream_blocked_footnote_distinguishes_from_bare_pending():
    # The footnote must teach the reader the difference: a bare `pending` awaits a run; a
    # `pending (upstream-blocked)` cell's run landed but is held by an upstream controller gap
    # and graduates on the fix, not on scheduling. Static honesty scaffolding, always rendered.
    out = render.render_matrix(_matrix_results(_full_gvisor_scenarios()))
    assert "pending (upstream-blocked)" in out
    assert "run DID land" in out
    assert "the moment the upstream fix lands" in out


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
    assert (
        "| gVisor | Warm-pool hit (Base image) | 4 /node · pending (cluster-fire) "
        "| 4 /node · pending (cluster-fire) | 0.6s (count=200) | 0.9s (count=200) | 100% |"
    ) in out


def test_matrix_empty_metrics_renders_pending_skeleton():
    scen = [{"name": "warmpool_cold_start", "outcome": "PASS", "n": 5}]
    out = render.render_matrix(_matrix_results(scen))
    warm = [l for l in out.splitlines() if l.startswith("| gVisor | Warm-pool hit")][0]
    cells = [c.strip() for c in warm.strip("|").split("|")]
    # thpt5, p50, exec all pending when the metrics dict is empty
    assert cells[2] == "pending" and cells[4] == "pending" and cells[6] == "pending"


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


# --- #3942: kata_results companion-artifact merge (separate sandbox-kata run) ----------------


def _kata_results(scenarios=None, provenance="default", **top):
    # Shape of the sandbox-kata companion artifact: native cold measured PASS; warm-pool
    # pending with the pool-topology-constrained reason (the 4/5-readyReplicas single-node
    # contention artifact — a topology property, not a kata number).
    if scenarios is None:
        scenarios = [
            {
                "name": "native_digest_cold", "outcome": "PASS", "n": 5,
                "sla_metrics": {
                    "ttfe_p50_ms": 4362, "ttfe_p95_ms": 5000, "exec_success_rate": 1.0,
                },
            },
            {
                "name": "warmpool_cold_start", "outcome": "pending",
                "pending_reason": "pool-topology-constrained", "n": 5,
            },
        ]
    r = {"product": "sandbox-kata", "scenarios": scenarios}
    if provenance == "default":
        r["provenance"] = {
            "runtime": "kata-microvm", "cluster_substrate": "gke-kata",
            "machine_type": "n2-standard-4", "node_count": 1,
        }
    elif provenance is not None:
        r["provenance"] = provenance
    r.update(top)
    return r


def test_matrix_kata_results_fills_kata_rows_gvisor_unchanged():
    # the companion artifact fills the kata native-cold row with real metrics while the
    # primary gVisor rows render exactly as without it.
    out = render.render_matrix(
        _matrix_results(_full_gvisor_scenarios()),
        kata_results=_kata_results(generated_at="2026-07-02T02:53:00Z"),
    )
    assert (
        "| gVisor | Warm-pool hit (Base image) | 4 /node · pending (cluster-fire) "
        "| 4 /node · pending (cluster-fire) | 0.6s (count=200) | 0.9s (count=200) | 100% |"
    ) in out
    kata_cold = [l for l in out.splitlines()
                 if l.startswith("| Kata + microVM | Unique-image cold")][0]
    cells = [c.strip() for c in kata_cold.strip("|").split("|")]
    # n=5 is below the TTFE comparability floor, so the dagger rides along — the low-N
    # honesty marker applies to kata_results rows exactly as it does to primary rows.
    assert cells[4] == f"4.362s (count=5) {render._LOW_N_MARK}"
    assert cells[5] == f"5s (count=5) {render._LOW_N_MARK}"
    assert "100%" in cells[6]


def test_matrix_kata_results_warmpool_renders_pool_topology_constrained():
    # the warm-pool kata cell is a topology artifact, published as a qualified pending —
    # never a bare FAIL headline and never leaked provisional metrics.
    out = render.render_matrix(
        _matrix_results(_full_gvisor_scenarios()), kata_results=_kata_results()
    )
    kata_warm = [l for l in out.splitlines()
                 if l.startswith("| Kata + microVM | Warm-pool hit")][0]
    cells = [c.strip() for c in kata_warm.strip("|").split("|")]
    pend = "pending (pool-topology-constrained)"
    assert cells[2] == pend and cells[3] == pend
    assert cells[4] == pend and cells[5] == pend
    assert cells[6] == pend


def test_matrix_kata_results_separate_run_footnote():
    # the kata rows come from a different substrate + machine shape than the build banner,
    # so their own closed-schema provenance is disclosed; the static not-yet-measured line
    # must be gone.
    out = render.render_matrix(
        _matrix_results(_full_gvisor_scenarios()),
        kata_results=_kata_results(generated_at="2026-07-02T02:53:00Z"),
    )
    assert "not-yet-measured (requires-kata-microvm)" not in out
    foot = [l for l in out.splitlines() if "separate run on the kata node pool" in l][0]
    assert "cluster_substrate=gke-kata" in foot
    assert "machine_type=n2-standard-4" in foot
    assert "node_count=1" in foot
    assert "generated-at=2026-07-02T02:53:00Z" in foot


def test_matrix_kata_results_wrong_product_ignored():
    # only the sandbox-kata product may fill the kata slot — anything else is dropped whole.
    kr = _kata_results()
    kr["product"] = "sandbox"
    out = render.render_matrix(_matrix_results(_full_gvisor_scenarios()), kata_results=kr)
    assert "4.362s" not in out
    assert "not-yet-measured (requires-kata-microvm)" in out


def test_matrix_kata_results_wrong_runtime_ignored():
    # a sandbox-kata artifact whose provenance.runtime is absent or non-kata is dropped whole
    # (the artifact does not prove what runtime it measured).
    for prov in (None, {"runtime": "gvisor"}, {"runtime": "trust-me-vm"}):
        out = render.render_matrix(
            _matrix_results(_full_gvisor_scenarios()),
            kata_results=_kata_results(provenance=prov),
        )
        assert "4.362s" not in out
        assert "not-yet-measured (requires-kata-microvm)" in out


def test_matrix_kata_measured_primary_wins_over_kata_results():
    # a kata-measured PRIMARY run owns the kata rows; kata_results is ignored and no
    # separate-run footnote renders (the main build banner already covers the kata rows).
    scen = [
        {
            "name": "warmpool_cold_start", "outcome": "PASS", "n": 50,
            "sla_metrics": {"ttfe_p50_ms": 700, "ttfe_p95_ms": 950, "exec_success_rate": 1.0},
        },
    ]
    out = render.render_matrix(
        _matrix_results(scen, provenance={"runtime": "kata-microvm"}),
        kata_results=_kata_results(),
    )
    kata_warm = [l for l in out.splitlines()
                 if l.startswith("| Kata + microVM | Warm-pool hit")][0]
    assert "0.7s" in kata_warm  # primary's number, not kata_results' pending
    assert "4.362s" not in out
    assert "separate run on the kata node pool" not in out
    assert "not-yet-measured (requires-kata-microvm)" not in out


def test_matrix_kata_results_resume_row_stays_na():
    # the N/A-by-design resume × kata cell is untouched by the companion artifact.
    out = render.render_matrix(
        _matrix_results(_full_gvisor_scenarios()), kata_results=_kata_results()
    )
    resume_kata = [l for l in out.splitlines()
                   if l.startswith("| Kata + microVM | Resume-from-suspend")][0]
    cells = [c.strip() for c in resume_kata.strip("|").split("|")]
    assert all(c == "N/A" for c in cells[2:9]), cells


def test_matrix_kata_results_invalid_provenance_values_dropped_from_footnote():
    # out-of-schema provenance values are filtered by _clean_provenance before the footnote —
    # a free-text machine_type or unknown substrate never reaches the page.
    kr = _kata_results(provenance={
        "runtime": "kata-microvm",
        "machine_type": "OUR SECRET RIG (ask ops)",
        "cluster_substrate": "internal-fleet-7",
        "node_count": 1,
    })
    out = render.render_matrix(_matrix_results(_full_gvisor_scenarios()), kata_results=kr)
    assert "SECRET RIG" not in out and "internal-fleet-7" not in out
    foot = [l for l in out.splitlines() if "separate run on the kata node pool" in l][0]
    assert "machine_type" not in foot and "cluster_substrate" not in foot
    assert "node_count=1" in foot


def test_matrix_kata_results_bad_generated_at_dropped():
    out = render.render_matrix(
        _matrix_results(_full_gvisor_scenarios()),
        kata_results=_kata_results(generated_at="last tuesday, trust me"),
    )
    assert "trust me" not in out
    foot = [l for l in out.splitlines() if "separate run on the kata node pool" in l][0]
    assert "generated-at" not in foot


def test_matrix_kata_results_none_identical_to_absent():
    a = render.render_matrix(_matrix_results(_full_gvisor_scenarios()))
    b = render.render_matrix(_matrix_results(_full_gvisor_scenarios()), kata_results=None)
    assert a == b


def _burst_scenario(metrics, n=10):
    return {"name": "burst_create", "outcome": "PASS", "n": n, "sla_metrics": metrics}


def test_burst_corroboration_inert_when_exec_absent():
    # #3954: burst_create carries only the pod-Ready count today (no exec field) ⇒ INERT.
    scen = _full_gvisor_scenarios() + [_burst_scenario({"sandboxes_ready_under_1s": 9})]
    out = render.render_burst_corroboration(_matrix_results(scen))
    assert out == ""


def test_burst_corroboration_inert_when_no_burst_scenario():
    out = render.render_burst_corroboration(_matrix_results(_full_gvisor_scenarios()))
    assert out == ""


def test_burst_corroboration_renders_gap_when_both_present():
    scen = _full_gvisor_scenarios() + [
        _burst_scenario({"sandboxes_ready_under_1s": 9, "sandboxes_exec_under_1s": 6})
    ]
    out = render.render_burst_corroboration(_matrix_results(scen))
    assert "## Burst Create — TTFE Corroboration" in out
    assert "| Pod-Ready <1s (weaker claim) | 9 |" in out
    assert "| Executed first-instruction <1s (TTFE, stronger claim) | 6 |" in out
    assert "| Ready-but-not-yet-run (gap) | 3 |" in out


def test_burst_corroboration_exec_success_under_100_flags_fraction():
    scen = _full_gvisor_scenarios() + [
        _burst_scenario(
            {
                "sandboxes_ready_under_1s": 10,
                "sandboxes_exec_under_1s": 8,
                "exec_success_rate": 0.9,
            },
            n=10,
        )
    ]
    out = render.render_burst_corroboration(_matrix_results(scen))
    assert "| Execution success (Honesty Check) | 90% (9/10) ⚠️ |" in out


def test_burst_corroboration_exec_success_100_renders_plain():
    scen = _full_gvisor_scenarios() + [
        _burst_scenario(
            {
                "sandboxes_ready_under_1s": 10,
                "sandboxes_exec_under_1s": 10,
                "exec_success_rate": 1.0,
            },
            n=10,
        )
    ]
    out = render.render_burst_corroboration(_matrix_results(scen))
    assert "| Execution success (Honesty Check) | 100% |" in out
    assert "⚠️" not in out


def test_burst_corroboration_bad_exec_value_dropped_then_inert():
    # a non-numeric exec value fails the predicate ⇒ dropped ⇒ ready-only ⇒ INERT.
    scen = _full_gvisor_scenarios() + [
        _burst_scenario(
            {"sandboxes_ready_under_1s": 9, "sandboxes_exec_under_1s": "soon (ask alex)"}
        )
    ]
    out = render.render_burst_corroboration(_matrix_results(scen))
    assert out == ""


def _turnover_scenario(metrics, n=None, outcome="PASS"):
    s = {"name": "session_turnover", "outcome": outcome, "sla_metrics": metrics}
    if n is not None:
        s["n"] = n
    return s


def test_session_turnover_inert_when_no_scenario():
    # #3868: pre-fire suites have no session_turnover cell at all ⇒ INERT.
    out = render.render_session_turnover(_matrix_results(_full_gvisor_scenarios()))
    assert out == ""


def test_session_turnover_inert_when_refill_absent():
    # a completed cell that emitted only the p90 tail (no median spine) ⇒ INERT (no partial lie).
    scen = _full_gvisor_scenarios() + [_turnover_scenario({"refill_p90_ms": 7800.0}, n=5)]
    out = render.render_session_turnover(_matrix_results(scen))
    assert out == ""


def test_session_turnover_inert_when_pool_never_refilled():
    # the scenario emits {} when the pool never refilled (INERT honesty) ⇒ renders "".
    scen = _full_gvisor_scenarios() + [_turnover_scenario({}, n=0, outcome="FAIL")]
    out = render.render_session_turnover(_matrix_results(scen))
    assert out == ""


def test_session_turnover_renders_median_with_n_footnote():
    scen = _full_gvisor_scenarios() + [
        _turnover_scenario({"refill_latency_ms": 4200.0, "refill_p90_ms": 7800.0}, n=5)
    ]
    out = render.render_session_turnover(_matrix_results(scen))
    assert "## Warm-Pool Turnover — Sustained-Churn Refill Latency" in out
    assert "| Median (p50) (over 5 cycles) | 4.2s |" in out
    assert "| Tail (p90) | 7.8s |" in out


def test_session_turnover_median_only_no_footnote_no_tail():
    # median present, no p90, no top-level n ⇒ renders the median row, omits footnote + tail row.
    scen = _full_gvisor_scenarios() + [_turnover_scenario({"refill_latency_ms": 4200.0})]
    out = render.render_session_turnover(_matrix_results(scen))
    assert "| Median (p50) | 4.2s |" in out
    assert "cycles)" not in out  # the "(over N cycles)" footnote must be absent
    assert "Tail (p90)" not in out


def test_session_turnover_n_from_sla_metrics_ignored():
    # the reserved "n" run.py POPS to a top-level field — a stray sla_metrics "n" must NOT be read
    # as the footnote source (it is dropped by the closed SESSION_TURNOVER_FIELDS allow-list anyway).
    scen = _full_gvisor_scenarios() + [
        _turnover_scenario({"refill_latency_ms": 4200.0, "n": 999})  # no top-level n
    ]
    out = render.render_session_turnover(_matrix_results(scen))
    assert "| Median (p50) | 4.2s |" in out
    assert "999" not in out and "cycles)" not in out


def test_session_turnover_bad_median_dropped_then_inert():
    # a non-numeric / negative median fails the nonneg predicate ⇒ dropped ⇒ INERT (no lie row).
    for bad in ("soon", -1.0):
        scen = _full_gvisor_scenarios() + [_turnover_scenario({"refill_latency_ms": bad}, n=5)]
        out = render.render_session_turnover(_matrix_results(scen))
        assert out == "", f"bad median {bad!r} must render INERT"


# --- #3868 administrative-suspend-latency axis ---------------------------------

def _scen_with_suspend(metrics):
    # Replace the fixture's suspend_resume cell's sla_metrics (it exists with resume-TTFE keys
    # but no suspend_latency_ms) so tests exercise the administrative-suspend pair, not a
    # never-reached second cell.
    scen = [dict(s) for s in _full_gvisor_scenarios()]
    for s in scen:
        if s["name"] == "suspend_resume":
            s["sla_metrics"] = metrics
    return scen


def _scen_without_suspend():
    return [s for s in _full_gvisor_scenarios() if s["name"] != "suspend_resume"]


def test_suspend_latency_inert_when_no_scenario():
    # #3868: a suite with no suspend_resume cell at all ⇒ INERT.
    out = render.render_suspend_latency(_matrix_results(_scen_without_suspend()))
    assert out == ""


def test_suspend_latency_inert_when_only_resume_ttfe():
    # today's real pre-fire shape: the suspend_resume cell exists carrying only resume-TTFE keys
    # (no suspend_latency_ms spine) ⇒ INERT (the axis is dark until a fire emits the median).
    out = render.render_suspend_latency(_matrix_results(_full_gvisor_scenarios()))
    assert out == ""


def test_suspend_latency_inert_when_median_absent():
    # a cell that emitted only the p90 tail (no median spine) ⇒ INERT (no partial lie).
    out = render.render_suspend_latency(_matrix_results(_scen_with_suspend({"suspend_p90_ms": 7800.0})))
    assert out == ""


def test_suspend_latency_inert_when_empty_sla_metrics():
    # the scenario emits {} when no suspend sample was captured (INERT honesty) ⇒ renders "".
    out = render.render_suspend_latency(_matrix_results(_scen_with_suspend({})))
    assert out == ""


def test_suspend_latency_renders_median_only():
    # median present, no p90 (n=1 fire) ⇒ renders the median row, omits the tail row.
    out = render.render_suspend_latency(_matrix_results(_scen_with_suspend({"suspend_latency_ms": 4200.0})))
    assert "## Administrative Suspend Latency" in out
    assert "| Median (p50) | 4.2s |" in out
    assert "Tail (p90)" not in out


def test_suspend_latency_renders_median_and_p90():
    out = render.render_suspend_latency(
        _matrix_results(_scen_with_suspend({"suspend_latency_ms": 4200.0, "suspend_p90_ms": 7800.0}))
    )
    assert "| Median (p50) | 4.2s |" in out
    assert "| Tail (p90) | 7.8s |" in out


def test_suspend_latency_closed_schema_drops_foreign_keys():
    # the suspend_resume cell also carries the resume-TTFE pair + pending_reason + n; the closed
    # SUSPEND_LATENCY_FIELDS allow-list must drop all of them so only the suspend pair renders.
    out = render.render_suspend_latency(
        _matrix_results(
            _scen_with_suspend(
                {
                    "suspend_latency_ms": 4200.0,
                    "ttfe_p50_ms": 1396.0,
                    "ttfe_p90_ms": 1722.0,
                    "pending_reason": "leaked",
                    "n": 999,
                }
            )
        )
    )
    assert "| Median (p50) | 4.2s |" in out
    assert "1.396s" not in out and "1.722s" not in out
    assert "leaked" not in out and "999" not in out


def test_suspend_latency_bad_median_dropped_then_inert():
    # a non-numeric / negative / bool median fails the nonneg predicate ⇒ dropped ⇒ INERT.
    for bad in ("soon", -1.0, True):
        out = render.render_suspend_latency(_matrix_results(_scen_with_suspend({"suspend_latency_ms": bad})))
        assert out == "", f"bad median {bad!r} must render INERT"


def test_suspend_latency_bad_p90_dropped_median_still_renders():
    # a bad p90 is dropped but the median spine survives ⇒ median-only render (tail omitted).
    out = render.render_suspend_latency(
        _matrix_results(_scen_with_suspend({"suspend_latency_ms": 4200.0, "suspend_p90_ms": -5.0}))
    )
    assert "| Median (p50) | 4.2s |" in out
    assert "Tail (p90)" not in out


def test_suspend_latency_never_frames_as_idle_or_auto():
    # HARD naming constraint: the block must not let a reader infer an idle/auto-suspend capability.
    out = render.render_suspend_latency(
        _matrix_results(_scen_with_suspend({"suspend_latency_ms": 4200.0, "suspend_p90_ms": 7800.0}))
    )
    assert "administrative" in out.lower()
    # the capability note must state the absence explicitly, and never brand this as idle/auto.
    assert "no idle-timeout, activity-reclaim, or auto-suspend" in out
    assert "idle suspend" not in out.lower()
    # "auto-suspend"/"auto suspend" may appear ONLY inside the negating capability-note phrase.
    scrubbed = out.lower().replace("no idle-timeout, activity-reclaim, or auto-suspend", "")
    assert "auto-suspend" not in scrubbed
    assert "auto suspend" not in scrubbed


# --- #3868 vCPU-footprint axis -------------------------------------------------

def test_vcpu_footprint_inert_when_no_footprint_fields():
    # pre-fire artifacts carry runtime but no footprint keys ⇒ INERT (byte-unchanged page).
    out = render.render_vcpu_footprint(
        _matrix_results(_full_gvisor_scenarios(), provenance={"runtime": "gvisor"})
    )
    assert out == ""


def test_vcpu_footprint_inert_on_unknown_product():
    out = render.render_vcpu_footprint(
        {"product": "not-a-product", "provenance": {
            "runtime": "gvisor", "sandbox_cpu_request_m": 10, "sandbox_mem_request_mib": 16}}
    )
    assert out == ""


def test_vcpu_footprint_inert_when_only_cpu_present():
    # a half-stated footprint is not a reproducible qualifier ⇒ INERT (all-or-nothing).
    out = render.render_vcpu_footprint(
        _matrix_results(_full_gvisor_scenarios(),
                        provenance={"runtime": "gvisor", "sandbox_cpu_request_m": 10})
    )
    assert out == ""


def test_vcpu_footprint_gvisor_measured_kata_pends():
    prov = {"runtime": "gvisor", "sandbox_cpu_request_m": 10, "sandbox_mem_request_mib": 16}
    out = render.render_vcpu_footprint(_matrix_results(_full_gvisor_scenarios(), provenance=prov))
    assert "## Per-Sandbox Footprint (declared request)" in out
    assert "| gVisor | 10m | 16Mi |" in out
    assert "| Kata + microVM | pending | pending |" in out


def test_vcpu_footprint_kata_fills_from_companion_artifact():
    prov = {"runtime": "gvisor", "sandbox_cpu_request_m": 10, "sandbox_mem_request_mib": 16}
    kr = _kata_results(provenance={
        "runtime": "kata-microvm", "sandbox_cpu_request_m": 500, "sandbox_mem_request_mib": 512})
    out = render.render_vcpu_footprint(
        _matrix_results(_full_gvisor_scenarios(), provenance=prov), kata_results=kr)
    assert "| gVisor | 10m | 16Mi |" in out
    assert "| Kata + microVM | 500m | 512Mi |" in out


def test_vcpu_footprint_negative_value_dropped_by_schema_then_inert():
    # a negative request fails the nonneg-int predicate ⇒ dropped by _clean_provenance ⇒
    # only mem survives ⇒ half-stated ⇒ INERT.
    out = render.render_vcpu_footprint(
        _matrix_results(_full_gvisor_scenarios(), provenance={
            "runtime": "gvisor", "sandbox_cpu_request_m": -10, "sandbox_mem_request_mib": 16})
    )
    assert out == ""


def test_vcpu_footprint_kata_companion_wrong_product_ignored():
    # kata_results only fills the kata slot when its product is sandbox-kata (guard mirrors density).
    prov = {"runtime": "gvisor", "sandbox_cpu_request_m": 10, "sandbox_mem_request_mib": 16}
    kr = {"product": "sandbox", "provenance": {
        "runtime": "kata-microvm", "sandbox_cpu_request_m": 500, "sandbox_mem_request_mib": 512}}
    out = render.render_vcpu_footprint(
        _matrix_results(_full_gvisor_scenarios(), provenance=prov), kata_results=kr)
    assert "| Kata + microVM | pending | pending |" in out


def _warmpool_scenario(metrics, n=30):
    return {"name": "warmpool_cold_start", "outcome": "PASS", "n": n, "sla_metrics": metrics}


def test_warm_bind_decomposition_inert_when_bind_absent():
    # inch #1: today's warm-pool row carries the ttfe pair but not the bind/exec pairs ⇒ INERT.
    out = render.render_warm_bind_decomposition(_matrix_results(_full_gvisor_scenarios()))
    assert out == ""


def test_warm_bind_decomposition_inert_when_no_warmpool_scenario():
    scen = [s for s in _full_gvisor_scenarios() if s["name"] != "warmpool_cold_start"]
    out = render.render_warm_bind_decomposition(_matrix_results(scen))
    assert out == ""


def test_warm_bind_decomposition_inert_when_ttfe_absent():
    # bind + exec pairs present but the matching ttfe pair missing ⇒ INERT (all six required).
    scen = [
        _warmpool_scenario(
            {
                "bind_p50_ms": 400, "bind_p95_ms": 600,
                "exec_p50_ms": 1000, "exec_p95_ms": 1150,
            }
        )
    ]
    out = render.render_warm_bind_decomposition(_matrix_results(scen))
    assert out == ""


def test_warm_bind_decomposition_inert_when_exec_absent():
    # bind + ttfe present but the measured exec pair missing ⇒ INERT (exec is measured, not
    # derived by subtraction, so its absence keeps the block dark).
    scen = [
        _warmpool_scenario(
            {
                "bind_p50_ms": 400, "bind_p95_ms": 600,
                "ttfe_p50_ms": 1396, "ttfe_p95_ms": 1722,
            }
        )
    ]
    out = render.render_warm_bind_decomposition(_matrix_results(scen))
    assert out == ""


def test_warm_bind_decomposition_renders_when_all_present():
    # exec is a MEASURED percentile passed through verbatim — deliberately NOT equal to
    # ttfe - bind (1000 != 1396-400=996, 1150 != 1722-600=1122) so the assertions prove the
    # render displays the measured value, never a p50(ttfe)-p50(bind) subtraction.
    scen = [
        _warmpool_scenario(
            {
                "bind_p50_ms": 400, "bind_p95_ms": 600,
                "exec_p50_ms": 1000, "exec_p95_ms": 1150,
                "ttfe_p50_ms": 1396, "ttfe_p95_ms": 1722,
            }
        )
    ]
    out = render.render_warm_bind_decomposition(_matrix_results(scen))
    assert "## Warm-Hit TTFE — Bind vs Exec Decomposition" in out
    assert "| Bind (create → bound, provisioning) | 0.4s | 0.6s |" in out
    assert "| Exec (websocket + first-instruction) | 1s | 1.15s |" in out
    assert "| **TTFE (total)** | **1.396s** | **1.722s** |" in out


def _decomp_scen():
    return [
        _warmpool_scenario(
            {
                "bind_p50_ms": 400, "bind_p95_ms": 600,
                "exec_p50_ms": 1000, "exec_p95_ms": 1150,
                "ttfe_p50_ms": 1396, "ttfe_p95_ms": 1722,
            }
        )
    ]


def test_warm_bind_decomposition_drained_caveat_renders():
    # #103/#111: provenance.regime == "drained" appends the regime caveat under the block.
    out = render.render_warm_bind_decomposition(
        _matrix_results(_decomp_scen(), provenance={"regime": "drained"})
    )
    assert "## Warm-Hit TTFE — Bind vs Exec Decomposition" in out
    assert "Regime caveat" in out
    assert "drained, low-contention cluster" in out


def test_warm_bind_decomposition_no_caveat_when_under_load():
    # under-load ⇒ the drained caveat MUST NOT render (data-keyed, cannot rot).
    out = render.render_warm_bind_decomposition(
        _matrix_results(_decomp_scen(), provenance={"regime": "under-load"})
    )
    assert "## Warm-Hit TTFE — Bind vs Exec Decomposition" in out
    assert "Regime caveat" not in out


def test_warm_bind_decomposition_no_caveat_when_regime_absent():
    # absent regime ⇒ no caveat (graceful degradation on pre-regime data).
    out = render.render_warm_bind_decomposition(_matrix_results(_decomp_scen()))
    assert "## Warm-Hit TTFE — Bind vs Exec Decomposition" in out
    assert "Regime caveat" not in out


def test_warm_bind_decomposition_scaling_term_renders_on_drained_caveat():
    # #4137: drained + a valid warm_scaling_term ⇒ the caveat NAMES the scaling term.
    out = render.render_warm_bind_decomposition(
        _matrix_results(
            _decomp_scen(),
            provenance={"regime": "drained", "warm_scaling_term": "bind-concurrency"},
        )
    )
    assert "Regime caveat" in out
    assert "bind (provisioning) concurrency" in out


def test_warm_bind_decomposition_no_scaling_clause_when_term_absent():
    # drained but no warm_scaling_term ⇒ caveat renders WITHOUT the scaling clause.
    out = render.render_warm_bind_decomposition(
        _matrix_results(_decomp_scen(), provenance={"regime": "drained"})
    )
    assert "Regime caveat" in out
    assert "bind (provisioning) concurrency" not in out


def test_warm_bind_decomposition_scaling_clause_absent_when_not_drained():
    # a warm_scaling_term with a non-drained regime ⇒ no caveat AND no scaling clause
    # (the clause qualifies the drained caveat; it disappears coherently with it).
    out = render.render_warm_bind_decomposition(
        _matrix_results(
            _decomp_scen(),
            provenance={"regime": "under-load", "warm_scaling_term": "bind-concurrency"},
        )
    )
    assert "Regime caveat" not in out
    assert "bind (provisioning) concurrency" not in out


def test_warm_bind_decomposition_bad_scaling_term_dropped():
    # an out-of-enum warm_scaling_term renders the drained caveat unchanged (no clause).
    out = render.render_warm_bind_decomposition(
        _matrix_results(
            _decomp_scen(),
            provenance={"regime": "drained", "warm_scaling_term": "exec-concurrency"},
        )
    )
    assert "Regime caveat" in out
    assert "bind (provisioning) concurrency" not in out


def test_every_warm_scaling_term_has_a_clause():
    # sync guard: every closed-enum WARM_SCALING_TERMS member must have a render clause,
    # else a valid emitted value would silently render nothing.
    from schema import WARM_SCALING_TERMS

    for term in WARM_SCALING_TERMS:
        assert term in render._WARM_SCALING_TERM_CLAUSE


def test_warm_bind_decomposition_bad_bind_value_dropped_then_inert():
    # a non-numeric bind value fails the predicate ⇒ dropped ⇒ missing bind ⇒ INERT.
    scen = [
        _warmpool_scenario(
            {
                "bind_p50_ms": "soon", "bind_p95_ms": 600,
                "exec_p50_ms": 1000, "exec_p95_ms": 1150,
                "ttfe_p50_ms": 1396, "ttfe_p95_ms": 1722,
            }
        )
    ]
    out = render.render_warm_bind_decomposition(_matrix_results(scen))
    assert out == ""


def _cold_scenario(metrics, n=1):
    return {"name": "native_digest_cold", "outcome": "PASS", "n": n, "sla_metrics": metrics}


def test_cold_bind_decomposition_inert_when_bind_absent():
    # inch #2: today's cold row carries the ttfe pair but not the bind/exec pairs ⇒ INERT.
    out = render.render_cold_bind_decomposition(_matrix_results(_full_gvisor_scenarios()))
    assert out == ""


def test_cold_bind_decomposition_inert_when_no_cold_scenario():
    scen = [s for s in _full_gvisor_scenarios() if s["name"] != "native_digest_cold"]
    out = render.render_cold_bind_decomposition(_matrix_results(scen))
    assert out == ""


def test_cold_bind_decomposition_inert_when_ttfe_absent():
    # bind + exec pairs present but the matching ttfe pair missing ⇒ INERT (all six required).
    scen = [
        _cold_scenario(
            {
                "bind_p50_ms": 2000, "bind_p95_ms": 2000,
                "exec_p50_ms": 200, "exec_p95_ms": 200,
            }
        )
    ]
    out = render.render_cold_bind_decomposition(_matrix_results(scen))
    assert out == ""


def test_cold_bind_decomposition_inert_when_exec_absent():
    # bind + ttfe present but the measured exec pair missing ⇒ INERT (exec is measured, not
    # derived by subtraction, so its absence keeps the block dark).
    scen = [
        _cold_scenario(
            {
                "bind_p50_ms": 2000, "bind_p95_ms": 2000,
                "ttfe_p50_ms": 2130, "ttfe_p95_ms": 2130,
            }
        )
    ]
    out = render.render_cold_bind_decomposition(_matrix_results(scen))
    assert out == ""


def test_cold_bind_decomposition_renders_when_all_present():
    # exec is a MEASURED percentile passed through verbatim — deliberately NOT equal to
    # ttfe - bind (200 != 2130-2000=130) so the assertions prove the render displays the
    # measured value, never a p50(ttfe)-p50(bind) subtraction. For cold, provision dominates.
    scen = [
        _cold_scenario(
            {
                "bind_p50_ms": 2000, "bind_p95_ms": 2000,
                "exec_p50_ms": 200, "exec_p95_ms": 200,
                "ttfe_p50_ms": 2130, "ttfe_p95_ms": 2130,
            }
        )
    ]
    out = render.render_cold_bind_decomposition(_matrix_results(scen))
    assert "## Cold-Start TTFE — Provision vs Exec Decomposition" in out
    assert "| Provision (create → Ready) | 2s | 2s |" in out
    assert "| Exec (websocket + first-instruction) | 0.2s | 0.2s |" in out
    assert "| **TTFE (total)** | **2.13s** | **2.13s** |" in out


def _cold_decomp_scen():
    return [
        _cold_scenario(
            {
                "bind_p50_ms": 2000, "bind_p95_ms": 2000,
                "exec_p50_ms": 200, "exec_p95_ms": 200,
                "ttfe_p50_ms": 2130, "ttfe_p95_ms": 2130,
            }
        )
    ]


def test_cold_bind_decomposition_drained_caveat_renders():
    # #103/#111: provenance.regime == "drained" appends the regime caveat under the block.
    out = render.render_cold_bind_decomposition(
        _matrix_results(_cold_decomp_scen(), provenance={"regime": "drained"})
    )
    assert "## Cold-Start TTFE — Provision vs Exec Decomposition" in out
    assert "Regime caveat" in out
    assert "drained, low-contention cluster" in out


def test_cold_bind_decomposition_no_caveat_when_under_load():
    # under-load ⇒ the drained caveat MUST NOT render (data-keyed, cannot rot).
    out = render.render_cold_bind_decomposition(
        _matrix_results(_cold_decomp_scen(), provenance={"regime": "under-load"})
    )
    assert "## Cold-Start TTFE — Provision vs Exec Decomposition" in out
    assert "Regime caveat" not in out


def test_cold_bind_decomposition_no_caveat_when_regime_absent():
    # absent regime ⇒ no caveat (graceful degradation on pre-regime data).
    out = render.render_cold_bind_decomposition(_matrix_results(_cold_decomp_scen()))
    assert "## Cold-Start TTFE — Provision vs Exec Decomposition" in out
    assert "Regime caveat" not in out


def test_cold_bind_decomposition_bad_exec_value_dropped_then_inert():
    # a non-numeric exec value fails the predicate ⇒ dropped ⇒ missing exec ⇒ INERT.
    scen = [
        _cold_scenario(
            {
                "bind_p50_ms": 2000, "bind_p95_ms": 2000,
                "exec_p50_ms": "soon", "exec_p95_ms": 200,
                "ttfe_p50_ms": 2130, "ttfe_p95_ms": 2130,
            }
        )
    ]
    out = render.render_cold_bind_decomposition(_matrix_results(scen))
    assert out == ""


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


def test_scale_proof_measured_at_renders_dated_subline():
    # #3952: a carried point-in-time block carries its own measured date, rendered as
    # a subline distinct from the page's daily-refreshed top-level timestamp.
    results = _matrix_results(
        _full_gvisor_scenarios(),
        scale_proof={
            "scale_points": [
                {"node_count": 1, "density": 1.88},
                {"node_count": 4, "density": 1.85},
            ],
            "density_retention": 0.984,
            "thpt_retention": 0.99,
            "measured_at": "2026-06-29T03:46:01Z",
        },
    )
    out = render.render_scale_proof(results)
    assert "_Measured 2026-06-29 — node-count linearity sweep" in out
    assert "point-in-time" in out


def test_scale_proof_measured_at_absent_no_subline():
    # No measured_at ⇒ no dated subline (back-compat with pre-#3952 blocks).
    results = _matrix_results(
        _full_gvisor_scenarios(),
        scale_proof={
            "scale_points": [
                {"node_count": 1, "density": 1.88},
                {"node_count": 4, "density": 1.85},
            ],
            "density_retention": 0.984,
            "thpt_retention": 0.99,
        },
    )
    out = render.render_scale_proof(results)
    assert "## Scale Proof (Linearity Check)" in out
    assert "_Measured" not in out


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


def test_scale_proof_superlinear_retention_reads_beat_not_regression():
    # a4s2 v2 lock (PR #28): asymmetric verdict. A superlinear result (retention > 1.1) is a
    # BEAT under the floor-not-ceiling framing, NOT a regression — must read ✅, never ⚠️.
    # (The prior symmetric 0.9–1.1 band wrongly flagged this legit beat as failure.)
    results = _matrix_results(
        _full_gvisor_scenarios(),
        scale_proof={
            "scale_points": [
                {"node_count": 1, "density": 1.88},
                {"node_count": 4, "density": 2.40},  # density GREW with scale ⇒ ratio 1.28
            ],
            "thpt_retention": 1.35,  # superlinear throughput beat
        },
    )
    out = render.render_scale_proof(results)
    assert "⚠️ No" not in out
    # both columns read the flat/beat ✅
    assert out.count("✅ Yes") == 2


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


def test_scale_proof_per_step_density_flat_three_points():
    # ≥3 points (2 steps) ⇒ per-step subline; every step ≥0.9 ⇒ holds-flat read.
    results = _matrix_results(
        _full_gvisor_scenarios(),
        scale_proof={
            "scale_points": [
                {"node_count": 1, "density": 2.0},
                {"node_count": 2, "density": 1.98},  # 0.99
                {"node_count": 4, "density": 1.96},  # 0.99
            ],
        },
    )
    out = render.render_scale_proof(results)
    assert "_Per-step density retention: 1→2 ✅ 0.99 · 2→4 ✅ 0.99 — holds flat step-to-step._" in out


def test_scale_proof_per_step_density_sag_is_visible():
    # The endpoint ratio averages a mid-sweep sag away; per-step exposes WHICH step sagged.
    # 1→2 holds (1.95/2.0=0.975 ✅) but 2→4 collapses (1.0/1.95≈0.51 ⚠️) ⇒ sags-mid-sweep.
    results = _matrix_results(
        _full_gvisor_scenarios(),
        scale_proof={
            "scale_points": [
                {"node_count": 1, "density": 2.0},
                {"node_count": 2, "density": 1.95},
                {"node_count": 4, "density": 1.0},
            ],
        },
    )
    out = render.render_scale_proof(results)
    assert "1→2 ✅" in out
    assert "2→4 ⚠️" in out
    assert "sags mid-sweep" in out


def test_scale_proof_per_step_superlinear_step_reads_beat():
    # A step where density GREW (ratio >1.0) is a beat under floor-not-ceiling ⇒ ✅, never ⚠️.
    results = _matrix_results(
        _full_gvisor_scenarios(),
        scale_proof={
            "scale_points": [
                {"node_count": 1, "density": 1.8},
                {"node_count": 2, "density": 1.9},  # 1.06 beat
                {"node_count": 4, "density": 1.88},  # 0.99
            ],
        },
    )
    out = render.render_scale_proof(results)
    assert "⚠️" not in out
    assert "holds flat step-to-step" in out


def test_scale_proof_per_step_absent_for_two_points():
    # A single step (2 points) IS the endpoint ratio ⇒ no per-step subline (no restating).
    results = _matrix_results(
        _full_gvisor_scenarios(),
        scale_proof={
            "scale_points": [
                {"node_count": 1, "density": 2.0},
                {"node_count": 4, "density": 1.9},
            ],
        },
    )
    out = render.render_scale_proof(results)
    assert "Per-step density retention" not in out


def test_scale_proof_per_step_zero_base_step_pending_not_crash():
    # A zero base density on a step ⇒ that step renders pending (no divide-by-zero),
    # the remaining measurable step still drives the read.
    results = _matrix_results(
        _full_gvisor_scenarios(),
        scale_proof={
            "scale_points": [
                {"node_count": 1, "density": 0.0},
                {"node_count": 2, "density": 1.9},
                {"node_count": 4, "density": 1.88},  # 0.99
            ],
        },
    )
    out = render.render_scale_proof(results)
    assert "1→2 pending" in out
    assert "2→4 ✅ 0.99" in out
    assert "holds flat step-to-step" in out


def test_scale_proof_per_step_throughput_flat_three_points():
    # The producer emits per-point throughput ⇒ a throughput subline renders alongside density.
    results = _matrix_results(
        _full_gvisor_scenarios(),
        scale_proof={
            "scale_points": [
                {"node_count": 1, "density": 2.0, "throughput": 10.0},
                {"node_count": 2, "density": 1.98, "throughput": 9.9},  # 0.99
                {"node_count": 4, "density": 1.96, "throughput": 9.8},  # 0.99
            ],
        },
    )
    out = render.render_scale_proof(results)
    assert "_Per-step throughput retention: 1→2 ✅ 0.99 · 2→4 ✅ 0.99 — holds flat step-to-step._" in out


def test_scale_proof_per_step_throughput_sag_is_visible():
    # A mid-sweep throughput collapse is exposed per-step (the endpoint ratio averages it away).
    results = _matrix_results(
        _full_gvisor_scenarios(),
        scale_proof={
            "scale_points": [
                {"node_count": 1, "density": 2.0, "throughput": 10.0},
                {"node_count": 2, "density": 1.98, "throughput": 9.8},  # 0.98 ✅
                {"node_count": 4, "density": 1.96, "throughput": 5.0},  # ~0.51 ⚠️
            ],
        },
    )
    out = render.render_scale_proof(results)
    assert "_Per-step throughput retention:" in out
    assert "2→4 ⚠️" in out
    assert "sags mid-sweep" in out


def test_scale_proof_per_step_throughput_absent_when_producer_omits():
    # Older blocks (density-only points) ⇒ no throughput subline, density subline still renders.
    results = _matrix_results(
        _full_gvisor_scenarios(),
        scale_proof={
            "scale_points": [
                {"node_count": 1, "density": 2.0},
                {"node_count": 2, "density": 1.98},
                {"node_count": 4, "density": 1.96},
            ],
        },
    )
    out = render.render_scale_proof(results)
    assert "Per-step density retention" in out
    assert "Per-step throughput retention" not in out


def test_scale_proof_per_step_compounding_decay_read():
    # a4s1's PR #54 fast-follow: every step within tolerance (≥0.9) yet the endpoint ratio
    # compounds below 0.9 — the table reads ⚠️ No, so the subline must NOT say "holds flat".
    # 1.0 → 0.93 → 0.8649: steps 0.93/0.93 both ✅, endpoint 0.8649 < 0.9.
    results = _matrix_results(
        _full_gvisor_scenarios(),
        scale_proof={
            "scale_points": [
                {"node_count": 1, "density": 1.0},
                {"node_count": 2, "density": 0.93},
                {"node_count": 4, "density": 0.8649},
            ],
        },
    )
    out = render.render_scale_proof(results)
    assert "1→2 ✅ 0.93" in out
    assert "2→4 ✅ 0.93" in out
    assert "compounds to an endpoint sag" in out
    assert "holds flat step-to-step" not in out


def test_warm_vs_cold_absent_renders_nothing():
    # No warm_vs_cold object ⇒ INERT (the block ships byte-absent until the harness emits it).
    assert render.render_warm_vs_cold(_matrix_results(_full_gvisor_scenarios())) == ""


def _wc(**over):
    base = {
        "warm_p50_ms": 300, "cold_ms": 3000, "speedup": 10.0,
        "semantic": "ttfe", "runtime_class": "gvisor", "n_warm": 200,
    }
    base.update(over)
    return base


def test_warm_vs_cold_complete_block_renders():
    out = render.render_warm_vs_cold(_matrix_results(_full_gvisor_scenarios(), warm_vs_cold=_wc()))
    assert "## Warm-vs-Cold Speedup" in out
    assert "A warm-pool provision is **10× faster** than a true-cold start (gVisor)." in out
    assert "| Leg | TTFE (p50) |" in out


def test_warm_vs_cold_punchline_only_headline_no_table():
    # hb#134 page-split: punchline_only=True renders the one-line headline + a DETAILS pointer,
    # with NO leg table (that moves to the deep-dive appendix). The ratio is recomputed the same
    # way, so the page headline (10×) matches the full-table Speedup row.
    out = render.render_warm_vs_cold(
        _matrix_results(_full_gvisor_scenarios(), warm_vs_cold=_wc()), punchline_only=True)
    assert "## Warm-vs-Cold Speedup" in out
    assert "A warm-pool provision is **10× faster** than a true-cold start (gVisor)" in out
    assert "[DETAILS.md](DETAILS.md)" in out
    assert "| Leg |" not in out
    assert "| Speedup (warm is N× faster) |" not in out


def test_warm_vs_cold_legs_and_speedup_math():
    # the N× headline and both leg cells render from the displayed values (cold ÷ warm = 10×).
    # #103: the warm leg carries its sample size INLINE (n=200 here) so it cannot be conflated
    # with the Core Metrics matrix "Warm-pool hit (Base image)" row (a different scenario/N).
    out = render.render_warm_vs_cold(_matrix_results(_full_gvisor_scenarios(), warm_vs_cold=_wc()))
    assert "| Warm-pool hit (gVisor, n=200) | 0.3s |" in out
    assert "| True-cold (unique-image) | 3s |" in out
    assert "| Speedup (warm is N× faster) | 10× |" in out


def test_warm_vs_cold_n_warm_subline_present():
    out = render.render_warm_vs_cold(_matrix_results(_full_gvisor_scenarios(), warm_vs_cold=_wc()))
    assert "over n=200 warm claims" in out


def test_warm_vs_cold_n_warm_absent_no_subline():
    # n_warm is optional ⇒ block still renders, but the "over n=…" qualifier is omitted AND the
    # #103 inline-n label fragment falls back cleanly to the bare runtime label (no ", n=").
    wc = _wc()
    del wc["n_warm"]
    out = render.render_warm_vs_cold(_matrix_results(_full_gvisor_scenarios(), warm_vs_cold=wc))
    assert "## Warm-vs-Cold Speedup" in out
    assert "over n=" not in out
    assert "| Warm-pool hit (gVisor) | 0.3s |" in out
    assert ", n=" not in out


def test_warm_vs_cold_out_of_enum_runtime_class_inert():
    # a free-text / out-of-enum runtime_class fails the closed-schema predicate ⇒ whole block INERT.
    results = _matrix_results(_full_gvisor_scenarios(), warm_vs_cold=_wc(runtime_class="trust-me-vm"))
    assert render.render_warm_vs_cold(results) == ""


def test_warm_vs_cold_missing_required_field_inert():
    # drop a required field (speedup) ⇒ the whole block is INERT (no partial render).
    wc = _wc()
    del wc["speedup"]
    results = _matrix_results(_full_gvisor_scenarios(), warm_vs_cold=wc)
    assert render.render_warm_vs_cold(results) == ""


def test_warm_vs_cold_zero_leg_inert():
    # a zero warm leg makes the ratio undefined ⇒ INERT, never a divide-by-zero render.
    results = _matrix_results(_full_gvisor_scenarios(), warm_vs_cold=_wc(warm_p50_ms=0))
    assert render.render_warm_vs_cold(results) == ""


def test_warm_vs_cold_malformed_object_inert():
    # a non-dict warm_vs_cold (e.g. a stray string) ⇒ "" (never a leak).
    results = _matrix_results(_full_gvisor_scenarios(), warm_vs_cold="SYNTHETIC-WARMCOLD-LEAK")
    assert render.render_warm_vs_cold(results) == ""


def test_warm_vs_cold_ttfi_semantic_label():
    # the TTFI semantic flips both the table header and the inline measurement-method label.
    out = render.render_warm_vs_cold(_matrix_results(_full_gvisor_scenarios(), warm_vs_cold=_wc(semantic="ttfi")))
    assert "| Leg | TTFI (p50) |" in out
    assert "TTFI (first-instruction accepted)" in out


def test_warm_vs_cold_kata_runtime_label():
    out = render.render_warm_vs_cold(_matrix_results(_full_gvisor_scenarios(), warm_vs_cold=_wc(runtime_class="kata-microvm")))
    assert "Kata + microVM" in out


def test_warm_vs_cold_measured_at_renders_dated_subline():
    # a carried point-in-time block carries its own measured date, rendered as a subline
    # distinct from the page's daily-refreshed top-level timestamp (mirrors scale_proof #3952).
    out = render.render_warm_vs_cold(
        _matrix_results(_full_gvisor_scenarios(), warm_vs_cold=_wc(measured_at="2026-06-29T03:46:01Z")))
    assert "_Measured 2026-06-29 — warm-vs-cold speedup" in out
    assert "point-in-time" in out


def test_warm_vs_cold_measured_at_absent_no_subline():
    # No measured_at ⇒ no dated subline (back-compat with pre-carry blocks).
    out = render.render_warm_vs_cold(_matrix_results(_full_gvisor_scenarios(), warm_vs_cold=_wc()))
    assert "_Measured " not in out


def test_warm_vs_cold_absent_mode_defaults_true_cold():
    # #4024: cold_start_mode is OPTIONAL — an object WITHOUT it (the locked native_digest_cold
    # shape) is still VALID (renders, not INERT) and falls back to the historical true-cold
    # phrasing byte-identical to pre-#4024.
    out = render.render_warm_vs_cold(_matrix_results(_full_gvisor_scenarios(), warm_vs_cold=_wc()))
    assert "| True-cold (unique-image) | 3s |" in out
    assert "than a true-cold start (gVisor)." in out


def test_warm_vs_cold_cold_pull_mode_explicit_matches_default():
    # #4024: an EXPLICIT cold-pull renders identically to the absent-mode default (true-cold).
    out = render.render_warm_vs_cold(
        _matrix_results(_full_gvisor_scenarios(), warm_vs_cold=_wc(cold_start_mode="cold-pull")))
    assert "| True-cold (unique-image) | 3s |" in out
    assert "than a true-cold start (gVisor)." in out


def test_warm_vs_cold_cold_provision_mode_honest_label():
    # #4024: cold-provision overflow must render visibly DISTINCT from unique-image true-cold —
    # the leg, headline descriptor, and mechanism all switch to the honest overflow phrasing.
    out = render.render_warm_vs_cold(
        _matrix_results(_full_gvisor_scenarios(), warm_vs_cold=_wc(cold_start_mode="cold-provision")))
    assert "| Cold-provision (node overflow) | 3s |" in out
    assert "than a cold-provision start (warm-pool overflow) (gVisor)." in out
    assert "SHARED base image" in out
    assert "NOT a unique image per claim" in out


def test_warm_vs_cold_cold_provision_never_claims_unique_image():
    # #4024 DoD: the cold-provision render must NOT anywhere claim unique-image (the over-claim
    # this issue exists to kill).
    out = render.render_warm_vs_cold(
        _matrix_results(_full_gvisor_scenarios(), warm_vs_cold=_wc(cold_start_mode="cold-provision")))
    assert "True-cold (unique-image)" not in out
    assert "than a true-cold start" not in out


def test_warm_vs_cold_unknown_mode_inert():
    # #4024 (a4s1 ask): a present-but-INVALID cold_start_mode (e.g. a typo "cold-provison")
    # must fail the block CLOSED ⇒ INERT, never silently fall through to the true-cold default
    # (which would over-claim unique-image for a mislabeled cold leg).
    results = _matrix_results(_full_gvisor_scenarios(), warm_vs_cold=_wc(cold_start_mode="cold-provison"))
    assert render.render_warm_vs_cold(results) == ""


# --- a#3960 step-up saturation render (#4030 saturation_point framing) ---------------------
def _su(**over):
    base = {
        # #4030 OPERATOR headline: the emitter-computed 2×2 (warm/cold × tight/loose) table.
        "saturation_point": {
            "tight_ms": 1000.0, "loose_ms": 5000.0,
            "basis": "max contiguous-from-step-1 offered rate with TTFE p95 under the bar",
            "warm": {"max_rate_under_tight": 300, "max_rate_under_loose": 500},
            "cold": {"max_rate_under_tight": 100, "max_rate_under_loose": 300},
        },
        "pareto_points": [
            {"offered_rate_per_s": 10, "ttfe_p50_ms": 400, "ttfe_p95_ms": 600, "ready_per_s": 9.8},
            {"offered_rate_per_s": 100, "ttfe_p50_ms": 500, "ttfe_p95_ms": 800, "ready_per_s": 97},
            {"offered_rate_per_s": 300, "ttfe_p50_ms": 650, "ttfe_p95_ms": 950, "ready_per_s": 290},
            {"offered_rate_per_s": 500, "ttfe_p50_ms": 1100, "ttfe_p95_ms": 3200, "ready_per_s": 460},
            {"offered_rate_per_s": 800, "ttfe_p50_ms": 3000, "ttfe_p95_ms": 9000, "ready_per_s": 600},
        ],
        "verdict": "saturated",
        "max_flat_rate": 300, "north_star_breach_rate": 500, "saturation_rate": 800,
        "node_count": 37, "machine_type": "e2-standard-16", "sld_s": 60, "wpr": 0.8,
        "measured_at": "2026-06-30T04:30:00Z",
    }
    base.update(over)
    return base


def test_stepup_absent_renders_nothing():
    # No stepup object ⇒ INERT (byte-absent until a sweep result is emitted).
    assert render.render_stepup(_matrix_results(_full_gvisor_scenarios())) == ""


def test_stepup_empty_tables_inert():
    # A stepup object carrying only sweep params (no measured table) ⇒ INERT (no-all-empty).
    results = _matrix_results(_full_gvisor_scenarios(), stepup={"sld_s": 60, "node_count": 37})
    assert render.render_stepup(results) == ""


def test_stepup_saturation_point_headline_table():
    # #4030 headline: the operator Saturation Point table read straight off the emitter's
    # pre-computed warm/cold × tight(1s)/loose(5s) block — no render-time frontier derivation.
    out = render.render_stepup(_matrix_results(_full_gvisor_scenarios(), stepup=_su()))
    assert "## Saturation Point — max sustained creation rate" in out
    assert "Max rate @ TTFE p95 < 1s" in out
    assert "@ p95 < 5s" in out
    assert "| Warm-pool hit | 300/s | 500/s |" in out
    assert "| Cold-provision (node overflow) | 100/s | 300/s |" in out


def test_stepup_saturation_point_em_dash_on_unmet_bar():
    # An unmet bar (rate None or absent) renders an em-dash, NEVER a fabricated 0.
    su = _su(saturation_point={
        "tight_ms": 1000.0, "loose_ms": 5000.0,
        "warm": {"max_rate_under_tight": 300, "max_rate_under_loose": None},  # loose unmet
        "cold": {"max_rate_under_loose": 100},                               # tight absent
    })
    out = render.render_stepup(_matrix_results(_full_gvisor_scenarios(), stepup=su))
    assert "| Warm-pool hit | 300/s | — |" in out  # loose bar unmet ⇒ em-dash, never 0
    assert "| Cold-provision (node overflow) | — | 100/s |" in out  # tight bar absent ⇒ em-dash


def test_stepup_saturation_point_single_leg():
    # Only a warm leg measured ⇒ only the warm row renders (no fabricated cold row).
    su = _su(saturation_point={
        "tight_ms": 1000.0, "loose_ms": 5000.0,
        "warm": {"max_rate_under_tight": 250, "max_rate_under_loose": 400},
    })
    out = render.render_stepup(_matrix_results(_full_gvisor_scenarios(), stepup=su))
    assert "| Warm-pool hit | 250/s | 400/s |" in out
    assert "Cold-provision" not in out


def test_stepup_saturation_point_invalid_inert_falls_to_study():
    # A present-but-INVALID saturation_point (no positive rate anywhere) fails the predicate ⇒
    # dropped on read; the block still renders off pareto_points under the study heading.
    su = _su(saturation_point={"tight_ms": 1000.0, "loose_ms": 5000.0, "warm": {}})
    out = render.render_stepup(_matrix_results(_full_gvisor_scenarios(), stepup=su))
    assert "## Saturation Point — max sustained creation rate" not in out
    assert "## Saturation — step-up throughput study" in out
    assert "Warm-pool hit" not in out


def test_stepup_band_rates_and_verdict_additive():
    # The 500ms/2000ms methodology study renders additively BELOW the operator headline.
    out = render.render_stepup(_matrix_results(_full_gvisor_scenarios(), stepup=_su()))
    assert "🛑 saturated" in out
    assert "highest rate under the 500ms North Star: **300/s**" in out
    assert "first rate to breach 500ms: 500/s" in out
    assert "first rate to cross 2000ms: 800/s" in out


def test_stepup_unknown_verdict_inert():
    # A verdict outside the closed set fails the predicate ⇒ the field is dropped; the block
    # still renders off the (valid) tables, but the bad verdict line never appears.
    out = render.render_stepup(_matrix_results(_full_gvisor_scenarios(), stepup=_su(verdict="totally-fine")))
    assert "## Saturation" in out
    assert "Curve verdict" not in out


def test_stepup_unknown_field_dropped():
    # Closed-schema: an undeclared key is dropped on read and never reaches the page.
    su = _su()
    su["operator_note"] = "internal-cluster-name-leak"
    out = render.render_stepup(_matrix_results(_full_gvisor_scenarios(), stepup=su))
    assert "internal-cluster-name-leak" not in out


def test_stepup_controller_proxy_caveat_renders():
    su = _su(controller_startup={
        "lower_bound": True, "verdict": "degrading",
        "pareto_points": [
            {"offered_rate_per_s": 300, "controller_startup_p95_ms": 700, "controller_ready_per_s": 295},
        ],
    })
    out = render.render_stepup(_matrix_results(_full_gvisor_scenarios(), stepup=su))
    assert "Controller-startup lower bound" in out
    assert "UNDER-reports true" in out


def test_stepup_proxy_only_renders_study_heading_not_saturation_point():
    # #3975 proxy-only sweep: NO saturation_point + NO true-TTFE pareto_points, only the
    # controller-startup proxy. The block renders under the study heading (never the operator
    # Saturation Point table) and never fabricates a headline rate from the optimistic proxy.
    su = {"controller_startup": {
        "lower_bound": True,
        "pareto_points": [
            {"offered_rate_per_s": 500, "controller_startup_p95_ms": 600},  # optimistic proxy
        ],
    }}
    out = render.render_stepup(_matrix_results(_full_gvisor_scenarios(), stepup=su))
    assert "## Saturation — step-up throughput study" in out
    assert "## Saturation Point — max sustained creation rate" not in out
    assert "500/s" not in out  # never derive an operator headline rate from the proxy
    assert "Controller-startup lower bound" in out


def test_stepup_sweep_params_subline():
    out = render.render_stepup(_matrix_results(_full_gvisor_scenarios(), stepup=_su()))
    assert "37 nodes, e2-standard-16, SLD 60s, WPR 0.8" in out
    assert "measured 2026-06-30" in out


def _cb(**over):
    base = {
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
    base.update(over)
    return base


def test_concurrent_burst_absent_renders_nothing():
    # No concurrent_burst object ⇒ INERT (byte-absent until a burst result is emitted).
    assert render.render_concurrent_burst(_matrix_results(_full_gvisor_scenarios())) == ""


def test_concurrent_burst_empty_legs_inert():
    # An object with an empty/missing legs list ⇒ INERT (no partial-lie table).
    assert render.render_concurrent_burst(
        _matrix_results(_full_gvisor_scenarios(), concurrent_burst={"legs": []})) == ""
    assert render.render_concurrent_burst(
        _matrix_results(_full_gvisor_scenarios(), concurrent_burst={"node_count": 20})) == ""


def test_concurrent_burst_renders_table():
    out = render.render_concurrent_burst(_matrix_results(_full_gvisor_scenarios(), concurrent_burst=_cb()))
    assert "## Concurrent Burst — TTFE at N simultaneous claims" in out
    # TTFE renders on the same seconds spine as the matrix; warm + cold rows present.
    assert "| 300 | Warm pool | 6.8743s | 9.393s | 0.392 | 0 | 100% |" in out
    assert "| 300 | Cold provision | 56.0294s | 58.4124s | 0 | 0 | 100% |" in out
    # provenance caption + measured_at subline.
    assert "node_count=20" in out and "`e2-standard-16`" in out
    assert "_Measured 2026-06-30 — concurrent-burst TTFE (point-in-time)._" in out


def test_concurrent_burst_em_dash_on_missing_throughput():
    # A leg that omits a throughput field renders an em-dash, NEVER a fabricated 0.
    cb = _cb(legs=[{"n": 500, "mode": "warm", "ttfe_p50_ms": 11188.0, "ttfe_p95_ms": 15374.0}])
    out = render.render_concurrent_burst(_matrix_results(_full_gvisor_scenarios(), concurrent_burst=cb))
    assert "| 500 | Warm pool | 11.188s | 15.374s | — | — | — |" in out


def test_concurrent_burst_sub_100_exec_flags():
    # An exec-success rate < 100% renders the fraction + ⚠️ (honest, never rounded to 100%).
    cb = _cb(legs=[{"n": 300, "mode": "cold", "ttfe_p50_ms": 5000.0, "ttfe_p95_ms": 6000.0,
                    "exec_success_rate": 0.99}])
    out = render.render_concurrent_burst(_matrix_results(_full_gvisor_scenarios(), concurrent_burst=cb))
    assert "99% (297/300) ⚠️" in out


def test_concurrent_burst_bad_leg_inert():
    # A single malformed leg (out-of-enum mode) fails the legs predicate ⇒ whole block INERT.
    cb = _cb(legs=[
        {"n": 300, "mode": "warm", "ttfe_p50_ms": 6874.3, "ttfe_p95_ms": 9393.0},
        {"n": 300, "mode": "lukewarm", "ttfe_p50_ms": 100.0, "ttfe_p95_ms": 200.0},
    ])
    assert render.render_concurrent_burst(_matrix_results(_full_gvisor_scenarios(), concurrent_burst=cb)) == ""


def test_concurrent_burst_invalid_provenance_dropped_spine_renders():
    # A registry-path-shaped machine_type fails its predicate ⇒ dropped on read; the valid spine
    # still renders (provenance is best-effort, never fabricated, never blocks the table).
    cb = _cb(machine_type="us-central1-docker.pkg.dev/proj/img:1")
    out = render.render_concurrent_burst(_matrix_results(_full_gvisor_scenarios(), concurrent_burst=cb))
    assert "## Concurrent Burst — TTFE at N simultaneous claims" in out
    assert "docker.pkg.dev" not in out  # internal registry path never reaches the page


def _scale_proof_obj():
    return {
        "scale_points": [
            {"node_count": 1, "density": 1.88},
            {"node_count": 2, "density": 1.86},
            {"node_count": 4, "density": 1.85},
        ],
        "density_retention": 0.984,
        "thpt_retention": 0.99,
    }


def test_cluster_scale_both_present_combined_section_demotes_subheadings():
    # hb#134: with BOTH sub-blocks present the combined section renders one ## question, the
    # intro, and the two tables DEMOTED to ### — the standalone ## headings must NOT appear.
    results = _matrix_results(
        _full_gvisor_scenarios(), scale_proof=_scale_proof_obj(), concurrent_burst=_cb())
    out = render.render_cluster_scale(results)
    assert "## Does it hold at cluster scale?" in out
    assert "### Linearity — throughput and density hold flat as nodes grow" in out
    assert "### Concurrent burst — TTFE at N simultaneous claims" in out
    # the two blocks' standalone ## headings are gone (demoted, not duplicated).
    assert "## Scale Proof (Linearity Check)" not in out
    assert "## Concurrent Burst — TTFE at N simultaneous claims" not in out
    # both tables' data still lands under the merged section.
    assert "| 1 → 2 → 4 | ✅ Yes (1.88 → 1.86 → 1.85) | ✅ Yes |" in out
    assert "| 300 | Warm pool | 6.8743s | 9.393s | 0.392 | 0 | 100% |" in out


def test_cluster_scale_both_absent_inert():
    # Neither sub-block present ⇒ the whole section is INERT (no empty header).
    assert render.render_cluster_scale(_matrix_results(_full_gvisor_scenarios())) == ""


def test_cluster_scale_only_burst_renders_wrapper_without_linearity():
    # Only concurrent_burst present ⇒ wrapper + burst ###; NO linearity sub-block.
    out = render.render_cluster_scale(
        _matrix_results(_full_gvisor_scenarios(), concurrent_burst=_cb()))
    assert "## Does it hold at cluster scale?" in out
    assert "### Concurrent burst — TTFE at N simultaneous claims" in out
    assert "### Linearity" not in out


def test_cluster_scale_only_linearity_renders_wrapper_without_burst():
    # Only scale_proof present ⇒ wrapper + linearity ###; NO concurrent-burst sub-block.
    out = render.render_cluster_scale(
        _matrix_results(_full_gvisor_scenarios(), scale_proof=_scale_proof_obj()))
    assert "## Does it hold at cluster scale?" in out
    assert "### Linearity — throughput and density hold flat as nodes grow" in out
    assert "### Concurrent burst" not in out


def test_cluster_scale_saturation_present_renders_third_subblock():
    # hb#132: cluster_saturation present ⇒ the "Does it hold at cluster scale?" wrapper renders a
    # demoted ### sub-block (Saturation), alongside the intro's "Four questions" framing (#134 fold
    # added the contention sub-block as the 4th cluster-scale question).
    out = render.render_cluster_scale(
        _matrix_results(_full_gvisor_scenarios(), cluster_saturation=_cs()))
    assert "## Does it hold at cluster scale?" in out
    assert "Four questions a bigger cluster raises" in out
    assert "### Saturation — the whole-cluster warm-hand-out ceiling" in out
    # its standalone ## heading is DEMOTED, not duplicated, inside the combined section.
    assert "## Cluster Saturation" not in out


def test_cluster_scale_all_three_subblocks_present():
    # All three cluster-scale questions present ⇒ one ## wrapper, all three ### sub-blocks, no
    # standalone ## sub-headings leak through (each demoted).
    results = _matrix_results(
        _full_gvisor_scenarios(), scale_proof=_scale_proof_obj(),
        concurrent_burst=_cb(), cluster_saturation=_cs())
    out = render.render_cluster_scale(results)
    assert "## Does it hold at cluster scale?" in out
    assert "### Linearity — throughput and density hold flat as nodes grow" in out
    assert "### Concurrent burst — TTFE at N simultaneous claims" in out
    assert "### Saturation — the whole-cluster warm-hand-out ceiling" in out
    assert "## Cluster Saturation" not in out


def _cs(**over):
    # A full cluster_saturation object — 1:1 all-warm N=600 saturation ceiling (pool==claim), the
    # measured per-cluster throughput triple, bind/exec decomposition, FAIL outcome + provenance.
    base = {
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
    base.update(over)
    return base


def test_cluster_saturation_absent_renders_nothing():
    # No cluster_saturation object ⇒ INERT (byte-absent until the harness emits it).
    assert render.render_cluster_saturation(_matrix_results(_full_gvisor_scenarios())) == ""


def test_cluster_saturation_missing_spine_inert():
    # Missing any REQUIRED spine field ⇒ INERT (no partial-lie block). The coupled per-cluster
    # triple is part of the spine, so dropping the cluster node-count kills the whole block.
    cs = _cs()
    del cs["thpt_cluster_node_count"]
    assert render.render_cluster_saturation(
        _matrix_results(_full_gvisor_scenarios(), cluster_saturation=cs)) == ""


def test_cluster_saturation_page_ceiling_posture_no_table():
    # Page (default) path: the collapsed per-cluster throughput + worst-case TTFE inline + FAIL
    # ceiling note + DETAILS pointer — with NO decomposition table (that moves to the appendix).
    out = render.render_cluster_saturation(
        _matrix_results(_full_gvisor_scenarios(), cluster_saturation=_cs()))
    assert "### Saturation — the whole-cluster warm-hand-out ceiling" in out
    # 1:1 all-warm framing (pool==claim, NOT over-subscribed) distinguishes it from contention.
    assert "**1:1 all-warm**" in out
    assert "**not** over-subscribed" in out
    # collapsed per-cluster throughput + worst-case TTFE surfaced inline (self-contained).
    assert "At **40 nodes** the cluster sustains only **2.558 claims/sec under 5s**" in out
    assert "(**0/sec under 1s**)" in out
    assert "TTFE degrades to **8.6308s p50** / **12.6103s p95**" in out
    # FAIL headlined as an honest SLA ceiling, not softened.
    assert "SLA ceiling: **not met**" in out
    assert "not a correctness failure" in out
    assert "[DETAILS.md](DETAILS.md)" in out
    # the decomposition table itself does NOT render on the page path.
    assert "| Pool | Claims | Nodes |" not in out
    assert "Bind p50" not in out


def test_cluster_saturation_renders_table():
    out = render.render_cluster_saturation(
        _matrix_results(_full_gvisor_scenarios(), cluster_saturation=_cs()), detail=True)
    assert "## Cluster Saturation — the whole-cluster warm-hand-out ceiling" in out
    assert "**1:1 all-warm**" in out
    # full per-node · per-cluster throughput triple + bind/exec decomposition in one row.
    assert ("| 600 | 600 | 40 | 8.6308s | 12.6103s | 0.064 /node · 2.558 /cluster | "
            "0 /node · 0 /cluster | 8.1916s | 12.1372s | 100% |") in out
    # per-cluster figure MEASURED at node_count, never a per-node × N extrapolation.
    assert "MEASURED at **40 nodes**" in out
    assert "never a per-node × N" in out
    assert "_Measured 2026-07-02 — whole-cluster saturation ceiling (point-in-time)._" in out
    assert "SLA ceiling: **not met**" in out


def test_cluster_saturation_bind_columns_dropped_when_absent():
    # bind_p50/p95 are optional — omitting them drops the Bind columns entirely (never fabricated).
    cs = _cs()
    del cs["bind_p50_ms"]
    del cs["bind_p95_ms"]
    out = render.render_cluster_saturation(
        _matrix_results(_full_gvisor_scenarios(), cluster_saturation=cs), detail=True)
    assert "Bind p50" not in out
    # the spine still renders — bind is optional decomposition, not load-bearing.
    assert "| 600 | 600 | 40 | 8.6308s | 12.6103s |" in out


def test_cluster_saturation_cluster_only_when_per_node_absent():
    # Per-node throughput halves are optional — when absent the cell renders cluster-only (never a
    # fabricated per-node figure), so the coupled-triple honesty holds even on a partial object.
    cs = _cs()
    del cs["thpt_under_5s_per_node"]
    del cs["thpt_under_1s_per_node"]
    out = render.render_cluster_saturation(
        _matrix_results(_full_gvisor_scenarios(), cluster_saturation=cs), detail=True)
    assert "2.558 /cluster" in out
    assert "/node ·" not in out


def test_cluster_saturation_out_of_enum_runtime_inert():
    # An out-of-enum runtime_class fails closed and drops the whole block (public RUNTIME_LABELS).
    assert render.render_cluster_saturation(
        _matrix_results(_full_gvisor_scenarios(), cluster_saturation=_cs(runtime_class="firecracker"))) == ""


def test_cluster_saturation_invalid_machine_type_dropped_spine_renders():
    # A registry-path-shaped machine_type is dropped (bounded GCP-shape), but a valid spine renders.
    out = render.render_cluster_saturation(
        _matrix_results(_full_gvisor_scenarios(),
                        cluster_saturation=_cs(machine_type="us-central1-docker.pkg.dev/proj/img:1")),
        detail=True)
    assert "## Cluster Saturation" in out
    assert "us-central1-docker.pkg.dev" not in out


def test_cluster_saturation_non_fail_outcome_no_sla_note():
    # outcome is optional — a non-FAIL (or absent) outcome renders NO SLA-ceiling note (the FAIL
    # framing is data-gated, never free-text).
    out = render.render_cluster_saturation(
        _matrix_results(_full_gvisor_scenarios(), cluster_saturation=_cs(outcome="PASS")))
    assert "SLA ceiling: **not met**" not in out


def _wpa(**over):
    base = {
        "runtime_class": "gvisor",
        "acq_p50_ms": 2939.65,
        "acq_p95_ms": 3878.44,
        "acq_p99_ms": 4009.62,
        "controller_startup_p95_ms": 1338.12,
        "machine_type": "n2-standard-16",
        "measured_at": "2026-07-01T01:03:12Z",
        "n": 600,
        "offered_rate_per_s": 300,
        "warmpool_size": 600,
    }
    base.update(over)
    return base


def test_warm_pool_acquisition_absent_renders_nothing():
    # No warm_pool_acquisition object ⇒ INERT (byte-absent until the harness emits it).
    assert render.render_warm_pool_acquisition(_matrix_results(_full_gvisor_scenarios())) == ""


def test_warm_pool_acquisition_missing_spine_inert():
    # Missing any REQUIRED spine field (here acq_p95_ms) ⇒ INERT (no partial-lie table).
    wpa = _wpa()
    del wpa["acq_p95_ms"]
    assert render.render_warm_pool_acquisition(
        _matrix_results(_full_gvisor_scenarios(), warm_pool_acquisition=wpa)) == ""


def test_warm_pool_acquisition_renders_table():
    out = render.render_warm_pool_acquisition(
        _matrix_results(_full_gvisor_scenarios(), warm_pool_acquisition=_wpa()))
    assert "## Warm-Pool Acquisition — how fast the pool hands you a sandbox" in out
    # The claim→bound sub-phase is explicitly NOT comparable to the TTFE columns.
    assert "**not comparable**" in out
    assert "| 600 | 2.93965s | 3.87844s | 4.00962s |" in out
    # offered-load + warm-pool context + cluster shape ride the caption.
    assert "300 claims/sec" in out and "warm pool of **600**" in out
    assert "`n2-standard-16`" in out
    # controller-startup lower-bound caveat + measured_at subline.
    assert "Controller-startup lower bound (p95 **1.33812s**)" in out
    assert "_Measured 2026-07-01 — warm-pool acquisition latency (point-in-time)._" in out


def test_warm_pool_acquisition_p99_em_dash_when_absent():
    # p99 is optional — omitting it renders an em-dash, NEVER a fabricated value.
    wpa = _wpa()
    del wpa["acq_p99_ms"]
    out = render.render_warm_pool_acquisition(
        _matrix_results(_full_gvisor_scenarios(), warm_pool_acquisition=wpa))
    assert "| 600 | 2.93965s | 3.87844s | — |" in out


def test_warm_pool_acquisition_no_controller_caveat_when_absent():
    # Omitting controller_startup_p95_ms drops the lower-bound caveat (never fabricated).
    wpa = _wpa()
    del wpa["controller_startup_p95_ms"]
    out = render.render_warm_pool_acquisition(
        _matrix_results(_full_gvisor_scenarios(), warm_pool_acquisition=wpa))
    assert "## Warm-Pool Acquisition — how fast the pool hands you a sandbox" in out
    assert "Controller-startup lower bound" not in out


def test_warm_pool_acquisition_out_of_enum_runtime_inert():
    # An out-of-enum runtime_class fails the RUNTIME_LABELS predicate ⇒ whole block INERT.
    assert render.render_warm_pool_acquisition(
        _matrix_results(_full_gvisor_scenarios(), warm_pool_acquisition=_wpa(runtime_class="lukewarm"))) == ""


def test_warm_pool_acquisition_invalid_machine_type_dropped_spine_renders():
    # A registry-path-shaped machine_type fails its predicate ⇒ dropped on read; the valid spine
    # still renders (provenance is best-effort, never fabricated, never blocks the table).
    out = render.render_warm_pool_acquisition(
        _matrix_results(_full_gvisor_scenarios(),
                        warm_pool_acquisition=_wpa(machine_type="us-central1-docker.pkg.dev/proj/img:1")))
    assert "## Warm-Pool Acquisition — how fast the pool hands you a sandbox" in out
    assert "| 600 | 2.93965s | 3.87844s | 4.00962s |" in out
    assert "docker.pkg.dev" not in out  # internal registry path never reaches the page


def _asc(**over):
    base = {
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
    base.update(over)
    return base


def test_at_scale_contention_absent_renders_nothing():
    # No at_scale_contention object ⇒ INERT (byte-absent until the harness emits it).
    assert render.render_at_scale_contention(_matrix_results(_full_gvisor_scenarios())) == ""


def test_at_scale_contention_missing_spine_inert():
    # Missing any REQUIRED spine field (here ttfe_p95_ms) ⇒ INERT (no partial-lie table).
    asc = _asc()
    del asc["ttfe_p95_ms"]
    assert render.render_at_scale_contention(
        _matrix_results(_full_gvisor_scenarios(), at_scale_contention=asc)) == ""


def test_at_scale_contention_page_retraction_posture_no_table():
    # hb#134 page-split: the DEFAULT (page) path renders the honest-limits retraction posture —
    # the friendlier heading, the retraction prose, the worst-case TTFE inline, and a DETAILS
    # pointer — with NO decomposition table (that moves to the appendix).
    out = render.render_at_scale_contention(
        _matrix_results(_full_gvisor_scenarios(), at_scale_contention=_asc()))
    assert "## Where it breaks today (honest limits)" in out
    assert "retraction" in out
    assert "does **not** hold here" in out
    assert "**2:1 contention**" in out
    # worst-case TTFE surfaced inline so the retraction is self-contained without the table.
    assert "**1.6589s p50** / **2.0169s p95**" in out
    assert "[DETAILS.md](DETAILS.md)" in out
    # the decomposition table itself does NOT render on the page path.
    assert "| Pool | Claims | Contention |" not in out
    assert "Bind p50" not in out


def test_at_scale_contention_renders_table():
    out = render.render_at_scale_contention(
        _matrix_results(_full_gvisor_scenarios(), at_scale_contention=_asc()), detail=True)
    assert "## At Scale Under Contention — where sub-second warm activation breaks" in out
    # It is framed explicitly as a retraction of the sub-second-at-scale claim.
    assert "retraction" in out
    assert "does **not** hold here" in out
    # 2:1 contention derived from data (claim_count / pool_size), never free-text.
    assert "**2:1 contention**" in out
    assert "| 30 | 60 | 2:1 | 1.6589s | 2.0169s | 1.384s | 1.7001s | 100% |" in out
    # node_count=1 provenance rides the caption + cluster shape.
    assert "node_count=1" in out and "`e2-standard-16`" in out
    # Per-node throughput is deliberately omitted as non-comparable to node_count=20 bursts.
    assert "per-node throughput axis is omitted" in out
    assert "_Measured 2026-07-01 — warm-pool at-scale contention ceiling (point-in-time)._" in out


def test_at_scale_contention_bind_columns_em_dash_when_absent():
    # bind_p50/p95 are optional — omitting them drops the Bind columns entirely (never fabricated).
    asc = _asc()
    del asc["bind_p50_ms"]
    del asc["bind_p95_ms"]
    out = render.render_at_scale_contention(
        _matrix_results(_full_gvisor_scenarios(), at_scale_contention=asc), detail=True)
    assert "Bind p50" not in out
    assert "| 30 | 60 | 2:1 | 1.6589s | 2.0169s | 100% |" in out


def test_at_scale_contention_no_exec_column_when_absent():
    # exec_success_rate is optional — omitting it drops the Execution Success column.
    asc = _asc()
    del asc["exec_success_rate"]
    out = render.render_at_scale_contention(
        _matrix_results(_full_gvisor_scenarios(), at_scale_contention=asc), detail=True)
    assert "Execution Success" not in out
    assert "| 30 | 60 | 2:1 | 1.6589s | 2.0169s | 1.384s | 1.7001s |" in out


def test_at_scale_contention_out_of_enum_runtime_inert():
    # An out-of-enum runtime_class fails the RUNTIME_LABELS predicate ⇒ whole block INERT.
    assert render.render_at_scale_contention(
        _matrix_results(_full_gvisor_scenarios(), at_scale_contention=_asc(runtime_class="lukewarm"))) == ""


def test_at_scale_contention_invalid_machine_type_dropped_spine_renders():
    # A registry-path-shaped machine_type fails its predicate ⇒ dropped on read; the valid spine
    # still renders (provenance is best-effort, never fabricated, never blocks the table).
    out = render.render_at_scale_contention(
        _matrix_results(_full_gvisor_scenarios(),
                        at_scale_contention=_asc(machine_type="us-central1-docker.pkg.dev/proj/img:1")),
        detail=True)
    assert "## At Scale Under Contention — where sub-second warm activation breaks" in out
    assert "| 30 | 60 | 2:1 | 1.6589s | 2.0169s | 1.384s | 1.7001s | 100% |" in out
    assert "docker.pkg.dev" not in out  # internal registry path never reaches the page


def test_at_scale_contention_sub_100_exec_flags_warn():
    # A <100% exec rate prints the succeeded/total fraction + ⚠️, never quietly dropped.
    out = render.render_at_scale_contention(
        _matrix_results(_full_gvisor_scenarios(), at_scale_contention=_asc(exec_success_rate=0.95)),
        detail=True)
    assert "⚠️" in out
    assert "95%" in out


# --- Operating Envelope (hb#134) ------------------------------------------------------------
# The headline "given MY load, what wait do I budget?" table. It is render-DERIVED (reads the
# live schema through the same cleaners the detail blocks use — never hardcoded illustrative
# numbers), ALWAYS renders as an honest skeleton (like the matrix), and each row inherits its
# source-block's pending semantics (INERT / absent source ⇒ that row reads `pending`).

def _envelope_results(**over):
    # A fully-populated results object exercising all four envelope rows, built from the same
    # fixtures the per-source detail-block tests use so the envelope stays in lock-step with them.
    r = _matrix_results(
        _full_gvisor_scenarios(),
        at_scale_contention=_asc(),
        concurrent_burst=_cb(),
        warm_pool_acquisition=_wpa(),
    )
    r.update(over)
    return r


def test_operating_envelope_always_renders_skeleton_all_pending():
    # No source blocks present ⇒ the table STILL renders (honest skeleton), every row `pending`.
    # Absence of a measurement is `pending`, never a dropped row and never a guessed number.
    out = render.render_operating_envelope(_matrix_results([]))
    assert "## Operating Envelope — what wait should I budget?" in out
    # All four rows render, all four waits pend.
    assert "| Steady trickle — warm pool keeps up with demand | pending | full start → first result |" in out
    assert "| Bursty — pool oversubscribed (more claims than ready pool) | pending | full start → first result |" in out
    assert "| Hundreds of sandboxes requested at once (1:1 pool) | pending | full start → first result |" in out
    assert "| Sustained high-rate churn | pending | pool hand-off only (before exec) |" in out


def test_operating_envelope_renders_all_four_rows():
    out = render.render_operating_envelope(_envelope_results())
    # Row 1 — matrix warm scenario (warmpool_cold_start ttfe_p50_ms=600 ⇒ ~0.6s).
    assert "| Steady trickle — warm pool keeps up with demand | ~0.6s | full start → first result |" in out
    # Row 2 — at-scale contention (claim_count=60 / pool_size=30 ⇒ 2:1, ttfe_p50_ms=1658.9 ⇒ ~1.7s).
    assert "| Bursty — pool oversubscribed 2:1 (60 claims / 30 ready) | ~1.7s | full start → first result |" in out
    # Row 3 — warm concurrent-burst leg (n=300, ttfe_p50_ms=6874.3 ⇒ ~6.9s).
    assert "| 300 sandboxes requested at once (1:1 pool) | ~6.9s | full start → first result |" in out
    # Row 4 — warm-pool acquisition (offered_rate_per_s=300, acq_p50_ms=2939.65 ⇒ ~2.9s).
    assert "| Sustained 300/sec churn | ~2.9s | pool hand-off only (before exec) |" in out


def test_operating_envelope_scope_column_is_load_bearing():
    # The Scope column must WARN that the acquisition-only row is not rankable against the
    # full-TTFE rows — this is the whole reason the 5 warm/wait numbers can share one table.
    out = render.render_operating_envelope(_envelope_results())
    assert "The **Scope** column is load-bearing" in out
    assert "do **not** rank its number against the full-TTFE rows" in out
    assert "before your code runs" in out
    # The first three rows share the full-TTFE scope; the fourth is the pool-handoff sub-phase.
    assert out.count("| full start → first result |") == 3
    assert out.count("| pool hand-off only (before exec) |") == 1


def test_operating_envelope_wait_format_is_coarse_not_exact():
    # The budgeting figure is a friendly 1-dp ~approximation, NOT the exact _fmt_secs precision
    # (the exact value lives in the detail block each row is sourced from). hb#134 "remove
    # unnecessary detail" mandate — the envelope is a plan-around-this summary for non-experts.
    out = render.render_operating_envelope(_envelope_results())
    assert "~1.7s" in out and "1.6589s" not in out
    assert "~6.9s" in out and "6.8743s" not in out
    assert "~2.9s" in out and "2.93965s" not in out


def test_operating_envelope_row1_pends_when_scenario_not_pass():
    # A non-PASS warm matrix scenario has no publishable warm figure ⇒ row 1 pends; the other
    # three rows (independent sources) still render.
    scen = _full_gvisor_scenarios()
    for s in scen:
        if s["name"] == "warmpool_cold_start":
            s["outcome"] = "FAIL"
    out = render.render_operating_envelope(
        _matrix_results(scen, at_scale_contention=_asc(),
                        concurrent_burst=_cb(), warm_pool_acquisition=_wpa()))
    assert "| Steady trickle — warm pool keeps up with demand | pending | full start → first result |" in out
    assert "| Bursty — pool oversubscribed 2:1 (60 claims / 30 ready) | ~1.7s | full start → first result |" in out


def test_operating_envelope_row1_pend_inherits_pending_reason():
    # hb#134 (a4s1 nit): a genuinely-pending row-1 inherits the matrix scenario's pending_reason
    # so a known upstream/cluster gap reads `pending (<reason>)` here exactly as the matrix does —
    # NOT a bare `pending` a reader could mistake for not-yet-run.
    scen = _full_gvisor_scenarios()
    for s in scen:
        if s["name"] == "warmpool_cold_start":
            s["outcome"] = "pending"
            s["pending_reason"] = "upstream-blocked"
    out = render.render_operating_envelope(
        _matrix_results(scen, at_scale_contention=_asc(),
                        concurrent_burst=_cb(), warm_pool_acquisition=_wpa()))
    assert ("| Steady trickle — warm pool keeps up with demand | "
            "pending (upstream-blocked) | full start → first result |") in out


def test_operating_envelope_row1_missing_ttfe_pass_is_bare_pending():
    # Reason decorates only a genuinely-pending scenario: a PASS warm scenario missing ttfe_p50_ms
    # (no publishable figure) falls back to bare `pending`, never a spurious `(reason)`.
    scen = _full_gvisor_scenarios()
    for s in scen:
        if s["name"] == "warmpool_cold_start":
            s["outcome"] = "PASS"
            s["pending_reason"] = "upstream-blocked"  # must NOT leak onto a non-pending row
            s.get("sla_metrics", {}).pop("ttfe_p50_ms", None)
    out = render.render_operating_envelope(
        _matrix_results(scen, at_scale_contention=_asc(),
                        concurrent_burst=_cb(), warm_pool_acquisition=_wpa()))
    assert "| Steady trickle — warm pool keeps up with demand | pending | full start → first result |" in out
    assert "pending (upstream-blocked)" not in out


def test_operating_envelope_row2_pends_when_contention_absent():
    # INERT at_scale_contention (absent) ⇒ row 2 inherits its pending semantics.
    out = render.render_operating_envelope(
        _matrix_results(_full_gvisor_scenarios(),
                        concurrent_burst=_cb(), warm_pool_acquisition=_wpa()))
    assert "| Bursty — pool oversubscribed (more claims than ready pool) | pending | full start → first result |" in out
    # its neighbours still render
    assert "| 300 sandboxes requested at once (1:1 pool) | ~6.9s | full start → first result |" in out


def test_operating_envelope_row2_pends_when_contention_inert_bad_runtime():
    # A present-but-INERT contention block (out-of-enum runtime) ⇒ row 2 pends (not a partial lie).
    out = render.render_operating_envelope(
        _matrix_results(_full_gvisor_scenarios(),
                        at_scale_contention=_asc(runtime_class="lukewarm"),
                        concurrent_burst=_cb(), warm_pool_acquisition=_wpa()))
    assert "| Bursty — pool oversubscribed (more claims than ready pool) | pending | full start → first result |" in out


def test_operating_envelope_row3_pends_when_burst_absent():
    out = render.render_operating_envelope(
        _matrix_results(_full_gvisor_scenarios(),
                        at_scale_contention=_asc(), warm_pool_acquisition=_wpa()))
    assert "| Hundreds of sandboxes requested at once (1:1 pool) | pending | full start → first result |" in out


def test_operating_envelope_row3_falls_back_to_largest_warm_leg():
    # With no n=300 warm leg, the row falls back to the LARGEST-n warm leg and labels the actual
    # N — render-derived, degrades gracefully, no hardcoded 300-lock.
    cb = _cb(legs=[
        {"n": 100, "mode": "warm", "ttfe_p50_ms": 3000.0, "ttfe_p95_ms": 4000.0},
        {"n": 500, "mode": "warm", "ttfe_p50_ms": 11188.0, "ttfe_p95_ms": 15374.0},
    ])
    out = render.render_operating_envelope(
        _matrix_results(_full_gvisor_scenarios(), concurrent_burst=cb))
    assert "| 500 sandboxes requested at once (1:1 pool) | ~11.2s | full start → first result |" in out


def test_operating_envelope_row3_ignores_cold_legs():
    # A burst block with ONLY cold legs has no warm leg ⇒ row 3 pends (never surfaces a cold
    # number under a warm-pool label).
    cb = _cb(legs=[{"n": 300, "mode": "cold", "ttfe_p50_ms": 56029.4, "ttfe_p95_ms": 58412.4}])
    out = render.render_operating_envelope(
        _matrix_results(_full_gvisor_scenarios(), concurrent_burst=cb))
    assert "| Hundreds of sandboxes requested at once (1:1 pool) | pending | full start → first result |" in out


def test_operating_envelope_row4_pends_when_acquisition_absent():
    out = render.render_operating_envelope(
        _matrix_results(_full_gvisor_scenarios(),
                        at_scale_contention=_asc(), concurrent_burst=_cb()))
    assert "| Sustained high-rate churn | pending | pool hand-off only (before exec) |" in out


def test_operating_envelope_row4_generic_label_without_rate():
    # offered_rate_per_s is optional context — absent ⇒ generic churn label, still renders the wait.
    wpa = _wpa()
    del wpa["offered_rate_per_s"]
    out = render.render_operating_envelope(
        _matrix_results(_full_gvisor_scenarios(), warm_pool_acquisition=wpa))
    assert "| Sustained high-rate churn | ~2.9s | pool hand-off only (before exec) |" in out


def test_operating_envelope_excludes_speedup_leg():
    # The warm_vs_cold speedup leg is ratio-CONTEXT, not a budgetable wait — it must NOT add a
    # fifth row nor change any wait. Rendering with and without it is byte-identical.
    base = render.render_operating_envelope(_envelope_results())
    withwc = render.render_operating_envelope(_envelope_results(warm_vs_cold=_wc(warm_p50_ms=700.5, speedup=7.283)))
    assert base == withwc
    # exactly four data rows (header + separator + 4), no fifth speedup row
    data_rows = [ln for ln in withwc.splitlines() if ln.startswith("| ") and "---" not in ln]
    assert len(data_rows) == 5  # 1 header + 4 data
    assert "speedup" not in withwc.lower()


# --- hb#134: "What this means for you" plain-English synthesis ------------------------------


def test_what_this_means_always_renders_skeleton_all_clauses_degrade():
    # No source blocks ⇒ the section STILL renders (always-on, mirrors render_operating_envelope);
    # every MEASURED clause degrades to its qualitative form (never a guessed number), and all four
    # static bullets are present.
    out = render.render_what_this_means(_matrix_results([]))
    assert "## What this means for you" in out
    # Clause 1/2/3 degraded (qualitative, no measured figure).
    assert "a new sandbox is ready quickly" in out
    assert "A warm-pool hit is much faster than starting cold." in out
    assert "Big simultaneous bursts still work, but they are the pool-overflow regime" in out
    # The measured phrasings must be ABSENT when nothing is measured.
    assert "ready in ~" not in out
    assert "faster than starting cold (" not in out
    assert "asked for at once settled in" not in out
    # Static bullets always present (rule-of-thumb + product-shape posture).
    assert "Rule of thumb for pool size" in out
    assert "Both runtimes are measured — choose by isolation need." in out
    assert "Do not design around suspend/resume yet." in out
    assert "is unmeasured, not bad." in out


def test_what_this_means_renders_measured_numbers():
    # Full fixture ⇒ every measured clause carries the render-derived number from the SAME cleaner
    # the tables use (warm p50/p95 from the matrix warm scenario; speedup+runtime from warm_vs_cold;
    # the many-at-once wait from the warm concurrent-burst leg).
    out = render.render_what_this_means(
        _matrix_results(_full_gvisor_scenarios(), warm_vs_cold=_wc(), concurrent_burst=_cb()))
    # Clause 1 — warmpool_cold_start ttfe_p50_ms=600 ⇒ ~0.6s, p95=900 ⇒ ~0.9s.
    assert "a new sandbox is ready in ~0.6s (~0.9s at the p95)." in out
    # Clause 2 — _wc() cold_ms/warm_p50_ms = 3000/300 = 10 ⇒ "about 10× faster ... (gVisor)".
    assert "A warm-pool hit is about 10× faster than starting cold (gVisor)." in out
    # Clause 3 — warm concurrent-burst leg n=300, ttfe_p50_ms=6874.3 ⇒ ~6.9s.
    assert "300 sandboxes asked for at once settled in ~6.9s." in out


def test_what_this_means_no_double_hedge_on_wait_figures():
    # _fmt_wait already prefixes "~"; the prose must not add "about" in front of a ~-value
    # (would read "about ~0.6s"). Speedup keeps "about" because that figure carries no "~".
    out = render.render_what_this_means(
        _matrix_results(_full_gvisor_scenarios(), warm_vs_cold=_wc(), concurrent_burst=_cb()))
    assert "about ~" not in out


def test_what_this_means_warm_clause_degrades_when_scenario_not_pass():
    # A non-PASS warmpool_cold_start ⇒ clause 1 degrades even though burst/speedup are measured.
    scen = [{"name": "warmpool_cold_start", "outcome": "pending", "n": 200,
             "sla_metrics": {"ttfe_p50_ms": 600, "ttfe_p95_ms": 900}}]
    out = render.render_what_this_means(
        _matrix_results(scen, warm_vs_cold=_wc(), concurrent_burst=_cb()))
    assert "a new sandbox is ready quickly" in out
    assert "ready in ~" not in out
    # The other two clauses still render their measured numbers.
    assert "about 10× faster" in out
    assert "settled in ~6.9s" in out


def test_what_this_means_speedup_clause_degrades_and_no_runtime_leak():
    # An out-of-enum runtime_class fails the warm_vs_cold block CLOSED ⇒ clause 2 degrades and the
    # bogus runtime string never reaches the page.
    out = render.render_what_this_means(
        _matrix_results(_full_gvisor_scenarios(), warm_vs_cold=_wc(runtime_class="trust-me-vm")))
    assert "A warm-pool hit is much faster than starting cold." in out
    assert "trust-me-vm" not in out
    assert "faster than starting cold (" not in out  # the "(runtime)" paren only rides the measured form


def test_what_this_means_burst_clause_degrades_when_burst_absent():
    # No concurrent_burst ⇒ clause 3 degrades; the measured "N sandboxes ... settled in" is absent.
    out = render.render_what_this_means(
        _matrix_results(_full_gvisor_scenarios(), warm_vs_cold=_wc()))
    assert "Big simultaneous bursts still work, but they are the pool-overflow regime" in out
    assert "asked for at once settled in" not in out


def test_what_this_means_public_safe_no_internal_names():
    # PII fence: static prose + public runtime labels only — no internal cluster names / project-ids.
    out = render.render_what_this_means(
        _matrix_results(_full_gvisor_scenarios(), warm_vs_cold=_wc(), concurrent_burst=_cb()))
    for forbidden in ("sandbox-scenarios-cluster", "substrate-demo-cluster",
                      "alexbu-gke-dev-d", "postgres-obs-0"):
        assert forbidden not in out


def test_what_this_means_in_full_readme_between_matrix_and_scale():
    # #134 fold: the Warm-vs-Cold punchline was dropped from the page (redundant with the
    # what-this-means speedup bullet; the full table stays in DETAILS), so "What this means for you"
    # now sits between the core-metrics matrix and the cluster-scale block.
    from generate import build_readme
    readme = build_readme()
    wtm_at = readme.find("## What this means for you")
    assert wtm_at != -1
    assert readme.index("## Agent Sandbox — Core Metrics") < wtm_at
    assert wtm_at < readme.index("## Does it hold at cluster scale?")


def test_recipe_renders_h2_and_is_static():
    # #4021: render_recipe is product-agnostic static prose (no results arg) and always renders.
    out = render.render_recipe()
    assert out.startswith("## Reproduce it")
    assert render.render_recipe() == out  # deterministic / no hidden state


def test_recipe_cross_links_reproduce_not_duplicated():
    # Note-1: the runnable version is cross-linked to recipe/REPRODUCE.md, not duplicated inline.
    out = render.render_recipe()
    assert "recipe/REPRODUCE.md" in out
    # It must NOT inline the runnable command surface (that is REPRODUCE.md's job).
    assert "python3 -m harness.run" not in out
    assert "kind create cluster" not in out


def test_recipe_drained_caveat_reference_is_soft_prose():
    # Note-2: the drained-regime warm_scaling_term clause only renders on the #4138 fire, so the
    # recipe must read fine whether or not that clause is on the page — the reference is
    # conditional soft prose ("When a drained-regime fire is on the page"), never a hard forward-ref.
    out = render.render_recipe()
    assert "When a drained-regime fire is on the page" in out


def test_recipe_no_contested_sub_1s_headline_and_no_stale_literal():
    # Note-3 (honest-metrics guardrail): the recipe must NOT slip a contested sub-1s@300/s headline
    # onto the page, and must NOT restate a hardcoded honest-today latency that could contradict the
    # live Warm-Pool Acquisition / Concurrent Burst cells — it references those cells by pointer.
    out = render.render_recipe()
    assert "not yet published" in out
    assert "1.76" not in out  # the drafted literal is stale vs the live 300/s acquisition p95
    assert "Warm-Pool Acquisition" in out and "Concurrent Burst" in out


def test_recipe_public_safe_generic_tokens_only():
    # PII fence: only generic/vendor-public tokens; no internal cluster names / project-ids.
    out = render.render_recipe()
    for tok in ("e2-standard-16", "gvisor", "RuntimeClass", "GKE", "DaemonSet"):
        assert tok in out
    for forbidden in ("sandbox-scenarios-cluster", "substrate-demo-cluster",
                      "alexbu-gke-dev-d", "postgres-obs-0", "googleplex"):
        assert forbidden not in out


def test_recipe_in_full_readme_after_data_sections():
    # Note-1 placement: the recipe renders ONCE, after the data sections (it forward-refs "above").
    from generate import build_readme
    readme = build_readme()
    assert readme.count("## Reproduce it") == 1
    recipe_at = readme.index("## Reproduce it")
    contention_at = readme.find("## At Scale Under Contention")
    if contention_at != -1:
        assert recipe_at > contention_at


# --- #4162 Option A: North-Star scorecard (warm-hit p95 vs the 500ms bar) --------------------


def test_north_star_gvisor_miss_prints_measured_gap():
    out = render.render_north_star(_matrix_results(_full_gvisor_scenarios()))
    assert "## North-Star check — warm-pool TTFE p95 < 0.5s" in out
    # 900ms warm-hit p95 misses the 500ms bar by exactly 0.4s — the gap is printed, not implied.
    assert "| gVisor | 0.9s (count=200) | ❌ not met (0.4s above the bar) |" in out


def test_north_star_met_bar_prints_headroom():
    scen = [{
        "name": "warmpool_cold_start", "outcome": "PASS", "n": 200,
        "sla_metrics": {"ttfe_p95_ms": 400},
    }]
    out = render.render_north_star(_matrix_results(scen))
    assert "| gVisor | 0.4s (count=200) | ✅ met (0.1s headroom) |" in out


def test_north_star_exact_bar_is_not_met():
    # Strict `<`: p95 == 500ms does NOT meet a "< 500ms" bar (never round toward the claim).
    scen = [{
        "name": "warmpool_cold_start", "outcome": "PASS", "n": 200,
        "sla_metrics": {"ttfe_p95_ms": 500},
    }]
    out = render.render_north_star(_matrix_results(scen))
    assert "❌ not met (0s above the bar)" in out


def test_north_star_low_n_dagger_on_p95_cell():
    scen = [{
        "name": "warmpool_cold_start", "outcome": "PASS", "n": 5,
        "sla_metrics": {"ttfe_p95_ms": 900},
    }]
    out = render.render_north_star(_matrix_results(scen))
    assert "| gVisor | 0.9s (count=5) † | ❌ not met (0.4s above the bar) |" in out


def test_north_star_unmeasured_runtime_pends():
    # No kata companion artifact ⇒ the kata row pends on both cells — never a guess.
    out = render.render_north_star(_matrix_results(_full_gvisor_scenarios()))
    assert "| Kata + microVM | pending | pending |" in out


def test_north_star_kata_pending_warm_scenario_falls_through_to_pending():
    # The default kata fixture's warm-pool scenario is pending (pool-topology-constrained):
    # its suppressed metrics must fall through to `pending`, not leak a number.
    out = render.render_north_star(
        _matrix_results(_full_gvisor_scenarios()), kata_results=_kata_results()
    )
    assert "| Kata + microVM | pending | pending |" in out


def test_north_star_kata_results_fills_kata_row():
    kata_scen = [{
        "name": "warmpool_cold_start", "outcome": "PASS", "n": 30,
        "sla_metrics": {"ttfe_p95_ms": 986.7},
    }]
    out = render.render_north_star(
        _matrix_results(_full_gvisor_scenarios()),
        kata_results=_kata_results(scenarios=kata_scen),
    )
    assert "| Kata + microVM | 0.9867s (count=30) | ❌ not met (0.4867s above the bar) |" in out
    # gVisor row unchanged by the companion artifact.
    assert "| gVisor | 0.9s (count=200) | ❌ not met (0.4s above the bar) |" in out


def test_north_star_kata_results_wrong_product_or_runtime_ignored():
    kata_scen = [{
        "name": "warmpool_cold_start", "outcome": "PASS", "n": 30,
        "sla_metrics": {"ttfe_p95_ms": 400},
    }]
    for kr in (
        _kata_results(scenarios=kata_scen, product="sandbox"),
        _kata_results(scenarios=kata_scen, provenance={"runtime": "gvisor"}),
    ):
        out = render.render_north_star(_matrix_results(_full_gvisor_scenarios()), kata_results=kr)
        assert "| Kata + microVM | pending | pending |" in out


def test_north_star_unknown_product_renders_nothing():
    out = render.render_north_star({"product": "not-a-product", "scenarios": []})
    assert out == ""


def test_north_star_in_full_readme_nested_under_what_this_means():
    # #134 fold: North-Star + Operating Envelope are demoted to ### sub-blocks under
    # "## What this means for you"; the envelope ("### What wait should I budget?") renders first,
    # the North-Star ("### How close to the North Star?") second.
    from generate import build_readme
    readme = build_readme()
    ns_at = readme.find("### How close to the North Star?")
    assert ns_at != -1
    envelope_at = readme.find("### What wait should I budget?")
    assert envelope_at != -1
    assert readme.index("## What this means for you") < envelope_at
    assert envelope_at < ns_at
    assert ns_at < readme.index("## Does it hold at cluster scale?")


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok: {fn.__name__}")
    print(f"test_render: all {len(fns)} assertions passed")


if __name__ == "__main__":
    _run_all()
