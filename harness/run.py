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
import math
import os
import pathlib
import uuid

from . import metrics
from . import results_schema
from . import slo_rate
from . import stepup_adapter
from . import warm_vs_cold as warm_vs_cold_mod
from .scenario_map import cells_for_product, substrate_satisfies
from .scenarios import runtime_class as rc

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
    runtime_construction = None
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
        # A runtime "enforced" badge_scope upgrade MUST carry its companion
        # badge_construction (#3950/#4051 over-claim guard): the data-plane probe
        # returns {"badge_scope": "enforced", "badge_construction": "standard-np"}
        # as one atomic pair. Lift badge_construction alongside badge_scope —
        # without this the construction key stays in sla_metrics and is dropped by
        # _coerce_sla_metrics (a string, not a metric), leaving a naked
        # badge_scope='enforced' that _coerce_scenario rejects, crashing the whole
        # run (#4629). The emitter validates it against the closed enum.
        runtime_construction = sla_metrics.pop("badge_construction", None)
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
    # badge_construction (#3950) is runtime-only for these cells — set it only when
    # the scenario returned one alongside an "enforced" upgrade. The emitter
    # validates it against the closed construction enum; pairing it with the scope
    # here satisfies the #4051 over-claim guard by construction.
    if isinstance(runtime_construction, str):
        raw["badge_construction"] = runtime_construction
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


def check_n_regression(raw: list[dict], prior_scenarios) -> list[str]:
    """Detect a refresh that would silently lower a published cell's sample size.

    A cell's published n is scoped to env knobs (`WARMPOOL_COLD_START_POOL_REPLICAS`
    for the warm cell, `NATIVE_DIGEST_COLD_SAMPLES` for the cold cell), so a
    wholesale refresh fired without those knobs resets a previously-graduated row
    back to its default n — re-introducing the comparability marker with no signal
    that anything was lost (hb#198, third gap in this class). This compares each
    fresh row's `n` against the committed row of the same name and reports every
    cell where fresh n < committed n while the committed row was actually measured
    (a `pending` prior never gates — a placeholder has no graduated n to protect).

    Returns human-readable regression lines; empty means clean. The caller decides
    the posture (main() fails closed unless BENCH_ALLOW_N_REGRESSION is set).
    """
    prior_by_name = {}
    if isinstance(prior_scenarios, list):
        for s in prior_scenarios:
            if isinstance(s, dict) and isinstance(s.get("name"), str):
                prior_by_name[s["name"]] = s
    regressions: list[str] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        prior = prior_by_name.get(row.get("name"))
        if not isinstance(prior, dict):
            continue
        p_out = prior.get("outcome")
        if not isinstance(p_out, str) or p_out.lower() == "pending":
            continue
        p_n, f_n = prior.get("n"), row.get("n")
        if (
            isinstance(p_n, int) and not isinstance(p_n, bool)
            and isinstance(f_n, int) and not isinstance(f_n, bool)
            and f_n < p_n
        ):
            regressions.append(
                f"{row['name']}: fresh n={f_n} < committed n={p_n}"
            )
    return regressions


def check_cell_downgrade(raw: list, prior_scenarios) -> list[str]:
    """Detect a refresh that would silently downgrade any published cell (hb#206).

    check_n_regression above is scoped to one field (`n`); this is the
    generalization the same-day recurrence demanded: the gVisor Max Density cell
    published 5.98 sb/vCPU on 07-02, then a canonical refresh fired without the
    density envs re-emitted the row with no `density_per_vcpu` at all — a
    filled→pending transition on the rendered page with zero signal in the run
    (hb#206; the identical failure class hb#198/#200 fixed for `n`).

    Three loss-directional legs, each compared against the committed row of the
    same name (prior `pending` rows never gate — a placeholder protects nothing):

    1. outcome downgrade — a measured (PASS/FAIL) prior whose fresh row's
       outcome is `pending` (or missing);
    2. sla_metrics key loss — any key present in the measured prior's
       sla_metrics that is absent from the fresh row's (value CHANGES never
       gate — a re-measure legitimately moves numbers; key GAINS never gate —
       new instrumentation is not a downgrade);
    3. row drop — a measured prior whose name is entirely absent from the fresh
       set (merge_seed_placeholders deliberately resurrects only `pending`
       priors, so a deregistered measured row would otherwise vanish silently).

    MUST run AFTER merge_seed_placeholders / merge_slo_sweeps /
    carry_prior_cluster_triples / carry_prior_density — the carry machinery
    legitimately restores fields a fresh single-node fire cannot produce, and
    gating before it would false-positive on every carried cluster triple.

    Returns human-readable downgrade lines; empty means clean. The caller
    decides the posture (main() fails closed unless BENCH_ALLOW_CELL_DOWNGRADE
    is set).
    """
    fresh_by_name = {
        r["name"]: r for r in raw
        if isinstance(r, dict) and isinstance(r.get("name"), str)
    }
    downgrades: list[str] = []
    if not isinstance(prior_scenarios, list):
        return downgrades
    for prior in prior_scenarios:
        if not isinstance(prior, dict) or not isinstance(prior.get("name"), str):
            continue
        name = prior["name"]
        p_out = prior.get("outcome")
        if not isinstance(p_out, str) or p_out.lower() == "pending":
            continue
        fresh = fresh_by_name.get(name)
        if fresh is None:
            downgrades.append(
                f"{name}: measured row (outcome={p_out}) dropped entirely from fresh set"
            )
            continue
        f_out = fresh.get("outcome")
        if not isinstance(f_out, str) or f_out.lower() == "pending":
            downgrades.append(
                f"{name}: outcome would downgrade {p_out} -> "
                f"{f_out if isinstance(f_out, str) else 'missing'}"
            )
        pm = prior.get("sla_metrics")
        fm = fresh.get("sla_metrics")
        if isinstance(pm, dict):
            fm_keys = set(fm.keys()) if isinstance(fm, dict) else set()
            lost = sorted(k for k in pm if k not in fm_keys)
            if lost:
                downgrades.append(
                    f"{name}: sla_metrics key(s) lost vs committed row: {', '.join(lost)}"
                )
    return downgrades


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


def _read_prior_provenance_machine_type(out_path: pathlib.Path) -> str | None:
    """Read the existing results file's provenance.machine_type (PR#313 review).

    Best-effort, mirroring _read_prior_scenarios: a missing/malformed file or an absent/
    non-string machine_type means there is nothing to compare against, so return None
    (build_provenance then omits prior_machine_type and no caveat renders).
    """
    try:
        prior = json.loads(out_path.read_text())
    except (FileNotFoundError, ValueError):
        return None
    prov = prior.get("provenance") if isinstance(prior, dict) else None
    mt = prov.get("machine_type") if isinstance(prov, dict) else None
    return mt if isinstance(mt, str) and mt else None


def _read_prior_warmpool_ttfe_p95(out_path: pathlib.Path) -> float | None:
    """Read the existing results file's warmpool_cold_start TTFE p95 (hb#5414).

    Best-effort, mirroring _read_prior_provenance_machine_type: a missing/
    malformed file, an absent warmpool_cold_start scenario, or a non-numeric
    ttfe_p95_ms means there is nothing to compare against, so return None
    (build_provenance then omits prior_warmpool_ttfe_p95_ms and no delta
    caveat renders — the North Star cell degrades to "no prior" quietly,
    never to a stale/wrong number).
    """
    try:
        prior = json.loads(out_path.read_text())
    except (FileNotFoundError, ValueError):
        return None
    scenarios = prior.get("scenarios") if isinstance(prior, dict) else None
    if not isinstance(scenarios, list):
        return None
    for scen in scenarios:
        if not isinstance(scen, dict) or scen.get("name") != "warmpool_cold_start":
            continue
        metrics = scen.get("sla_metrics")
        if not isinstance(metrics, dict):
            return None
        p95 = metrics.get("ttfe_p95_ms")
        if isinstance(p95, bool) or not isinstance(p95, (int, float)) or p95 <= 0:
            return None
        return float(p95)
    return None


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


def _read_prior_cluster_saturation(out_path: pathlib.Path):
    """Read the existing results file's top-level cluster_saturation object (hb#132).

    Best-effort, mirroring _read_prior_at_scale_contention: a missing/malformed file
    or an absent cluster_saturation key means there is nothing to carry forward, so
    return None (the honest-absence signal the emitter understands).
    """
    try:
        prior = json.loads(out_path.read_text())
    except (FileNotFoundError, ValueError):
        return None
    cs = prior.get("cluster_saturation") if isinstance(prior, dict) else None
    return cs if isinstance(cs, dict) else None


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


def carry_prior_cluster_saturation(fresh, prior, *, generated_at: str):
    """Persist the cluster-saturation ceiling block across the daily refresh (hb#132; #112 gap).

    Exact mirror of carry_prior_at_scale_contention: the cluster-saturation block is the
    whole-cluster warm-hand-out ceiling, produced only by the heavy, manual, collision-acked
    ~40-node saturating fire — the daily single-node auto-refresh never produces one, so `fresh`
    is always None here and the prior committed block is carried forward. Without this, the daily
    `harness.run --product sandbox` refresh would build_results a wholesale write that silently
    DROPS the ceiling block from the public table.

    Fresh always wins: a real fire this run stamps `measured_at = generated_at` via setdefault and
    is returned as-is. Otherwise the prior committed block is carried forward UNCHANGED. Both paths
    flow through the closed emitter (`_coerce_cluster_saturation`), so a carried block that is not a
    valid cluster_saturation is dropped, never published as a partial lie.
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
# classifier returns {} by construction). It is armed ONLY in a coordinated,
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


# Per-mode SLO cluster-rate ingestion (hb#132/#149 wiring). DEFAULT-OFF.
#
# The matrix cluster halves are an SLO-GATED RATE (#149): the sustained creation
# rate at which p95 TTFE stays within the bar. The honest producer is a
# per-activation-mode STEP-UP SWEEP — same out-of-process CL2 orchestrator and
# scrubbed nested record shape as BENCH_STEPUP_RESULT, fired once per matrix row
# with that row's activation mode. Each sweep's derived triple merges into the
# matching scenario's sla_metrics; the derivation itself is harness/slo_rate.py.
#
# The scenario list mirrors render/schema.py's ACTIVATION_MODE_ROWS (the matrix
# rows). Kept as a literal here because the harness never imports the render
# package (offline-portability discipline); the cross-contract suite asserts the
# two stay in sync.
SLO_SWEEP_SCENARIOS = (
    "warmpool_cold_start",
    "native_digest_cold",
    "suspend_resume",
)

# Keys of the hb#132 per-cluster emit triple (schema-locked in metrics.py).
_CLUSTER_TRIPLE_KEYS = (
    "thpt_under_5s_per_cluster",
    "thpt_under_1s_per_cluster",
    "thpt_cluster_node_count",
)

# hb#230 per-bar basis stamps (mutually exclusive per cell with the whole-triple
# `thpt_slo_basis` — see results_schema._BASIS_KEYS / the mixed-basis REFUSE).
_PER_BAR_BASIS_KEYS = (
    "thpt_slo_basis_5s",
    "thpt_slo_basis_1s",
)

# Products whose runs carry a matrix cluster half to fill (hb#149 / Path A). BOTH
# gVisor (product "sandbox") and Kata microVM (product "sandbox-kata") own a
# warm-row cluster cell in render_matrix (the kata slot fills from kata_results),
# so a kata run's SLO sweep must merge its triple too. Deliberately WIDER than the
# gVisor-only gate on maybe_stepup / maybe_scale_proof / maybe_warm_vs_cold: those
# produce gVisor-page-only artifacts, whereas the cluster triple is a per-runtime
# matrix cell that both runtimes render.
_SLO_SWEEP_PRODUCTS = ("sandbox", "sandbox-kata")


def slo_sweep_env_var(scenario: str) -> str:
    """Env var holding the nested sweep-record path for one activation mode."""
    return "BENCH_SLO_SWEEP_" + scenario.upper()


def _sweep_record_runtime(rec) -> str:
    """The runtime this sweep record was MEASURED under, if the producer stamped it.

    hb#169: the BENCH_SLO_SWEEP_* env namespace is shared across the gVisor
    (product `sandbox`) and Kata+microVM (product `sandbox-kata`) merges, so a
    stale env var left set across the OTHER product's run would cross-merge one
    runtime's rate into the other's matrix cell. The durable guard is a runtime
    stamp on the record (`params.runtime_class`, which the step-up producer
    already emits; `params.runtime` also accepted for the hb#169 provenance-field
    name) checked against the merging run's product runtime in merge_slo_sweeps.

    Returns the stamped runtime string, or "" when the record carries no stamp
    (a legacy/shakeout record) — the caller tolerates absent (back-compat) and
    only rejects a PRESENT-and-mismatched stamp.
    """
    params = rec.get("params")
    if not isinstance(params, dict):
        return ""
    for key in ("runtime", "runtime_class"):
        val = params.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def merge_slo_sweeps(raw: list, product: str) -> None:
    """Merge per-mode SLO-sweep-derived cluster triples into scenario sla_metrics.

    For each activation-mode scenario, when BENCH_SLO_SWEEP_<SCENARIO> points at a
    readable nested sweep record, flatten it (stepup_adapter.stepup_nested_to_flat)
    and derive the hb#132 emit triple (slo_rate.slo_sla_metrics_from_stepup); a
    non-empty derivation merges into that scenario's raw sla_metrics, then flows
    through the closed emitter (_coerce_sla_metrics) like any measured key.

    Fail-closed, mirroring maybe_stepup: a product outside the sandbox family
    (_SLO_SWEEP_PRODUCTS = sandbox / sandbox-kata), unset/blank env,
    unreadable/malformed file, or an underivable record (no compliant rung, no
    valid node_count) merge NOTHING — the cell keeps pending, never a fabricated
    0. A sweep-derived triple OVERWRITES a direct-emit triple on the same cell:
    the sweep derivation is the preferred producer (see the cluster_node_count
    caller contract in metrics.ttfe_sla_metrics). Mutates raw in place; a
    scenario absent from this run (or with a non-dict sla_metrics) is skipped.
    Runs for BOTH runtimes: a kata run's warm-row cluster half fills from its own
    sweep, merged into the sandbox-kata scenario cell that render_matrix reads via
    kata_results.

    hb#169 runtime-match gate: the BENCH_SLO_SWEEP_* env namespace is shared by
    both runtime products, so a record STAMPED with a runtime other than the one
    this run measures (_matrix_runtime_for(product)) is a cross-runtime
    contamination — a stale env var from the other product's fire — and is
    skipped fail-closed, exactly like an unreadable/underivable record. An
    unstamped record is tolerated (back-compat: legacy/shakeout records merge as
    before under a matching product); only a PRESENT-and-mismatched stamp is
    rejected.
    """
    if product not in _SLO_SWEEP_PRODUCTS:
        return
    expected_runtime = _matrix_runtime_for(product)
    for name in SLO_SWEEP_SCENARIOS:
        path = os.environ.get(slo_sweep_env_var(name), "").strip()
        if not path:
            continue
        try:
            rec = json.loads(pathlib.Path(path).read_text())
        except (OSError, ValueError):
            continue
        stamped_runtime = _sweep_record_runtime(rec)
        if stamped_runtime and rc.runtime_family(stamped_runtime) != rc.runtime_family(expected_runtime):
            continue
        derived = slo_rate.slo_sla_metrics_from_stepup(
            stepup_adapter.stepup_nested_to_flat(rec)
        )
        if not derived:
            continue
        cell = _raw_cell(raw, name)
        if cell is None or not isinstance(cell.get("sla_metrics"), dict):
            continue
        cell["sla_metrics"].update(derived)


def carry_prior_cluster_triples(raw: list, prior_scenarios) -> None:
    """Carry each scenario's SLO cluster triple across the daily refresh.

    The cluster halves of a matrix row come from a deliberate manual sweep fire
    (merge_slo_sweeps above, or the direct emit leg), not from the daily
    single-node refresh — which wholesale-rewrites every registered scenario cell
    and would silently DROP the triple the next day. Same do-not-auto-decay
    posture as carry_prior_scale_proof / carry_prior_stepup, applied at the
    per-scenario-key level: a fresh cell that already carries ANY triple key wins
    outright (a fresh sweep or direct emit is never mixed with a stale one);
    otherwise the prior committed cell's triple is copied onto the fresh cell.

    Honesty spine: a carried triple is copied ALL-together and only when the
    prior cell has a node count plus >=1 per-cluster rate — never a per-cluster
    figure without its measurement size (the render pins X from
    thpt_cluster_node_count). Mixing scales inside one row is the hb#132 design
    itself: the per-node half is today's fire, the cluster half is the sweep's,
    and the render captions each cluster half with its own X.

    hb#174: the basis stamp (`thpt_slo_basis`, or its per-bar split
    `thpt_slo_basis_1s`/`thpt_slo_basis_5s` — the two conventions are mutually
    exclusive per cell, see results_schema._BASIS_KEYS) and the literal
    sample-size stamp (`thpt_slo_n_exec_ok`) travel WITH the triple — a carried
    rate keeps the disclosure of which measured basis produced it and how thin
    its weakest credited sample was. hb#230: the cold/warm floor-zero flag
    (`thpt_slo_floor_zero`) and each bar's per-node companion
    (`thpt_under_5s_per_node`, `thpt_under_1s_per_node`) are stamped by the same
    producer call as the per-cluster triple (_derive_cold_floor_zero /
    _derive_literal_floor_zero_5s emit them together as one dict) and must ride
    along the same way — a carry that drops them re-introduces exactly the
    silent-decay bug this function exists to prevent (a cell-downgrade fire on
    a fully-clean measurement run, traced to this allowlist predating both the
    hb#174 per-bar split and the hb#230 floor-zero/per-node keys).
    All of the above are passengers, not members: the fresh-wins check and the
    eligibility guard key on the triple keys only, so a prior cell carrying a
    stamp but no rate (a producer inconsistency) still carries nothing.

    Per-node companion caveat: unlike the basis/floor-zero stamps, the per-node
    keys are NOT always atomic with the cluster triple — metrics.ttfe_sla_metrics
    computes thpt_under_5s_per_node/thpt_under_1s_per_node on every call whether
    or not cluster_node_count is supplied, so a scenario like warmpool_cold_start
    has a genuinely fresh per-node pair on a routine refresh even though its
    cluster triple (a manual-sweep-only figure) is carried. Passenger copying is
    therefore fresh-wins PER-KEY, not just per-cell: a passenger already present
    on the fresh cell is left alone rather than overwritten by the carried
    (possibly stale) value — only an absent passenger is filled in from the
    prior row.
    """
    if not isinstance(prior_scenarios, list):
        return
    prior_by_name = {
        s.get("name"): s for s in prior_scenarios if isinstance(s, dict)
    }
    for cell in raw:
        if not isinstance(cell, dict):
            continue
        m = cell.get("sla_metrics")
        if not isinstance(m, dict) or any(k in m for k in _CLUSTER_TRIPLE_KEYS):
            continue
        prior = prior_by_name.get(cell.get("name"))
        pm = prior.get("sla_metrics") if isinstance(prior, dict) else None
        if not isinstance(pm, dict):
            continue
        carried = {k: pm[k] for k in _CLUSTER_TRIPLE_KEYS if k in pm}
        has_rate = any(
            k in carried
            for k in ("thpt_under_5s_per_cluster", "thpt_under_1s_per_cluster")
        )
        if "thpt_cluster_node_count" not in carried or not has_rate:
            continue
        for passenger in (
            "thpt_slo_basis",
            "thpt_slo_basis_1s",
            "thpt_slo_basis_5s",
            "thpt_slo_n_exec_ok",
            "thpt_slo_floor_zero",
            "thpt_under_5s_per_node",
            "thpt_under_1s_per_node",
        ):
            # Fresh wins per-key, not just per-cell: thpt_under_5s_per_node /
            # thpt_under_1s_per_node are NOT atomic with the cluster triple in
            # general -- metrics.ttfe_sla_metrics computes them on every call
            # regardless of whether cluster_node_count is supplied, so a
            # scenario like warmpool_cold_start has a genuinely fresh per-node
            # pair on every routine refresh even when the cluster triple itself
            # is carried. Unconditionally overwriting via m.update(carried)
            # would silently discard that fresh measurement in favor of a
            # stale carried one -- the same class of bug this function exists
            # to prevent, just aimed at a different key. Only carry a
            # passenger the fresh cell doesn't already have.
            if passenger in pm and passenger not in m:
                carried[passenger] = pm[passenger]
        m.update(carried)


def carry_prior_basis_caveats(raw: list, prior_scenarios) -> None:
    """Carry a prior STANDALONE per-bar basis caveat across the daily refresh.

    carry_prior_cluster_triples above carries the per-bar basis stamps
    (`thpt_slo_basis_5s`/`_1s`) ONLY as passengers riding a valid cluster triple
    — a prior cell that stamps a per-bar basis but has NO triple carries nothing
    (the "producer inconsistency" branch, keyed on the triple keys only). That is
    the wrong call for the ONE legitimate stamp-without-triple shape: the hb#230
    `unresolved_bounds_bar_bracketed` caveat, hand-authored onto native_digest_cold
    by regen_hb230_caveat_cells.py. It records that the bar's SLO rate was
    genuinely MEASURED but sits inside an unresolvable [lower,upper] bracket, so no
    per-cluster rate can be published (the cell renders `unk.***`) — a true
    disclosure with deliberately no triple to ride. A refresh that re-measures the
    cell drops the caveat (no sweep env reproduces it, and the triple-passenger
    carry skips it), and check_cell_downgrade then correctly fires on the lost
    sla_metrics key — turning an honest re-measure into a refused write.

    This carry closes that gap with the same do-not-auto-decay posture, scoped
    tightly to avoid re-introducing a mixed-basis or shadow-a-fresh-sweep bug:

      * fresh cell must carry NO cluster-triple key (a fresh triple owns its own
        basis — that path is carry_prior_cluster_triples', not this one), AND
      * fresh cell must carry NO basis key of any convention (whole-triple or
        per-bar) — never overlay a caveat onto a cell that stamped its own basis,
        and never create the schema's mixed-basis REFUSE shape, AND
      * prior cell must hold a per-bar basis stamp AND itself have NO triple (a
        genuine standalone caveat, not a triple passenger).

    Only the per-bar keys are carried (a standalone whole-triple `thpt_slo_basis`
    with no triple is meaningless — it describes a triple that isn't there). Runs
    AFTER carry_prior_cluster_triples so a fresh or carried triple always wins.
    """
    if not isinstance(prior_scenarios, list):
        return
    prior_by_name = {
        s.get("name"): s for s in prior_scenarios if isinstance(s, dict)
    }
    for cell in raw:
        if not isinstance(cell, dict):
            continue
        m = cell.get("sla_metrics")
        if not isinstance(m, dict):
            continue
        # Fresh cell must own neither a triple nor any basis stamp of its own.
        if any(k in m for k in _CLUSTER_TRIPLE_KEYS):
            continue
        if any(k in m for k in ("thpt_slo_basis", *_PER_BAR_BASIS_KEYS)):
            continue
        prior = prior_by_name.get(cell.get("name"))
        pm = prior.get("sla_metrics") if isinstance(prior, dict) else None
        if not isinstance(pm, dict):
            continue
        # Prior must be a genuine standalone caveat: a per-bar basis stamp with
        # NO triple (a triple-passenger caveat is already handled above).
        if any(k in pm for k in _CLUSTER_TRIPLE_KEYS):
            continue
        carried = {k: pm[k] for k in _PER_BAR_BASIS_KEYS if k in pm}
        if carried:
            m.update(carried)


def carry_prior_density(raw: list, prior_scenarios) -> None:
    """Carry `density_per_vcpu` across the daily refresh (hb#206).

    Max Density is definitionally a cross-fire value: the deliberate saturation
    probe MEASURES it, and the canonical refresh merely PUBLISHES it via the
    BENCH_DENSITY_* env stamp on the density-source scenario. A refresh fired
    without those envs therefore produces a row with no density — which, pre
    this carry, silently reverted the rendered cell to `pending` (the 5.98
    gVisor loss, published 07-02 at 6c85606, lost 07-04). Same
    do-not-auto-decay posture as carry_prior_cluster_triples above, scoped to
    the ONE field whose provenance is cross-fire by design — generalizing this
    carry to same-fire metrics (ttfe, exec rates) would mix two fires' numbers
    in one row, which the honesty spine forbids.

    Fresh wins outright: a row that already carries density_per_vcpu (a new
    env-stamped fire) is never overwritten. The prior must be a measured
    (non-pending) row holding a finite non-negative real density — a pending
    placeholder or malformed value carries nothing.
    """
    if not isinstance(prior_scenarios, list):
        return
    prior_by_name = {
        s.get("name"): s for s in prior_scenarios if isinstance(s, dict)
    }
    for cell in raw:
        if not isinstance(cell, dict):
            continue
        m = cell.get("sla_metrics")
        if not isinstance(m, dict) or "density_per_vcpu" in m:
            continue
        prior = prior_by_name.get(cell.get("name"))
        if not isinstance(prior, dict):
            continue
        p_out = prior.get("outcome")
        if not isinstance(p_out, str) or p_out.lower() == "pending":
            continue
        pm = prior.get("sla_metrics")
        if not isinstance(pm, dict):
            continue
        val = pm.get("density_per_vcpu")
        if (
            isinstance(val, (int, float)) and not isinstance(val, bool)
            and math.isfinite(val) and val >= 0
        ):
            m["density_per_vcpu"] = val


_PER_CLUSTER_RATE_KEYS = (
    "thpt_under_5s_per_cluster",
    "thpt_under_1s_per_cluster",
)


def finalize_cluster_node_count(raw: list) -> list[str]:
    """hb#214 part 2: verify every per-cluster SLO rate carries its measurement size.

    The render pins the "at N nodes" X from `thpt_cluster_node_count`; a
    per-cluster rate without it has no disclosed measurement size, so the
    render's defense-in-depth silently pends/drops the block at publish time.
    Every in-harness producer of the triple is all-or-nothing (ttfe_sla_metrics,
    slo_sla_metrics_from_stepup, carry_prior_cluster_triples), so the leak path
    is a manual data-only fire or a future leg emitting rates bare. This is the
    finalize-time encode-then-merge stamp: node_count is stamped from run
    provenance and verified present as part of finalize — never "publish now,
    stamp later".

    Stamp source is BENCH_NODE_COUNT *explicitly set* to a valid int >= 1.
    build_provenance's silent default of 1 is deliberately NOT used here:
    silently stamping 1 onto a multi-node fire would fabricate the measurement
    size, which is worse than failing loud. A leg-emitted node_count always
    wins (the leg measured it); the stamp fills only the gap.

    Mutates raw in place. Returns problem lines (empty == verified). No
    BENCH_ALLOW_* escape hatch — the fix is to set BENCH_NODE_COUNT correctly.
    """
    problems: list[str] = []
    env_raw = os.environ.get("BENCH_NODE_COUNT")
    env_node_count = None
    env_error = None
    if env_raw is not None:
        try:
            v = int(env_raw)
        except ValueError:
            env_error = f"BENCH_NODE_COUNT={env_raw!r} is not an int"
        else:
            if v < 1:
                env_error = f"BENCH_NODE_COUNT={env_raw!r} must be >= 1"
            else:
                env_node_count = v
    for cell in raw:
        if not isinstance(cell, dict):
            continue
        sla = cell.get("sla_metrics")
        if not isinstance(sla, dict):
            continue
        if "thpt_cluster_node_count" in sla:
            continue
        rate_keys = [k for k in _PER_CLUSTER_RATE_KEYS if k in sla]
        if not rate_keys:
            continue
        name = cell.get("name", "<unnamed>")
        if env_node_count is not None:
            sla["thpt_cluster_node_count"] = env_node_count
            log.info(
                "stamped thpt_cluster_node_count=%d onto %s from run provenance "
                "(BENCH_NODE_COUNT; leg emitted %s without it)",
                env_node_count, name, "/".join(rate_keys),
            )
        elif env_error is not None:
            problems.append(
                f"{name}: {'/'.join(rate_keys)} without "
                f"thpt_cluster_node_count; {env_error}"
            )
        else:
            problems.append(
                f"{name}: {'/'.join(rate_keys)} without thpt_cluster_node_count "
                "and BENCH_NODE_COUNT is unset — no provenance source to stamp from"
            )
    return problems


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


def _matrix_runtime_for(product: str) -> str:
    """Derive provenance.runtime for the sandbox matrix (#3942/#830).

    render's render_matrix selects the measured runtime column from
    provenance.runtime; the emit path derives it product-side (sandbox->gvisor,
    sandbox-kata->kata-microvm) with a BENCH_MATRIX_RUNTIME override for a fire that
    pins a runtimeClassName off the product default. Returns "" for products with
    no matrix runtime (substrate), so build_provenance omits the key and never
    carries a meaningless runtime onto a non-matrix product. An out-of-enum
    override is NOT validated here — the emitter's _coerce_provenance fails closed
    on it (single source of truth), mirroring the cold_start_mode pattern.
    """
    override = os.environ.get("BENCH_MATRIX_RUNTIME", "").strip()
    if override:
        return override
    return {
        "sandbox": "gvisor",
        "sandbox-kata": "kata-microvm",
    }.get(product, "")


def build_provenance(
    substrate: str,
    product: str = results_schema.DEFAULT_PRODUCT,
    prior_machine_type: str | None = None,
    prior_warmpool_ttfe_p95: float | None = None,
) -> dict:
    prov = {
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
    # Node machine shape (PR#313 review): stamps the rig every run so a
    # machine-class change between a comparison's baseline and its refresh is self-describing
    # on the page (the reproducible-numbers contract needs the machine class, not just the
    # substrate label — two runs can share cluster_substrate=gke-sandbox on different machine
    # shapes, e.g. the ephemeral hb-refresh CI cluster vs a persistent internal
    # cluster). Absent env -> key omitted (unknown rig is never guessed).
    machine_type = os.environ.get("BENCH_MACHINE_TYPE", "").strip()
    if machine_type:
        prov["machine_type"] = machine_type
        # Machine-class-change caveat (PR#313 review): carry the PREVIOUS
        # published run's machine_type forward as its own field (rather than diffing here)
        # so the renderer can data-key an honest "this delta may be machine-class, not
        # substrate" caveat off two closed-schema-validated values — same posture as the
        # drained-regime caveat. Stamped only when the rig actually changed: a same-rig
        # refresh needs no caveat, and this also means a run with no prior machine_type on
        # record (the very first stamped run) never emits a spurious comparison.
        if prior_machine_type and prior_machine_type != machine_type:
            prov["prior_machine_type"] = prior_machine_type
    # Matrix runtime column (#3942/#830): emitted only for sandbox-family products
    # so render flips that runtime's rows to measured and the other to pending.
    runtime = _matrix_runtime_for(product)
    if runtime:
        prov["runtime"] = runtime
        # vCPU-footprint axis (#3868): the per-sandbox DECLARED request the density
        # figures were measured under — a run-level property of the runtime, not a
        # per-scenario measurement, so it rides provenance next to `runtime`. Sourced
        # from the SAME `container_resources_from_env` the matrix scenarios pod-spec
        # with (runtime_family normalizes kata-microvm -> the kata floor), so it
        # picks up any BENCH_POD_* override in lock-step. Sandbox-family only —
        # substrate omits `runtime`, so it never gets a footprint either.
        requests = rc.container_resources_from_env(runtime).get("requests", {})
        prov["sandbox_cpu_request_m"] = metrics.parse_cpu_millicores(requests["cpu"])
        prov["sandbox_mem_request_mib"] = metrics.parse_mem_mib(requests["memory"])
        # Node-image / gVisor runsc version (hb#317, mirrors machine_type's
        # hb#313 pattern): same env-passthrough-or-omit posture — absent
        # env means the key is omitted, never guessed. Sandbox-family only (same
        # gate as `runtime` above), since these fields only matter where a sandbox
        # runtime's node-side build is in play. No caveat-diffing logic here (unlike
        # machine_type) — landing in provenance is sufficient for a future regression
        # investigation to read back historically; non-goal per hb#317.
        node_image = os.environ.get("BENCH_NODE_IMAGE", "").strip()
        if node_image:
            prov["node_image"] = node_image
        runsc_version = os.environ.get("BENCH_RUNSC_VERSION", "").strip()
        if runsc_version:
            prov["runsc_version"] = runsc_version
        # Prior-run North Star TTFE p95 (hb#5414 refresh-delta tripwire): carry
        # the PREVIOUSLY published warmpool_cold_start p95 forward as its own
        # provenance field (rather than diffing here), same posture as
        # prior_machine_type — the renderer data-keys a ">2x delta" / verdict-
        # flip caveat off two closed-schema-validated values. Unlike
        # prior_machine_type's "only if it differs" gate, stamped whenever a
        # prior value exists: TTFE p95 is EXPECTED to vary every run, so the
        # renderer needs the prior on every run to compute the delta, not just
        # on a qualitative change. Sandbox-family only (same `if runtime:`
        # gate as node_image/runsc_version) since warmpool_cold_start is a
        # sandbox-family-only scenario.
        if prior_warmpool_ttfe_p95:
            prov["prior_warmpool_ttfe_p95_ms"] = prior_warmpool_ttfe_p95
    return prov


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
    # Refuse a refresh that would silently downgrade a graduated cell's sample
    # size (hb#198): a fire missing the graduation-shape knobs must fail loudly
    # here, not publish a lower-n row. Deliberate downgrades stay possible via
    # the explicit BENCH_ALLOW_N_REGRESSION opt-in, which converts the refusal
    # into a loud warning — the decision is explicit either way.
    n_regressions = check_n_regression(raw, prior_scenarios)
    if n_regressions:
        for line in n_regressions:
            log.error("n-regression: %s", line)
        if _env_flag("BENCH_ALLOW_N_REGRESSION"):
            log.warning(
                "BENCH_ALLOW_N_REGRESSION set — publishing %d lower-n cell(s) "
                "anyway (deliberate downgrade)", len(n_regressions),
            )
        else:
            log.error(
                "refusing to write %s: %d cell(s) would regress below their "
                "committed sample size. Re-fire with the canonical refresh "
                "command in recipe/REPRODUCE.md (graduation-shape env knobs), "
                "or set BENCH_ALLOW_N_REGRESSION=1 to downgrade deliberately.",
                out, len(n_regressions),
            )
            return 1
    prior_scale_proof = _read_prior_scale_proof(out)
    prior_stepup = _read_prior_stepup(out)
    prior_warm_vs_cold = _read_prior_warm_vs_cold(out)
    prior_kata_activation = _read_prior_kata_activation(out)
    prior_concurrent_burst = _read_prior_concurrent_burst(out)
    prior_warm_pool_acquisition = _read_prior_warm_pool_acquisition(out)
    prior_at_scale_contention = _read_prior_at_scale_contention(out)
    prior_cluster_saturation = _read_prior_cluster_saturation(out)
    prior_provisioning_rate_sweep = _read_prior_provisioning_rate_sweep(out)
    prior_machine_type = _read_prior_provenance_machine_type(out)
    prior_warmpool_ttfe_p95 = _read_prior_warmpool_ttfe_p95(out)
    raw = merge_seed_placeholders(raw, prior_scenarios)
    # Per-mode SLO cluster-rate legs (hb#132/#149): fresh env-armed sweep
    # derivations merge first (fresh wins), then prior committed triples carry
    # onto cells that still lack them, so the matrix cluster halves neither decay
    # on the daily refresh nor shadow a deliberate new sweep.
    merge_slo_sweeps(raw, args.product)
    carry_prior_cluster_triples(raw, prior_scenarios)
    # Carry a prior STANDALONE per-bar basis caveat (hb#230
    # unresolved_bounds_bar_bracketed) that has no triple to ride — the
    # triple-passenger carry above skips it, so an honest re-measure would drop
    # the still-true caveat and trip check_cell_downgrade. Runs AFTER the triple
    # carry so a fresh or carried triple always wins.
    carry_prior_basis_caveats(raw, prior_scenarios)
    # Carry the cross-fire Max Density value (hb#206): the saturation probe
    # measures it, the canonical refresh publishes it via env stamp — a refresh
    # without the density envs must not decay the published cell to pending.
    carry_prior_density(raw, prior_scenarios)
    # hb#214: encode-then-merge — every per-cluster SLO rate must carry its
    # measurement size BEFORE the write. Stamp from run provenance where the leg
    # omitted it; refuse the write when neither source has it, so the gap fails
    # HERE at finalize instead of silently pending at render/publish time. Runs
    # AFTER both triple producers (merge_slo_sweeps + carry_prior_cluster_triples)
    # so it sees final cell state, and BEFORE check_cell_downgrade (stamping only
    # ADDS a key — never a downgrade).
    node_count_gaps = finalize_cluster_node_count(raw)
    if node_count_gaps:
        for line in node_count_gaps:
            log.error("node-count-gap: %s", line)
        log.error(
            "refusing to write %s: %d per-cluster rate cell(s) lack "
            "thpt_cluster_node_count and BENCH_NODE_COUNT provides no valid "
            "run-provenance value to stamp. Set BENCH_NODE_COUNT to the fire's "
            "node count (the same value build_provenance records) and re-run.",
            out, len(node_count_gaps),
        )
        return 1
    # Refuse a refresh that would silently downgrade ANY published cell —
    # measured→pending outcome, sla_metrics key loss, or a dropped measured row
    # (hb#206, generalizing the n-scoped guard above to cell-state transitions).
    # Runs AFTER all scenario-level carries so legitimately-carried fields
    # (cluster triples, density) never false-positive. Deliberate downgrades
    # stay possible via the explicit BENCH_ALLOW_CELL_DOWNGRADE opt-in.
    cell_downgrades = check_cell_downgrade(raw, prior_scenarios)
    if cell_downgrades:
        for line in cell_downgrades:
            log.error("cell-downgrade: %s", line)
        if _env_flag("BENCH_ALLOW_CELL_DOWNGRADE"):
            log.warning(
                "BENCH_ALLOW_CELL_DOWNGRADE set — publishing %d downgraded "
                "cell(s) anyway (deliberate downgrade)", len(cell_downgrades),
            )
        else:
            log.error(
                "refusing to write %s: %d cell(s) would downgrade a published "
                "value (measured->pending, lost sla_metrics key, or dropped "
                "row). Re-fire carrying the envs the committed cells were "
                "measured with, or set BENCH_ALLOW_CELL_DOWNGRADE=1 to "
                "downgrade deliberately.",
                out, len(cell_downgrades),
            )
            return 1
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
    # Carry the cluster-saturation ceiling block across the daily refresh (hb#132).
    # Same posture as at_scale_contention: no in-process producer (written only by the
    # heavy manual ~40-node saturating fire), so `fresh` is always None and the prior
    # committed block is carried forward — without this the daily refresh would
    # build_results a wholesale write that DROPS it.
    cluster_saturation = carry_prior_cluster_saturation(
        None, prior_cluster_saturation, generated_at=generated_at
    )
    # Carry the provisioning rate-sweep block across the daily refresh (#4086). Same
    # posture as the producer-less blocks above: no in-process producer, so `fresh` is
    # always None and the prior committed block is carried forward — without this the
    # daily refresh would build_results a wholesale write that DROPS it.
    provisioning_rate_sweep = carry_prior_provisioning_rate_sweep(
        None, prior_provisioning_rate_sweep, generated_at=generated_at
    )
    results = results_schema.build_results(
        raw, build_provenance(
            substrate, args.product,
            prior_machine_type=prior_machine_type,
            prior_warmpool_ttfe_p95=prior_warmpool_ttfe_p95,
        ),
        generated_at=generated_at, product=args.product,
        scale_proof=scale_proof, stepup=stepup, warm_vs_cold=warm_vs_cold_obj,
        kata_activation=kata_activation, concurrent_burst=concurrent_burst,
        warm_pool_acquisition=warm_pool_acquisition,
        at_scale_contention=at_scale_contention,
        cluster_saturation=cluster_saturation,
        provisioning_rate_sweep=provisioning_rate_sweep,
    )

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2, sort_keys=True) + "\n")
    log.info("wrote %d scenario cells to %s", len(results["scenarios"]), out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
