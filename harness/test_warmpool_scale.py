"""Offline lock for warmpool_cold_start's large-N concurrent-fire knobs.

Run with bare python3 (no pytest, so the auto-refresh GH-runner needs nothing
extra):  python3 -m harness.test_warmpool_scale
or directly:               python3 harness/test_warmpool_scale.py

WHY THIS EXISTS — the warm-pool scenario is the producer for BOTH the
"Warm-pool hit" matrix cell (POOL_REPLICAS=N, every claim served warm) AND the
all-cold matrix cell (POOL_REPLICAS=0, every claim overflows to a cold-provision
— the same path the 5-warm/5-cold default exercises for its cold tier). Two
properties the large-N fire depends on are pure (no cluster) and locked here:

  test_timeouts_default_to_canonical
    With no env override the warm-up and per-claim-bind ceilings are the canonical
    180s — the small-N shape is byte-identical to its pre-scale behavior.

  test_timeouts_honor_env_override
    A large-N fire (300/500 claims) MUST be able to raise both ceilings: warming
    300 gVisor pods, or cold-provisioning 300 concurrent claims on a finite node
    pool, legitimately exceeds 180s. If a future edit drops the env read, the
    scale fire silently reverts to 180s and times out — this test fails first.

  test_pool_replicas_zero_is_a_valid_cold_burst
    POOL_REPLICAS=0 is the all-cold producer: the warm-pool warmup wait
    short-circuits immediately (readyReplicas>=0 is true on the first poll), so a
    0-replica pool never blocks on warmup, and every fired claim cold-provisions.
"""

from __future__ import annotations

import importlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _reload_with_env(**env) -> object:
    """Reload the scenario module under a temporary env, restoring it after."""
    keys = (
        "WARMPOOL_COLD_START_WARMUP_TIMEOUT_S",
        "WARMPOOL_COLD_START_BIND_TIMEOUT_S",
        "WARMPOOL_COLD_START_POOL_REPLICAS",
        "WARMPOOL_COLD_START_CLAIM_COUNT",
    )
    saved = {k: os.environ.get(k) for k in keys}
    try:
        for k in keys:
            os.environ.pop(k, None)
        for k, v in env.items():
            os.environ[k] = str(v)
        import harness.scenarios.warmpool_cold_start as w
        return importlib.reload(w)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_timeouts_default_to_canonical():
    w = _reload_with_env()
    assert w._WARMUP_TIMEOUT_S == 180, w._WARMUP_TIMEOUT_S
    assert w._BIND_TIMEOUT_S == 180, w._BIND_TIMEOUT_S


def test_timeouts_honor_env_override():
    w = _reload_with_env(
        WARMPOOL_COLD_START_WARMUP_TIMEOUT_S=600,
        WARMPOOL_COLD_START_BIND_TIMEOUT_S=900,
    )
    assert w._WARMUP_TIMEOUT_S == 600, w._WARMUP_TIMEOUT_S
    assert w._BIND_TIMEOUT_S == 900, w._BIND_TIMEOUT_S


def test_pool_replicas_zero_is_a_valid_cold_burst():
    w = _reload_with_env(
        WARMPOOL_COLD_START_POOL_REPLICAS=0,
        WARMPOOL_COLD_START_CLAIM_COUNT=300,
    )
    assert w._POOL_REPLICAS == 0, w._POOL_REPLICAS
    assert w._CLAIM_COUNT == 300, w._CLAIM_COUNT
    # The warmup wait gates on `readyReplicas >= target_ready`; with target_ready=0
    # the first poll satisfies it, so a 0-replica pool never blocks on warmup.
    import inspect
    src = inspect.getsource(w._wait_for_pool_warm)
    assert "if ready >= target_ready:" in src, src


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
