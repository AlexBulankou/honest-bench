"""Unit tests for the hb#5396 box-3 PHASE-B stamp assembler (harness.ttfe_stamp).

Fully offline: every case feeds captured Prometheus scrape TEXT (inline
fixtures), never a live scrape. The assembler is a COMPOSITION layer over the
already-tested prom_ttfe primitives, so these tests assert the composition
(rung pairing, offered/ready pass-through, honest drop/None handling, the
dead-by-construction path) and cross-check the numeric p95/count against
prom_ttfe directly rather than re-deriving the quantile math tested there.
"""

import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from harness import prom_ttfe as p
from harness import ttfe_stamp as s


def _check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _cold_scrape(buckets, count, sum_ms):
    """A HEADLINE_METRIC scrape with one cold series (cumulative (le, count) buckets)."""
    lines = [
        f'agent_sandbox_claim_startup_latency_ms_bucket{{launch_type="cold",le="{le}"}} {c}'
        for le, c in buckets
    ]
    lines.append(f'agent_sandbox_claim_startup_latency_ms_sum{{launch_type="cold"}} {sum_ms}')
    lines.append(f'agent_sandbox_claim_startup_latency_ms_count{{launch_type="cold"}} {count}')
    return "\n".join(lines) + "\n"


def _warm_scrape(buckets, count, sum_ms):
    lines = [
        f'agent_sandbox_claim_startup_latency_ms_bucket{{launch_type="warm",le="{le}"}} {c}'
        for le, c in buckets
    ]
    lines.append(f'agent_sandbox_claim_startup_latency_ms_sum{{launch_type="warm"}} {sum_ms}')
    lines.append(f'agent_sandbox_claim_startup_latency_ms_count{{launch_type="warm"}} {count}')
    return "\n".join(lines) + "\n"


# Cold cumulative snapshots forming a 2-rung fire. Boundary snapshots b0<b1<b2;
# rung0 spans (b0,b1), rung1 spans (b1,b2). Counts grow so the increments are
# non-trivial and interpolate within finite buckets (not just land in +Inf).
_B0 = _cold_scrape([(1000, 0), (2500, 0), (5000, 0), ("+Inf", 0)], 0, 0.0)
_B1 = _cold_scrape([(1000, 40), (2500, 90), (5000, 98), ("+Inf", 100)], 100, 180000.0)
_B2 = _cold_scrape([(1000, 40), (2500, 190), (5000, 240), ("+Inf", 250)], 250, 520000.0)


def test_happy_path_two_rungs():
    rungs = [
        {"offered_rate_per_s": 10, "ready_per_s": 9.8, "start_text": _B0, "end_text": _B1},
        {"offered_rate_per_s": 30, "ready_per_s": 27.5, "start_text": _B1, "end_text": _B2},
    ]
    out = s.build_true_ttfe_stamp(rungs)

    _check(set(out) == {"pareto", "true_ttfe_webhook_stamped_claims"},
           "stamp carries exactly the two contract fields")
    par = out["pareto"]
    _check(len(par) == 2, "both rungs measured cold -> two pareto points")

    # offered/ready pass straight through (driver bookkeeping, not from scrape)
    _check(par[0]["offered_rate_per_s"] == 10 and par[0]["ready_per_s"] == 9.8,
           "rung0 offered/ready passthrough")
    _check(par[1]["offered_rate_per_s"] == 30 and par[1]["ready_per_s"] == 27.5,
           "rung1 offered/ready passthrough")

    # p95 is the INCREMENT p95, cross-checked against the PHASE-A delta helper
    exp0 = p.ttfe_by_launch_type_delta(_B0, _B1)["cold"]["ttfe_p95_ms"]
    exp1 = p.ttfe_by_launch_type_delta(_B1, _B2)["cold"]["ttfe_p95_ms"]
    _check(par[0]["ttfe_p95_ms"] == exp0, f"rung0 p95 matches delta helper ({exp0})")
    _check(par[1]["ttfe_p95_ms"] == exp1, f"rung1 p95 matches delta helper ({exp1})")

    # stamped count is the summed per-rung webhook-stamped increment
    c0 = p.webhook_stamped_claim_count_delta(_B0, _B1)
    c1 = p.webhook_stamped_claim_count_delta(_B1, _B2)
    _check(out["true_ttfe_webhook_stamped_claims"] == c0 + c1,
           f"count == sum of rung increments ({c0}+{c1})")
    _check(out["true_ttfe_webhook_stamped_claims"] >= 1,
           "a real fire clears the read-back guard (>=1)")


def test_dead_by_construction_webhook_absent():
    # No HEADLINE_METRIC anywhere (webhook not deployed): the pre-Friday state.
    empty = "# no webhook metric yet\n"
    rungs = [
        {"offered_rate_per_s": 10, "ready_per_s": 9.0, "start_text": empty, "end_text": empty},
        {"offered_rate_per_s": 30, "ready_per_s": 20.0, "start_text": empty, "end_text": empty},
    ]
    out = s.build_true_ttfe_stamp(rungs)
    _check(out["pareto"] == [], "no metric -> no pareto points (never a fake 0)")
    _check(out["true_ttfe_webhook_stamped_claims"] is None,
           "count is None (measured=False) -> guard falls through to literal bases")


def test_selected_launch_type_absent_drops_point_but_count_accrues():
    # A rung with ONLY warm claims: cold launch_type absent -> no pareto point,
    # but the full-population webhook-stamped count still accrues (its job is
    # 'was the webhook live', not 'match this rung's pareto population').
    w0 = _warm_scrape([(250, 0), (500, 0), ("+Inf", 0)], 0, 0.0)
    w1 = _warm_scrape([(250, 30), (500, 48), ("+Inf", 50)], 50, 12000.0)
    rungs = [{"offered_rate_per_s": 5, "ready_per_s": 4.9, "start_text": w0, "end_text": w1}]
    out = s.build_true_ttfe_stamp(rungs)
    _check(out["pareto"] == [], "cold absent -> no cold pareto point")
    _check(out["true_ttfe_webhook_stamped_claims"] == 50,
           "full-population count still corroborates the live webhook")


def test_counter_reset_fails_closed():
    # end cold cumulative < start (controller restart mid-rung): both the p95 and
    # the full-population count go measured=False for that rung -> fail closed.
    hi = _cold_scrape([(1000, 40), (2500, 90), ("+Inf", 100)], 100, 180000.0)
    lo = _cold_scrape([(1000, 1), (2500, 2), ("+Inf", 3)], 3, 4000.0)
    rungs = [{"offered_rate_per_s": 10, "ready_per_s": 9.0, "start_text": hi, "end_text": lo}]
    out = s.build_true_ttfe_stamp(rungs)
    _check(out["pareto"] == [], "reset -> no pareto point (meaningless cross-reset delta)")
    _check(out["true_ttfe_webhook_stamped_claims"] is None,
           "reset -> count None, guard discards the true-TTFE basis")


def test_empty_start_equals_cumulative():
    # Fresh-restarted controller (empty start scrape): the increment equals the
    # end histogram, so the rung p95/count equal the cumulative reads.
    rungs = [{"offered_rate_per_s": 100, "ready_per_s": 41.0, "start_text": "", "end_text": _B2}]
    out = s.build_true_ttfe_stamp(rungs)
    _check(len(out["pareto"]) == 1, "one measured rung")
    _check(out["pareto"][0]["ttfe_p95_ms"] == p.ttfe_by_launch_type(_B2)["cold"]["ttfe_p95_ms"],
           "empty-start p95 == cumulative p95")
    _check(out["true_ttfe_webhook_stamped_claims"] == p.webhook_stamped_claim_count(_B2),
           "empty-start count == cumulative count")


def test_rungs_from_boundary_scrapes_pairs_consecutive():
    rates = [
        {"offered_rate_per_s": 10, "ready_per_s": 9.8},
        {"offered_rate_per_s": 30, "ready_per_s": 27.5},
    ]
    rungs = s.rungs_from_boundary_scrapes([_B0, _B1, _B2], rates)
    _check(len(rungs) == 2, "N+1 boundaries -> N rungs")
    _check(rungs[0]["start_text"] is _B0 and rungs[0]["end_text"] is _B1, "rung0 spans (b0,b1)")
    _check(rungs[1]["start_text"] is _B1 and rungs[1]["end_text"] is _B2, "rung1 spans (b1,b2)")
    _check(rungs[0]["offered_rate_per_s"] == 10 and rungs[1]["ready_per_s"] == 27.5,
           "rates zip in order")
    # end-to-end: the paired rungs feed the assembler unchanged
    out = s.build_true_ttfe_stamp(rungs)
    _check(len(out["pareto"]) == 2, "paired rungs assemble to two points")


def test_rungs_from_boundary_scrapes_length_mismatch_raises():
    rates = [{"offered_rate_per_s": 10, "ready_per_s": 9.8}]
    try:
        s.rungs_from_boundary_scrapes([_B0, _B1, _B2], rates)  # 3 scrapes, 1 rung
    except ValueError as e:
        _check("expected 2 boundary scrapes" in str(e), "mismatch raises with count in message")
    else:
        raise AssertionError("length mismatch must raise, not silently truncate")


def test_missing_scrape_text_raises():
    rungs = [{"offered_rate_per_s": 10, "ready_per_s": 9.8, "end_text": _B1}]  # no start_text
    try:
        s.build_true_ttfe_stamp(rungs)
    except ValueError as e:
        _check("start_text" in str(e), "missing start_text raises naming the field")
    else:
        raise AssertionError("missing scrape text must raise")
