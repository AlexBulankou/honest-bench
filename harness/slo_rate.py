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
# hb#214 part 1: the pre-declared floor-rate honest-ZERO basis. Produced only by
# _derive_literal_floor_zero_5s (see its docstring for the full predicate); always
# paired with thpt_slo_floor_zero=1 — a 0.0 rate without that stamp is schema-invalid.
SLO_BASIS_LITERAL_FLOOR_ZERO = "literal_ttfe_upper_bound+floor_zero_margin"
# hb#230 (alex doctrine flip, 2026-07-08): the UNCORROBORATED acq-side basis. Gated on
# the acquisition p95 (acq_p95_s, the clean sub-second signal), crediting
# acq_fulfilled_per_s, with the controller-agreement cross-check DROPPED — so it is
# single-source. Consulted AFTER the corroborated bases derive nothing (the every_n=1
# exec-probe contention that garbages literal_warm_p95_ms leaves acq~ctrl divergent, so
# the dual-leg gate drops every rung). Fills BOTH bars (the acq p95 is sub-second at
# every gVisor warm rung, so the 1s cell is no longer honest-empty-by-construction).
# render marks it with the Class A *** caveat (upstream #940 -> fix #1087). A caveated
# measured number beats an honest-empty cell — publish best measured, not withheld.
SLO_BASIS_ACQ_P95_UNCORROBORATED = "acq_fulfilled+acq_p95_uncorroborated"
# hb#230 Fork 4 (alex doctrine flip, 2026-07-08): the COLD-START honest-ZERO basis.
# The cold-start controller floor exceeds BOTH bars at every offered rate, so the
# compliant rate is an honest 0 at both bars (rate-independent) — NOT a Class-B
# upper-bound-pending. Produced only by _derive_cold_floor_zero (see its docstring for
# the full two-signal predicate); always paired with thpt_slo_floor_zero=1. Distinct
# from SLO_BASIS_LITERAL_FLOOR_ZERO (the warm 5s-only floor-zero over exec-probe
# samples) — this one is over the controller cold-start distribution and fills BOTH
# bars, with a trusted-rung corroboration requirement. render marks it with the Fork-4
# caveat (cold-start floor ~p50 controller-measured; acq clean; upstream #751 -> #761).
SLO_BASIS_COLD_FLOOR_ZERO = "controller_cold_floor_zero_corroborated"
# hb#230 (a4s1 Kata-cold ruling, 2026-07-08): the HONEST-UNKNOWN basis — no number,
# either direction. The symmetry rule the whole doctrine rests on: a NEGATIVE claim (an
# honest 0) requires the controller LOWER bound to breach the bar by margin; a POSITIVE
# claim (a compliant rate) requires the literal exec-probe UPPER bound to clear it. When
# the bar falls INSIDE the proven [lower, upper] TTFE bracket, NEITHER claim is
# supportable — the cell is genuinely unresolved despite measurement (e.g. Kata cold at
# the 5s bar: ctrl-cold p50 ~2.3s < 7.5s so no honest-0, literal p95 ~8.4s > 5s so no
# positive claim, 5s bar bracketed). This basis carries NO per-cluster/per-node figure;
# render emits `unk.` + the *** footnote (bracket disclosed there). It is DISTINCT from
# every measured basis AND from the floor-zero bases, so the fail-closed mixed-basis
# aggregation guard excludes it — an unresolved cell contributes no number to any
# caption. NOT a `pending` (which implies a future measurement); the measurement WAS
# taken and the bar is provably unresolvable at the offered rungs.
SLO_BASIS_UNRESOLVED_BOUNDS = "unresolved_bounds_bar_bracketed"
SLO_BASIS_ENUM = (
    SLO_BASIS_TRUE_TTFE,
    SLO_BASIS_LITERAL_CONTROLLER,
    SLO_BASIS_LITERAL_ACQ,
    SLO_BASIS_LITERAL_FLOOR_ZERO,
    SLO_BASIS_ACQ_P95_UNCORROBORATED,
    SLO_BASIS_COLD_FLOOR_ZERO,
    SLO_BASIS_UNRESOLVED_BOUNDS,
)

# hb#174 sign-off condition (c): a literal rung is SLO-eligible only when its warm
# exec-probe sample count clears this HARD floor. Below it, the rung proves nothing
# about a p95 (a 12-sample "p95" is the ~2nd-worst sample) => the bar stays pending.
# Rungs with 20 <= n < 100 still derive but render captions them "coarse p95 (n=X)"
# via the thpt_slo_n_exec_ok stamp. Absent n => ineligible (fail-closed; the producer
# always emits literal_warm_n_exec_ok, so absence means an unknown provenance).
LITERAL_N_EXEC_OK_FLOOR = 20

# hb#174 sign-off amendment 2: the literal cell is trusted only when the two
# independent rate legs — acquisition fulfilled-claims (measured fulfillment
# window, a#4279) and controller completion (inter-scrape boundary window) —
# agree per-rung within this RELATIVE tolerance: |acq - ctrl| / max(acq, ctrl)
# <= TOL. The gate is a SUSTAINABILITY discriminator: acq ~= ctrl means the
# rung is refill-limited steady state (real throughput); acq >> ctrl means the
# rung is draining a warm buffer (transient, non-sustainable), so dropping it
# is correct. TOL bounds how far acq may exceed the refill rate before the
# throughput is deemed non-sustainable. 0.10 is UNVALIDATED against a real
# passing rung — revisit once a low-rate fire (at/below controller refill
# rate) produces one.
LITERAL_RATE_AGREEMENT_TOL = 0.10

# hb#214 part 1 — floor-rate honest-ZERO margin (PINNED at the hb#214 weigh-in).
# Derivation note: 1.5 is chosen to dominate the max observed literal/true basis
# ratio plus noise headroom (exec setup overhead + probe contention, quantified
# below); revisit only via a data-cited PR, never by judgment.
# The honest-zero predicate is a
# strong NEGATIVE claim, and the literal basis is an UPPER bound on true TTFE — an
# upper-bound sample exceeding the bar does NOT by itself prove the true latency
# exceeds it (the polarity that makes the basis conservative for POSITIVE claims
# inverts for negative ones). The margin restores conservatism: the producer counts a
# known warm sample as over-bar ONLY when its literal latency exceeds
# THRESHOLD_5S_MS * HONEST_ZERO_BAR_MARGIN (7.5s at 1.5), which absorbs the
# ~0.5-0.7s exec websocket-setup overhead plus every_n=1 probe-contention inflation
# with room to spare. 1.5 is chosen so the known candidate (Kata cold, p95 ~8.3-8.4s
# at every rung, ~66% over the bar at p50) still trips while any rung whose overage
# could plausibly be overhead/contention artifact cannot. Pre-declared here — the
# ONE shared place — so the producer's count and this derive can never disagree on
# what "over the bar" means; tuning it post-hoc to make a zero fire (or not fire)
# is forbidden, same as bars and TOL.
HONEST_ZERO_BAR_MARGIN = 1.5

# hb#214 part 1 — evaluability cap on the unknown-sentinel fraction (pinned in
# hb#214 discussion): if more than half the floor rung's samples are unknown-stamped
# (upstream #1087 sentinels), the adversarial-fill predicate is arithmetically
# un-trippable (over-bar knowns can never form a strict majority), so the cell stays
# pending under the existing closed-enum reason — no new enum value.
HONEST_ZERO_MAX_UNKNOWN_FRACTION = 0.5


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


def _valid_count(v) -> Optional[int]:
    """v as int if a finite, non-bool integral number >= 0; else None."""
    fv = _finite_number(v)
    if fv is None or fv != int(fv):
        return None
    n = int(fv)
    return n if n >= 0 else None


def _derive_literal_floor_zero_5s(pareto_points) -> dict:
    """hb#214 part 1 (DRAFT): the pre-declared floor-rate honest-ZERO predicate, 5s bar.

    The one case where a measured 0 is honest (per hb#214): the sweep's FLOOR rung —
    the lowest offered rate probed — was fully measured and trusted, and its latency
    distribution fails the bar so decisively that no lower rate could pass either.
    Closed predicate over the record, decided BEFORE any fire it applies to; never a
    post-hoc judgment call. Everything below must hold or the cell stays pending:

      1. FLOOR RUNG ONLY: the rung with the minimum finite `offered_rate_per_s` > 0.
         A mid-ladder rung failing the bar proves nothing about lower rates.
      2. STEADY-STATE TRUST (same eligibility spine as the positive literal path,
         minus bar compliance): `literal_warm_n_exec_ok` >= LITERAL_N_EXEC_OK_FLOOR,
         BOTH rate legs finite > 0, agreement <= LITERAL_RATE_AGREEMENT_TOL. A zero
         asserted from an untrusted rung would be the fabricated-0 trap in negative
         polarity.
      3. COUNT FIELDS PRESENT (fail-closed): `literal_warm_n_over_bar_5s` (known warm
         samples with literal latency > THRESHOLD_5S_MS * HONEST_ZERO_BAR_MARGIN —
         producer-computed against the shared margin constant) AND
         `literal_warm_n_unknown` (unknown-sentinel-stamped samples; 0 until upstream
         #1087 lands). Absence of either => ineligible: a producer that predates the
         count contract cannot prove the distribution, so no zero.
      4. PRODUCER CONSISTENCY: n_over_bar <= n_exec_ok (an over-bar count exceeding
         the known-sample count is an inconsistency, not a measurement).
      5. EVALUABILITY CAP: n_unknown / n_total <= HONEST_ZERO_MAX_UNKNOWN_FRACTION
         (n_total = n_exec_ok + n_unknown). Beyond it the adversarial fill below is
         arithmetically un-trippable; explicit early-out for clarity.
      6. ADVERSARIAL FILL (the pinned hb#214 unknown semantics for NEGATIVE claims):
         the predicate fires only when n_over_bar > 0.5 * n_total — i.e. even
         granting EVERY unknown sample a pass, a strict majority of all samples
         exceed the margined bar, so the full-population p50 is over the bar. Plain
         exclusion of unknowns is NOT safe here: if fast samples disproportionately
         land unknown, the known-subset p50 overstates and would trip the 0 too
         easily. Adversarial fill closes that hole with no distributional assumption.

    On fire: {"thpt_under_5s_per_cluster": 0.0, "thpt_under_5s_per_node": 0.0,
    "thpt_slo_floor_zero": 1, "thpt_slo_n_exec_ok": n}. The per-node leg rides
    along because exactly-0 is the one case where the two denominators are
    interchangeable (a cluster rate of exactly 0 forces the per-node rate to 0 —
    no extrapolation), and the renderer's dual cell keys on the per-node leg: a
    cluster-only emit would be swallowed by the node-absent arm (a stamp with no
    disclosed figure). The stamp is numeric 1 (not True) so it rides the
    numeric-only sla_metrics coercer without a new carve-out; schema-side pairing
    validation rejects a 0.0 rate without the stamp AND a stamp without BOTH 5s
    legs at 0.0 — a bare zero can never publish. Empty dict when any condition
    fails (pending).
    """
    if not isinstance(pareto_points, list):
        return {}
    floor_pt: Optional[dict] = None
    floor_rate: Optional[float] = None
    for pt in pareto_points:
        if not isinstance(pt, dict):
            continue
        rate = _finite_number(pt.get("offered_rate_per_s"))
        if rate is None or rate <= 0:
            continue
        if floor_rate is None or rate < floor_rate:
            floor_rate = rate
            floor_pt = pt
    if floor_pt is None:
        return {}
    n = _valid_n_exec_ok(floor_pt.get("literal_warm_n_exec_ok"))
    acq = _finite_number(floor_pt.get("acq_fulfilled_per_s"))
    ctrl = _finite_number(floor_pt.get("controller_completed_per_s"))
    if n is None or acq is None or ctrl is None or acq <= 0 or ctrl <= 0:
        return {}
    if abs(acq - ctrl) / max(acq, ctrl) > LITERAL_RATE_AGREEMENT_TOL:
        return {}
    n_over = _valid_count(floor_pt.get("literal_warm_n_over_bar_5s"))
    n_unknown = _valid_count(floor_pt.get("literal_warm_n_unknown"))
    if n_over is None or n_unknown is None:
        return {}
    if n_over > n:
        return {}
    n_total = n + n_unknown
    if n_unknown > HONEST_ZERO_MAX_UNKNOWN_FRACTION * n_total:
        return {}
    if not (n_over > 0.5 * n_total):
        return {}
    return {
        "thpt_under_5s_per_cluster": 0.0,
        "thpt_under_5s_per_node": 0.0,
        "thpt_slo_floor_zero": 1,
        "thpt_slo_n_exec_ok": n,
    }


def _derive_acq_p95_uncorroborated(pareto_points) -> dict:
    """Both-bar UNCORROBORATED acq-side fill: gated on acq_p95_s, NO controller cross-check.

    hb#230 doctrine flip (alex, 2026-07-08): where the corroborated basis derives
    nothing — the dual-leg agreement gate drops every rung because the every_n=1
    exec-probe contention that garbages `literal_warm_p95_ms` (34-58s) leaves the two
    rate legs acq~ctrl divergent — publish the best MEASURED acquisition rate rather
    than an honest-empty cell. This basis:

      - credits `acq_fulfilled_per_s` (fulfilled claim->bound per s, steady-state), the
        SAME quantity the corroborated literal_acq basis credits;
      - gates on the ACQUISITION p95 `acq_p95_s` (SECONDS; converted to ms against the
        shared THRESHOLD_*_MS bars) rather than the literal-TTFE exec-probe p95 — the
        acq p95 is the clean sub-second signal, the literal-TTFE p95 is the garbage;
      - DROPS the controller-agreement cross-check (that gate becoming a caveat IS the
        doctrine flip) — so the result is single-source, uncorroborated. render marks
        it with the Class A *** caveat (upstream #940 -> fix #1087).

    Fills BOTH bars independently (unlike the 5s-only corroborated literal_acq): the acq
    p95 is sub-second at every gVisor warm rung, so the 1s cell is no longer
    honest-empty-by-construction (that construction was about literal-TTFE exec overhead
    eating the 1s budget, which the acq basis sidesteps). Each bar credits the max
    `acq_fulfilled_per_s` among rungs whose `acq_p95_s` clears that bar. `offered_rate_per_s`
    is NEVER credited — offered is the load knob, not a measurement. Empty dict when no
    rung clears either bar (the cell stays pending — never fabricated).
    """
    if not isinstance(pareto_points, list):
        return {}
    out: dict = {}
    for bar_ms, cluster_key in (
        (THRESHOLD_5S_MS, "thpt_under_5s_per_cluster"),
        (THRESHOLD_1S_MS, "thpt_under_1s_per_cluster"),
    ):
        best: Optional[float] = None
        for pt in pareto_points:
            if not isinstance(pt, dict):
                continue
            acq = _finite_number(pt.get("acq_fulfilled_per_s"))
            p95_s = _finite_number(pt.get("acq_p95_s"))
            if acq is None or p95_s is None or acq <= 0 or p95_s < 0:
                continue
            if p95_s * 1000.0 <= bar_ms and (best is None or acq > best):
                best = acq
        if best is not None:
            out[cluster_key] = round(best, 3)
    return out


def _cold_p50_ms(pt) -> Optional[float]:
    """The controller cold-start p50 (ms) from a flat per-rung cold record, or None.

    Reads `controller_startup_cold_ms.p50`. Absent block / non-dict / non-finite p50
    => None (the rung carries no usable cold-floor signal).
    """
    if not isinstance(pt, dict):
        return None
    csc = pt.get("controller_startup_cold_ms")
    if not isinstance(csc, dict):
        return None
    return _finite_number(csc.get("p50"))


def _derive_cold_floor_zero(records, node_count) -> dict:
    """hb#230 Fork 4: the COLD-START honest-ZERO predicate — both bars, corroborated.

    The cold-start case (per Fork 4, a4s1-ruled 2026-07-08): the controller cold-start
    floor is so far over the bar that NO offered rate could bring a compliant fraction
    under either bar — the compliant rate is an honest 0 at BOTH bars, rate-independent.
    This is the honest-ZERO polarity (a strong NEGATIVE claim), so — like the warm
    floor-zero — it must clear a closed, pre-declared predicate, never a post-hoc call.

    Input is a LIST of FLAT per-rung cold records (one dict per offered rate; the
    permode-legB cold shape), NOT the pareto_points-bearing flat that
    slo_sla_metrics_from_stepup consumes. Each rung carries `rate_per_s`,
    `controller_measured` (bool), and `controller_startup_cold_ms.{p50,...}`.

    TWO-SIGNAL predicate — BOTH required (a4s1 ruling, verbatim intent):

      (a) FLOOR RUNG over BOTH margined bars: the rung with the minimum finite
          `rate_per_s` > 0 has a finite `controller_startup_cold_ms.p50` > 0 that
          exceeds BOTH THRESHOLD_5S_MS * HONEST_ZERO_BAR_MARGIN AND
          THRESHOLD_1S_MS * HONEST_ZERO_BAR_MARGIN. The floor rung establishes "no
          lower rate could pass". But the floor rung MAY be controller-untrusted
          (`controller_measured` False) — on its own it is a latency proxy, not proof,
          so signal (a) alone must NOT assert an honest 0.

      (b) TRUSTED CORROBORATOR: >= 1 rung in the SAME set with `controller_measured`
          is True whose cold p50 is ALSO finite > 0 and over BOTH margined bars. A
          trusted rung confirms the cold-start floor is real, not an artifact of the
          untrusted floor rung's measurement. If NO trusted rung clears both bars, the
          cell stays UNKNOWN (empty dict) — an untrusted floor alone never fabricates a
          zero (the negative-polarity fabricated-0 trap).

    The margin (HONEST_ZERO_BAR_MARGIN, 1.5x) is the SAME shared conservatism the warm
    floor-zero uses: a cold p50 counts as over-bar only when it clears the margined bar,
    so no plausible overhead/noise artifact can trip the zero.

    On fire: honest 0 at BOTH bars —
      {"thpt_under_5s_per_cluster": 0.0, "thpt_under_5s_per_node": 0.0,
       "thpt_under_1s_per_cluster": 0.0, "thpt_under_1s_per_node": 0.0,
       "thpt_slo_floor_zero": 1, "thpt_slo_basis": SLO_BASIS_COLD_FLOOR_ZERO,
       "thpt_cluster_node_count": <n>}.
    Both per-node legs ride along because exactly-0 makes the two denominators
    interchangeable (the renderer's dual cell keys on the per-node leg), and BOTH bars
    are zeroed because a cold floor over the 5s bar is a fortiori over the 1s bar. The
    floor_zero stamp + basis are stamped here (this is a distinct entry point, not
    routed through slo_sla_metrics_from_stepup), so the caller merges the dict as-is.
    The controller-untrusted-floor / trusted-corroborator disclosure lives in the
    render *** footnote (the cell shows 0 /node · 0 /cluster ***). Empty dict when
    either signal fails (the cell stays pending — never a fabricated zero).
    """
    if not isinstance(records, list):
        return {}
    if isinstance(node_count, bool) or not isinstance(node_count, Real):
        return {}
    nc = float(node_count)
    if nc != nc or nc in (float("inf"), float("-inf")):  # NaN / inf
        return {}
    if nc != int(nc) or int(nc) < 1:
        return {}
    bar5 = THRESHOLD_5S_MS * HONEST_ZERO_BAR_MARGIN
    bar1 = THRESHOLD_1S_MS * HONEST_ZERO_BAR_MARGIN
    # Signal (a): locate the floor rung (min positive rate_per_s) and gate its cold p50.
    floor_pt: Optional[dict] = None
    floor_rate: Optional[float] = None
    for pt in records:
        if not isinstance(pt, dict):
            continue
        rate = _finite_number(pt.get("rate_per_s"))
        if rate is None or rate <= 0:
            continue
        if floor_rate is None or rate < floor_rate:
            floor_rate = rate
            floor_pt = pt
    if floor_pt is None:
        return {}
    floor_p50 = _cold_p50_ms(floor_pt)
    if floor_p50 is None or floor_p50 <= 0:
        return {}
    if not (floor_p50 > bar5 and floor_p50 > bar1):
        return {}
    # Signal (b): >= 1 controller_measured=True rung whose cold p50 also clears both bars.
    corroborated = False
    for pt in records:
        if not isinstance(pt, dict):
            continue
        if pt.get("controller_measured") is not True:
            continue
        p50 = _cold_p50_ms(pt)
        if p50 is None or p50 <= 0:
            continue
        if p50 > bar5 and p50 > bar1:
            corroborated = True
            break
    if not corroborated:
        return {}
    return {
        "thpt_under_5s_per_cluster": 0.0,
        "thpt_under_5s_per_node": 0.0,
        "thpt_under_1s_per_cluster": 0.0,
        "thpt_under_1s_per_node": 0.0,
        "thpt_slo_floor_zero": 1,
        "thpt_slo_basis": SLO_BASIS_COLD_FLOOR_ZERO,
        "thpt_cluster_node_count": int(node_count),
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
                # hb#230 (alex doctrine flip, 2026-07-08): the corroborated dual-leg
                # gate (_derive_literal_5s) dropped every rung -> fall through to the
                # UNCORROBORATED acq-side basis (gated on acq_p95_s, controller
                # cross-check dropped) before conceding a zero. A positive measured
                # rate, even single-source, always outranks a floor-zero.
                out = _derive_acq_p95_uncorroborated(lt.get("pareto_points"))
                basis = SLO_BASIS_ACQ_P95_UNCORROBORATED
                if not out:
                    # hb#214 part 1 (DRAFT): only after ALL positive bases derived
                    # nothing may the floor-zero predicate be consulted — a positive
                    # rate anywhere always outranks a zero.
                    out = _derive_literal_floor_zero_5s(lt.get("pareto_points"))
                    basis = SLO_BASIS_LITERAL_FLOOR_ZERO
    if not out:
        return {}
    out["thpt_cluster_node_count"] = int(node_count)
    out["thpt_slo_basis"] = basis
    return out
