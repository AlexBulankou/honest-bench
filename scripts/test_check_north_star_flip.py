#!/usr/bin/env python3
"""Tests for the cross-lane North Star PASS->FAIL merge guard (hb#623).

Dependency-free: `python3 scripts/test_check_north_star_flip.py` (exit 0 = pass).
Auto-discovered by the offline unit-tests gate (find . -name 'test_*.py').
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check_north_star_flip as g  # noqa: E402


def _results(p95, outcome, *, runtime="gvisor", product="sandbox", include_scenario=True):
    """Minimal closed-schema-clean results dict with one warmpool_cold_start scenario."""
    scenarios = []
    if include_scenario:
        scenarios.append(
            {
                "name": "warmpool_cold_start",
                "n": 30,
                "outcome": outcome,
                "sla_metrics": {"ttfe_p95_ms": p95, "ttfe_p50_ms": (p95 - 100 if p95 else None)},
            }
        )
    return {"product": product, "provenance": {"runtime": runtime}, "scenarios": scenarios}


def _write(tmp, name, obj):
    p = os.path.join(tmp, name)
    with open(p, "w") as fh:
        json.dump(obj, fh)
    return p


def _run(base_obj, pr_obj, *, base_missing=False, pr_malformed=False):
    with tempfile.TemporaryDirectory() as tmp:
        base = os.path.join(tmp, "base.json")
        if not base_missing:
            _write(tmp, "base.json", base_obj)
        if pr_malformed:
            pr = os.path.join(tmp, "pr.json")
            with open(pr, "w") as fh:
                fh.write("{not valid json")
        else:
            pr = _write(tmp, "pr.json", pr_obj)
        return g.check(base, pr)


def _verdict_label(results):
    v = g._verdicts(results)
    # measured runtime is the only non-pending entry
    return {k: val for k, val in v.items() if val != "pending"}


def main():
    failures = []

    def expect(cond, msg):
        if not cond:
            failures.append(msg)

    # Verdict rule -----------------------------------------------------------
    expect(_verdict_label(_results(500, "PASS")) == {"gVisor": "PASS"},
           "p95<bar + PASS outcome should be PASS")
    expect(_verdict_label(_results(1500, "PASS")) == {"gVisor": "FAIL"},
           "p95>=bar should be FAIL even with PASS outcome")
    expect(_verdict_label(_results(500, "FAIL")) == {"gVisor": "FAIL"},
           "FAIL outcome overrides a sub-bar p95 (customer sees Scenario-FAIL)")
    expect(g._verdicts(_results(None, "pending", include_scenario=False)).get("gVisor") == "pending",
           "no warmpool scenario -> pending, never a verdict")

    # Flip detection ---------------------------------------------------------
    code, flips, _ = _run(_results(500, "PASS"), _results(500, "PASS"))
    expect(code == 0 and not flips, "PASS->PASS must not flip (exit 0)")

    code, flips, _ = _run(_results(500, "PASS"), _results(1500, "PASS"))
    expect(code == 3 and flips == [("gVisor", "PASS", "FAIL")],
           "PASS->FAIL via p95 must block (exit 3)")

    code, flips, _ = _run(_results(500, "PASS"), _results(500, "FAIL"))
    expect(code == 3 and flips == [("gVisor", "PASS", "FAIL")],
           "PASS->FAIL via outcome must block (exit 3)")

    code, flips, _ = _run(_results(1500, "PASS"), _results(500, "PASS"))
    expect(code == 0 and not flips, "FAIL->PASS upgrade must NOT block (exit 0)")

    code, flips, _ = _run(_results(500, "PASS"),
                          _results(None, "pending", include_scenario=False))
    expect(code == 0 and not flips,
           "PASS->pending (unmeasured) is not a PASS->FAIL flip (exit 0)")

    # Bootstrap / error handling --------------------------------------------
    code, _flips, _ = _run(None, _results(1500, "PASS"), base_missing=True)
    expect(code == 0, "missing base file -> bootstrap, block nothing (exit 0)")

    code, _flips, _ = _run(_results(500, "PASS"), None, pr_malformed=True)
    expect(code == 2, "malformed PR json -> fail closed (exit 2)")

    # Override path (main() wiring) -----------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        base = _write(tmp, "base.json", _results(500, "PASS"))
        pr = _write(tmp, "pr.json", _results(1500, "PASS"))
        expect(g.main(["--base", base, "--pr", pr]) == 3,
               "un-overridden flip via main() exits 3")
        expect(g.main(["--base", base, "--pr", pr, "--allow-flip"]) == 3,
               "hb#827: --allow-flip with NO ack stamp still fails closed (exit 3)")

    # hb#827: _find_flip_p95 -------------------------------------------------
    base_r = _results(500, "PASS")
    pr_r = _results(1500, "PASS")
    label, prior, current = g._find_flip_p95(base_r, pr_r)
    expect((label, prior, current) == ("gVisor", 500, 1500),
           "_find_flip_p95 resolves the single flipped label + its p95 pair")

    try:
        g._find_flip_p95(_results(500, "PASS"), _results(500, "PASS"))
        expect(False, "_find_flip_p95 on PASS->PASS should raise ValueError (no flip)")
    except ValueError:
        pass

    try:
        g._find_flip_p95(_results(500, "PASS"), _results(1500, "PASS"), runtime_label="Kata + microVM")
        expect(False, "_find_flip_p95 with a --runtime not in the flip set should raise ValueError")
    except ValueError:
        pass

    # hb#827: write_stamp ------------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        base = _write(tmp, "base.json", _results(500, "PASS"))
        pr = _write(tmp, "pr.json", _results(1500, "PASS"))
        try:
            g.write_stamp(base, pr, "not-a-real-reason")
            expect(False, "write_stamp with an out-of-enum reason should raise ValueError")
        except ValueError:
            pass

        label, prior, current = g.write_stamp(base, pr, "confirmed-regression")
        expect((label, prior, current) == ("gVisor", 500, 1500),
               "write_stamp returns the (label, prior, current) triple it stamped")

        stamped = json.load(open(pr))
        prov = stamped.get("provenance", {})
        expect(prov.get("north_star_flip_ack_prior_ttfe_p95_ms") == 500,
               "write_stamp persists the prior p95")
        expect(prov.get("north_star_flip_ack_current_ttfe_p95_ms") == 1500,
               "write_stamp persists the current p95")
        expect(prov.get("north_star_flip_ack_reason") == "confirmed-regression",
               "write_stamp persists the reason")
        expect(prov.get("runtime") == "gvisor",
               "write_stamp preserves pre-existing provenance keys")

        # --allow-flip now succeeds once the stamp matches the live flip.
        expect(g.main(["--base", base, "--pr", pr, "--allow-flip"]) == 0,
               "hb#827: --allow-flip WITH a matching ack stamp permits the flip (exit 0)")

    # hb#827: write_stamp with no flip present (nothing to adjudicate) ------
    with tempfile.TemporaryDirectory() as tmp:
        base = _write(tmp, "base.json", _results(500, "PASS"))
        pr = _write(tmp, "pr.json", _results(500, "PASS"))
        try:
            g.write_stamp(base, pr, "confirmed-regression")
            expect(False, "write_stamp with no PASS->FAIL flip should raise ValueError")
        except ValueError:
            pass

    # hb#827: _stamp_covers_flips mismatch (auto-clear) ----------------------
    with tempfile.TemporaryDirectory() as tmp:
        base = _write(tmp, "base.json", _results(500, "PASS"))
        pr = _write(tmp, "pr.json", _results(1500, "PASS"))
        g.write_stamp(base, pr, "confirmed-regression")

        # Now superscede the PR's measured p95 without re-stamping -- the
        # committed stamp no longer matches the pair actually being flipped,
        # so it must NOT cover the (now-different) flip.
        stale_pr_obj = _results(1600, "PASS")
        stamped = json.load(open(pr))
        stale_pr_obj["provenance"].update(
            {
                "north_star_flip_ack_prior_ttfe_p95_ms": stamped["provenance"][
                    "north_star_flip_ack_prior_ttfe_p95_ms"
                ],
                "north_star_flip_ack_current_ttfe_p95_ms": stamped["provenance"][
                    "north_star_flip_ack_current_ttfe_p95_ms"
                ],
                "north_star_flip_ack_reason": stamped["provenance"]["north_star_flip_ack_reason"],
            }
        )
        pr2 = _write(tmp, "pr2.json", stale_pr_obj)
        expect(g.main(["--base", base, "--pr", pr2, "--allow-flip"]) == 3,
               "hb#827: a stamp pinned to a superseded p95 pair does not cover a fresh flip")

    if failures:
        print("FAIL:")
        for f in failures:
            print("  -", f)
        return 1
    print("check_north_star_flip: all cases pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
