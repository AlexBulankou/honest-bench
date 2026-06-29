"""Cost axis for the step-up throughput study -- USD per 1000 ready sandboxes.

This is the a#3960 item-4 cost axis: the schema (#47) reserved
``cost_usd_per_1k_ready`` as an optional Pareto-point field, but nothing computed
it, so the axis rendered permanently ``pending``. This module is the producer.

## The metric

A throughput-saturation study sustains a creation RATE on a warm pool, so the
honest cost question is "what does it cost to keep producing ready sandboxes at
this rate?" -- a per-throughput unit cost, directly comparable to the vendor
$/sandbox quotes (E2B / Modal / Daytona / etc.):

    cost_usd_per_1k_ready = (cluster $/hour) / (ready sandboxes/hour) * 1000
                          = (node_count * $/node-hour) / (ready_per_s * 3600) * 1000

Both inputs already live in the sweep record: ``ready_per_s`` is a Pareto-point
field (achieved throughput per step) and ``node_count`` / ``machine_type`` are in
``params`` (cluster shape). So the cost axis is pure math on data the schema
already carries -- no controller scrape, no cluster, no clock. It builds and
tests offline, independent of any fire (unlike the PHASE B scrape wiring).

## Price input -- explicit rate primary, list-price table fallback

The accurate cost depends on the cluster's REAL billing rate (on-demand vs
committed-use vs spot vs sustained-use), which only the operator knows. So the
primary path takes an explicit ``usd_per_node_hour``. The fallback is a small,
clearly-dated table of public GCP on-demand LIST prices for the common cluster
shapes -- coarse reference only; real billing is lower under CUD / spot / SUD.
The table is a convenience default, never a precision claim.

## Honest posture (mirrors prom_ttfe + the scale_proof contract)

  - Unknown machine_type AND no explicit rate -> None. We do not guess a price.
  - ``ready_per_s`` None / <= 0 -> None. No measured throughput to amortize over;
    a step that produced no ready sandboxes has no honest unit cost (exactly the
    "print 0 only when you measured 0" rule -- here there is nothing to divide by).
  - ``node_count`` None / <= 0 -> None.
  - Returns None rather than a fabricated 0 or a guess, so the schema field stays
    ABSENT (honest ``pending``) instead of rendering a fake cost. This is the same
    INFRA-vs-test split the TTFE parser uses.

Every function is a deterministic transform of plain numbers -- pure, offline,
bare-python3 testable.
"""

from __future__ import annotations

from typing import Optional


# Public GCP Compute Engine on-demand LIST prices, us-central1, USD/node-hour.
# COARSE REFERENCE DEFAULTS ONLY -- committed-use, spot, and sustained-use
# discounts make real billing materially lower. The accurate path is to pass an
# explicit usd_per_node_hour; this table is a labeled fallback for the common a4
# cluster shapes so a sweep that did not record its billing rate still renders a
# ballpark cost rather than blank. Source: public cloud.google.com VM pricing
# list, captured 2026-06. Keep this a coarse reference, not a precision claim.
_LIST_PRICE_USD_PER_NODE_HOUR = {
    "e2-standard-16": 0.5363,
    "e2-standard-8": 0.2682,
    "n2d-standard-8": 0.3252,
    "n2d-standard-4": 0.1626,
}

_SECONDS_PER_HOUR = 3600.0


def _is_pos_number(v) -> bool:
    """True iff v is a real (non-bool) number strictly greater than zero."""
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0.0


def list_price_usd_per_node_hour(machine_type: Optional[str]) -> Optional[float]:
    """Public on-demand LIST price for a known machine_type, else None.

    A thin lookup over the dated coarse-reference table -- exposed so callers can
    surface "we used the fallback list price" vs "the operator gave us the real
    rate" honestly. None for an unknown machine_type (never a guessed price)."""
    if machine_type is None:
        return None
    return _LIST_PRICE_USD_PER_NODE_HOUR.get(machine_type)


def resolve_usd_per_node_hour(usd_per_node_hour: Optional[float] = None,
                              machine_type: Optional[str] = None) -> Optional[float]:
    """Pick the node-hour price: explicit rate wins; else the list-price fallback.

    Returns None when neither an explicit positive rate nor a known machine_type
    is available -- the signal that there is no honest price to compute cost from.
    An explicit non-positive rate (<= 0) is rejected (returns None) rather than
    silently falling through to the table, so a bad explicit input fails honestly
    instead of being papered over by a default."""
    if usd_per_node_hour is not None:
        return usd_per_node_hour if _is_pos_number(usd_per_node_hour) else None
    return list_price_usd_per_node_hour(machine_type)


def cost_usd_per_1k_ready(ready_per_s: Optional[float], *,
                          node_count: Optional[int],
                          usd_per_node_hour: Optional[float] = None,
                          machine_type: Optional[str] = None) -> Optional[float]:
    """USD to sustain 1000 ready sandboxes/hour-equivalent at this throughput.

    cost = (node_count * $/node-hour) / (ready_per_s * 3600) * 1000

    Returns None (-> schema field absent -> honest ``pending``) when any input is
    missing or non-positive, or when no node-hour price can be resolved. Never
    fabricates a 0 or a guessed cost. See the module docstring for the honesty
    rationale."""
    rate = resolve_usd_per_node_hour(usd_per_node_hour, machine_type)
    if rate is None:
        return None
    if not _is_pos_number(node_count):
        return None
    if not _is_pos_number(ready_per_s):
        return None
    cluster_usd_per_hour = node_count * rate
    ready_per_hour = ready_per_s * _SECONDS_PER_HOUR
    return (cluster_usd_per_hour / ready_per_hour) * 1000.0
