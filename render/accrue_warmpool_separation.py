"""Sole-writer accrual for the warmpool-separation-ratio measurement history (#6890 item 3).

The #6890 decision (2026-08-17) surfaced a first-class honest-display finding: the SAME
controller_digest (sha256:f511a1ab3350...) fired twice produced `warmpool_gate_separation_ratio`
0.27x then 1.06x — a >3x swing on byte-identical build inputs. The per-product
`results/latest.json` is a single snapshot — a re-fire overwrites it, so that variance is
invisible on the public page unless captured cross-fire. This script captures it: it reads
`<product>/results/latest.json` and APPENDS one closed-schema row per fire into
`<product>/results/warmpool-separation-history.jsonl`.

This is a SIBLING of accrue_history.py, not a shared code path with it — the two stores answer
different questions and need different write semantics:
  - accrue_history.py (HISTORY_FIELDS / history.jsonl): "what is the THROUGHPUT COUNT
    trajectory build-over-build?" — upserted ONE ROW PER controller_digest (latest wins), because
    for that question a stale measurement of an old build is noise, not signal.
  - This script (WARMPOOL_SEPARATION_HISTORY_FIELDS / warmpool-separation-history.jsonl): "does
    the SAME build measure consistently?" — APPEND-ONLY, keyed by run_id (never upserted by
    digest), because for THIS question a second measurement of the same build overwriting the
    first is exactly the failure mode: it would erase the variance the store exists to disclose.

Contract (mirrors accrue_history.py's shape where the questions align):
  - SOLE WRITER of warmpool-separation-history.jsonl. The fire/render path runs it AFTER a fire
    renders latest.json; nothing else writes the file.
  - Idempotent-by-run_id, append-only otherwise: re-running accrual against the same latest.json
    (same run_id) is a no-op on a file that already carries that run_id's row — it does NOT
    duplicate the row. A genuinely new fire (new run_id), even one sharing a controller_digest
    with an existing row, always appends rather than overwriting.
  - Honest-skip vs loud-fail (two distinct None-cases — do not conflate):
      CASE 1 (benign) — a latest.json whose warmpool_cold_start cell carries no sla_metrics (the
        scenario itself emits {} when the gate metric was never computed) produces NO row — you
        cannot chart a ratio that was not measured. Exit 0, no write. Note this is NOT an outcome
        check: the scenario surfaces warmpool_gate_separation_ratio on BOTH PASS and FAIL (a
        below-gate ratio is exactly what FAILs the scenario and exactly what this store exists to
        chart), so a FAIL cell with sla_metrics IS measurable and DOES chart.
      CASE 2 (defect) — a ratio *was* measured but a provenance field cannot anchor it to a build
        (e.g. controller_digest empty because BENCH_CONTROLLER_DIGEST capture flaked). Skipping
        this silently freezes the variance trend while the fire reports success, so it fails LOUD
        + closed (rc=3, naming the failing field + the measured ratio) rather than emitting the
        misleading "no measurable ratio" reason.
  - Closed-schema on the way in: only schema.WARMPOOL_SEPARATION_HISTORY_FIELDS are extracted;
    the row is validated field-by-field before it is written, so no harness free-text reaches the
    file.

Usage:
  python3 -m render.accrue_warmpool_separation <product>
  python3 -m render.accrue_warmpool_separation sandbox --latest P --history Q   # explicit (tests)

Import note: same namespace-package shadowing dodge as generate.py / accrue_history.py —
`render/` binds as a namespace package under `-m render.accrue_warmpool_separation`, so we put
`_HERE` first on sys.path and import schema flatly. That resolves identically under `-m`,
`python3 render/accrue_warmpool_separation.py`, and `cd render && python3
accrue_warmpool_separation.py`, with no __init__.py.
"""

import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

from schema import (  # noqa: E402
    backfill_legacy_warmpool_separation_row,
    WARMPOOL_SEPARATION_HISTORY_FIELDS,
)
from warmpool_verdict import rig_stratified_comparison  # noqa: E402


def _repo_root():
    return os.path.dirname(_HERE)


def _warmpool_separation_cell(results):
    """Return (ratio, n, outcome, ttfe_p95_ms) from a MEASURED warmpool_cold_start cell, or None.

    "Measurable" means sla_metrics carries warmpool_gate_separation_ratio — NOT
    outcome == "PASS". The scenario surfaces the ratio on BOTH PASS and FAIL (a below-gate ratio
    is exactly what FAILs the scenario and exactly the signal this store exists to chart); only a
    scenario run that never computed the gate metric at all (sla_metrics absent/empty, or present
    without the key) emits nothing to chart.

    hb#727 follow-up: ttfe_p95_ms is read from the SAME cell, on a best-effort basis — its
    absence never suppresses the row (the ratio is still the required, gating measurement); a
    fire that measured the ratio but not ttfe_p95_ms just carries ttfe_p95_ms=None downstream.
    """
    if not isinstance(results, dict):
        return None
    for s in results.get("scenarios", []) or []:
        if not isinstance(s, dict) or s.get("name") != "warmpool_cold_start":
            continue
        m = s.get("sla_metrics")
        if not isinstance(m, dict) or "warmpool_gate_separation_ratio" not in m:
            return None
        ratio = m.get("warmpool_gate_separation_ratio")
        n = s.get("n")
        outcome = s.get("outcome")
        ttfe_p95_ms = m.get("ttfe_p95_ms")
        return ratio, n, outcome, ttfe_p95_ms
    return None


def _candidate_row(results):
    """Return the pre-validation candidate row, or None if no separation ratio was measured.

    None here is CASE 1 — a legitimate honest-skip: the run carries no warmpool_cold_start cell
    with a measured separation ratio, so there is genuinely nothing to chart. This is distinct
    from CASE 2 (a ratio *was* measured but a provenance field cannot anchor it to a build), which
    is a validated-None from `_validate_row` below — the caller must tell the two apart so it can
    report an accurate reason instead of a false "no ratio" skip.
    """
    measured = _warmpool_separation_cell(results)
    if measured is None:
        return None
    ratio, n, outcome, ttfe_p95_ms = measured
    prov = results.get("provenance") if isinstance(results, dict) else None
    prov = prov if isinstance(prov, dict) else {}
    return {
        "generated_at": results.get("generated_at"),
        "controller_digest": prov.get("controller_digest"),
        "suite_git_sha": prov.get("suite_git_sha"),
        "run_id": prov.get("run_id"),
        "cluster_substrate": prov.get("cluster_substrate"),
        "n": n,
        "outcome": outcome,
        "separation_ratio": ratio,
        # hb#700 rig-stratified disclosure (closing disposition, 2026-08-25): rig shape rides in
        # provenance.node_count on every fire already -- not gated by CASE-1/CASE-2 above, since
        # those gate count-measurability/build-anchoring specifically, and a missing node_count
        # is rig-shape metadata, not a correctness-critical measurement. Absent -> None (honest
        # "not recorded"), never guessed.
        "node_count": prov.get("node_count"),
        # hb#727 follow-up (a4z1's rig-confound question, 2026-08-25): extend the same
        # rig-stratified pattern to the raw warm-hit TTFE, so a headline delta banner like
        # hb#727's (6.80s -> 15.79s across a node_count 4->2 + node_image float) is automatically
        # contextualized by node-count-stratified history instead of a single unstratified
        # before/after point. Both new fields are non-gating metadata (like node_count): absent
        # -> None, never guessed, never blocks the row on a missing value.
        "ttfe_p95_ms": ttfe_p95_ms,
        "node_image": prov.get("node_image"),
    }


def _validate_row(candidate):
    """Validate a candidate against WARMPOOL_SEPARATION_HISTORY_FIELDS.

    Return (row, None) or (None, bad_key); `bad_key` names the first field that is missing or
    fails its predicate — i.e. the reason a measured ratio could not be anchored to a build
    (CASE 2). Keeping the failing key lets the caller emit a loud, accurate diagnosis rather than
    a silent, misleading honest-skip.
    """
    row = {}
    for key, ok in WARMPOOL_SEPARATION_HISTORY_FIELDS.items():
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

    Returns a dict containing exactly WARMPOOL_SEPARATION_HISTORY_FIELDS keys, each value already
    validated; or None when the run carries no measurable separation ratio (honest-skip) or a
    required field fails its predicate (cannot anchor the row to a build). Callers that need to
    tell those two None-cases apart use `_candidate_row` + `_validate_row` directly (see main()).
    """
    candidate = _candidate_row(results)
    if candidate is None:
        return None
    row, _ = _validate_row(candidate)
    return row


def load_history(path):
    """Read warmpool-separation-history.jsonl into a list of validated rows.

    Malformed lines (bad JSON, or a row failing WARMPOOL_SEPARATION_HISTORY_FIELDS validation)
    are dropped rather than raising — a corrupt line degrades the store to fewer trend rows,
    never to a crash or a leak. `node_count` became a required key in hb#700; a pre-hb#700 row is
    back-filled to node_count=None (mirroring accrue_history.load_history's
    backfill_legacy_history_row call) so those rows survive rather than being silently and
    permanently erased on the next append()'s rewrite-from-survivors.
    """
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
            obj = backfill_legacy_warmpool_separation_row(obj)
            clean = {}
            ok_all = True
            for key, ok in WARMPOOL_SEPARATION_HISTORY_FIELDS.items():
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


def append(row, history_path):
    """Append `row` to warmpool-separation-history.jsonl; return the written rows.

    Idempotent-by-run_id: if a row with the same run_id already exists, this is a no-op (the
    existing rows are returned unchanged, file untouched) — re-running accrual against the same
    fire's latest.json must not duplicate it. Any OTHER row, including one sharing a
    controller_digest with an existing row, is always appended — never upserted-by-digest, which
    is precisely the behavior that would erase the same-build variance this store exists to
    disclose. Rows are written ordered by generated_at so the file reads as a timeline.
    """
    rows = load_history(history_path)
    if any(r["run_id"] == row["run_id"] for r in rows):
        return rows
    rows.append(row)
    rows.sort(key=lambda r: r["generated_at"])
    os.makedirs(os.path.dirname(history_path), exist_ok=True)
    with open(history_path, "w") as fh:
        for r in rows:
            fh.write(json.dumps({k: r[k] for k in WARMPOOL_SEPARATION_HISTORY_FIELDS}, sort_keys=True) + "\n")
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description="accrue warmpool-separation-ratio measurement history")
    ap.add_argument("product", help="product whose results/latest.json to accrue (e.g. sandbox)")
    ap.add_argument("--latest", default=None, help="override path to latest.json")
    ap.add_argument("--history", default=None, help="override path to warmpool-separation-history.jsonl")
    args = ap.parse_args(argv)

    root = _repo_root()
    latest = args.latest or os.path.join(root, args.product, "results", "latest.json")
    history = args.history or os.path.join(
        root, args.product, "results", "warmpool-separation-history.jsonl"
    )

    if not os.path.exists(latest):
        sys.stderr.write(f"accrue_warmpool_separation: no latest.json at {latest} — nothing to accrue\n")
        return 0
    with open(latest) as fh:
        results = json.load(fh)

    candidate = _candidate_row(results)
    if candidate is None:
        # CASE 1: genuinely no measurable separation ratio (warmpool_cold_start absent, or its
        # sla_metrics never computed the gate metric). A ratio that was never measured cannot be
        # charted — benign honest-skip, exit 0.
        sys.stderr.write(
            "accrue_warmpool_separation: latest.json has no measurable warmpool_gate_separation_ratio — skip\n"
        )
        return 0

    row, bad_key = _validate_row(candidate)
    if row is None:
        # CASE 2: a ratio *was* measured but the run cannot be anchored to a build (a provenance
        # field is missing/invalid). Skipping here silently freezes the variance trend while the
        # fire still reports success — a trust-surface silent-degrade. Fail LOUD + closed instead
        # (rc=3): the fire reds, its EXIT-trap teardown still runs, and the accurate reason is on
        # the log — never a false "no measurable ratio" message.
        ratio = candidate.get("separation_ratio")
        bad_val = candidate.get(bad_key)
        sys.stderr.write(
            f"accrue_warmpool_separation: measured separation_ratio={ratio!r} but "
            f"field {bad_key!r} (value={bad_val!r}) failed validation — cannot anchor to a "
            "build; refusing to silently drop a real measurement\n"
        )
        return 3

    rows = append(row, history)
    sys.stderr.write(
        f"accrue_warmpool_separation: appended run_id={row['run_id']} "
        f"digest={row['controller_digest']} ratio={row['separation_ratio']} "
        f"outcome={row['outcome']} -> {history} ({len(rows)} total rows)\n"
    )

    # hb#700 item 3b (closing disposition, 2026-08-25): standing rig-stratified bootstrap,
    # recomputed fresh from the full accrued history on every accrual run. Advisory only
    # ("flag, don't verdict") -- never affects this script's exit code or the fire's outcome,
    # and never moves WARMPOOL_SEPARATION_MIN_RATIO or any other threshold. A flagged result is
    # surfaced loudly (stderr) so it's visible in fire logs without gating anything; an
    # unflagged or insufficient-data result stays quiet (nothing yet worth a human's attention).
    #
    # hb#727 follow-up (a4z1's rig-confound question, 2026-08-25): the SAME stratified-bootstrap
    # machinery is run a SECOND time against `ttfe_p95_ms` -- a headline warm-hit TTFE delta
    # banner (like hb#727's 6.80s -> 15.79s) is exactly as confounded by rig shape as a bare
    # separation_ratio delta was pre-hb#700, so it gets the identical treatment rather than
    # staying a single unstratified before/after point.
    for comparison in (
        rig_stratified_comparison(rows),
        rig_stratified_comparison(rows, metric_field="ttfe_p95_ms"),
    ):
        if comparison["reason"] == "flagged":
            sys.stderr.write(
                "accrue_warmpool_separation: RIG-STRATIFIED FLAG (hb#700 item 3b, advisory only) -- "
                f"metric={comparison['metric_field']} "
                f"{comparison['group_field']}={comparison['group_a']} median="
                f"{comparison['median_a']:.3f} (n={comparison['n_a']}) vs "
                f"{comparison['group_field']}={comparison['group_b']} median="
                f"{comparison['median_b']:.3f} (n={comparison['n_b']}): "
                f"P(B>A)={comparison['p_b_gt_a']:.3f}, "
                f"{int(comparison['confidence'] * 100)}% CI=[{comparison['ci_low']:.3f}, "
                f"{comparison['ci_high']:.3f}] -- names a candidate worth a human's attention, does "
                "not itself conclude causation or move any threshold\n"
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
