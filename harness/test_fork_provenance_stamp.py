"""WS4(c) (epic #6669) — build_provenance stamps the three fork-build provenance parts.

When the controller under test was built from alex's fork (not the upstream-published image),
build_provenance stamps three closed-schema parts the renderer composes into
`fork@<sha> (+N fixes over upstream@<base>)`:
  BENCH_FORK_SHA               -> fork_sha
  BENCH_FORK_BASE_UPSTREAM_SHA -> fork_base_upstream_sha
  BENCH_FORK_FIX_COUNT         -> fork_fix_count (int-parsed)

Env-passthrough-or-omit posture (same as upstream_ref): a set env stamps the key; unset /
blank / non-int env omits it entirely (never guessed). Run-level, so both substrate and
sandbox products carry it. Dependency-free: `python3 harness/test_fork_provenance_stamp.py`.
"""

from __future__ import annotations

# Runnable BOTH as `python3 harness/test_x.py` and `-m harness.test_x`.
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import os

from harness import run

_FORK_ENV = ("BENCH_FORK_SHA", "BENCH_FORK_BASE_UPSTREAM_SHA", "BENCH_FORK_FIX_COUNT")


def _check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _prov_with_env(values, substrate="gke-sandbox", product="sandbox"):
    """Call build_provenance with the three fork env vars set per `values`; restore after.

    `values` is a dict mapping any of _FORK_ENV -> str (or the key absent to leave it unset).
    """
    saved = {k: os.environ.get(k) for k in _FORK_ENV}
    try:
        for k in _FORK_ENV:
            os.environ.pop(k, None)
        for k, v in values.items():
            os.environ[k] = v
        return run.build_provenance(substrate, product)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_stamps_all_three_from_valid_env():
    prov = _prov_with_env({
        "BENCH_FORK_SHA": "79203112",
        "BENCH_FORK_BASE_UPSTREAM_SHA": "2b3a4715",
        "BENCH_FORK_FIX_COUNT": "2",
    })
    _check(prov.get("fork_sha") == "79203112", f"fork_sha: {prov.get('fork_sha')!r}")
    _check(prov.get("fork_base_upstream_sha") == "2b3a4715",
           f"fork_base_upstream_sha: {prov.get('fork_base_upstream_sha')!r}")
    _check(prov.get("fork_fix_count") == 2,
           f"fork_fix_count must be int 2, got {prov.get('fork_fix_count')!r}")


def test_fix_count_parsed_as_int_not_str():
    prov = _prov_with_env({
        "BENCH_FORK_SHA": "abc1234",
        "BENCH_FORK_BASE_UPSTREAM_SHA": "def5678",
        "BENCH_FORK_FIX_COUNT": "7",
    })
    _check(isinstance(prov.get("fork_fix_count"), int) and not isinstance(prov.get("fork_fix_count"), bool),
           f"fork_fix_count must be a non-bool int, got {type(prov.get('fork_fix_count'))}")


def test_unset_env_omits_all_keys():
    prov = _prov_with_env({})
    for k in ("fork_sha", "fork_base_upstream_sha", "fork_fix_count"):
        _check(k not in prov, f"unset fork env must omit {k}, not stamp a default")


def test_blank_env_omits_keys():
    prov = _prov_with_env({
        "BENCH_FORK_SHA": "   ",
        "BENCH_FORK_BASE_UPSTREAM_SHA": "",
        "BENCH_FORK_FIX_COUNT": "  ",
    })
    for k in ("fork_sha", "fork_base_upstream_sha", "fork_fix_count"):
        _check(k not in prov, f"blank fork env must omit {k}")


def test_non_int_fix_count_omitted_other_two_kept():
    # A malformed fix-count must not crash and must not stamp; the two sha parts still stamp.
    prov = _prov_with_env({
        "BENCH_FORK_SHA": "abc1234",
        "BENCH_FORK_BASE_UPSTREAM_SHA": "def5678",
        "BENCH_FORK_FIX_COUNT": "not-a-number",
    })
    _check("fork_fix_count" not in prov, "non-int fix count must be omitted, not stamped")
    _check(prov.get("fork_sha") == "abc1234", "fork_sha must still stamp alongside a bad count")
    _check(prov.get("fork_base_upstream_sha") == "def5678",
           "fork_base_upstream_sha must still stamp alongside a bad count")


def test_partial_env_stamps_only_present_parts():
    # Only fork_sha set: the other two omit. (The renderer requires all three, but stamping
    # is per-part omit-when-absent — the compose gate lives in render, not here.)
    prov = _prov_with_env({"BENCH_FORK_SHA": "abc1234"})
    _check(prov.get("fork_sha") == "abc1234", "fork_sha must stamp when set alone")
    _check("fork_base_upstream_sha" not in prov, "unset base must omit")
    _check("fork_fix_count" not in prov, "unset count must omit")


def test_stamped_on_substrate_product_too():
    prov = _prov_with_env({
        "BENCH_FORK_SHA": "79203112",
        "BENCH_FORK_BASE_UPSTREAM_SHA": "2b3a4715",
        "BENCH_FORK_FIX_COUNT": "2",
    }, substrate="substrate", product="substrate")
    _check(prov.get("fork_sha") == "79203112",
           "fork parts must stamp for substrate too (run-level, not sandbox-gated)")


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
