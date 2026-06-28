"""Sole-writer accrual for the build-over-build throughput history (#3918).

alex's #1 HB directive is to drive the sandbox-creation THROUGHPUT COUNT *up,
build-over-build*. The per-product `results/latest.json` is a single snapshot — re-firing a
new controller build overwrites it, so the page can show today's COUNT but not the
trajectory. This script captures that trajectory: it reads `<product>/results/latest.json`
and UPSERTS one closed-schema row per distinct controller build into
`<product>/results/history.jsonl`, keyed by `controller_digest`. The render side
(render.render_trend) then shows COUNT + delta-vs-prior-build from that file.

Contract (mirrors the fleet's other accrual stores):
  - SOLE WRITER of history.jsonl. The fire/render path runs it AFTER a fire renders
    latest.json; nothing else writes the file.
  - Idempotent + upsert-by-digest: re-running on the same build refreshes that build's row
    (latest measurement of a build wins) rather than appending a duplicate, so the file is
    exactly one row per distinct build, ordered by generated_at.
  - Honest-skip: a latest.json whose burst_create cell is not a PASS carrying the COUNT
    metric produces NO row (you cannot chart a COUNT that was not measured). Exit 0, no write.
  - Closed-schema on the way in: only schema.HISTORY_FIELDS are extracted; the row is
    validated field-by-field before it is written, so no harness free-text reaches the file.

Usage:
  python3 -m render.accrue_history <product>            # default path <product>/results/*
  python3 -m render.accrue_history sandbox --latest P --history Q   # explicit paths (tests)

Import note: same namespace-package shadowing dodge as generate.py — `render/` binds as a
namespace package under `-m render.accrue_history`, so we put `_HERE` first on sys.path and
import schema flatly. That resolves identically under `-m`, `python3 render/accrue_history.py`,
and `cd render && python3 accrue_history.py`, with no __init__.py.
"""

import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

from schema import HISTORY_FIELDS  # noqa: E402


def _repo_root():
    return os.path.dirname(_HERE)


def _burst_count_row(results):
    """Return (count, density, n) from a PASS burst_create cell, or None if not measurable."""
    if not isinstance(results, dict):
        return None
    for s in results.get("scenarios", []) or []:
        if not isinstance(s, dict) or s.get("name") != "burst_create":
            continue
        if s.get("outcome") != "PASS":
            return None
        m = s.get("sla_metrics")
        if not isinstance(m, dict):
            return None
        count = m.get("sandboxes_ready_under_1s")
        density = m.get("density_per_vcpu")
        n = s.get("n")
        return count, density, n
    return None


def extract_row(results):
    """Build a closed-schema history row from a parsed latest.json, or None to skip.

    Returns a dict containing exactly HISTORY_FIELDS keys, each value already validated; or
    None when the run carries no measurable burst_create COUNT (honest-skip) or a required
    field fails its predicate (cannot anchor the row to a build).
    """
    measured = _burst_count_row(results)
    if measured is None:
        return None
    count, density, n = measured
    prov = results.get("provenance") if isinstance(results, dict) else None
    prov = prov if isinstance(prov, dict) else {}
    candidate = {
        "generated_at": results.get("generated_at"),
        "controller_digest": prov.get("controller_digest"),
        "suite_git_sha": prov.get("suite_git_sha"),
        "run_id": prov.get("run_id"),
        "cluster_substrate": prov.get("cluster_substrate"),
        "sandboxes_ready_under_1s": count,
        "density_per_vcpu": density,
        "n": n,
    }
    row = {}
    for key, ok in HISTORY_FIELDS.items():
        if key not in candidate:
            return None
        val = candidate[key]
        try:
            if not ok(val):
                return None
        except (TypeError, ValueError):
            return None
        row[key] = val
    return row


def load_history(path):
    """Read history.jsonl into a list of validated rows (malformed lines dropped)."""
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            clean = {}
            ok_all = True
            for key, ok in HISTORY_FIELDS.items():
                if key not in obj:
                    ok_all = False
                    break
                try:
                    if not ok(obj[key]):
                        ok_all = False
                        break
                except (TypeError, ValueError):
                    ok_all = False
                    break
                clean[key] = obj[key]
            if ok_all:
                rows.append(clean)
    return rows


def upsert(row, history_path):
    """Upsert `row` into history.jsonl keyed by controller_digest; return the written rows.

    One row per distinct controller_digest (latest measurement of a build wins). Rows are
    written ordered by generated_at so the file reads as a build-over-build timeline.
    """
    rows = [r for r in load_history(history_path) if r["controller_digest"] != row["controller_digest"]]
    rows.append(row)
    rows.sort(key=lambda r: r["generated_at"])
    os.makedirs(os.path.dirname(history_path), exist_ok=True)
    with open(history_path, "w") as fh:
        for r in rows:
            fh.write(json.dumps({k: r[k] for k in HISTORY_FIELDS}, sort_keys=True) + "\n")
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description="accrue build-over-build throughput history")
    ap.add_argument("product", help="product whose results/latest.json to accrue (e.g. sandbox)")
    ap.add_argument("--latest", default=None, help="override path to latest.json")
    ap.add_argument("--history", default=None, help="override path to history.jsonl")
    args = ap.parse_args(argv)

    root = _repo_root()
    latest = args.latest or os.path.join(root, args.product, "results", "latest.json")
    history = args.history or os.path.join(root, args.product, "results", "history.jsonl")

    if not os.path.exists(latest):
        sys.stderr.write(f"accrue_history: no latest.json at {latest} — nothing to accrue\n")
        return 0
    with open(latest) as fh:
        results = json.load(fh)
    row = extract_row(results)
    if row is None:
        sys.stderr.write("accrue_history: latest.json has no measurable burst_create COUNT — skip\n")
        return 0
    rows = upsert(row, history)
    sys.stderr.write(
        f"accrue_history: upserted build {row['controller_digest'][:19]} "
        f"(count={row['sandboxes_ready_under_1s']:g}) — {len(rows)} builds in {history}\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
