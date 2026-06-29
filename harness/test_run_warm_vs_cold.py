"""Offline tests for run.maybe_warm_vs_cold / carry_prior_warm_vs_cold — no cluster,
no I/O beyond a self-managed tempfile.

Run with bare python3 (no pytest, so the auto-refresh GH-runner needs nothing
extra):  python3 -m harness.test_run_warm_vs_cold
or directly:               python3 harness/test_run_warm_vs_cold.py

These assert the default-off, dual-gated wiring of the warm-vs-cold speedup producer
into the run loop (#1018). Unlike scale_proof (its own sweep) and stepup (an
out-of-process file), BOTH warm-vs-cold legs come from cells already in this
in-process suite: the warm leg is burst_create's per-claim warm-pool TTFE sample
list (surfaced under the reserved `warm_ttfe_samples_ms` key), the cold leg is
native_digest_cold's single cold TTFE sample (its emitted `ttfe_p50_ms`). Both are
only present when BENCH_TTFE_EXEC armed the literal-TTFE path, so the gate is:
BENCH_TTFE_EXEC armed AND product=="sandbox", fail-closed otherwise; a missing leg
or any classifier honesty-gate failure (semantic/runtime-class mismatch, empty/
corrupt warm, non-positive cold) collapses to None so no warm_vs_cold key is emitted
(cell renders pending, not a fabricated ratio).
"""

from __future__ import annotations

# Make this file runnable BOTH as `python3 harness/test_x.py` and
# `python3 -m harness.test_x` by putting the repo root on sys.path before
# the absolute `from harness import ...` below (mirrors test_run_stepup.py).
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import json
import os
import pathlib
import tempfile

from harness import run


def _check(cond, msg):
    if not cond:
        raise AssertionError(msg)


# A minimal raw suite: one burst_create cell carrying the reserved warm-sample list,
# one native_digest_cold cell whose emitted sla_metrics carries the single cold TTFE
# sample. warm p50 of [200, 400, 600] = 400; cold ttfe_p50_ms = 3000 -> speedup 7.5.
def _raw(warm_samples=(200.0, 400.0, 600.0), cold_ttfe_p50=3000.0,
         with_burst=True, with_cold=True):
    cells = []
    if with_burst:
        burst = {"name": "burst_create", "outcome": "pass", "sla_metrics": {}}
        if warm_samples is not None:
            burst["warm_ttfe_samples_ms"] = list(warm_samples)
        cells.append(burst)
    if with_cold:
        cold_sla = {}
        if cold_ttfe_p50 is not None:
            cold_sla["ttfe_p50_ms"] = cold_ttfe_p50
        cells.append({"name": "native_digest_cold", "outcome": "pass",
                      "sla_metrics": cold_sla})
    return cells


def _with(product, raw, *, armed=True, warm_rc="gvisor", cold_rc="gvisor"):
    """Run maybe_warm_vs_cold under a given env, restoring os.environ after."""
    keys = ("BENCH_TTFE_EXEC", "BURST_CREATE_RUNTIME_CLASS",
            "NATIVE_DIGEST_COLD_RUNTIME_CLASS")
    saved = {k: os.environ.get(k) for k in keys}
    try:
        if armed:
            os.environ["BENCH_TTFE_EXEC"] = "1"
        else:
            os.environ.pop("BENCH_TTFE_EXEC", None)
        os.environ["BURST_CREATE_RUNTIME_CLASS"] = warm_rc
        os.environ["NATIVE_DIGEST_COLD_RUNTIME_CLASS"] = cold_rc
        return run.maybe_warm_vs_cold(product, raw)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# --- gate tests: default-off + dual-gate ---

def test_unarmed_returns_none():
    _check(_with("sandbox", _raw(), armed=False) is None,
           "BENCH_TTFE_EXEC unset -> None")


def test_non_sandbox_product_fail_closed():
    result = _with("substrate", _raw())
    _check(result is None, f"product=substrate -> None (fail-closed), got {result!r}")


def test_missing_burst_cell_returns_none():
    result = _with("sandbox", _raw(with_burst=False))
    _check(result is None, f"no burst_create cell -> None, got {result!r}")


def test_missing_cold_cell_returns_none():
    result = _with("sandbox", _raw(with_cold=False))
    _check(result is None, f"no native_digest_cold cell -> None, got {result!r}")


def test_missing_warm_samples_returns_none():
    result = _with("sandbox", _raw(warm_samples=None))
    _check(result is None, f"burst_create without warm samples -> None, got {result!r}")


def test_missing_cold_sample_returns_none():
    result = _with("sandbox", _raw(cold_ttfe_p50=None))
    _check(result is None, f"cold cell without ttfe_p50_ms -> None, got {result!r}")


def test_empty_warm_samples_returns_none():
    # An armed fire with an empty warm list is a classifier honesty-gate failure.
    result = _with("sandbox", _raw(warm_samples=[]))
    _check(result is None, f"empty warm samples -> None, got {result!r}")


def test_runtime_class_mismatch_returns_none():
    # gVisor-warm vs runc-cold is apples-to-oranges: the parity gate refuses.
    result = _with("sandbox", _raw(), warm_rc="gvisor", cold_rc="runc")
    _check(result is None, f"runtime-class mismatch -> None, got {result!r}")


def test_falsy_runtime_class_returns_none():
    # An empty runtime class (unset env knob) leaves the comparison basis undefined.
    result = _with("sandbox", _raw(), warm_rc="", cold_rc="")
    _check(result is None, f"empty runtime class -> None, got {result!r}")


# --- armed happy path: both legs present + parity-clean -> inner object ---

def test_armed_sandbox_classifies():
    result = _with("sandbox", _raw())
    _check(isinstance(result, dict) and result, f"armed -> inner dict, got {result!r}")
    _check(result["warm_p50_ms"] == 400.0, f"warm p50, got {result.get('warm_p50_ms')!r}")
    _check(result["cold_ms"] == 3000.0, f"cold passthrough, got {result.get('cold_ms')!r}")
    _check(result["speedup"] == 7.5, f"3000/400 = 7.5x, got {result.get('speedup')!r}")
    _check(result["semantic"] == "ttfe", f"semantic ttfe, got {result.get('semantic')!r}")
    _check(result["runtime_class"] == "gvisor",
           f"runtime_class gvisor, got {result.get('runtime_class')!r}")
    _check(result["n_warm"] == 3, f"n_warm=3, got {result.get('n_warm')!r}")


def test_armed_kata_runtime_classifies():
    # kata-microvm is the other published runtime class; parity-clean -> classifies.
    result = _with("sandbox", _raw(), warm_rc="kata-microvm", cold_rc="kata-microvm")
    _check(isinstance(result, dict) and result["runtime_class"] == "kata-microvm",
           f"kata-microvm classifies, got {result!r}")


# --- carry_prior_warm_vs_cold (#1018): persist across the daily refresh ---

_GEN_AT = "2026-06-29T12:00:00Z"
_FRESH = {"warm_p50_ms": 400.0, "cold_ms": 3000.0, "speedup": 7.5,
          "semantic": "ttfe", "runtime_class": "gvisor", "n_warm": 3}
_PRIOR = {"warm_p50_ms": 500.0, "cold_ms": 2000.0, "speedup": 4.0,
          "semantic": "ttfe", "runtime_class": "gvisor", "n_warm": 5,
          "measured_at": "2026-06-28T03:46:01Z"}


def test_carry_fresh_wins_and_is_stamped():
    out = run.carry_prior_warm_vs_cold(dict(_FRESH), _PRIOR, generated_at=_GEN_AT)
    _check(out["speedup"] == 7.5, "fresh wins")
    _check(out["measured_at"] == _GEN_AT,
           f"fresh stamps measured_at=generated_at, got {out.get('measured_at')!r}")
    _check("measured_at" not in _FRESH, "must not mutate the input fresh dict")


def test_carry_fresh_preexisting_measured_at_respected():
    fresh = dict(_FRESH)
    fresh["measured_at"] = "2025-01-01T00:00:00Z"
    out = run.carry_prior_warm_vs_cold(fresh, _PRIOR, generated_at=_GEN_AT)
    _check(out["measured_at"] == "2025-01-01T00:00:00Z",
           "a fresh dict that already carries measured_at keeps it (setdefault)")


def test_carry_no_fresh_carries_prior_unchanged():
    out = run.carry_prior_warm_vs_cold(None, _PRIOR, generated_at=_GEN_AT)
    _check(out == _PRIOR, f"no fresh -> carry prior verbatim, got {out!r}")
    _check(out["measured_at"] == "2026-06-28T03:46:01Z",
           "carried block keeps its ORIGINAL measured_at, not generated_at")


def test_carry_empty_fresh_carries_prior():
    out = run.carry_prior_warm_vs_cold({}, _PRIOR, generated_at=_GEN_AT)
    _check(out == _PRIOR, f"empty fresh ({{}}) -> carry prior, got {out!r}")


def test_carry_no_fresh_no_prior_is_none():
    out = run.carry_prior_warm_vs_cold(None, None, generated_at=_GEN_AT)
    _check(out is None, f"no fresh + no prior -> None (cell absent), got {out!r}")


# --- _read_prior_warm_vs_cold: best-effort top-level read ---

def _read_with(file_content):
    """Write file_content (dict->json, str->verbatim, None->no file) and read back."""
    fd, tmp = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w") as fh:
            if isinstance(file_content, str):
                fh.write(file_content)
            elif file_content is not None:
                json.dump(file_content, fh)
        if file_content is None:
            pathlib.Path(tmp).unlink(missing_ok=True)
        return run._read_prior_warm_vs_cold(pathlib.Path(tmp))
    finally:
        pathlib.Path(tmp).unlink(missing_ok=True)


def test_read_prior_missing_file_is_none():
    _check(_read_with(None) is None, "missing file -> None")


def test_read_prior_malformed_is_none():
    _check(_read_with("{not valid json") is None, "malformed JSON -> None")


def test_read_prior_absent_key_is_none():
    _check(_read_with({"scenarios": []}) is None, "no warm_vs_cold key -> None")


def test_read_prior_present_returns_block():
    out = _read_with({"warm_vs_cold": _PRIOR, "scenarios": []})
    _check(out == _PRIOR, f"present warm_vs_cold returned, got {out!r}")


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
