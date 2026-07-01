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
from . import stepup_adapter
from . import warm_vs_cold as warm_vs_cold_mod
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
    runtime_scope = None
    if isinstance(sla_metrics, dict):
        n = sla_metrics.pop("n", None)
        if isinstance(n, (int, float)) and not isinstance(n, bool):
            raw["n"] = int(n)
        # A scenario may UPGRADE its static badge_scope at fire time under the
        # reserved "badge_scope" key inside sla_metrics — the data-plane probe
        # path (#3907) returns "enforced" only when the in-Pod connectivity probe
        # actually confirmed the policy blocks traffic, upgrading the static
        # "control-plane" admission badge to "enforced" BY CONSTRUCTION. Pop it
        # before coercion (a string would be dropped by _coerce_sla_metrics, and
        # it matches the metric-key regex). The emitter validates it against
        # BADGE_SCOPE_ENUM; a junk override raises there (fail-closed).
        runtime_scope = sla_metrics.pop("badge_scope", None)
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
        # A scenario may surface its per-claim warm-pool TTFE samples under the
        # reserved "warm_ttfe_samples_ms" key inside sla_metrics (burst_create
        # does so under BENCH_TTFE_EXEC, #1018). They are the warm leg of the
        # warm-vs-cold speedup; maybe_warm_vs_cold reads them from raw[...] AFTER
        # this loop. Pop them so they never reach the public results schema (a
        # list would be dropped by _coerce_sla_metrics anyway, but popping keeps
        # the raw cell clean and makes the channel explicit). Keep only a clean
        # list of finite-looking numbers; anything else is a corrupt warm leg and
        # the classifier's own gates will refuse it.
        warm = sla_metrics.pop("warm_ttfe_samples_ms", None)
        if isinstance(warm, list):
            raw["warm_ttfe_samples_ms"] = warm
    raw["sla_metrics"] = sla_metrics
    # badge_scope (#3905) is a static per-scenario property — inject the Cell's value
    # onto the outcome so a PASS carries its scope qualifier BY CONSTRUCTION (#3948),
    # not via a per-fire manual patch. The emitter validates it against
    # BADGE_SCOPE_ENUM; render suffixes it on the PASS token. Only set when the Cell
    # declares one (isolation badges) — perf cells stay clean. A scenario-returned
    # runtime override (the data-plane probe's "enforced", #3907) takes precedence
    # over the static Cell value, so an armed+passing probe upgrades the badge
    # without a per-fire manual patch.
    scope = runtime_scope if isinstance(runtime_scope, str) else cell.badge_scope
    if scope is not None:
        raw["badge_scope"] = scope
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


def _read_prior_scale_proof(out_path: pathlib.Path):
    """Read the existing results file's top-level scale_proof object (#3952).

    Best-effort, mirroring _read_prior_scenarios: a missing/malformed file or an
    absent scale_proof key means there is nothing to carry forward, so return None
    (the honest-absence signal the emitter understands).
    """
    try:
        prior = json.loads(out_path.read_text())
    except (FileNotFoundError, ValueError):
        return None
    sp = prior.get("scale_proof") if isinstance(prior, dict) else None
    return sp if isinstance(sp, dict) else None


def _read_prior_stepup(out_path: pathlib.Path):
    """Read the existing results file's top-level stepup object (#3960).

    Best-effort, mirroring _read_prior_scale_proof: a missing/malformed file or an
    absent stepup key means there is nothing to carry forward, so return None (the
    honest-absence signal the emitter understands).
    """
    try:
        prior = json.loads(out_path.read_text())
    except (FileNotFoundError, ValueError):
        return None
    su = prior.get("stepup") if isinstance(prior, dict) else None
    return su if isinstance(su, dict) else None


def _read_prior_warm_vs_cold(out_path: pathlib.Path):
    """Read the existing results file's top-level warm_vs_cold object (#1018).

    Best-effort, mirroring _read_prior_stepup: a missing/malformed file or an
    absent warm_vs_cold key means there is nothing to carry forward, so return None
    (the honest-absence signal the emitter understands).
    """
    try:
        prior = json.loads(out_path.read_text())
    except (FileNotFoundError, ValueError):
        return None
    wvc = prior.get("warm_vs_cold") if isinstance(prior, dict) else None
    return wvc if isinstance(wvc, dict) else None


def _read_prior_kata_activation(out_path: pathlib.Path):
    """Read the existing results file's top-level kata_activation object (#3942).

    Best-effort, mirroring _read_prior_warm_vs_cold: a missing/malformed file or an
    absent kata_activation key means there is nothing to carry forward, so return
    None (the honest-absence signal the emitter understands).
    """
    try:
        prior = json.loads(out_path.read_text())
    except (FileNotFoundError, ValueError):
        return None
    ka = prior.get("kata_activation") if isinstance(prior, dict) else None
    return ka if isinstance(ka, dict) else None


def _read_prior_concurrent_burst(out_path: pathlib.Path):
    """Read the existing results file's top-level concurrent_burst object (#4021).

    Best-effort, mirroring _read_prior_kata_activation: a missing/malformed file or
    an absent concurrent_burst key means there is nothing to carry forward, so
    return None (the honest-absence signal the emitter understands).
    """
    try:
        prior = json.loads(out_path.read_text())
    except (FileNotFoundError, ValueError):
        return None
    cb = prior.get("concurrent_burst") if isinstance(prior, dict) else None
    return cb if isinstance(cb, dict) else None


def _read_prior_warm_pool_acquisition(out_path: pathlib.Path):
    """Read the existing results file's top-level warm_pool_acquisition object (#4083).

    Best-effort, mirroring _read_prior_concurrent_burst: a missing/malformed file or
    an absent warm_pool_acquisition key means there is nothing to carry forward, so
    return None (the honest-absence signal the emitter understands).
    """
    try:
        prior = json.loads(out_path.read_text())
    except (FileNotFoundError, ValueError):
        return None
    wpa = prior.get("warm_pool_acquisition") if isinstance(prior, dict) else None
    return wpa if isinstance(wpa, dict) else None


def _read_prior_at_scale_contention(out_path: pathlib.Path):
    """Read the existing results file's top-level at_scale_contention object (#810).

    Best-effort, mirroring _read_prior_warm_pool_acquisition: a missing/malformed file
    or an absent at_scale_contention key means there is nothing to carry forward, so
    return None (the honest-absence signal the emitter understands).
    """
    try:
        prior = json.loads(out_path.read_text())
    except (FileNotFoundError, ValueError):
        return None
    asc = prior.get("at_scale_contention") if isinstance(prior, dict) else None
    return asc if isinstance(asc, dict) else None


def _read_prior_provisioning_rate_sweep(out_path: pathlib.Path):
    """Read the existing results file's top-level provisioning_rate_sweep object (#4086).

    Best-effort, mirroring _read_prior_at_scale_contention: a missing/malformed file
    or an absent provisioning_rate_sweep key means there is nothing to carry forward,
    so return None (the honest-absence signal the emitter understands).
    """
    try:
        prior = json.loads(out_path.read_text())
    except (FileNotFoundError, ValueError):
        return None
    prs = prior.get("provisioning_rate_sweep") if isinstance(prior, dict) else None
    return prs if isinstance(prs, dict) else None


def carry_prior_scale_proof(fresh, prior, *, generated_at: str):
    """Persist the Scale Proof block across the daily single-node refresh (#3952).

    The Scale Proof (Linearity Check) is produced only by the heavy, manual,
    collision-acked multi-K sweep (`maybe_scale_proof`, gated BENCH_SCALE_SLOPE=1).
    The daily single-node auto-refresh never arms that sweep, so without this
    carry-forward the published block would vanish ~22h after a sweep (the
    gated-too-long auto-decay #3952 fixes) — the sweep is not a standing job, so
    "transient" means "gone until the next manual fire".

    Fresh always wins: a real sweep this run stamps `measured_at = generated_at`
    (the instant it was measured) and is returned as-is. Otherwise the prior
    committed block is carried forward UNCHANGED — keeping its own original
    `measured_at`, so a carried point-in-time block stays honestly dated against the
    daily-refreshed top-level `generated_at`. Both paths flow through the closed
    emitter (`_coerce_scale_proof`), so a carried block that is not a valid
    scale_proof is dropped, never published as a partial lie.
    """
    if isinstance(fresh, dict) and fresh:
        stamped = dict(fresh)
        stamped.setdefault("measured_at", generated_at)
        return stamped
    return prior


def carry_prior_stepup(fresh, prior, *, generated_at: str):
    """Persist the step-up throughput-saturation block across the daily refresh (#3960).

    Exact mirror of carry_prior_scale_proof, and for the same reason: the step-up
    sweep is produced only by the heavy, manual, collision-acked CL2 step-up
    backfill (out-of-process, read via maybe_stepup from BENCH_STEPUP_RESULT). The
    daily single-node auto-refresh never produces one, so without this carry-forward
    the published Pareto curve would vanish on the next refresh after a sweep.

    Fresh always wins: a real sweep this run carries its own producer-stamped
    `measured_at` (the true measure time) and only setdefaults `generated_at` if the
    producer somehow omitted it. Otherwise the prior committed block is carried
    forward UNCHANGED, keeping its original `measured_at` so a carried point-in-time
    block stays honestly dated against the daily-refreshed top-level `generated_at`.
    Both paths flow through the closed emitter (`_coerce_stepup`), so a carried block
    that is not a valid stepup is dropped, never published as a partial lie.
    """
    if isinstance(fresh, dict) and fresh:
        stamped = dict(fresh)
        stamped.setdefault("measured_at", generated_at)
        return stamped
    return prior


def carry_prior_warm_vs_cold(fresh, prior, *, generated_at: str):
    """Persist the warm-vs-cold speedup block across the daily refresh (#1018).

    Exact mirror of carry_prior_scale_proof / carry_prior_stepup, and for the same
    reason: the honest warm-vs-cold ratio needs BOTH legs measured the same way in
    the SAME fire (warm samples from burst_create + the single cold TTFE sample from
    native_digest_cold, both under BENCH_TTFE_EXEC). A daily refresh that does not
    arm TTFE produces no fresh block, so without this carry-forward the published
    speedup would vanish on the next refresh after a measured fire.

    Fresh always wins: a real measurement this run stamps `measured_at = generated_at`
    (the instant it was measured) via setdefault, and is returned as-is. Otherwise
    the prior committed block is carried forward UNCHANGED, keeping its original
    `measured_at` so a carried point-in-time block stays honestly dated against the
    daily-refreshed top-level `generated_at`. Both paths flow through the closed
    emitter (`_coerce_warm_vs_cold`), so a carried block that is not a valid
    warm_vs_cold is dropped, never published as a partial lie.
    """
    if isinstance(fresh, dict) and fresh:
        stamped = dict(fresh)
        stamped.setdefault("measured_at", generated_at)
        return stamped
    return prior


def carry_prior_kata_activation(fresh, prior, *, generated_at: str):
    """Persist the Kata+microVM activation block across the daily refresh (#3942; #112 gap).

    Exact mirror of carry_prior_warm_vs_cold, and for the same reason: the Kata block
    is produced only by the heavy, manual, collision-acked Kata matrix-fill fire on
    the nested-virt pool (#1021) — the daily single-node auto-refresh never arms it,
    so without this carry-forward the published Kata activation block would vanish on
    the next refresh after a fire (the #112 wholesale-write drop this fixes).

    Fresh always wins: a real fire this run stamps `measured_at = generated_at` via
    setdefault and is returned as-is. Otherwise the prior committed block is carried
    forward UNCHANGED, keeping its original `measured_at`. Both paths flow through the
    closed emitter (`_coerce_kata_activation`), so a carried block that is not a valid
    kata_activation is dropped, never published as a partial lie.
    """
    if isinstance(fresh, dict) and fresh:
        stamped = dict(fresh)
        stamped.setdefault("measured_at", generated_at)
        return stamped
    return prior


def carry_prior_concurrent_burst(fresh, prior, *, generated_at: str):
    """Persist the concurrent-burst block across the daily refresh (#4021; #112 gap).

    Exact mirror of carry_prior_kata_activation: the concurrent-burst block is
    produced only by the heavy, manual, collision-acked all-at-once burst fire — the
    daily single-node auto-refresh never produces one, so without this carry-forward
    the published block would vanish on the next refresh after a fire (the #112
    wholesale-write drop this fixes).

    Fresh always wins: a real fire this run stamps `measured_at = generated_at` via
    setdefault and is returned as-is. Otherwise the prior committed block is carried
    forward UNCHANGED. Both paths flow through the closed emitter
    (`_coerce_concurrent_burst`), so a carried block that is not a valid
    concurrent_burst is dropped, never published as a partial lie.
    """
    if isinstance(fresh, dict) and fresh:
        stamped = dict(fresh)
        stamped.setdefault("measured_at", generated_at)
        return stamped
    return prior


def carry_prior_warm_pool_acquisition(fresh, prior, *, generated_at: str):
    """Persist the warm-pool acquisition-latency block across the daily refresh (#4083; #112 gap).

    Exact mirror of carry_prior_concurrent_burst: the acquisition-latency block is
    produced only by the heavy, manual, collision-acked step-up fire with the
    per-claim acquisition watch-timer (#1043) — the daily single-node auto-refresh
    never arms it, so without this carry-forward the published block would vanish on
    the next refresh after a fire (the #112 wholesale-write drop this fixes).

    Fresh always wins: a real fire this run stamps `measured_at = generated_at` via
    setdefault and is returned as-is. Otherwise the prior committed block is carried
    forward UNCHANGED. Both paths flow through the closed emitter
    (`_coerce_warm_pool_acquisition`), so a carried block that is not a valid
    warm_pool_acquisition is dropped, never published as a partial lie.
    """
    if isinstance(fresh, dict) and fresh:
        stamped = dict(fresh)
        stamped.setdefault("measured_at", generated_at)
        return stamped
    return prior


def carry_prior_at_scale_contention(fresh, prior, *, generated_at: str):
    """Persist the at-scale-contention RETRACTION block across the daily refresh (#810; #112 gap).

    Exact mirror of carry_prior_warm_pool_acquisition: the at-scale-contention block is
    the deliberate retraction of the sub-second-at-scale claim, produced only by the
    heavy, manual, collision-acked over-subscribed-pool fire — the daily single-node
    auto-refresh never produces one, so `fresh` is always None here and the prior
    committed block is carried forward. Without this, the daily `harness.run --product
    sandbox` refresh would build_results a wholesale write that silently DROPS the
    retraction block from the public table.

    Fresh always wins: a real fire this run stamps `measured_at = generated_at` via
    setdefault and is returned as-is. Otherwise the prior committed block is carried
    forward UNCHANGED. Both paths flow through the closed emitter
    (`_coerce_at_scale_contention`), so a carried block that is not a valid
    at_scale_contention is dropped, never published as a partial lie.
    """
    if isinstance(fresh, dict) and fresh:
        stamped = dict(fresh)
        stamped.setdefault("measured_at", generated_at)
        return stamped
    return prior


def carry_prior_provisioning_rate_sweep(fresh, prior, *, generated_at: str):
    """Persist the provisioning rate-sweep block across the daily refresh (#4086; #112 gap).

    Exact mirror of carry_prior_at_scale_contention: the rate-sweep block is produced only by
    the heavy, manual, collision-acked multi-rate warm-pool provisioning fire — the daily
    single-node auto-refresh never produces one, so `fresh` is always None here and the prior
    committed block is carried forward. Without this, the daily `harness.run --product sandbox`
    refresh would build_results a wholesale write that silently DROPS the block from the page.

    Fresh always wins: a real fire this run stamps `measured_at = generated_at` via setdefault
    and is returned as-is. Otherwise the prior committed block is carried forward UNCHANGED,
    keeping its original `measured_at` so a carried point-in-time block stays honestly dated
    against the daily-refreshed top-level `generated_at`. Both paths flow through the closed
    emitter (`_coerce_provisioning_rate_sweep`), so a carried block that is not a valid
    provisioning_rate_sweep is dropped, never published as a partial lie.
    """
    if isinstance(fresh, dict) and fresh:
        stamped = dict(fresh)
        stamped.setdefault("measured_at", generated_at)
        return stamped
    return prior


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


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


# Scale Proof (Linearity Check) producer opt-in. DEFAULT-OFF.
#
# gated: heavy multi-K cluster fire, armed only in the coordinated sweep [#3949].
# scale_slope.run_sweep() provisions K*slots warm pools across multiple node-counts
# and fires that many claims — it is a heavy MUTATING live producer, NOT part of the
# default single-node auto-refresh (on one node only K=1 is achievable, so the
# classifier returns {} by construction). It is armed ONLY in a4s1's coordinated,
# collision-acked multi-node sweep. Dual-gated below: BENCH_SCALE_SLOPE=1 AND the
# sandbox product — any other product returns None (fail-closed), since the Scale
# Proof table is a sandbox-page artifact and the substrate suite has no scale_proof
# render contract. Flip/launch issue: #3949.
def maybe_scale_proof(product: str):
    """Return the scale_proof object when armed for sandbox, else None.

    None is the honest absence signal the emitter understands: build_results passes
    it through _coerce_scale_proof, which omits the top-level scale_proof key for
    None / empty / malformed input, so the Scale Proof table simply does not render
    rather than showing a partial lie. A K=1-only sweep (single-node cluster) makes
    run_sweep return {} — also None-equivalent here — so the table stays absent until
    a genuine multi-K sweep produces >=2 points.
    """
    if not _env_flag("BENCH_SCALE_SLOPE") or product != "sandbox":
        return None
    from .scenarios import scale_slope
    proof = scale_slope.run_sweep()
    return proof or None


def _stepup_usd_per_node_hour():
    """Optional explicit node-hour price override for the step-up cost axis (#3960).

    Returns a positive float parsed from BENCH_STEPUP_USD_PER_NODE_HOUR, else None.
    None hands cost enrichment back to the machine_type list-price fallback in
    cost.py; a blank/unset/non-positive/non-numeric value is treated as "no
    override" (None) rather than raising, so a fat-fingered fire-time knob degrades
    to the list-price path instead of failing the whole run.
    """
    raw = os.environ.get("BENCH_STEPUP_USD_PER_NODE_HOUR", "").strip()
    if not raw:
        return None
    try:
        val = float(raw)
    except ValueError:
        return None
    return val if val > 0 else None


# Step-up throughput-saturation producer ingestion. DEFAULT-OFF.
#
# Unlike maybe_scale_proof (which runs its sweep IN-PROCESS via a kubernetes
# client), the step-up sweep is driven by an out-of-process CL2 (ClusterLoader2)
# orchestrator that cannot run inside this harness — it names clusters, kubeconfigs,
# and namespaces and so lives in the INTERNAL repo. That orchestrator writes a
# SCRUBBED nested result file; this function READS that file (path in
# BENCH_STEPUP_RESULT), flattens it to the flat schema shape via stepup_adapter, and
# enriches the cost axis. DEFAULT-OFF: with BENCH_STEPUP_RESULT unset the function
# returns None (honest absence), so the daily single-node auto-refresh emits no
# step-up block until a deliberate, collision-acked sweep produces the file.
def maybe_stepup(product: str):
    """Return the flat stepup object read from BENCH_STEPUP_RESULT, else None.

    Dual-gated like maybe_scale_proof: the file path must be set AND the product must
    be sandbox (the step-up Pareto table is a sandbox-page artifact; the substrate
    suite has no stepup render contract). None is the honest absence signal — a missing
    path, a missing/non-sandbox product, an unreadable/malformed file, or a flatten that
    yields nothing all return None, and build_results omits the top-level stepup key for
    None input, so the table simply does not render rather than showing a partial lie.
    The flat record still flows through the closed emitter (`_coerce_stepup`), which is
    the single schema gate — this function only locates, flattens, and cost-enriches.
    """
    if product != "sandbox":
        return None
    path = os.environ.get("BENCH_STEPUP_RESULT", "").strip()
    if not path:
        return None
    try:
        rec = json.loads(pathlib.Path(path).read_text())
    except (OSError, ValueError):
        return None
    flat = stepup_adapter.stepup_nested_to_flat(rec)
    flat = stepup_adapter.enrich_pareto_cost(
        flat, usd_per_node_hour=_stepup_usd_per_node_hour()
    )
    return flat or None


def _raw_cell(raw: list, name: str):
    """Return the raw per-scenario dict for `name`, or None if the cell did not run."""
    for cell in raw:
        if isinstance(cell, dict) and cell.get("name") == name:
            return cell
    return None


# Warm-vs-cold speedup producer. DEFAULT-OFF (rides BENCH_TTFE_EXEC).
#
# Unlike maybe_scale_proof (which runs its own sweep) and maybe_stepup (which reads
# an out-of-process file), the warm-vs-cold legs are BOTH produced by cells already
# in this same in-process suite — so there is no extra fire to arm. The warm leg is
# burst_create's per-claim warm-pool TTFE sample list (surfaced under the reserved
# `warm_ttfe_samples_ms` key, #1018); the cold leg is native_digest_cold's single
# cold TTFE sample (its emitted `ttfe_p50_ms`, n=1). Both are only present when
# BENCH_TTFE_EXEC armed the literal-TTFE path on this fire, so the same flag gates
# this producer. The runtime classes come from the two scenarios' own env knobs
# (BURST_CREATE_RUNTIME_CLASS / NATIVE_DIGEST_COLD_RUNTIME_CLASS) — burst_create's is
# read-back-verified against the live pods on gke-sandbox, so a misconfigured fire
# (gvisor-warm vs runc/empty-cold) makes the classifier's parity gate refuse to
# publish rather than print an apples-to-oranges ratio.
def maybe_warm_vs_cold(product: str, raw: list):
    """Return the warm_vs_cold inner object when armed for sandbox, else None.

    Dual-gated like maybe_scale_proof / maybe_stepup: BENCH_TTFE_EXEC must be armed
    AND the product must be sandbox (the warm-vs-cold cell is a sandbox-page artifact;
    the substrate suite has no warm_vs_cold render contract). None is the honest
    absence signal — an unarmed fire, a non-sandbox product, a missing leg, or any
    classifier honesty-gate failure (semantic/runtime-class mismatch, empty/corrupt
    warm samples, non-positive cold) all return None, and build_results omits the
    top-level warm_vs_cold key for None input so the cell renders pending rather than
    a fabricated number. The pure classifier (`classify_warm_vs_cold`) is the single
    honesty gate — this function only locates the two legs and reads their env-knob
    runtime classes.
    """
    if not _env_flag("BENCH_TTFE_EXEC") or product != "sandbox":
        return None
    warm_cell = _raw_cell(raw, "burst_create")
    cold_cell = _raw_cell(raw, "native_digest_cold")
    if warm_cell is None or cold_cell is None:
        return None
    warm_samples = warm_cell.get("warm_ttfe_samples_ms")
    cold_sla = cold_cell.get("sla_metrics")
    cold_sample = cold_sla.get("ttfe_p50_ms") if isinstance(cold_sla, dict) else None
    if not isinstance(warm_samples, list) or cold_sample is None:
        return None
    result = warm_vs_cold_mod.classify_warm_vs_cold(
        warm_samples,
        cold_sample,
        warm_semantic="ttfe",
        cold_semantic="ttfe",
        warm_runtime_class=os.environ.get("BURST_CREATE_RUNTIME_CLASS", "").strip(),
        cold_runtime_class=os.environ.get(
            "NATIVE_DIGEST_COLD_RUNTIME_CLASS", ""
        ).strip(),
    )
    return result or None


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
    prior_scenarios = _read_prior_scenarios(out)
    prior_scale_proof = _read_prior_scale_proof(out)
    prior_stepup = _read_prior_stepup(out)
    prior_warm_vs_cold = _read_prior_warm_vs_cold(out)
    prior_kata_activation = _read_prior_kata_activation(out)
    prior_concurrent_burst = _read_prior_concurrent_burst(out)
    prior_warm_pool_acquisition = _read_prior_warm_pool_acquisition(out)
    prior_at_scale_contention = _read_prior_at_scale_contention(out)
    prior_provisioning_rate_sweep = _read_prior_provisioning_rate_sweep(out)
    raw = merge_seed_placeholders(raw, prior_scenarios)
    generated_at = _now_iso()
    # Carry the Scale Proof block across the daily refresh (#3952): a fresh sweep
    # this run wins and is stamped measured_at=generated_at; otherwise the prior
    # committed block is carried forward so the public table does not auto-decay.
    scale_proof = carry_prior_scale_proof(
        maybe_scale_proof(args.product), prior_scale_proof, generated_at=generated_at
    )
    # Carry the step-up throughput-saturation block across the daily refresh (#3960),
    # same posture: a fresh out-of-process CL2 sweep (read via BENCH_STEPUP_RESULT)
    # wins; otherwise the prior committed block is carried so the Pareto curve does
    # not auto-decay between manual sweeps.
    stepup = carry_prior_stepup(
        maybe_stepup(args.product), prior_stepup, generated_at=generated_at
    )
    # Carry the warm-vs-cold speedup block across the daily refresh (#1018), same
    # posture: a fresh measurement this run (both TTFE legs present + parity-clean)
    # wins; otherwise the prior committed block is carried so the speedup cell does
    # not auto-decay between TTFE-armed fires.
    warm_vs_cold_obj = carry_prior_warm_vs_cold(
        maybe_warm_vs_cold(args.product, raw), prior_warm_vs_cold,
        generated_at=generated_at,
    )
    # Carry the kata-activation (#3942), concurrent-burst (#4021), and
    # warm-pool-acquisition (#4083) blocks across the daily refresh (#112). These
    # three blocks have no in-process producer — they are written only by manual
    # data-only fires straight into latest.json — so `fresh` is always None here
    # and the prior committed block is carried forward. Without this, the daily
    # `harness.run --product sandbox` refresh would build_results a wholesale
    # write that silently DROPS all three blocks from the public table.
    kata_activation = carry_prior_kata_activation(
        None, prior_kata_activation, generated_at=generated_at
    )
    concurrent_burst = carry_prior_concurrent_burst(
        None, prior_concurrent_burst, generated_at=generated_at
    )
    warm_pool_acquisition = carry_prior_warm_pool_acquisition(
        None, prior_warm_pool_acquisition, generated_at=generated_at
    )
    # Carry the at-scale-contention retraction block across the daily refresh (#810).
    # Same posture as the three producer-less blocks above: no in-process producer, so
    # `fresh` is always None and the prior committed block is carried forward — without
    # this the daily refresh would build_results a wholesale write that DROPS it.
    at_scale_contention = carry_prior_at_scale_contention(
        None, prior_at_scale_contention, generated_at=generated_at
    )
    # Carry the provisioning rate-sweep block across the daily refresh (#4086). Same
    # posture as the producer-less blocks above: no in-process producer, so `fresh` is
    # always None and the prior committed block is carried forward — without this the
    # daily refresh would build_results a wholesale write that DROPS it.
    provisioning_rate_sweep = carry_prior_provisioning_rate_sweep(
        None, prior_provisioning_rate_sweep, generated_at=generated_at
    )
    results = results_schema.build_results(
        raw, build_provenance(substrate), generated_at=generated_at, product=args.product,
        scale_proof=scale_proof, stepup=stepup, warm_vs_cold=warm_vs_cold_obj,
        kata_activation=kata_activation, concurrent_burst=concurrent_burst,
        warm_pool_acquisition=warm_pool_acquisition,
        at_scale_contention=at_scale_contention,
        provisioning_rate_sweep=provisioning_rate_sweep,
    )

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2, sort_keys=True) + "\n")
    log.info("wrote %d scenario cells to %s", len(results["scenarios"]), out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
