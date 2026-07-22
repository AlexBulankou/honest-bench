"""Offline tests for hb#5414's refresh-delta / verdict-flip tripwire — the harness-side
half (reader + build_provenance stamping + schema round-trip). No cluster, no I/O beyond
self-managed tempfiles.

Run with bare python3:
  python3 -m harness.test_run_north_star_delta_5414
or directly:
  python3 harness/test_run_north_star_delta_5414.py

Why this file exists: mirrors PR#313's prior_machine_type carry-forward tests in
test_run_carry_forward_112.py, but for a distinct field/mechanism (hb#5414, not #112's
six producer-less blocks) — kept in its own file rather than appended to the #112 file's
scope. The render-side half (the >2x-delta / verdict-flip caveat itself) is tested in
render/test_render.py under the "hb#5414: refresh-delta / verdict-flip tripwire" section,
which can see both harness and render schemas across the sys.path split.
"""

from __future__ import annotations

import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import json
import pathlib
import tempfile

from harness import results_schema as rs
from harness import run


def _check(cond, msg):
    if not cond:
        raise AssertionError(msg)


_GEN_AT = "2026-07-01T12:00:00Z"


# --- _read_prior_warmpool_ttfe_p95: best-effort read of the prior published p95 ---

def _read_with(file_content):
    fd, tmp = tempfile.mkstemp(suffix=".json")
    try:
        with _os.fdopen(fd, "w") as fh:
            if isinstance(file_content, str):
                fh.write(file_content)
            elif file_content is not None:
                json.dump(file_content, fh)
        if file_content is None:
            pathlib.Path(tmp).unlink(missing_ok=True)
        return run._read_prior_warmpool_ttfe_p95(pathlib.Path(tmp))
    finally:
        pathlib.Path(tmp).unlink(missing_ok=True)


def test_read_prior_missing_file_is_none():
    _check(_read_with(None) is None, "missing file -> None")


def test_read_prior_malformed_json_is_none():
    _check(_read_with("{not valid json") is None, "malformed JSON -> None")


def test_read_prior_no_scenarios_is_none():
    _check(_read_with({"product": "sandbox"}) is None, "absent scenarios key -> None")


def test_read_prior_no_warmpool_cold_start_scenario_is_none():
    _check(
        _read_with({"scenarios": [{"name": "native_digest_cold", "sla_metrics": {}}]}) is None,
        "no warmpool_cold_start entry -> None",
    )


def test_read_prior_absent_sla_metrics_is_none():
    _check(
        _read_with({"scenarios": [{"name": "warmpool_cold_start"}]}) is None,
        "warmpool_cold_start present but no sla_metrics -> None",
    )


def test_read_prior_absent_ttfe_p95_is_none():
    _check(
        _read_with({"scenarios": [
            {"name": "warmpool_cold_start", "sla_metrics": {"ttfe_p50_ms": 600}},
        ]}) is None,
        "sla_metrics present but no ttfe_p95_ms -> None",
    )


def test_read_prior_non_numeric_or_non_positive_ttfe_p95_is_none():
    for bad in ("900", True, 0, -5):
        _check(
            _read_with({"scenarios": [
                {"name": "warmpool_cold_start", "sla_metrics": {"ttfe_p95_ms": bad}},
            ]}) is None,
            f"non-numeric/non-positive ttfe_p95_ms={bad!r} -> None",
        )


def test_read_prior_present_returns_float():
    out = _read_with({"scenarios": [
        {"name": "warmpool_cold_start", "sla_metrics": {"ttfe_p95_ms": 900}},
    ]})
    _check(out == 900.0 and isinstance(out, float), f"present p95 returned as float, got {out!r}")


# --- build_provenance: stamps prior_warmpool_ttfe_p95_ms unconditionally (not "only if
# it differs", unlike prior_machine_type — the metric is expected to vary every run) ---

def test_build_provenance_no_prior_omits_field():
    prov = run.build_provenance("kind", "sandbox")
    _check("prior_warmpool_ttfe_p95_ms" not in prov, "no prior supplied -> field omitted")


def test_build_provenance_stamps_prior_even_when_unchanged():
    # Unlike prior_machine_type, this is stamped on every run with a prior value,
    # including a same-value refresh -- the renderer needs it every run to compute a delta.
    prov = run.build_provenance("kind", "sandbox", prior_warmpool_ttfe_p95=900.0)
    _check(prov.get("prior_warmpool_ttfe_p95_ms") == 900.0,
           f"prior stamped unconditionally, got {prov.get('prior_warmpool_ttfe_p95_ms')!r}")


def test_build_provenance_zero_or_none_prior_omits_field():
    for bad in (None, 0, 0.0):
        prov = run.build_provenance("kind", "sandbox", prior_warmpool_ttfe_p95=bad)
        _check("prior_warmpool_ttfe_p95_ms" not in prov,
               f"falsy prior={bad!r} -> field omitted")


def test_build_provenance_substrate_product_omits_field():
    # Sandbox-family only (same posture as node_image/runsc_version): substrate carries
    # no matrix runtime, and warmpool_cold_start is a sandbox-family-only scenario.
    prov = run.build_provenance("kind", "substrate", prior_warmpool_ttfe_p95=900.0)
    _check("prior_warmpool_ttfe_p95_ms" not in prov,
           "non-sandbox-family product omits prior_warmpool_ttfe_p95_ms")


# --- Round trip through the emitter (results_schema._coerce_provenance) ---

def _prov():
    return {"cluster_substrate": "gke-sandbox", "commit": "abc1234", "runner": "test"}


def test_build_provenance_prior_round_trips_through_emitter():
    prov = run.build_provenance("gke-sandbox", "sandbox", prior_warmpool_ttfe_p95=900.0)
    r = rs.build_results([], prov, _GEN_AT, product="sandbox")
    _check(r["provenance"]["prior_warmpool_ttfe_p95_ms"] == 900.0,
           "prior_warmpool_ttfe_p95_ms survives the emitter coercion")


def test_coerce_provenance_drops_non_numeric_or_non_positive():
    for bad in ("900", True, 0, -5, [900]):
        r = rs.build_results([], dict(_prov(), prior_warmpool_ttfe_p95_ms=bad), _GEN_AT)
        _check("prior_warmpool_ttfe_p95_ms" not in r["provenance"],
               f"non-numeric/non-positive value {bad!r} dropped by the emitter")


def test_coerce_provenance_coerces_int_to_float():
    r = rs.build_results([], dict(_prov(), prior_warmpool_ttfe_p95_ms=900), _GEN_AT)
    out = r["provenance"]["prior_warmpool_ttfe_p95_ms"]
    _check(out == 900.0 and isinstance(out, float), f"int coerced to float, got {out!r}")


def test_provenance_allow_list_includes_field():
    _check("prior_warmpool_ttfe_p95_ms" in rs.PROVENANCE_FIELDS,
           "prior_warmpool_ttfe_p95_ms declared in the allow-list")


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
