#!/usr/bin/env python3
"""Portable benchmark harness — single in-process scenario loop.

A stranger can `git clone && python3 -m harness.run` against whatever cluster their
KUBECONFIG points at (kind default, or their own GKE / GKE-Sandbox) and reproduce
every cell of a product's page. `--product` selects the suite (default: sandbox);
its results are written to `<product>/results/latest.json`. Honest by construction:
the README cells are machine-rendered from that file; no hand numbers.

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
from .scenario_map import cells_for_product, substrate_satisfies

log = logging.getLogger("bench-harness")

_SCENARIOS_PKG = "harness.scenarios"


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
    # badge_scope (#3905) is a static per-scenario property — inject the Cell's value
    # onto the outcome so a PASS carries its scope qualifier BY CONSTRUCTION (#3948),
    # not via a per-fire manual patch. The emitter validates it against
    # BADGE_SCOPE_ENUM; render suffixes it on the PASS token. Only set when the Cell
    # declares one (isolation badges) — perf cells stay clean.
    if cell.badge_scope is not None:
        raw["badge_scope"] = cell.badge_scope
    return raw


def merge_seed_placeholders(raw: list[dict], prior_scenarios) -> list[dict]:
    """Carry forward hand-seeded `pending` placeholder rows for cells the current
    suite does not register, so a partial run does not silently drop them (#3909).

    The runner writes `<product>/results/latest.json` wholesale, emitting only the
    cells the registered suite produced. When that suite is a SUBSET of the
    hand-seeded file (e.g. `--product substrate` registers 1 of 3 seeded cells), the
    unregistered placeholders would vanish from the public render — both names are in
    render's vocabulary, so it is a real lost row, not just a JSON-file diff. This
    appends each seeded row whose name is NOT in the freshly-run set, but ONLY when
    its outcome is `pending`: a stale measured (PASS/FAIL) row is never resurrected,
    and a registered cell always wins via its fresh run. The carried rows still pass
    through the closed-schema emitter (`build_results`), so honest-by-construction is
    preserved — a carried row that is not a valid pending cell raises there, it is
    never silently published.

    Fresh rows keep their suite order; carried placeholders are appended in seed
    order. This is a NO-OP whenever the seed names all equal the registered cells
    (the sandbox case today), and becomes a no-op for substrate too once its two
    perf cells register — so it never conflicts with building the real cells.
    """
    fresh_names = {r["name"] for r in raw if isinstance(r.get("name"), str)}
    carried: list[dict] = []
    if isinstance(prior_scenarios, list):
        for s in prior_scenarios:
            if not isinstance(s, dict):
                continue
            name = s.get("name")
            outcome = s.get("outcome")
            if not isinstance(name, str) or name in fresh_names:
                continue
            if not isinstance(outcome, str) or outcome.lower() != "pending":
                continue
            carried.append(s)
    return raw + carried


def _read_prior_scenarios(out_path: pathlib.Path) -> list:
    """Read the existing results file's scenarios list (for the seed-merge above).

    Best-effort: a missing or malformed file means there is nothing to preserve, so
    return [] rather than failing the run.
    """
    try:
        prior = json.loads(out_path.read_text())
    except (FileNotFoundError, ValueError):
        return []
    scen = prior.get("scenarios") if isinstance(prior, dict) else None
    return scen if isinstance(scen, list) else []


def run_suite(cells, substrate: str) -> list[dict]:
    raw = []
    for cell in cells:
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
    ap = argparse.ArgumentParser(description="portable benchmark harness")
    ap.add_argument(
        "--product",
        default=results_schema.DEFAULT_PRODUCT,
        choices=results_schema.PRODUCT_ENUM,
        help="which product's scenario suite to run (default: sandbox)",
    )
    ap.add_argument(
        "--out",
        default=None,
        help=(
            "path to write <product>/results/latest.json "
            "(default: derived from --product)"
        ),
    )
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    # Resolve the cell suite BEFORE touching the cluster — an unregistered product
    # raises here (SystemExit) and never reaches the write path, so a typo'd
    # --product can never overwrite a hand-seeded <product>/results/latest.json.
    cells = cells_for_product(args.product)
    substrate = detect_substrate()
    out = (
        pathlib.Path(args.out)
        if args.out
        else pathlib.Path(__file__).resolve().parent.parent / args.product / "results" / "latest.json"
    )
    log.info("running %s suite on substrate=%s", args.product, substrate)
    raw = run_suite(cells, substrate)
    # Preserve hand-seeded pending placeholders for cells this suite does not yet
    # register (#3909) — read BEFORE the wholesale write below, which would drop them.
    raw = merge_seed_placeholders(raw, _read_prior_scenarios(out))
    results = results_schema.build_results(
        raw, build_provenance(substrate), generated_at=_now_iso(), product=args.product
    )

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2, sort_keys=True) + "\n")
    log.info("wrote %d scenario cells to %s", len(results["scenarios"]), out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
