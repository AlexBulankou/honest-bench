"""Tests for the variance-aware warm-pool separation verdict (#6918).
Dependency-free: `python3 test_warmpool_verdict.py` (exit 0 = pass).

These assert the verdict layer's contract: the noise floor is the pooled within-digest stdev
of ln(separation_ratio); a single fire whose margin to the threshold is inside the noise band
is refused (INDETERMINATE) with an honest n_required; a margin comfortably outside the band
resolves to PASS/FAIL; median-of-N shrinks the band by sqrt(N); and the whole thing degrades
to INDETERMINATE (never a silent PASS/FAIL) when no same-build replication exists to estimate
noise from.
"""

import math

import warmpool_verdict as wv


def _rows(*pairs):
    """Build validated-shape history rows from (digest_suffix, ratio) pairs."""
    rows = []
    for i, (suffix, ratio) in enumerate(pairs):
        rows.append(
            {
                "controller_digest": "sha256:" + (suffix * 64)[:64],
                "separation_ratio": ratio,
                "run_id": f"run-{i}",
            }
        )
    return rows


def _approx(a, b, tol=1e-9):
    return abs(a - b) <= tol


# --- noise-floor estimator -----------------------------------------------------------------

def test_sigma_none_without_replication():
    # No digest has >=2 measurements -> no way to measure run-to-run noise.
    assert wv.estimate_log_noise_sigma([]) is None
    assert wv.estimate_log_noise_sigma(_rows(("a", 1.4), ("b", 3.7))) is None


def test_sigma_pooled_within_digest():
    # Two same-build measurements: sigma = stdev of their log-ratios (dof=1).
    rows = _rows(("a", 0.5), ("a", 2.0))
    sigma = wv.estimate_log_noise_sigma(rows)
    logs = [math.log(0.5), math.log(2.0)]
    mean = sum(logs) / 2
    expected = math.sqrt(sum((x - mean) ** 2 for x in logs) / 1)
    assert _approx(sigma, expected), (sigma, expected)


def test_sigma_pools_variance_not_mean_across_digests():
    # Two digests with different TRUE ratios but the same within-build spread must NOT inflate
    # sigma with the between-digest difference — only the within-digest variances pool.
    rows = _rows(("a", 1.0), ("a", 4.0), ("b", 100.0), ("b", 400.0))
    sigma = wv.estimate_log_noise_sigma(rows)
    # Each digest contributes the SAME within-spread (factor 4), so pooled sigma == that spread's
    # single-digest sigma, unaffected by the 100x offset between digests.
    single = wv.estimate_log_noise_sigma(_rows(("a", 1.0), ("a", 4.0)))
    assert _approx(sigma, single), (sigma, single)


def test_sigma_skips_nonpositive_ratio():
    rows = _rows(("a", 0.5), ("a", 2.0), ("b", -1.0))
    # The negative-ratio row is dropped; only digest a (2 rows) contributes.
    assert wv.estimate_log_noise_sigma(rows) is not None


# --- refuse-single-fire (the DoD core) -----------------------------------------------------

def test_single_fire_inside_noise_band_is_indeterminate():
    # Empirical #6890 same-build pair -> a wide noise floor. A single fire near the 1.8x gate
    # must be refused, with an honest n_required.
    rows = _rows(("f", 0.2717607788232771), ("f", 1.05949977285474))
    r = wv.variance_aware_verdict(1.06, rows, threshold=1.8)
    assert r["verdict"] == wv.INDETERMINATE
    assert r["reason"] == "indeterminate-ci-straddles-threshold"
    assert r["n_fires"] == 1
    assert r["n_required"] is not None and r["n_required"] > 1
    # CI must actually straddle the threshold when INDETERMINATE.
    assert r["ci_low"] < 1.8 < r["ci_high"]


def test_even_far_pass_refused_under_huge_noise():
    # The finding's teeth: with the measured noise floor, even a 3.75x observation (>2x the gate)
    # is NOT a defensible single-fire PASS. This is the honest, non-obvious result.
    rows = _rows(
        ("f", 0.2717607788232771),
        ("f", 1.05949977285474),
        ("e", 2.41),
        ("e", 0.35),
    )
    r = wv.variance_aware_verdict(3.746, rows, threshold=1.8)
    assert r["verdict"] == wv.INDETERMINATE, r
    assert r["n_required"] > 1


def test_resolved_pass_when_margin_beats_band():
    # Tight noise floor (same-build measurements agree closely) -> a modest margin resolves.
    rows = _rows(("a", 1.9), ("a", 2.1))  # narrow spread -> small sigma
    r = wv.variance_aware_verdict(5.0, rows, threshold=1.8)
    assert r["verdict"] == wv.PASS, r
    assert r["reason"] == "resolved-pass"
    assert r["resolvable"] is True
    assert r["n_required"] is None
    assert r["ci_low"] > 1.8  # CI entirely above threshold


def test_resolved_fail_when_margin_beats_band():
    rows = _rows(("a", 1.0), ("a", 1.05))  # very tight -> tiny sigma
    r = wv.variance_aware_verdict(0.3, rows, threshold=1.8)
    assert r["verdict"] == wv.FAIL, r
    assert r["reason"] == "resolved-fail"
    assert r["ci_high"] < 1.8  # CI entirely below threshold


# --- median-of-N shrinks the band ----------------------------------------------------------

def test_more_fires_can_flip_indeterminate_to_resolved():
    rows = _rows(("f", 0.2717607788232771), ("f", 1.05949977285474))
    # One fire at 3.75x: indeterminate under the wide band.
    one = wv.variance_aware_verdict(3.746, rows, threshold=1.8)
    assert one["verdict"] == wv.INDETERMINATE
    # Enough consistent fires at ~3.75x shrink half_width by sqrt(N) until the margin clears.
    many = wv.variance_aware_verdict([3.746] * one["n_required"], rows, threshold=1.8)
    assert many["verdict"] == wv.PASS, many
    assert many["n_fires"] == one["n_required"]
    assert many["half_width_log"] < one["half_width_log"]


def test_point_estimate_is_median_not_mean():
    rows = _rows(("a", 1.9), ("a", 2.1))
    # Median of [4,5,100] is 5, robust to the 100 outlier (a mean would be ~36).
    r = wv.variance_aware_verdict([4.0, 5.0, 100.0], rows, threshold=1.8)
    assert _approx(r["point_ratio"], 5.0), r["point_ratio"]


# --- degrade-closed to INDETERMINATE, never a silent verdict --------------------------------

def test_no_noise_floor_is_indeterminate_not_pass():
    # A gorgeous 10x observation with NO same-build history to estimate noise from must still be
    # INDETERMINATE — we have no basis to claim the measurement is stable. Never a silent PASS.
    r = wv.variance_aware_verdict(10.0, _rows(("a", 1.4)), threshold=1.8)
    assert r["verdict"] == wv.INDETERMINATE
    assert r["reason"] == "indeterminate-no-noise-floor"
    assert r["sigma_log"] is None
    assert r["ci_low"] is None and r["ci_high"] is None


# --- MDE helper ----------------------------------------------------------------------------

def test_min_fires_for_margin_monotonic_in_sigma():
    # Wider noise floor needs more fires to resolve the same margin.
    small = wv.min_fires_for_margin(2.0, 0.3)
    big = wv.min_fires_for_margin(2.0, 1.2)
    assert big > small, (small, big)


def test_min_fires_none_on_bad_inputs():
    assert wv.min_fires_for_margin(2.0, None) is None
    assert wv.min_fires_for_margin(1.0, 0.5) is None  # margin_ratio <= 1
    assert wv.min_fires_for_margin(0.5, 0.5) is None


def test_min_fires_at_least_one():
    # A margin so large it's resolvable at a single fire still returns >=1, never 0.
    assert wv.min_fires_for_margin(1000.0, 0.1) >= 1


# --- input validation ----------------------------------------------------------------------

def test_rejects_nonpositive_observed():
    for bad in (0.0, -1.0):
        try:
            wv.variance_aware_verdict(bad, _rows(("a", 1.0), ("a", 1.1)))
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for observed={bad}")


def test_rejects_empty_sequence():
    try:
        wv.variance_aware_verdict([], _rows(("a", 1.0), ("a", 1.1)))
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for empty observed_ratios")


def test_rejects_unsupported_confidence():
    try:
        wv.variance_aware_verdict(2.0, _rows(("a", 1.0), ("a", 1.1)), confidence=0.5)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for unsupported confidence")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"ok - {len(fns)} tests passed")
