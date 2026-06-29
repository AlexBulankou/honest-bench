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


def test_controller_only_scrape_yields_empty():
    # The stale-controller-digest case (a Phase-0 abort, 2026-06-29): a deployed controller
    # on an old image emits ONLY the _controller_ sibling, NOT the headline metric. The
    # headline parser must then measure NOTHING -- it must never fall back to the
    # controller-observed series, which excludes the claim-create->observe queueing lag and
    # so under-reports TTFE under load. This is the honest-abort property the live
    # metric-present fire-gate relies on (sibling present, headline absent -> measured=False).
    controller_only = (
        'agent_sandbox_claim_controller_startup_latency_ms_bucket{launch_type="warm",le="100"} 50\n'
        'agent_sandbox_claim_controller_startup_latency_ms_bucket{launch_type="warm",le="+Inf"} 100\n'
        'agent_sandbox_claim_controller_startup_latency_ms_count{launch_type="warm"} 100\n'
    )
    _check(p.parse_metric_histograms(controller_only) == [],
           "controller-only scrape -> zero HEADLINE histograms (sibling never folded in)")
    _check(p.ttfe_by_launch_type(controller_only) == {},
           "controller-only scrape -> {} (measured=False, never substitute the sibling)")


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


# ---------------------------------------------------------------- per-step delta convergence

# Two consecutive CUMULATIVE warm scrapes from one controller lifetime. The per-step
# INCREMENT (END - START) has a DIFFERENT quantile than either scrape's union -- that
# divergence is the whole reason the delta path exists (a multi-step end-scrape union
# matches no single per-step pareto point).
def _warm_scrape(buckets, count):
    lines = [
        f'agent_sandbox_claim_startup_latency_ms_bucket{{launch_type="warm",le="{le}"}} {c}'
        for le, c in buckets
    ]
    lines.append(f'agent_sandbox_claim_startup_latency_ms_count{{launch_type="warm"}} {count}')
    return "\n".join(lines) + "\n"


_DELTA_START = _warm_scrape(
    [("100", 0), ("250", 5), ("500", 20), ("1000", 40), ("2500", 49), ("+Inf", 50)], 50)
_DELTA_END = _warm_scrape(
    [("100", 0), ("250", 10), ("500", 50), ("1000", 90), ("2500", 99), ("+Inf", 100)], 100)


def test_delta_window_quantile_differs_from_union():
    # increment cum: (100,0)(250,5)(500,30)(1000,50)(2500,50)(inf,50), total 50.
    # p50 rank=25 -> bucket (250,500]: 250 + 250*((25-5)/(30-5)) = 250 + 250*(20/25) = 450.0
    step = p.ttfe_by_launch_type_delta(_DELTA_START, _DELTA_END)
    union = p.ttfe_by_launch_type(_DELTA_END)
    _check(_close(step["warm"]["ttfe_p50_ms"], 450.0), "per-step p50 == 450.0 (windowed)")
    _check(_close(union["warm"]["ttfe_p50_ms"], 500.0), "union p50 == 500.0 (cumulative)")
    _check(step["warm"]["ttfe_p50_ms"] != union["warm"]["ttfe_p50_ms"],
           "per-step quantile DIFFERS from the cumulative-union quantile")


def test_delta_empty_start_equals_single_end_scrape():
    # Fresh-restarted controller (empty histogram before the step): the increment IS the
    # end scrape, so the delta path collapses to exactly ttfe_by_launch_type(end) -- the
    # Phase-1 single-step bonus (no diffing needed).
    _check(p.ttfe_by_launch_type_delta("", _DELTA_END) == p.ttfe_by_launch_type(_DELTA_END),
           "empty start -> delta == single end-scrape quantile")
    _check(p.ttfe_by_launch_type_delta("# nothing\n", _DELTA_END)
           == p.ttfe_by_launch_type(_DELTA_END), "comment-only start -> same as empty")


def test_delta_counter_reset_yields_measured_false():
    # END < START (controller restarted between scrapes) -> the launch_type is OMITTED,
    # never a meaningless cross-reset delta.
    reset_end = _warm_scrape(
        [("100", 0), ("250", 1), ("500", 2), ("1000", 3), ("2500", 4), ("+Inf", 5)], 5)
    _check(p.ttfe_by_launch_type_delta(_DELTA_END, reset_end) == {},
           "reset (end<start) -> {} (measured=False, not a fabricated delta)")


def test_delta_zero_increment_omitted_not_zeroed():
    # No new claims this step (END == START) -> increment total 0 -> launch_type omitted,
    # never a fabricated 0ms.
    _check(p.ttfe_by_launch_type_delta(_DELTA_END, _DELTA_END) == {},
           "zero increment -> omitted, never a fabricated 0ms")


def test_delta_new_launch_type_carries_full_end_histogram():
    # cold first appears DURING this step (absent from start) -> its whole end histogram is
    # the increment, so cold is measured even though start had no cold series.
    start = _DELTA_START  # warm only
    end = _DELTA_END + (
        'agent_sandbox_claim_startup_latency_ms_bucket{launch_type="cold",le="1000"} 0\n'
        'agent_sandbox_claim_startup_latency_ms_bucket{launch_type="cold",le="2500"} 5\n'
        'agent_sandbox_claim_startup_latency_ms_bucket{launch_type="cold",le="5000"} 8\n'
        'agent_sandbox_claim_startup_latency_ms_bucket{launch_type="cold",le="+Inf"} 10\n'
        'agent_sandbox_claim_startup_latency_ms_count{launch_type="cold"} 10\n'
    )
    step = p.ttfe_by_launch_type_delta(start, end)
    _check(set(step) == {"warm", "cold"}, "cold (new since start) is measured via full-end increment")
    _check(_close(step["cold"]["ttfe_p50_ms"], 2500.0), "cold p50 from its full end histogram")


def test_histogram_delta_subtracts_and_detects_reset():
    s = p.parse_metric_histograms(_DELTA_START)[0]
    e = p.parse_metric_histograms(_DELTA_END)[0]
    d = p.histogram_delta(s, e)
    by_le = dict(d.buckets)
    _check(by_le[500.0] == 30.0, "le=500 increment == 50-20 == 30")
    _check(by_le[float("inf")] == 50.0, "+Inf increment == 100-50 == 50")
    _check(d.count == 50.0, "_count increment == 100-50 == 50")
    _check(p.histogram_delta(e, s) is None, "reset (end<start) -> None at the Histogram level")


# ---------------------------------------------------------------- sandbox_template aggregation
#
# The controller emits one histogram series per (launch_type, sandbox_template) pair. The
# PUBLIC page surfaces latency ONLY by launch_type, so the parser must SUM the template series
# per launch_type before taking a percentile -- and the template label VALUES (internal
# scenario names) must never reach the output. The template names below are SYNTHETIC.

# warm has two templates with DIFFERENT distributions, so first-template-wins (the old bug)
# would give a DIFFERENT, wrong answer than the aggregate: alpha all <=100ms, beta all in
# (1000,2500]. cold is single-template.
_WARM_ALPHA = [("100", 50), ("250", 50), ("500", 50), ("1000", 50), ("2500", 50), ("+Inf", 50)]
_WARM_BETA = [("100", 0), ("250", 0), ("500", 0), ("1000", 0), ("2500", 50), ("+Inf", 50)]
_COLD_GAMMA = [("1000", 0), ("2500", 6), ("5000", 9), ("+Inf", 10)]


def _tpl_bucket(metric, launch_type, template, le, c):
    return (f'{metric}_bucket{{launch_type="{launch_type}",'
            f'sandbox_template="{template}",le="{le}"}} {c}')


def _multi_template_scrape(metric, scale=1.0):
    def series(lt, tpl, buckets):
        out = [_tpl_bucket(metric, lt, tpl, le, int(c * scale)) for le, c in buckets]
        out.append(f'{metric}_count{{launch_type="{lt}",sandbox_template="{tpl}"}} '
                   f'{int(buckets[-1][1] * scale)}')
        return out
    lines = (series("warm", "alpha", _WARM_ALPHA)
             + series("warm", "beta", _WARM_BETA)
             + series("cold", "gamma", _COLD_GAMMA))
    return "\n".join(lines) + "\n"


def test_aggregate_sums_templates_per_launch_type():
    agg = p.aggregate_by_launch_type(
        p.parse_metric_histograms(_multi_template_scrape(p.HEADLINE_METRIC)))
    _check(set(agg) == {"warm", "cold"}, "aggregated to launch_types only")
    warm = dict(agg["warm"].buckets)
    _check(warm[100.0] == 50.0, "warm le=100 == 50(alpha)+0(beta) == 50")
    _check(warm[2500.0] == 100.0, "warm le=2500 == 50(alpha)+50(beta) == 100")
    _check(warm[float("inf")] == 100.0, "warm +Inf == 100 (both templates)")
    _check(agg["warm"].count == 100.0, "warm _count summed across templates")
    _check(set(agg["warm"].labels) == {"launch_type"}, "template label dropped from aggregate")


def test_aggregate_percentile_differs_from_first_template():
    out = p.ttfe_by_launch_type(_multi_template_scrape(p.HEADLINE_METRIC))
    # aggregated warm p50 rank=50 -> le=100 boundary (cum 50) -> 0 + 100*(50/50) == 100.0;
    # first-template-only (alpha) would give 50.0 -- so aggregation is load-bearing here.
    _check(_close(out["warm"]["ttfe_p50_ms"], 100.0), "aggregated warm p50 == 100.0 (not 50.0)")
    _check(_close(out["warm"]["ttfe_p95_ms"], 2350.0), "aggregated warm p95 == 2350.0")


def test_aggregate_skips_series_without_launch_type():
    scrape = (
        f'{p.HEADLINE_METRIC}_bucket{{sandbox_template="alpha",le="100"}} 5\n'
        f'{p.HEADLINE_METRIC}_bucket{{sandbox_template="alpha",le="+Inf"}} 10\n'
    )
    _check(p.aggregate_by_launch_type(p.parse_metric_histograms(scrape)) == {},
           "series with no launch_type label is skipped, not invented")


# ---------------------------------------------------------------- controller-startup proxy
#
# PROXY_METRIC (controller-observed -> ready) populates on the deployed controller NOW. Its
# percentiles are a LOWER BOUND on true TTFE, surfaced as a distinct caveated field with
# proxy emit keys -- never aliased onto the headline.

def test_proxy_assembler_emits_proxy_keys_aggregated():
    out = p.controller_startup_by_launch_type(_multi_template_scrape(p.PROXY_METRIC))
    _check(set(out) == {"warm", "cold"}, "proxy measured for both launch_types")
    _check(set(out["warm"]) == {
        "controller_startup_p50_ms", "controller_startup_p95_ms", "controller_startup_p99_ms"},
        "proxy emits exactly the controller_startup triple")
    _check(_close(out["warm"]["controller_startup_p50_ms"], 100.0),
           "proxy warm p50 aggregated == 100.0")
    _check(_close(out["warm"]["controller_startup_p95_ms"], 2350.0),
           "proxy warm p95 aggregated == 2350.0")


def test_proxy_and_headline_never_alias():
    # A scrape carrying ONLY the headline metric -> proxy assembler measures nothing; and a
    # scrape with ONLY the proxy metric -> headline assembler measures nothing. The exact-name
    # match keeps the lower-bound proxy and the headline TTFE from ever bleeding together.
    headline_only = _multi_template_scrape(p.HEADLINE_METRIC)
    proxy_only = _multi_template_scrape(p.PROXY_METRIC)
    _check(p.controller_startup_by_launch_type(headline_only) == {},
           "proxy assembler ignores the headline metric")
    _check(p.ttfe_by_launch_type(proxy_only) == {},
           "headline assembler ignores the proxy metric")


def test_proxy_output_carries_no_template_names():
    # The public-page rule: a template label VALUE must never surface. The proxy output is
    # keyed by launch_type, and each value dict holds only controller_startup_* numeric keys.
    out = p.controller_startup_by_launch_type(_multi_template_scrape(p.PROXY_METRIC))
    for lt, triple in out.items():
        _check(lt in ("warm", "cold"), "top key is a launch_type, never a template")
        for k in triple:
            _check(k.startswith("controller_startup_p"),
                   f"{k} is a proxy percentile key, not a template name")


def test_proxy_delta_windowed_and_aggregated():
    # Per-step proxy increment over multi-template scrapes: start = half-count, end = full,
    # so the increment is the other half. The increment still aggregates across templates and
    # emits the proxy triple.
    start = _multi_template_scrape(p.PROXY_METRIC, scale=0.5)
    end = _multi_template_scrape(p.PROXY_METRIC, scale=1.0)
    step = p.controller_startup_by_launch_type_delta(start, end)
    _check(set(step["warm"]) == {
        "controller_startup_p50_ms", "controller_startup_p95_ms", "controller_startup_p99_ms"},
        "proxy delta emits the controller_startup triple")
    # warm increment per le: alpha 25 each + beta {2500:25,+Inf:25} -> le=100..1000 cum 25,
    # le=2500 cum 50, +Inf 50. p50 rank=25 -> le=100 boundary -> 0 + 100*(25/25) == 100.0.
    _check(_close(step["warm"]["controller_startup_p50_ms"], 100.0),
           "proxy delta warm p50 aggregated == 100.0")


def test_proxy_delta_empty_start_equals_single_end():
    end = _multi_template_scrape(p.PROXY_METRIC)
    _check(p.controller_startup_by_launch_type_delta("", end)
           == p.controller_startup_by_launch_type(end),
           "empty start -> proxy delta == single end-scrape proxy quantile")


def _run_all():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"ok - {len(tests)} prom_ttfe tests passed")


if __name__ == "__main__":
    _run_all()
