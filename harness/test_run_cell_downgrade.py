"""Offline tests for run.check_cell_downgrade + run.carry_prior_density — no
cluster, no I/O.

Run with bare python3 (no pytest, so the auto-refresh GH-runner needs nothing
extra):  python3 -m harness.test_run_cell_downgrade
or directly:               python3 harness/test_run_cell_downgrade.py

These assert the hb#206 property: a refresh that would silently downgrade ANY
published cell — measured→pending outcome, a lost sla_metrics key, or a dropped
measured row — is detected (the caller then refuses the wholesale write unless
BENCH_ALLOW_CELL_DOWNGRADE is set). Prior `pending` placeholders never gate,
value changes never gate, key GAINS never gate. carry_prior_density is the
paired restore path: the one cross-fire field (Max Density) is carried across a
refresh fired without the density envs, fresh wins outright.
"""

from __future__ import annotations

# Make this file runnable BOTH as `python3 harness/test_x.py` and
# `python3 -m harness.test_x` by putting the repo root on sys.path before
# the absolute `from harness import ...` below (mirrors test_run_merge.py).
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from harness.run import carry_prior_density, check_cell_downgrade


def _check(cond, msg):
    if not cond:
        raise AssertionError(msg)


# ---------------------------------------------------------------- guard legs

def test_key_loss_detected():
    # The hb#206 shape: committed warm row carries density_per_vcpu, a refresh
    # without the density envs emits the same row minus that key.
    prior = [{
        "name": "warmpool_cold_start", "outcome": "PASS", "n": 30,
        "sla_metrics": {"ttfe_p50_ms": 755.6, "density_per_vcpu": 5.98},
    }]
    raw = [{
        "name": "warmpool_cold_start", "outcome": "PASS", "n": 30,
        "sla_metrics": {"ttfe_p50_ms": 741.2},
    }]
    lines = check_cell_downgrade(raw, prior)
    _check(len(lines) == 1, f"expected 1 downgrade, got {lines!r}")
    _check("density_per_vcpu" in lines[0] and "warmpool_cold_start" in lines[0],
           f"unexpected line: {lines[0]!r}")


def test_outcome_downgrade_detected():
    prior = [{"name": "burst_create", "outcome": "PASS",
              "sla_metrics": {"ttfe_p50_ms": 1.0}}]
    raw = [{"name": "burst_create", "outcome": "pending",
            "sla_metrics": {"ttfe_p50_ms": 1.0}}]
    lines = check_cell_downgrade(raw, prior)
    _check(len(lines) == 1, f"expected 1 downgrade, got {lines!r}")
    _check("PASS -> pending" in lines[0], f"unexpected line: {lines[0]!r}")


def test_row_drop_detected():
    # merge_seed_placeholders only resurrects pending priors, so a
    # deregistered MEASURED row silently vanishes — the guard makes it loud.
    prior = [{"name": "gvisor_canary", "outcome": "PASS",
              "sla_metrics": {"ttfe_p50_ms": 1.0}}]
    raw = [{"name": "burst_create", "outcome": "PASS", "sla_metrics": {}}]
    lines = check_cell_downgrade(raw, prior)
    _check(len(lines) == 1, f"expected 1 downgrade, got {lines!r}")
    _check("dropped entirely" in lines[0], f"unexpected line: {lines[0]!r}")


def test_clean_refresh_and_gains_pass():
    # Same keys + value changes + NEW keys (fresh instrumentation) = clean.
    # This is the real 07-04 fresh row shape: it GAINED thpt_slo_* fields.
    prior = [{
        "name": "warmpool_cold_start", "outcome": "PASS",
        "sla_metrics": {"ttfe_p50_ms": 755.6, "exec_success_rate": 1.0},
    }]
    raw = [{
        "name": "warmpool_cold_start", "outcome": "PASS",
        "sla_metrics": {"ttfe_p50_ms": 741.2, "exec_success_rate": 1.0,
                        "thpt_cluster_node_count": 10.0},
    }]
    _check(check_cell_downgrade(raw, prior) == [],
           "value changes and key gains must not gate")


def test_prior_pending_never_gates():
    prior = [{"name": "suspend_resume", "outcome": "pending",
              "sla_metrics": {"ttfe_p50_ms": 1.0}}]
    raw = [{"name": "burst_create", "outcome": "PASS", "sla_metrics": {}}]
    _check(check_cell_downgrade(raw, prior) == [],
           "prior pending row must never gate (even when dropped)")


def test_fail_to_pass_is_not_a_downgrade():
    prior = [{"name": "x", "outcome": "FAIL", "sla_metrics": {"a": 1.0}}]
    raw = [{"name": "x", "outcome": "PASS", "sla_metrics": {"a": 2.0}}]
    _check(check_cell_downgrade(raw, prior) == [],
           "FAIL->PASS with same keys must not gate")


def test_multiple_legs_reported_together():
    prior = [
        {"name": "a", "outcome": "PASS", "sla_metrics": {"k1": 1.0, "k2": 2.0}},
        {"name": "b", "outcome": "FAIL", "sla_metrics": {"k1": 1.0}},
    ]
    raw = [{"name": "a", "outcome": "pending", "sla_metrics": {"k1": 1.0}}]
    lines = check_cell_downgrade(raw, prior)
    # a: outcome downgrade + key loss (k2); b: dropped.
    _check(len(lines) == 3, f"expected 3 downgrade lines, got {lines!r}")


def test_downgraded_names_populated():
    # The optional downgraded_names side-channel lets the caller
    # recover exactly which fresh rows are about to be discarded on refusal,
    # without re-deriving the guard's own predicate logic. One name per
    # affected scenario even when a single scenario trips multiple legs
    # (outcome downgrade + key loss below) — a set, not a multiset.
    prior = [
        {"name": "a", "outcome": "PASS", "sla_metrics": {"k1": 1.0, "k2": 2.0}},
        {"name": "b", "outcome": "FAIL", "sla_metrics": {"k1": 1.0}},
        {"name": "c", "outcome": "PASS", "sla_metrics": {"k1": 1.0}},
    ]
    raw = [
        {"name": "a", "outcome": "pending", "sla_metrics": {"k1": 1.0}},
        {"name": "c", "outcome": "PASS", "sla_metrics": {"k1": 1.0}},
    ]
    names: set = set()
    lines = check_cell_downgrade(raw, prior, names)
    # a: outcome downgrade + key loss (both legs, same name); b: dropped;
    # c: clean, never added.
    _check(names == {"a", "b"}, f"expected {{'a', 'b'}}, got {names!r}")
    _check(len(lines) == 3, f"expected 3 downgrade lines, got {lines!r}")


def test_downgraded_names_none_by_default():
    # Passing no third arg must behave exactly as before (default None,
    # no side-channel) — every pre-existing call site stays untouched.
    prior = [{"name": "a", "outcome": "PASS", "sla_metrics": {"k1": 1.0}}]
    raw = [{"name": "a", "outcome": "pending", "sla_metrics": {"k1": 1.0}}]
    lines = check_cell_downgrade(raw, prior)  # no crash, no third arg
    _check(len(lines) == 1, f"expected 1 downgrade, got {lines!r}")


def test_measured_with_suffixes_key_loss_line():
    # hb#723: a committed row carrying measured_with makes the key-loss line
    # name the concrete envs that gated the lost key.
    prior = [{
        "name": "warmpool_cold_start", "outcome": "PASS", "n": 30,
        "sla_metrics": {"ttfe_p50_ms": 755.6, "density_per_vcpu": 5.98},
        "measured_with": {"WARMPOOL_COLD_START_POOL_REPLICAS": 5,
                           "WARMPOOL_COLD_START_CLAIM_COUNT": 10},
    }]
    raw = [{
        "name": "warmpool_cold_start", "outcome": "PASS", "n": 30,
        "sla_metrics": {"ttfe_p50_ms": 741.2},
    }]
    lines = check_cell_downgrade(raw, prior)
    _check(len(lines) == 1, f"expected 1 downgrade, got {lines!r}")
    _check("WARMPOOL_COLD_START_POOL_REPLICAS" in lines[0],
           f"measured_with not surfaced: {lines[0]!r}")


def test_measured_with_suffixes_outcome_downgrade_line():
    prior = [{"name": "native_digest_cold", "outcome": "PASS",
              "sla_metrics": {"cold_start_ms": 1.0},
              "measured_with": {"NATIVE_DIGEST_COLD_SAMPLES": 5}}]
    raw = [{"name": "native_digest_cold", "outcome": "pending",
            "sla_metrics": {"cold_start_ms": 1.0}}]
    lines = check_cell_downgrade(raw, prior)
    _check(len(lines) == 1, f"expected 1 downgrade, got {lines!r}")
    _check("NATIVE_DIGEST_COLD_SAMPLES" in lines[0],
           f"measured_with not surfaced: {lines[0]!r}")


def test_measured_with_suffixes_row_drop_line():
    prior = [{"name": "gvisor_canary", "outcome": "PASS",
              "sla_metrics": {"ttfe_p50_ms": 1.0},
              "measured_with": {"NATIVE_DIGEST_COLD_SAMPLES": 3}}]
    raw = [{"name": "burst_create", "outcome": "PASS", "sla_metrics": {}}]
    lines = check_cell_downgrade(raw, prior)
    _check(len(lines) == 1, f"expected 1 downgrade, got {lines!r}")
    _check("NATIVE_DIGEST_COLD_SAMPLES" in lines[0],
           f"measured_with not surfaced: {lines[0]!r}")


def test_no_measured_with_leaves_line_unchanged():
    # Cells with no env knobs (the common case) must render exactly as before —
    # no empty/None suffix noise.
    prior = [{"name": "warmpool_cold_start", "outcome": "PASS",
              "sla_metrics": {"ttfe_p50_ms": 755.6, "density_per_vcpu": 5.98}}]
    raw = [{"name": "warmpool_cold_start", "outcome": "PASS",
            "sla_metrics": {"ttfe_p50_ms": 741.2}}]
    lines = check_cell_downgrade(raw, prior)
    _check(len(lines) == 1, f"expected 1 downgrade, got {lines!r}")
    _check("measured_with" not in lines[0],
           f"unexpected measured_with suffix with no knobs: {lines[0]!r}")


def test_config_mismatch_key_loss_skipped_not_flagged():
    # hb#808: a config-MISMATCH diagnostic fire (pool=45) drops ttfe keys the
    # committed row (pool=30) carried. Both sides explicitly self-report
    # DIFFERENT measured_with, so the key-SET comparison is not apples-to-apples
    # in the first place -- the key-loss leg must skip entirely (no downgrade
    # line) rather than fire a non-actionable ERROR for a fire that was never a
    # real candidate to replace the canonical cell. (hb#807's predecessor
    # behavior annotated-but-still-fired this case; hb#808 skips it outright.)
    prior = [{
        "name": "warmpool_cold_start", "outcome": "PASS", "n": 30,
        "sla_metrics": {"ttfe_p50_ms": 755.6, "ttfe_p95_ms": 900.0},
        "measured_with": {"WARMPOOL_COLD_START_POOL_REPLICAS": 30},
    }]
    raw = [{
        "name": "warmpool_cold_start", "outcome": "FAIL", "n": 45,
        "sla_metrics": {},
        "measured_with": {"WARMPOOL_COLD_START_POOL_REPLICAS": 45},
    }]
    lines = check_cell_downgrade(raw, prior)
    _check(lines == [], f"expected no downgrade (config mismatch skips key-loss leg), got {lines!r}")


def test_same_config_key_loss_still_fires():
    # hb#808 regression guard: when BOTH sides explicitly report the SAME
    # measured_with, the key-set comparison is apples-to-apples and must still
    # fire exactly as before the fix.
    prior = [{
        "name": "warmpool_cold_start", "outcome": "PASS", "n": 30,
        "sla_metrics": {"ttfe_p50_ms": 755.6, "ttfe_p95_ms": 900.0},
        "measured_with": {"WARMPOOL_COLD_START_POOL_REPLICAS": 30},
    }]
    raw = [{
        "name": "warmpool_cold_start", "outcome": "PASS", "n": 30,
        "sla_metrics": {"ttfe_p50_ms": 741.2},
        "measured_with": {"WARMPOOL_COLD_START_POOL_REPLICAS": 30},
    }]
    lines = check_cell_downgrade(raw, prior)
    _check(len(lines) == 1, f"expected 1 downgrade (same config), got {lines!r}")
    _check("ttfe_p95_ms" in lines[0], f"unexpected line: {lines[0]!r}")


def test_null_transition_with_recognized_reason_passes():
    # #4420 guard-then-fill: a registered key that STAYS PRESENT but nulls out
    # is not a downgrade when the fresh row's sibling reason field names a
    # recognized absent-reason (e.g. no true cold-tier claims this fire).
    prior = [{
        "name": "warmpool_cold_start", "outcome": "PASS",
        "sla_metrics": {"warmpool_gate_cold_min_ms": 5000.0},
    }]
    raw = [{
        "name": "warmpool_cold_start", "outcome": "PASS",
        "sla_metrics": {
            "warmpool_gate_cold_min_ms": None,
            "warmpool_gate_cold_absent_reason": "no_true_cold_bucket_claims",
        },
    }]
    _check(check_cell_downgrade(raw, prior) == [],
           "recognized-reason null transition must not gate")


def test_null_transition_without_reason_fails_closed():
    # Same null transition, but the fresh row carries NO sibling reason field
    # at all -- must fail closed like any other information-reducing
    # transition, not silently pass just because the key is still "present".
    prior = [{
        "name": "warmpool_cold_start", "outcome": "PASS",
        "sla_metrics": {"warmpool_gate_cold_min_ms": 5000.0},
    }]
    raw = [{
        "name": "warmpool_cold_start", "outcome": "PASS",
        "sla_metrics": {"warmpool_gate_cold_min_ms": None},
    }]
    lines = check_cell_downgrade(raw, prior)
    _check(len(lines) == 1, f"expected 1 downgrade, got {lines!r}")
    _check("nulled without a recognized absent-reason" in lines[0]
           and "warmpool_gate_cold_min_ms" in lines[0],
           f"unexpected line: {lines[0]!r}")


def test_null_transition_with_unrecognized_reason_fails_closed():
    # Closed-set enforcement: a reason string outside
    # _RECOGNIZED_ABSENT_REASONS (a future typo, or an unhandled new condition
    # class) must fail closed exactly like a missing reason -- the vocabulary
    # is closed, not "any non-empty string".
    prior = [{
        "name": "warmpool_cold_start", "outcome": "PASS",
        "sla_metrics": {"warmpool_gate_cold_min_ms": 5000.0},
    }]
    raw = [{
        "name": "warmpool_cold_start", "outcome": "PASS",
        "sla_metrics": {
            "warmpool_gate_cold_min_ms": None,
            "warmpool_gate_cold_absent_reason": "some_future_typo",
        },
    }]
    lines = check_cell_downgrade(raw, prior)
    _check(len(lines) == 1, f"expected 1 downgrade, got {lines!r}")
    _check("warmpool_gate_cold_min_ms" in lines[0],
           f"unexpected line: {lines[0]!r}")


def test_null_transition_wrong_type_reason_fails_closed():
    # A non-string reason value (e.g. the field left as JSON null, or
    # accidentally serialized as a number) must also fail closed -- the
    # isinstance(reason_val, str) check is load-bearing, not incidental.
    prior = [{
        "name": "warmpool_cold_start", "outcome": "PASS",
        "sla_metrics": {"warmpool_gate_separation_ratio": 1.8},
    }]
    raw = [{
        "name": "warmpool_cold_start", "outcome": "PASS",
        "sla_metrics": {
            "warmpool_gate_separation_ratio": None,
            "warmpool_gate_cold_absent_reason": None,
        },
    }]
    lines = check_cell_downgrade(raw, prior)
    _check(len(lines) == 1, f"expected 1 downgrade, got {lines!r}")
    _check("warmpool_gate_separation_ratio" in lines[0],
           f"unexpected line: {lines[0]!r}")


def test_null_transition_recognized_reason_covers_all_registered_keys():
    # The closed-set registry applies uniformly to all three registered keys,
    # not just cold_min_ms -- exercise cold_p50_ms and separation_ratio too,
    # all nulled together under one recognized reason (the real emitter shape
    # for the "no true cold tier" condition: all three null out at once).
    prior = [{
        "name": "warmpool_cold_start", "outcome": "PASS",
        "sla_metrics": {
            "warmpool_gate_cold_min_ms": 5000.0,
            "warmpool_gate_cold_p50_ms": 5200.0,
            "warmpool_gate_separation_ratio": 3.1,
        },
    }]
    raw = [{
        "name": "warmpool_cold_start", "outcome": "PASS",
        "sla_metrics": {
            "warmpool_gate_cold_min_ms": None,
            "warmpool_gate_cold_p50_ms": None,
            "warmpool_gate_separation_ratio": None,
            "warmpool_gate_cold_absent_reason": "no_true_cold_bucket_claims",
        },
    }]
    _check(check_cell_downgrade(raw, prior) == [],
           "all three registered keys nulled under one recognized reason must not gate")


def test_null_transition_partial_null_unrecognized_reason_flags_only_that_key():
    # degenerate_warm_p50 shape: only separation_ratio nulls out (cold_min/p50
    # stay real numbers) -- an unrecognized reason must flag exactly the
    # nulled key, not the still-populated siblings.
    prior = [{
        "name": "warmpool_cold_start", "outcome": "PASS",
        "sla_metrics": {
            "warmpool_gate_cold_min_ms": 5000.0,
            "warmpool_gate_cold_p50_ms": 5000.0,
            "warmpool_gate_separation_ratio": 3.1,
        },
    }]
    raw = [{
        "name": "warmpool_cold_start", "outcome": "PASS",
        "sla_metrics": {
            "warmpool_gate_cold_min_ms": 5000.0,
            "warmpool_gate_cold_p50_ms": 5000.0,
            "warmpool_gate_separation_ratio": None,
            "warmpool_gate_cold_absent_reason": "not_a_real_reason",
        },
    }]
    lines = check_cell_downgrade(raw, prior)
    _check(len(lines) == 1, f"expected 1 downgrade, got {lines!r}")
    _check("warmpool_gate_separation_ratio" in lines[0]
           and "warmpool_gate_cold_min_ms" not in lines[0],
           f"unexpected line: {lines[0]!r}")


def test_reason_field_vanishes_on_null_to_value_upgrade_passes():
    # The gVisor-refresh false-positive (build 560b8492, 09-11): the committed
    # row observed NO dip, so it carried during_dip metrics=null + the sibling
    # absent_reason="no_dip_observed". A later fire OBSERVED the dip, so it
    # populated during_dip median/n (non-null) and the emitter dropped the
    # now-unneeded absent_reason. The key-loss leg saw the reason field vanish
    # and flagged it as a downgrade -- but it is the correct consequence of a
    # null->value UPGRADE, not a downgrade. It must NOT gate.
    prior = [{
        "name": "warmpool_cold_start", "outcome": "PASS",
        "sla_metrics": {
            "warmpool_gate_ttfe_during_dip_median_ms": None,
            "warmpool_gate_ttfe_during_dip_n": None,
            "warmpool_gate_ttfe_during_dip_absent_reason": "no_dip_observed",
        },
    }]
    raw = [{
        "name": "warmpool_cold_start", "outcome": "PASS",
        "sla_metrics": {
            "warmpool_gate_ttfe_during_dip_median_ms": 2632.93,
            "warmpool_gate_ttfe_during_dip_n": 14,
        },
    }]
    _check(check_cell_downgrade(raw, prior) == [],
           "reason field vanishing on a null->value upgrade must not gate")


def test_reason_field_lost_without_sibling_repopulation_still_fires():
    # Guard the exemption's boundary: a reason field that vanishes while EVERY
    # sibling metric it explains stays null (or absent) is NOT an upgrade -- it
    # is a genuine loss of the recorded cause for a still-absent bucket, and
    # must still gate. (A null metric with no recognized reason is exactly the
    # information-loss shape #4420 forbids.)
    prior = [{
        "name": "warmpool_cold_start", "outcome": "PASS",
        "sla_metrics": {
            "warmpool_gate_ttfe_during_dip_median_ms": None,
            "warmpool_gate_ttfe_during_dip_n": None,
            "warmpool_gate_ttfe_during_dip_absent_reason": "no_dip_observed",
        },
    }]
    raw = [{
        "name": "warmpool_cold_start", "outcome": "PASS",
        "sla_metrics": {
            "warmpool_gate_ttfe_during_dip_median_ms": None,
            "warmpool_gate_ttfe_during_dip_n": None,
        },
    }]
    lines = check_cell_downgrade(raw, prior)
    _check(len(lines) == 1, f"expected 1 downgrade, got {lines!r}")
    _check("warmpool_gate_ttfe_during_dip_absent_reason" in lines[0],
           f"unexpected line: {lines[0]!r}")


def test_reason_field_exempt_only_covers_its_own_siblings():
    # The exemption is per-reason-field: re-populating a during_dip metric must
    # NOT exempt the loss of the cold-tier reason field (different sibling set).
    # cold metrics stay null AND the cold reason vanishes -> that leg still fires,
    # even though the unrelated during_dip bucket was upgraded in the same fire.
    prior = [{
        "name": "warmpool_cold_start", "outcome": "PASS",
        "sla_metrics": {
            "warmpool_gate_ttfe_during_dip_median_ms": None,
            "warmpool_gate_ttfe_during_dip_n": None,
            "warmpool_gate_ttfe_during_dip_absent_reason": "no_dip_observed",
            "warmpool_gate_cold_min_ms": None,
            "warmpool_gate_cold_p50_ms": None,
            "warmpool_gate_cold_absent_reason": "no_true_cold_bucket_claims",
        },
    }]
    raw = [{
        "name": "warmpool_cold_start", "outcome": "PASS",
        "sla_metrics": {
            "warmpool_gate_ttfe_during_dip_median_ms": 2632.93,
            "warmpool_gate_ttfe_during_dip_n": 14,
            "warmpool_gate_cold_min_ms": None,
            "warmpool_gate_cold_p50_ms": None,
        },
    }]
    lines = check_cell_downgrade(raw, prior)
    _check(len(lines) == 1, f"expected 1 downgrade, got {lines!r}")
    _check("warmpool_gate_cold_absent_reason" in lines[0]
           and "warmpool_gate_ttfe_during_dip_absent_reason" not in lines[0],
           f"unexpected line: {lines[0]!r}")


def test_null_transition_skipped_on_config_mismatch():
    # The null-transition check lives inside the same mw_match-gated branch as
    # the key-loss leg (hb#808) -- a config-mismatched fire skips the whole
    # leg, including the null-transition sub-check, not just key-loss.
    prior = [{
        "name": "warmpool_cold_start", "outcome": "PASS", "n": 30,
        "sla_metrics": {"warmpool_gate_cold_min_ms": 5000.0},
        "measured_with": {"WARMPOOL_COLD_START_POOL_REPLICAS": 30},
    }]
    raw = [{
        "name": "warmpool_cold_start", "outcome": "PASS", "n": 45,
        "sla_metrics": {
            "warmpool_gate_cold_min_ms": None,
            "warmpool_gate_cold_absent_reason": "bogus_unrecognized",
        },
        "measured_with": {"WARMPOOL_COLD_START_POOL_REPLICAS": 45},
    }]
    _check(check_cell_downgrade(raw, prior) == [],
           "config-mismatched fire must skip the null-transition sub-check too")


def test_fresh_measured_with_surfaced_when_committed_has_none():
    # Asymmetric case: only the fresh fire self-reported knobs. The committed
    # side reads "none reported" so the asymmetry (the mismatch cue) is visible.
    prior = [{
        "name": "warmpool_cold_start", "outcome": "PASS",
        "sla_metrics": {"ttfe_p50_ms": 755.6},
    }]
    raw = [{
        "name": "warmpool_cold_start", "outcome": "PASS", "sla_metrics": {},
        "measured_with": {"WARMPOOL_COLD_START_POOL_REPLICAS": 45},
    }]
    lines = check_cell_downgrade(raw, prior)
    _check(len(lines) == 1, f"expected 1 downgrade, got {lines!r}")
    ln = lines[0]
    _check("committed row measured_with: none reported" in ln,
           f"committed 'none reported' not surfaced: {ln!r}")
    _check("WARMPOOL_COLD_START_POOL_REPLICAS" in ln,
           f"fresh measured_with not surfaced: {ln!r}")


def test_malformed_inputs_tolerated():
    _check(check_cell_downgrade([{"name": "x"}], []) == [],
           "empty prior must be a no-op")
    _check(check_cell_downgrade([{"name": "x"}], None) == [],
           "non-list prior must be a no-op")
    prior = ["not-a-dict", {"name": 42, "outcome": "PASS"},
             {"outcome": "PASS"},
             {"name": "y", "outcome": "PASS", "sla_metrics": "not-a-dict"}]
    raw = ["not-a-dict", {"name": "y", "outcome": "PASS", "sla_metrics": {}}]
    _check(check_cell_downgrade(raw, prior) == [],
           "malformed rows on either side must not gate or raise")


# ------------------------------------------------------------- density carry

def test_density_carried_onto_fresh_row():
    prior = [{"name": "warmpool_cold_start", "outcome": "PASS",
              "sla_metrics": {"density_per_vcpu": 5.98, "ttfe_p50_ms": 755.6}}]
    raw = [{"name": "warmpool_cold_start", "outcome": "PASS",
            "sla_metrics": {"ttfe_p50_ms": 741.2}}]
    carry_prior_density(raw, prior)
    _check(raw[0]["sla_metrics"].get("density_per_vcpu") == 5.98,
           f"density not carried: {raw[0]['sla_metrics']!r}")
    # and only density — the same-fire metric must not travel
    _check("ttfe_p50_ms" in raw[0]["sla_metrics"]
           and raw[0]["sla_metrics"]["ttfe_p50_ms"] == 741.2,
           "carry must be density-only; fresh same-fire metrics untouched")
    # carried row must now pass the downgrade guard
    _check(check_cell_downgrade(raw, prior) == [],
           "carry + guard must compose: carried row is clean")


def test_density_fresh_wins():
    prior = [{"name": "warmpool_cold_start", "outcome": "PASS",
              "sla_metrics": {"density_per_vcpu": 5.98}}]
    raw = [{"name": "warmpool_cold_start", "outcome": "PASS",
            "sla_metrics": {"density_per_vcpu": 6.1}}]
    carry_prior_density(raw, prior)
    _check(raw[0]["sla_metrics"]["density_per_vcpu"] == 6.1,
           "fresh env-stamped density must win outright")


def test_density_pending_prior_not_carried():
    prior = [{"name": "warmpool_cold_start", "outcome": "pending",
              "sla_metrics": {"density_per_vcpu": 5.98}}]
    raw = [{"name": "warmpool_cold_start", "outcome": "PASS",
            "sla_metrics": {}}]
    carry_prior_density(raw, prior)
    _check("density_per_vcpu" not in raw[0]["sla_metrics"],
           "pending prior must not seed a density value")


def test_density_invalid_values_not_carried():
    for bad in (True, float("nan"), float("inf"), -1.0, "5.98", None):
        prior = [{"name": "warmpool_cold_start", "outcome": "PASS",
                  "sla_metrics": {"density_per_vcpu": bad}}]
        raw = [{"name": "warmpool_cold_start", "outcome": "PASS",
                "sla_metrics": {}}]
        carry_prior_density(raw, prior)
        _check("density_per_vcpu" not in raw[0]["sla_metrics"],
               f"invalid density {bad!r} must not be carried")


def test_density_malformed_inputs_tolerated():
    carry_prior_density([{"name": "x"}], None)
    carry_prior_density(["not-a-dict"], [{"name": "x"}])
    raw = [{"name": "x", "outcome": "PASS", "sla_metrics": {}}]
    carry_prior_density(raw, [{"name": "x", "outcome": "PASS",
                               "sla_metrics": "not-a-dict"}])
    _check(raw[0]["sla_metrics"] == {},
           "malformed prior sla_metrics must carry nothing")


def test_density_not_carried_onto_empty_zero_delivery_sla():
    # hb#700 Arm B fire #3 recovery incident: burst_create's zero-delivery
    # burst emits sla_metrics={} (its own honest-zero-measurement sentinel,
    # #546) — a genuinely empty dict must stay empty, never get seeded with a
    # prior fire's density_per_vcpu. The carry logic keyed only on "does the
    # fresh dict already have density_per_vcpu", so a valid measured prior
    # would silently turn {} into {"density_per_vcpu": <stale value>} — a
    # non-empty-but-count-less sla_metrics that downstream accrue_history.py
    # cannot distinguish from a real (partial) measurement, and crashed on.
    prior = [{"name": "burst_create", "outcome": "PASS",
              "sla_metrics": {"density_per_vcpu": 5.98,
                               "sandboxes_ready_under_1s": 4, "n": 10}}]
    raw = [{"name": "burst_create", "outcome": "PASS", "sla_metrics": {}}]
    carry_prior_density(raw, prior)
    _check(raw[0]["sla_metrics"] == {},
           f"empty zero-delivery sla_metrics must stay empty: "
           f"{raw[0]['sla_metrics']!r}")


def test_present_only_disclosure_key_vanishing_on_heal_passes():
    # hb#866: lever2_prescale_degraded is emitted ONLY while the lever-2
    # prescale ceiling is unreached (hb#863: as 1.0, since a bare bool is
    # dropped). A later fire that reaches a healthy ceiling correctly stops
    # emitting the key at all -- that is a degrade->heal IMPROVEMENT, not a
    # lost measurement, and must not gate.
    prior = [{
        "name": "warmpool_cold_start", "outcome": "PASS",
        "sla_metrics": {
            "lever2_prescale_degraded": 1.0,
            "warmpool_gate_cold_min_ms": 5000.0,
        },
    }]
    raw = [{
        "name": "warmpool_cold_start", "outcome": "PASS",
        "sla_metrics": {
            "warmpool_gate_cold_min_ms": 5000.0,
        },
    }]
    _check(check_cell_downgrade(raw, prior) == [],
           "a present-only disclosure key vanishing on heal must not gate")


def test_present_only_disclosure_key_exemption_does_not_cover_other_keys():
    # Guard the exemption's boundary: the same fire that healthily drops
    # lever2_prescale_degraded must NOT get a free pass on an unrelated,
    # genuinely lost metric key.
    prior = [{
        "name": "warmpool_cold_start", "outcome": "PASS",
        "sla_metrics": {
            "lever2_prescale_degraded": 1.0,
            "warmpool_gate_cold_min_ms": 5000.0,
        },
    }]
    raw = [{
        "name": "warmpool_cold_start", "outcome": "PASS",
        "sla_metrics": {},
    }]
    lines = check_cell_downgrade(raw, prior)
    _check(len(lines) == 1, f"expected 1 downgrade, got {lines!r}")
    _check("warmpool_gate_cold_min_ms" in lines[0]
           and "lever2_prescale_degraded" not in lines[0],
           f"unexpected line: {lines[0]!r}")


def test_non_registered_present_only_key_still_fires_on_loss():
    # Control: an ordinary metric key that happens to vanish is NOT exempt
    # merely by superficial resemblance (e.g. sharing a scenario with a
    # registered present-only key) -- only keys actually enumerated in
    # _PRESENT_ONLY_DISCLOSURE_KEYS get the pass.
    prior = [{
        "name": "warmpool_cold_start", "outcome": "PASS",
        "sla_metrics": {
            "lever2_prescale_degraded": 1.0,
            "lever2_prescale_ceiling_reached": 1.0,
        },
    }]
    raw = [{
        "name": "warmpool_cold_start", "outcome": "PASS",
        "sla_metrics": {},
    }]
    lines = check_cell_downgrade(raw, prior)
    _check(len(lines) == 1, f"expected 1 downgrade, got {lines!r}")
    _check("lever2_prescale_ceiling_reached" in lines[0]
           and "lever2_prescale_degraded" not in lines[0],
           f"unexpected line: {lines[0]!r}")


def main() -> int:
    tests = [
        test_key_loss_detected,
        test_outcome_downgrade_detected,
        test_row_drop_detected,
        test_clean_refresh_and_gains_pass,
        test_prior_pending_never_gates,
        test_fail_to_pass_is_not_a_downgrade,
        test_multiple_legs_reported_together,
        test_downgraded_names_populated,
        test_downgraded_names_none_by_default,
        test_measured_with_suffixes_key_loss_line,
        test_measured_with_suffixes_outcome_downgrade_line,
        test_measured_with_suffixes_row_drop_line,
        test_no_measured_with_leaves_line_unchanged,
        test_config_mismatch_key_loss_skipped_not_flagged,
        test_same_config_key_loss_still_fires,
        test_null_transition_with_recognized_reason_passes,
        test_null_transition_without_reason_fails_closed,
        test_null_transition_with_unrecognized_reason_fails_closed,
        test_null_transition_wrong_type_reason_fails_closed,
        test_null_transition_recognized_reason_covers_all_registered_keys,
        test_null_transition_partial_null_unrecognized_reason_flags_only_that_key,
        test_reason_field_vanishes_on_null_to_value_upgrade_passes,
        test_reason_field_lost_without_sibling_repopulation_still_fires,
        test_reason_field_exempt_only_covers_its_own_siblings,
        test_null_transition_skipped_on_config_mismatch,
        test_fresh_measured_with_surfaced_when_committed_has_none,
        test_malformed_inputs_tolerated,
        test_density_carried_onto_fresh_row,
        test_density_fresh_wins,
        test_density_pending_prior_not_carried,
        test_density_invalid_values_not_carried,
        test_density_malformed_inputs_tolerated,
        test_density_not_carried_onto_empty_zero_delivery_sla,
        test_present_only_disclosure_key_vanishing_on_heal_passes,
        test_present_only_disclosure_key_exemption_does_not_cover_other_keys,
        test_non_registered_present_only_key_still_fires_on_loss,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
    if failed:
        print(f"{failed}/{len(tests)} FAILED")
        return 1
    print(f"all {len(tests)} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
