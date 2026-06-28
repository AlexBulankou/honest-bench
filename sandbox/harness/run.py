#!/usr/bin/env python3
"""Portable benchmark harness — single in-process scenario loop.

A stranger can `git clone && python3 -m sandbox.harness.run` against whatever
cluster their KUBECONFIG points at (kind default, or their own GKE / GKE-Sandbox)
and reproduce every cell of the sandbox page. Honest by construction: the README
cells are machine-rendered from the `results/latest.json` this writes; no hand
numbers.

This replaces the four internal bindings of the in-cluster runner with portable
equivalents:
  1. obs-Postgres write  -> aggregate per-scenario dicts in memory; no DB.
  2. pinned cluster ctx  -> whatever KUBECONFIG the runner finds; substrate read live.
  3. in-cluster CronJob fan-out -> this single loop (kind has one node; no Job fan-out).
  4. internal AR image   -> the OSS controller built/pulled from upstream main per recipe.

The scenario `run(name) -> (outcome, excerpt, sla_metrics)` contract is preserved.
`excerpt` is read for PASS/FAIL classification ONLY and is NEVER written to
results.json (the public-safety FORBIDDEN raw-failure_excerpt rule) — see how it is
dropped on the floor below.
"""

from __future__ import annotations

import argparse
import datetime
import importlib
import json
import logging
import os
import pathlib
import uuid

from . import results_schema
from .scenario_map import MVP_CELLS, substrate_satisfies

log = logging.getLogger("sandbox-harness")

_SCENARIOS_PKG = "sandbox.harness.scenarios"


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def detect_substrate() -> str:
    """Read the cluster substrate live (never pinned).

    Phase-1 default is the env override `BENCH_CLUSTER_SUBSTRATE` (set by the
    auto-refresh Action to `kind`); live GKE/GKE-Sandbox detection from node
    labels is the Phase-2 integration seam.
    """
    sub = os.environ.get("BENCH_CLUSTER_SUBSTRATE", "kind")
    if sub not in results_schema.CLUSTER_SUBSTRATE_ENUM:
        raise SystemExit(
            f"BENCH_CLUSTER_SUBSTRATE={sub!r} not in "
            f"{results_schema.CLUSTER_SUBSTRATE_ENUM}"
        )
    return sub


def _run_one(cell, substrate: str) -> dict:
    """Drive one cell; return a raw per-scenario dict for the emitter to coerce.

    On a substrate that cannot satisfy the cell (gVisor isolation on kind) we do
    NOT call the scenario — we emit a `pending` cell that says exactly why, so a
    missing runtime reads as honest-pending, never as a false FAIL.
    """
    if not substrate_satisfies(cell, substrate):
        return {
            "name": cell.module,
            "outcome": "pending",
            "pending_reason": cell.pending_reason,
        }

    mod = importlib.import_module(f"{_SCENARIOS_PKG}.{cell.module}")
    outcome, excerpt, sla_metrics = mod.run(cell.module)
    # excerpt is classification-only — drop it here; it is NEVER emitted.
    del excerpt
    raw: dict = {"name": cell.module, "outcome": outcome}
    # A scenario may report the sample count backing its measurement under the
    # reserved "n" key inside sla_metrics. Lift it to the top-level schema field
    # so it renders as "(n=N)" beside the measurement; popping it here keeps the
    # emitter from coercing it into a pseudo-metric (it matches the metric-key
    # regex). A scenario that does not measure (pending / under-delivery FAIL)
    # emits no "n" and the row renders without a sample count.
    if isinstance(sla_metrics, dict):
        n = sla_metrics.pop("n", None)
        if isinstance(n, (int, float)) and not isinstance(n, bool):
            raw["n"] = int(n)
        # A scenario returning a pending outcome carries its reason under the
        # reserved "pending_reason" key inside sla_metrics (the substrate-gate
        # path sets pending_reason directly from cell.pending_reason and never
        # calls the scenario, so this is the only channel for a scenario-return
        # pending). Lift it to the top-level schema field; the emitter validates
        # it against PENDING_REASON_ENUM. Pop it so it is not coerced into a
        # pseudo-metric (it would match the metric-key regex).
        reason = sla_metrics.pop("pending_reason", None)
        if isinstance(reason, str):
            raw["pending_reason"] = reason
    raw["sla_metrics"] = sla_metrics
    return raw


def run_suite(substrate: str) -> list[dict]:
    raw = []
    for cell in MVP_CELLS:
        try:
            raw.append(_run_one(cell, substrate))
        except Exception as exc:  # a scenario crash is a FAIL cell, not a suite abort
            log.exception("cell %s raised", cell.module)
            # The excerpt (str(exc)) is read for classification only, not emitted.
            raw.append({"name": cell.module, "outcome": "fail"})
    return raw


def build_provenance(substrate: str) -> dict:
    return {
        "cluster_substrate": substrate,
        "controller_image": os.environ.get("BENCH_CONTROLLER_IMAGE", ""),
        "controller_digest": os.environ.get("BENCH_CONTROLLER_DIGEST", ""),
        "crd_version": os.environ.get("BENCH_CRD_VERSION", ""),
        "suite_git_sha": os.environ.get("BENCH_SUITE_GIT_SHA", ""),
        "run_id": uuid.uuid4().hex,
        "node_count": int(os.environ.get("BENCH_NODE_COUNT", "1")),
        # Image-cache posture for the native_digest_cold cell (#3885). Conservative
        # default cold-provision (claims less); the refresh Action sets cold-pull
        # only on a freshly-created kind cluster, where the empty image cache makes
        # the pull provably cold. The emitter validates this against the closed
        # COLD_START_MODE_ENUM and fails closed on a typo'd value.
        "cold_start_mode": os.environ.get(
            "BENCH_NATIVE_DIGEST_COLD_MODE", "cold-provision"
        ),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="portable sandbox benchmark harness")
    ap.add_argument(
        "--out",
        default=str(pathlib.Path(__file__).resolve().parent.parent / "results" / "latest.json"),
        help="path to write results/latest.json",
    )
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    substrate = detect_substrate()
    log.info("running MVP suite on substrate=%s", substrate)
    raw = run_suite(substrate)
    results = results_schema.build_results(
        raw, build_provenance(substrate), generated_at=_now_iso()
    )

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2, sort_keys=True) + "\n")
    log.info("wrote %d scenario cells to %s", len(results["scenarios"]), out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
