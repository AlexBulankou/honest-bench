"""Offline tests for burst_create's #3954 literal-TTFE corroboration helpers —
no cluster, no clock, no I/O.

Run with bare python3 (no pytest):  python3 -m harness.test_burst_create
or directly:                        python3 harness/test_burst_create.py

These cover the two PURE functions on the BENCH_TTFE_EXEC path:

  _assemble_probe_results — flattens the per-claim concurrent-probe deposits
    (one (ttfe_ms_or_None, exec_ok) per claim that bound) into the two parallel
    lists the classifier consumes. One exec_oks entry per claim FIRED; a claim
    absent from the deposit map (never bound / no pod / probe disabled) drags
    exec_success_rate honestly with exec_ok=False and NO latency sample.

  _classify_exec_corroboration — the additive corroboration emit. Returns {}
    on zero attempts (nothing to corroborate, no fabricated number); otherwise
    always returns both fields, even when the sub-1s exec count is 0 (a real
    "Ready but none usable under 1s" measurement). sandboxes_exec_under_1s is
    strictly the count of literal first-instruction latencies under the SAME
    sub-1s bar; exec_success_rate is the attempted-denominator success fraction.
"""

from __future__ import annotations

# Make this file runnable BOTH as `python3 harness/test_x.py` and
# `python3 -m harness.test_x` by putting the repo root on sys.path before
# the absolute `from harness import ...` below (mirrors test_warm_vs_cold.py).
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from harness.scenarios import burst_create as bc


def _check(cond, msg):
    if not cond:
        raise AssertionError(msg)


# --- _assemble_probe_results: flatten concurrent deposits -> parallel lists ---


def test_assemble_all_present_and_executed():
    out = bc._assemble_probe_results(
        ["a", "b", "c"],
        {"a": (420.0, True), "b": (980.0, True), "c": (1500.0, True)},
    )
    ttfe, oks = out
    _check(ttfe == [420.0, 980.0, 1500.0], f"all samples carried in order, got {ttfe!r}")
    _check(oks == [True, True, True], f"one ok per fired claim, got {oks!r}")


def test_assemble_missing_claim_is_attempted_failure_no_sample():
    # 'b' never bound (absent from deposit map) -> exec_ok False, NO latency.
    ttfe, oks = bc._assemble_probe_results(
        ["a", "b", "c"],
        {"a": (420.0, True), "c": (1500.0, True)},
    )
    _check(ttfe == [420.0, 1500.0], f"absent claim contributes no sample, got {ttfe!r}")
    _check(oks == [True, False, True], f"absent claim drags as False, got {oks!r}")


def test_assemble_present_failed_exec_no_sample():
    # 'b' bound but its exec failed (ttfe None, ok False) -> ok carried, no sample.
    ttfe, oks = bc._assemble_probe_results(
        ["a", "b"],
        {"a": (420.0, True), "b": (None, False)},
    )
    _check(ttfe == [420.0], f"failed-exec claim contributes no sample, got {ttfe!r}")
    _check(oks == [True, False], f"failed exec carried as False, got {oks!r}")


def test_assemble_present_ok_but_no_latency():
    # Defensive: a present (None, True) deposit carries the ok but no sample.
    ttfe, oks = bc._assemble_probe_results(["a"], {"a": (None, True)})
    _check(ttfe == [], f"no latency -> no sample even when ok, got {ttfe!r}")
    _check(oks == [True], f"ok carried, got {oks!r}")


def test_assemble_no_claims_empty_lists():
    ttfe, oks = bc._assemble_probe_results([], {})
    _check(ttfe == [] and oks == [], f"no claims -> two empty lists, got {(ttfe, oks)!r}")


def test_assemble_one_exec_ok_per_claim_fired():
    # The attempt-total invariant: len(exec_oks) == len(claim_names) ALWAYS,
    # regardless of how many bound/executed.
    names = ["a", "b", "c", "d"]
    _, oks = bc._assemble_probe_results(names, {"a": (1.0, True)})
    _check(len(oks) == len(names), f"one ok per fired claim, got {len(oks)} vs {len(names)}")


# --- _classify_exec_corroboration: the additive emit ---


def test_classify_no_attempts_returns_empty():
    out = bc._classify_exec_corroboration([], [], ttfi_ceiling_s=1.0)
    _check(out == {}, f"no attempts -> {{}} (no fabricated number), got {out!r}")


def test_classify_counts_under_ceiling():
    # 420 and 980 under 1000ms; 1500 over. exec_success_rate over 3 attempts.
    out = bc._classify_exec_corroboration(
        [420.0, 980.0, 1500.0], [True, True, True], ttfi_ceiling_s=1.0
    )
    _check(out[bc._KEY_EXEC_COUNT] == 2.0, f"two under 1s, got {out.get(bc._KEY_EXEC_COUNT)!r}")
    _check(out[bc._KEY_EXEC_RATE] == 1.0, f"all attempts ok, got {out.get(bc._KEY_EXEC_RATE)!r}")


def test_classify_count_is_float():
    out = bc._classify_exec_corroboration([420.0], [True], ttfi_ceiling_s=1.0)
    _check(isinstance(out[bc._KEY_EXEC_COUNT], float),
           f"count emitted as float, got {type(out[bc._KEY_EXEC_COUNT])}")


def test_classify_zero_under_ceiling_still_emits():
    # Ready but none usable under 1s: a REAL measurement, must still emit (not {}).
    out = bc._classify_exec_corroboration([1500.0, 2000.0], [True, True], ttfi_ceiling_s=1.0)
    _check(out[bc._KEY_EXEC_COUNT] == 0.0, f"zero under bar, got {out.get(bc._KEY_EXEC_COUNT)!r}")
    _check(out[bc._KEY_EXEC_RATE] == 1.0, "exec succeeded but slow -> rate 1.0")


def test_classify_success_rate_drags_on_failed_attempts():
    # 4 attempts, 2 ok -> 0.5. Only the 2 ok ones have samples, both under 1s.
    out = bc._classify_exec_corroboration(
        [420.0, 600.0], [True, True, False, False], ttfi_ceiling_s=1.0
    )
    _check(out[bc._KEY_EXEC_COUNT] == 2.0, f"two usable under 1s, got {out.get(bc._KEY_EXEC_COUNT)!r}")
    _check(out[bc._KEY_EXEC_RATE] == 0.5, f"2/4 attempts ok, got {out.get(bc._KEY_EXEC_RATE)!r}")


def test_classify_strict_less_than_ceiling():
    # Exactly at the ceiling does NOT count (strict <).
    out = bc._classify_exec_corroboration([1000.0], [True], ttfi_ceiling_s=1.0)
    _check(out[bc._KEY_EXEC_COUNT] == 0.0, "exactly-at-ceiling is not under (strict <)")


def test_classify_honors_ceiling_param():
    # A 1500ms sample counts under a 2s ceiling, not under a 1s ceiling.
    under_2s = bc._classify_exec_corroboration([1500.0], [True], ttfi_ceiling_s=2.0)
    under_1s = bc._classify_exec_corroboration([1500.0], [True], ttfi_ceiling_s=1.0)
    _check(under_2s[bc._KEY_EXEC_COUNT] == 1.0, "1500ms under 2s ceiling")
    _check(under_1s[bc._KEY_EXEC_COUNT] == 0.0, "1500ms NOT under 1s ceiling")


def test_assemble_then_classify_end_to_end():
    # The two compose: deposits -> lists -> corroboration. 'b' never bound.
    names = ["a", "b", "c"]
    deposits = {"a": (420.0, True), "c": (1500.0, True)}
    ttfe, oks = bc._assemble_probe_results(names, deposits)
    out = bc._classify_exec_corroboration(ttfe, oks, ttfi_ceiling_s=1.0)
    _check(out[bc._KEY_EXEC_COUNT] == 1.0, f"only a under 1s, got {out.get(bc._KEY_EXEC_COUNT)!r}")
    # 3 attempted (a,b,c), 2 ok (a,c) -> 0.6667.
    _check(out[bc._KEY_EXEC_RATE] == round(2 / 3, 4),
           f"2/3 attempts ok, got {out.get(bc._KEY_EXEC_RATE)!r}")


def _all_tests():
    return [v for k, v in sorted(globals().items())
            if k.startswith("test_") and callable(v)]


def main() -> int:
    failures = 0
    for t in _all_tests():
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {t.__name__}: {e}")
    print(f"\n{len(_all_tests()) - failures}/{len(_all_tests())} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
