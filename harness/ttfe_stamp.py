"""hb#5396 box-3 PHASE-B producer stamp assembler.

Composes the pure PHASE-A prom parsers (``harness.prom_ttfe``) into the top-level
stamp a CL2 step-up sweep record must carry for the true-TTFE basis:

    {"pareto": [ {offered_rate_per_s, ready_per_s, ttfe_p95_ms}, ... ],
     "true_ttfe_webhook_stamped_claims": <int | None>}

WHY THIS LAYER EXISTS (the PHASE-A / PHASE-B split)
---------------------------------------------------
``prom_ttfe`` is a PURE offline parser -- it turns one (or a start/end pair of)
Prometheus scrape TEXT blobs into percentiles and counts, and is deliberately
*not* imported by ``harness.run`` (it never touches a cluster, a clock, or a live
scrape). That is PHASE-A. What was missing (the ``no-compliant-rung`` /
``upstream_links.json`` gap this closes) is the PHASE-B *assembler*: the piece
that walks a whole step-up sweep's captured per-rung scrapes and produces the two
top-level fields the record contract in ``recipe/REPRODUCE.md`` requires, so the
downstream seam (``stepup_adapter`` -> ``slo_rate`` read-back guard) can pick the
true-TTFE basis.

This module is STILL pure/offline by construction: it operates on captured scrape
TEXT that a fire already wrote to disk, so it is unit-testable with inline
fixtures and needs no cluster. It is the ONE piece that had to be built before a
Kata re-fire could yield a record that clears the read-back guard -- a re-fire
without it produces an empty-``pareto`` / ``None``-count record that the guard
correctly rejects (falls through to the literal upper bound), so wiring this is
the prerequisite that de-risks the runtime-coupled re-fire, not a nicety.

CONVERGENCE WITH CL2 (one parser, one scrape)
---------------------------------------------
The per-rung true-TTFE ``ttfe_p95_ms`` is the p95 of the HEADLINE_METRIC
(``agent_sandbox_claim_startup_latency_ms``) INCREMENT across that rung's window
-- exactly the population CL2's own ``histogram_quantile(0.95, sum(rate(
..._bucket[$promRange])) by (le))`` measures, reimplemented byte-for-byte offline
via ``prom_ttfe.ttfe_by_launch_type_delta`` (rate() is a no-op on a quantile; the
per-rung INCREMENT is what makes a multi-step sweep's per-rung point honest --
see the delta-convergence note in ``prom_ttfe``). The stamped COUNT comes off the
SAME scrapes' same metric ``_count`` -- that histogram is ``.Observe()``d once
per claim bearing the asbx#761 ``agents.x-k8s.io/webhook-first-observed-at``
annotation, so its observation count IS the webhook-stamped population. One
parser, one scrape, convergent by construction.

HONESTY SPINE (measured=False is a first-class value, never a fabricated 0)
--------------------------------------------------------------------------
- A rung whose selected ``launch_type`` did not measure this window (no such
  claims, or a counter reset -> ``ttfe_by_launch_type_delta`` omits it) yields NO
  pareto point -- never a fake ``ttfe_p95_ms`` of 0. The pareto simply carries
  fewer rungs, which the downstream per-bar fill reads honestly.
- The stamped count is ``None`` (measured=False) iff EVERY rung's count is
  unmeasurable -- i.e. the webhook metric is absent from every scrape (webhook
  not deployed / not yet live) or a reset nukes every window. This is the
  dead-by-construction pre-deploy state: the record carries ``None``, the
  read-back guard reads absent-or-<1 as "discard true-TTFE, fall to the literal
  bases", and nothing is published as fresh that was not measured. The moment the
  webhook is live the same code auto-populates a real >=1 count.
- Count and p95 are reported strictly as-measured; the assembler NEVER papers
  over a divergence (e.g. a mid-window reset that leaves a rung's p95 measurable
  but its full-population count ``None``). That fail-closed handoff is the guard's
  job, and squaring it here would be exactly the silent trust-downgrade the
  encode-then-merge doctrine forbids.
"""

from __future__ import annotations

from typing import Iterable, Mapping, Optional

from harness.prom_ttfe import (
    HEADLINE_METRIC,
    ttfe_by_launch_type_delta,
    webhook_stamped_claim_count_delta,
)

# The Kata unique-image-COLD 5s cell is the sole remaining cell on the true-TTFE
# axis (gVisor's cold cell already graduated to controller_cold_floor_zero_
# corroborated, needs no webhook). Its claims carry launch_type="cold", so that
# is the default population the pareto p95 is taken over. Overridable for a warm
# or full-matrix sweep.
TRUE_TTFE_COLD_LAUNCH_TYPE = "cold"

_PARETO_P95_KEY = "ttfe_p95_ms"


def build_true_ttfe_stamp(
    rungs: Iterable[Mapping[str, object]],
    *,
    launch_type: str = TRUE_TTFE_COLD_LAUNCH_TYPE,
    metric_name: str = HEADLINE_METRIC,
) -> dict:
    """Assemble the ``{pareto, true_ttfe_webhook_stamped_claims}`` record stamp.

    ``rungs`` is the ordered per-rung sweep bookkeeping, one mapping per step-up
    rung, each carrying:

      - ``offered_rate_per_s`` -- the rung's target offer rate (pass-through; the
        CL2 driver owns it, the scrape cannot report it);
      - ``ready_per_s``        -- the rung's MEASURED completion rate
        (pass-through; only the measured rate is ever credited, never the offer);
      - ``start_text``, ``end_text`` -- that rung's captured Prometheus scrape
        TEXT bracketing the rung window (consecutive cumulative scrapes; the
        INCREMENT between them is this rung's population).

    Returns the two top-level fields ``recipe/REPRODUCE.md`` requires. See the
    module docstring for the honesty spine; in brief: a rung whose ``launch_type``
    did not measure is dropped from the pareto (never a fake 0), and the stamped
    count is ``None`` iff the webhook metric was absent/unmeasurable in every
    rung (dead-by-construction), else the summed webhook-stamped population.
    """
    pareto: list[dict] = []
    count_total: Optional[int] = None

    for rung in rungs:
        start_text = _require_text(rung, "start_text")
        end_text = _require_text(rung, "end_text")

        by_lt = ttfe_by_launch_type_delta(start_text, end_text, metric_name)
        triple = by_lt.get(launch_type)
        if triple is not None and _PARETO_P95_KEY in triple:
            pareto.append(
                {
                    "offered_rate_per_s": rung.get("offered_rate_per_s"),
                    "ready_per_s": rung.get("ready_per_s"),
                    _PARETO_P95_KEY: triple[_PARETO_P95_KEY],
                }
            )

        rung_count = webhook_stamped_claim_count_delta(start_text, end_text, metric_name)
        if rung_count is not None:
            count_total = (count_total or 0) + rung_count

    return {
        "pareto": pareto,
        "true_ttfe_webhook_stamped_claims": count_total,
    }


def rungs_from_boundary_scrapes(
    boundary_texts: list[str],
    rates: list[Mapping[str, object]],
) -> list[dict]:
    """Pair N+1 ordered boundary scrape TEXTs with N per-rung rate mappings.

    A step-up fire snapshots the cumulative metric at each rung boundary: one
    pre-fire snapshot then one after each of the N rungs, so N rungs need N+1
    boundary scrapes and rung ``i`` spans ``(boundary_texts[i], boundary_texts
    [i+1])``. ``rates[i]`` supplies that rung's ``offered_rate_per_s`` /
    ``ready_per_s`` (the driver bookkeeping the scrape cannot report).

    Pure pairing logic -- the fire caller reads the ``metrics-step-*.txt`` files
    off disk and hands the TEXT in here, keeping this module free of file I/O and
    fully fixture-testable. Raises ``ValueError`` on a length mismatch rather than
    silently truncating (a dropped scrape must fail loud, not mis-align rungs).
    """
    n_rungs = len(rates)
    if len(boundary_texts) != n_rungs + 1:
        raise ValueError(
            f"expected {n_rungs + 1} boundary scrapes for {n_rungs} rungs, "
            f"got {len(boundary_texts)}"
        )
    rungs: list[dict] = []
    for i, rate in enumerate(rates):
        rungs.append(
            {
                "offered_rate_per_s": rate.get("offered_rate_per_s"),
                "ready_per_s": rate.get("ready_per_s"),
                "start_text": boundary_texts[i],
                "end_text": boundary_texts[i + 1],
            }
        )
    return rungs


def _require_text(rung: Mapping[str, object], key: str) -> str:
    val = rung.get(key)
    if not isinstance(val, str):
        raise ValueError(f"rung missing required scrape text {key!r} (got {type(val).__name__})")
    return val
