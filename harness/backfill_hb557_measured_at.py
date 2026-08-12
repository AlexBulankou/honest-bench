#!/usr/bin/env python3
"""hb#557 backfill — stamp the historical ``thpt_slo_measured_at`` onto the
gVisor cold-start cell's floor-zero cluster-triple.

Root cause (hb#557, follow-up to hb#554): ``native_digest_cold``'s
``thpt_under_5s_per_cluster`` / ``thpt_under_1s_per_cluster`` /
``thpt_cluster_node_count`` triple was hand-authored by
``regen_hb230_caveat_cells.py`` (commit be28a49, hb#230 doctrine flip) from
committed leg-B cold-sweep records, and has been correctly held frozen since
via ``carry_prior_cluster_triples`` (the same do-not-auto-decay carry
hb#554 fixed) while the per-node/TTFE columns keep advancing. Structurally
identical gap to hb#554 — the triple was never wrong, only undisclosed as a
point-in-time measurement — but this cell's provenance is NOT a single sweep
commit's own ``generated_at`` (hb#554's mechanism): at the graduating commit
the file's top-level ``generated_at`` (2026-07-04T18:32:56Z) actually
PREDATES the leg-B records the derivation consumed (2026-07-06), because the
cell was synthesized after-the-fact from already-committed records rather
than produced by a fresh same-day sweep. Reusing the file's ``generated_at``
verbatim would therefore stamp a WRONG (too-early) instant — hb#557 was
filed specifically to avoid that mistake and trace the real one instead.

Confirmed via git + record archaeology (read-only, not re-derived here):
  * ``harness.slo_rate._derive_cold_floor_zero`` (the pure function
    ``regen_hb230_caveat_cells.convert_gvisor_cold`` calls) implements a
    TWO-SIGNAL predicate over the three committed leg-B records: (a) the
    FLOOR rung (minimum positive ``rate_per_s``) must clear both margined
    bars, and (b) >=1 ``controller_measured=True`` rung must ALSO clear both
    bars (the "trusted corroborator"). Neither signal alone is sufficient.
  * Of the three committed records, the r5 rung (``rate_per_s=5``,
    ``controller_measured=False``, ``partial_written_at``
    2026-07-06T02:49:34Z) is the floor signal; the r20 rung
    (``rate_per_s=20``, ``controller_measured=True``, ``partial_written_at``
    2026-07-06T03:19:18Z) is the ONLY controller-measured rung and is the
    corroborating signal. The r10 rung is not load-bearing for either signal.
  * The predicate could not fire — i.e. the triple did not exist as a
    derivable fact — until BOTH load-bearing records existed. The later of
    the two, r20 at 2026-07-06T03:19:18Z, is therefore the correct sweep
    completion instant to stamp: the point-in-time disclosure hb#554
    introduced measures "when did this become true", not "when was any one
    contributing sample taken".

Deliberately OUT of scope (see hb#557 for the full reasoning):
  * ``sandbox-kata/results/latest.json``'s ``native_digest_cold`` — its 5s
    bar carries the standalone ``unresolved_bounds_bar_bracketed`` per-bar
    caveat (``convert_kata_cold``), which per ``carry_prior_basis_caveats``'s
    docstring is "the ONE legitimate stamp-without-triple shape" — it has no
    ``thpt_under_5s_per_cluster``/``thpt_cluster_node_count`` triple to ride
    a ``thpt_slo_measured_at`` passenger on. Nothing to backfill there.

Data-only, no fresh run: reads the committed ``sandbox/results/latest.json``
and the three committed leg-B records, adds the one key, routes the result
through ``results_schema._coerce_sla_metrics`` (the same fail-closed build
guard the ingest path uses) as a build guard, and writes back in the
canonical writer format. Idempotent (--check returns 1 if stale; a second
run is a no-op on an already-backfilled file).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

try:
    from harness.results_schema import _coerce_sla_metrics
except ImportError as exc:  # pragma: no cover - invocation hint only
    raise SystemExit(
        "run from the honest-bench root as: "
        "python3 -m harness.backfill_hb557_measured_at"
    ) from exc

REPO_ROOT = Path(__file__).resolve().parent.parent

GVISOR_LATEST = "sandbox/results/latest.json"
TARGET_SCENARIO = "native_digest_cold"
# The corroborator leg (r20) — the LATER of the two load-bearing leg-B
# records (r5 floor @ 02:49:34Z, r20 corroborator @ 03:19:18Z) that
# _derive_cold_floor_zero's two-signal predicate required to fire. See the
# module docstring for the record-level archaeology that confirmed this,
# and why the file's own generated_at at graduation time would be wrong.
MEASURED_AT = "2026-07-06T03:19:18Z"


def _load(rel: str) -> dict:
    return json.loads((REPO_ROOT / rel).read_text())


def _scenario(results: dict, name: str) -> dict:
    for sc in results["scenarios"]:
        if sc.get("name") == name:
            return sc
    raise KeyError(f"scenario {name!r} not found in results")


def regen() -> dict:
    """Apply the one backfill in memory and return the results dict."""
    gvisor = _load(GVISOR_LATEST)
    sc = _scenario(gvisor, TARGET_SCENARIO)
    sla = sc["sla_metrics"]
    if "thpt_under_5s_per_cluster" not in sla:
        raise ValueError(
            f"{TARGET_SCENARIO!r} has no per-cluster triple to stamp — "
            "backfill target has drifted, re-verify before running"
        )
    sla = dict(sla)
    sla["thpt_slo_measured_at"] = MEASURED_AT
    # Fail-closed build guard: never write a block the schema would reject.
    sc["sla_metrics"] = _coerce_sla_metrics(sla)
    return {GVISOR_LATEST: gvisor}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    check_only = "--check" in argv
    outputs = regen()
    changed = False
    for rel, data in outputs.items():
        path = REPO_ROOT / rel
        # Match the canonical writer (harness/run.py) exactly so the diff is
        # confined to the one backfilled key, not a whole-file re-key.
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
