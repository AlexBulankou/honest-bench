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
    GOAL_COLUMNS,
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
        n = s.get("n")
        n = n if isinstance(n, int) and not isinstance(n, bool) and n >= 0 else 0
        rows.append(
            {
                "label": SCENARIO_LABELS[name],
                "outcome": outcome,
                "pending_reason": reason,
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
        return "PASS · " + ", ".join(parts)
    return "PASS"


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
