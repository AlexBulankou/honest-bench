#!/usr/bin/env python3
"""Cross-lane North Star PASS->FAIL merge-order guard (hb#623).

honest-bench renders the live customer-facing headline from a SINGLE per-product
`latest.json`, so the README shows only the LAST-merged fire. Two auto-refresh
lanes write the same target: the fork lane (`auto/refresh-gke-sandbox`) and the
upstream lane (`auto/refresh-gke-sandbox-upstream-<build-id>`, a unique branch
per fire since hb#682). They keep SEPARATE provenance chains, so the existing
refresh-over-refresh delta caveat (`_north_star_delta_flag`
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
from schema import NORTH_STAR_FLIP_REASONS  # noqa: E402


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


def _p95_by_label(results):
    """{runtime_label: ttfe_p95_ms} from one product's results dict, p95-present rows only."""
    return {
        label: p95
        for label, p95, _cell, _p50, _n, _outcome in _north_star_rows(results)
        if p95 is not None
    }


def _find_flip_p95(base, pr, runtime_label=None):
    """Resolve the single (label, prior_p95, current_p95) triple a --write-stamp call

    should record. Raises ValueError (caller's job to turn into an exit-2 message) when
    the pair is ambiguous or ill-formed -- a hand-run maintenance command should refuse
    to guess which flip it is being asked to adjudicate.
    """
    base_v = _verdicts(base)
    pr_v = _verdicts(pr)
    flips = [
        label
        for label, verdict in pr_v.items()
        if base_v.get(label) == "PASS" and verdict == "FAIL"
    ]
    if runtime_label is not None:
        if runtime_label not in flips:
            raise ValueError(
                f"--runtime {runtime_label!r} is not a PASS->FAIL flip between --base and "
                f"--pr (flips present: {flips!r})"
            )
        flips = [runtime_label]
    if not flips:
        raise ValueError("no PASS->FAIL flip between --base and --pr -- nothing to stamp")
    if len(flips) > 1:
        raise ValueError(
            f"multiple PASS->FAIL flips present ({flips!r}) -- pass --runtime to disambiguate "
            "(the ack stamp is a single triple per product file, so only one flip can be "
            "adjudicated per --write-stamp call)"
        )
    label = flips[0]
    prior = _p95_by_label(base).get(label)
    current = _p95_by_label(pr).get(label)
    if prior is None or current is None:
        raise ValueError(f"could not resolve a measured p95 pair for flipped label {label!r}")
    return label, prior, current


def write_stamp(base_path, pr_path, reason, runtime_label=None):
    """Patch --pr's top-level `provenance` dict with the hb#827 north_star_flip_ack
    triple, in place. Returns (label, prior_p95, current_p95) on success."""
    if reason not in NORTH_STAR_FLIP_REASONS:
        raise ValueError(f"--reason must be one of {sorted(NORTH_STAR_FLIP_REASONS)!r}, got {reason!r}")
    base = _load(base_path)
    pr = _load(pr_path)
    label, prior, current = _find_flip_p95(base, pr, runtime_label=runtime_label)
    prov = pr.setdefault("provenance", {})
    if not isinstance(prov, dict):
        raise ValueError(f"{pr_path}: top-level 'provenance' is not an object")
    prov["north_star_flip_ack_prior_ttfe_p95_ms"] = prior
    prov["north_star_flip_ack_current_ttfe_p95_ms"] = current
    prov["north_star_flip_ack_reason"] = reason
    with open(pr_path, "w") as fh:
        json.dump(pr, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return label, prior, current


def _stamp_covers_flips(pr, base, flips):
    """Return (covered, uncovered_labels). `covered` is True iff --pr's provenance carries
    a north_star_flip_ack stamp whose (prior, current) pair matches EVERY flip in `flips`
    exactly -- the same value-equality gate render.py's caveat applies, so a stamp left over
    from a different (already-superseded) pair can never silently cover a fresh flip."""
    prov = pr.get("provenance")
    if not isinstance(prov, dict):
        return False, [label for label, _b, _p in flips]
    reason = prov.get("north_star_flip_ack_reason")
    ack_prior = prov.get("north_star_flip_ack_prior_ttfe_p95_ms")
    ack_current = prov.get("north_star_flip_ack_current_ttfe_p95_ms")
    valid_ack = (
        reason in NORTH_STAR_FLIP_REASONS
        and isinstance(ack_prior, (int, float)) and not isinstance(ack_prior, bool)
        and isinstance(ack_current, (int, float)) and not isinstance(ack_current, bool)
    )
    base_p95 = _p95_by_label(base)
    pr_p95 = _p95_by_label(pr)
    uncovered = []
    for label, _b, _p in flips:
        if (
            valid_ack
            and base_p95.get(label) == ack_prior
            and pr_p95.get(label) == ack_current
        ):
            continue
        uncovered.append(label)
    return (not uncovered), uncovered


def main(argv=None):
    ap = argparse.ArgumentParser(description="Cross-lane North Star PASS->FAIL merge guard (hb#623)")
    ap.add_argument("--base", required=True, help="main's currently-merged latest.json for this product")
    ap.add_argument("--pr", required=True, help="the PR head's latest.json for this product")
    ap.add_argument(
        "--allow-flip",
        action="store_true",
        help="downgrade a detected flip from a hard block to a loud warning "
        "(set by the Cloud Build step only when the PR head carries a "
        "[NORTH-STAR-FLIP-OK] trailer) -- ALSO now requires a matching north_star_flip_ack "
        "stamp on --pr (hb#827): an override with no stamp still fails closed.",
    )
    ap.add_argument(
        "--write-stamp",
        action="store_true",
        help="maintenance mode (hb#827): instead of checking, patch --pr's provenance with "
        "a north_star_flip_ack stamp recording WHY the detected PASS->FAIL flip is being "
        "adjudicated via [NORTH-STAR-FLIP-OK]. Requires --reason. Run this by hand once, "
        "before merging the flip-carrying PR, then commit the stamped --pr file.",
    )
    ap.add_argument(
        "--reason",
        choices=sorted(NORTH_STAR_FLIP_REASONS),
        default=None,
        help="required with --write-stamp: why the flip is being acknowledged",
    )
    ap.add_argument(
        "--runtime",
        default=None,
        help="disambiguate which flipped runtime label --write-stamp should adjudicate, "
        "only needed if --base/--pr show more than one PASS->FAIL flip",
    )
    args = ap.parse_args(argv)

    if args.write_stamp:
        if args.reason is None:
            print("[flip-gate] --write-stamp requires --reason -- failing closed (exit 2)")
            return 2
        try:
            label, prior, current = write_stamp(
                args.base, args.pr, args.reason, runtime_label=args.runtime
            )
        except (OSError, ValueError, json.JSONDecodeError) as e:
            print(f"[flip-gate] --write-stamp ERROR: {e} -- failing closed (exit 2)")
            return 2
        print(
            f"[flip-gate] stamped {args.pr}: runtime={label!r} prior_p95={prior} "
            f"current_p95={current} reason={args.reason!r} -- commit this file"
        )
        return 0

    code, flips, lines = check(args.base, args.pr)
    for ln in lines:
        print(ln)

    if code == 3 and flips and args.allow_flip:
        base = _load(args.base)
        pr = _load(args.pr)
        covered, uncovered = _stamp_covers_flips(pr, base, flips)
        if covered:
            print(
                "[flip-gate] --allow-flip set ([NORTH-STAR-FLIP-OK] override) AND a matching "
                "north_star_flip_ack stamp covers every flipped runtime -- flip permitted, "
                "exiting 0"
            )
            return 0
        print(
            "[flip-gate] --allow-flip set but NO matching north_star_flip_ack stamp covers "
            f"runtime(s) {uncovered!r} -- an override with no stamp fails closed (hb#827). Run "
            "`check_north_star_flip.py --write-stamp --reason <enum> --base <base> --pr <pr>` "
            "and commit the stamped file before merging."
        )
        return code
    return code


if __name__ == "__main__":
    sys.exit(main())
