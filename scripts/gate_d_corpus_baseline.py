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

Provenance guard: the ctrl leg only counts toward any population when it is a
genuine controller measurement (controller_measured is True AND ctrl_span_source
is present). An unmeasured/estimated or provenance-blind ctrl leg is excluded
from both the ratio and the rel-diff — a ratio built on an estimated denominator
would let the cold CONTROL (the rung the verdict leans on to isolate the
mechanism) rest on a non-measurement. Exclusions are surfaced, not silent; if the
exclusion thins the cold control or the warm rungs below the minimum, the verdict
falls to INSUFFICIENT_DATA (fail-closed HOLD), never a definitive call.

Read-only over the committed corpus. No cluster, no network, no fire.

Post-as#1087-merge expectation: the warm sub-saturation ratio collapses toward
~1.0 and the gate-(d) rel-diff drops under 0.10 on those rungs. If it does not,
the double-count was not the (whole) root cause.

The --verdict mode turns the same signals into a fail-closed graduation gate
for the trust-gated gate-(d) cell (a downgrade of a trust surface must fail
closed, never silently): it exits 0 ONLY when the corpus
shows the double-count SIGNATURE_CLEARED, so flipping the cell to green once
as#1087's re-record lands becomes mechanical + auditable instead of a
human eyeball call. Every non-cleared state (still-present, ambiguous,
too-little-data) exits non-zero -> the cell HOLDS.

Usage:
  scripts/gate_d_corpus_baseline.py [records_dir]             # human summary + verdict
  scripts/gate_d_corpus_baseline.py --verdict [records_dir]   # machine gate: exit 0 iff CLEARED
"""
import glob
import json
import os
import statistics
import sys

GATE_TOL = 0.10          # harness/slo_rate.py LITERAL_RATE_AGREEMENT_TOL
HIGH_RATE_OFFERED = 10.0  # offered rate at/above which the ctrl leg is throughput-limited

# --- Falsification-verdict thresholds (--verdict mode) --------------------
# The verdict mechanizes a fail-closed HOLD on the trust-gated gate-(d) cell
# (a trust surface graduates loud + conservative, never on a silent maybe):
# only SIGNATURE_CLEARED permits graduating the cell to green once
# as#1087's re-record lands. Every other state HOLDS. Thresholds are derived
# from the pre-fix corpus baseline (warm sub-saturation ratio median ~1.75,
# cold control ~1.0) with deliberate dead-bands so a noisy re-record can't
# alias CLEARED.
WARM_SIGNATURE_MIN = 1.40      # warm_sub ratio median >= this => double-count present
WARM_CLEARED_MAX = 1.20       # warm_sub ratio median <= this => double-count gone
COLD_CONTROL_LO = 0.85        # cold ratio must stay in [LO,HI] to isolate the mechanism
COLD_CONTROL_HI = 1.15
WARM_PASS_FRACTION_MIN = 0.60  # fraction of warm rungs passing gate-(d) for CLEARED
MIN_WARM_RUNGS = 3            # below this, warm median is not judgeable -> INSUFFICIENT

# Verdict -> process exit code. Graduation gate reads "exit 0 iff CLEARED";
# the specific non-zero code says why the cell HELD. 1 is reserved for a
# real error (no records), so the verdict codes start at 2.
VERDICT_EXIT = {
    "SIGNATURE_CLEARED": 0,
    "SIGNATURE_PRESENT": 2,
    "AMBIGUOUS": 3,
    "INSUFFICIENT_DATA": 4,
}


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


def ctrl_provenance_ok(step):
    """True iff this step's ctrl leg is a genuine controller measurement.

    Gate-(d)'s whole verdict rests on the ctrl leg (ctrl_event_count / span)
    being a real controller /metrics count, not an estimate. A step whose
    controller_measured is not True, or whose ctrl_span_source is absent, is
    provenance-degraded: its ctrl rate/count is a floor/estimate, so a ratio or
    rel-diff built on it compares acq against an untrustworthy denominator.
    Per the trust-surface fail-closed idiom, such a leg must not count toward a
    graduation-permitting population — the verdict HOLDs on measured legs only.
    """
    return (step.get("controller_measured") is True
            and bool(step.get("ctrl_span_source")))


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
        # A ctrl leg that is not a genuine controller measurement is
        # provenance-degraded: null its ratio AND rel-diff so every downstream
        # population (summarize + verdict) uniformly excludes it via the
        # existing `is not None` filters. The exclusion is surfaced (not silent)
        # via the provenance_degraded marker below — a quiet drop of a rung
        # would itself be the kind of trust-surface degrade this guards against.
        provenance_ok = ctrl_provenance_ok(step)
        ratio = (ctrl_event_count / acq_n) if (ctrl_event_count and acq_n) else None
        rd = rel_diff(acq_rate, ctrl_rate)
        if not provenance_ok:
            ratio = None
            rd = None
        rungs.append({
            "file": filename,
            "offered_rate_per_s": offered,
            "acq_n": acq_n,
            "ctrl_event_count": ctrl_event_count,
            "controller_measured": step.get("controller_measured"),
            "ctrl_span_source": step.get("ctrl_span_source"),
            "provenance_degraded": not provenance_ok,
            "rel_diff": rd,
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
        "provenance_degraded_total": sum(1 for r in rungs if r.get("provenance_degraded")),
        "provenance_degraded_by_regime": {},
    }
    for regime in ("warm_sub", "cold", "warm_high"):
        vals = [r["ratio"] for r in rungs if r["regime"] == regime and r["ratio"] is not None]
        out["ratio_by_regime"][regime] = _stats(vals)
        out["provenance_degraded_by_regime"][regime] = sum(
            1 for r in rungs if r["regime"] == regime and r.get("provenance_degraded"))
    return out


def verdict(rungs):
    """Fail-closed falsification verdict on the as#1087 double-count fix.

    This is the mechanical graduation gate for the trust-gated gate-(d) cell:
    the cell may flip to green ONLY when this returns
    SIGNATURE_CLEARED on a re-recorded corpus. Every other state HOLDS — a
    noisy, partial, or mechanism-unclear re-record must never alias CLEARED.

    The judgement rests on three independent signals:
      - warm_sub ratio median   — the double-count magnitude (~1.75 pre-fix,
                                   ~1.0 once as#1087's re-record lands)
      - cold ratio median       — the CONTROL: cold has no warm-pool informer
                                   so no replay; it must stay ~1.0 for any
                                   definitive verdict. Cold drifting out of
                                   band means the mechanism is not isolated,
                                   so neither PRESENT nor CLEARED is trustable.
      - warm gate-(d) pass frac — |acq-ctrl|/max <= GATE_TOL on warm rungs;
                                   the fix's whole point is these rungs start
                                   passing the live agreement gate.

    States: SIGNATURE_PRESENT (pre-fix, HOLD), SIGNATURE_CLEARED (graduate),
    AMBIGUOUS (intermediate / mechanism-unclear, HOLD), INSUFFICIENT_DATA
    (too few warm rungs or no cold control, HOLD).
    """
    # ratio / rel_diff on provenance-degraded rungs were already nulled in
    # extract_rungs, so these `is not None` filters exclude an unmeasured ctrl
    # leg here — the verdict populations are measured-leg-only by construction.
    warm_ratios = [r["ratio"] for r in rungs
                   if r["regime"] == "warm_sub" and r["ratio"] is not None]
    cold_ratios = [r["ratio"] for r in rungs
                   if r["regime"] == "cold" and r["ratio"] is not None]
    warm_rel = [r["rel_diff"] for r in rungs
                if r["regime"] == "warm_sub" and r["rel_diff"] is not None]
    cold_degraded = sum(1 for r in rungs
                        if r["regime"] == "cold" and r.get("provenance_degraded"))
    warm_degraded = sum(1 for r in rungs
                        if r["regime"] == "warm_sub" and r.get("provenance_degraded"))

    def _v(state, reason, **extra):
        out = {"state": state, "reason": reason,
               "warm_n": len(warm_ratios), "cold_n": len(cold_ratios),
               "warm_ratio_median": (statistics.median(warm_ratios)
                                     if warm_ratios else None),
               "cold_ratio_median": (statistics.median(cold_ratios)
                                     if cold_ratios else None),
               "warm_pass_fraction": (sum(1 for x in warm_rel if x <= GATE_TOL)
                                      / len(warm_rel)) if warm_rel else None,
               "cold_provenance_degraded": cold_degraded,
               "warm_provenance_degraded": warm_degraded}
        out.update(extra)
        return out

    if len(warm_ratios) < MIN_WARM_RUNGS or not cold_ratios or not warm_rel:
        excl = ""
        if cold_degraded or warm_degraded:
            excl = (f" (excluded {warm_degraded} warm + {cold_degraded} cold "
                    f"provenance-degraded ctrl legs)")
        return _v("INSUFFICIENT_DATA",
                  f"need >= {MIN_WARM_RUNGS} warm rungs with a ratio, a cold "
                  f"control, and warm rel-diffs; have warm_ratio={len(warm_ratios)} "
                  f"cold={len(cold_ratios)} warm_rel={len(warm_rel)}{excl}")

    warm_med = statistics.median(warm_ratios)
    cold_med = statistics.median(cold_ratios)
    warm_pass = sum(1 for x in warm_rel if x <= GATE_TOL) / len(warm_rel)

    # Cold control gates EVERY definitive verdict: if the control regime is
    # itself off ~1.0, the mechanism is not isolated and no PRESENT/CLEARED
    # call is trustworthy -> HOLD as AMBIGUOUS.
    if not (COLD_CONTROL_LO <= cold_med <= COLD_CONTROL_HI):
        return _v("AMBIGUOUS",
                  f"cold control median {cold_med:.2f} outside "
                  f"[{COLD_CONTROL_LO},{COLD_CONTROL_HI}] -- mechanism not "
                  f"isolated, verdict withheld")

    if warm_med >= WARM_SIGNATURE_MIN and warm_pass < WARM_PASS_FRACTION_MIN:
        return _v("SIGNATURE_PRESENT",
                  f"warm ratio median {warm_med:.2f} >= {WARM_SIGNATURE_MIN} and "
                  f"warm gate-(d) pass {warm_pass:.0%} < {WARM_PASS_FRACTION_MIN:.0%} "
                  f"-- double-count present, cell HOLDS")

    if warm_med <= WARM_CLEARED_MAX and warm_pass >= WARM_PASS_FRACTION_MIN:
        return _v("SIGNATURE_CLEARED",
                  f"warm ratio median {warm_med:.2f} <= {WARM_CLEARED_MAX} and "
                  f"warm gate-(d) pass {warm_pass:.0%} >= {WARM_PASS_FRACTION_MIN:.0%} "
                  f"-- double-count gone, cell may graduate")

    return _v("AMBIGUOUS",
              f"warm ratio median {warm_med:.2f} / pass {warm_pass:.0%} in the "
              f"dead-band between PRESENT (>= {WARM_SIGNATURE_MIN} & pass < "
              f"{WARM_PASS_FRACTION_MIN:.0%}) and CLEARED (<= {WARM_CLEARED_MAX} & "
              f"pass >= {WARM_PASS_FRACTION_MIN:.0%}) -- verdict withheld")


def _fmt(st):
    if not st:
        return "n=0"
    return (f"n={st['n']} median={st['median']:.2f} mean={st['mean']:.2f} "
            f"range=[{st['min']:.2f},{st['max']:.2f}]")


def main(argv):
    args = argv[1:]
    verdict_only = "--verdict" in args
    positional = [a for a in args if not a.startswith("--")]
    records_dir = positional[0] if positional else "sandbox/records"

    records = load_records(records_dir)
    if not records:
        print(f"no records under {records_dir}", file=sys.stderr)
        return 1
    rungs = []
    for filename, record in records:
        rungs.extend(extract_rungs(filename, record))

    v = verdict(rungs)

    if verdict_only:
        # Machine gate: one terse verdict line + exit code (0 iff CLEARED).
        print(f"VERDICT: {v['state']} -- {v['reason']}")
        return VERDICT_EXIT[v["state"]]

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
        deg = s["provenance_degraded_by_regime"][regime]
        deg_note = f"  [+{deg} excluded: provenance-degraded]" if deg else ""
        print(f"  {label:44} {_fmt(s['ratio_by_regime'][regime])}{deg_note}")
    if s["provenance_degraded_total"]:
        print(f"provenance-degraded ctrl legs excluded (not controller-measured "
              f"or span-source absent): {s['provenance_degraded_total']}")
    print(f"\nfalsification verdict: {v['state']}")
    print(f"  {v['reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
