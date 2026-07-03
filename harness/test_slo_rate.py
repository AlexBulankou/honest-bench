"""Tests for the per-mode SLO cluster-rate derivation (harness/slo_rate.py, #149 follow-up).

The derivation's honesty spine under test:
  - max measured ready_per_s among COMPLIANT rungs, per bar, independently;
  - offered_rate_per_s is never substituted for ready_per_s;
  - no compliant rung => key OMITTED (pend), never 0.0;
  - node_count required for any emit; int-coerced into the triple;
  - non-monotonic sweeps credit only rungs that themselves comply.
"""

from __future__ import annotations

# Make this file runnable BOTH as `python3 harness/test_slo_rate.py` (the CB
# unit-tests gate's bare-python3 discover-all convention) and via pytest, by
# putting the repo root on sys.path before the absolute import below (mirrors
# test_run_slo_sweep.py / test_run_stepup.py).
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import math

from harness.slo_rate import (
    LITERAL_N_EXEC_OK_FLOOR,
    SLO_BASIS_ENUM,
    SLO_BASIS_LITERAL_ACQ,
    SLO_BASIS_LITERAL_CONTROLLER,
    SLO_BASIS_TRUE_TTFE,
    slo_cluster_rate,
    slo_sla_metrics_from_stepup,
)


def _rung(offered, ready, p95):
    return {"offered_rate_per_s": offered, "ready_per_s": ready, "ttfe_p95_ms": p95}


# A typical sweep: p95 rises with rate; 1s bar clears only at the low rung,
# 5s bar clears through the middle rung, top rung overloads past both.
SWEEP = [
    _rung(10, 9.8, 850.0),
    _rung(30, 28.4, 3200.0),
    _rung(100, 41.0, 12610.3),
]


class TestSloClusterRate:
    def test_max_compliant_rung_per_bar(self):
        assert slo_cluster_rate(SWEEP, 5000.0) == 28.4
        assert slo_cluster_rate(SWEEP, 1000.0) == 9.8

    def test_no_compliant_rung_returns_none_not_zero(self):
        # Sweep never probed below the boundary: proves nothing about it.
        assert slo_cluster_rate([_rung(100, 41.0, 12610.3)], 1000.0) is None

    def test_empty_and_non_list_return_none(self):
        assert slo_cluster_rate([], 5000.0) is None
        assert slo_cluster_rate(None, 5000.0) is None
        assert slo_cluster_rate({"offered_rate_per_s": 10}, 5000.0) is None

    def test_missing_ready_per_s_makes_rung_ineligible(self):
        # offered present, ready absent — offered must NOT be substituted.
        pts = [{"offered_rate_per_s": 10, "ttfe_p95_ms": 850.0}]
        assert slo_cluster_rate(pts, 5000.0) is None

    def test_missing_p95_makes_rung_ineligible(self):
        pts = [{"offered_rate_per_s": 10, "ready_per_s": 9.8}]
        assert slo_cluster_rate(pts, 5000.0) is None

    def test_zero_or_negative_ready_ineligible(self):
        assert slo_cluster_rate([_rung(10, 0.0, 850.0)], 5000.0) is None
        assert slo_cluster_rate([_rung(10, -1.0, 850.0)], 5000.0) is None

    def test_nan_inf_and_bool_values_ineligible(self):
        assert slo_cluster_rate([_rung(10, math.nan, 850.0)], 5000.0) is None
        assert slo_cluster_rate([_rung(10, 9.8, math.inf)], 5000.0) is None
        assert slo_cluster_rate([_rung(10, True, 850.0)], 5000.0) is None

    def test_non_dict_rungs_skipped(self):
        pts = ["junk", None, _rung(10, 9.8, 850.0)]
        assert slo_cluster_rate(pts, 5000.0) == 9.8

    def test_non_monotonic_sweep_credits_only_compliant_rungs(self):
        # p95 dips back under the bar at a HIGHER rate: that rung complies on its
        # own and is credited; the overloaded middle rung is not.
        pts = [
            _rung(10, 9.8, 850.0),
            _rung(30, 28.4, 6200.0),   # over 5s bar
            _rung(50, 44.1, 4100.0),   # dips back under 5s
        ]
        assert slo_cluster_rate(pts, 5000.0) == 44.1
        assert slo_cluster_rate(pts, 1000.0) == 9.8

    def test_boundary_inclusive(self):
        assert slo_cluster_rate([_rung(10, 9.8, 5000.0)], 5000.0) == 9.8


class TestSloSlaMetricsFromStepup:
    def test_both_bars_land_with_node_count(self):
        flat = {"pareto_points": SWEEP, "node_count": 40}
        out = slo_sla_metrics_from_stepup(flat)
        assert out == {
            "thpt_under_5s_per_cluster": 28.4,
            "thpt_under_1s_per_cluster": 9.8,
            "thpt_cluster_node_count": 40,
            "thpt_slo_basis": SLO_BASIS_TRUE_TTFE,
        }

    def test_partial_fill_one_bar_only(self):
        # Lowest rung clears 5s but not 1s: 5s half fills, 1s half stays pending.
        pts = [_rung(30, 28.4, 3200.0), _rung(100, 41.0, 12610.3)]
        out = slo_sla_metrics_from_stepup({"pareto_points": pts, "node_count": 40})
        assert out == {
            "thpt_under_5s_per_cluster": 28.4,
            "thpt_cluster_node_count": 40,
            "thpt_slo_basis": SLO_BASIS_TRUE_TTFE,
        }
        assert "thpt_under_1s_per_cluster" not in out

    def test_no_compliant_rung_emits_nothing(self):
        pts = [_rung(100, 41.0, 12610.3)]
        assert slo_sla_metrics_from_stepup({"pareto_points": pts, "node_count": 40}) == {}

    def test_missing_or_invalid_node_count_emits_nothing(self):
        assert slo_sla_metrics_from_stepup({"pareto_points": SWEEP}) == {}
        assert slo_sla_metrics_from_stepup({"pareto_points": SWEEP, "node_count": 0}) == {}
        assert slo_sla_metrics_from_stepup({"pareto_points": SWEEP, "node_count": -3}) == {}
        assert slo_sla_metrics_from_stepup({"pareto_points": SWEEP, "node_count": True}) == {}
        assert slo_sla_metrics_from_stepup({"pareto_points": SWEEP, "node_count": 40.5}) == {}

    def test_integral_float_node_count_coerced_to_int(self):
        out = slo_sla_metrics_from_stepup({"pareto_points": SWEEP, "node_count": 40.0})
        assert out["thpt_cluster_node_count"] == 40
        assert isinstance(out["thpt_cluster_node_count"], int)

    def test_non_dict_input_emits_nothing(self):
        assert slo_sla_metrics_from_stepup(None) == {}
        assert slo_sla_metrics_from_stepup([SWEEP]) == {}

    def test_rounding_matches_throughput_per_cluster_convention(self):
        pts = [_rung(10, 9.87654, 850.0)]
        out = slo_sla_metrics_from_stepup({"pareto_points": pts, "node_count": 40})
        assert out["thpt_under_5s_per_cluster"] == 9.877

    def test_output_passes_scenario_sla_coercion(self):
        # The derived dict must survive results_schema._coerce_sla_metrics untouched —
        # including the hb#174 thpt_slo_basis stamp via its enum-gated carve-out.
        from harness.results_schema import _coerce_sla_metrics

        out = slo_sla_metrics_from_stepup({"pareto_points": SWEEP, "node_count": 40})
        assert "thpt_slo_basis" in out
        assert _coerce_sla_metrics(out) == out


def _lit_rung(offered, warm_p95, ctrl=None, acq=None, n=24):
    # n defaults above LITERAL_N_EXEC_OK_FLOOR so pre-floor tests stay valid;
    # pass n=None to build a rung with the sample count ABSENT.
    pt = {"offered_rate_per_s": offered, "literal_warm_p95_ms": warm_p95}
    if ctrl is not None:
        pt["controller_completed_per_s"] = ctrl
    if acq is not None:
        pt["acq_fulfilled_per_s"] = acq
    if n is not None:
        pt["literal_warm_n_exec_ok"] = n
    return pt


class TestSloBasisSelection:
    """hb#174: literal upper-bound fallback basis — order, gating, and the stamp."""

    def test_true_ttfe_wins_when_it_derives(self):
        # A live true-TTFE pareto shadows the literal leg entirely.
        flat = {
            "pareto_points": SWEEP,
            "node_count": 40,
            "literal_ttfe": {
                "upper_bound": True,
                "pareto_points": [_lit_rung(10, 900.0, ctrl=99.0)],
            },
        }
        out = slo_sla_metrics_from_stepup(flat)
        assert out["thpt_slo_basis"] == SLO_BASIS_TRUE_TTFE
        assert out["thpt_under_5s_per_cluster"] == 28.4

    def test_literal_controller_fallback_when_true_ttfe_dead(self):
        # The #3975 dead-family shape: true-TTFE points carry no ready_per_s.
        dead = [{"offered_rate_per_s": 10, "ready_per_s": None, "ttfe_p95_ms": None}]
        flat = {
            "pareto_points": dead,
            "node_count": 2,
            "literal_ttfe": {
                "upper_bound": True,
                "pareto_points": [
                    _lit_rung(10, 850.0, ctrl=9.4, acq=9.9),
                    _lit_rung(30, 3200.0, ctrl=27.1, acq=29.5),
                    _lit_rung(100, 12610.3, ctrl=40.2, acq=44.0),
                ],
            },
        }
        out = slo_sla_metrics_from_stepup(flat)
        assert out == {
            "thpt_under_5s_per_cluster": 27.1,
            "thpt_under_1s_per_cluster": 9.4,
            "thpt_cluster_node_count": 2,
            "thpt_slo_basis": SLO_BASIS_LITERAL_CONTROLLER,
            "thpt_slo_n_exec_ok": 24,
        }

    def test_acq_fallback_only_when_controller_rate_absent(self):
        flat = {
            "node_count": 2,
            "literal_ttfe": {
                "upper_bound": True,
                "pareto_points": [_lit_rung(10, 850.0, acq=9.9)],
            },
        }
        out = slo_sla_metrics_from_stepup(flat)
        assert out["thpt_slo_basis"] == SLO_BASIS_LITERAL_ACQ
        assert out["thpt_under_1s_per_cluster"] == 9.9

    def test_one_basis_per_triple_never_mixed(self):
        # 5s bar derivable from controller rate; 1s bar would need the acq rate
        # (controller absent on the low rung). One basis per triple: the acq-only
        # 1s rung must NOT fill from a second basis alongside the controller 5s.
        flat = {
            "node_count": 2,
            "literal_ttfe": {
                "upper_bound": True,
                "pareto_points": [
                    _lit_rung(10, 850.0, acq=9.9),        # 1s-compliant, acq only
                    _lit_rung(30, 3200.0, ctrl=27.1),     # 5s-compliant, ctrl only
                ],
            },
        }
        out = slo_sla_metrics_from_stepup(flat)
        assert out["thpt_slo_basis"] == SLO_BASIS_LITERAL_CONTROLLER
        assert out["thpt_under_5s_per_cluster"] == 27.1
        assert "thpt_under_1s_per_cluster" not in out

    def test_upper_bound_flag_gates_literal_leg(self):
        # Missing/False/None upper_bound: the literal leg is never consulted —
        # an unflagged latency basis could fabricate compliance.
        pts = [_lit_rung(10, 850.0, ctrl=9.4)]
        for flag in (False, None, "true", 1):
            flat = {
                "node_count": 2,
                "literal_ttfe": {"upper_bound": flag, "pareto_points": pts},
            }
            assert slo_sla_metrics_from_stepup(flat) == {}, f"flag={flag!r}"
        assert slo_sla_metrics_from_stepup({"node_count": 2, "literal_ttfe": pts}) == {}

    def test_literal_never_reads_true_ttfe_keys(self):
        # A literal rung mislabeled with true-TTFE keys derives nothing — the
        # namespaced keys are load-bearing, no aliasing.
        flat = {
            "node_count": 2,
            "literal_ttfe": {
                "upper_bound": True,
                "pareto_points": [_rung(10, 9.8, 850.0)],
            },
        }
        assert slo_sla_metrics_from_stepup(flat) == {}

    def test_offered_rate_never_substituted_in_literal_leg(self):
        # literal rung with a compliant p95 but no measured rate: nothing derives.
        flat = {
            "node_count": 2,
            "literal_ttfe": {
                "upper_bound": True,
                "pareto_points": [_lit_rung(10, 850.0)],
            },
        }
        assert slo_sla_metrics_from_stepup(flat) == {}

    def test_enum_matches_results_schema_mirror(self):
        # Cross-contract: the closed-schema emitter's independent mirror must not drift.
        from harness.results_schema import SLO_BASIS_ENUM as SCHEMA_ENUM

        assert tuple(SCHEMA_ENUM) == tuple(SLO_BASIS_ENUM)
        assert SLO_BASIS_ENUM == (
            SLO_BASIS_TRUE_TTFE,
            SLO_BASIS_LITERAL_CONTROLLER,
            SLO_BASIS_LITERAL_ACQ,
        )

    def test_true_ttfe_triple_never_carries_n_exec_ok(self):
        out = slo_sla_metrics_from_stepup({"pareto_points": SWEEP, "node_count": 40})
        assert out["thpt_slo_basis"] == SLO_BASIS_TRUE_TTFE
        assert "thpt_slo_n_exec_ok" not in out

    def test_literal_output_passes_scenario_sla_coercion(self):
        from harness.results_schema import _coerce_sla_metrics

        flat = {
            "node_count": 2,
            "literal_ttfe": {
                "upper_bound": True,
                "pareto_points": [_lit_rung(10, 850.0, ctrl=9.4)],
            },
        }
        out = slo_sla_metrics_from_stepup(flat)
        assert out["thpt_slo_basis"] == SLO_BASIS_LITERAL_CONTROLLER
        assert _coerce_sla_metrics(out) == out


class TestLiteralNExecOkFloor:
    """hb#174 sign-off condition (c): warm-exec sample floor on literal rungs."""

    def _flat(self, pts):
        return {
            "node_count": 2,
            "literal_ttfe": {"upper_bound": True, "pareto_points": pts},
        }

    def test_floor_constant_is_twenty(self):
        assert LITERAL_N_EXEC_OK_FLOOR == 20

    def test_sub_floor_rung_ineligible(self):
        out = slo_sla_metrics_from_stepup(
            self._flat([_lit_rung(10, 850.0, ctrl=9.4, n=19)])
        )
        assert out == {}

    def test_floor_boundary_inclusive(self):
        out = slo_sla_metrics_from_stepup(
            self._flat([_lit_rung(10, 850.0, ctrl=9.4, n=20)])
        )
        assert out["thpt_under_1s_per_cluster"] == 9.4
        assert out["thpt_slo_n_exec_ok"] == 20

    def test_absent_n_fails_closed(self):
        out = slo_sla_metrics_from_stepup(
            self._flat([_lit_rung(10, 850.0, ctrl=9.4, n=None)])
        )
        assert out == {}

    def test_bool_and_non_integral_n_ineligible(self):
        for bad in (True, 24.5, "24", float("nan")):
            out = slo_sla_metrics_from_stepup(
                self._flat([_lit_rung(10, 850.0, ctrl=9.4, n=bad)])
            )
            assert out == {}, f"n={bad!r}"

    def test_integral_float_n_coerced_to_int(self):
        out = slo_sla_metrics_from_stepup(
            self._flat([_lit_rung(10, 850.0, ctrl=9.4, n=24.0)])
        )
        assert out["thpt_slo_n_exec_ok"] == 24
        assert isinstance(out["thpt_slo_n_exec_ok"], int)

    def test_n_stamp_is_min_across_credited_rungs(self):
        # 1s bar credits the n=20 rung; 5s bar credits the n=48 rung. The stamp is
        # the MIN — the weakest sample behind any published figure.
        out = slo_sla_metrics_from_stepup(
            self._flat(
                [
                    _lit_rung(10, 850.0, ctrl=9.4, n=20),
                    _lit_rung(30, 3200.0, ctrl=27.1, n=48),
                ]
            )
        )
        assert out["thpt_under_1s_per_cluster"] == 9.4
        assert out["thpt_under_5s_per_cluster"] == 27.1
        assert out["thpt_slo_n_exec_ok"] == 20

    def test_sub_floor_rung_skipped_not_bar_dropped(self):
        # The higher-rate compliant rung is thin-sample: it is SKIPPED, and the bar
        # honestly credits the lower-rate rung that cleared the floor — the floor
        # gates rung eligibility, it never discards a bar that has a floored rung.
        out = slo_sla_metrics_from_stepup(
            self._flat(
                [
                    _lit_rung(30, 3200.0, ctrl=27.1, n=12),
                    _lit_rung(10, 2900.0, ctrl=9.4, n=32),
                ]
            )
        )
        assert out["thpt_under_5s_per_cluster"] == 9.4
        assert out["thpt_slo_n_exec_ok"] == 32
        assert "thpt_under_1s_per_cluster" not in out


def _all_tests():
    tests = []
    for k, v in sorted(globals().items()):
        if k.startswith("Test") and isinstance(v, type):
            for m in sorted(dir(v)):
                if m.startswith("test_"):
                    tests.append((f"{k}.{m}", getattr(v(), m)))
    return tests


def main() -> int:
    tests = _all_tests()
    failures = 0
    for name, t in tests:
        try:
            t()
            print(f"PASS {name}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {name}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
