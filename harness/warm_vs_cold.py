"""Warm-vs-cold speedup axis: how many times faster is a warm-pool provision than cold?

This producer answers the page's "warm-hit vs cold matrix" question with ONE honest,
repeatable number a reader can quote: **warm provisioning is N times faster than cold.**
Today the page shows warm (the burst TTFI histogram / activation_ms) and cold
(native_digest_cold's cold_start_ms) as two unrelated cells; the reader has to do the
division themselves and has no guard that the two numbers are even commensurable
(same measurement semantic? same runtime class?). This module makes the comparison
explicit AND honest-by-construction.

    Inputs : warm leg  -- per-claim warm-pool latencies (one semantic), and
             cold leg  -- a single cold-path latency sample (n=1 cold path).
    Output : warm_p50_ms = percentile(warm_samples_ms, 50)
             cold_ms     = the cold sample
             speedup     = cold_ms / warm_p50_ms  (so "warm is N times faster")
    Why    : a single cold-start number says nothing about what the warm pool BUYS
             you. The speedup ratio is the portable headline: a stranger reruns both
             legs on their own cluster and gets the same shape.

## Framing A -- warm vs TRUE-cold (locked)

There are three latency tiers in play, not two: warm (a ready warm-pool slot),
overflow (a claim that arrived after the warm slots were consumed -- still a warm
controller + node-cached image), and true-cold (native_digest_cold: pull=Always, no
warm pool, fresh node path). The published headline is **warm vs true-cold** -- the
honest "what does the warm pool buy you vs not having one" number. Overflow-vs-warm
is a weaker headline (both tiers share a warm controller) and is reserved as a FUTURE
ADDITIVE field (see `overflow_separation` note below) -- it is NOT built here.

## Emit contract -- TOP-LEVEL warm_vs_cold object (mirrors scale_proof)

The render side reads a TOP-LEVEL `warm_vs_cold` object (an analogue of `scale_proof`).
This classifier returns the INNER object directly:

    {"warm_p50_ms": float, "cold_ms": float, "speedup": float,
     "semantic": "ttfi"|"ttfe", "runtime_class": str, "n_warm": int}

The `warm_vs_cold` key is added later by results_schema.build_results(warm_vs_cold=...)
-- exactly as scale_slope returns the inner object and the harness wraps it. (This
diverges from the design doc's literally-wrapped `{"warm_vs_cold": {...}}` illustration;
the inner-object convention matches scale_slope's live shape so run.py composes both
the same way.)

## emit-only-when-complete -- the honesty crux (measurement-semantic parity, design S6)

A warm-vs-cold ratio is only meaningful when both legs were measured the SAME way.
This classifier returns {} (object absent -> cell renders pending, NEVER a fabricated
number) on ANY of:

  - warm samples empty / missing,
  - cold sample missing (None) or non-finite or <= 0,
  - any warm sample non-finite / negative (a corrupt measurement, not a pending leg),
  - warm_semantic != cold_semantic  (TTFI-warm vs TTFE-cold is apples-to-oranges),
  - the shared semantic is not one of {"ttfi", "ttfe"},
  - warm_runtime_class != cold_runtime_class  (gVisor-warm vs runc-cold is not the claim),
  - the shared runtime_class is falsy (unknown class -> the comparison's basis is undefined),
  - warm_p50 <= 0 after rounding (degenerate; would make the ratio undefined).

The fire path sets BENCH_TTFE_EXEC consistently across BOTH the warm
leg (burst_create) and the cold leg (native_digest_cold) for the same fire; this
classifier is the BACKSTOP that refuses to publish if the two legs ever diverge.

## Pure -- no cluster, no clock, no new math

`classify_warm_vs_cold` is pure and delegates every number to the LOCKED metrics.py
functions (percentile + retention) -- it adds NO arithmetic primitive. The speedup
ratio reuses `metrics.retention(base, max) = round(max/base, 3)`: with base=warm_p50
and max=cold_ms the ratio is cold/warm, i.e. "warm is N times faster". It is computed
from the DISPLAYED rounded warm_p50/cold_ms so the published page is self-consistent
(the reader can re-derive speedup from the two cells and get the same value).

## overflow_separation -- FUTURE ADDITIVE field (NOT built here)

warmpool_cold_start already computes a warm-vs-overflow `separation_observed` per fire
and discards it. A future change may surface it as an OPTIONAL secondary subline on
this same object -- `{"overflow_separation": float}` -- exactly the way scale_proof
carries optional sublines. It is deliberately NOT implemented now (framing A is warm
vs true-cold); when added it is purely additive and never changes the headline speedup.
"""

from __future__ import annotations

try:  # package context (production: harness.warm_vs_cold)
    from . import metrics
except ImportError:  # standalone (dependency-free test from the harness/ dir)
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
    import metrics  # type: ignore

import math
from numbers import Real
from typing import Optional, Sequence

_VALID_SEMANTICS = ("ttfi", "ttfe")


def _all_finite_nonneg(samples) -> bool:
    """True iff every sample is a present, finite, non-negative real number.

    A corrupt warm leg (None / NaN / inf / negative latency) is a measurement
    defect, not a pending state -- the classifier emits {} rather than fabricating
    a p50 from partial or impossible data, and never raises into a live fire.
    """
    for s in samples:
        if s is None or isinstance(s, bool) or not isinstance(s, Real):
            return False
        f = float(s)
        if not math.isfinite(f) or f < 0:
            return False
    return True


def classify_warm_vs_cold(
    warm_samples_ms: Sequence[Real],
    cold_sample_ms: Optional[Real],
    *,
    warm_semantic: str,
    cold_semantic: str,
    warm_runtime_class: Optional[str],
    cold_runtime_class: Optional[str],
) -> dict:
    """Pure warm-vs-cold speedup classifier. No cluster, no clock -- unit-testable.

    Returns the INNER warm_vs_cold object (the harness wraps it under the
    `warm_vs_cold` key via build_results), or {} when any honesty gate fails. See the
    module docstring for the full emit-only-when-complete contract.
    """
    # --- semantic-parity backstop (design S6): both legs must be the SAME mode ---
    if warm_semantic != cold_semantic:
        return {}
    semantic = warm_semantic
    if semantic not in _VALID_SEMANTICS:
        return {}

    # --- runtime-class parity: gVisor-warm vs runc-cold is not the claim ---
    if warm_runtime_class != cold_runtime_class:
        return {}
    runtime_class = warm_runtime_class
    if not runtime_class:  # None or empty -> basis undefined
        return {}

    # --- warm leg present + clean ---
    if not warm_samples_ms:
        return {}
    if not _all_finite_nonneg(warm_samples_ms):
        return {}

    # --- cold leg present, finite, positive ---
    if cold_sample_ms is None or isinstance(cold_sample_ms, bool):
        return {}
    if not isinstance(cold_sample_ms, Real):
        return {}
    cold_raw = float(cold_sample_ms)
    if not math.isfinite(cold_raw) or cold_raw <= 0:
        return {}

    warm_p50 = round(metrics.percentile(warm_samples_ms, 50), 1)
    cold_ms = round(cold_raw, 1)
    if warm_p50 <= 0 or cold_ms <= 0:  # degenerate after rounding -> ratio undefined
        return {}

    # speedup = cold/warm, computed from the DISPLAYED rounded values so the page is
    # self-consistent. retention(base, max) = round(max/base, 3); base=warm, max=cold.
    speedup = metrics.retention(warm_p50, cold_ms)

    return {
        "warm_p50_ms": warm_p50,
        "cold_ms": cold_ms,
        "speedup": speedup,
        "semantic": semantic,
        "runtime_class": runtime_class,
        "n_warm": len(warm_samples_ms),
    }
