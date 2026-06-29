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
    BADGE_SCOPES,
    BURST_CORROBORATION_FIELDS,
    DENSITY_SOURCE_SCENARIOS,
    GOAL_COLUMNS,
    HISTORY_FIELDS,
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
        n = s.get("n")
        n = n if isinstance(n, int) and not isinstance(n, bool) and n >= 0 else 0
        rows.append(
            {
                "label": SCENARIO_LABELS[name],
                "outcome": outcome,
                "pending_reason": reason,
                "badge_scope": scope,
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
    # bare PASS forms.
    scope = row.get("badge_scope")
    pass_token = f"PASS ({scope})" if scope else "PASS"
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

            thpt5 = cell("thpt_under_5s_per_node", _fmt_num)
            thpt1 = cell("thpt_under_1s_per_node", _fmt_num)
            p50 = cell("ttfe_p50_ms", _fmt_secs)
            p95 = cell("ttfe_p95_ms", _fmt_secs)
            n_val = sc["n"] if (sc and sc["n"] > 0) else None
            n_cell = str(n_val) if n_val else _PENDING
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
    return clean


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
    lines = ["## Warm-vs-Cold Speedup", ""]
    lines.append(
        f"A warm-pool provision is **{speedup}× faster** than a true-cold start "
        f"({rt_label}). The warm pool keeps a ready slot so a claim skips the fresh-node "
        "image-pull path a cold start pays in full. Both legs are measured the same way "
        f"({sem_label}); the ratio is the portable headline you can reproduce on your own "
        "cluster.")
    lines.append("")
    header = ["Leg", _SEMANTIC_LABELS[wc["semantic"]].split(" ")[0] + " (p50)"]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")
    lines.append(f"| Warm-pool hit ({rt_label}) | {_fmt_secs(wc['warm_p50_ms'])} |")
    lines.append(f"| True-cold (unique-image) | {_fmt_secs(wc['cold_ms'])} |")
    lines.append(f"| Speedup (warm is N× faster) | {speedup}× |")
    lines.append("")
    n_note = f" over n={wc['n_warm']} warm claims" if "n_warm" in wc else ""
    lines.append(
        f"_Speedup = cold ÷ warm, computed from the displayed values{n_note}; the warm leg "
        "is the p50 so half of warm claims beat it._")
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
