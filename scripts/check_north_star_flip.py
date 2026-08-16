#!/usr/bin/env python3
"""Cross-lane North Star PASS->FAIL merge-order guard (hb#623).

honest-bench renders the live customer-facing headline from a SINGLE per-product
`latest.json`, so the README shows only the LAST-merged fire. Two auto-refresh
lanes write the same target: the fork lane (`auto/refresh-gke-sandbox`) and the
upstream lane (`auto/refresh-gke-sandbox-upstream`). They keep SEPARATE provenance
chains, so the existing refresh-over-refresh delta caveat (`_north_star_delta_flag`
in render/render.py) compares each lane only against its OWN carried-forward prior
fire -- it structurally cannot see a CROSS-lane overwrite. Merging an upstream-lane
FAIL after a fork-lane PASS therefore silently flips the live headline PASS->FAIL
(the cross-lane merge-order near-miss), inverting the "fix-in-fork" narrative with
no guard.

This is a fail-closed PR-check: it compares the North Star verdict of the PR's
`latest.json` against `main`'s CURRENTLY-MERGED `latest.json` (fetched by the
Cloud Build step, NOT the lane's own prior fire) and refuses a verdict flip from
PASS to FAIL on any runtime -- the trust-surface idiom: a downgrade on a
customer-facing surface must fail closed, never overwrite quietly.

Override: pass --allow-flip (the Cloud Build step sets this only when the PR head
commit carries a `[NORTH-STAR-FLIP-OK]` trailer line, mirroring the fleet
`[ROLL-NOW]` opt-in idiom). An overridden flip prints loudly and exits 0.

Verdict rule (mirrors what the rendered caption shows, render/render.py
render_north_star_caption + _north_star_fail_caveat): for the measured runtime's
`warmpool_cold_start` scenario --
  PASS  iff ttfe_p95_ms is present AND < NORTH_STAR_TTFE_P95_MS AND outcome != FAIL
  FAIL  iff ttfe_p95_ms is present AND (>= bar OR outcome == FAIL)
  (a missing p95 is `pending`, an unmeasured runtime -- never a verdict, never a flip)

Exit codes:
  0  no PASS->FAIL flip (or an overridden flip, or a bootstrap with no base file)
  2  checker/input error (malformed base or PR JSON) -- fail closed
  3  an un-overridden PASS->FAIL flip was detected -- blocks the merge
"""

import argparse
import json
import os
import sys

# Reuse the real render logic so the gate and the rendered headline cannot drift.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO_ROOT, "render"))
from render import NORTH_STAR_TTFE_P95_MS, _north_star_rows  # noqa: E402


def _verdicts(results):
    """{runtime_label: 'PASS'|'FAIL'|'pending'} from one product's results dict.

    Uses render._north_star_rows so the p95 sourcing + FAIL-outcome override match
    the rendered caption exactly (single product only -- no kata merge, since each
    product's latest.json is its own independent overwrite target).
    """
    out = {}
    for label, p95, _cell, _p50, _n, outcome in _north_star_rows(results):
        if p95 is None:
            out[label] = "pending"
        elif p95 < NORTH_STAR_TTFE_P95_MS and outcome != "FAIL":
            out[label] = "PASS"
        else:
            out[label] = "FAIL"
    return out


def _load(path):
    with open(path) as fh:
        d = json.load(fh)
    if not isinstance(d, dict):
        raise ValueError(f"{path}: top-level JSON is not an object")
    return d


def check(base_path, pr_path):
    """Return (exit_code, flips, report_lines). flips = [(label, base, pr), ...]."""
    lines = []
    if not os.path.exists(base_path):
        # No currently-merged baseline for this product -> nothing to protect.
        # A verdict cannot flip from a baseline that does not exist (bootstrap /
        # brand-new product lane). Fail OPEN: block nothing.
        lines.append(f"[flip-gate] no base file at {base_path} -- bootstrap, nothing to protect (PASS)")
        return 0, [], lines

    try:
        base = _load(base_path)
        pr = _load(pr_path)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        lines.append(f"[flip-gate] ERROR reading results: {e} -- failing closed (exit 2)")
        return 2, [], lines

    base_v = _verdicts(base)
    pr_v = _verdicts(pr)

    flips = []
    for label, pr_verdict in pr_v.items():
        if base_v.get(label) == "PASS" and pr_verdict == "FAIL":
            flips.append((label, "PASS", "FAIL"))

    prod = pr.get("product", "?")
    lines.append(f"[flip-gate] product={prod}  base={base_v}  pr={pr_v}")
    if flips:
        for label, b, p in flips:
            lines.append(f"[flip-gate] VERDICT FLIP {b}->{p} on runtime '{label}' (customer headline downgrade)")
        return 3, flips, lines
    lines.append("[flip-gate] no PASS->FAIL flip")
    return 0, flips, lines


def main(argv=None):
    ap = argparse.ArgumentParser(description="Cross-lane North Star PASS->FAIL merge guard (hb#623)")
    ap.add_argument("--base", required=True, help="main's currently-merged latest.json for this product")
    ap.add_argument("--pr", required=True, help="the PR head's latest.json for this product")
    ap.add_argument(
        "--allow-flip",
        action="store_true",
        help="downgrade a detected flip from a hard block to a loud warning "
        "(set by the Cloud Build step only when the PR head carries a "
        "[NORTH-STAR-FLIP-OK] trailer)",
    )
    args = ap.parse_args(argv)

    code, flips, lines = check(args.base, args.pr)
    for ln in lines:
        print(ln)

    if code == 3 and flips and args.allow_flip:
        print("[flip-gate] --allow-flip set ([NORTH-STAR-FLIP-OK] override) -- flip permitted, exiting 0")
        return 0
    return code


if __name__ == "__main__":
    sys.exit(main())
