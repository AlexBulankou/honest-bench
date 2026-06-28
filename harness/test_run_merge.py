"""Offline tests for run.merge_seed_placeholders — no cluster, no I/O.

Run with bare python3 (no pytest, so the auto-refresh GH-runner needs nothing
extra):  python3 -m harness.test_run_merge
or directly:               python3 harness/test_run_merge.py

These assert the #3909 property: a partial `--product` run preserves hand-seeded
`pending` placeholder rows for cells the registered suite does not (yet) produce,
instead of clobbering them on the wholesale write.
"""

from __future__ import annotations

from .run import merge_seed_placeholders


def _check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def test_unregistered_pending_placeholders_carried():
    # The substrate shape: suite registers 1 cell, seed declares 3.
    raw = [{"name": "agent_identity_podcert", "outcome": "pending",
            "pending_reason": "requires-gke"}]
    prior = [
        {"name": "cold_reconcile", "outcome": "pending",
         "pending_reason": "not-yet-measured", "n": 0},
        {"name": "suspend_resume_carryover", "outcome": "pending",
         "pending_reason": "not-yet-measured", "n": 0},
        {"name": "agent_identity_podcert", "outcome": "pending",
         "pending_reason": "not-yet-measured", "n": 0},
    ]
    merged = merge_seed_placeholders(raw, prior)
    names = [s["name"] for s in merged]
    _check(names == ["agent_identity_podcert", "cold_reconcile",
                     "suspend_resume_carryover"],
           f"fresh first, unregistered placeholders appended in seed order, got {names}")
    # The registered cell wins via its FRESH run, not the seed's stale reason.
    _check(merged[0]["pending_reason"] == "requires-gke",
           "registered cell keeps fresh value, not seed's not-yet-measured")


def test_noop_when_seed_matches_suite():
    # The sandbox shape: every seeded name is also produced fresh -> nothing carried.
    raw = [{"name": "warmpool_cold_start", "outcome": "PASS", "n": 1},
           {"name": "suspend_resume", "outcome": "PASS"}]
    prior = [{"name": "warmpool_cold_start", "outcome": "pending",
              "pending_reason": "not-yet-measured"},
             {"name": "suspend_resume", "outcome": "pending",
              "pending_reason": "not-yet-measured"}]
    merged = merge_seed_placeholders(raw, prior)
    _check(merged == raw, f"no-op when all seed names are freshly run, got {merged}")


def test_stale_measured_unregistered_row_not_resurrected():
    # A seeded UNregistered row that is PASS/FAIL (not pending) is dropped, not
    # carried — we never resurrect a stale measurement the suite no longer produces.
    raw = [{"name": "a", "outcome": "PASS"}]
    prior = [{"name": "old_measured", "outcome": "PASS", "sla_metrics": {"x": 1.0}},
             {"name": "old_pending", "outcome": "pending",
              "pending_reason": "not-yet-measured"}]
    merged = merge_seed_placeholders(raw, prior)
    names = [s["name"] for s in merged]
    _check(names == ["a", "old_pending"],
           f"only unregistered PENDING carried, stale measured dropped, got {names}")


def test_empty_prior_is_passthrough():
    raw = [{"name": "a", "outcome": "PASS"}]
    _check(merge_seed_placeholders(raw, []) == raw, "empty seed -> passthrough")
    _check(merge_seed_placeholders(raw, None) == raw, "None seed -> passthrough")


def test_malformed_prior_rows_skipped():
    raw = [{"name": "a", "outcome": "PASS"}]
    prior = ["not-a-dict", {"no_name": True},
             {"name": 123, "outcome": "pending"},          # non-str name
             {"name": "ok", "outcome": "pending",
              "pending_reason": "not-yet-measured"}]
    merged = merge_seed_placeholders(raw, prior)
    names = [s["name"] for s in merged]
    _check(names == ["a", "ok"], f"only the well-formed pending row carried, got {names}")


def _all_tests():
    return [v for k, v in sorted(globals().items())
            if k.startswith("test_") and callable(v)]


def main() -> int:
    failures = 0
    for t in _all_tests():
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {t.__name__}: {e}")
    print(f"\n{len(_all_tests()) - failures}/{len(_all_tests())} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
