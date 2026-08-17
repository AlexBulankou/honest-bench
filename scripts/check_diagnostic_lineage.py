#!/usr/bin/env python3
"""Diagnostic-lineage merge guard (a#6915, hb#646 root-cause follow-up).

hb#643/#644 merged a deliberate zero-fix "clean-upstream" reproduction fire
(fork_fix_count=0, fork_sha == fork_base_upstream_sha -- staged for the #6890
warm-pool investigation) straight into `main`'s `results/latest.json`, silently
overwriting the validated production fork pin with known-bad diagnostic data
for ~1.9h until the hb#646 revert. Nothing at merge time distinguished that
diagnostic fire from the auto-refresh cron's normal output -- this checker
closes that gap.

Signature: a fork-build fire is a DIAGNOSTIC reproduction, not a production
lineage, when either:
  - fork_fix_count == 0 (a fork with zero staged fixes over its base -- a
    clean-upstream reproduction build, not a validated fix pin), or
  - fork_sha == fork_base_upstream_sha (the "fork" HEAD is byte-identical to
    the upstream base it branched from -- self-referential, not a fork at all)

Either shape is intrinsically bad on a committed `latest.json` -- it does not
need a comparison against `main`'s prior state, unlike the North Star flip
gate (hb#623) that only cares about a RELATIVE PASS->FAIL transition. So this
checker takes only the PR-side file: no `origin/main` fetch, no per-product
base lookup.

Override: --allow-diagnostic (the Cloud Build step sets this only when the PR
head commit carries a [DIAGNOSTIC-LINEAGE-OK] trailer, mirroring the fleet
[ROLL-NOW] / [NORTH-STAR-FLIP-OK] opt-in idiom). An overridden signature
prints loudly and exits 0 -- the escape hatch for a deliberate, reviewed
"yes, publish diagnostic data" decision (expected to be rare).

Exit codes:
  0  no diagnostic-lineage signature on this product (or overridden)
  2  checker/input error (malformed PR JSON) -- fail closed
  3  an un-overridden diagnostic-lineage signature was detected -- blocks the
     merge
"""

import argparse
import json
import sys


def _load(path):
    with open(path) as fh:
        d = json.load(fh)
    if not isinstance(d, dict):
        raise ValueError(f"{path}: top-level JSON is not an object")
    return d


def _diagnostic_signature(prov):
    """Return a list of human-readable reasons if `prov` (a provenance dict)
    carries a diagnostic-lineage fork-build signature, else []."""
    if not isinstance(prov, dict):
        return []
    reasons = []
    fork_sha = prov.get("fork_sha")
    base = prov.get("fork_base_upstream_sha")
    n = prov.get("fork_fix_count")
    has_fix_count = isinstance(n, int) and not isinstance(n, bool)
    if has_fix_count and n == 0:
        reasons.append(
            "fork_fix_count == 0 (a zero-fix reproduction build, not a validated fix pin)"
        )
    if (
        isinstance(fork_sha, str)
        and isinstance(base, str)
        and fork_sha
        and base
        and fork_sha == base
    ):
        reasons.append(
            f"fork_sha == fork_base_upstream_sha == {fork_sha!r} "
            "(the \"fork\" is byte-identical to its own base)"
        )
    return reasons


def check(pr_path):
    """Return (exit_code, reasons, report_lines)."""
    lines = []
    try:
        pr = _load(pr_path)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        lines.append(
            f"[diagnostic-lineage-gate] ERROR reading {pr_path}: {e} -- failing closed (exit 2)"
        )
        return 2, [], lines

    prod = pr.get("product", "?")
    reasons = _diagnostic_signature(pr.get("provenance"))
    if reasons:
        for r in reasons:
            lines.append(f"[diagnostic-lineage-gate] product={prod}: {r}")
        return 3, reasons, lines
    lines.append(f"[diagnostic-lineage-gate] product={prod}: no diagnostic-lineage signature")
    return 0, [], lines


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Diagnostic-lineage merge guard (a#6915, hb#646 root-cause follow-up)"
    )
    ap.add_argument("--pr", required=True, help="the PR head's latest.json for this product")
    ap.add_argument(
        "--allow-diagnostic",
        action="store_true",
        help="downgrade a detected diagnostic-lineage signature from a hard block to a loud "
        "warning (set by the Cloud Build step only when the PR head carries a "
        "[DIAGNOSTIC-LINEAGE-OK] trailer)",
    )
    args = ap.parse_args(argv)

    code, reasons, lines = check(args.pr)
    for ln in lines:
        print(ln)

    if code == 3 and reasons and args.allow_diagnostic:
        print(
            "[diagnostic-lineage-gate] --allow-diagnostic set ([DIAGNOSTIC-LINEAGE-OK] "
            "override) -- signature permitted, exiting 0"
        )
        return 0
    return code


if __name__ == "__main__":
    sys.exit(main())
