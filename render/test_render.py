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


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok: {fn.__name__}")
    print(f"test_render: all {len(fns)} assertions passed")


if __name__ == "__main__":
    _run_all()
