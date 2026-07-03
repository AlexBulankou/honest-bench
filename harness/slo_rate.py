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
  - Literal-basis rungs (hb#174) additionally require `literal_warm_n_exec_ok` >=
    LITERAL_N_EXEC_OK_FLOOR (absent => ineligible, fail-closed): a p95 over a handful
    of exec samples must not prove SLO compliance. The credited rungs' MIN n is
    stamped as `thpt_slo_n_exec_ok` so render can caption coarse-p95 figures.
"""

from __future__ import annotations

from numbers import Real
from typing import Optional

from .metrics import THRESHOLD_1S_MS, THRESHOLD_5S_MS

# hb#174: which measured basis produced a derived cluster triple. A CLOSED enum —
# results_schema.SLO_BASIS_ENUM mirrors it (cross-contract test) so the stamp can ride
# sla_metrics through the numeric-only coercer via an explicit enum-gated carve-out.
#
#   - true_ttfe: the true-TTFE pareto (ttfe_p95_ms x ready_per_s) — the original basis.
#   - literal_ttfe_upper_bound+controller_completed: literal exec-probe warm p95 (UPPER
#     bound — includes exec websocket-setup overhead, so compliance at the bar is
#     conservative/honest) gated against the boundary-scrape controller completion rate
#     (count-delta / inter-scrape wall time; a measured cluster-wide delivered rate).
#   - literal_ttfe_upper_bound+acq_fulfilled: same latency gate against the acquisition
#     watch fulfilled-claims rate ((bound-edges + prebound) / duration; pending excluded
#     => a lower bound on delivered rate — still conservative for the SLO cell).
#
# Never mixed: one basis per triple. The literal bases fill ONLY when the true-TTFE
# pareto derives nothing for either bar (the #3975 dead-family gap), and
# controller_completed is preferred over acq_fulfilled (bound-edge != Ready ambiguity).
SLO_BASIS_TRUE_TTFE = "true_ttfe"
SLO_BASIS_LITERAL_CONTROLLER = "literal_ttfe_upper_bound+controller_completed"
SLO_BASIS_LITERAL_ACQ = "literal_ttfe_upper_bound+acq_fulfilled"
SLO_BASIS_ENUM = (
    SLO_BASIS_TRUE_TTFE,
    SLO_BASIS_LITERAL_CONTROLLER,
    SLO_BASIS_LITERAL_ACQ,
)

# hb#174 sign-off condition (c): a literal rung is SLO-eligible only when its warm
# exec-probe sample count clears this HARD floor. Below it, the rung proves nothing
# about a p95 (a 12-sample "p95" is the ~2nd-worst sample) => the bar stays pending.
# Rungs with 20 <= n < 100 still derive but render captions them "coarse p95 (n=X)"
# via the thpt_slo_n_exec_ok stamp. Absent n => ineligible (fail-closed; the producer
# always emits literal_warm_n_exec_ok, so absence means an unknown provenance).
LITERAL_N_EXEC_OK_FLOOR = 20


def _finite_number(v) -> Optional[float]:
    """v as float if it is a finite, non-bool real number; else None."""
    if isinstance(v, bool) or not isinstance(v, Real):
        return None
    fv = float(v)
    if fv != fv or fv in (float("inf"), float("-inf")):
        return None
    return fv


def _slo_rate(pareto_points, threshold_ms, p95_key, rate_key) -> Optional[float]:
    """Max measured rate among rungs whose latency p95 <= threshold_ms (keyed form).

    The generalized spine behind slo_cluster_rate: the same eligibility rules applied
    to a caller-chosen (latency-key, rate-key) pair, so the literal-TTFE upper-bound
    leg (hb#174) reuses the exact scan without aliasing its namespaced keys onto the
    true-TTFE names. `offered_rate_per_s` is NEVER a valid rate_key — offered is the
    load knob, not a measurement.
    """
    if not isinstance(pareto_points, list):
        return None
    best: Optional[float] = None
    for pt in pareto_points:
        if not isinstance(pt, dict):
            continue
        p95 = _finite_number(pt.get(p95_key))
        ready = _finite_number(pt.get(rate_key))
        if p95 is None or ready is None or ready <= 0:
            continue
        if p95 <= threshold_ms and (best is None or ready > best):
            best = ready
    return best


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
    return _slo_rate(pareto_points, threshold_ms, "ttfe_p95_ms", "ready_per_s")


def _derive_bars(pareto_points, p95_key, rate_key) -> dict:
    """Both bar keys derivable from one (points, latency-key, rate-key) basis.

    Per-bar independent fill, rounding matched to throughput_per_cluster (3 decimals).
    Empty dict when neither bar has a compliant rung.
    """
    out: dict = {}
    rate_5s = _slo_rate(pareto_points, THRESHOLD_5S_MS, p95_key, rate_key)
    if rate_5s is not None:
        out["thpt_under_5s_per_cluster"] = round(rate_5s, 3)
    rate_1s = _slo_rate(pareto_points, THRESHOLD_1S_MS, p95_key, rate_key)
    if rate_1s is not None:
        out["thpt_under_1s_per_cluster"] = round(rate_1s, 3)
    return out


def _valid_n_exec_ok(v) -> Optional[int]:
    """v as int if a finite, non-bool integral number >= LITERAL_N_EXEC_OK_FLOOR; else None."""
    fv = _finite_number(v)
    if fv is None or fv != int(fv):
        return None
    n = int(fv)
    return n if n >= LITERAL_N_EXEC_OK_FLOOR else None


def _literal_bar(pareto_points, threshold_ms, rate_key):
    """(best rate, that rung's n_exec_ok) among floor-eligible compliant literal rungs.

    The literal twin of _slo_rate with one extra eligibility gate (hb#174 sign-off
    condition c): the rung's `literal_warm_n_exec_ok` must be a non-bool integral
    number >= LITERAL_N_EXEC_OK_FLOOR. Absent or sub-floor n => the rung is ineligible
    (fail-closed) — a thin-sample p95 must not prove SLO compliance. Returns None when
    no eligible rung complies.
    """
    if not isinstance(pareto_points, list):
        return None
    best: Optional[tuple] = None
    for pt in pareto_points:
        if not isinstance(pt, dict):
            continue
        p95 = _finite_number(pt.get("literal_warm_p95_ms"))
        rate = _finite_number(pt.get(rate_key))
        n = _valid_n_exec_ok(pt.get("literal_warm_n_exec_ok"))
        if p95 is None or rate is None or rate <= 0 or n is None:
            continue
        if p95 <= threshold_ms and (best is None or rate > best[0]):
            best = (rate, n)
    return best


def _derive_literal_bars(pareto_points, rate_key) -> dict:
    """Literal-leg bar fill + the credited rungs' sample-size stamp.

    Same per-bar independent fill and rounding as _derive_bars, plus (hb#174 sign-off
    condition c) `thpt_slo_n_exec_ok`: the MIN warm-exec sample count across the rungs
    actually credited (one per landed bar) — the honest "weakest sample behind these
    figures" number render uses for the coarse-p95 caption (20 <= n < 100).
    """
    out: dict = {}
    ns: list = []
    for key, threshold_ms in (
        ("thpt_under_5s_per_cluster", THRESHOLD_5S_MS),
        ("thpt_under_1s_per_cluster", THRESHOLD_1S_MS),
    ):
        hit = _literal_bar(pareto_points, threshold_ms, rate_key)
        if hit is not None:
            out[key] = round(hit[0], 3)
            ns.append(hit[1])
    if out:
        out["thpt_slo_n_exec_ok"] = min(ns)
    return out


def slo_sla_metrics_from_stepup(flat) -> dict:
    """Derive the hb#132 per-cluster emit triple from a FLAT step-up record.

    Input is the `stepup_nested_to_flat` shape: `pareto_points` (list of rung dicts)
    + `node_count`, plus (hb#174) an optional `literal_ttfe` block. Output is a dict
    ready to merge into that activation mode's scenario `sla_metrics`:

      - `thpt_under_5s_per_cluster` / `thpt_under_1s_per_cluster`: each present ONLY
        when its bar has a compliant rung (independent per-bar fill).
      - `thpt_cluster_node_count`: present whenever >= 1 bar landed.
      - `thpt_slo_basis` (hb#174): which measured basis produced the triple — one of
        SLO_BASIS_ENUM, stamped whenever >= 1 bar landed.
      - `thpt_slo_n_exec_ok` (hb#174, literal bases only): MIN warm-exec sample count
        across the credited rungs — always >= LITERAL_N_EXEC_OK_FLOOR by construction
        (sub-floor rungs are ineligible, fail-closed); render captions coarse p95 when
        20 <= n < 100. Never present on a true-TTFE triple.

    Basis selection (hb#174) — ONE basis per triple, never mixed across bars:

      1. true-TTFE pareto (`ttfe_p95_ms` x `ready_per_s`) — always tried first; while
         the #3975 production stamp is unlanded this derives nothing by construction.
      2. literal-TTFE UPPER-bound leg (`literal_warm_p95_ms`, exec-probe: every sample
         carries exec websocket-setup overhead, so a rung compliant at the bar is
         conservatively/provably compliant) gated with `controller_completed_per_s`
         (boundary-scrape count-delta / inter-scrape wall span — the span includes
         inter-step overhead, so it under-reads the sustained rate: conservative).
      3. same latency gate with `acq_fulfilled_per_s` ((bound-edges + prebound) /
         step duration_s; pending excluded => lower bound; bound-edge != Ready).

    The two literal rate candidates are measured over NON-IDENTICAL windows (per-step
    duration_s vs inter-scrape wall span) — both err conservative, and the basis stamp
    discloses which one the cell used so render can caption it. The literal leg is
    consulted only when its block carries `upper_bound is True` (the polarity flag the
    producer sets; the controller-startup LOWER-bound proxy remains banned here — an
    under-reading latency basis could fabricate compliance). Each literal rung must
    additionally clear the LITERAL_N_EXEC_OK_FLOOR sample floor (`literal_warm_n_exec_ok`
    >= 20, absent => ineligible) — a thin-sample p95 must not prove compliance.

    Empty dict ({}) when nothing is derivable — no valid node_count, or no basis with a
    compliant rung on either bar. Rounding matches `throughput_per_cluster` (3
    decimals) so derived and directly-measured figures render identically.
    """
    if not isinstance(flat, dict):
        return {}
    node_count = flat.get("node_count")
    if isinstance(node_count, bool) or not isinstance(node_count, Real):
        return {}
    if float(node_count) != int(node_count) or int(node_count) < 1:
        return {}

    out = _derive_bars(flat.get("pareto_points"), "ttfe_p95_ms", "ready_per_s")
    basis = SLO_BASIS_TRUE_TTFE
    if not out:
        lt = flat.get("literal_ttfe")
        if isinstance(lt, dict) and lt.get("upper_bound") is True:
            lt_points = lt.get("pareto_points")
            out = _derive_literal_bars(lt_points, "controller_completed_per_s")
            basis = SLO_BASIS_LITERAL_CONTROLLER
            if not out:
                out = _derive_literal_bars(lt_points, "acq_fulfilled_per_s")
                basis = SLO_BASIS_LITERAL_ACQ
    if not out:
        return {}
    out["thpt_cluster_node_count"] = int(node_count)
    out["thpt_slo_basis"] = basis
    return out
