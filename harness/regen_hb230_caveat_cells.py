#!/usr/bin/env python3
"""hb#230 doctrine-flip regen — stamp caveated MEASURED numbers into the five
activation-mode cells that were rendering honest-empty ``pending (...)``.

Doctrine (alex, hb#230): *a caveated measured number ALWAYS beats an empty cell.*
``pending`` survives ONLY where no measurement of any kind exists. Several
activation-mode cells DID have a recorded measurement (an upper-bound, an
uncorroborated acq rate, a cold floor over both bars, a resume probe ceiling)
but were withholding it behind a ``pending``/``*_pend_reason``. This script is
the data-only producer that converts those five cells to
``<measured> ***`` + upstream caveat.

Data-only, no fresh run:
  * reads the COMMITTED per-mode leg records under ``sandbox/records/`` — the
    source of truth for every derivation,
  * runs the PURE ``harness.slo_rate`` derivations (never a live cluster fire,
    so no new ``run_id`` is minted),
  * routes every emitted ``sla_metrics`` block through
    ``results_schema._coerce_sla_metrics`` — the same fail-closed build guard
    the ingest path uses, so an invalid construction RAISES here rather than
    publishing,
  * merges the caveat triples into the two committed ``results/latest.json``
    files (gVisor ``sandbox/`` + Kata ``sandbox-kata/``).

Idempotent: re-running reads the same records and re-applies the same
drop/add set, so a second run is a no-op on already-converted files.

The five conversions (see hb#230 / a4s1 rulings):
  1. gVisor warm  (warmpool_cold_start, sandbox/)      — per-bar basis split:
     5s stays corroborated-literal (CLEAN), 1s becomes acq-p95-uncorroborated
     (Class A ***); publish 1s per-cluster from the acq derivation.
  2. gVisor cold  (native_digest_cold, sandbox/)       — cold floor-zero over
     BOTH bars (measured-0 ***), from the leg-B cold sweep.
  3. gVisor resume(suspend_resume, sandbox/)           — top-level resume probe
     ceiling; render publishes ``>=N.Ns ***`` across the resume row.
  4. Kata warm    (warmpool_cold_start, sandbox-kata/) — acq-p95-uncorroborated
     both bars (Class A ***), from the Kata warm leg.
  5. Kata cold    (native_digest_cold, sandbox-kata/)  — 5s bar bracketed
     (unresolved-bounds ***); 1s stays render-derived-0.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Run as a module (``python3 -m harness.regen_hb230_caveat_cells``) so the
# package-relative imports resolve; guard with a friendly hint otherwise.
try:
    from harness.results_schema import _coerce_sla_metrics
    from harness.slo_rate import (
        SLO_BASIS_ACQ_P95_UNCORROBORATED,
        SLO_BASIS_LITERAL_ACQ,
        SLO_BASIS_UNRESOLVED_BOUNDS,
        _derive_acq_p95_uncorroborated,
        _derive_cold_floor_zero,
    )
except ImportError as exc:  # pragma: no cover - invocation hint only
    raise SystemExit(
        "run from the honest-bench root as: "
        "python3 -m harness.regen_hb230_caveat_cells"
    ) from exc

REPO_ROOT = Path(__file__).resolve().parent.parent

# --- source records (COMMITTED; the derivations' ground truth) ---------------
GVISOR_WARM_1S_RECORD = "sandbox/records/lowrate-dense-refire-10n-2026-07-04.json"
GVISOR_COLD_LEG_RECORDS = [
    "sandbox/records/permode-legB-gvisor-cold-r5-partial-2026-07-06.json",
    "sandbox/records/permode-legB-gvisor-cold-r10-partial-2026-07-06.json",
    "sandbox/records/permode-legB-gvisor-cold-r20-partial-2026-07-06.json",
]
# node_count for the gVisor cold floor-zero — from the sibling same-campaign warm
# leg (informational only; both floor-zero legs are 0.0 regardless of node_count,
# but the predicate requires a positive int to fire and stamps it as the "@X nodes"
# caption).
GVISOR_COLD_NODE_COUNT_RECORD = "sandbox/records/permode-legA-gvisor-warm-2026-07-06.json"
KATA_WARM_RECORD = "sandbox/records/permode-ladder-kata-warm-2026-07-08.json"
# The 07-08 ladder record's params.cluster_nodes carries the producer's stale
# prior-shape default (6); the ladder actually ran on the 2-node Kata pool.
# Carry the fire-time node_count explicitly rather than trusting the stale stamp.
KATA_WARM_NODE_COUNT = 2

GVISOR_LATEST = "sandbox/results/latest.json"
KATA_LATEST = "sandbox-kata/results/latest.json"


def _load(rel: str) -> dict:
    return json.loads((REPO_ROOT / rel).read_text())


def _pareto(record: dict) -> list:
    """Step-up records carry their Pareto rungs under ``literal_ttfe.pareto``."""
    pareto = (record.get("literal_ttfe") or {}).get("pareto")
    if not isinstance(pareto, list) or not pareto:
        raise ValueError("record missing literal_ttfe.pareto rungs")
    return pareto


def _scenario(results: dict, name: str) -> dict:
    for sc in results["scenarios"]:
        if sc.get("name") == name:
            return sc
    raise KeyError(f"scenario {name!r} not found in results")


def _guard(sla: dict) -> dict:
    """Route a built sla_metrics block through the fail-closed build guard.

    Returns the guard's numeric/basis-only view. An invalid construction
    (mixed basis, unpaired floor-zero, bad enum, ...) RAISES here — the same
    posture as the ingest path — so this script can never publish a block the
    schema would reject.
    """
    return _coerce_sla_metrics(sla)


def _apply(sla: dict, drop: tuple[str, ...], add: dict) -> dict:
    """Copy ``sla``, drop the named keys, merge ``add``, then guard-validate.

    The guarded output is what we write — canonical numeric/basis-only form,
    with every dropped ``*_pend_reason`` gone (the guard drops non-numeric,
    non-basis keys) and every added caveat key blessed by the schema.
    """
    built = {k: v for k, v in sla.items() if k not in drop}
    built.update(add)
    return _guard(built)


def convert_gvisor_warm(results: dict) -> None:
    """5s corroborated-literal (CLEAN) + 1s acq-p95-uncorroborated (Class A ***)."""
    acq = _derive_acq_p95_uncorroborated(_pareto(_load(GVISOR_WARM_1S_RECORD)))
    one_s = acq["thpt_under_1s_per_cluster"]
    assert one_s > 0, f"gVisor warm 1s acq derivation non-positive: {one_s}"
    sc = _scenario(results, "warmpool_cold_start")
    sc["sla_metrics"] = _apply(
        sc["sla_metrics"],
        drop=("thpt_slo_basis", "thpt_under_1s_per_cluster_pend_reason"),
        add={
            "thpt_slo_basis_5s": SLO_BASIS_LITERAL_ACQ,
            "thpt_slo_basis_1s": SLO_BASIS_ACQ_P95_UNCORROBORATED,
            "thpt_under_1s_per_cluster": one_s,
        },
    )


def convert_gvisor_cold(results: dict) -> None:
    """Cold floor-zero over BOTH bars (measured-0 ***)."""
    legs = [_load(r) for r in GVISOR_COLD_LEG_RECORDS]
    node_count = int(_load(GVISOR_COLD_NODE_COUNT_RECORD)["params"]["cluster_nodes"])
    floor = _derive_cold_floor_zero(legs, node_count)
    if not floor or floor.get("thpt_slo_floor_zero") != 1:
        raise ValueError(
            "gVisor cold floor-zero predicate did not fire on the committed leg-B "
            f"records @ node_count={node_count} — refusing to publish a 0 without it"
        )
    sc = _scenario(results, "native_digest_cold")
    sc["sla_metrics"] = _apply(
        sc["sla_metrics"],
        drop=("thpt_under_5s_pend_reason",),
        add=floor,
    )


def convert_gvisor_resume(results: dict) -> None:
    """Top-level resume probe ceiling (render publishes ``>=N.Ns ***``)."""
    sc = _scenario(results, "suspend_resume")
    # The probe waited out a never-clearing Suspended condition; its own recorded
    # ttfe_p50==ttfe_p95 IS the wall-clock ceiling it gave up at. Carry it
    # TOP-LEVEL so it survives render's pending-cell metric suppression. Outcome
    # stays pending — the operation never completed.
    ceiling = sc["sla_metrics"]["ttfe_p50_ms"]
    assert ceiling > 0, f"resume ceiling non-positive: {ceiling}"
    sc["resume_probe_ceiling_ms"] = float(ceiling)


def convert_kata_warm(results: dict) -> None:
    """acq-p95-uncorroborated, both bars (Class A ***), whole-triple basis."""
    record = _load(KATA_WARM_RECORD)
    acq = _derive_acq_p95_uncorroborated(_pareto(record))
    five_s = acq["thpt_under_5s_per_cluster"]
    one_s = acq["thpt_under_1s_per_cluster"]
    assert five_s > 0 and one_s > 0, f"Kata warm acq non-positive: {acq}"
    # The acq derivation returns per-CLUSTER rate keys only; render gates the
    # per-cluster figure behind thpt_cluster_node_count, so carry the fire-time
    # node_count explicitly or the per-cluster value falls back to
    # ``pending (cluster-fire)`` (same node_count carry the gVisor cold
    # floor-zero derivation stamps for its per-cluster bars).
    node_count = KATA_WARM_NODE_COUNT
    sc = _scenario(results, "warmpool_cold_start")
    sc["sla_metrics"] = _apply(
        sc["sla_metrics"],
        drop=(
            "thpt_under_5s_per_cluster_pend_reason",
            "thpt_under_1s_per_cluster_pend_reason",
        ),
        add={
            "thpt_under_5s_per_cluster": five_s,
            "thpt_under_1s_per_cluster": one_s,
            "thpt_slo_basis": SLO_BASIS_ACQ_P95_UNCORROBORATED,
            "thpt_cluster_node_count": node_count,
        },
    )


def convert_kata_cold(results: dict) -> None:
    """5s bar bracketed (unresolved-bounds ***); 1s stays render-derived-0."""
    sc = _scenario(results, "native_digest_cold")
    sc["sla_metrics"] = _apply(
        sc["sla_metrics"],
        drop=("thpt_under_5s_pend_reason",),
        add={"thpt_slo_basis_5s": SLO_BASIS_UNRESOLVED_BOUNDS},
    )


def regen() -> dict:
    """Apply all five conversions in memory and return the two results dicts."""
    gvisor = _load(GVISOR_LATEST)
    kata = _load(KATA_LATEST)
    convert_gvisor_warm(gvisor)
    convert_gvisor_cold(gvisor)
    convert_gvisor_resume(gvisor)
    convert_kata_warm(kata)
    convert_kata_cold(kata)
    return {GVISOR_LATEST: gvisor, KATA_LATEST: kata}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    check_only = "--check" in argv
    outputs = regen()
    changed = False
    for rel, data in outputs.items():
        path = REPO_ROOT / rel
        # Match the canonical writer (harness/run.py) exactly so the diff is
        # confined to the five converted cells, not a whole-file re-key.
        new_text = json.dumps(data, indent=2, sort_keys=True) + "\n"
        old_text = path.read_text() if path.exists() else ""
        if new_text != old_text:
            changed = True
            if not check_only:
                path.write_text(new_text)
            print(f"{'would update' if check_only else 'updated'}: {rel}")
        else:
            print(f"unchanged: {rel}")
    if check_only and changed:
        print("--check: latest.json is stale; run without --check to regenerate")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
