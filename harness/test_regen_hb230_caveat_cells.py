"""Offline tests for the hb#230 doctrine-flip regen producer.

Run with bare python3 (no pytest dependency, so the auto-refresh GH-runner needs
nothing extra):  python3 -m harness.test_regen_hb230_caveat_cells
or directly:      python3 harness/test_regen_hb230_caveat_cells.py

These assert the load-bearing properties of ``regen()``:
  * every converted cell's published number equals the PURE ``slo_rate``
    derivation on the SAME committed record the script reads (no drift between
    what the script stamps and what the derivation says),
  * every converted ``sla_metrics`` block survives the fail-closed build guard
    (``_coerce_sla_metrics``) — the same posture as the ingest path,
  * the drop/add set retires the right ``*_pend_reason`` keys and stamps the
    right basis/caveat triples,
  * the transform is IDEMPOTENT — re-applying the conversions to already-
    converted cells is a no-op (a second script run must not double-convert).
"""
from __future__ import annotations

import copy
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from harness import regen_hb230_caveat_cells as regen
from harness.results_schema import _coerce_sla_metrics
from harness.slo_rate import (
    SLO_BASIS_ACQ_P95_UNCORROBORATED,
    SLO_BASIS_COLD_FLOOR_ZERO,
    SLO_BASIS_LITERAL_ACQ,
    SLO_BASIS_UNRESOLVED_BOUNDS,
    _derive_acq_p95_uncorroborated,
    _derive_cold_floor_zero,
)


def _check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _cells():
    """Run the in-memory conversion once and return the two results dicts."""
    return regen.regen()


def _sc(results_dict, rel, name):
    return regen._scenario(results_dict[rel], name)


# --- 1. gVisor warm: 5s literal-clean, 1s acq-uncorroborated *** ------------
def test_gvisor_warm_1s_matches_derivation():
    out = _cells()
    acq = _derive_acq_p95_uncorroborated(
        regen._pareto(regen._load(regen.GVISOR_WARM_1S_RECORD))
    )
    sla = _sc(out, regen.GVISOR_LATEST, "warmpool_cold_start")["sla_metrics"]
    _check(
        sla["thpt_under_1s_per_cluster"] == acq["thpt_under_1s_per_cluster"],
        f"gVisor warm 1s per-cluster {sla.get('thpt_under_1s_per_cluster')} "
        f"!= derivation {acq['thpt_under_1s_per_cluster']}",
    )
    _check(sla["thpt_slo_basis_5s"] == SLO_BASIS_LITERAL_ACQ,
           "gVisor warm 5s basis must stay corroborated-literal (CLEAN)")
    _check(sla["thpt_slo_basis_1s"] == SLO_BASIS_ACQ_P95_UNCORROBORATED,
           "gVisor warm 1s basis must be acq-p95-uncorroborated (Class A ***)")
    _check("thpt_under_1s_per_cluster_pend_reason" not in sla,
           "gVisor warm 1s pend_reason must be retired")
    _check("thpt_slo_basis" not in sla,
           "whole-triple basis must be dropped in favor of per-bar bases")


# --- 2. gVisor cold: floor-zero over BOTH bars ------------------------------
def test_gvisor_cold_floor_zero_both_bars():
    out = _cells()
    node_count = int(
        regen._load(regen.GVISOR_COLD_NODE_COUNT_RECORD)["params"]["cluster_nodes"]
    )
    floor = _derive_cold_floor_zero(
        [regen._load(r) for r in regen.GVISOR_COLD_LEG_RECORDS], node_count
    )
    sla = _sc(out, regen.GVISOR_LATEST, "native_digest_cold")["sla_metrics"]
    _check(sla.get("thpt_slo_floor_zero") == 1,
           "gVisor cold must carry the floor-zero stamp")
    _check(sla["thpt_slo_basis"] == SLO_BASIS_COLD_FLOOR_ZERO,
           "gVisor cold basis must be cold-floor-zero")
    _check(sla["thpt_under_5s_per_cluster"] == 0.0
           and sla["thpt_under_1s_per_cluster"] == 0.0,
           "gVisor cold both per-cluster bars must be measured-0")
    _check(sla["thpt_cluster_node_count"] == floor["thpt_cluster_node_count"],
           "gVisor cold node_count must match the floor derivation")
    _check("thpt_under_5s_pend_reason" not in sla,
           "gVisor cold 5s pend_reason must be retired")


# --- 3. gVisor resume: top-level probe ceiling ------------------------------
def test_gvisor_resume_ceiling_top_level():
    out = _cells()
    sc = _sc(out, regen.GVISOR_LATEST, "suspend_resume")
    ceiling = sc.get("resume_probe_ceiling_ms")
    _check(isinstance(ceiling, float) and ceiling > 0,
           f"resume ceiling must be a positive float, got {ceiling!r}")
    _check(ceiling == float(sc["sla_metrics"]["ttfe_p50_ms"]),
           "resume ceiling must equal the probe's own recorded ttfe_p50_ms")
    # The operation never completed — outcome must stay pending.
    _check(sc.get("outcome") != "pass",
           "resume outcome must not be flipped to pass by the ceiling carry")


# --- 4. Kata warm: acq-uncorroborated BOTH bars, with node_count -----------
def test_kata_warm_acq_both_bars_with_node_count():
    out = _cells()
    record = regen._load(regen.KATA_WARM_RECORD)
    acq = _derive_acq_p95_uncorroborated(regen._pareto(record))
    node_count = int(record["params"]["cluster_nodes"])
    sla = _sc(out, regen.KATA_LATEST, "warmpool_cold_start")["sla_metrics"]
    _check(sla["thpt_under_5s_per_cluster"] == acq["thpt_under_5s_per_cluster"],
           "Kata warm 5s per-cluster must match derivation")
    _check(sla["thpt_under_1s_per_cluster"] == acq["thpt_under_1s_per_cluster"],
           "Kata warm 1s per-cluster must match derivation")
    _check(sla["thpt_slo_basis"] == SLO_BASIS_ACQ_P95_UNCORROBORATED,
           "Kata warm whole-triple basis must be acq-p95-uncorroborated")
    _check(sla.get("thpt_cluster_node_count") == node_count,
           "Kata warm must carry node_count so render publishes the per-cluster "
           "figure instead of falling back to pending (cluster-fire)")
    _check("thpt_under_5s_per_cluster_pend_reason" not in sla
           and "thpt_under_1s_per_cluster_pend_reason" not in sla,
           "Kata warm per-cluster pend_reasons must be retired")


# --- 5. Kata cold: 5s bar unresolved-bounds; 1s stays render-derived-0 -----
def test_kata_cold_unresolved_5s():
    out = _cells()
    sla = _sc(out, regen.KATA_LATEST, "native_digest_cold")["sla_metrics"]
    _check(sla["thpt_slo_basis_5s"] == SLO_BASIS_UNRESOLVED_BOUNDS,
           "Kata cold 5s basis must be unresolved-bounds")
    _check("thpt_under_5s_pend_reason" not in sla,
           "Kata cold 5s pend_reason must be retired")


# --- 6. Every converted cell survives the fail-closed build guard ----------
def test_all_converted_cells_survive_coercion():
    out = _cells()
    targets = [
        (regen.GVISOR_LATEST, "warmpool_cold_start"),
        (regen.GVISOR_LATEST, "native_digest_cold"),
        (regen.KATA_LATEST, "warmpool_cold_start"),
        (regen.KATA_LATEST, "native_digest_cold"),
    ]
    for rel, name in targets:
        sla = _sc(out, rel, name)["sla_metrics"]
        # Must not raise — same posture as the ingest path.
        guarded = _coerce_sla_metrics(sla)
        _check(guarded == sla,
               f"{rel}:{name} sla_metrics is not already guard-canonical "
               "(regen must write the guard's output verbatim)")


# --- 7. Idempotent — a second conversion pass is a no-op -------------------
def test_idempotent():
    out = _cells()
    gvisor2 = copy.deepcopy(out[regen.GVISOR_LATEST])
    kata2 = copy.deepcopy(out[regen.KATA_LATEST])
    regen.convert_gvisor_warm(gvisor2)
    regen.convert_gvisor_cold(gvisor2)
    regen.convert_gvisor_resume(gvisor2)
    regen.convert_kata_warm(kata2)
    regen.convert_kata_cold(kata2)
    _check(gvisor2 == out[regen.GVISOR_LATEST],
           "re-applying gVisor conversions changed the result — not idempotent")
    _check(kata2 == out[regen.KATA_LATEST],
           "re-applying Kata conversions changed the result — not idempotent")


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
