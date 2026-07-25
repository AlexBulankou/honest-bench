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
  - Honest-skip vs loud-fail (two distinct None-cases — do not conflate):
      CASE 1 (benign) — a latest.json whose burst_create cell is not a PASS carrying the COUNT
        metric produces NO row (you cannot chart a COUNT that was not measured). Exit 0, no write.
      CASE 2 (defect) — a COUNT *was* measured but a provenance field cannot anchor it to a
        build (e.g. controller_digest empty because BENCH_CONTROLLER_DIGEST capture flaked).
        Skipping this silently freezes the throughput trend while the fire reports success, so
        it fails LOUD + closed (rc=3, naming the failing field + the measured count) rather than
        emitting the misleading "no measurable COUNT" reason.
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


def _candidate_row(results):
    """Return the pre-validation candidate row, or None if no burst_create COUNT was measured.

    None here is CASE 1 — a legitimate honest-skip: the run carries no PASS burst_create cell
    with the COUNT metric, so there is genuinely nothing to chart. This is distinct from
    CASE 2 (a COUNT *was* measured but a provenance field cannot anchor it to a build), which
    is a validated-None from `_validate_row` below — the caller must tell the two apart so it
    can report an accurate reason instead of a false "no COUNT" skip.
    """
    measured = _burst_count_row(results)
    if measured is None:
        return None
    count, density, n = measured
    prov = results.get("provenance") if isinstance(results, dict) else None
    prov = prov if isinstance(prov, dict) else {}
    return {
        "generated_at": results.get("generated_at"),
        "controller_digest": prov.get("controller_digest"),
        "suite_git_sha": prov.get("suite_git_sha"),
        "run_id": prov.get("run_id"),
        "cluster_substrate": prov.get("cluster_substrate"),
        "sandboxes_ready_under_1s": count,
        "density_per_vcpu": density,
        "n": n,
    }


def _validate_row(candidate):
    """Validate a candidate against HISTORY_FIELDS. Return (row, None) or (None, bad_key).

    `bad_key` names the first field that is missing or fails its predicate — i.e. the reason a
    measured COUNT could not be anchored to a build (CASE 2). Keeping the failing key lets the
    caller emit a loud, accurate diagnosis rather than a silent, misleading honest-skip.
    """
    row = {}
    for key, ok in HISTORY_FIELDS.items():
        if key not in candidate:
            return None, key
        val = candidate[key]
        try:
            if not ok(val):
                return None, key
        except (TypeError, ValueError):
            return None, key
        row[key] = val
    return row, None


def extract_row(results):
    """Build a closed-schema history row from a parsed latest.json, or None to skip.

    Returns a dict containing exactly HISTORY_FIELDS keys, each value already validated; or
    None when the run carries no measurable burst_create COUNT (honest-skip) or a required
    field fails its predicate (cannot anchor the row to a build). Callers that need to tell
    those two None-cases apart use `_candidate_row` + `_validate_row` directly (see main()).
    """
    candidate = _candidate_row(results)
    if candidate is None:
        return None
    row, _ = _validate_row(candidate)
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

    candidate = _candidate_row(results)
    if candidate is None:
        # CASE 1: genuinely no measurable COUNT (burst_create absent or not PASS). A COUNT that
        # was never measured cannot be charted — benign honest-skip, exit 0.
        sys.stderr.write("accrue_history: latest.json has no measurable burst_create COUNT — skip\n")
        return 0

    row, bad_key = _validate_row(candidate)
    if row is None:
        # CASE 2: a COUNT *was* measured but the run cannot be anchored to a build (a provenance
        # field is missing/invalid — e.g. BENCH_CONTROLLER_DIGEST capture flaked to ""). Skipping
        # here silently freezes alex's #1 build-over-build throughput trend while the fire still
        # reports success — a trust-surface silent-degrade. Fail LOUD + closed instead (rc=3): the
        # fire reds, its EXIT-trap teardown still runs, and the accurate reason is on the log —
        # never the old false "no measurable COUNT" message.
        count = candidate.get("sandboxes_ready_under_1s")
        bad_val = candidate.get(bad_key)
        sys.stderr.write(
            f"accrue_history: MEASURED burst_create count={count!r} but CANNOT anchor to a build "
            f"— provenance field {bad_key!r} is missing/invalid ({bad_val!r}); trend NOT advanced. "
            f"Fix provenance capture (BENCH_CONTROLLER_DIGEST / suite_git_sha) in the fire.\n"
        )
        return 3

    rows = upsert(row, history)
    sys.stderr.write(
        f"accrue_history: upserted build {row['controller_digest'][:19]} "
        f"(count={row['sandboxes_ready_under_1s']:g}) — {len(rows)} builds in {history}\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
