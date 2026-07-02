"""Tests for the per-mode SLO cluster-rate derivation (harness/slo_rate.py, #149 follow-up).

The derivation's honesty spine under test:
  - max measured ready_per_s among COMPLIANT rungs, per bar, independently;
  - offered_rate_per_s is never substituted for ready_per_s;
  - no compliant rung => key OMITTED (pend), never 0.0;
  - node_count required for any emit; int-coerced into the triple;
  - non-monotonic sweeps credit only rungs that themselves comply.
"""

import math

from .slo_rate import slo_cluster_rate, slo_sla_metrics_from_stepup


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
        }

    def test_partial_fill_one_bar_only(self):
        # Lowest rung clears 5s but not 1s: 5s half fills, 1s half stays pending.
        pts = [_rung(30, 28.4, 3200.0), _rung(100, 41.0, 12610.3)]
        out = slo_sla_metrics_from_stepup({"pareto_points": pts, "node_count": 40})
        assert out == {
            "thpt_under_5s_per_cluster": 28.4,
            "thpt_cluster_node_count": 40,
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
        # The derived dict must survive results_schema._coerce_sla_metrics untouched.
        from .results_schema import _coerce_sla_metrics

        out = slo_sla_metrics_from_stepup({"pareto_points": SWEEP, "node_count": 40})
        assert _coerce_sla_metrics(out) == out
