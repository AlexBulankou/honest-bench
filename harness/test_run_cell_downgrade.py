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
        test_malformed_inputs_tolerated,
        test_density_carried_onto_fresh_row,
        test_density_fresh_wins,
        test_density_pending_prior_not_carried,
        test_density_invalid_values_not_carried,
        test_density_malformed_inputs_tolerated,
        test_density_not_carried_onto_empty_zero_delivery_sla,
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
