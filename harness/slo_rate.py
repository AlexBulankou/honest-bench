"""Derive the matrix's per-mode SLO cluster rates from a step-up sweep — pure, no I/O.

The matrix cluster halves are defined (README footnote, locked in #145/#149) as an
SLO-GATED RATE: "the sustained creation rate at which p95 TTFE stays within the bar"
(Q1). A saturation fire measures completion throughput AT OVERLOAD (Q2) — a different
quantity that never fills these cells (#149). The honest producer of the Q1 quantity
is a per-activation-mode STEP-UP SWEEP: each Pareto rung holds an offered rate and
reports the measured delivered rate (`ready_per_s`) plus that rung's own `ttfe_p95_ms`.
The SLO rate for bar B is then read straight off the sweep:

    slo_rate(B) = max measured ready_per_s over rungs whose ttfe_p95_ms <= B

One TRUE-TTFE sweep fills BOTH bars (the 5s-boundary rung and the 1s-boundary rung
generally differ), which is exactly why derivation-from-sweep beats a single boundary
fire: a fire pinned at one bar's boundary cannot honestly state the other bar's rate,
but the coupled emit triple carries both. The LITERAL upper-bound basis fills the 5s
bar ONLY (hb#174 sign-off amendment 1): every literal sample carries ~0.5-0.7s of
exec-probe websocket-setup overhead, which consumes 50-70% of the 1s budget and would
under-credit genuinely-compliant rungs to near-empty. Subtracting an overhead floor is
forbidden — it would break the cannot-over-credit guarantee — so under literal basis
the 1s cell stays HONEST-EMPTY by construction until the true-TTFE stamp lands.

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
    of exec samples must not prove SLO compliance. The credited rung's n is stamped
    as `thpt_slo_n_exec_ok` so render can caption coarse-p95 figures.
  - Literal-basis rungs are ALSO dual-leg agreement-gated (hb#174 sign-off amendment
    2): a rung is eligible only when BOTH `acq_fulfilled_per_s` AND
    `controller_completed_per_s` are present, finite, > 0, AND agree within
    LITERAL_RATE_AGREEMENT_TOL relative tolerance. The credited value is the
    acquisition rate (fulfilled claim->bound per s, steady-state); the controller
    completion rate is the independent trust cross-check. Divergence makes THAT RUNG
    ineligible (per-rung gating, not sweep-level): the deliberate above-knee overload
    rung legitimately diverges (flow conservation breaks at overload) and must not
    poison the compliant rungs below it.
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
#   - literal_ttfe_upper_bound+acq_fulfilled: literal exec-probe warm p95 (UPPER bound —
#     includes exec websocket-setup overhead, so compliance at the bar is
#     conservative/honest), crediting the acquisition-watch fulfilled-claims rate
#     (fulfilled claim->bound per s, steady-state; pending excluded => a lower bound on
#     the delivered rate), TRUST-GATED per-rung against `controller_completed_per_s`
#     agreement (hb#174 sign-off amendment 2). Fills the 5s cell ONLY (amendment 1).
#   - literal_ttfe_upper_bound+controller_completed: RETAINED for already-stamped
#     records' schema compat — no longer produced (superseded by the amendment-2
#     dual-leg gate above, which requires both legs and credits acq).
#
# Never mixed: one basis per triple. The literal basis fills ONLY when the true-TTFE
# pareto derives nothing for either bar (the #3975 dead-family gap).
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

# hb#174 sign-off amendment 2: the literal cell is trusted only when the two
# independent rate legs — acquisition fulfilled-claims (per-step duration window) and
# controller completion (inter-scrape boundary window) — agree per-rung within this
# RELATIVE tolerance: |acq - ctrl| / max(acq, ctrl) <= TOL. Steady-state flow
# conservation says the two must converge in a sustained hold; divergence means the
# rung was not steady-state (overload, backlog drain, scrape skew) and its rate cannot
# be credited. First-cut value 0.10 — disclosed in the PR for a4s2 adjudication.
LITERAL_RATE_AGREEMENT_TOL = 0.10


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


def _derive_literal_5s(pareto_points) -> dict:
    """5s-bar-ONLY literal fill: dual-leg agreement-gated, acq-credited (hb#174 amds).

    Amendment 1: the literal upper-bound basis may fill `thpt_under_5s_per_cluster`
    ONLY — the 1s cell stays honest-empty (exec-probe overhead eats the 1s budget; no
    overhead-floor subtraction, it would break cannot-over-credit).

    Amendment 2 (per-rung eligibility, ALL required, fail-closed on absence):
      - `literal_warm_p95_ms` finite and <= THRESHOLD_5S_MS,
      - `literal_warm_n_exec_ok` >= LITERAL_N_EXEC_OK_FLOOR (thin-sample p95 proves
        nothing),
      - BOTH `acq_fulfilled_per_s` AND `controller_completed_per_s` finite and > 0,
      - the two legs agree: |acq - ctrl| / max(acq, ctrl) <=
        LITERAL_RATE_AGREEMENT_TOL (steady-state flow-conservation trust check; the
        deliberate above-knee overload rung legitimately diverges and simply drops
        out — per-rung gating, never sweep-level poison).

    The credited value is the ACQUISITION rate (fulfilled claim->bound per s,
    steady-state) of the best eligible rung; that rung's n lands as
    `thpt_slo_n_exec_ok` for render's coarse-p95 caption. Empty dict when no rung
    qualifies (the bar renders pending — never 0).
    """
    if not isinstance(pareto_points, list):
        return {}
    best: Optional[tuple] = None
    for pt in pareto_points:
        if not isinstance(pt, dict):
            continue
        p95 = _finite_number(pt.get("literal_warm_p95_ms"))
        n = _valid_n_exec_ok(pt.get("literal_warm_n_exec_ok"))
        acq = _finite_number(pt.get("acq_fulfilled_per_s"))
        ctrl = _finite_number(pt.get("controller_completed_per_s"))
        if p95 is None or n is None or acq is None or ctrl is None:
            continue
        if acq <= 0 or ctrl <= 0:
            continue
        if abs(acq - ctrl) / max(acq, ctrl) > LITERAL_RATE_AGREEMENT_TOL:
            continue
        if p95 <= THRESHOLD_5S_MS and (best is None or acq > best[0]):
            best = (acq, n)
    if best is None:
        return {}
    return {
        "thpt_under_5s_per_cluster": round(best[0], 3),
        "thpt_slo_n_exec_ok": best[1],
    }


def slo_sla_metrics_from_stepup(flat) -> dict:
    """Derive the hb#132 per-cluster emit triple from a FLAT step-up record.

    Input is the `stepup_nested_to_flat` shape: `pareto_points` (list of rung dicts)
    + `node_count`, plus (hb#174) an optional `literal_ttfe` block. Output is a dict
    ready to merge into that activation mode's scenario `sla_metrics`:

      - `thpt_under_5s_per_cluster` / `thpt_under_1s_per_cluster`: each present ONLY
        when its bar has a compliant rung. True-TTFE basis fills the bars
        independently; the literal basis may fill the 5s bar ONLY (hb#174 sign-off
        amendment 1 — the 1s cell stays honest-empty under literal basis).
      - `thpt_cluster_node_count`: present whenever >= 1 bar landed.
      - `thpt_slo_basis` (hb#174): which measured basis produced the triple — one of
        SLO_BASIS_ENUM, stamped whenever >= 1 bar landed.
      - `thpt_slo_n_exec_ok` (hb#174, literal basis only): the credited rung's
        warm-exec sample count — always >= LITERAL_N_EXEC_OK_FLOOR by construction
        (sub-floor rungs are ineligible, fail-closed); render captions coarse p95 when
        20 <= n < 100. Never present on a true-TTFE triple.

    Basis selection (hb#174, amended by the sign-off) — ONE basis per triple:

      1. true-TTFE pareto (`ttfe_p95_ms` x `ready_per_s`) — always tried first; while
         the #3975 production stamp is unlanded this derives nothing by construction.
      2. literal-TTFE UPPER-bound leg (`literal_warm_p95_ms`, exec-probe: every sample
         carries exec websocket-setup overhead, so a rung compliant at the bar is
         conservatively/provably compliant), crediting `acq_fulfilled_per_s`
         (fulfilled claim->bound per s, steady-state; pending excluded => lower
         bound) with `controller_completed_per_s` as the per-rung agreement
         cross-check (amendment 2; see _derive_literal_5s). 5s cell only.

    The two literal rate legs are measured over NON-IDENTICAL windows (per-step
    duration_s vs inter-scrape wall span) — steady-state flow conservation makes them
    converge in a sustained hold, which is exactly why their agreement is the trust
    gate: a rung where they diverge was not steady-state and is ineligible. The
    literal leg is consulted only when its block carries `upper_bound is True` (the
    polarity flag the producer sets; the controller-startup LOWER-bound proxy remains
    banned here — an under-reading latency basis could fabricate compliance). Each
    literal rung must additionally clear the LITERAL_N_EXEC_OK_FLOOR sample floor
    (`literal_warm_n_exec_ok` >= 20, absent => ineligible) — a thin-sample p95 must
    not prove compliance. The SLO_BASIS_LITERAL_CONTROLLER enum value is retained for
    already-stamped records but is no longer produced.

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
            out = _derive_literal_5s(lt.get("pareto_points"))
            basis = SLO_BASIS_LITERAL_ACQ
    if not out:
        return {}
    out["thpt_cluster_node_count"] = int(node_count)
    out["thpt_slo_basis"] = basis
    return out
