"""Offline tests for render.check_render_downgrade -- no cluster, no I/O.

Run with bare python3 (mirrors harness/test_run_cell_downgrade.py and
render/test_render.py's own convention):
    python3 render/test_render_downgrade.py

These assert the hb#550 property: a rendered page that would silently
downgrade a trust surface -- a measured FAIL scenario missing its disclosure
marker in the rendered text, or a measured (non-pending) activation-mode
scenario whose matrix-cleaned metrics come back entirely empty (every cell
would render as bare `pending`, indistinguishable from never-measured) -- is
detected. The check is scoped to ACTIVATION_MODE_ROWS names only: a
non-activation scenario (e.g. burst_create) legitimately carries metric keys
outside MATRIX_METRIC_FIELDS (its own BURST_CORROBORATION_FIELDS set) and
must never false-positive.
"""

import render


def test_clean_measured_pass_no_findings():
    scenarios = [
        {
            "name": "warmpool_cold_start",
            "outcome": "PASS",
            "n": 30,
            "sla_metrics": {"ttfe_p50_ms": 755.6, "ttfe_p95_ms": 900.0},
        }
    ]
    assert render.check_render_downgrade(scenarios, "some rendered page text") == []


def test_fail_with_disclosure_marker_present_clean():
    scenarios = [
        {
            "name": "warmpool_cold_start",
            "outcome": "FAIL",
            "n": 30,
            "sla_metrics": {"ttfe_p95_ms": 5200.0},
        }
    ]
    text = "row shows warmpool_cold_start ⚠️ FAIL somewhere in the matrix"
    assert render.check_render_downgrade(scenarios, text) == []


def test_fail_missing_disclosure_marker_detected():
    scenarios = [
        {
            "name": "warmpool_cold_start",
            "outcome": "FAIL",
            "n": 30,
            "sla_metrics": {"ttfe_p95_ms": 5200.0},
        }
    ]
    lines = render.check_render_downgrade(scenarios, "no disclosure anywhere in this text")
    assert len(lines) == 1, f"expected 1 finding, got {lines!r}"
    assert "warmpool_cold_start" in lines[0] and "FAIL" in lines[0]


def test_fail_with_details_only_marker_still_clean():
    # render_what_this_means's marker only ever lands in DETAILS.md, not README.md --
    # the caller must scan the COMBINED text, which this asserts at the check-fn level.
    scenarios = [
        {
            "name": "suspend_resume",
            "outcome": "FAIL",
            "n": 10,
            "sla_metrics": {"ttfe_p50_ms": 4000.0},
        }
    ]
    text = "...somewhere in DETAILS.md: this scenario did NOT clear its SLA..."
    assert render.check_render_downgrade(scenarios, text) == []


def test_fail_without_real_ttfe_not_gated():
    # A FAIL that never reached first execution (no ttfe_p50/p95) is disclosed via the
    # Execution-Success cell instead -- mirrors render_matrix's own hb#4420 scope gate.
    scenarios = [
        {
            "name": "native_digest_cold",
            "outcome": "FAIL",
            "n": 10,
            "sla_metrics": {"exec_success_rate": 0.0, "exec_success_n": 10},
        }
    ]
    assert render.check_render_downgrade(scenarios, "no disclosure marker at all") == []


def test_measured_non_pending_empty_cleaned_metrics_detected():
    # sla_metrics carries only unknown/non-matrix keys -> _clean_matrix_metrics empties it
    # -> every cell for this row would render bare `pending`, indistinguishable from
    # never-measured. That is the silent downgrade this leg exists to catch.
    scenarios = [
        {
            "name": "suspend_resume",
            "outcome": "PASS",
            "n": 5,
            "sla_metrics": {"some_unknown_internal_field": 42},
        }
    ]
    lines = render.check_render_downgrade(scenarios, "irrelevant rendered text")
    assert len(lines) == 1, f"expected 1 finding, got {lines!r}"
    assert "suspend_resume" in lines[0] and "pending" in lines[0]


def test_pending_outcome_never_gates():
    # A genuinely pending scenario (never measured) is not a downgrade -- it's the
    # honest starting state.
    scenarios = [
        {
            "name": "suspend_resume",
            "outcome": "pending",
            "sla_metrics": {"ttfe_p50_ms": 1.0},
        }
    ]
    assert render.check_render_downgrade(scenarios, "") == []


def test_no_measured_metrics_never_gates():
    scenarios = [
        {"name": "warmpool_cold_start", "outcome": "PASS", "sla_metrics": {}}
    ]
    assert render.check_render_downgrade(scenarios, "") == []


def test_non_activation_scenario_with_foreign_metrics_never_false_positives():
    # burst_create is NOT an activation-mode row: its sla_metrics legitimately carry
    # BURST_CORROBORATION_FIELDS keys that are entirely outside MATRIX_METRIC_FIELDS, so
    # _clean_matrix_metrics legitimately empties them. The scoping to ACTIVATION_MODE_ROWS
    # is exactly what must prevent this from reading as a downgrade.
    scenarios = [
        {
            "name": "burst_create",
            "outcome": "FAIL",
            "n": 100,
            "sla_metrics": {
                "sandboxes_ready_under_1s": 40,
                "sandboxes_exec_under_1s": 35,
                "exec_success_rate": 0.9,
            },
        }
    ]
    assert render.check_render_downgrade(scenarios, "no disclosure marker present") == []


def test_multiple_findings_reported_together():
    scenarios = [
        {
            "name": "warmpool_cold_start",
            "outcome": "FAIL",
            "sla_metrics": {"ttfe_p50_ms": 4000.0},
        },
        {
            "name": "native_digest_cold",
            "outcome": "PASS",
            "sla_metrics": {"unknown_key": 1},
        },
    ]
    lines = render.check_render_downgrade(scenarios, "nothing disclosed here")
    assert len(lines) == 2, f"expected 2 findings, got {lines!r}"


def test_malformed_inputs_tolerated():
    assert render.check_render_downgrade(None, "") == []
    assert render.check_render_downgrade([], "") == []
    assert render.check_render_downgrade([], None) == []
    assert render.check_render_downgrade(
        [
            "not-a-dict",
            {"outcome": "PASS", "sla_metrics": {"ttfe_p50_ms": 1.0}},  # no name
            {"name": 42, "outcome": "PASS", "sla_metrics": {"ttfe_p50_ms": 1.0}},  # non-str name
            {"name": "warmpool_cold_start", "outcome": "PASS", "sla_metrics": "not-a-dict"},
        ],
        None,
    ) == []


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok: {fn.__name__}")
    print(f"test_render_downgrade: all {len(fns)} assertions passed")


if __name__ == "__main__":
    _run_all()
