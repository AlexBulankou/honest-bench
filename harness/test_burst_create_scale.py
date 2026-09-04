"""Offline lock for burst_create's pre-fire fill-gate target (hb#813).

Run with bare python3 (no pytest, so the auto-refresh GH-runner needs nothing
extra):  python3 -m harness.test_burst_create_scale
or directly:               python3 harness/test_burst_create_scale.py

WHY THIS EXISTS -- burst_create's default config fires exactly as many claims
as the pool has slots (K==K), so `_fill_gate_target` is normally a no-op. But
BURST_CREATE_POOL_REPLICAS and BURST_CREATE_CLAIM_COUNT are independently
env-tunable, and a diagnostic fire sized below the pool (claim_count <
pool_replicas) would otherwise make the pre-fire gate wait for readyReplicas
to reach the raw pool size even though only claim_count of those slots will
ever be claimed -- burning warmup-timeout budget on replicas the burst never
touches. Same shape hb#804/hb#809 fixed in warmpool_cold_start's fill gate
(locked by harness/test_warmpool_scale.py); mirrored here for burst_create.

  test_fill_gate_target_capped_at_claim_count_for_undersized_burst
    A diagnostic fire with pool_replicas > claim_count must cap the pre-fire
    gate at claim_count, not the raw pool size.

  test_fill_gate_target_unaffected_for_committed_config
    The committed K==K config (pool_replicas == claim_count) is unaffected --
    the gate target is exactly the pool size, unchanged from pre-fix behavior.

  test_fill_gate_target_pool_replicas_le_zero_passthrough
    burst_create has no legitimate zero/negative pool mode (unlike
    warmpool_cold_start's all-cold cell), but the helper stays total: a
    pool_replicas<=0 input passes through untouched rather than raising.
"""

from __future__ import annotations

import importlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _reload_with_env(**env) -> object:
    """Reload the scenario module under a temporary env, restoring it after."""
    keys = (
        "BURST_CREATE_POOL_REPLICAS",
        "BURST_CREATE_CLAIM_COUNT",
    )
    saved = {k: os.environ.get(k) for k in keys}
    try:
        for k in keys:
            os.environ.pop(k, None)
        for k, v in env.items():
            os.environ[k] = str(v)
        import harness.scenarios.burst_create as b
        return importlib.reload(b)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_fill_gate_target_capped_at_claim_count_for_undersized_burst():
    # A diagnostic fire may size the pool ABOVE the claim burst
    # (pool_replicas > claim_count) to prove pool health independent of an
    # undersized burst. The pre-fire fill gate must be capped at claim_count
    # or it blocks waiting for readyReplicas to reach a raw pool size the
    # burst never drives.
    b = _reload_with_env(
        BURST_CREATE_POOL_REPLICAS=20,
        BURST_CREATE_CLAIM_COUNT=10,
    )
    assert b._fill_gate_target(20, 10) == 10


def test_fill_gate_target_unaffected_for_committed_config():
    # Committed K==K config (pool_replicas == claim_count, the scenario
    # default): fill gate still waits for the full raw pool size, unchanged
    # from pre-fix behavior.
    b = _reload_with_env(
        BURST_CREATE_POOL_REPLICAS=10,
        BURST_CREATE_CLAIM_COUNT=10,
    )
    assert b._fill_gate_target(10, 10) == 10


def test_fill_gate_target_pool_replicas_le_zero_passthrough():
    # No legitimate zero/negative pool mode for this scenario, but the
    # helper stays total -- pool_replicas<=0 passes through untouched.
    b = _reload_with_env()
    assert b._fill_gate_target(0, 40) == 0
    assert b._fill_gate_target(-1, 40) == -1


def _all_tests():
    return [
        v
        for k, v in sorted(globals().items())
        if k.startswith("test_") and callable(v)
    ]


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
