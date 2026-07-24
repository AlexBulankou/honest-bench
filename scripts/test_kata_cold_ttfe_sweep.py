"""Offline unit tests for scripts/kata_cold_ttfe_sweep.py's pure assembly.

Fully offline: feeds captured Prometheus scrape TEXT (inline fixtures), never a live
scrape or cluster. The sweep's fire path (create/measure/cleanup) is a thin Kubernetes
orchestrator over already-tested harness helpers; the honesty-critical, testable
decision is `assemble_record` — that the stamp is built against `launch_type="cold"`
(NOT a warm series that leaked in) and that the record shape matches the slo_rate
read-back guard's contract. These tests pin exactly that, cross-checking the numeric
p95/count against the prom_ttfe primitives rather than re-deriving quantile math.

The second block (test_*_graduat* / test_*_never_graduates* / test_*_fails_closed) is
the END-TO-END composition proof mirroring the gVisor warm sibling's own composition
tests (scripts/test_gvisor_warm_ttfe_sweep.py): the sweep's REAL `assemble_record`
output, flowed through the exact `stepup_adapter -> slo_rate` seam the render matrix
uses, either graduates to the BARE `true_ttfe` basis or fails closed to `{}` — never a
cold measurement dressed as something it isn't. The per-seam units are each already
tested in isolation (assemble_record here; adapter in harness/test_stepup_adapter.py;
basis-pick in harness/test_slo_rate.py) — this composition pins that they COMPOSE
under the producer's exact emitted shape, so a cross-contract drift (e.g. a
`params.cluster_nodes` rename) fails here OFFLINE, before the heavy shared-cluster
fire, not after it.

The module sets three os.environ knobs at import (runtime/substrate/namespace) via
setdefault, so importing it here is side-effect-safe.
"""
import os as _os
import sys as _sys

_HB_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _HB_ROOT)
_sys.path.insert(0, _os.path.join(_HB_ROOT, "scripts"))

from harness import prom_ttfe as p  # noqa: E402
from harness import slo_rate as _slo  # noqa: E402
from harness import stepup_adapter as _adapter  # noqa: E402
import kata_cold_ttfe_sweep as sweep  # noqa: E402


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


# Cold cumulative snapshots forming a 2-rung fire. Cold claims pay full provisioning, so
# the buckets concentrate in the high le's (2500/5000/10000ms) — the shape a genuinely
# cold Kata provision delivers. Counts grow so both rung increments are non-trivial.
_C0 = _cold_scrape([(2500, 0), (5000, 0), (10000, 0), ("+Inf", 0)], 0, 0.0)
_C1 = _cold_scrape([(2500, 0), (5000, 1), (10000, 1), ("+Inf", 1)], 1, 4200.0)
_C2 = _cold_scrape([(2500, 0), (5000, 1), (10000, 3), ("+Inf", 3)], 3, 15600.0)

_RATES = [
    {"offered_rate_per_s": 0.5, "ready_per_s": 0.4},
    {"offered_rate_per_s": 0.8, "ready_per_s": 0.6},
]


def test_assemble_record_shape_and_params():
    rec = sweep.assemble_record(
        [_C0, _C1, _C2], _RATES,
        runtime_class="kata-clh", node_count=3, warmpool_size=0,
    )
    _check(set(rec) == {"params", "true_ttfe_webhook_stamped_claims", "pareto"},
           "record carries exactly the three contract fields the gVisor sibling / "
           "slo_rate guard expect")
    prm = rec["params"]
    _check(prm["runtime_class"] == "kata-clh", "runtime_class stamped")
    _check(prm["cluster_nodes"] == 3, "node_count stamped")
    _check(prm["warmpool_size"] == 0, "warmpool_size stamped (0 => cold provenance)")


def test_assemble_record_builds_against_cold_series():
    # Two cold rungs measured -> two cold pareto points, p95 == the COLD increment
    # delta (not warm). This is the load-bearing honesty assertion of the sweep.
    rec = sweep.assemble_record(
        [_C0, _C1, _C2], _RATES,
        runtime_class="kata-clh", node_count=3, warmpool_size=0,
    )
    par = rec["pareto"]
    _check(len(par) == 2, "both cold rungs measured -> two pareto points")
    exp0 = p.ttfe_by_launch_type_delta(_C0, _C1)["cold"]["ttfe_p95_ms"]
    exp1 = p.ttfe_by_launch_type_delta(_C1, _C2)["cold"]["ttfe_p95_ms"]
    _check(par[0]["ttfe_p95_ms"] == exp0, f"rung0 p95 == cold delta ({exp0})")
    _check(par[1]["ttfe_p95_ms"] == exp1, f"rung1 p95 == cold delta ({exp1})")
    # offered/ready pass straight through from the driver bookkeeping
    _check(par[0]["offered_rate_per_s"] == 0.5 and par[1]["ready_per_s"] == 0.6,
           "rates zip through in order")
    # stamped count is the summed per-rung cold webhook-stamped increment
    c0 = p.webhook_stamped_claim_count_delta(_C0, _C1)
    c1 = p.webhook_stamped_claim_count_delta(_C1, _C2)
    _check(rec["true_ttfe_webhook_stamped_claims"] == c0 + c1,
           f"count == sum of rung increments ({c0}+{c1})")
    _check(rec["true_ttfe_webhook_stamped_claims"] >= 1,
           "a real cold fire clears the slo_rate read-back guard (>=1)")


def test_warm_only_scrape_drops_cold_pareto_but_count_accrues():
    # A rung that (unexpectedly, given WARMPOOL_SIZE=0) bound warm instead of cold: the
    # cold launch_type is absent, so NO cold pareto point is fabricated. The full
    # population webhook count still accrues (its job is 'was the webhook live', not
    # 'match the cold population'). This proves the sweep never silently reports a warm
    # measurement under a cold label.
    w0 = _warm_scrape([(250, 0), (500, 0), ("+Inf", 0)], 0, 0.0)
    w1 = _warm_scrape([(250, 4), (500, 6), ("+Inf", 10)], 10, 3200.0)
    rec = sweep.assemble_record(
        [w0, w1], [_RATES[0]],
        runtime_class="kata-clh", node_count=3, warmpool_size=0,
    )
    _check(rec["pareto"] == [], "cold absent -> no cold pareto point (never a warm-as-cold)")
    _check(rec["true_ttfe_webhook_stamped_claims"] == 10,
           "full-population count still corroborates the live webhook")


def test_webhook_absent_is_dead_by_construction():
    # No HEADLINE_METRIC anywhere (webhook not deployed): pareto empty, count None ->
    # the slo_rate guard discards the true-TTFE basis and falls to the literal bases.
    empty = "# no webhook metric yet\n"
    rec = sweep.assemble_record(
        [empty, empty, empty], _RATES,
        runtime_class="kata-clh", node_count=None, warmpool_size=0,
    )
    _check(rec["pareto"] == [], "no metric -> no pareto points (never a fake 0)")
    _check(rec["true_ttfe_webhook_stamped_claims"] is None,
           "count None -> guard falls through to literal bases")


def test_boundary_rate_length_mismatch_raises():
    # N+1 boundary scrapes must pair with exactly N rate mappings; a mismatch is a
    # driver bug, not something to silently truncate.
    try:
        sweep.assemble_record(
            [_C0, _C1, _C2], [_RATES[0]],  # 3 scrapes, 1 rate
            runtime_class="kata-clh", node_count=3, warmpool_size=0,
        )
    except ValueError as e:
        _check("expected 2 boundary scrapes" in str(e),
               "mismatch raises with count in message")
    else:
        raise AssertionError("length mismatch must raise, not silently truncate")


def test_module_config_constants():
    # The cold-sweep invariants the fire path depends on, pinned so a later edit that
    # breaks them (e.g. WARMPOOL_SIZE>0) fails a test rather than silently measuring warm.
    _check(sweep.WARMPOOL_SIZE == 0, "every claim must pay a real cold provision")
    _check(sweep.RUNTIME_CLASS == "kata-clh", "runtime pinned to kata-clh")
    _check(len(sweep.RUNG_SIZES) >= 2, "at least two rungs -> a real pareto, not one point")


def _graduate(rec):
    """Producer record -> adapter flatten -> slo_rate basis decision.

    The exact seam the render matrix uses: `stepup_adapter.stepup_nested_to_flat`
    relabels the nested producer record into the flat shape, then
    `slo_rate.slo_sla_metrics_from_stepup` picks the basis + fills the bars. Returns
    the sla_metrics dict ({} when nothing derivable).
    """
    return _slo.slo_sla_metrics_from_stepup(_adapter.stepup_nested_to_flat(rec))


def test_cold_record_graduates_to_true_ttfe():
    # THE load-bearing end-to-end honesty proof: a real webhook-corroborated COLD sweep
    # record graduates to the BARE true_ttfe basis through the identical seam the render
    # matrix uses for every other stepup-shaped producer. Pinned offline so a
    # cross-contract shape drift fails HERE, not after the heavy shared-cluster fire.
    rec = sweep.assemble_record(
        [_C0, _C1, _C2], _RATES,
        runtime_class="kata-clh", node_count=3, warmpool_size=0,
    )
    out = _graduate(rec)
    _check(out.get("thpt_slo_basis") == "true_ttfe",
           f"cold+corroborated -> bare true_ttfe basis (got {out.get('thpt_slo_basis')})")
    _check(out.get("thpt_cluster_node_count") == 3,
           "node_count flows params.cluster_nodes -> flat.node_count -> out")
    _check("thpt_slo_n_exec_ok" not in out,
           "a true_ttfe triple never carries the literal-basis exec-sample count")
    # The graduated rate is the slo_rate primitive on the SAME pareto, not a fabricated
    # number: pin whichever bar(s) landed against slo_cluster_rate directly.
    flat = _adapter.stepup_nested_to_flat(rec)
    for bar_ms, key in ((1000, "thpt_under_1s_per_cluster"), (5000, "thpt_under_5s_per_cluster")):
        if key in out:
            exp = _slo.slo_cluster_rate(flat["pareto_points"], bar_ms)
            _check(exp is not None and out[key] == round(exp, 3),
                   f"{key} == slo_cluster_rate primitive (honesty spine preserved)")


def test_warm_only_record_never_graduates_as_cold():
    # A rung that measured WARM (cold launch_type absent -> empty cold pareto) never
    # graduates the cold true_ttfe basis, even though the full-population webhook count
    # accrues. No cold bars AND no literal_ttfe leg -> the seam yields honest nothing
    # ({}): a warm measurement is NEVER dressed as a graduated cold rate.
    w0 = _warm_scrape([(250, 0), (500, 0), ("+Inf", 0)], 0, 0.0)
    w1 = _warm_scrape([(250, 4), (500, 6), ("+Inf", 10)], 10, 3200.0)
    rec = sweep.assemble_record(
        [w0, w1], [_RATES[0]],
        runtime_class="kata-clh", node_count=3, warmpool_size=0,
    )
    _check(_graduate(rec) == {}, "warm-only -> no basis, honest empty seam output")


def test_webhook_absent_record_fails_closed():
    # Webhook not deployed (no HEADLINE_METRIC anywhere): empty pareto + count None.
    # The read-back guard fails closed -> no true_ttfe basis. Proves a fire against a
    # cluster whose webhook is dead can't silently fabricate a graduated bar.
    empty = "# no webhook metric yet\n"
    rec = sweep.assemble_record(
        [empty, empty, empty], _RATES,
        runtime_class="kata-clh", node_count=3, warmpool_size=0,
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
