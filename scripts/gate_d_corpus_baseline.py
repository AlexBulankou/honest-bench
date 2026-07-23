#!/usr/bin/env python3
"""Gate-(d) acq/ctrl agreement — corpus-wide pre-fix baseline over stored records.

Gate (d) (harness/slo_rate.py) publishes a literal-basis SLO rung only when the
two independent throughput legs agree within LITERAL_RATE_AGREEMENT_TOL (0.10):

  - acq leg  = acq_fulfilled_per_s  (the acquisition watch's drive-aligned rate)
  - ctrl leg = controller_completed_per_s  (a controller /metrics count-delta /span)

The ctrl leg is inflated by the upstream stale-informer replay double-count that
as#1087 fixes: a replayed informer event re-records the same claim's Ready
completion, so the controller's completion COUNT runs ~2x the true number of
claims at warm sub-saturation. This tool quantifies that signature across every
stored step-record so the eventual as#1087 merge + re-record has a mechanical
falsification target.

What it computes, joining each pareto rung's acq leg to its ctrl raw counts
(keyed on the identical controller_completed_per_s float that both surfaces carry):

  1. gate-(d) rel-diff distribution   |acq - ctrl| / max(acq, ctrl)
  2. the double-count ratio            ctrl_event_count / acq_n
     split by regime:
       - warm sub-saturation : ~1.7-2.0  (the stale-informer replay)
       - cold                : ~1.0      (no warm-pool informer -> no replay;
                                          the control that isolates the mechanism)
       - warm high-rate      : <1.0      (controller throughput-limited above the
                                          knee -- a DISTINCT confound, not replay)

Read-only over the committed corpus. No cluster, no network, no fire.

Post-as#1087-merge expectation: the warm sub-saturation ratio collapses toward
~1.0 and the gate-(d) rel-diff drops under 0.10 on those rungs. If it does not,
the double-count was not the (whole) root cause.

Usage:
  scripts/gate_d_corpus_baseline.py [records_dir]   # default: sandbox/records
"""
import glob
import json
import os
import statistics
import sys

GATE_TOL = 0.10          # harness/slo_rate.py LITERAL_RATE_AGREEMENT_TOL
HIGH_RATE_OFFERED = 10.0  # offered rate at/above which the ctrl leg is throughput-limited


def load_records(records_dir):
    out = []
    for path in sorted(glob.glob(os.path.join(records_dir, "*.json"))):
        try:
            with open(path) as fh:
                out.append((os.path.basename(path), json.load(fh)))
        except (OSError, ValueError):
            continue
    return out


def rel_diff(a, b):
    """Gate-(d) relative difference; None if either leg is missing / non-positive."""
    if not a or not b:
        return None
    hi = max(a, b)
    if hi <= 0:
        return None
    return abs(a - b) / hi


def classify_regime(filename, offered_rate):
    """cold | warm_high | warm_sub — the three ctrl/acq regimes."""
    if "cold" in filename:
        return "cold"
    if offered_rate is not None and offered_rate >= HIGH_RATE_OFFERED:
        return "warm_high"
    return "warm_sub"


def extract_rungs(filename, record):
    """Join each literal_ttfe.pareto rung (acq leg) to its steps[] ctrl raw counts.

    steps[].offered_rate_per_s is not populated, but both surfaces carry the
    identical controller_completed_per_s float, so that is the stable join key.
    """
    steps_by_ctrl = {}
    for step in record.get("steps") or []:
        ctrl = step.get("controller_completed_per_s")
        if ctrl is not None:
            steps_by_ctrl[round(float(ctrl), 9)] = step

    rungs = []
    literal = record.get("literal_ttfe") or {}
    for rung in literal.get("pareto") or []:
        acq_rate = rung.get("acq_fulfilled_per_s")
        ctrl_rate = rung.get("controller_completed_per_s")
        acq_n = rung.get("acq_n")
        offered = rung.get("offered_rate_per_s")
        step = steps_by_ctrl.get(round(float(ctrl_rate), 9), {}) if ctrl_rate is not None else {}
        ctrl_event_count = step.get("ctrl_event_count")
        ratio = (ctrl_event_count / acq_n) if (ctrl_event_count and acq_n) else None
        rungs.append({
            "file": filename,
            "offered_rate_per_s": offered,
            "acq_n": acq_n,
            "ctrl_event_count": ctrl_event_count,
            "rel_diff": rel_diff(acq_rate, ctrl_rate),
            "ratio": ratio,
            "regime": classify_regime(filename, offered),
        })
    return rungs


def _stats(values):
    if not values:
        return None
    return {
        "n": len(values),
        "min": min(values),
        "median": statistics.median(values),
        "mean": statistics.mean(values),
        "max": max(values),
    }


def summarize(rungs):
    rel = [r["rel_diff"] for r in rungs if r["rel_diff"] is not None]
    out = {
        "rel_diff": _stats(rel),
        "rel_diff_pass_gate": sum(1 for x in rel if x <= GATE_TOL),
        "rel_diff_total": len(rel),
        "ratio_by_regime": {},
    }
    for regime in ("warm_sub", "cold", "warm_high"):
        vals = [r["ratio"] for r in rungs if r["regime"] == regime and r["ratio"] is not None]
        out["ratio_by_regime"][regime] = _stats(vals)
    return out


def _fmt(st):
    if not st:
        return "n=0"
    return (f"n={st['n']} median={st['median']:.2f} mean={st['mean']:.2f} "
            f"range=[{st['min']:.2f},{st['max']:.2f}]")


def main(argv):
    records_dir = argv[1] if len(argv) > 1 else "sandbox/records"
    records = load_records(records_dir)
    if not records:
        print(f"no records under {records_dir}", file=sys.stderr)
        return 1
    rungs = []
    for filename, record in records:
        rungs.extend(extract_rungs(filename, record))
    s = summarize(rungs)

    print(f"records: {len(records)}  pareto rungs: {len(rungs)}")
    rd = s["rel_diff"]
    if rd:
        print(f"gate-(d) rel-diff: {_fmt(rd)}  "
              f"PASS<=0.10: {s['rel_diff_pass_gate']}/{s['rel_diff_total']} "
              f"({100 * s['rel_diff_pass_gate'] / s['rel_diff_total']:.0f}%)")
    print("ctrl_event_count / acq_n (double-count ratio):")
    labels = {
        "warm_sub": "warm sub-saturation (double-count ~1.7-2.0)",
        "cold": "cold (control, ~1.0 -> passes)",
        "warm_high": "warm high-rate (controller-lag <1.0)",
    }
    for regime, label in labels.items():
        print(f"  {label:44} {_fmt(s['ratio_by_regime'][regime])}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
