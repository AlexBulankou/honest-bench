"""WS3 (epic #6669) — build_provenance stamps `upstream_ref` from BENCH_UPSTREAM_REF.

Env-passthrough-or-omit posture (same as machine_type / node_image): a set env stamps the
key; unset / blank env omits it entirely (never guessed). Run-level, so both substrate and
sandbox products carry it. Dependency-free: `python3 harness/test_upstream_ref_stamp.py`.
"""

from __future__ import annotations

# Make this file runnable BOTH as `python3 harness/test_x.py` and `-m harness.test_x`
# (mirrors test_run_finalize_node_count.py).
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import os

from harness import run


def _check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _prov_with_env(value, substrate="gke-sandbox", product="sandbox"):
    """Call build_provenance with BENCH_UPSTREAM_REF set/unset; restore after."""
    saved = os.environ.get("BENCH_UPSTREAM_REF")
    try:
        if value is None:
            os.environ.pop("BENCH_UPSTREAM_REF", None)
        else:
            os.environ["BENCH_UPSTREAM_REF"] = value
        return run.build_provenance(substrate, product)
    finally:
        if saved is None:
            os.environ.pop("BENCH_UPSTREAM_REF", None)
        else:
            os.environ["BENCH_UPSTREAM_REF"] = saved


def test_stamps_from_valid_env():
    prov = _prov_with_env("v0.5.5-66-g575b05f9")
    _check(prov.get("upstream_ref") == "v0.5.5-66-g575b05f9",
           f"expected upstream_ref stamped, got {prov.get('upstream_ref')!r}")


def test_unset_env_omits_key():
    prov = _prov_with_env(None)
    _check("upstream_ref" not in prov,
           "unset BENCH_UPSTREAM_REF must omit the key, not stamp a default")


def test_blank_env_omits_key():
    prov = _prov_with_env("   ")
    _check("upstream_ref" not in prov,
           "blank/whitespace BENCH_UPSTREAM_REF must be stripped to empty and omitted")


def test_strips_surrounding_whitespace():
    prov = _prov_with_env("  v0.5.5  ")
    _check(prov.get("upstream_ref") == "v0.5.5",
           f"expected stripped 'v0.5.5', got {prov.get('upstream_ref')!r}")


def test_stamped_on_substrate_product_too():
    # Run-level, not sandbox-gated: a substrate run also carries the pin.
    prov = _prov_with_env("v0.5.5", substrate="substrate", product="substrate")
    _check(prov.get("upstream_ref") == "v0.5.5",
           "upstream_ref must stamp for substrate too (run-level, not sandbox-gated)")


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
