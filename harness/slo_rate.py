"""Derive the matrix's per-mode SLO cluster rates from a step-up sweep — pure, no I/O.

The matrix cluster halves are defined (README footnote, locked in #145/#149) as an
SLO-GATED RATE: "the sustained creation rate at which p95 TTFE stays within the bar"
(Q1). A saturation fire measures completion throughput AT OVERLOAD (Q2) — a different
quantity that never fills these cells (#149). The honest producer of the Q1 quantity
is a per-activation-mode STEP-UP SWEEP: each Pareto rung holds an offered rate and
reports the measured delivered rate (`ready_per_s`) plus that rung's own `ttfe_p95_ms`.
The SLO rate for bar B is then read straight off the sweep:

    slo_rate(B) = max measured ready_per_s over rungs whose ttfe_p95_ms <= B

One sweep fills BOTH bars (the 5s-boundary rung and the 1s-boundary rung generally
differ), which is exactly why derivation-from-sweep beats a single boundary fire: a
fire pinned at one bar's boundary cannot honestly state the other bar's rate, but the
coupled emit triple carries both.

Honesty spine (mirrors the rest of the harness):

  - A rung is eligible for bar B only when BOTH `ttfe_p95_ms` and `ready_per_s` are
    present, finite, non-bool numbers with ready_per_s > 0. `offered_rate_per_s` is
    NEVER substituted for `ready_per_s` — offered is the load setting, ready is the
    measured delivered rate; publishing offered would print a knob as a measurement.
  - No compliant rung for a bar => that bar's key is OMITTED (the cell renders
    `pending (cluster-fire)`), NEVER a fabricated 0.0. A sweep that only probed rates
    above the bar's boundary proves nothing about the boundary itself — printing 0
    there would be the same overload-artifact trap #149 closed. The existing honest-0
    path (a measured baseline p95 that misses the bar) is untouched and lives in the
    emit leg, not here.
  - The two bars fill INDEPENDENTLY: render gates each cluster half per-cell
    (`cluster_key in metrics` + a landed node count), so a sweep whose lowest rung
    only clears the 5s bar honestly fills the 5s half while the 1s half keeps pending.
  - `thpt_cluster_node_count` is emitted only when >= 1 bar landed AND the sweep
    record carries a valid node_count (> 0). Without a node count the render cannot
    caption the X, so nothing is emitted at all (whole-result pend) — never a
    per-cluster figure with an undisclosed measurement size.
  - NEVER per-node x N: the rates here are the sweep's own measured cluster-wide
    delivered rates; no node divisor or multiplier ever touches them.
"""

from __future__ import annotations

from numbers import Real
from typing import Optional

from .metrics import THRESHOLD_1S_MS, THRESHOLD_5S_MS


def _finite_number(v) -> Optional[float]:
    """v as float if it is a finite, non-bool real number; else None."""
    if isinstance(v, bool) or not isinstance(v, Real):
        return None
    fv = float(v)
    if fv != fv or fv in (float("inf"), float("-inf")):
        return None
    return fv


def slo_cluster_rate(pareto_points, threshold_ms) -> Optional[float]:
    """Max measured ready_per_s among rungs whose ttfe_p95_ms <= threshold_ms.

    Returns None (the honest pend) when no eligible rung complies — including the
    nothing-measured shapes (non-list, empty, rungs missing ready_per_s). A rung with
    ready_per_s <= 0 is ineligible: a delivered rate of zero cannot carry a p95 of its
    own deliveries, so such a pair is a producer inconsistency, not a measurement.

    Non-monotonic sweeps are handled by construction: max() over ALL compliant rungs,
    not the last-compliant rung, so a p95 dip at a higher rate is still credited only
    if that rung itself complies.
    """
    if not isinstance(pareto_points, list):
        return None
    best: Optional[float] = None
    for pt in pareto_points:
        if not isinstance(pt, dict):
            continue
        p95 = _finite_number(pt.get("ttfe_p95_ms"))
        ready = _finite_number(pt.get("ready_per_s"))
        if p95 is None or ready is None or ready <= 0:
            continue
        if p95 <= threshold_ms and (best is None or ready > best):
            best = ready
    return best


def slo_sla_metrics_from_stepup(flat) -> dict:
    """Derive the hb#132 per-cluster emit triple from a FLAT step-up record.

    Input is the `stepup_nested_to_flat` shape: `pareto_points` (list of rung dicts)
    + `node_count`. Output is a dict ready to merge into that activation mode's
    scenario `sla_metrics`:

      - `thpt_under_5s_per_cluster` / `thpt_under_1s_per_cluster`: each present ONLY
        when its bar has a compliant rung (independent per-bar fill).
      - `thpt_cluster_node_count`: present whenever >= 1 bar landed.

    Empty dict ({}) when nothing is derivable — no valid node_count, or no bar with a
    compliant rung. Rounding matches `throughput_per_cluster` (3 decimals) so derived
    and directly-measured figures render identically.
    """
    if not isinstance(flat, dict):
        return {}
    node_count = flat.get("node_count")
    if isinstance(node_count, bool) or not isinstance(node_count, Real):
        return {}
    if float(node_count) != int(node_count) or int(node_count) < 1:
        return {}

    points = flat.get("pareto_points")
    out: dict = {}
    rate_5s = slo_cluster_rate(points, THRESHOLD_5S_MS)
    if rate_5s is not None:
        out["thpt_under_5s_per_cluster"] = round(rate_5s, 3)
    rate_1s = slo_cluster_rate(points, THRESHOLD_1S_MS)
    if rate_1s is not None:
        out["thpt_under_1s_per_cluster"] = round(rate_1s, 3)
    if out:
        out["thpt_cluster_node_count"] = int(node_count)
    return out
