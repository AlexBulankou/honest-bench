# Warm-pool separation — variance-aware verdict protocol

Status: adopted (verdict layer landed in `render/warmpool_verdict.py`, tests in
`render/test_warmpool_verdict.py`). Promoted to the **published, authoritative** verdict
(hb#659): the public page now adjudicates upstream-vs-fork over the **median of N≥3 accrued
fires**, not a single fire — see [Adjudication (median-of-N)](#adjudication-median-of-n--the-published-verdict).

## The finding this protocol exists to fix

The warm-pool separation gate (`harness/scenarios/warmpool_cold_start._classify_latencies`)
issues a PASS/FAIL verdict from **one** fire's `separation_observed = cold_p50 / warm_p50`
against a fixed threshold (`WARMPOOL_SEPARATION_MIN_RATIO`, 1.8x). A same-build suspect-check
showed that verdict is not attributable: the **same** controller build digest, fired twice on
byte-identical inputs, produced separation ratios 2.41x (PASS) then 0.35x (FAIL) — a ~6.9x
swing — and a second digest independently swung 0.27x↔1.06x. The measurement's run-to-run
noise floor dwarfs any commit-level signal, so a single-fire verdict cannot tell "this build
regressed" from "this fire got an unlucky draw".

This protocol makes the **verdict itself** variance-aware. It is the sibling of the already-
landed *disclosure* work (`render.render._warmpool_separation_variance_caveat`), which surfaces
same-build variance on the public page. Disclosure tells a reader the number is noisy; this
protocol makes the gate **refuse to issue** a single-fire PASS/FAIL when the historical noise
band is wider than the observed margin to the threshold.

## Why log-space

Separation ratios are multiplicative (a ratio of two latencies). In linear space their noise is
asymmetric and scale-dependent; in log-space multiplicative noise becomes **additive and
symmetric**, so ordinary Gaussian statistics (pooled variance, z-intervals) apply. Every
statistic below is computed on `ln(separation_ratio)`.

## The three steps

### 1. Noise floor (sigma)

Estimate measurement noise as the **pooled within-digest standard deviation** of
`ln(separation_ratio)`: for every `controller_digest` with ≥2 history measurements, take its
sample variance of the log-ratios; pool across digests by residual degrees of freedom
(`sqrt(Σ SS / Σ dof)`).

Pooling the **variances, not the means**, across builds is the correct estimator. Different
builds may have genuinely different *true* ratios, so their means must not be pooled — but the
run-to-run *measurement noise* is assumed homoscedastic across builds. That assumption is
exactly the finding: a fixed build measures inconsistently, which is a property of the
measurement, not the build.

If **no** digest has ≥2 measurements there is no same-build replication to estimate noise from,
and sigma is `None`. A `None` sigma is itself a verdict input — see step 3.

### 2. Fires-per-verdict (median-of-N)

The point estimate for a verdict is the **median** of N independent same-config fires (median,
not mean, for robustness to a single wild draw). The sampling half-width of that estimate at
confidence C is `z(C) * sigma / sqrt(N)`.

The z-multiplier is the **normal** (not Student-t) two-sided value. This is deliberate: the
pooled degrees of freedom from real history are tiny (often 1–2), so a t-multiplier would widen
the interval further. The normal z is therefore the **optimistic** bound, and the protocol's
conclusion (single-fire verdicts are indefensible under the measured noise) holds *a fortiori*
under t. It also keeps the implementation dependency-free (no scipy). Callers wanting the
conservative reading should treat `n_required` as a floor, not an exact count.

### 3. Minimum-detectable-effect / refuse-single-fire rule

A verdict is **issued** only when the log-space margin to the threshold,
`|ln(point / threshold)|`, exceeds the sampling half-width — i.e. the confidence interval does
not straddle the threshold. Otherwise the verdict is **INDETERMINATE** and the module reports
`n_required`, the fires needed to resolve the observed margin:

```
n_required = ceil((z * sigma / margin)^2)
```

With N=1 this is exactly the requirement: *refuse single-fire verdicts where the historical
variance bound exceeds the pass/fail margin.*

## INDETERMINATE is a first-class outcome

INDETERMINATE is **not** a soft PASS or a soft FAIL. It says: "the measurement cannot resolve
which side of the threshold this build is on at the fires collected so far." A trust surface
that renders it must not collapse it to either verdict — doing so silently re-introduces the
exact single-fire false-attribution this protocol exists to prevent. This is the
fail-closed-on-downgrade idiom (a trust-surface downgrade must fail closed, never silently
pick a side).

Two ways a verdict lands INDETERMINATE:

- **`indeterminate-no-noise-floor`** — sigma is `None` (no ≥2-measurement build in history).
  No noise-floor estimate exists, so no verdict is defensible. No CI is published.
- **`indeterminate-ci-straddles-threshold`** — the CI straddles the threshold at N fires.
  `n_required` reports the fires that would resolve the observed margin.

## Adjudication (median-of-N): the published verdict

The verdict layer above defines *how* a variance-aware verdict is computed. This section
defines *which input* the **published** upstream-vs-fork verdict feeds it — the change hb#659
requires.

The failure hb#659 targets is a single Cloud Build draw flipping the published verdict. At the
1.8x bar that single draw is noise-dominated: the same controller build fired twice spanned
0.27x–1.06x, and an N=5 remeasure of identical bits spanned 0.31x–4.05x with **2 of 5 draws
inverted** (warm measured slower than cold — physically impossible as a real signal). A verdict
that flips on one such draw is not measuring the build.

The published verdict therefore adjudicates over the **median of the most recent N≥3
same-`cluster_substrate` fires** accrued in the committed history
(`sandbox/results/warmpool-separation-history.jsonl`), fed to `variance_aware_verdict`:

- **N < 3 accrued fires** for a substrate → **HELD** (no flip). Too few fires to adjudicate;
  the prior conservative posture (fork retained, `HB_FORK_BUILD=1`) is kept until enough fires
  accrue. `WARMPOOL_ADJUDICATION_MIN_N` (schema) sets the floor.
- **N ≥ 3** → the median is the point estimate and the pooled within-build noise floor gives
  the CI. A side (PASS/FAIL) is issued **only** when that CI clears the gate; otherwise the
  verdict is **HELD** (INDETERMINATE — no flip), stating the fires that would resolve the
  margin. INDETERMINATE never collapses to a side (the fail-closed idiom above).

Two properties make this **no-spend**:

1. It consumes rows already committed to the history JSONL — it is a *methodology change over
   accrued data*, not a new benchmark fire. No cluster is spun up to adjudicate.
2. It is **pure over the accrued history and deliberately does not read the live fire** — the
   live single ratio is exactly the noise-dominated input this layer exists to refuse. As
   routine accrual crons append rows, a thin/straddling history resolves to a defensible
   PASS/FAIL on its own.

On the history that motivated this work (4 gke-sandbox rows) the adjudicated verdict is
median-of-4 = 1.24x with a 95% band that straddles the gate → **HELD (no flip)**, ~26
consistent fires needed to resolve — i.e. the fork is retained, matching the conservative prior
exactly, with no verdict fabricated from thin data.

Rendered on the public page as the *Adjudicated separation verdict (median-of-N)* subsection
(`render.render._warmpool_separation_adjudicated_verdict`), closed-vocabulary output only.

## What this does NOT change

The in-cluster harness gate (`_classify_latencies`) is **untouched**. It must keep emitting its
raw per-fire ratio — that raw measurement is the input this verdict layer consumes. This is an
**additive** layer on top of the measurement, not a modification of the live gate, so its blast
radius is a pure-function module plus its tests. The adjudication above changes only *which
published surface is authoritative* (median-of-N, not the raw single-fire caveat); the raw
per-fire gate and its disclosure caveat remain exactly as before.

## API

`render/warmpool_verdict.py` (pure stdlib: `math`, `statistics`):

- `estimate_log_noise_sigma(history_rows) -> float | None` — the pooled within-digest noise
  floor, or `None` when unestimable.
- `min_fires_for_margin(margin_ratio, sigma, *, confidence=0.95) -> int | None` — the MDE
  helper: fires needed to resolve a target multiplicative margin.
- `variance_aware_verdict(observed_ratios, history_rows, *, threshold=1.8, confidence=0.95)
  -> dict` — the verdict. `observed_ratios` is one float (the refuse-single-fire case) or a
  non-empty sequence (median-of-N). Returns a stable, JSON-serializable dict; `reason` is a
  closed vocabulary (`resolved-pass`, `resolved-fail`, `indeterminate-no-noise-floor`,
  `indeterminate-ci-straddles-threshold`) so a consumer can branch on it and a public renderer
  can never leak a raw string.

`history_rows` are the validated rows produced by
`render/accrue_warmpool_separation.load_history` — each carries `controller_digest` and a
positive `separation_ratio`.

## What the measured noise floor implies

On the same-build history that motivated this work the pooled noise floor is roughly
`sigma_log ≈ 1.2` — a single-fire 95% band of about ±10x. Under that band a verdict sitting near
the 1.8x gate needs tens of consistent fires to resolve; only an extreme observation (many times
the gate) resolves at a handful of fires. That is the honest, non-obvious consequence the
finding predicted, now quantified: at the measured noise floor, a single-fire PASS/FAIL near the
threshold is not defensible.
