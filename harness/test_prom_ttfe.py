"""Offline tests for the controller-histogram TTFE parser -- no scrape, no cluster.

Run with bare python3 (the auto-refresh GH-runner needs nothing extra):
    python3 -m harness.test_prom_ttfe
or directly:
    python3 harness/test_prom_ttfe.py

The load-bearing tests are:
  - histogram_quantile matches HAND-COMPUTED classic-Prometheus values (interpolation
    within a finite bucket, the first-finite-bucket lower-bound-0 path, and the +Inf
    fall-through that returns the highest finite upper bound) -- this is the property
    that makes the parser a faithful cross-check oracle for CL2's own histogram_quantile;
  - honest count==0 -> the launch_type is OMITTED, never a fabricated 0ms;
  - a missing metric / no-+Inf histogram -> EMPTY result (measured=False), not collapse;
  - the sibling _controller_ variant is NOT folded into the headline metric (exact-name
    match), so the headline TTFE is never diluted by the controller-observed series;
  - warm + cold are parsed as two distinct series and assembled independently.
"""

from __future__ import annotations

from . import prom_ttfe as p


def _check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _close(a, b, eps=0.1):
    return a is not None and abs(a - b) <= eps


# A realistic two-launch_type scrape, plus the _controller_ sibling (different values) to
# prove exact-name matching excludes it, plus HELP/TYPE comment lines.
SCRAPE = """\
# HELP agent_sandbox_claim_startup_latency_ms claim-create to ready, by launch_type
# TYPE agent_sandbox_claim_startup_latency_ms histogram
agent_sandbox_claim_startup_latency_ms_bucket{launch_type="warm",le="100"} 0
agent_sandbox_claim_startup_latency_ms_bucket{launch_type="warm",le="250"} 10
agent_sandbox_claim_startup_latency_ms_bucket{launch_type="warm",le="500"} 50
agent_sandbox_claim_startup_latency_ms_bucket{launch_type="warm",le="1000"} 90
agent_sandbox_claim_startup_latency_ms_bucket{launch_type="warm",le="2500"} 99
agent_sandbox_claim_startup_latency_ms_bucket{launch_type="warm",le="+Inf"} 100
agent_sandbox_claim_startup_latency_ms_sum{launch_type="warm"} 51234.5
agent_sandbox_claim_startup_latency_ms_count{launch_type="warm"} 100
agent_sandbox_claim_startup_latency_ms_bucket{launch_type="cold",le="1000"} 0
agent_sandbox_claim_startup_latency_ms_bucket{launch_type="cold",le="2500"} 5
agent_sandbox_claim_startup_latency_ms_bucket{launch_type="cold",le="5000"} 8
agent_sandbox_claim_startup_latency_ms_bucket{launch_type="cold",le="+Inf"} 10
agent_sandbox_claim_startup_latency_ms_sum{launch_type="cold"} 31000.0
agent_sandbox_claim_startup_latency_ms_count{launch_type="cold"} 10
# the controller-observed variant -- MUST be ignored by the headline parser
agent_sandbox_claim_controller_startup_latency_ms_bucket{launch_type="warm",le="100"} 100
agent_sandbox_claim_controller_startup_latency_ms_bucket{launch_type="warm",le="+Inf"} 100
agent_sandbox_claim_controller_startup_latency_ms_count{launch_type="warm"} 100
"""


# ---------------------------------------------------------------- histogram_quantile

def test_hq_interpolates_within_finite_bucket():
    # warm cumulative: (100,0)(250,10)(500,50)(1000,90)(2500,99)(inf,100)
    # p50 rank=50 -> bucket (250,500]: 250 + 250*((50-10)/(50-10)) = 500.0
    b = [(100, 0), (250, 10), (500, 50), (1000, 90), (2500, 99), (float("inf"), 100)]
    _check(_close(p.histogram_quantile(0.50, b), 500.0), "p50 interpolated == 500.0")
    # p95 rank=95 -> bucket (1000,2500]: 1000 + 1500*((95-90)/(99-90)) = 1833.33
    _check(_close(p.histogram_quantile(0.95, b), 1833.3), "p95 interpolated == 1833.3")
    # p99 rank=99 -> bucket (1000,2500], rank lands on the bucket's top: == 2500.0
    _check(_close(p.histogram_quantile(0.99, b), 2500.0), "p99 == 2500.0")


def test_hq_first_finite_bucket_starts_at_zero():
    # (100,60)(inf,100): p50 rank=50 lands in first finite bucket -> 0 + 100*(50/60)
    b = [(100, 60), (float("inf"), 100)]
    _check(_close(p.histogram_quantile(0.50, b), 83.3), "first-bucket lower bound is 0")


def test_hq_rank_in_inf_bucket_returns_highest_finite_le():
    # (1000,90)(2500,95)(inf,100): p99 rank=99 > 95 -> falls in +Inf -> highest finite le
    b = [(1000, 90), (2500, 95), (float("inf"), 100)]
    _check(p.histogram_quantile(0.99, b) == 2500.0, "p99 in +Inf -> 2500.0")


def test_hq_undefined_cases_return_none():
    _check(p.histogram_quantile(0.5, []) is None, "empty buckets -> None")
    _check(p.histogram_quantile(0.5, [(100, 5), (1000, 9)]) is None, "no +Inf -> None")
    _check(p.histogram_quantile(0.5, [(float("inf"), 0)]) is None, "zero total -> None")
    _check(p.histogram_quantile(0.5, [(float("inf"), 10)]) is None, "+Inf-only -> None")


def test_hq_q_out_of_range_raises():
    try:
        p.histogram_quantile(1.5, [(float("inf"), 10)])
    except ValueError:
        return
    raise AssertionError("q>1 should raise ValueError")


# ---------------------------------------------------------------- parsing

def test_parse_splits_warm_and_cold():
    hists = p.parse_metric_histograms(SCRAPE)
    by_lt = {h.launch_type: h for h in hists}
    _check(set(by_lt) == {"warm", "cold"}, "two launch_types parsed (no controller series)")
    _check(by_lt["warm"].count == 100.0, "warm _count attached")
    _check(by_lt["cold"].count == 10.0, "cold _count attached")
    _check(by_lt["warm"].sum == 51234.5, "warm _sum attached")
    # +Inf bucket present and cumulative count == total
    _check(p._total_observations(by_lt["warm"]) == 100.0, "warm total via +Inf bucket")


def test_controller_variant_excluded():
    # The headline parser must see ONLY the headline metric, not the _controller_ sibling.
    hists = p.parse_metric_histograms(SCRAPE)
    for h in hists:
        # controller warm had le=100 cum=100; headline warm has le=100 cum=0
        if h.launch_type == "warm":
            le100 = dict(h.buckets).get(100.0)
            _check(le100 == 0.0, "warm le=100 is headline (0), not controller (100)")


def test_label_escapes_parsed():
    lbls = p._parse_labels(r'launch_type="wa\"rm",le="100"')
    _check(lbls["launch_type"] == 'wa"rm', "escaped quote in label value")
    _check(lbls["le"] == "100", "le alongside escaped label")


# ---------------------------------------------------------------- assembler + honesty

def test_assembler_emits_locked_triple_per_launch_type():
    out = p.ttfe_by_launch_type(SCRAPE)
    _check(set(out) == {"warm", "cold"}, "both launch_types measured")
    _check(set(out["warm"]) == {"ttfe_p50_ms", "ttfe_p95_ms", "ttfe_p99_ms"},
           "warm emits exactly the locked triple")
    _check(_close(out["warm"]["ttfe_p50_ms"], 500.0), "warm p50 == 500.0")
    _check(_close(out["warm"]["ttfe_p95_ms"], 1833.3), "warm p95 == 1833.3")
    _check(_close(out["cold"]["ttfe_p50_ms"], 2500.0), "cold p50 == 2500.0")
    _check(_close(out["cold"]["ttfe_p95_ms"], 5000.0), "cold p95 -> highest finite le")


def test_zero_count_launch_type_omitted_not_zeroed():
    scrape = (
        'agent_sandbox_claim_startup_latency_ms_bucket{launch_type="warm",le="100"} 0\n'
        'agent_sandbox_claim_startup_latency_ms_bucket{launch_type="warm",le="+Inf"} 0\n'
        'agent_sandbox_claim_startup_latency_ms_count{launch_type="warm"} 0\n'
    )
    out = p.ttfe_by_launch_type(scrape)
    _check(out == {}, "count==0 -> launch_type omitted, never a fabricated 0ms")


def test_missing_metric_yields_empty():
    _check(p.ttfe_by_launch_type("# nothing here\n") == {}, "absent metric -> empty (measured=False)")
    _check(p.ttfe_by_launch_type("") == {}, "empty scrape -> empty")


def test_no_inf_bucket_omitted():
    scrape = (
        'agent_sandbox_claim_startup_latency_ms_bucket{launch_type="warm",le="100"} 5\n'
        'agent_sandbox_claim_startup_latency_ms_bucket{launch_type="warm",le="1000"} 9\n'
        'agent_sandbox_claim_startup_latency_ms_count{launch_type="warm"} 9\n'
    )
    out = p.ttfe_by_launch_type(scrape)
    _check(out == {}, "no +Inf bucket -> malformed -> omitted, not collapse")


def test_partial_triple_never_ships():
    # A histogram that yields a valid p50 yields p95/p99 too; assert all-or-nothing holds
    # by construction on the realistic scrape (each measured launch_type has all three).
    out = p.ttfe_by_launch_type(SCRAPE)
    for lt, m in out.items():
        _check(len(m) == 3, f"{lt} ships the full triple or nothing")


def _run_all():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"ok - {len(tests)} prom_ttfe tests passed")


if __name__ == "__main__":
    _run_all()
