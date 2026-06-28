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
    BADGE_SCOPES,
    GOAL_COLUMNS,
    HISTORY_FIELDS,
    METRIC_LABELS,
    NON_PUBLIC,
    OUTCOMES,
    PENDING_REASONS,
    PRODUCTS,
    PROVENANCE_FIELDS,
    SCENARIO_LABELS,
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
