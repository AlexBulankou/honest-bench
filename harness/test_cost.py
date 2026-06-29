"""Offline tests for the cost axis (cost_usd_per_1k_ready) -- no cluster, no clock.

Run with bare python3 (the auto-refresh GH-runner needs nothing extra):
    python3 -m harness.test_cost
or directly:
    python3 harness/test_cost.py

The load-bearing tests are:
  - cost_usd_per_1k_ready matches a HAND-COMPUTED value from the closed formula
    (node_count * $/node-hour) / (ready_per_s * 3600) * 1000 -- the arithmetic the
    public cost axis renders;
  - explicit usd_per_node_hour OVERRIDES the machine_type list-price fallback (the
    operator's real billing rate wins over the coarse default);
  - the honesty posture: unknown machine_type + no explicit rate -> None; ready_per_s
    or node_count None/<=0 -> None; a non-positive explicit rate -> None. Never a
    fabricated 0 or a guessed cost (the field stays absent -> honest pending);
  - the schema predicate accepts a computed cost and rejects a bad one, so the
    producer and the closed-schema allow-list agree on the field's contract.
"""

from __future__ import annotations

import math

from harness import cost as c


def _check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _close(a, b, eps=1e-6):
    return a is not None and b is not None and math.isclose(a, b, rel_tol=eps, abs_tol=eps)


def test_cost_matches_hand_computed_formula():
    # 150 nodes * $0.5363/node-hr = $80.445/hr cluster cost.
    # 300 ready/s * 3600 = 1_080_000 ready/hr.
    # cost per 1k = 80.445 / 1_080_000 * 1000 = 0.0744861... USD per 1000 ready.
    got = c.cost_usd_per_1k_ready(300.0, node_count=150, machine_type="e2-standard-16")
    want = (150 * 0.5363) / (300.0 * 3600.0) * 1000.0
    _check(_close(got, want), f"hand-computed cost mismatch: {got} != {want}")
    _check(_close(got, 0.07448611111), f"absolute cost drifted: {got}")


def test_explicit_rate_overrides_list_price():
    # Same shape, but the operator's real committed-use rate ($0.30) must win over
    # the e2-standard-16 list price ($0.5363).
    got = c.cost_usd_per_1k_ready(300.0, node_count=150,
                                  usd_per_node_hour=0.30, machine_type="e2-standard-16")
    want = (150 * 0.30) / (300.0 * 3600.0) * 1000.0
    _check(_close(got, want), f"explicit rate did not override list price: {got} != {want}")


def test_list_price_fallback_used_when_no_explicit_rate():
    got = c.cost_usd_per_1k_ready(100.0, node_count=10, machine_type="n2d-standard-8")
    want = (10 * 0.3252) / (100.0 * 3600.0) * 1000.0
    _check(_close(got, want), f"list-price fallback mismatch: {got} != {want}")


def test_unknown_machine_no_rate_is_none():
    got = c.cost_usd_per_1k_ready(300.0, node_count=150, machine_type="totally-unknown-x99")
    _check(got is None, "unknown machine_type with no explicit rate must be None, not a guess")


def test_no_price_at_all_is_none():
    got = c.cost_usd_per_1k_ready(300.0, node_count=150)
    _check(got is None, "no machine_type and no explicit rate must be None")


def test_zero_throughput_is_none_not_zero():
    _check(c.cost_usd_per_1k_ready(0.0, node_count=150, machine_type="e2-standard-16") is None,
           "ready_per_s == 0 must be None (nothing to amortize over), never a div-by-zero or fake cost")
    _check(c.cost_usd_per_1k_ready(None, node_count=150, machine_type="e2-standard-16") is None,
           "ready_per_s None must be None")
    _check(c.cost_usd_per_1k_ready(-5.0, node_count=150, machine_type="e2-standard-16") is None,
           "negative ready_per_s must be None")


def test_bad_node_count_is_none():
    _check(c.cost_usd_per_1k_ready(300.0, node_count=0, machine_type="e2-standard-16") is None,
           "node_count == 0 must be None")
    _check(c.cost_usd_per_1k_ready(300.0, node_count=None, machine_type="e2-standard-16") is None,
           "node_count None must be None")
    _check(c.cost_usd_per_1k_ready(300.0, node_count=-3, machine_type="e2-standard-16") is None,
           "negative node_count must be None")


def test_nonpositive_explicit_rate_rejected_not_fallthrough():
    # A bad explicit rate must fail honestly, NOT silently fall through to the
    # list-price table (that would paper over an operator input error).
    got = c.cost_usd_per_1k_ready(300.0, node_count=150,
                                  usd_per_node_hour=0.0, machine_type="e2-standard-16")
    _check(got is None, "explicit rate of 0 must be None, not a fallthrough to list price")
    got2 = c.cost_usd_per_1k_ready(300.0, node_count=150,
                                   usd_per_node_hour=-1.0, machine_type="e2-standard-16")
    _check(got2 is None, "negative explicit rate must be None")


def test_bool_is_not_a_number():
    # bool is an int subclass; the cost math must not treat True/False as 1/0.
    _check(c.cost_usd_per_1k_ready(True, node_count=150, machine_type="e2-standard-16") is None,
           "ready_per_s True must be rejected as non-numeric")
    _check(c.cost_usd_per_1k_ready(300.0, node_count=True, machine_type="e2-standard-16") is None,
           "node_count True must be rejected as non-numeric")
    _check(c.resolve_usd_per_node_hour(usd_per_node_hour=True) is None,
           "explicit rate True must be rejected as non-numeric")


def test_resolve_helpers():
    _check(_close(c.list_price_usd_per_node_hour("e2-standard-16"), 0.5363),
           "list price lookup wrong")
    _check(c.list_price_usd_per_node_hour("nope") is None, "unknown machine -> None")
    _check(c.list_price_usd_per_node_hour(None) is None, "None machine -> None")
    _check(_close(c.resolve_usd_per_node_hour(0.42), 0.42), "explicit rate not returned")
    _check(_close(c.resolve_usd_per_node_hour(None, "n2d-standard-4"), 0.1626),
           "fallback to list price failed")
    _check(c.resolve_usd_per_node_hour(None, None) is None, "no inputs -> None")


def test_cost_passes_schema_predicate():
    # The producer and the closed-schema allow-list must agree: a computed cost is a
    # valid optional Pareto-point value, and the honest None (absent field) is fine too.
    from render.schema import _stepup_points_ok
    cost = c.cost_usd_per_1k_ready(300.0, node_count=150, machine_type="e2-standard-16")
    point_with_cost = {"offered_rate_per_s": 300, "ttfe_p95_ms": 420.0,
                       "ready_per_s": 300.0, "cost_usd_per_1k_ready": cost}
    _check(_stepup_points_ok([point_with_cost]), "computed cost must satisfy the schema predicate")
    # Honest pending: cost absent (None -> emitter drops the key) is a valid point too.
    point_no_cost = {"offered_rate_per_s": 300, "ttfe_p95_ms": 420.0, "ready_per_s": 300.0}
    _check(_stepup_points_ok([point_no_cost]), "a point with no cost field is still valid (pending)")
    # A negative cost would be a producer bug -> the schema predicate must reject it.
    bad = {"offered_rate_per_s": 300, "ttfe_p95_ms": 420.0, "cost_usd_per_1k_ready": -0.01}
    _check(not _stepup_points_ok([bad]), "negative cost must fail the schema predicate")


def _run_all():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"ok - {len(tests)} cost tests passed")


if __name__ == "__main__":
    _run_all()
