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
    AT_SCALE_CONTENTION_FIELDS,
    BADGE_CONSTRUCTIONS,
    BADGE_SCOPES,
    BURST_CORROBORATION_FIELDS,
    CLUSTER_SATURATION_FIELDS,
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
    PROVISIONING_RATE_SWEEP_FIELDS,
    RUNTIME_LABELS,
    SCALE_PROOF_FIELDS,
    SCENARIO_LABELS,
    STEPUP_PARETO_FIELDS,
    TTFE_COMPARABILITY_MIN_N,
    WARM_BIND_FIELDS,
    WARM_POOL_ACQUISITION_FIELDS,
    WARM_VS_COLD_FIELDS,
    _ISO,
)

# #4137: the sentence appended to the drained-regime warm caveat that NAMES the term driving
# warm-hit TTFE growth with claim-count. Keyed by the schema WARM_SCALING_TERMS enum so the
# attribution is data-driven, not hand-entered free-text; the dict .get() below is what
# validates the emitted value against this closed vocabulary (an out-of-enum value renders no
# clause). Every WARM_SCALING_TERMS member MUST have an entry here — enforced by a sync test.
_WARM_SCALING_TERM_CLAUSE = {
    "bind-concurrency": (
        " The term that grows with claim-count here is **bind (provisioning) concurrency**, "
        "not exec: on this fixed drained node-set the per-claim bind time climbs as more "
        "claims contend for provisioning while exec stays flat — so the warm-hit distribution "
        "straddles 1s at higher N because of provisioning concurrency, not the exec channel."
    ),
}


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

# N/A-by-construction cell (distinct from `pending`, which awaits a measurement). Used for
# the resume-from-suspend × Kata+microVM cell: CRIU checkpoint/restore does not transfer to
# the Kata VM model, so that cell can never be measured — na-by-design, not not-yet-measured.
_NA = "N/A"

# hb#132 dual-throughput. Each throughput cell carries TWO numbers: `<node> /node · <cluster>`.
# The per-node half is the engineering rate (comparable across runtimes); the cluster half is a
# MEASURED saturation rate at X nodes — never a per-node × N extrapolation (that fiction breaks
# above the controller reconcile ceiling). The cluster half pends `pending (cluster-fire)` until
# OUR own schema-validated saturation fire carries thpt_*_per_cluster. A landed cluster figure
# below the sizing target renders with ⚠️ (honest under-target signal); the target itself is the
# test-sizing floor and is NEVER printed as a value.
_CLUSTER_FIRE = "cluster-fire"
CLUSTER_THROUGHPUT_TARGET = 300


def _fmt_num(v):
    """Compact numeric (no trailing zeros): 4.0 -> 4, 1.86 -> 1.86."""
    return f"{v:g}"


def _fmt_ratio(r):
    """A retention ratio to 2 dp, no trailing zeros: 0.989474 -> 0.99, 1.06 -> 1.06."""
    return f"{round(r, 2):g}"


def _fmt_secs(ms):
    """Milliseconds -> the doc's seconds format: 600 -> 0.6s, 1560 -> 1.56s."""
    return f"{ms / 1000.0:g}s"


def _fmt_wait(ms):
    """Operating-envelope budgeting figure: a friendly 1-dp APPROXIMATION with a `~` prefix
    (631.7 -> ~0.6s, 2939.65 -> ~2.9s). Deliberately coarser than `_fmt_secs` — the envelope is
    a plan-around-this summary for non-experts, and the exact measured value lives in the detail
    table each row is sourced from. Still render-derived; only the display precision differs."""
    return f"~{round(ms / 1000.0, 1):g}s"


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
        outcome = s.get("outcome")
        metrics = _clean_matrix_metrics(s.get("sla_metrics"))
        # Carry the pending_reason through (closed-enum-guarded, same discipline as the
        # non-matrix _clean_scenario at 106-107) so a pending matrix cell can say WHY it is
        # pending. This is the honesty distinction: a gVisor resume cell whose run DID land
        # but is held by an upstream controller bug is `upstream-blocked` — it must NOT read
        # like a not-yet-run `not-yet-measured` pending. A free-text / unknown reason is
        # dropped (renders bare `pending`), never leaked to the public page.
        reason = s.get("pending_reason")
        if reason is not None and reason not in PENDING_REASONS:
            reason = None
        # A `pending` scenario has no publishable measurement: its sla_metrics are
        # provisional gap-probe artifacts, not results. The upstream-blocked resume probe
        # is the canonical case — it records the probe's timeout CEILING (the wall-clock it
        # waits out a never-clearing Suspended condition), not a real resume TTFE. Suppress
        # those metrics so a pending matrix cell renders `pending (<reason>)` across EVERY
        # metric column instead of leaking a number a reader would rank against a real
        # distribution. A PASS scenario keeps its metrics (absent individual keys still
        # fall through to per-cell `pending`), so a resume row graduates cleanly
        # pending -> real the moment its outcome flips to PASS.
        if outcome == "pending":
            metrics = {}
        out[name] = {
            "outcome": outcome,
            "n": n,
            "metrics": metrics,
            "pending_reason": reason,
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


def render_density_detail(results, kata_results=None):
    """DETAILS.md deep-dive: per-runtime Max-Density (sandboxes per node-allocatable
    sandbox-schedulable vCPU). Relocated off the headline matrix (hb#134 page-friendliness
    pass — a non-infra reader does not need it in the core table) but PRESERVED here so the
    #133/#135 saturation measurement is not lost. Same per-runtime source logic as the
    matrix: the primary results claim their measured runtime; kata_results (the sandbox-kata
    product) may fill the kata-microvm slot. Unmeasured runtimes render `pending`. Returns ""
    (INERT) only for an unknown product — otherwise it always renders the runtime skeleton,
    rows pending individually, mirroring the matrix's honest-skeleton behaviour."""
    product = results.get("product")
    if product not in PRODUCTS:
        return ""
    prov = _clean_provenance(results.get("provenance"))
    measured_runtime = prov.get("runtime") or "gvisor"
    sources = {measured_runtime: _matrix_scenarios(results.get("scenarios"))}
    if (
        isinstance(kata_results, dict)
        and kata_results.get("product") == "sandbox-kata"
        and "kata-microvm" not in sources
    ):
        kp = _clean_provenance(kata_results.get("provenance"))
        if kp.get("runtime") == "kata-microvm":
            sources["kata-microvm"] = _matrix_scenarios(kata_results.get("scenarios"))
    lines = ["## Max Density (sandboxes per vCPU)", ""]
    lines.append(
        "Max Density is sandboxes per node-allocatable sandbox-schedulable vCPU (the "
        "per-node denominator), not per total-cluster vCPU. An unmeasured runtime "
        "renders `pending`."
    )
    lines.append("")
    lines.append("| Runtime | Max Density (sb/vCPU) |")
    lines.append("|---|---|")
    for rt in MATRIX_RUNTIMES:
        rt_scen = sources.get(rt)
        density = _runtime_density(rt_scen) if rt_scen is not None else None
        cell = _fmt_num(density) if density is not None else _PENDING
        lines.append(f"| {RUNTIME_LABELS[rt]} | {cell} |")
    lines.append("")
    return "\n".join(lines) + "\n"


def render_sample_sizes(results, kata_results=None):
    """DETAILS.md deep-dive: the sample size (N) behind every Core Metrics matrix row.

    hb#134 dropped the "Samples (N)" column from the headline matrix (page-friendliness pass),
    but N is the receipt behind every TTFE p50/p95 — without it a reader cannot tell a warm-pool
    p50 over hundreds of samples from a cold p50 over one. This block restores that receipt in
    the appendix: the same runtime x activation-mode rows as the matrix, keyed by the same
    per-scenario n, so the front page stays scannable while the number stays inspectable. Rows
    below the cross-row comparability floor (N < TTFE_COMPARABILITY_MIN_N) are exactly the rows
    marked _LOW_N_MARK on the matrix TTFE cells — the caption ties the two together. Same
    per-runtime source + pending-reason discipline as the matrix: primary results claim their
    measured runtime, kata_results may fill the kata-microvm slot, a pending row renders
    `pending (<reason>)`, and resume x Kata is N/A by construction. Returns "" (INERT) only for
    an unknown product; otherwise it always renders the runtime x mode skeleton, cells pending
    individually, mirroring the matrix's honest-skeleton behaviour."""
    product = results.get("product")
    if product not in PRODUCTS:
        return ""
    prov = _clean_provenance(results.get("provenance"))
    measured_runtime = prov.get("runtime") or "gvisor"
    sources = {measured_runtime: _matrix_scenarios(results.get("scenarios"))}
    if (
        isinstance(kata_results, dict)
        and kata_results.get("product") == "sandbox-kata"
        and "kata-microvm" not in sources
    ):
        kp = _clean_provenance(kata_results.get("provenance"))
        if kp.get("runtime") == "kata-microvm":
            sources["kata-microvm"] = _matrix_scenarios(kata_results.get("scenarios"))
    lines = ["## Sample Sizes (N per Core Metrics row)", ""]
    lines.append(
        "The receipt behind the Core Metrics table: the N each row's TTFE p50/p95 was measured "
        f"over. Rows with N < {TTFE_COMPARABILITY_MIN_N} are exactly the ones marked "
        f"{_LOW_N_MARK} on the matrix TTFE cells — a single-sample p50 is not a distribution, so "
        "do not rank a low-N row against a high-N one. An unmeasured or not-yet-graduated row "
        "renders `pending`; resume-from-suspend on Kata is N/A by construction."
    )
    lines.append("")
    lines.append("| Runtime | Activation Mode | Samples (N) |")
    lines.append("|---|---|---|")
    for rt in MATRIX_RUNTIMES:
        rt_label = RUNTIME_LABELS[rt]
        rt_scen = sources.get(rt)
        measured = rt_scen is not None
        for scen_name, mode_label in ACTIVATION_MODE_ROWS:
            if scen_name == "suspend_resume" and rt == "kata-microvm":
                lines.append(f"| {rt_label} | {mode_label} | {_NA} |")
                continue
            sc = rt_scen.get(scen_name) if measured else None
            sc_pending = bool(sc) and sc.get("outcome") == "pending"
            pending_tok = _PENDING
            if sc_pending:
                reason = sc.get("pending_reason")
                if reason:
                    pending_tok = f"{_PENDING} ({reason})"
            n_val = sc["n"] if (sc and sc["n"] > 0 and not sc_pending) else None
            if n_val is None:
                cell = pending_tok
            elif n_val < TTFE_COMPARABILITY_MIN_N:
                cell = f"{n_val} {_LOW_N_MARK}"
            else:
                cell = str(n_val)
            lines.append(f"| {rt_label} | {mode_label} | {cell} |")
    lines.append("")
    return "\n".join(lines) + "\n"


def _landed_cluster_x(m):
    """A metrics dict's landed, valid thpt_cluster_node_count as int — None if absent/invalid.

    The single validity rule shared by the caption resolver AND the per-cell cluster-half gate,
    so "has an X" means the same thing in both places (numeric, non-bool, > 0)."""
    x = (m or {}).get("thpt_cluster_node_count")
    if isinstance(x, (int, float)) and not isinstance(x, bool) and x > 0:
        return int(x)
    return None


def _resolve_cluster_x(sources):
    """hb#132: the X in the `@X nodes` cluster-throughput caption — the node count the per-cluster
    figures were MEASURED at, resolved PER RUNTIME (first landed thpt_cluster_node_count within
    each runtime's scenarios). Returns {runtime: X}; empty ⇒ no cluster figure has landed yet
    (caption says cluster halves pend). Per-runtime because two runtimes' cluster fires may land
    at DIFFERENT X — a single first-match X would silently caption one runtime's figures with the
    other runtime's node count (the mixed-X ambiguity)."""
    xs = {}
    for rt, rt_scen in sources.items():
        if not rt_scen:
            continue
        for sc in rt_scen.values():
            x = _landed_cluster_x((sc or {}).get("metrics"))
            if x is not None:
                xs[rt] = x
                break
    return xs


def render_matrix(results, kata_results=None):
    """Render the doc's 7-column Core Metrics Table (primary results + optional kata results).

    A single run measures ONE runtime (provenance.runtime, default gvisor); that runtime's rows
    fill from the measured scenarios. The Kata + microVM rows can fill from a SECOND results
    file (`kata_results`, the sandbox-kata product) measured in a separate run on the kata pool
    — the run split exists because `run --product sandbox-kata` writes its own latest.json and
    can never overwrite the gVisor artifact (harness/scenario_map.py). kata_results is used
    ONLY when its product is "sandbox-kata" AND its cleaned provenance.runtime is kata-microvm;
    the primary results win on conflict (a kata-measured primary run ignores kata_results).
    Unmeasured runtime rows render `pending`. Per-metric cells render `pending` until the
    TTFE-instrumented harness emits them, so the page degrades to an honest skeleton rather
    than a blank or a guess.
    """
    product = results.get("product")
    if product not in PRODUCTS:
        raise ValueError(f"unknown product (not in closed schema): {product!r}")

    prov = _clean_provenance(results.get("provenance"))
    measured_runtime = prov.get("runtime") or "gvisor"
    # Per-runtime scenario sources. The primary results claim their measured runtime first;
    # kata_results may ONLY fill the kata-microvm slot if still empty (primary wins).
    sources = {measured_runtime: _matrix_scenarios(results.get("scenarios"))}
    kata_prov = None
    kata_gen = None
    if (
        isinstance(kata_results, dict)
        and kata_results.get("product") == "sandbox-kata"
        and "kata-microvm" not in sources
    ):
        kp = _clean_provenance(kata_results.get("provenance"))
        if kp.get("runtime") == "kata-microvm":
            sources["kata-microvm"] = _matrix_scenarios(kata_results.get("scenarios"))
            kata_prov = kp
            g = kata_results.get("generated_at")
            if isinstance(g, str) and _ISO.match(g):
                kata_gen = g

    header = [
        "Runtime",
        "Activation Mode",
        "Throughput @ <5s TTFE (sb/s — node · cluster)",
        "Throughput @ <1s TTFE (sb/s — node · cluster)",
        "TTFE p50",
        "TTFE p95",
        "Execution Success (Honesty Check)",
    ]
    lines = ["## Agent Sandbox — Core Metrics", ""]
    lines.append(
        "**Read TTFE down a column, not across rows.** Activation-mode rows differ in sample "
        "size by orders of magnitude, and a p50 over hundreds of samples and a p50 over one are "
        "not comparable: cross-row TTFE ranking is only meaningful between rows with similar "
        f"sample counts. Rows measured over fewer than N={TTFE_COMPARABILITY_MIN_N} samples are "
        f"marked {_LOW_N_MARK} on their TTFE cells."
    )
    lines.append("")
    # hb#132: throughput cells are dual (`per-node · per-cluster`). Pin the cluster measurement
    # size (X nodes) in the caption above the table, not per-cell. X is resolved per runtime from
    # the landed thpt_cluster_node_count; absent everywhere, the cluster halves render
    # `pending (cluster-fire)`. When two runtimes' cluster legs landed at the SAME X the caption
    # stays single-figure; at DIFFERENT X it names each runtime's X explicitly so one runtime's
    # figures are never captioned with the other's node count (the mixed-X ambiguity).
    cluster_xs = _resolve_cluster_x(sources)
    distinct_xs = set(cluster_xs.values())
    if len(distinct_xs) == 1:
        cluster_x = next(iter(distinct_xs))
        lines.append(
            "**Throughput is dual — `per-node · per-cluster`.** The per-node figure is the "
            "engineering rate (comparable across runtimes); the per-cluster figure is a MEASURED "
            f"cluster saturation rate at {cluster_x} nodes — never a per-node × N extrapolation "
            "(that fiction breaks above the controller reconcile ceiling). A per-cluster figure "
            "below the cluster sizing target renders with ⚠️."
        )
    elif len(distinct_xs) > 1:
        per_rt = "; ".join(
            f"{RUNTIME_LABELS[rt]} at {cluster_xs[rt]} nodes"
            for rt in MATRIX_RUNTIMES
            if rt in cluster_xs
        )
        lines.append(
            "**Throughput is dual — `per-node · per-cluster`.** The per-node figure is the "
            "engineering rate (comparable across runtimes); the per-cluster figure is a MEASURED "
            f"cluster saturation rate, measured per runtime at DIFFERENT node counts — {per_rt} "
            "— never a per-node × N extrapolation (that fiction breaks above the controller "
            "reconcile ceiling). Per-cluster figures are NOT comparable across runtimes here "
            "(different X). A per-cluster figure below the cluster sizing target renders with ⚠️."
        )
    else:
        lines.append(
            "**Throughput is dual — `per-node · per-cluster`.** The per-node figure is the "
            "engineering rate (comparable across runtimes); the per-cluster figure is a MEASURED "
            "cluster saturation rate (never a per-node × N extrapolation). Cluster halves render "
            "`pending (cluster-fire)` until our own schema-validated saturation fire lands them."
        )
    lines.append("")
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")

    for rt in MATRIX_RUNTIMES:
        rt_label = RUNTIME_LABELS[rt]
        rt_scen = sources.get(rt)
        measured = rt_scen is not None
        for scen_name, mode_label in ACTIVATION_MODE_ROWS:
            is_resume = scen_name == "suspend_resume"
            # Resume-from-suspend × Kata+microVM is N/A by construction: CRIU
            # checkpoint/restore does not transfer to the Kata VM isolation model
            # (harness/scenarios/suspend_resume.py), so this cell can NEVER be
            # measured. Render it na-by-design — NOT `pending`, which would imply a
            # future measurement that is structurally impossible. Holds regardless of
            # which runtime this run measured (a kata-measured run still N/As it).
            if is_resume and rt == "kata-microvm":
                lines.append("| " + " | ".join([rt_label, mode_label] + [_NA] * 5) + " |")
                continue
            sc = rt_scen.get(scen_name) if measured else None
            sc_pending = bool(sc) and sc.get("outcome") == "pending"
            m = sc["metrics"] if sc else {}

            # A pending cell distinguishes WHY it is pending. The canonical case is the
            # gVisor resume row: its run DID land, but an upstream controller bug (the
            # Suspended condition never clears) blocks graduation — that is `upstream-blocked`,
            # NOT a not-yet-run cell. Render `pending (<reason>)` so a reader cannot mistake a
            # known-upstream-gap for an unmeasured one. A pending scenario with no carried
            # reason falls back to bare `pending` (a genuinely not-yet-run cell). Non-measured
            # runtime rows (sc is None) also fall back to bare `pending` — correct, since they
            # simply were not measured in this run.
            pending_tok = _PENDING
            if sc_pending:
                reason = sc.get("pending_reason")
                if reason:
                    pending_tok = f"{_PENDING} ({reason})"

            def cell(key, fmt):
                return fmt(m[key]) if key in m else pending_tok

            # A pending scenario's N is a probe-attempt count, not a published sample size;
            # render it `pending` too so the whole row reads pending until graduation.
            n_val = sc["n"] if (sc and sc["n"] > 0 and not sc_pending) else None
            n_cell = str(n_val) if n_val else pending_tok

            # Low-N TTFE cells carry a small-sample marker so a reader does not rank them
            # against a high-N row (a single-sample p50 is not a distribution). Mark only a
            # rendered measurement (not `pending`) whose N is known and below the floor.
            low_n_ttfe = n_val is not None and n_val < TTFE_COMPARABILITY_MIN_N

            def ttfe_cell(key):
                v = cell(key, _fmt_secs)
                return f"{v} {_LOW_N_MARK}" if (v != _PENDING and low_n_ttfe) else v

            # hb#132 dual cell: `<node> /node · <cluster>`. The per-node half preserves the prior
            # single-figure behavior (absent ⇒ the whole cell is pending, incl. `pending
            # (<reason>)` for a pending scenario since m is empty). The cluster half pends
            # `pending (cluster-fire)` until the schema-validated fire carries the per-cluster
            # field; a landed cluster figure below the sizing target carries ⚠️. The cluster half
            # additionally requires thpt_cluster_node_count in the SAME metrics dict — a
            # per_cluster figure with no X has no measurement size to disclose, so it pends
            # rather than rendering a real rate under a caption that can't pin its X
            # (defense-in-depth: the emit side already couples the triple all-or-nothing).
            def thpt_dual_cell(node_key, cluster_key):
                if node_key not in m:
                    return pending_tok
                node_half = f"{_fmt_num(m[node_key])} /node"
                if cluster_key in m and _landed_cluster_x(m) is not None:
                    cluster_half = f"{_fmt_num(m[cluster_key])} /cluster"
                    if m[cluster_key] < CLUSTER_THROUGHPUT_TARGET:
                        cluster_half += " ⚠️"
                else:
                    cluster_half = f"{_PENDING} ({_CLUSTER_FIRE})"
                return f"{node_half} · {cluster_half}"

            thpt5 = thpt_dual_cell("thpt_under_5s_per_node", "thpt_under_5s_per_cluster")
            thpt1 = thpt_dual_cell("thpt_under_1s_per_node", "thpt_under_1s_per_cluster")
            p50 = ttfe_cell("ttfe_p50_ms")
            p95 = ttfe_cell("ttfe_p95_ms")
            if "exec_success_rate" in m:
                exec_cell = _exec_cell(m["exec_success_rate"], n_val, m.get("exec_success_n"))
            else:
                exec_cell = pending_tok

            lines.append(
                "| "
                + " | ".join(
                    [rt_label, mode_label, thpt5, thpt1, p50, p95, exec_cell]
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
        "_Throughput cells are dual — `per-node · per-cluster`. The per-node figure is the "
        "engineering rate; the per-cluster figure is a MEASURED cluster saturation rate, never a "
        "per-node × N extrapolation. The cluster half renders `pending (cluster-fire)` until our "
        "own schema-validated saturation fire lands it; a landed figure below the cluster sizing "
        "target carries ⚠️._"
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
    if "kata-microvm" not in sources:
        lines.append("_Kata + microVM rows are not-yet-measured (requires-kata-microvm)._")
    elif kata_prov is not None:
        # The kata rows fill from a SEPARATE run (the sandbox-kata product) on the kata
        # node pool — a different cluster substrate + machine shape than the build banner
        # below, so disclose that run's own provenance rather than letting the gVisor
        # banner silently cover both. Same closed-schema fields as the banner; no
        # free-text can ride this line.
        kata_banner = [
            f"{k}={kata_prov[k]}"
            for k in ("cluster_substrate", "machine_type", "node_count")
            if k in kata_prov
        ]
        if kata_gen:
            kata_banner.append(f"generated-at={kata_gen}")
        lines.append(
            "_Kata + microVM rows are measured in a separate run on the kata node pool"
            + (": " + " · ".join(kata_banner) if kata_banner else "")
            + "._"
        )
    lines.append(
        "_Resume-from-suspend × Kata + microVM renders `N/A` by construction — CRIU "
        "checkpoint/restore does not transfer to the Kata VM isolation model, so that cell "
        "can never be measured (distinct from `pending`, which awaits a run)._"
    )
    lines.append(
        "_A bare `pending` cell awaits its TTFE-instrumented run. A `pending (upstream-blocked)` "
        "cell is different: that run DID land, but an upstream controller gap (the resume path's "
        "Suspended condition never clears) holds it — the cell graduates to a real number the "
        "moment the upstream fix lands, not merely when a run is scheduled._"
    )
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


# --- hb#134: operating-envelope headline table -------------------------------------------
# The single "given MY load, what wait do I budget?" table — the reader's only real question.
# It does NOT re-measure anything: it reconciles the warm/wait numbers already measured across
# four INDEPENDENT blocks (the warmpool_cold_start scenario, at_scale_contention, the warm
# concurrent_burst leg, and warm_pool_acquisition) into ONE reader-facing envelope. Every number
# is READ live from the schema via the SAME closed-schema cleaners the source blocks use — no
# hardcoded illustrative figures — and each row INHERITS its source block's pending semantics: an
# INERT/absent source renders THAT row `pending` rather than dropping it (honest skeleton, same
# discipline as the matrix). A `scope` column keeps a full-TTFE row from ever being silently
# compared against the acquisition-only sub-phase. The warm-vs-cold speedup leg is deliberately
# EXCLUDED — it is a ratio-context number, not a wait a reader budgets.
_ENVELOPE_FULL_TTFE = "full start → first result"
_ENVELOPE_ACQ_ONLY = "pool hand-off only (before exec)"


def _envelope_warm_burst_leg(results):
    """The representative "many at once" warm concurrent-burst leg for the envelope.

    Prefer the n=300 warm leg (the agreed representative simultaneous-burst point); if absent,
    fall back to the LARGEST-n warm leg so the row degrades to whatever simultaneous burst was
    actually measured, labelled with its true N. None ⇒ no warm burst leg ⇒ that row pends.
    """
    cb = _clean_concurrent_burst(results)
    if not cb:
        return None
    warm = [
        leg for leg in cb["legs"]
        if leg.get("mode") == "warm" and "ttfe_p50_ms" in leg
        and isinstance(leg.get("n"), int) and not isinstance(leg.get("n"), bool)
    ]
    if not warm:
        return None
    for leg in warm:
        if leg["n"] == 300:
            return leg
    return max(warm, key=lambda leg: leg["n"])


def render_operating_envelope(results):
    """Render the hb#134 operating-envelope headline table (always renders; rows pend individually).

    Answers the one question a model-builder / agentic-dev actually has: *given my load pattern,
    what wait do I budget?* Four load patterns, each with its measured p50 wait and a `scope`
    column so the acquisition-only row is never mis-ranked against the full-TTFE rows. Numbers are
    render-derived from the live schema; a source block that is absent/INERT pends its row.
    """
    scen = _matrix_scenarios(results.get("scenarios"))
    asc = _clean_at_scale_contention(results)
    burst = _envelope_warm_burst_leg(results)
    wpa = _clean_warm_pool_acquisition(results)

    rows = []

    # Row 1 — steady trickle, warm pool keeps up (full TTFE, from the matrix warm scenario).
    sc = scen.get("warmpool_cold_start")
    label1 = "Steady trickle — warm pool keeps up with demand"
    if sc and sc.get("outcome") == "PASS" and "ttfe_p50_ms" in sc["metrics"]:
        rows.append((label1, _fmt_wait(sc["metrics"]["ttfe_p50_ms"]), _ENVELOPE_FULL_TTFE))
    else:
        # hb#134 (a4s1 nit): a row-1 pend inherits the matrix scenario's pending_reason so a
        # known upstream/cluster gap reads `pending (<reason>)` here exactly as it does in the
        # matrix, not a bare `pending` that looks not-yet-run. The reason decorates only a
        # genuinely pending scenario (mirrors the matrix pending_tok logic at ~653); a
        # missing-ttfe PASS or an absent scenario falls back to bare `pending`.
        pending_tok = _PENDING
        if sc is not None and sc.get("outcome") == "pending":
            reason = sc.get("pending_reason")
            if reason:
                pending_tok = f"{_PENDING} ({reason})"
        rows.append((label1, pending_tok, _ENVELOPE_FULL_TTFE))

    # Row 2 — bursty, pool oversubscribed (full TTFE, from the contention retraction point).
    if asc:
        pool, claims = asc["pool_size"], asc["claim_count"]
        ratio = f"{_fmt_ratio(claims / pool)}:1" if pool else "?:1"
        rows.append((
            f"Bursty — pool oversubscribed {ratio} ({_fmt_num(claims)} claims / {_fmt_num(pool)} ready)",
            _fmt_wait(asc["ttfe_p50_ms"]), _ENVELOPE_FULL_TTFE,
        ))
    else:
        rows.append((
            "Bursty — pool oversubscribed (more claims than ready pool)",
            _PENDING, _ENVELOPE_FULL_TTFE,
        ))

    # Row 3 — many simultaneous @1:1 (full TTFE, from the warm concurrent-burst leg).
    if burst:
        rows.append((
            f"{_fmt_num(burst['n'])} sandboxes requested at once (1:1 pool)",
            _fmt_wait(burst["ttfe_p50_ms"]), _ENVELOPE_FULL_TTFE,
        ))
    else:
        rows.append((
            "Hundreds of sandboxes requested at once (1:1 pool)",
            _PENDING, _ENVELOPE_FULL_TTFE,
        ))

    # Row 4 — sustained high-rate churn (acquisition-ONLY sub-phase — NOT comparable above).
    if wpa:
        rate = wpa.get("offered_rate_per_s")
        label4 = (
            f"Sustained {_fmt_num(rate)}/sec churn" if rate is not None
            else "Sustained high-rate churn"
        )
        rows.append((label4, _fmt_wait(wpa["acq_p50_ms"]), _ENVELOPE_ACQ_ONLY))
    else:
        rows.append(("Sustained high-rate churn", _PENDING, _ENVELOPE_ACQ_ONLY))

    lines = ["## Operating Envelope — what wait should I budget?", ""]
    lines.append(
        "Find the row closest to **your** load; the p50 is the wait to plan around. The **Scope** "
        "column is load-bearing: the first three rows are the **full** start→first-result wait "
        "(TTFE), directly comparable to one another; the last row is only the **pool hand-off** "
        "sub-phase (it stops the moment you hold a ready sandbox, before your code runs), so do "
        "**not** rank its number against the full-TTFE rows above it. Every number is measured, "
        "not modelled — an unmeasured row reads `pending`, never a guess."
    )
    lines.append("")
    header = ["Your load pattern", "Wait to budget (p50)", "Scope"]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")
    for label, wait, scope in rows:
        lines.append(f"| {label} | {wait} | {scope} |")
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


def _clean_warm_bind_decomposition(scenarios):
    """Find warmpool_cold_start and closed-schema-clean its TTFE decomposition (inch #1).

    Returns {bind_p50, bind_p95, exec_p50, exec_p95, ttfe_p50, ttfe_p95} ONLY when ALL of the six
    percentile keys are present — that all-six-required gate is what keeps the block INERT until a
    decomposition-instrumented fire lands (today's pre-decomposition data has the ttfe pair but
    not the bind/exec pairs, so this renders nothing). Returns None otherwise. Every value is a
    GENUINELY-MEASURED percentile: exec_p50_ms/exec_p95_ms come from the producer's per-claim
    (ttfe_ms - bind_ms) distribution, NOT a render-side p50(ttfe)-p50(bind) subtraction. Any
    sla_metrics key not in WARM_BIND_FIELDS, or failing its predicate, is dropped (closed schema).
    """
    if not isinstance(scenarios, list):
        return None
    for s in scenarios:
        if not isinstance(s, dict) or s.get("name") != "warmpool_cold_start":
            continue
        metrics = s.get("sla_metrics")
        if not isinstance(metrics, dict):
            return None
        clean = {}
        for key, ok in WARM_BIND_FIELDS.items():
            if key in metrics:
                try:
                    if ok(metrics[key]):
                        clean[key] = metrics[key]
                except (TypeError, ValueError):
                    pass
        needed = (
            "bind_p50_ms", "bind_p95_ms",
            "exec_p50_ms", "exec_p95_ms",
            "ttfe_p50_ms", "ttfe_p95_ms",
        )
        if any(k not in clean for k in needed):
            return None
        return {
            "bind_p50": clean["bind_p50_ms"],
            "bind_p95": clean["bind_p95_ms"],
            "exec_p50": clean["exec_p50_ms"],
            "exec_p95": clean["exec_p95_ms"],
            "ttfe_p50": clean["ttfe_p50_ms"],
            "ttfe_p95": clean["ttfe_p95_ms"],
        }
    return None


def render_warm_bind_decomposition(results):
    """Render the warm-hit TTFE bind-vs-exec decomposition (inch #1), or "" when INERT.

    The warm-pool-hit TTFE (create->first-instruction-result) splits into BIND (create->bound,
    i.e. provisioning) + EXEC (websocket setup + the first-instruction round-trip). When the
    warm-hit p50/p95 sits above the <1s North Star, this block shows WHERE the time lives: a bind
    p50 near the TTFE p50 means provisioning dominates (a real controller/clone target); a small
    bind p50 with a large exec p50 means the exec channel (websocket setup) dominates (a
    harness/product artifact, not a controller regression).

    HONESTY: bind, exec, and TTFE are each an INDEPENDENTLY-MEASURED percentile of its own
    per-claim distribution — exec comes from the producer's per-claim (ttfe_ms - bind_ms) samples,
    NOT a render-side p50(ttfe)-p50(bind) subtraction (percentiles do not subtract linearly). The
    three rows therefore need NOT sum. Rendered ONLY when all of bind, exec, AND TTFE percentiles
    are present (see _clean_warm_bind_decomposition), so the public page is byte-unchanged until a
    fire emits the keys. Diagnostic-only — adds a block, changes no existing cell.
    """
    dec = _clean_warm_bind_decomposition(results.get("scenarios"))
    if not dec:
        return ""
    lines = ["## Warm-Hit TTFE — Bind vs Exec Decomposition", ""]
    lines.append(
        "Warm-hit TTFE (create → first-instruction result) splits into **bind** (create → bound, "
        "i.e. provisioning the pool member) and **exec** (websocket setup + the first-instruction "
        "round-trip). This block shows *where* a warm-hit above the <1s target lives — a large "
        "bind points at provisioning (a controller/clone target); a large exec points at the "
        "exec channel (a harness/product artifact, not a controller regression)."
    )
    lines.append("")
    header = ["Stage", "p50", "p95"]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")
    lines.append(
        f"| Bind (create → bound, provisioning) | {_fmt_secs(dec['bind_p50'])} | "
        f"{_fmt_secs(dec['bind_p95'])} |"
    )
    lines.append(
        f"| Exec (websocket + first-instruction) | {_fmt_secs(dec['exec_p50'])} | "
        f"{_fmt_secs(dec['exec_p95'])} |"
    )
    lines.append(
        f"| **TTFE (total)** | **{_fmt_secs(dec['ttfe_p50'])}** | **{_fmt_secs(dec['ttfe_p95'])}** |"
    )
    lines.append("")
    lines.append(
        "_Each row is an independently-measured percentile of its own per-claim distribution "
        "(exec is measured per-claim as TTFE − bind, then percentiled — not p50(TTFE) − p50(bind)). "
        "Percentiles do not sum, so bind and exec need not add exactly to the total TTFE._"
    )
    # Drained-regime caveat (#103/#111), data-keyed on provenance.regime so it cannot rot:
    # once an under-load fire clears the bar and emits regime="under-load" (or omits it),
    # this caveat stops rendering by construction. Kept off any measured cell — it qualifies
    # the claim, it does not alter a number.
    regime = None
    prov = results.get("provenance")
    if isinstance(prov, dict):
        regime = prov.get("regime")
    if regime == "drained":
        caveat = (
            "> ⚠️ **Regime caveat:** this warm tier was measured on a **drained, "
            "low-contention cluster** (single fire, small claim count). A green warm tier "
            "here is honest for THIS fire but is **not yet a sustained North-Star claim** — "
            "it wants corroboration under representative load before sub-1s warm is treated "
            "as durable."
        )
        # #4137: name the scaling term ON the caveat, only when the drained fire emits a valid
        # warm_scaling_term. The dict .get() validates against the closed WARM_SCALING_TERMS
        # vocabulary; an absent or out-of-enum value renders the caveat unchanged.
        scaling_term = prov.get("warm_scaling_term") if isinstance(prov, dict) else None
        caveat += _WARM_SCALING_TERM_CLAUSE.get(scaling_term, "")
        lines.append("")
        lines.append(caveat)
    lines.append("")
    return "\n".join(lines)


def _clean_cold_bind_decomposition(scenarios):
    """Find native_digest_cold and closed-schema-clean its TTFE decomposition (inch #2).

    The cold twin of _clean_warm_bind_decomposition: same six percentile keys, same
    all-six-required INERT gate (today's pre-decomposition cold data has the ttfe pair
    but not the bind/exec pairs, so this renders nothing until a decomposition-
    instrumented cold fire lands). Scoped to the native_digest_cold scenario. Reuses
    WARM_BIND_FIELDS (identical field set + predicates). Every value is a
    GENUINELY-MEASURED percentile: for the n=1 cold cell, bind is the measured
    create->Ready time and exec is the measured residual (ttfe_ms - bind_ms) against
    the SAME shared t0, NOT a render-side subtraction of percentiles. Any sla_metrics
    key not in WARM_BIND_FIELDS, or failing its predicate, is dropped (closed schema).
    """
    if not isinstance(scenarios, list):
        return None
    for s in scenarios:
        if not isinstance(s, dict) or s.get("name") != "native_digest_cold":
            continue
        metrics = s.get("sla_metrics")
        if not isinstance(metrics, dict):
            return None
        clean = {}
        for key, ok in WARM_BIND_FIELDS.items():
            if key in metrics:
                try:
                    if ok(metrics[key]):
                        clean[key] = metrics[key]
                except (TypeError, ValueError):
                    pass
        needed = (
            "bind_p50_ms", "bind_p95_ms",
            "exec_p50_ms", "exec_p95_ms",
            "ttfe_p50_ms", "ttfe_p95_ms",
        )
        if any(k not in clean for k in needed):
            return None
        return {
            "bind_p50": clean["bind_p50_ms"],
            "bind_p95": clean["bind_p95_ms"],
            "exec_p50": clean["exec_p50_ms"],
            "exec_p95": clean["exec_p95_ms"],
            "ttfe_p50": clean["ttfe_p50_ms"],
            "ttfe_p95": clean["ttfe_p95_ms"],
        }
    return None


def render_cold_bind_decomposition(results):
    """Render the cold-start TTFE provision-vs-exec decomposition (inch #2), or "" when INERT.

    The cold twin of render_warm_bind_decomposition. Cold TTFE (create->first-instruction-
    result) splits into PROVISION (create->Ready: controller reconcile + pod schedule + image
    pull + container start) + EXEC (websocket setup + the first-instruction round-trip on the
    already-Ready sandbox). Unlike the warm case — where a large bind is a surprise worth
    flagging — for cold the provision is EXPECTED to dominate (a cold pull is genuinely slow),
    so this block's diagnostic value is inverted: a *large exec* is the surprise, pointing at
    an exec-channel artifact (websocket setup) rather than the cold provision itself.

    HONESTY: provision, exec, and TTFE are each an INDEPENDENTLY-MEASURED value — for the n=1
    cold cell, provision is the measured create->Ready time, exec is the measured residual
    (ttfe_ms - bind_ms) against the SAME shared t0, and TTFE is the measured total; they are
    NOT a render-side subtraction of percentiles. Rendered ONLY when all of provision, exec,
    AND TTFE keys are present (see _clean_cold_bind_decomposition), so the public page is
    byte-unchanged until a decomposition-instrumented cold fire lands. Diagnostic-only — adds
    a block, changes no existing cell.
    """
    dec = _clean_cold_bind_decomposition(results.get("scenarios"))
    if not dec:
        return ""
    lines = ["## Cold-Start TTFE — Provision vs Exec Decomposition", ""]
    lines.append(
        "Cold-start TTFE (create → first-instruction result) splits into **provision** "
        "(create → Ready: controller reconcile + pod schedule + image pull + container start) "
        "and **exec** (websocket setup + the first-instruction round-trip on the already-Ready "
        "sandbox). For a cold start the provision is *expected* to dominate — a cold image pull "
        "is genuinely slow — so the signal to watch here is a large **exec**, which would point "
        "at the exec channel (a harness/product artifact), not the cold provision itself."
    )
    lines.append("")
    header = ["Stage", "p50", "p95"]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")
    lines.append(
        f"| Provision (create → Ready) | {_fmt_secs(dec['bind_p50'])} | "
        f"{_fmt_secs(dec['bind_p95'])} |"
    )
    lines.append(
        f"| Exec (websocket + first-instruction) | {_fmt_secs(dec['exec_p50'])} | "
        f"{_fmt_secs(dec['exec_p95'])} |"
    )
    lines.append(
        f"| **TTFE (total)** | **{_fmt_secs(dec['ttfe_p50'])}** | **{_fmt_secs(dec['ttfe_p95'])}** |"
    )
    lines.append("")
    lines.append(
        "_Each row is an independently-measured value against the same shared t0 (exec is the "
        "measured residual TTFE − provision, not a subtraction of percentiles). For the "
        "single-sample cold cell the p50 and p95 are the one measured sample._"
    )
    # Drained-regime caveat (#103/#111), data-keyed on provenance.regime so it cannot rot —
    # same posture as the warm block: once an under-load fire emits regime != "drained" this
    # caveat stops rendering by construction. Kept off any measured cell.
    regime = None
    prov = results.get("provenance")
    if isinstance(prov, dict):
        regime = prov.get("regime")
    if regime == "drained":
        lines.append("")
        lines.append(
            "> ⚠️ **Regime caveat:** this cold decomposition was measured on a **drained, "
            "low-contention cluster** (single cold provision, n=1). The split is honest for "
            "THIS fire but wants corroboration under representative load before the "
            "provision/exec ratio is treated as durable."
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


def render_scale_proof(results, heading="## Scale Proof (Linearity Check)"):
    """Render the doc's Scale Proof (Linearity Check) table, or "" when no scale_proof present.

    Proof that per-node throughput + density hold flat as the cluster grows — the linearity the
    doc's second table asserts. Retention >= ~0.9 reads ✅ (flat or a superlinear beat); only a
    sag below ~0.9 reads ⚠️ (controller-is-ceiling). See _flat_verdict for the asymmetric framing.

    hb#134: `heading` is overridable so the combined "Does it hold at cluster scale?" section
    (render_cluster_scale) can demote this to a `###` sub-block; default keeps the standalone `##`.
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
    lines = [heading, ""]
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


def render_warm_vs_cold(results, punchline_only=False):
    """Render the warm-vs-cold speedup block (#3954 sibling), or "" when INERT.

    Composes the warm leg (warm-pool TTFx p50) and the true-cold leg (unique-image cold) into
    ONE honest headline a reader can quote: warm provisioning is N times faster than cold. INERT
    (returns "") until the harness emits a complete, closed-schema-clean warm_vs_cold object —
    the classifier itself fails closed if the two legs ever diverge in semantic or runtime class.

    hb#134 page-split: `punchline_only=True` renders ONLY the one-line headline a non-infra
    reader needs (kept on the headline page, right under the matrix), with a pointer to the
    full leg-by-leg table + coherence caveats in DETAILS.md. The default (full) path renders
    the table and moves to the deep-dive appendix. The ratio is recomputed identically in both
    paths, so page and appendix can never disagree.
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
    if punchline_only:
        pl = ["## Warm-vs-Cold Speedup", ""]
        pl.append(
            f"A warm-pool provision is **{speedup}× faster** than {cold['descriptor']} "
            f"({rt_label}) — both legs measured the same way ({sem_label}). Full leg-by-leg "
            "table and the cross-block caveats are in the deep-dive appendix, "
            "[DETAILS.md](DETAILS.md).")
        pl.append("")
        if wc.get("measured_at"):
            pl.append(
                f"_Measured {wc['measured_at'][:10]} — warm-vs-cold speedup "
                "(point-in-time; refreshed on the next TTFE fire)._")
            pl.append("")
        return "\n".join(pl)
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
    # #103: carry the warm sample size INLINE in the leg label so this row cannot be visually
    # conflated with the Core Metrics matrix "Warm-pool hit (Base image)" row — that is the
    # dedicated warmpool_cold_start scenario (its own N in the matrix's Samples column); THIS
    # is the point-in-time warm-vs-cold pair's warm leg at its own n. Two different scenarios,
    # two different operating points — the inline n (data-keyed off wc, so it cannot rot)
    # plus the cross-block caveat below make that unambiguous to a reader scanning both tables.
    warm_n = f", n={wc['n_warm']}" if "n_warm" in wc else ""
    lines.append(f"| Warm-pool hit ({rt_label}{warm_n}) | {_fmt_secs(wc['warm_p50_ms'])} |")
    lines.append(f"| {cold['leg']} | {_fmt_secs(wc['cold_ms'])} |")
    lines.append(f"| Speedup (warm is N× faster) | {speedup}× |")
    lines.append("")
    n_note = f" over n={wc['n_warm']} warm claims" if "n_warm" in wc else ""
    lines.append(
        f"_Speedup = cold ÷ warm, computed from the displayed values{n_note}; the warm leg "
        "is the p50 so half of warm claims beat it._")
    lines.append("")
    # Cross-block coherence caveat (#103 / a4s1): this warm-vs-cold pair is its own
    # point-in-time run at its own operating point — NOT the same measurement as the
    # Core Metrics matrix "Warm-pool hit" row. A reader comparing the two warm p50s
    # across blocks must not read a divergence as a contradiction. Static prose (no numbers,
    # no sample-size comparison) so it can never rot against either block's independent
    # refresh cadence or either block's n.
    lines.append(
        "_This warm-vs-cold pair is a standalone point-in-time run; its warm-pool leg is a "
        "separate measurement from the Core Metrics matrix \"Warm-pool hit\" row (an "
        "independent run at its own operating point, refreshed on its own cadence). Read "
        "each block on its own terms — the two warm p50s are not directly comparable._")
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


def render_concurrent_burst(results, heading="## Concurrent Burst — TTFE at N simultaneous claims"):
    """Render the concurrent-burst sweep block (#4021), or "" when INERT.

    Publishes a single all-at-once burst of N concurrent claims (the complement to the per-second
    rate the matrix/step-up report), warm-pool vs cold-provision, on the SAME TTFE spine as the
    Core Metrics matrix — so the TTFE columns ARE comparable to the matrix. INERT until the harness
    emits a closed-schema-clean concurrent_burst object.

    hb#134: `heading` is overridable so the combined "Does it hold at cluster scale?" section
    (render_cluster_scale) can demote this to a `###` sub-block; default keeps the standalone `##`.
    """
    cb = _clean_concurrent_burst(results)
    if not cb:
        return ""
    lines = [heading, ""]
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


def render_cluster_scale(results):
    """hb#134: the combined "Does it hold at cluster scale?" headline section.

    Merges the two cluster-scale questions a non-infra reader actually has — does per-node
    throughput/density stay flat as nodes grow (linearity, render_scale_proof) and what does a
    single all-at-once burst of N claims cost (concurrency, render_concurrent_burst) — under one
    user-facing question, with the two tables demoted to `###` sub-blocks. Each sub-block stays
    independently closed-schema INERT (an absent one simply doesn't render); the wrapper heading +
    intro appear ONLY when at least one sub-block is present, so the section degrades to nothing
    rather than an empty header. Same page-split discipline as render_warm_vs_cold/at_scale.
    """
    scale = render_scale_proof(
        results, heading="### Linearity — throughput and density hold flat as nodes grow")
    burst = render_concurrent_burst(
        results, heading="### Concurrent burst — TTFE at N simultaneous claims")
    saturation = render_cluster_saturation(
        results, heading="### Saturation — the whole-cluster warm-hand-out ceiling")
    if not scale.strip() and not burst.strip() and not saturation.strip():
        return ""
    lines = ["## Does it hold at cluster scale?", ""]
    lines.append(
        "Three questions a bigger cluster raises: does throughput stay flat as you add nodes "
        "(**linearity**), what does a single all-at-once burst of N claims cost (**concurrency**), "
        "and where does the whole-cluster warm hand-out rate saturate (**ceiling**)? All below, on "
        "the same TTFE spine as the headline matrix.")
    lines.append("")
    if scale.strip():
        lines.append(scale.rstrip())
        lines.append("")
    if burst.strip():
        lines.append(burst.rstrip())
        lines.append("")
    if saturation.strip():
        lines.append(saturation.rstrip())
    return "\n".join(lines).rstrip()


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


# --- at-scale-under-contention RETRACTION render ------------------------------------------
def _clean_at_scale_contention(results):
    """Closed-schema-validate the TOP-LEVEL at_scale_contention object. None ⇒ INERT.

    Returns the cleaned dict ONLY when the REQUIRED spine (runtime_class, pool_size, claim_count,
    ttfe_p50_ms, ttfe_p95_ms) is present and each field passes its predicate. Optional bind/exec
    decomposition + provenance fields render only when valid; a present-but-invalid one is dropped
    on read, never fabricated. runtime_class validates against the PUBLIC RUNTIME_LABELS enum, so
    an out-of-enum runtime fails closed and drops the whole block.
    """
    asc = results.get("at_scale_contention")
    if not isinstance(asc, dict):
        return None
    clean = {}
    for key, ok in AT_SCALE_CONTENTION_FIELDS.items():
        if key in asc:
            try:
                if ok(asc[key]):
                    clean[key] = asc[key]
            except (TypeError, ValueError):
                pass
    if not all(k in clean for k in ("runtime_class", "pool_size", "claim_count", "ttfe_p50_ms", "ttfe_p95_ms")):
        return None
    return clean


def render_at_scale_contention(results, detail=False):
    """Render the at-scale-under-contention RETRACTION block, or "" when INERT.

    The deliberate counter-point to the flattering 1:1 warm bursts: a single measured operating
    point where the pool is OVER-SUBSCRIBED (claim_count > pool_size) and warm activation is NO
    LONGER sub-second. Publishing this ceiling keeps the fast matrix/burst numbers from reading as
    an unconditional guarantee. TTFE is node-count-independent, so it IS comparable to the matrix /
    Concurrent Burst TTFE columns; the per-node throughput axis is DELIBERATELY absent (this point
    ran at node_count=1, non-comparable to the node_count=20 bursts). INERT until the harness emits
    a closed-schema-clean at_scale_contention object.

    hb#134 page-split: the DEFAULT (page) path renders the honest-limits retraction posture — the
    prose + the headline TTFE p50/p95 the reader needs to budget for the worst case — under the
    friendlier "Where it breaks today" heading, with a pointer to the full bind/exec decomposition
    table in DETAILS.md. `detail=True` renders that full table (deep-dive appendix). The retraction
    NEVER leaves the headline page — only the decomposition working moves.
    """
    asc = _clean_at_scale_contention(results)
    if not asc:
        return ""
    label = RUNTIME_LABELS[asc["runtime_class"]]
    pool, claims = asc["pool_size"], asc["claim_count"]
    ratio = f"{_fmt_ratio(claims / pool)}:1" if pool else "—"
    heading = ("## At Scale Under Contention — where sub-second warm activation breaks"
               if detail else "## Where it breaks today (honest limits)")
    lines = [heading, ""]
    # hb#134 (a4s1 nit): the Concurrent Burst table lives on the headline README, so "above" is
    # correct on the page path but dangles in the DETAILS detail-path (nothing is above it there).
    burst_locator = "on the headline page" if detail else "above"
    caption = (
        f"The Concurrent Burst legs {burst_locator} are **1:1** — N ready sandboxes hit with N claims. This "
        "is the deliberate **retraction**: the operating point where the pool is "
        "**over-subscribed** (more concurrent claims than ready pool members), and warm activation "
        f"**stops being sub-second**. Measured on **{label}**: a pool of **{_fmt_num(pool)}** ready "
        f"sandboxes hit with **{_fmt_num(claims)}** simultaneous claims (**{ratio} contention**). "
        "Every claim still binds, but the over-subscription serializes the bind path — so the "
        "\"warm hit is <1s\" claim from the Core Metrics matrix does **not** hold here."
    )
    shape = []
    if asc.get("node_count") is not None:
        shape.append(f"node_count={asc['node_count']}")
    if asc.get("machine_type"):
        shape.append(f"`{asc['machine_type']}`")
    if shape:
        caption += f" Cluster shape: {', '.join(shape)}."
    lines.append(caption)
    lines.append("")
    if not detail:
        # Page path: surface the worst-case TTFE inline (so the retraction is self-contained
        # without the table) + point to the full bind/exec decomposition in the appendix.
        lines.append(
            f"Under this contention, TTFE degrades to **{_fmt_secs(asc['ttfe_p50_ms'])} p50** / "
            f"**{_fmt_secs(asc['ttfe_p95_ms'])} p95** — budget for that, not the sub-second warm "
            "hit, when your claim rate can outrun your pool. Full bind/exec decomposition is in "
            "the deep-dive appendix, [DETAILS.md](DETAILS.md).")
        lines.append("")
        if asc.get("measured_at"):
            lines.append(f"_Measured {asc['measured_at'][:10]} — warm-pool at-scale contention ceiling (point-in-time)._")
            lines.append("")
        return "\n".join(lines)
    header = ["Pool", "Claims", "Contention", "TTFE p50", "TTFE p95"]
    have_bind = "bind_p50_ms" in asc and "bind_p95_ms" in asc
    if have_bind:
        header += ["Bind p50", "Bind p95"]
    have_exec = "exec_success_rate" in asc
    if have_exec:
        header.append("Execution Success")
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")
    row = [
        _fmt_num(pool),
        _fmt_num(claims),
        ratio,
        _fmt_secs(asc["ttfe_p50_ms"]),
        _fmt_secs(asc["ttfe_p95_ms"]),
    ]
    if have_bind:
        row += [_fmt_secs(asc["bind_p50_ms"]), _fmt_secs(asc["bind_p95_ms"])]
    if have_exec:
        row.append(_exec_cell(asc["exec_success_rate"], claims))
    lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    lines.append(
        "_Not directly comparable to the 1:1 Concurrent Burst legs: this point ran at "
        "node_count=1 with an over-subscribed pool — a distinct operating point. Latency is "
        "node-count-independent (so the TTFE columns DO compare to the matrix/burst TTFE), but the "
        "per-node throughput axis is omitted here as non-comparable to the node_count=20 bursts._"
    )
    lines.append("")
    if asc.get("measured_at"):
        lines.append(f"_Measured {asc['measured_at'][:10]} — warm-pool at-scale contention ceiling (point-in-time)._")
        lines.append("")
    return "\n".join(lines)


# --- cluster-scale SATURATION render ------------------------------------------------------
def _clean_cluster_saturation(results):
    """Closed-schema-validate the TOP-LEVEL cluster_saturation object. None ⇒ INERT.

    Returns the cleaned dict ONLY when the REQUIRED spine is present and each field passes its
    predicate: runtime_class, pool_size, claim_count, node_count, ttfe_p50_ms, ttfe_p95_ms, AND the
    measured per-cluster throughput triple (thpt_under_5s_per_cluster, thpt_under_1s_per_cluster,
    thpt_cluster_node_count) — the coupled-triple rule the matrix uses, so a per-cluster figure
    never renders without the node count it was measured at. Optional per-node halves, bind/exec
    decomposition, outcome, and provenance render only when valid; a present-but-invalid one is
    dropped on read, never fabricated. runtime_class validates against the PUBLIC RUNTIME_LABELS
    enum, so an out-of-enum runtime fails closed and drops the whole block.
    """
    cs = results.get("cluster_saturation")
    if not isinstance(cs, dict):
        return None
    clean = {}
    for key, ok in CLUSTER_SATURATION_FIELDS.items():
        if key in cs:
            try:
                if ok(cs[key]):
                    clean[key] = cs[key]
            except (TypeError, ValueError):
                pass
    required = (
        "runtime_class", "pool_size", "claim_count", "node_count",
        "ttfe_p50_ms", "ttfe_p95_ms",
        "thpt_under_5s_per_cluster", "thpt_under_1s_per_cluster", "thpt_cluster_node_count",
    )
    if not all(k in clean for k in required):
        return None
    return clean


def render_cluster_saturation(results, heading="### Saturation — the whole-cluster warm-hand-out ceiling", detail=False):
    """Render the cluster-scale SATURATION block, or "" when INERT.

    The third cluster-scale question, distinct from Linearity and Concurrent Burst above it and from
    the at-scale-contention retraction: a 1:1 ALL-WARM fire (pool == claim, NOT over-subscribed)
    driven to CLUSTER saturation — a large claim burst spread across many nodes where the bind path
    saturates even though every claim has a ready warm pool member. This is the honest ceiling for
    "how fast can the whole cluster hand out warm sandboxes at once": the per-cluster throughput
    (MEASURED at node_count, never a per-node × N extrapolation) collapses far below the per-node
    engineering rate, and the sub-second warm hit the Core Metrics matrix reports does NOT hold at
    this scale. outcome=FAIL is headlined as the honest SLA-not-met ceiling, not softened.

    hb#134 page-split: the DEFAULT (page) path renders under a demoted `###` sub-heading (it is the
    third sub-block of the "Does it hold at cluster scale?" section, render_cluster_scale) — the
    prose + the headline per-cluster throughput + TTFE the reader needs, with a pointer to the full
    per-node/per-cluster + bind/exec decomposition table in DETAILS.md. `detail=True` renders that
    full table under a standalone `##` heading (deep-dive appendix). The ceiling posture NEVER
    leaves the headline page — only the decomposition working moves.
    """
    cs = _clean_cluster_saturation(results)
    if not cs:
        return ""
    label = RUNTIME_LABELS[cs["runtime_class"]]
    pool, claims, nodes = cs["pool_size"], cs["claim_count"], cs["node_count"]
    x = _landed_cluster_x(cs)
    if detail:
        heading = "## Cluster Saturation — the whole-cluster warm-hand-out ceiling"
    lines = [heading, ""]
    caption = (
        "The Concurrent Burst legs above are small 1:1 warm bursts. This is the **saturation** "
        f"ceiling: a **1:1 all-warm** fire — a pool of **{_fmt_num(pool)}** ready sandboxes hit "
        f"with **{_fmt_num(claims)}** simultaneous claims (**not** over-subscribed), spread across "
        f"**{_fmt_num(nodes)}** nodes on **{label}**. Every claim has a ready warm pool member, yet "
        "at this scale the bind path itself saturates — so the whole-cluster warm hand-out rate "
        "collapses far below the per-node engineering rate, and the \"warm hit is <1s\" claim from "
        "the Core Metrics matrix does **not** hold here."
    )
    if cs.get("machine_type"):
        caption += f" Cluster shape: `{cs['machine_type']}`."
    lines.append(caption)
    lines.append("")
    if not detail:
        # Page path: surface the collapsed per-cluster throughput + worst-case TTFE inline (so the
        # ceiling is self-contained without the table) + point to the full decomposition in DETAILS.
        lines.append(
            f"At **{_fmt_num(x)} nodes** the cluster sustains only "
            f"**{_fmt_num(cs['thpt_under_5s_per_cluster'])} claims/sec under 5s** "
            f"(**{_fmt_num(cs['thpt_under_1s_per_cluster'])}/sec under 1s**) across the whole "
            f"cluster, and TTFE degrades to **{_fmt_secs(cs['ttfe_p50_ms'])} p50** / "
            f"**{_fmt_secs(cs['ttfe_p95_ms'])} p95**. This is the honest per-cluster hand-out "
            "ceiling — budget for it when your claim rate can outrun the bind path, not for the "
            "sub-second per-node warm hit. Full per-node/per-cluster and bind/exec decomposition is "
            "in the deep-dive appendix, [DETAILS.md](DETAILS.md).")
        lines.append("")
        if cs.get("outcome") == "FAIL":
            lines.append(
                "_SLA ceiling: **not met** at this operating point — this row is the honest "
                "saturation limit, not a warm-hit guarantee. Every claim still bound and executed; "
                "the FAIL is the throughput collapse against the sizing floor, not a correctness "
                "failure._")
            lines.append("")
        if cs.get("measured_at"):
            lines.append(f"_Measured {cs['measured_at'][:10]} — whole-cluster saturation ceiling (point-in-time)._")
            lines.append("")
        return "\n".join(lines)
    # Detail path: the full per-node + per-cluster throughput triple + bind/exec decomposition.
    header = ["Pool", "Claims", "Nodes", "TTFE p50", "TTFE p95",
              "Throughput @ <5s", "Throughput @ <1s"]
    have_bind = "bind_p50_ms" in cs and "bind_p95_ms" in cs
    if have_bind:
        header += ["Bind p50", "Bind p95"]
    have_exec = "exec_success_rate" in cs
    if have_exec:
        header.append("Execution Success")
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")

    def _triple(node_key, cluster_key):
        cluster_half = f"{_fmt_num(cs[cluster_key])} /cluster"
        if node_key in cs:
            return f"{_fmt_num(cs[node_key])} /node · {cluster_half}"
        return cluster_half

    row = [
        _fmt_num(pool),
        _fmt_num(claims),
        _fmt_num(nodes),
        _fmt_secs(cs["ttfe_p50_ms"]),
        _fmt_secs(cs["ttfe_p95_ms"]),
        _triple("thpt_under_5s_per_node", "thpt_under_5s_per_cluster"),
        _triple("thpt_under_1s_per_node", "thpt_under_1s_per_cluster"),
    ]
    if have_bind:
        row += [_fmt_secs(cs["bind_p50_ms"]), _fmt_secs(cs["bind_p95_ms"])]
    if have_exec:
        row.append(_exec_cell(cs["exec_success_rate"], claims))
    lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    lines.append(
        f"_Per-cluster throughput MEASURED at **{_fmt_num(x)} nodes** — never a per-node × N "
        "extrapolation (that fiction breaks above the controller reconcile ceiling). This is a "
        "**1:1 all-warm** operating point (pool == claim, not over-subscribed), distinct from the "
        "over-subscribed contention ceiling: the collapse here is the bind path saturating at "
        "cluster scale, not pool exhaustion. Latency is node-count-independent (so the TTFE columns "
        "DO compare to the matrix/burst TTFE)._")
    lines.append("")
    if cs.get("outcome") == "FAIL":
        lines.append(
            "_SLA ceiling: **not met** at this operating point — the honest saturation limit. "
            "Execution success confirms every claim still bound and executed; the FAIL is the "
            "throughput collapse against the sizing floor, not a correctness failure._")
        lines.append("")
    if cs.get("measured_at"):
        lines.append(f"_Measured {cs['measured_at'][:10]} — whole-cluster saturation ceiling (point-in-time)._")
        lines.append("")
    return "\n".join(lines)


# --- #4086: Provisioning rate sweep (reconcile-bound warm-pool convergence) ---------------
# A THIRD distinct axis, deliberately NOT folded into stepup's TTFE Pareto or the
# at_scale_contention claim:pool ratio: this measures warm-pool PROVISIONING convergence
# (Ready% within pool_warm_timeout) as a function of the OFFERED reconcile RATE. Folding it
# into either of those would falsely imply a same-regime measurement — an honesty violation.


def _clean_provisioning_rate_sweep(results):
    """Closed-schema-validate the TOP-LEVEL provisioning_rate_sweep object. None ⇒ INERT.

    Requires a non-empty rate_points list (each point validated by _rate_points_ok). Optional
    runtime_class / ceiling_low_per_s / ceiling_high_per_s / measured_at render only when valid;
    a present-but-invalid optional is dropped on read, never fabricated. runtime_class validates
    against the PUBLIC RUNTIME_LABELS enum, so an out-of-enum runtime fails closed.
    """
    prs = results.get("provisioning_rate_sweep")
    if not isinstance(prs, dict):
        return None
    clean = {}
    for key, ok in PROVISIONING_RATE_SWEEP_FIELDS.items():
        if key in prs:
            try:
                if ok(prs[key]):
                    clean[key] = prs[key]
            except (TypeError, ValueError):
                pass
    if "rate_points" not in clean:
        return None
    clean["rate_points"] = sorted(clean["rate_points"], key=lambda p: p["offered_rate_per_s"])
    return clean


def _rate_verdict_cell(point):
    """One provisioning-rate row's outcome cell: ✅ when the pool converged (Ready% hit target
    within the warm timeout), ❌ when it timed out under-provisioned. The measured Ready% is
    always shown so a partial fill reads honestly, never rounded up to a pass/fail bit."""
    pct = point["ready_pct"]
    converged = point.get("converged")
    if converged is None:
        converged = pct >= 100.0
    mark = "✅" if converged else "❌"
    cell = f"{mark} {_fmt_num(round(pct, 1))}%"
    el, to = point.get("elapsed_s"), point.get("timeout_s")
    if converged and el is not None:
        cell += f" (converged ~{_fmt_num(round(el))}s)"
    elif not converged and to is not None:
        cell += f" (timeout {_fmt_num(round(to))}s)"
    return cell


def render_provisioning_rate_sweep(results):
    """Render the provisioning rate-sweep block (#4086), or "" when INERT.

    Warm-pool provisioning convergence vs OFFERED reconcile rate: at each offered rate the harness
    drives a warm-pool target and measures whether the pool reaches Ready within pool_warm_timeout.
    Monotonic degradation past a rate ceiling reads reconcile-bound (the controller reconcile path
    is the ceiling), NOT node/quota-bound. INERT until the harness emits a closed-schema-clean
    provisioning_rate_sweep object.
    """
    prs = _clean_provisioning_rate_sweep(results)
    if not prs:
        return ""
    label = RUNTIME_LABELS.get(prs.get("runtime_class"))
    lines = ["## Provisioning Rate Sweep — where warm-pool fill goes reconcile-bound", ""]
    caption = (
        "The warm-pool numbers elsewhere assume the pool is **already Ready**. This block measures "
        "the step before that: how fast the pool can be **provisioned** as a function of the "
        "**offered reconcile rate** (sandboxes requested per second). At each rate the pool is "
        "driven to a target size and we measure whether it reaches Ready **within the warm "
        "timeout**."
    )
    if label:
        caption += f" Measured on **{label}**."
    lines.append(caption)
    lines.append("")
    header = ["Offered reconcile rate", "Warm-pool target", "Ready within timeout"]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")
    for p in prs["rate_points"]:
        wps = p.get("warmpool_size")
        lines.append(
            "| "
            + " | ".join(
                [
                    f"{_fmt_num(p['offered_rate_per_s'])} sb/s",
                    _fmt_num(wps) if wps is not None else "—",
                    _rate_verdict_cell(p),
                ]
            )
            + " |"
        )
    lines.append("")
    lo, hi = prs.get("ceiling_low_per_s"), prs.get("ceiling_high_per_s")
    if lo is not None and hi is not None:
        lines.append(
            f"**Provisioning converges at ~{_fmt_num(lo)} sb/s; over-subscribed beyond "
            f"~({_fmt_num(lo)}, {_fmt_num(hi)}) sb/s** — monotonic degradation past the ceiling is "
            "**reconcile-bound** (the controller reconcile path is the ceiling), not node- or "
            "quota-bound."
        )
        lines.append("")
    lines.append(
        "_A distinct axis from the Concurrent Burst (claim:pool ratio) and Step-up (creation-rate "
        "TTFE) blocks: this measures provisioning **offered-rate** convergence, a separate regime — "
        "not directly comparable to those latency/throughput points._"
    )
    lines.append("")
    if prs.get("measured_at"):
        lines.append(
            f"_Measured {prs['measured_at'][:10]} — warm-pool provisioning rate sweep "
            "(point-in-time; refreshed on the next rate sweep)._"
        )
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


# --- #4021: Reproducibility Recipe (static, product-agnostic) ------------------------------
# The preamble promises "reproducible from the recipe at the bottom"; this is that section.
# It is STATIC architecture-shape prose — no measured numbers, so it carries zero PII risk and
# needs no results arg. The RUNNABLE version (exact commands, pinned installs, CI workflows)
# lives in recipe/REPRODUCE.md and is cross-linked, not duplicated. The one honesty rule baked
# in: the honest-today latency is referenced BY POINTER to the live Warm-Pool Acquisition /
# Concurrent Burst cells rather than restated as a literal, so it can never go stale or
# contradict the machine-rendered tables above it, and no contested sub-1s@300/s headline can
# be slipped in ahead of the measured cells.
_RECIPE = """\
## Reproduce it

Every number above comes from a *vanilla* GKE architecture you can provision yourself — no
private tuning. The **runnable** version (exact commands, pinned installs, dispatch-only CI)
lives in [`recipe/REPRODUCE.md`](recipe/REPRODUCE.md); the load-bearing cluster shape is:

- **Cluster** — a regional GKE Standard cluster on **Kubernetes ≥ 1.31** with a **gVisor**-enabled
  node pool (`--enable-sandbox=type=gvisor`, which installs the `gvisor` `RuntimeClass` the burst
  pins to) on a **16-vCPU** machine type (e.g. `e2-standard-16`). Set the pool's autoscaling max
  to the node count the headline needs *before* the fire, on a **`/16`** pod CIDR, so the burst
  tops out on the sandbox path — not the autoscaler or IP exhaustion.
- **Warm pool** — size the `SandboxWarmPool` so a ready slot waits for each claim (replicas ≈
  active-concurrency × 0.75, replenished at the claim rate); otherwise a sustained burst drains
  into the cold-overflow path partway through. When a drained-regime fire is on the page, the
  Warm-Pool decomposition (in [DETAILS.md](DETAILS.md)) names the scaling term directly.
- **Zero-cold-start** — run an image pre-pull **`DaemonSet`** (`recipe/prepull-daemonset.yaml`) so
  a node that joins mid-burst adds no image-pull tax to the first sandbox scheduled onto it.

**Honesty:** a row marked `pending` is not-yet-measured — never a provisional number dressed as a
result. The **sub-1s @ 300/s warm headline is not yet published**; the honest published-today
figures are exactly the measured cells above (Core Metrics + **Concurrent Burst**) plus the
**Warm-Pool Acquisition** decomposition in [DETAILS.md](DETAILS.md) — the recipe points at those
cells rather than restate a number that could drift out of sync. TRUE-TTFE (webhook-stamped
first-instruction) stays `pending` until the upstream stamper lands.
"""


def render_recipe():
    """Render the static "Reproduce it" H2 block (#4021; hb#134 page-pass trim + rename).

    Product-agnostic architecture-shape prose, always rendered (the preamble forward-refs it).
    No measured numbers — the honest-today latency is referenced by pointer to the live
    Warm-Pool Acquisition / Concurrent Burst cells above, so this block can never go stale or
    contradict the machine-rendered tables. The runnable recipe (commands, pinned installs, CI)
    is cross-linked to recipe/REPRODUCE.md, not duplicated.
    """
    return _RECIPE.rstrip()


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
