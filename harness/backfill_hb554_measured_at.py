#!/usr/bin/env python3
"""hb#554 backfill — stamp the historical ``thpt_slo_measured_at`` onto the
gVisor warm-pool cell's per-cluster SLO triple.

Root cause (hb#554): ``warmpool_cold_start``'s ``thpt_under_5s_per_cluster`` /
``thpt_under_1s_per_cluster`` / ``thpt_cluster_node_count`` triple graduated to
the clean ``true_ttfe`` basis at commit 6e7f231 (2026-07-25T03:05:17Z) and has
been correctly held frozen since — ``carry_prior_cluster_triples`` (the
do-not-auto-decay carry) preserves it across every subsequent daily refresh
while the per-node/TTFE columns keep advancing. The triple was never wrong;
what was missing was the point-in-time DISCLOSURE that lets a reader tell it
apart from a fresh same-day measurement. The propagation fix (``slo_rate.py``
/ ``results_schema.py`` / ``stepup_adapter.py`` / ``run.py`` / render's schema
+ render.py) now carries a fresh ``thpt_slo_measured_at`` on every NEW sweep;
this script backfills the one cell that graduated before the fix existed.

Confirmed via git archaeology (read-only, not re-derived here):
  * commit 6e7f231 (2026-07-25T03:19:06+00:00, generated_at
    2026-07-25T03:05:17Z) is the sole commit that set the current 7.727/7.727
    @ 4-node triple.
  * Four subsequent auto-refresh commits (a0a1ab2, a147a06, 77d2969, bd2fb92)
    left the triple byte-identical while ``thpt_under_5s_per_node`` /
    ``thpt_under_1s_per_node`` / ``ttfe_p50_ms`` / ``ttfe_p95_ms`` all moved —
    the exact frozen-triple-while-siblings-refresh signature the fix targets.

Deliberately OUT of scope (see hb#554 adjudication comment for the full
reasoning, not re-litigated here):
  * ``sandbox-kata/results/latest.json`` — the whole file's own ``generated_at``
    has not advanced since 2026-07-24T21:02:55Z (commit a40b3e5c); render
    already discloses that date via the ``generated-at=`` Kata banner, so
    there is no frozen-triple-masquerading-as-fresh defect to backfill here.
  * ``native_digest_cold`` (gVisor, same file) — its floor-zero-corroborated
    triple was produced by ``regen_hb230_caveat_cells.py`` from prior
    committed leg records, a pure derivation that deliberately never bumped
    ``generated_at``; there is no single unambiguous sweep instant to stamp,
    unlike the warm-pool cell's clean graduation commit.

Data-only, no fresh run: reads the committed ``sandbox/results/latest.json``,
adds the one key, routes the result through ``results_schema._coerce_sla_metrics``
(the same fail-closed build guard the ingest path uses) as a build guard, and
writes back in the canonical writer format. Idempotent (--check returns 1 if
stale; a second run is a no-op on an already-backfilled file).
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
        "python3 -m harness.backfill_hb554_measured_at"
    ) from exc

REPO_ROOT = Path(__file__).resolve().parent.parent

GVISOR_LATEST = "sandbox/results/latest.json"
TARGET_SCENARIO = "warmpool_cold_start"
# generated_at of commit 6e7f231 — the graduation fire that set the current,
# still-live 7.727/7.727 @ 4-node triple. See module docstring for the git
# archaeology that confirmed this is the correct, sole historical instant.
MEASURED_AT = "2026-07-25T03:05:17Z"


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
