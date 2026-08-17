"""Variance-aware verdict layer for the warm-pool separation gate.

## The finding this exists to fix

The single-fire separation gate (`warmpool_cold_start._classify_latencies`) issues a
PASS/FAIL verdict from ONE fire's `separation_observed = cold_p50 / warm_p50` against a
fixed threshold (`WARMPOOL_SEPARATION_MIN_RATIO`, 1.8x). A same-build suspect-check
showed that verdict is not attributable: the SAME controller build digest, fired twice on
byte-identical inputs, produced separation ratios 2.41x (PASS) then 0.35x (FAIL) — a ~6.9x
swing — and a second digest independently swung 0.27x↔1.06x. The measurement's run-to-run
noise floor dwarfs any commit-level signal, so a single-fire verdict cannot tell "this build
regressed" from "this fire got an unlucky draw".

A sibling DISCLOSURE layer (`render.accrue_warmpool_separation` +
`render.render._warmpool_separation_variance_caveat`) surfaces that same-build variance on the
public page. This module is the sibling VERDICT layer: it makes the *verdict itself*
variance-aware, so the
gate refuses to issue a single-fire PASS/FAIL when the historical noise band is wider than the
observed margin to the threshold.

## The protocol (see WARMPOOL_SEPARATION_VERDICT_PROTOCOL.md for the full write-up)

Separation ratios are multiplicative (a ratio of two latencies), so all statistics are done in
log-space, where multiplicative noise becomes additive and symmetric.

1. **Noise floor (sigma).** Estimate the measurement noise as the pooled within-digest
   standard deviation of `ln(separation_ratio)` — for every controller_digest with >=2
   history measurements, its sample variance of the log-ratios, pooled across digests by
   residual degrees of freedom. Pooling the *variances* (not the means) across builds is the
   correct noise-floor estimator: different builds may have genuinely different true ratios
   (so their means must not be pooled), but the run-to-run measurement noise is assumed
   homoscedastic across builds — the finding is precisely that a fixed build measures
   inconsistently, which is a property of the measurement, not the build.

2. **Fires-per-verdict (median-of-N).** The point estimate for a verdict is the median of N
   independent same-config fires (median, not mean, for robustness to a single wild draw). The
   sampling half-width of that estimate at confidence C is `z(C) * sigma / sqrt(N)`.

3. **Minimum-detectable-effect (MDE) / refuse-single-fire rule.** A verdict is only ISSUED
   when the log-space margin to the threshold, `|ln(point / threshold)|`, exceeds the sampling
   half-width — i.e. the confidence interval does not straddle the threshold. Otherwise the
   verdict is INDETERMINATE and the module reports `n_required`, the fires needed to resolve
   the observed margin: `ceil((z * sigma / margin)^2)`. With N=1 this is exactly the DoD's
   "refuse single-fire verdicts where the historical variance bound exceeds the pass/fail
   margin".

This module is PURE (stdlib `math`/`statistics` only) and cluster-free: it consumes the
already-accrued history rows and a fire's observed ratio(s), and never touches the live gate,
Kubernetes, or the network. It does NOT modify the in-cluster harness gate — that gate must
keep emitting its raw per-fire ratio (the raw measurement is the input this layer needs);
this is an additive verdict layer on top of that measurement.
"""

import math
import statistics


# Two-sided normal z-multipliers for the common confidence levels. The normal approximation
# (not Student-t) is used deliberately: the pooled degrees of freedom from real history are
# tiny (often 1-2), so a t-multiplier would widen the interval further — the normal z is the
# OPTIMISTIC bound, and the protocol's conclusion (single-fire verdicts are indefensible) holds
# even under the optimistic bound, so it holds a fortiori under t. Keeping it dependency-free
# (no scipy) is worth the documented small-sample caveat. Callers wanting the conservative
# reading should read n_required as a floor, not an exact count.
_Z_BY_CONFIDENCE = {
    0.80: 1.2815515594457831,
    0.90: 1.6448536269514722,
    0.95: 1.959963984540054,
    0.99: 2.5758293035489004,
}

# Verdict enum. INDETERMINATE is a first-class, honest outcome — it is NOT a soft PASS or a
# soft FAIL: it says "the measurement cannot resolve which side of the threshold this build is
# on at the fires collected so far". A trust surface that renders it must not collapse it to
# either verdict (that would silently re-introduce the exact single-fire false-attribution this
# layer exists to prevent — a trust-surface downgrade must fail closed, never silently pick a side).
PASS = "PASS"
FAIL = "FAIL"
INDETERMINATE = "INDETERMINATE"


def _z(confidence):
    """z-multiplier for a supported confidence level (raises on an unsupported one)."""
    try:
        return _Z_BY_CONFIDENCE[confidence]
    except KeyError:
        raise ValueError(
            f"confidence must be one of {sorted(_Z_BY_CONFIDENCE)}, got {confidence!r}"
        )


def estimate_log_noise_sigma(history_rows):
    """Pooled within-digest stdev of ln(separation_ratio) — the measurement noise floor.

    `history_rows` are validated rows (as produced by
    `accrue_warmpool_separation.load_history`): each carries `controller_digest` and a positive
    `separation_ratio`. Groups by digest; for every digest with >=2 measurements, accumulates
    the sum-of-squared log-ratio residuals and its degrees of freedom (n-1); returns
    `sqrt(total_SS / total_dof)`.

    Returns `None` when the noise floor cannot be estimated — no digest has >=2 measurements,
    so there is no same-build replication to measure run-to-run noise from. A `None` sigma is
    itself a verdict input: with no noise-floor estimate, NO verdict can be defended (see
    `variance_aware_verdict`, which returns INDETERMINATE in that case). Rows with a
    non-positive ratio are skipped (log undefined); a digest left with <2 usable rows drops out.
    """
    by_digest = {}
    for r in history_rows:
        ratio = r.get("separation_ratio")
        digest = r.get("controller_digest")
        if digest is None or not isinstance(ratio, (int, float)) or ratio <= 0:
            continue
        by_digest.setdefault(digest, []).append(math.log(ratio))

    total_ss = 0.0
    total_dof = 0
    for logs in by_digest.values():
        if len(logs) < 2:
            continue
        mean = statistics.mean(logs)
        total_ss += sum((x - mean) ** 2 for x in logs)
        total_dof += len(logs) - 1

    if total_dof <= 0:
        return None
    return math.sqrt(total_ss / total_dof)


def min_fires_for_margin(margin_ratio, sigma, *, confidence=0.95):
    """Minimum-detectable-effect: fires needed to resolve a target multiplicative margin.

    `margin_ratio` is the multiplicative distance from the threshold you want to be able to
    resolve — e.g. 2.0 to reliably distinguish a build sitting at 2x the threshold from one at
    the threshold. Returns `ceil((z * sigma / ln(margin_ratio))^2)`, the N at which the
    sampling half-width `z*sigma/sqrt(N)` shrinks below the log-space margin `ln(margin_ratio)`.

    Returns 1 for a margin so large it is already resolvable at a single fire, and `None` when
    `sigma` is None (no noise-floor estimate) or `margin_ratio <= 1` (a zero/negative log-margin
    is not a resolvable target). This is the honest "how many fires is this verdict worth"
    number the protocol requires be stated alongside any point value.
    """
    if sigma is None or margin_ratio is None or margin_ratio <= 1.0:
        return None
    log_margin = math.log(margin_ratio)
    z = _z(confidence)
    n = (z * sigma / log_margin) ** 2
    return max(1, math.ceil(n))


def variance_aware_verdict(
    observed_ratios,
    history_rows,
    *,
    threshold=1.8,
    confidence=0.95,
):
    """Issue a PASS/FAIL/INDETERMINATE separation verdict that respects the noise floor.

    `observed_ratios` is either a single float (one fire, the DoD's refuse-single-fire case) or
    a non-empty sequence of floats (median-of-N). The point estimate is their median. `threshold`
    is the separation gate (default `WARMPOOL_SEPARATION_MIN_RATIO` == 1.8x). `history_rows` are
    the accrued same-build measurements used to estimate the noise floor (sigma).

    The rule (all in log-space, where the multiplicative ratio noise is additive/symmetric):
      - half_width = z(confidence) * sigma / sqrt(N)         # sampling uncertainty of the median
      - margin     = |ln(point / threshold)|                  # distance to the gate
      - if sigma is None (no >=2-measurement build in history) -> INDETERMINATE
        (no noise-floor estimate exists, so no verdict is defensible)
      - elif margin <= half_width                            -> INDETERMINATE
        (the CI straddles the threshold; the sign of point-threshold is not resolved) and
        `n_required` reports the fires needed to resolve the observed margin
      - else                                                 -> PASS if point >= threshold else FAIL

    Returns a dict (a stable, render-friendly, JSON-serializable shape — no free-text that could
    leak):
      {
        "verdict": PASS | FAIL | INDETERMINATE,
        "point_ratio": float,          # median of observed_ratios
        "n_fires": int,                # len(observed_ratios)
        "threshold": float,
        "confidence": float,
        "sigma_log": float | None,     # estimated noise floor; None when unestimable
        "ci_low": float | None,        # multiplicative CI on the point estimate
        "ci_high": float | None,
        "margin_log": float | None,    # |ln(point/threshold)|
        "half_width_log": float | None,# z*sigma/sqrt(N)
        "resolvable": bool,            # margin > half_width AND sigma known
        "n_required": int | None,      # fires to resolve the observed margin (when not resolvable)
        "reason": str,                 # closed-vocabulary reason code (never free text)
      }

    `reason` is a closed vocabulary, not prose: "resolved-pass", "resolved-fail",
    "indeterminate-no-noise-floor", "indeterminate-ci-straddles-threshold". Keeping it a fixed
    enum means a consumer can branch on it and a public renderer can never leak a raw string.
    """
    if isinstance(observed_ratios, (int, float)):
        ratios = [float(observed_ratios)]
    else:
        ratios = [float(r) for r in observed_ratios]
    if not ratios:
        raise ValueError("observed_ratios must be a float or a non-empty sequence of floats")
    if any(r <= 0 for r in ratios):
        raise ValueError("observed separation ratios must be positive (log-space verdict)")
    if threshold <= 0:
        raise ValueError("threshold must be positive")

    n = len(ratios)
    point = statistics.median(ratios)
    sigma = estimate_log_noise_sigma(history_rows)
    z = _z(confidence)

    result = {
        "verdict": INDETERMINATE,
        "point_ratio": point,
        "n_fires": n,
        "threshold": threshold,
        "confidence": confidence,
        "sigma_log": sigma,
        "ci_low": None,
        "ci_high": None,
        "margin_log": None,
        "half_width_log": None,
        "resolvable": False,
        "n_required": None,
        "reason": "indeterminate-no-noise-floor",
    }

    if sigma is None:
        # No same-build replication in history -> the noise floor is unknown, so we cannot
        # defend ANY verdict. Honest INDETERMINATE; no CI to publish.
        return result

    half_width = z * sigma / math.sqrt(n)
    margin = abs(math.log(point / threshold))
    ci_low = point * math.exp(-half_width)
    ci_high = point * math.exp(half_width)

    result.update(
        {
            "ci_low": ci_low,
            "ci_high": ci_high,
            "margin_log": margin,
            "half_width_log": half_width,
        }
    )

    if margin <= half_width:
        # CI straddles the threshold — the sign of (point - threshold) is not resolved at N
        # fires. Refuse the verdict and say how many fires WOULD resolve this margin.
        result["resolvable"] = False
        result["reason"] = "indeterminate-ci-straddles-threshold"
        # n to shrink half_width below the CURRENT observed margin: z*sigma/sqrt(N) < margin.
        result["n_required"] = (
            math.ceil((z * sigma / margin) ** 2) if margin > 0 else None
        )
        return result

    result["resolvable"] = True
    if point >= threshold:
        result["verdict"] = PASS
        result["reason"] = "resolved-pass"
    else:
        result["verdict"] = FAIL
        result["reason"] = "resolved-fail"
    return result
