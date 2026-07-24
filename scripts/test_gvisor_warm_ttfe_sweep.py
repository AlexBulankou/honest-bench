"""Offline unit tests for scripts/gvisor_warm_ttfe_sweep.py's pure assembly.

Fully offline: feeds captured Prometheus scrape TEXT (inline fixtures), never a live
scrape or cluster. The sweep's fire path (create/warm-gate/measure/cleanup) is a thin
Kubernetes orchestrator over already-tested harness helpers; the honesty-critical,
testable decision is `assemble_record` — that the stamp is built against
`launch_type="warm"` (NOT the cold series) and that the record shape matches the
slo_rate read-back guard's contract. These tests pin exactly that, cross-checking the
numeric p95/count against the prom_ttfe primitives rather than re-deriving quantile math.

The second block (test_*_graduat* / test_*_never_graduates* / test_*_fails_closed) is
the END-TO-END composition proof: the sweep's REAL `assemble_record` output, flowed
through the exact `stepup_adapter -> slo_rate` seam the render matrix uses, graduates the
1s bar to the BARE `true_ttfe` basis (off the `***U` acq-uncorroborated floor the gVisor
warm-pool cell rests on today). The per-seam units are each already tested in isolation
(assemble_record here; adapter in harness/test_stepup_adapter.py; basis-pick in
harness/test_slo_rate.py) — this composition pins that they COMPOSE under the producer's
exact emitted shape, so a cross-contract drift (e.g. a `params.cluster_nodes` rename)
fails here OFFLINE, before the heavy shared-cluster fire (#4364), not after it.

The module sets three os.environ knobs at import (runtime/substrate/namespace) via
setdefault, so importing it here is side-effect-safe standalone. Under a full-suite
`pytest scripts/` run, though, the kata cold sibling (scripts/kata_cold_ttfe_sweep.py)
sets the SAME WARMPOOL_COLD_START_RUNTIME_CLASS var via its own setdefault with a
DIFFERENT default ("kata-clh") -- if that module's import were ever collected first,
this module's own setdefault would silently no-op and RUNTIME_CLASS would read back
"kata-clh" instead of "gvisor". Force the value explicitly (not setdefault) right
before the import below so this module's constant is correct regardless of what ran
earlier in the same process.
"""
import os as _os
import sys as _sys

_HB_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _HB_ROOT)
_sys.path.insert(0, _os.path.join(_HB_ROOT, "scripts"))

from harness import prom_ttfe as p  # noqa: E402
from harness import slo_rate as _slo  # noqa: E402
from harness import stepup_adapter as _adapter  # noqa: E402

# See module docstring: force (not setdefault) ahead of import to survive full-suite
# collection order against the kata sibling's colliding setdefault default.
_os.environ["WARMPOOL_COLD_START_RUNTIME_CLASS"] = "gvisor"
_os.environ["BENCH_CLUSTER_SUBSTRATE"] = "gke-sandbox"
import gvisor_warm_ttfe_sweep as sweep  # noqa: E402


def _check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _warm_scrape(buckets, count, sum_ms):
    """A HEADLINE_METRIC scrape with one warm series (cumulative (le, count) buckets)."""
    lines = [
        f'agent_sandbox_claim_startup_latency_ms_bucket{{launch_type="warm",le="{le}"}} {c}'
        for le, c in buckets
    ]
    lines.append(f'agent_sandbox_claim_startup_latency_ms_sum{{launch_type="warm"}} {sum_ms}')
    lines.append(f'agent_sandbox_claim_startup_latency_ms_count{{launch_type="warm"}} {count}')
    return "\n".join(lines) + "\n"


def _cold_scrape(buckets, count, sum_ms):
    lines = [
        f'agent_sandbox_claim_startup_latency_ms_bucket{{launch_type="cold",le="{le}"}} {c}'
        for le, c in buckets
    ]
    lines.append(f'agent_sandbox_claim_startup_latency_ms_sum{{launch_type="cold"}} {sum_ms}')
    lines.append(f'agent_sandbox_claim_startup_latency_ms_count{{launch_type="cold"}} {count}')
    return "\n".join(lines) + "\n"


# Warm cumulative snapshots forming a 2-rung fire. Warm claims land sub-second, so the
# buckets concentrate in the low le's (250/500/1000ms) — the shape a genuinely-warm
# gVisor pool delivers. Counts grow so both rung increments are non-trivial.
_W0 = _warm_scrape([(250, 0), (500, 0), (1000, 0), ("+Inf", 0)], 0, 0.0)
_W1 = _warm_scrape([(250, 8), (500, 9), (1000, 10), ("+Inf", 10)], 10, 2600.0)
_W2 = _warm_scrape([(250, 24), (500, 28), (1000, 30), ("+Inf", 30)], 30, 7400.0)

_RATES = [
    {"offered_rate_per_s": 5.0, "ready_per_s": 4.9},
    {"offered_rate_per_s": 12.0, "ready_per_s": 11.5},
]


def test_assemble_record_shape_and_params():
    rec = sweep.assemble_record(
        [_W0, _W1, _W2], _RATES,
        runtime_class="gvisor", node_count=4, warmpool_size=2,
    )
    _check(set(rec) == {"params", "true_ttfe_webhook_stamped_claims", "pareto"},
           "record carries exactly the three contract fields the kata sibling / "
           "slo_rate guard expect")
    prm = rec["params"]
    _check(prm["runtime_class"] == "gvisor", "runtime_class stamped")
    _check(prm["cluster_nodes"] == 4, "node_count stamped")
    _check(prm["warmpool_size"] == 2, "warmpool_size stamped (>0 => warm provenance)")
    _check(prm["launch_type"] == "warm", "launch_type stamped warm in params")


def test_assemble_record_builds_against_warm_series():
    # Two warm rungs measured -> two warm pareto points, p95 == the WARM increment
    # delta (not cold). This is the load-bearing honesty assertion of the sweep.
    rec = sweep.assemble_record(
        [_W0, _W1, _W2], _RATES,
        runtime_class="gvisor", node_count=4, warmpool_size=2,
    )
    par = rec["pareto"]
    _check(len(par) == 2, "both warm rungs measured -> two pareto points")
    exp0 = p.ttfe_by_launch_type_delta(_W0, _W1)["warm"]["ttfe_p95_ms"]
    exp1 = p.ttfe_by_launch_type_delta(_W1, _W2)["warm"]["ttfe_p95_ms"]
    _check(par[0]["ttfe_p95_ms"] == exp0, f"rung0 p95 == warm delta ({exp0})")
    _check(par[1]["ttfe_p95_ms"] == exp1, f"rung1 p95 == warm delta ({exp1})")
    # offered/ready pass straight through from the driver bookkeeping
    _check(par[0]["offered_rate_per_s"] == 5.0 and par[1]["ready_per_s"] == 11.5,
           "rates zip through in order")
    # stamped count is the summed per-rung warm webhook-stamped increment
    c0 = p.webhook_stamped_claim_count_delta(_W0, _W1)
    c1 = p.webhook_stamped_claim_count_delta(_W1, _W2)
    _check(rec["true_ttfe_webhook_stamped_claims"] == c0 + c1,
           f"count == sum of rung increments ({c0}+{c1})")
    _check(rec["true_ttfe_webhook_stamped_claims"] >= 1,
           "a real warm fire clears the slo_rate read-back guard (>=1)")


def test_cold_only_scrape_drops_warm_pareto_but_count_accrues():
    # A pool that measured COLD (rung fired before re-warm — the exact failure the
    # inter-rung warm gate prevents): the warm launch_type is absent, so NO warm pareto
    # point is fabricated. The full-population webhook count still accrues (its job is
    # 'was the webhook live', not 'match the warm population'). This proves the sweep
    # never silently reports a cold measurement under a warm label.
    c0 = _cold_scrape([(1000, 0), (2500, 0), ("+Inf", 0)], 0, 0.0)
    c1 = _cold_scrape([(1000, 0), (2500, 6), ("+Inf", 10)], 10, 22000.0)
    rec = sweep.assemble_record(
        [c0, c1], [_RATES[0]],
        runtime_class="gvisor", node_count=4, warmpool_size=2,
    )
    _check(rec["pareto"] == [], "warm absent -> no warm pareto point (never a cold-as-warm)")
    _check(rec["true_ttfe_webhook_stamped_claims"] == 10,
           "full-population count still corroborates the live webhook")


def test_webhook_absent_is_dead_by_construction():
    # No HEADLINE_METRIC anywhere (webhook not deployed): pareto empty, count None ->
    # the slo_rate guard discards the true-TTFE basis and falls to the literal bases.
    empty = "# no webhook metric yet\n"
    rec = sweep.assemble_record(
        [empty, empty, empty], _RATES,
        runtime_class="gvisor", node_count=None, warmpool_size=2,
    )
    _check(rec["pareto"] == [], "no metric -> no pareto points (never a fake 0)")
    _check(rec["true_ttfe_webhook_stamped_claims"] is None,
           "count None -> guard falls through to literal bases")


def test_boundary_rate_length_mismatch_raises():
    # N+1 boundary scrapes must pair with exactly N rate mappings; a mismatch is a
    # driver bug, not something to silently truncate.
    try:
        sweep.assemble_record(
            [_W0, _W1, _W2], [_RATES[0]],  # 3 scrapes, 1 rate
            runtime_class="gvisor", node_count=4, warmpool_size=2,
        )
    except ValueError as e:
        _check("expected 2 boundary scrapes" in str(e),
               "mismatch raises with count in message")
    else:
        raise AssertionError("length mismatch must raise, not silently truncate")


def test_module_config_constants():
    # The warm-sweep invariants the fire path depends on, pinned so a later edit that
    # breaks them (e.g. WARMPOOL_SIZE=0) fails a test rather than silently measuring cold.
    _check(sweep.LAUNCH_TYPE == "warm", "sweep fires as warm")
    _check(sweep.WARMPOOL_SIZE > 0, "warm pool must be non-empty (WARMPOOL_SIZE>0)")
    _check(sweep.WARMPOOL_SIZE >= max(sweep.RUNG_SIZES),
           "pool must cover the largest rung so every claim can bind warm")
    _check(sweep.WARMUP_STABILITY_POLLS > 1,
           "hb#379: a warm gate needs >1 consecutive poll to reject a draining peak")
    _check(sweep.RUNTIME_CLASS == "gvisor", "runtime pinned to gvisor")


def _graduate(rec):
    """Producer record -> adapter flatten -> slo_rate basis decision.

    The exact seam the render matrix uses: `stepup_adapter.stepup_nested_to_flat`
    relabels the nested producer record into the flat shape, then
    `slo_rate.slo_sla_metrics_from_stepup` picks the basis + fills the bars. Returns
    the sla_metrics dict ({} when nothing derivable).
    """
    return _slo.slo_sla_metrics_from_stepup(_adapter.stepup_nested_to_flat(rec))


def test_warm_record_graduates_1s_bar_to_true_ttfe():
    # THE load-bearing end-to-end honesty proof of what firing #4364 buys: a real
    # webhook-corroborated WARM sweep record graduates the 1s bar to the BARE true_ttfe
    # basis (off the ***U acq-uncorroborated floor the gVisor warm-pool cell rests on
    # today). Pinned offline so a cross-contract shape drift fails HERE, not after the
    # heavy shared-cluster fire.
    rec = sweep.assemble_record(
        [_W0, _W1, _W2], _RATES,
        runtime_class="gvisor", node_count=4, warmpool_size=2,
    )
    out = _graduate(rec)
    _check(out.get("thpt_slo_basis") == "true_ttfe",
           f"warm+corroborated -> bare true_ttfe basis (got {out.get('thpt_slo_basis')})")
    _check("thpt_under_1s_per_cluster" in out,
           "the 1s bar graduates (present) — the #4364 residual this sweep closes")
    _check("thpt_under_5s_per_cluster" in out,
           "the 5s bar also derives under the true_ttfe basis")
    _check(out.get("thpt_cluster_node_count") == 4,
           "node_count flows params.cluster_nodes -> flat.node_count -> out")
    _check("thpt_slo_n_exec_ok" not in out,
           "a true_ttfe triple never carries the literal-basis exec-sample count")
    # The graduated 1s rate is the slo_rate primitive on the SAME pareto, not a
    # fabricated number: max ready_per_s among rungs whose p95 <= 1000ms.
    flat = _adapter.stepup_nested_to_flat(rec)
    exp_1s = _slo.slo_cluster_rate(flat["pareto_points"], 1000)
    _check(exp_1s is not None and out["thpt_under_1s_per_cluster"] == round(exp_1s, 3),
           "graduated 1s rate == slo_cluster_rate primitive (honesty spine preserved)")


def test_cold_only_record_never_graduates_as_warm():
    # A pool that measured COLD (warm launch_type absent -> empty warm pareto) never
    # graduates the warm true_ttfe basis, even though the full-population webhook count
    # accrues. No warm bars AND no literal_ttfe leg -> the seam yields honest nothing
    # ({}): a cold measurement is NEVER dressed as a warm graduated rate.
    c0 = _cold_scrape([(1000, 0), (2500, 0), ("+Inf", 0)], 0, 0.0)
    c1 = _cold_scrape([(1000, 0), (2500, 6), ("+Inf", 10)], 10, 22000.0)
    rec = sweep.assemble_record(
        [c0, c1], [_RATES[0]],
        runtime_class="gvisor", node_count=4, warmpool_size=2,
    )
    _check(_graduate(rec) == {}, "cold-only -> no basis, honest empty seam output")


def test_webhook_absent_record_fails_closed():
    # Webhook not deployed (no HEADLINE_METRIC anywhere): empty pareto + count None.
    # The read-back guard fails closed -> no true_ttfe basis. Proves a fire against a
    # cluster whose webhook is dead can't silently fabricate a graduated bar.
    empty = "# no webhook metric yet\n"
    rec = sweep.assemble_record(
        [empty, empty, empty], _RATES,
        runtime_class="gvisor", node_count=4, warmpool_size=2,
    )
    out = _graduate(rec)
    _check(out.get("thpt_slo_basis") != "true_ttfe",
           "webhook-absent must never graduate the true_ttfe basis")
    _check(out == {}, "no metric -> honest empty seam output")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")
