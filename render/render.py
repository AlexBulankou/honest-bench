"""Closed-schema README renderer for the public honest-benchmarks repo.

Consumes a harness results JSON and emits a Markdown table + build banner + provenance
footer. It is generate-only: every cell traces to a schema-validated field of the input,
goal columns render "(non-public)" when the internal targets file is absent, and any field
not declared in schema.py is silently dropped. No hand-entered numbers, no free-text.

Usage:
  python3 render.py <results.json> [--targets <targets.json>]   # prints one product table
"""

import argparse
import json
import sys

from schema import (
    ACTIVATION_MODE_ROWS,
    BADGE_CONSTRUCTIONS,
    BADGE_SCOPES,
    BURST_CORROBORATION_FIELDS,
    CONCURRENT_BURST_FIELDS,
    DENSITY_SOURCE_SCENARIOS,
    GOAL_COLUMNS,
    HISTORY_FIELDS,
    KATA_ACTIVATION_FIELDS,
    MATRIX_METRIC_FIELDS,
    MATRIX_RUNTIMES,
    METRIC_LABELS,
    NON_PUBLIC,
    OUTCOMES,
    PENDING_REASONS,
    PRODUCTS,
    PROVENANCE_FIELDS,
    RUNTIME_LABELS,
    SCALE_PROOF_FIELDS,
    SCENARIO_LABELS,
    STEPUP_PARETO_FIELDS,
    TTFE_COMPARABILITY_MIN_N,
    WARM_POOL_ACQUISITION_FIELDS,
    WARM_VS_COLD_FIELDS,
    _ISO,
)


def _clean_provenance(prov):
    """Return only schema-declared provenance keys whose values validate. Drops the rest."""
    if not isinstance(prov, dict):
        return {}
    out = {}
    for key, ok in PROVENANCE_FIELDS.items():
        if key in prov:
            try:
                if ok(prov[key]):
                    out[key] = prov[key]
            except (TypeError, ValueError):
                pass
    return out


def _clean_metrics(metrics):
    """Keep only known metric keys with numeric (non-bool) values."""
    if not isinstance(metrics, dict):
        return {}
    out = {}
    for key, val in metrics.items():
        if key in METRIC_LABELS and isinstance(val, (int, float)) and not isinstance(val, bool):
            out[key] = val
    return out


def _clean_scenarios(scenarios):
    """Map each row through the closed schema. Returns (rows, dropped_count).

    A row is dropped (not rendered) when its name is not in the scenario vocabulary or its
    outcome is not a known enum — exactly the cases where unexpected harness output could
    otherwise leak. Dropped rows are counted (a safe integer) so the drop is visible.
    """
    rows, dropped = [], 0
    if not isinstance(scenarios, list):
        return rows, dropped
    for s in scenarios:
        if not isinstance(s, dict):
            dropped += 1
            continue
        name = s.get("name")
        outcome = s.get("outcome")
        if name not in SCENARIO_LABELS or outcome not in OUTCOMES:
            dropped += 1
            continue
        reason = s.get("pending_reason")
        if reason is not None and reason not in PENDING_REASONS:
            reason = None  # drop unknown free-text reason, keep the row
        scope = s.get("badge_scope")
        if scope is not None and scope not in BADGE_SCOPES:
            scope = None  # drop unknown scope, keep the row (never render free-text)
        construction = s.get("badge_construction")
        if construction is not None and construction not in BADGE_CONSTRUCTIONS:
            construction = None  # drop unknown construction, keep the row
        n = s.get("n")
        n = n if isinstance(n, int) and not isinstance(n, bool) and n >= 0 else 0
        rows.append(
            {
                "label": SCENARIO_LABELS[name],
                "outcome": outcome,
                "pending_reason": reason,
                "badge_scope": scope,
                "badge_construction": construction,
                "n": n,
                "metrics": _clean_metrics(s.get("sla_metrics")),
            }
        )
    return rows, dropped


def _goal_cells():
    """Goal columns are (non-public) by construction in the public render.

    There is deliberately no targets-file input here: the internal targets never ship to
    the public repo, so the committed/target/north-star columns can only ever be (non-public).
    Keeping the targets out of this code path (rather than reading-then-suppressing) is what
    makes the guarantee structural instead of conditional.
    """
    return {c: NON_PUBLIC for c in GOAL_COLUMNS}


def _measured_cell(row, cold_start_mode=None):
    if row["outcome"] == "pending":
        reason = row["pending_reason"] or "not-yet-measured"
        return f"pending ({reason})"
    if row["outcome"] == "FAIL":
        reason = row["pending_reason"] or "not-yet-measured"
        return f"FAIL ({reason})"
    # badge_scope (#3905) qualifies what a security-isolation PASS asserts (control-plane
    # admission vs data-plane enforcement); suffix it on the PASS token so the badge cannot
    # over-claim. Absent ⇒ no suffix (graceful degradation). Applies to both the metric and
    # bare PASS forms. badge_construction (#3950) is an ORTHOGONAL second term naming WHICH
    # NetworkPolicy mechanism was measured (standard-np vs managed-np); it renders only
    # alongside a scope (it qualifies the enforcement claim and is meaningless alone), so an
    # `enforced` flip discloses the mechanism (e.g. "PASS (enforced, standard-np)") and can
    # never be read as a managed-gke-sandbox-NP guarantee it does not make.
    scope = row.get("badge_scope")
    construction = row.get("badge_construction")
    if scope and construction:
        pass_token = f"PASS ({scope}, {construction})"
    elif scope:
        pass_token = f"PASS ({scope})"
    else:
        pass_token = "PASS"
    if row["metrics"]:
        parts = []
        for k in sorted(row["metrics"]):
            part = f"{METRIC_LABELS[k]} {row['metrics'][k]:g}"
            # cold_start_mode (#3894) is run-level provenance describing the image-cache
            # posture of the cold-start measurement; surface it next to cold_start_ms so a
            # cold-pull number (which includes full layer download) is not misread as a
            # warm-cached cold-provision one. Absent ⇒ no label (graceful degradation).
            if k == "cold_start_ms" and cold_start_mode:
                part += f" ({cold_start_mode})"
            parts.append(part)
        return pass_token + " · " + ", ".join(parts)
    return pass_token


def render_product(results):
    """results: parsed dict. Goal columns always render (non-public) — see _goal_cells."""
    product = results.get("product")
    if product not in PRODUCTS:
        raise ValueError(f"unknown product (not in closed schema): {product!r}")

    prov = _clean_provenance(results.get("provenance"))
    # cold_start_mode rides in provenance (run-level) but renders on the cold_start_ms cell,
    # not the build banner (kept out of banner_order below to avoid double-rendering).
    cold_start_mode = prov.get("cold_start_mode")
    rows, dropped = _clean_scenarios(results.get("scenarios"))
    goals = _goal_cells()

    lines = [f"## {product}", ""]
    # scorecard table
    header = ["Scenario", "Measured (N)"] + [c.title() for c in GOAL_COLUMNS]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")
    for r in rows:
        cells = [
            r["label"],
            f"{_measured_cell(r, cold_start_mode=cold_start_mode)} (n={r['n']})",
            goals["committed"],
            goals["target"],
            goals["north-star"],
        ]
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")

    # build banner (only validated provenance fields)
    banner_order = [
        "cluster_substrate",
        "controller_image",
        "controller_digest",
        "crd_version",
        "suite_git_sha",
        "run_id",
        "node_count",
    ]
    banner = [f"{k}={prov[k]}" for k in banner_order if k in prov]
    if banner:
        lines.append("_build: " + " · ".join(banner) + "_")
    gen = results.get("generated_at")
    if isinstance(gen, str) and _ISO.match(gen):
        lines.append(f"_generated-at: {gen}_")
    if dropped:
        lines.append(f"_rows dropped by closed-schema guard: {dropped}_")
    lines.append("")
    return "\n".join(lines)


def _clean_history(rows):
    """Closed-schema-validate history rows, drop any that fail, sort by generated_at.

    Same discipline as the per-product render: a row renders ONLY HISTORY_FIELDS keys, each
    passing its predicate; a row missing a field or failing a predicate is dropped entirely
    (a malformed history file degrades to fewer trend rows, never to a leak).
    """
    clean = []
    if not isinstance(rows, list):
        return clean
    for r in rows:
        if not isinstance(r, dict):
            continue
        ok_all = True
        out = {}
        for key, ok in HISTORY_FIELDS.items():
            if key not in r:
                ok_all = False
                break
            try:
                if not ok(r[key]):
                    ok_all = False
                    break
            except (TypeError, ValueError):
                ok_all = False
                break
            out[key] = r[key]
        if ok_all:
            clean.append(out)
    clean.sort(key=lambda r: r["generated_at"])
    return clean


def render_trend(history_rows):
    """Render the build-over-build THROUGHPUT-COUNT trend table (#3918), or "" if empty.

    One row per distinct controller build (the accrual store is upsert-by-digest), oldest →
    newest. The headline COUNT (sandboxes ready <1s in one 1.0s burst against one warm pool)
    carries a delta-vs-prior-build column — the build-over-build trajectory alex's #1 directive
    asks for, which a single latest.json snapshot cannot show. First build is the baseline
    (delta "—"); every later build shows the signed change in COUNT vs the build before it.
    """
    rows = _clean_history(history_rows)
    if not rows:
        return ""
    lines = [
        "## Throughput — build-over-build",
        "",
        "The headline COUNT — sandboxes ready in <1s in a single 1.0s burst against one warm",
        "pool — tracked across distinct controller builds (oldest first). **Δ** is the change in",
        "COUNT vs the prior build; the first build is the baseline. Drive this COUNT up.",
        "",
    ]
    header = ["Build (controller digest)", "Date", "Sandboxes ready <1s", "Δ", "Density /vCPU", "n"]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")
    prev = None
    for r in rows:
        count = r["sandboxes_ready_under_1s"]
        if prev is None:
            delta = "—"
        else:
            d = count - prev
            delta = f"+{d:g}" if d >= 0 else f"{d:g}"
        prev = count
        cells = [
            f"`{r['controller_digest'][:19]}…`",
            r["generated_at"][:10],
            f"{count:g}",
            delta,
            f"{r['density_per_vcpu']:g}",
            f"{r['n']:g}",
        ]
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")
    return "\n".join(lines)


# --- Goal 2.1: Core Benchmark Matrix render -----------------------------------------------
# The customer-facing page is the doc's exact 9-column "Agent Sandbox Core Metrics Table":
# rows are (runtime × activation-mode), columns are throughput@TTFE-threshold / TTFE p50,p95 /
# samples / density / exec-success. The honesty spine is TTFE (the sandbox executed its first
# instruction and returned a result) — NOT pod-Ready. A cell we have not yet measured renders
# `pending`; a sub-1s throughput the p95 misses renders the harness-emitted honest `0`.

_PENDING = "pending"

# Small-sample marker for TTFE cells below TTFE_COMPARABILITY_MIN_N. A dagger, NOT the ⚠️ used
# for the exec-success honesty check — overloading ⚠️ would conflate "this run had failures"
# with "this row's N is too small to rank against another row".
_LOW_N_MARK = "†"


def _fmt_num(v):
    """Compact numeric (no trailing zeros): 4.0 -> 4, 1.86 -> 1.86."""
    return f"{v:g}"


def _fmt_ratio(r):
    """A retention ratio to 2 dp, no trailing zeros: 0.989474 -> 0.99, 1.06 -> 1.06."""
    return f"{round(r, 2):g}"


def _fmt_secs(ms):
    """Milliseconds -> the doc's seconds format: 600 -> 0.6s, 1560 -> 1.56s."""
    return f"{ms / 1000.0:g}s"


def _exec_cell(rate, n_total, n_succ=None):
    """Doc's exec-success ("Honesty Check") cell.

    100% renders plain; <100% shows the succeeded/total fraction + a ⚠️ flag (the doc's
    "92.8% (1277/1376) ⚠️"). The numerator is exec_success_n when the harness emits it,
    else derived as round(rate * N) so the fraction always reconciles to the Samples column.
    """
    cell = f"{round(rate * 100, 1):g}%"
    if rate < 1.0:
        if n_succ is None and n_total:
            n_succ = round(rate * n_total)
        if n_succ is not None and n_total:
            cell += f" ({n_succ}/{n_total})"
        cell += " ⚠️"
    return cell


def _clean_matrix_metrics(metrics):
    """Keep only MATRIX_METRIC_FIELDS keys whose values pass their predicate (closed schema)."""
    out = {}
    if not isinstance(metrics, dict):
        return out
    for key, ok in MATRIX_METRIC_FIELDS.items():
        if key in metrics:
            try:
                if ok(metrics[key]):
                    out[key] = metrics[key]
            except (TypeError, ValueError):
                pass
    return out


def _matrix_scenarios(scenarios):
    """Map scenario internal-NAME -> {outcome, n, metrics} for matrix-row lookup.

    Keyed by the harness name (not the display label) because matrix rows are addressed by
    activation-mode scenario id. sla_metrics are closed-schema-cleaned to MATRIX_METRIC_FIELDS;
    unknown metric keys are dropped before they can reach the public page.
    """
    out = {}
    if not isinstance(scenarios, list):
        return out
    for s in scenarios:
        if not isinstance(s, dict):
            continue
        name = s.get("name")
        if not isinstance(name, str):
            continue
        n = s.get("n")
        n = n if isinstance(n, int) and not isinstance(n, bool) and n >= 0 else 0
        out[name] = {
            "outcome": s.get("outcome"),
            "n": n,
            "metrics": _clean_matrix_metrics(s.get("sla_metrics")),
        }
    return out


def _runtime_density(scen_by_name):
    """Per-runtime Max-Density /vCPU: first of DENSITY_SOURCE_SCENARIOS carrying it, else any
    activation-mode scenario that emitted its own density_per_vcpu. None ⇒ render pending."""
    for name in DENSITY_SOURCE_SCENARIOS:
        sc = scen_by_name.get(name)
        if sc and "density_per_vcpu" in sc["metrics"]:
            return sc["metrics"]["density_per_vcpu"]
    for name, _label in ACTIVATION_MODE_ROWS:
        sc = scen_by_name.get(name)
        if sc and "density_per_vcpu" in sc["metrics"]:
            return sc["metrics"]["density_per_vcpu"]
    return None


def render_matrix(results):
    """Render the doc's 9-column Core Metrics Table for one results.json (one runtime measured).

    A single run measures ONE runtime (provenance.runtime, default gvisor); that runtime's rows
    fill from the measured scenarios, the other runtime's rows render `pending`. Per-metric cells
    render `pending` until the TTFE-instrumented harness emits them, so the page degrades to an
    honest skeleton rather than a blank or a guess.
    """
    product = results.get("product")
    if product not in PRODUCTS:
        raise ValueError(f"unknown product (not in closed schema): {product!r}")

    prov = _clean_provenance(results.get("provenance"))
    measured_runtime = prov.get("runtime") or "gvisor"
    scen_by_name = _matrix_scenarios(results.get("scenarios"))

    header = [
        "Runtime",
        "Activation Mode",
        "Throughput @ <5s TTFE (sb/s/node)",
        "Throughput @ <1s TTFE (sb/s/node)",
        "TTFE p50",
        "TTFE p95",
        "Samples (N)",
        "Max Density (sb/vCPU)",
        "Execution Success (Honesty Check)",
    ]
    lines = ["## Agent Sandbox — Core Metrics", ""]
    lines.append(
        "**Read TTFE down a column, not across rows.** Each activation-mode row carries its own "
        "sample size (the Samples (N) column) — they differ by orders of magnitude. A p50 over "
        "hundreds of samples and a p50 over one are not comparable: cross-row TTFE ranking is "
        f"only meaningful between rows with similar N. Rows below N={TTFE_COMPARABILITY_MIN_N} "
        f"are marked {_LOW_N_MARK} on their TTFE cells."
    )
    lines.append("")
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")

    for rt in MATRIX_RUNTIMES:
        rt_label = RUNTIME_LABELS[rt]
        measured = rt == measured_runtime
        density = _runtime_density(scen_by_name) if measured else None
        for scen_name, mode_label in ACTIVATION_MODE_ROWS:
            is_resume = scen_name == "suspend_resume"
            sc = scen_by_name.get(scen_name) if measured else None
            m = sc["metrics"] if sc else {}

            def cell(key, fmt):
                return fmt(m[key]) if key in m else _PENDING

            n_val = sc["n"] if (sc and sc["n"] > 0) else None
            n_cell = str(n_val) if n_val else _PENDING

            # Low-N TTFE cells carry a small-sample marker so a reader does not rank them
            # against a high-N row (a single-sample p50 is not a distribution). Mark only a
            # rendered measurement (not `pending`) whose N is known and below the floor.
            low_n_ttfe = n_val is not None and n_val < TTFE_COMPARABILITY_MIN_N

            def ttfe_cell(key):
                v = cell(key, _fmt_secs)
                return f"{v} {_LOW_N_MARK}" if (v != _PENDING and low_n_ttfe) else v

            thpt5 = cell("thpt_under_5s_per_node", _fmt_num)
            thpt1 = cell("thpt_under_1s_per_node", _fmt_num)
            p50 = ttfe_cell("ttfe_p50_ms")
            p95 = ttfe_cell("ttfe_p95_ms")
            if "exec_success_rate" in m:
                exec_cell = _exec_cell(m["exec_success_rate"], n_val, m.get("exec_success_n"))
            else:
                exec_cell = _PENDING
            if is_resume:
                dens_cell = "N/A"
            elif density is not None:
                dens_cell = _fmt_num(density)
            else:
                dens_cell = _PENDING

            lines.append(
                "| "
                + " | ".join(
                    [rt_label, mode_label, thpt5, thpt1, p50, p95, n_cell, dens_cell, exec_cell]
                )
                + " |"
            )
    lines.append("")

    # honesty / provenance footnotes (no internal refs — public PII fence).
    lines.append(
        "_TTFE = Time-To-First-Instruction: the sandbox executed its first instruction and "
        "returned a result — not merely pod-Ready._"
    )
    lines.append(
        "_Throughput @ <1s renders the harness-emitted `0` when the p95 misses the 1s bar "
        "(we print a zero rather than round up)._"
    )
    lines.append(
        "_Max Density is sandboxes per node-allocatable sandbox-schedulable vCPU (the "
        "per-node denominator), not per total-cluster vCPU._"
    )
    lines.append(
        "_Execution Success is the Honesty Check: <100% prints the succeeded/total fraction "
        "and a ⚠️ flag._"
    )
    lines.append(
        f"_{_LOW_N_MARK} marks a TTFE measured over fewer than N={TTFE_COMPARABILITY_MIN_N} "
        "samples — read it as a single observation, not a distribution, and do not rank it "
        "against a high-N row._"
    )
    lines.append("_Kata + microVM rows are not-yet-measured (requires-kata-microvm)._")
    lines.append("_Cells render `pending` until the TTFE-instrumented run lands._")
    lines.append("")

    banner_order = [
        "cluster_substrate",
        "controller_image",
        "controller_digest",
        "crd_version",
        "suite_git_sha",
        "run_id",
        "node_count",
    ]
    banner = [f"{k}={prov[k]}" for k in banner_order if k in prov]
    if banner:
        lines.append("_build: " + " · ".join(banner) + "_")
    gen = results.get("generated_at")
    if isinstance(gen, str) and _ISO.match(gen):
        lines.append(f"_generated-at: {gen}_")
    lines.append("")
    return "\n".join(lines)


def _clean_burst_corroboration(scenarios):
    """Find burst_create and closed-schema-clean its corroboration metrics (#3954).

    Returns {ready, exec, n, exec_success_rate, exec_success_n} ONLY when BOTH the pod-Ready
    count (sandboxes_ready_under_1s) and the executed-TTFE count (sandboxes_exec_under_1s) are
    present — that BOTH-required gate is what keeps the block INERT until the #3954 exec fields
    land (today's ready-only data renders nothing). Returns None otherwise. Any sla_metrics key
    not in BURST_CORROBORATION_FIELDS, or failing its predicate, is dropped (closed schema).
    """
    if not isinstance(scenarios, list):
        return None
    for s in scenarios:
        if not isinstance(s, dict) or s.get("name") != "burst_create":
            continue
        metrics = s.get("sla_metrics")
        if not isinstance(metrics, dict):
            return None
        clean = {}
        for key, ok in BURST_CORROBORATION_FIELDS.items():
            if key in metrics:
                try:
                    if ok(metrics[key]):
                        clean[key] = metrics[key]
                except (TypeError, ValueError):
                    pass
        if "sandboxes_ready_under_1s" not in clean or "sandboxes_exec_under_1s" not in clean:
            return None
        n = s.get("n")
        n = n if isinstance(n, int) and not isinstance(n, bool) and n >= 0 else 0
        return {
            "ready": clean["sandboxes_ready_under_1s"],
            "exec": clean["sandboxes_exec_under_1s"],
            "n": n,
            "exec_success_rate": clean.get("exec_success_rate"),
            "exec_success_n": clean.get("exec_success_n"),
        }
    return None


def render_burst_corroboration(results):
    """Render the burst-create TTFE corroboration block (#3954), or "" when INERT.

    The headline burst count is POD-READY (the weaker claim — a pod can report Ready before it
    can run your code). This block surfaces the stronger TTFE claim (the sandbox executed its
    first instruction and returned a result <1s) alongside it, and the GAP between them —
    sandboxes that reported Ready but had not yet run code, i.e. the over-claim a pod-Ready-only
    headline would hide. Rendered ONLY when both counts are present (see
    _clean_burst_corroboration), so the public page is byte-unchanged until a #3954 fire lands.
    """
    corr = _clean_burst_corroboration(results.get("scenarios"))
    if not corr:
        return ""
    gap = corr["ready"] - corr["exec"]
    lines = ["## Burst Create — TTFE Corroboration", ""]
    lines.append(
        "The headline burst count is **pod-Ready** — but a pod can report Ready before it can "
        "run your code. TTFE is the stronger claim: the sandbox *executed its first instruction "
        "and returned a result*. This block corroborates the two; the **gap** is sandboxes that "
        "reported Ready but had not yet run code."
    )
    lines.append("")
    header = ["Signal", "Count"]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")
    lines.append(f"| Pod-Ready <1s (weaker claim) | {_fmt_num(corr['ready'])} |")
    lines.append(
        f"| Executed first-instruction <1s (TTFE, stronger claim) | {_fmt_num(corr['exec'])} |"
    )
    lines.append(f"| Ready-but-not-yet-run (gap) | {_fmt_num(gap)} |")
    if corr["exec_success_rate"] is not None:
        n_total = corr["n"] or None
        lines.append(
            "| Execution success (Honesty Check) | "
            + _exec_cell(corr["exec_success_rate"], n_total, corr["exec_success_n"])
            + " |"
        )
    lines.append("")
    lines.append(
        "_Pod-Ready ≥ executed-TTFE by construction; the gap is the over-claim a pod-Ready "
        "headline would hide._"
    )
    lines.append("")
    return "\n".join(lines)


def _flat_verdict(retention):
    """✅/⚠️ flat verdict for a retention ratio; pending when absent.

    ASYMMETRIC framing (a4s2 v2 lock, PR #28): retention >= ~0.9 reads flat/linear-or-better
    — a superlinear result (>1.0) is a BEAT under the floor-not-ceiling framing, NOT a
    regression, so it must read ✅, not ⚠️. Only retention < ~0.9 reads ⚠️ No: the per-node
    number sagged as the cluster grew, i.e. the controller is the ceiling and the page says so.
    """
    if retention is None:
        return _PENDING
    return "✅ Yes" if retention >= 0.9 else "⚠️ No"


def _per_step_retention_line(points, key, noun):
    """Per-step retention (the delta) for `key` plus a convergence read.

    The Scale-Proof table's column is the ENDPOINT ratio (val@maxN / val@minN).
    That averages a single mid-sweep sag into the whole span, so a 1→2 hold
    followed by a 2→4 collapse reads the same as a uniform gentle decay. This
    subline exposes each ADJACENT step (1→2, 2→4) so the shape of the decay is
    visible — derived from the same scale_points the table already uses.

    THREE-WAY convergence read (fixes the slow-uniform-decay wording gap, a4s1's
    PR #54 fast-follow): a per-step ⚠️ is not the only way the endpoint sags. Each
    step can sit within tolerance (every step ✅, ≥0.9) yet COMPOUND to an endpoint
    below 0.9 — the table reads ⚠️ No while the steps all read ✅, an apparent
    contradiction. So:
      • any step < 0.9            → "sags mid-sweep" (a visible single-step sag)
      • all steps ≥ 0.9 but the endpoint (val@maxN/val@minN) < 0.9
                                  → "within tolerance each step but compounds to an
                                     endpoint sag" (reconciles the ⚠️ table cell)
      • all steps ≥ 0.9 and endpoint ≥ 0.9
                                  → "holds flat step-to-step"

    Returns "" for a sweep with fewer than two steps (a single step IS the endpoint
    ratio, so per-step would just restate the table) or when no step is measurable
    (a zero base everywhere). Same asymmetric ≥0.9 threshold as _flat_verdict: a
    superlinear step is a beat (✅), only a sag below 0.9 reads ⚠️.
    """
    if len(points) < 3:
        return ""
    steps = []
    all_flat = True
    measurable = False
    for prev, cur in zip(points, points[1:]):
        base = prev.get(key)
        label = f"{prev['node_count']}→{cur['node_count']}"
        if not base:
            steps.append(f"{label} {_PENDING}")
            continue
        cur_val = cur.get(key)
        if cur_val is None:
            steps.append(f"{label} {_PENDING}")
            continue
        ratio = cur_val / base
        measurable = True
        mark = "✅" if ratio >= 0.9 else "⚠️"
        if ratio < 0.9:
            all_flat = False
        steps.append(f"{label} {mark} {_fmt_ratio(ratio)}")
    if not measurable:
        return ""
    if not all_flat:
        read = "sags mid-sweep"
    else:
        base0, valN = points[0].get(key), points[-1].get(key)
        endpoint = valN / base0 if base0 else None
        if endpoint is not None and endpoint < 0.9:
            read = "within tolerance each step but compounds to an endpoint sag"
        else:
            read = "holds flat step-to-step"
    return f"_Per-step {noun} retention: " + " · ".join(steps) + f" — {read}._"


def _clean_scale_proof(results):
    """Closed-schema-validate the scale_proof object. Requires scale_points; retentions are
    optional (density_retention derives from the points if absent). None ⇒ no table."""
    sp = results.get("scale_proof")
    if not isinstance(sp, dict):
        return None
    pts = sp.get("scale_points")
    try:
        if not SCALE_PROOF_FIELDS["scale_points"](pts):
            return None
    except (TypeError, ValueError):
        return None
    points = sorted(pts, key=lambda p: p["node_count"])

    def _ratio(key):
        v = sp.get(key)
        try:
            return v if SCALE_PROOF_FIELDS[key](v) else None
        except (TypeError, ValueError):
            return None

    dens_ret = _ratio("density_retention")
    if dens_ret is None and points and points[0]["density"]:
        dens_ret = points[-1]["density"] / points[0]["density"]
    ma = sp.get("measured_at")
    try:
        measured_at = ma if SCALE_PROOF_FIELDS["measured_at"](ma) else None
    except (TypeError, ValueError):
        measured_at = None
    return {
        "points": points,
        "density_retention": dens_ret,
        "thpt_retention": _ratio("thpt_retention"),
        "measured_at": measured_at,
    }


def render_scale_proof(results):
    """Render the doc's Scale Proof (Linearity Check) table, or "" when no scale_proof present.

    Proof that per-node throughput + density hold flat as the cluster grows — the linearity the
    doc's second table asserts. Retention >= ~0.9 reads ✅ (flat or a superlinear beat); only a
    sag below ~0.9 reads ⚠️ (controller-is-ceiling). See _flat_verdict for the asymmetric framing.
    """
    sp = _clean_scale_proof(results)
    if not sp:
        return ""
    nodes = " → ".join(str(p["node_count"]) for p in sp["points"])
    dens_seq = " → ".join(_fmt_num(p["density"]) for p in sp["points"])
    dens_verdict = _flat_verdict(sp["density_retention"])
    if sp["density_retention"] is not None:
        dens_verdict += f" ({dens_seq})"
    thpt_verdict = _flat_verdict(sp["thpt_retention"])

    header = ["Nodes Tested", "Density Holds Flat?", "Throughput Holds Flat?"]
    lines = ["## Scale Proof (Linearity Check)", ""]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")
    lines.append("| " + " | ".join([nodes, dens_verdict, thpt_verdict]) + " |")
    lines.append("")
    for key, noun in (("density", "density"), ("throughput", "throughput")):
        step_line = _per_step_retention_line(sp["points"], key, noun)
        if step_line:
            lines.append(step_line)
            lines.append("")
    # Dated subline (#3952): the Scale Proof is a point-in-time multi-node sweep,
    # carried forward across the daily single-node refresh — so it carries its own
    # measured date, honestly distinct from the page's daily-refreshed timestamp.
    if sp.get("measured_at"):
        lines.append(
            f"_Measured {sp['measured_at'][:10]} — node-count linearity sweep "
            "(point-in-time; refreshed on the next multi-node sweep)._"
        )
        lines.append("")
    return "\n".join(lines)


# Public semantic labels for the warm-vs-cold legs. TTFE is the page's headline metric
# (executed first-instruction, returned a result); TTFI is the weaker "accepted" claim.
# Out-of-enum semantics never reach here — the closed-schema predicate drops the block.
_SEMANTIC_LABELS = {
    "ttfe": "TTFE (executed first-instruction)",
    "ttfi": "TTFI (first-instruction accepted)",
}


def _clean_warm_vs_cold(results):
    """Closed-schema-validate the TOP-LEVEL warm_vs_cold object (#3954 sibling).

    Returns the cleaned dict ONLY when every REQUIRED field (warm_p50_ms, cold_ms, speedup,
    semantic, runtime_class) is present and passes its predicate AND warm_p50_ms/cold_ms are
    strictly positive (a zero leg makes the ratio undefined); None otherwise (⇒ INERT). n_warm
    is optional. runtime_class is validated against the PUBLIC RUNTIME_LABELS enum, so a
    free-text or out-of-enum runtime fails closed and drops the whole block.
    """
    wc = results.get("warm_vs_cold")
    if not isinstance(wc, dict):
        return None
    clean = {}
    for key, ok in WARM_VS_COLD_FIELDS.items():
        if key in wc:
            try:
                if ok(wc[key]):
                    clean[key] = wc[key]
            except (TypeError, ValueError):
                pass
    for req in ("warm_p50_ms", "cold_ms", "speedup", "semantic", "runtime_class"):
        if req not in clean:
            return None
    if clean["warm_p50_ms"] <= 0 or clean["cold_ms"] <= 0:
        return None
    # cold_start_mode (#4024) is OPTIONAL but NOT silently-droppable: a present-but-invalid
    # mode (e.g. a typo "cold-provison") must fail the block CLOSED rather than fall through
    # to the true-cold default phrasing, which would silently over-claim unique-image. The
    # validation loop above only adds it to `clean` when valid, so "in wc but not in clean"
    # == present-but-invalid ⇒ INERT. Absent stays valid (⇒ true-cold default). (a4s1 ask.)
    if "cold_start_mode" in wc and "cold_start_mode" not in clean:
        return None
    return clean


# Public cold-leg phrasing keyed by cold_start_mode (#4024). The warm-vs-cold cold leg can
# be a true unique-image cold pull (cold-pull, the locked Framing-A native_digest_cold leg)
# or a warm-pool-overflow fresh-node provision off the SHARED base image (cold-provision) —
# NOT the same cost, so an overflow provision must never claim "unique-image". Each entry
# supplies the three public surfaces the cold semantic touches: the table leg label, the
# headline cold-descriptor, and the mechanism sentence. Absent ⇒ _COLD_LEG_DEFAULT, which is
# byte-identical to the pre-#4024 hardcoded true-cold phrasing, so the existing locked block
# + its tests are unchanged (graceful degradation, mirrors _measured_cell).
_COLD_LEG = {
    "cold-pull": {
        "leg": "True-cold (unique-image)",
        "descriptor": "a true-cold start",
        "mechanism": ("The warm pool keeps a ready slot so a claim skips the fresh-node "
                      "image-pull path a cold start pays in full."),
    },
    "cold-provision": {
        "leg": "Cold-provision (node overflow)",
        "descriptor": "a cold-provision start (warm-pool overflow)",
        "mechanism": ("The warm pool keeps a ready slot so a claim skips the fresh-node "
                      "provisioning path an overflow claim pays when the pool is exhausted "
                      "— provisioning off the SHARED base image (one node-cacheable image, "
                      "NOT a unique image per claim)."),
    },
}
_COLD_LEG_DEFAULT = _COLD_LEG["cold-pull"]


def render_warm_vs_cold(results):
    """Render the warm-vs-cold speedup block (#3954 sibling), or "" when INERT.

    Composes the warm leg (warm-pool TTFx p50) and the true-cold leg (unique-image cold) into
    ONE honest headline a reader can quote: warm provisioning is N times faster than cold. INERT
    (returns "") until the harness emits a complete, closed-schema-clean warm_vs_cold object —
    the classifier itself fails closed if the two legs ever diverge in semantic or runtime class.
    """
    wc = _clean_warm_vs_cold(results)
    if not wc:
        return ""
    rt_label = RUNTIME_LABELS[wc["runtime_class"]]
    sem_label = _SEMANTIC_LABELS[wc["semantic"]]
    # Recompute the displayed ratio from the two displayed legs rather than printing the
    # emitter's `speedup` verbatim, so the headline/table/footnote can never contradict the
    # legs shown beside them (the footnote literally claims "computed from the displayed
    # values"). The legs are strictly-positive-gated in _clean_warm_vs_cold, so the ratio is
    # always defined and positive — this also closes an emitter speedup<=0.
    speedup = _fmt_num(wc["cold_ms"] / wc["warm_p50_ms"])
    cold = _COLD_LEG.get(wc.get("cold_start_mode"), _COLD_LEG_DEFAULT)
    lines = ["## Warm-vs-Cold Speedup", ""]
    lines.append(
        f"A warm-pool provision is **{speedup}× faster** than {cold['descriptor']} "
        f"({rt_label}). {cold['mechanism']} Both legs are measured the same way "
        f"({sem_label}); the ratio is the portable headline you can reproduce on your own "
        "cluster.")
    lines.append("")
    header = ["Leg", _SEMANTIC_LABELS[wc["semantic"]].split(" ")[0] + " (p50)"]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")
    lines.append(f"| Warm-pool hit ({rt_label}) | {_fmt_secs(wc['warm_p50_ms'])} |")
    lines.append(f"| {cold['leg']} | {_fmt_secs(wc['cold_ms'])} |")
    lines.append(f"| Speedup (warm is N× faster) | {speedup}× |")
    lines.append("")
    n_note = f" over n={wc['n_warm']} warm claims" if "n_warm" in wc else ""
    lines.append(
        f"_Speedup = cold ÷ warm, computed from the displayed values{n_note}; the warm leg "
        "is the p50 so half of warm claims beat it._")
    lines.append("")
    # Dated subline (mirrors scale_proof #3952): the warm-vs-cold pair is a point-in-time
    # measurement carried forward across the daily refresh, so it carries its own measured
    # date, honestly distinct from the page's daily-refreshed timestamp.
    if wc.get("measured_at"):
        lines.append(
            f"_Measured {wc['measured_at'][:10]} — warm-vs-cold speedup "
            "(point-in-time; refreshed on the next TTFE fire)._"
        )
        lines.append("")
    return "\n".join(lines)


# Optional Kata fields that fail the block CLOSED when present-but-invalid (mirrors warm_vs_cold's
# cold_start_mode handling): a typo'd hypervisor / a registry-path image / a free-text resume must
# never publish, so "present in raw but not in clean" ⇒ INERT for these. (Required fields are
# enforced by the separate required-loop below.) Kept in sync with the emitter coercer's fail-closed
# optionals; a drift is caught by the cross-contract test.
_KATA_FAIL_CLOSED_OPTIONALS = ("warm_image", "hypervisor", "resume_status", "kata_version")


def _clean_kata_activation(results):
    """Closed-schema-validate the TOP-LEVEL kata_activation object (#3942).

    Returns the cleaned dict ONLY when every REQUIRED field (runtime_class, microvm_activation_ms,
    warm_ready_ms, cold_ready, guest_kernel, host_kernel) is present and passes its predicate; None
    otherwise (⇒ INERT). The optional enum/shape fields fail the block CLOSED when present-but-invalid
    (a typo'd hypervisor / registry-path image / free-text resume never publishes), mirroring
    warm_vs_cold's cold_start_mode posture.
    """
    ka = results.get("kata_activation")
    if not isinstance(ka, dict):
        return None
    clean = {}
    for key, ok in KATA_ACTIVATION_FIELDS.items():
        if key in ka:
            try:
                if ok(ka[key]):
                    clean[key] = ka[key]
            except (TypeError, ValueError):
                pass
    for req in ("runtime_class", "microvm_activation_ms", "warm_ready_ms",
                "cold_ready", "guest_kernel", "host_kernel"):
        if req not in clean:
            return None
    # Present-but-invalid optional enum/shape ⇒ fail closed (over-claim guard).
    for opt in _KATA_FAIL_CLOSED_OPTIONALS:
        if opt in ka and opt not in clean:
            return None
    return clean


def render_kata_activation(results):
    """Render the Kata+microVM activation block (#3942), or "" when INERT.

    Publishes Kata pod-Ready / microVM-activation latency. This is DELIBERATELY NOT the TTFE the
    Core Metrics matrix keys on (executed first-instruction + returned a result): the matrix TTFE
    cells for Kata stay honestly `pending`, and the caption restates the distinction so a reader
    cannot read these Ready numbers as TTFE or compare them against the gVisor TTFE columns. The
    resume cell reads N/A — upstream-blocked (CRIU resume not wired upstream, #3097), a genuine
    upstream gap rather than an unrun or failed test. INERT (returns "") until the harness emits a
    complete, closed-schema-clean kata_activation object.
    """
    ka = _clean_kata_activation(results)
    if not ka:
        return ""
    rt_label = RUNTIME_LABELS[ka["runtime_class"]]
    lines = ["## Kata + microVM Activation (pod-Ready — NOT TTFE)", ""]
    caption = (
        f"These are **{rt_label} pod-Ready / microVM-activation** latencies — the time to bring "
        "the guest microVM up and the pod Ready. They are **not TTFE** (the Core Metrics matrix's "
        "executed-first-instruction-and-returned-a-result metric), so they are **not comparable "
        "to the matrix TTFE columns**; the Kata TTFE cells there stay `pending` until a TTFE probe "
        "runs under Kata."
    )
    meta = []
    if ka.get("hypervisor"):
        meta.append(f"hypervisor **{ka['hypervisor']}**")
    if ka.get("kata_version"):
        meta.append(f"Kata **{ka['kata_version']}**")
    meta.append(f"guest kernel `{ka['guest_kernel']}`")
    meta.append(f"host kernel `{ka['host_kernel']}`")
    n_note = f", n={ka['n']}" if "n" in ka else ""
    lines.append(caption + f" Measured on {', '.join(meta)}{n_note}.")
    lines.append("")
    header = ["Phase", "Pod-Ready latency"]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")
    warm_img = f" ({ka['warm_image']})" if ka.get("warm_image") else ""
    lines.append(f"| microVM activation | {_fmt_secs(ka['microvm_activation_ms'])} |")
    lines.append(f"| Warm-pool hit{warm_img} | {_fmt_secs(ka['warm_ready_ms'])} |")
    for e in ka["cold_ready"]:
        pull = f" (image pull {_fmt_secs(e['image_pull_ms'])})" if "image_pull_ms" in e else ""
        lines.append(f"| Cold start — {e['image']}{pull} | {_fmt_secs(e['ready_ms'])} |")
    # Resume row: a genuine upstream gap, NOT a failed/unrun test. (a4s1 ask (b).)
    lines.append("| Snapshot resume | N/A — upstream-blocked (CRIU resume not wired, #3097) |")
    lines.append("")
    if ka.get("measured_at"):
        lines.append(
            f"_Measured {ka['measured_at'][:10]} — Kata pod-Ready / microVM-activation "
            "(point-in-time; not TTFE)._"
        )
        lines.append("")
    return "\n".join(lines)


# --- #4021: concurrent-burst sweep render -------------------------------------------------
# The Core Metrics matrix and the step-up table both report a per-SECOND creation RATE (sandboxes
# launched per second, ramped). This block reports the complementary axis alex/a4z1 asked for: a
# single ALL-AT-ONCE burst of N concurrent claims (300/500), warm-pool vs cold-provision. Same TTFE
# honesty spine as the matrix (executed-first-instruction-and-returned-a-result), so the numbers are
# directly comparable to the matrix TTFE columns. INERT (returns "") until the harness emits a
# complete, closed-schema-clean concurrent_burst object.


def _clean_concurrent_burst(results):
    """Closed-schema-validate the TOP-LEVEL concurrent_burst object (#4021). None ⇒ INERT.

    Returns the cleaned dict ONLY when the REQUIRED `legs` list is present and every leg passes
    its predicate (n, mode, ttfe_p50_ms, ttfe_p95_ms required per leg; throughput + exec fractions
    optional). Optional provenance scalars (node_count, machine_type, measured_at) render only when
    valid; a present-but-invalid one is dropped on read, never fabricated.
    """
    cb = results.get("concurrent_burst")
    if not isinstance(cb, dict):
        return None
    clean = {}
    for key, ok in CONCURRENT_BURST_FIELDS.items():
        if key in cb:
            try:
                if ok(cb[key]):
                    clean[key] = cb[key]
            except (TypeError, ValueError):
                pass
    if "legs" not in clean:
        return None
    return clean


_CONCURRENT_BURST_MODE_LABELS = {
    "warm": "Warm pool",
    "cold": "Cold provision",
}


def _cb_thpt_cell(leg, key):
    """Throughput-per-node cell: the value as-is (compact), or em-dash when the leg omits it —
    honest "not measured", never a fabricated 0."""
    if key in leg:
        return _fmt_num(leg[key])
    return "—"


def render_concurrent_burst(results):
    """Render the concurrent-burst sweep block (#4021), or "" when INERT.

    Publishes a single all-at-once burst of N concurrent claims (the complement to the per-second
    rate the matrix/step-up report), warm-pool vs cold-provision, on the SAME TTFE spine as the
    Core Metrics matrix — so the TTFE columns ARE comparable to the matrix. INERT until the harness
    emits a closed-schema-clean concurrent_burst object.
    """
    cb = _clean_concurrent_burst(results)
    if not cb:
        return ""
    lines = ["## Concurrent Burst — TTFE at N simultaneous claims", ""]
    caption = (
        "Each row is a **single all-at-once burst of N concurrent claims** (not a ramped "
        "per-second rate). TTFE is the same metric the Core Metrics matrix reports "
        "(executed-first-instruction-and-returned-a-result), so these columns **are comparable "
        "to the matrix TTFE columns**. *Warm pool* fires against a pre-provisioned pool of N "
        "ready sandboxes; *cold provision* starts from an empty pool (node-autoscaler + image-pull "
        "in the critical path)."
    )
    meta = []
    if cb.get("node_count") is not None:
        meta.append(f"node_count={cb['node_count']}")
    if cb.get("machine_type"):
        meta.append(f"`{cb['machine_type']}`")
    if meta:
        caption += f" Measured on {', '.join(meta)}."
    lines.append(caption)
    lines.append("")
    header = [
        "Concurrency (N)", "Activation Mode", "TTFE p50", "TTFE p95",
        "Throughput @ <5s/node", "Throughput @ <1s/node", "Execution Success",
    ]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")
    for leg in cb["legs"]:
        mode_label = _CONCURRENT_BURST_MODE_LABELS.get(leg["mode"], leg["mode"])
        exec_cell = (
            _exec_cell(leg["exec_success_rate"], leg["n"])
            if "exec_success_rate" in leg else "—"
        )
        row = [
            _fmt_num(leg["n"]),
            mode_label,
            _fmt_secs(leg["ttfe_p50_ms"]),
            _fmt_secs(leg["ttfe_p95_ms"]),
            _cb_thpt_cell(leg, "thpt_under_5s_per_node"),
            _cb_thpt_cell(leg, "thpt_under_1s_per_node"),
            exec_cell,
        ]
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    if cb.get("measured_at"):
        lines.append(f"_Measured {cb['measured_at'][:10]} — concurrent-burst TTFE (point-in-time)._")
        lines.append("")
    return "\n".join(lines)


# --- #4083: warm-pool acquisition-latency render ------------------------------------------
def _clean_warm_pool_acquisition(results):
    """Closed-schema-validate the TOP-LEVEL warm_pool_acquisition object (#4083). None ⇒ INERT.

    Returns the cleaned dict ONLY when the REQUIRED spine (runtime_class, acq_p50_ms, acq_p95_ms,
    n) is present and each field passes its predicate. Optional decomposition/provenance fields
    (acq_p99_ms, offered_rate_per_s, warmpool_size, controller_startup_p95_ms, machine_type,
    node_count, measured_at) render only when valid; a present-but-invalid one is dropped on read,
    never fabricated. runtime_class validates against the PUBLIC RUNTIME_LABELS enum, so an
    out-of-enum runtime fails closed and drops the whole block.
    """
    wpa = results.get("warm_pool_acquisition")
    if not isinstance(wpa, dict):
        return None
    clean = {}
    for key, ok in WARM_POOL_ACQUISITION_FIELDS.items():
        if key in wpa:
            try:
                if ok(wpa[key]):
                    clean[key] = wpa[key]
            except (TypeError, ValueError):
                pass
    if not all(k in clean for k in ("runtime_class", "acq_p50_ms", "acq_p95_ms", "n")):
        return None
    return clean


def render_warm_pool_acquisition(results):
    """Render the warm-pool acquisition-latency block (#4083), or "" when INERT.

    Reports the DECOMPOSED claim→bound sub-phase of TTFE — SandboxClaim requested → bound (a ready
    warm sandbox handed back), the number a warm-pool operator sizes against. It EXCLUDES the
    exec-attach + first-instruction round-trip the concurrent_burst/matrix TTFE legs include, so
    the caption states plainly it is NOT comparable to those TTFE columns. The optional
    controller_startup_p95 renders as an explicit LOWER-BOUND proxy (mirrors the step-up #3975
    discipline). INERT until the harness emits a closed-schema-clean warm_pool_acquisition object.
    """
    wpa = _clean_warm_pool_acquisition(results)
    if not wpa:
        return ""
    label = RUNTIME_LABELS[wpa["runtime_class"]]
    lines = ["## Warm-Pool Acquisition — how fast the pool hands you a sandbox", ""]
    caption = (
        f"Acquisition latency on **{label}**: the time from a `SandboxClaim` being **requested** "
        "to it being **bound** — a warm, ready sandbox handed back to the caller. This is a "
        "**decomposed sub-phase of TTFE**, not the whole thing: it stops at the moment you hold a "
        "ready sandbox and **excludes** the exec-attach + first-instruction round-trip the "
        "Concurrent Burst and Core Metrics tables measure — so these numbers are **not comparable** "
        "to those TTFE columns. It is the earlier, isolated question a warm-pool operator sizes "
        "against: *once my pool is warm, how quickly do I get a sandbox?*"
    )
    ctx = []
    if wpa.get("offered_rate_per_s") is not None:
        ctx.append(f"a sustained **{_fmt_num(wpa['offered_rate_per_s'])} claims/sec** offered load")
    if wpa.get("warmpool_size") is not None:
        ctx.append(f"a warm pool of **{_fmt_num(wpa['warmpool_size'])}**")
    if ctx:
        caption += " Measured under " + " against ".join(ctx) + "."
    shape = []
    if wpa.get("node_count") is not None:
        shape.append(f"node_count={wpa['node_count']}")
    if wpa.get("machine_type"):
        shape.append(f"`{wpa['machine_type']}`")
    if shape:
        caption += f" Cluster shape: {', '.join(shape)}."
    lines.append(caption)
    lines.append("")
    header = ["Sample (n)", "Acquisition p50", "Acquisition p95", "Acquisition p99"]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")
    row = [
        _fmt_num(wpa["n"]),
        _fmt_secs(wpa["acq_p50_ms"]),
        _fmt_secs(wpa["acq_p95_ms"]),
        _fmt_secs(wpa["acq_p99_ms"]) if "acq_p99_ms" in wpa else "—",
    ]
    lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    if wpa.get("controller_startup_p95_ms") is not None:
        lines.append(
            f"_Controller-startup lower bound (p95 **{_fmt_secs(wpa['controller_startup_p95_ms'])}**): "
            "controller-first-observed → Ready, which EXCLUDES the claim-admission → first-reconcile "
            "queueing lag — it UNDER-reports the true acquisition path, so treat it as a floor on the "
            "controller's own contribution, not a second acquisition measurement._")
        lines.append("")
    if wpa.get("measured_at"):
        lines.append(f"_Measured {wpa['measured_at'][:10]} — warm-pool acquisition latency (point-in-time)._")
        lines.append("")
    return "\n".join(lines)


# --- a#3960: Step-up backfill saturation render -------------------------------------------
# The saturation headline alex/a4z1 asked for ("max sandboxes/sec under 5s AND under 1s,
# warm+cold") is computed by the internal classifier (#4030) and emitted as the pre-validated
# saturation_point block (2×2 warm/cold × tight(1s)/loose(5s) bars). Render reads it straight —
# the operator headline is the emitter's number, not a render-time re-derivation. The schema
# characteristic band-rates + verdict (North Star 500ms / collapse 2000ms) and the per-step
# Pareto table render additively below as the methodology/study story.


def _clean_stepup(results):
    """Closed-schema-validate the TOP-LEVEL stepup object (a#3960). None ⇒ INERT.

    Every present field renders ONLY if it is declared in STEPUP_PARETO_FIELDS and passes its
    predicate; anything else is dropped on read. The block is INERT unless the union of
    {pareto_points, controller_startup} is non-empty (the emitter's no-all-empty invariant) —
    a stepup object carrying only sweep params but no measured table never renders.
    """
    su = results.get("stepup")
    if not isinstance(su, dict):
        return None
    clean = {}
    for key, ok in STEPUP_PARETO_FIELDS.items():
        if key in su:
            try:
                if ok(su[key]):
                    clean[key] = su[key]
            except (TypeError, ValueError):
                pass
    if not any(k in clean for k in ("saturation_point", "pareto_points", "controller_startup")):
        return None
    return clean


def _sp_cell(leg, key):
    """One saturation-point table cell: the positive-int rate as "N/s", or em-dash when the bar
    was unmet (value None or absent) — honest "nothing met this bar", never a fabricated 0."""
    rv = leg.get(key)
    if isinstance(rv, int) and not isinstance(rv, bool):
        return f"{_fmt_num(rv)}/s"
    return "—"


_STEPUP_VERDICT_LABELS = {
    "flat-through-sweep": "✅ flat through the whole sweep (no measured step breached the North Star)",
    "degrading": "⚠️ degrading (at least one step breached the North Star; none collapsed)",
    "saturated": "🛑 saturated (at least one step crossed the collapse band)",
    "no-measured-steps": "pending (no step produced a measured TTFE — infra/scrape gap, honest)",
}


def render_stepup(results):
    """Render the step-up saturation block (a#3960), or "" when INERT.

    Headline = the operator Saturation Point table (#4030): max sustained creation rate with
    TTFE p95 under the 1s (tight) and 5s (loose) bars, split by leg (warm-pool hit vs cold-
    provision overflow), read straight off the emitter's pre-validated saturation_point block.
    An unmet bar renders em-dash, never a fabricated 0. The schema verdict + characteristic
    band-rates (North Star 500ms / collapse 2000ms) and the per-step Pareto table render
    additively below as the methodology/study story. The controller_startup proxy renders as an
    explicit LOWER BOUND (it excludes claim→first-reconcile queueing, so it under-reports true
    TTFE). INERT until a closed-schema-clean stepup object with a non-empty table is emitted.
    """
    su = _clean_stepup(results)
    if not su:
        return ""
    lines = []

    sp = su.get("saturation_point")
    if sp:
        tight = _fmt_secs(sp["tight_ms"])
        loose = _fmt_secs(sp["loose_ms"])
        lines.append("## Saturation Point — max sustained creation rate")
        lines.append("")
        lines.append(
            "Max sustained creation rate (offered sandboxes/sec) that held TTFE p95 under each "
            "operator bar, split by leg — warm-pool hit vs cold-provision (node overflow). An "
            "em-dash means no swept rate met that bar; we never round a miss up to a 0.")
        lines.append("")
        header = ["Leg", f"Max rate @ TTFE p95 < {tight}", f"@ p95 < {loose}"]
        lines.append("| " + " | ".join(header) + " |")
        lines.append("|" + "|".join(["---"] * len(header)) + "|")
        for leg_key, leg_label in (("warm", "Warm-pool hit"), ("cold", "Cold-provision (node overflow)")):
            leg = sp.get(leg_key)
            if not isinstance(leg, dict):
                continue
            lines.append(f"| {leg_label} | {_sp_cell(leg, 'max_rate_under_tight')} | {_sp_cell(leg, 'max_rate_under_loose')} |")
        lines.append("")
    else:
        # No saturation_point — e.g. the #3975 proxy-only sweep (no true-TTFE steps, so the
        # classifier emits no headline). The study/proxy detail below still renders.
        lines.append("## Saturation — step-up throughput study")
        lines.append("")

    pts = su.get("pareto_points")
    # Schema verdict + characteristic band-rates (stricter 500ms/2000ms framing) — additive.
    if "verdict" in su:
        lines.append(f"Curve verdict (North Star p95<500ms / collapse 2000ms bands): {_STEPUP_VERDICT_LABELS[su['verdict']]}.")
        lines.append("")
    band_bits = []
    if "max_flat_rate" in su:
        band_bits.append(f"highest rate under the 500ms North Star: **{_fmt_num(su['max_flat_rate'])}/s**")
    if "north_star_breach_rate" in su:
        band_bits.append(f"first rate to breach 500ms: {_fmt_num(su['north_star_breach_rate'])}/s")
    if "saturation_rate" in su:
        band_bits.append(f"first rate to cross 2000ms: {_fmt_num(su['saturation_rate'])}/s")
    if band_bits:
        lines.append("Characteristic rates — " + "; ".join(band_bits) + ".")
        lines.append("")

    # True-TTFE Pareto table.
    if pts:
        header = ["Offered rate (/s)", "TTFE p50", "TTFE p95", "TTFE p99", "Ready /s"]
        lines.append("| " + " | ".join(header) + " |")
        lines.append("|" + "|".join(["---"] * len(header)) + "|")
        for p in sorted(pts, key=lambda q: q["offered_rate_per_s"]):
            cells = [
                _fmt_num(p["offered_rate_per_s"]),
                _fmt_secs(p["ttfe_p50_ms"]) if "ttfe_p50_ms" in p else "—",
                _fmt_secs(p["ttfe_p95_ms"]),
                _fmt_secs(p["ttfe_p99_ms"]) if "ttfe_p99_ms" in p else "—",
                _fmt_num(p["ready_per_s"]) if "ready_per_s" in p else "—",
            ]
            lines.append("| " + " | ".join(cells) + " |")
        lines.append("")

    # Controller-startup LOWER-BOUND proxy (#3975) — separate table, explicit caveat keyed
    # off lower_bound (load-bearing: the schema requires lower_bound=true, so this caveat can
    # never be dropped while the proxy renders).
    cs = su.get("controller_startup")
    if cs:
        lines.append(
            "_Controller-startup lower bound: controller-first-observed → Ready, which "
            "EXCLUDES the claim-admission → first-reconcile queueing lag — it UNDER-reports true "
            "TTFE, so treat it as a floor, not a TTFE measurement._")
        lines.append("")
        if "verdict" in cs:
            lines.append(f"Proxy curve verdict: {_STEPUP_VERDICT_LABELS[cs['verdict']]}.")
            lines.append("")
        header = ["Offered rate (/s)", "Ctrl-startup p50", "Ctrl-startup p95", "Ctrl-startup p99", "Ready /s"]
        lines.append("| " + " | ".join(header) + " |")
        lines.append("|" + "|".join(["---"] * len(header)) + "|")
        for p in sorted(cs["pareto_points"], key=lambda q: q["offered_rate_per_s"]):
            cells = [
                _fmt_num(p["offered_rate_per_s"]),
                _fmt_secs(p["controller_startup_p50_ms"]) if "controller_startup_p50_ms" in p else "—",
                _fmt_secs(p["controller_startup_p95_ms"]),
                _fmt_secs(p["controller_startup_p99_ms"]) if "controller_startup_p99_ms" in p else "—",
                _fmt_num(p["controller_ready_per_s"]) if "controller_ready_per_s" in p else "—",
            ]
            lines.append("| " + " | ".join(cells) + " |")
        lines.append("")

    # Sweep-parameter subline (Little's-law inputs + date) — public-safe scalars.
    params = []
    if "node_count" in su:
        params.append(f"{_fmt_num(su['node_count'])} nodes")
    if "machine_type" in su:
        params.append(su["machine_type"])
    if "sld_s" in su:
        params.append(f"SLD {_fmt_num(su['sld_s'])}s")
    if "wpr" in su:
        params.append(f"WPR {_fmt_num(su['wpr'])}")
    tail = ""
    if su.get("measured_at"):
        tail = f" — measured {su['measured_at'][:10]} (point-in-time; refreshed on the next sweep)"
    if params or tail:
        lines.append(f"_Sweep: {', '.join(params)}{tail}._")
        lines.append("")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("results")
    args = ap.parse_args(argv)
    with open(args.results) as fh:
        results = json.load(fh)
    sys.stdout.write(render_product(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
