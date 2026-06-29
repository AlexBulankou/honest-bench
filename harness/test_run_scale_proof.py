"""Offline tests for run.maybe_scale_proof — no cluster, no I/O.

Run with bare python3 (no pytest, so the auto-refresh GH-runner needs nothing
extra):  python3 -m harness.test_run_scale_proof
or directly:               python3 harness/test_run_scale_proof.py

These assert the default-off, dual-gated wiring of the Scale Proof producer into
the run loop (#3949): run_sweep (the heavy MUTATING multi-K cluster fire) is
invoked ONLY when BENCH_SCALE_SLOPE=1 AND product=="sandbox", fail-closed for any
other product, and an empty sweep ({}) collapses to None so no scale_proof key is
emitted. run_sweep is monkeypatched to a sentinel so the gate is exercised without
a cluster — and the not-armed paths assert it is NEVER called.
"""

from __future__ import annotations

import os

from . import run
from .scenarios import scale_slope


def _check(cond, msg):
    if not cond:
        raise AssertionError(msg)


class _Spy:
    """Records whether run_sweep was invoked; returns a preset value."""

    def __init__(self, ret):
        self.ret = ret
        self.called = False

    def __call__(self, *a, **k):
        self.called = True
        return self.ret


def _with(flag, product, sweep_ret):
    """Run maybe_scale_proof under a given flag/product, with run_sweep spied.

    Returns (result, spy.called). Restores os.environ + the patched attribute.
    """
    saved_env = os.environ.get("BENCH_SCALE_SLOPE")
    saved_sweep = scale_slope.run_sweep
    spy = _Spy(sweep_ret)
    try:
        if flag is None:
            os.environ.pop("BENCH_SCALE_SLOPE", None)
        else:
            os.environ["BENCH_SCALE_SLOPE"] = flag
        scale_slope.run_sweep = spy
        result = run.maybe_scale_proof(product)
        return result, spy.called
    finally:
        scale_slope.run_sweep = saved_sweep
        if saved_env is None:
            os.environ.pop("BENCH_SCALE_SLOPE", None)
        else:
            os.environ["BENCH_SCALE_SLOPE"] = saved_env


_PROOF = {"scale_points": [{"node_count": 1, "density": 1.88},
                           {"node_count": 2, "density": 1.88}],
          "density_retention": 1.0, "thpt_retention": 1.0}


def test_flag_off_sandbox_returns_none_and_does_not_fire():
    result, called = _with(None, "sandbox", _PROOF)
    _check(result is None, f"flag absent -> None, got {result!r}")
    _check(not called, "flag off must NOT invoke the heavy sweep")


def test_flag_explicitly_off_returns_none_and_does_not_fire():
    result, called = _with("0", "sandbox", _PROOF)
    _check(result is None, f"flag=0 -> None, got {result!r}")
    _check(not called, "flag=0 must NOT invoke the heavy sweep")


def test_non_sandbox_product_fail_closed_even_when_flag_on():
    result, called = _with("1", "substrate", _PROOF)
    _check(result is None, f"product=substrate -> None (fail-closed), got {result!r}")
    _check(not called, "non-sandbox product must NOT invoke the sweep even with flag on")


def test_armed_sandbox_returns_proof():
    result, called = _with("1", "sandbox", _PROOF)
    _check(called, "armed (flag on + sandbox) MUST invoke the sweep")
    _check(result == _PROOF, f"armed -> the sweep's proof, got {result!r}")


def test_armed_but_empty_sweep_collapses_to_none():
    # A single-node cluster yields only K=1 -> classifier returns {} -> None here,
    # so the emitter omits the scale_proof key (table absent, not a partial lie).
    result, called = _with("1", "sandbox", {})
    _check(called, "armed path still invokes the sweep")
    _check(result is None, f"empty sweep ({{}}) collapses to None, got {result!r}")


def test_truthy_flag_variants_arm():
    for variant in ("1", "true", "TRUE", "yes", "on"):
        result, called = _with(variant, "sandbox", _PROOF)
        _check(called, f"flag={variant!r} should arm")
        _check(result == _PROOF, f"flag={variant!r} -> proof, got {result!r}")


# --- carry_prior_scale_proof (#3952): persist across the daily refresh ---

_GEN_AT = "2026-06-29T12:00:00Z"
_PRIOR = {"scale_points": [{"node_count": 1, "density": 5.18},
                           {"node_count": 4, "density": 5.18}],
          "density_retention": 1.0, "thpt_retention": 0.222,
          "measured_at": "2026-06-29T03:46:01Z"}


def test_carry_fresh_sweep_wins_and_is_stamped():
    out = run.carry_prior_scale_proof(dict(_PROOF), _PRIOR, generated_at=_GEN_AT)
    _check(out["scale_points"] == _PROOF["scale_points"], "fresh points must win")
    _check(out["measured_at"] == _GEN_AT,
           f"fresh sweep stamps measured_at=generated_at, got {out.get('measured_at')!r}")
    # original fresh dict not mutated
    _check("measured_at" not in _PROOF, "must not mutate the input fresh dict")


def test_carry_fresh_preexisting_measured_at_respected():
    fresh = dict(_PROOF)
    fresh["measured_at"] = "2025-01-01T00:00:00Z"
    out = run.carry_prior_scale_proof(fresh, _PRIOR, generated_at=_GEN_AT)
    _check(out["measured_at"] == "2025-01-01T00:00:00Z",
           "a fresh dict that already carries measured_at keeps it (setdefault)")


def test_carry_no_fresh_carries_prior_unchanged():
    out = run.carry_prior_scale_proof(None, _PRIOR, generated_at=_GEN_AT)
    _check(out == _PRIOR, f"no fresh sweep -> carry prior verbatim, got {out!r}")
    _check(out["measured_at"] == "2026-06-29T03:46:01Z",
           "carried block keeps its ORIGINAL measured_at, not generated_at")


def test_carry_empty_fresh_carries_prior():
    out = run.carry_prior_scale_proof({}, _PRIOR, generated_at=_GEN_AT)
    _check(out == _PRIOR, f"empty fresh ({{}}) -> carry prior, got {out!r}")


def test_carry_no_fresh_no_prior_is_none():
    out = run.carry_prior_scale_proof(None, None, generated_at=_GEN_AT)
    _check(out is None, f"no fresh + no prior -> None (table absent), got {out!r}")


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
