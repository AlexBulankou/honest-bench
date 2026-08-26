#!/usr/bin/env python3
"""Data-refresh fast-lane eligibility signal (hb#765).

WHY THIS EXISTS. honest-bench's per-fire data-refresh PRs (the gke-sandbox and
gke-kata auto-refresh lanes) sit in the peer-review queue like any other PR even
when a fire only refreshed numbers. #765 asks whether a provably data/docs-only
refresh could skip that latency -- but #762 is the load-bearing counter-example:
that refresh PR's diff LOOKED like a routine numbers-only update yet actually
flipped two anomalies from cleared to ACTIVE, deleted a live caveat section
(moving its content to the archive), and added brand-new FAIL/ACTIVE annotations
to the README's headline table and status list. A file-path allow-list alone
(README.md/DETAILS.md/results/*.json are always the touched paths, refresh or
not) cannot distinguish that PR from a boring one -- it would have called #762
fast-lane-eligible.

WHAT THIS CHECKS. Two independent gates, both must hold for eligibility:

  1. Changed-file set (git diff --name-only <base-ref> HEAD) is a SUBSET of the
     product's allow-listed refresh outputs -- no incidental edit to a script,
     workflow config, or a file outside the known refresh-output surface rode
     along in the same commit.
  2. The "disclosure fingerprint" of README.md and DETAILS.md -- every line that
     carries a caveat/anomaly/status marker (warning glyphs, FAIL/ACTIVE/pending
     tokens, the full Known-anomalies and Resolved-archive sections) -- is
     BYTE-IDENTICAL between <base-ref> and HEAD. Any change inside that
     fingerprint (a new caveat, a cleared one, a flipped status cell, an
     archived-vs-live section move) fails the gate. Plain metric-value churn
     OUTSIDE those marked lines never trips it.

This is advisory, not a merge gate (mirrors the fleet's transition-guards idiom:
a downgrade signal fails closed by DEFAULTING to "needs review", never by
silently upgrading a real disclosure change to "safe to skip"). It never blocks
a build or a merge -- see cloudbuild-refresh-gke-sandbox.yaml/
cloudbuild-refresh-gke-kata.yaml's sdev-delta-notify step, which posts the
verdict to #s-dev and always exits 0 regardless of eligibility.

Exit codes:
  0  ran cleanly (ELIGIBLE or NOT-ELIGIBLE -- see printed verdict line)
  2  checker/input error (bad git ref, unreadable file) -- fail closed to
     NOT-ELIGIBLE by construction (caller must not read exit 2 as eligible)
"""

import argparse
import re
import subprocess
import sys

# Per-product allow-lists, mirroring each pipeline's own open-pr `git add` list
# (cloudbuild-refresh-gke-sandbox.yaml / cloudbuild-refresh-gke-kata.yaml) --
# keep these in sync if either pipeline's committed-file list changes.
ALLOWED_FILES = {
    "sandbox": {
        "sandbox/results/latest.json",
        "sandbox/results/history.jsonl",
        "sandbox/results/warmpool-separation-history.jsonl",
        "README.md",
        "DETAILS.md",
        "render/upstream_links.json",
    },
    "sandbox-kata": {
        "sandbox-kata/results/latest.json",
        "README.md",
        "DETAILS.md",
    },
}

# Lines matching ANY of these are part of the disclosure fingerprint. Intentionally
# broad/coarse (a false "changed" is safe -- it only costs a fast-lane skip; a false
# "unchanged" would silently wave through a real disclosure regression).
_MARKER_RE = re.compile(r"⚠️|❌|✅|\bFAIL\b|\bACTIVE\b|\bpending\b")
# Section headings whose ENTIRE body (heading through the next heading of the
# same-or-higher level, or EOF) is part of the fingerprint -- these carry
# disclosure prose that may not put a marker glyph on every line.
_SECTION_HEADINGS = (
    "## Known anomalies",
    "## Resolved (archive)",
)


def _run_git(args):
    return subprocess.run(
        ["git"] + args, capture_output=True, text=True, check=True
    ).stdout


def read_at_ref(ref, path):
    """Return file content at a git ref, or None if the path doesn't exist there."""
    try:
        return _run_git(["show", f"{ref}:{path}"])
    except subprocess.CalledProcessError:
        return None


def read_worktree(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def changed_files(base_ref, head_ref="HEAD"):
    out = _run_git(["diff", "--name-only", base_ref, head_ref])
    return {ln for ln in out.splitlines() if ln}


def _section_body(lines, heading):
    """Extract the full body of `heading` through the next '## '/'# ' heading."""
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == heading)
    except StopIteration:
        return None
    end = len(lines)
    for i in range(start + 1, len(lines)):
        stripped = lines[i].strip()
        if stripped.startswith("## ") or stripped.startswith("# "):
            end = i
            break
    return "\n".join(lines[start:end])


def disclosure_fingerprint(text):
    """A deterministic string capturing every disclosure-relevant line/section."""
    if text is None:
        return None
    lines = text.splitlines()
    parts = []
    for heading in _SECTION_HEADINGS:
        body = _section_body(lines, heading)
        if body is not None:
            parts.append(f"### SECTION {heading} ###\n{body}")
    marked = sorted({ln for ln in lines if _MARKER_RE.search(ln)})
    parts.append("### MARKED LINES ###\n" + "\n".join(marked))
    return "\n".join(parts)


def check(product, base_ref, head_ref="HEAD", worktree_paths=("README.md", "DETAILS.md")):
    """Return (eligible: bool, reasons: list[str])."""
    reasons = []
    allowed = ALLOWED_FILES.get(product)
    if allowed is None:
        return False, [f"unknown product '{product}' -- no allow-list, failing closed"]

    changed = changed_files(base_ref, head_ref)
    if not changed:
        reasons.append("no files changed vs base-ref -- trivially eligible (nothing to publish)")
        return True, reasons

    disallowed = changed - allowed
    if disallowed:
        reasons.append(
            f"changed-file set includes non-allow-listed path(s): {sorted(disallowed)}"
        )
    else:
        reasons.append(f"changed-file set is fully allow-listed: {sorted(changed)}")

    disclosure_changed = []
    for path in worktree_paths:
        base_text = read_at_ref(base_ref, path)
        head_text = read_worktree(path)
        base_fp = disclosure_fingerprint(base_text)
        head_fp = disclosure_fingerprint(head_text)
        if base_fp != head_fp:
            disclosure_changed.append(path)

    if disclosure_changed:
        reasons.append(
            f"disclosure fingerprint CHANGED in: {disclosure_changed} "
            "(a caveat/anomaly/status line differs -- needs a human read)"
        )
    else:
        reasons.append("disclosure fingerprint unchanged in README.md/DETAILS.md")

    eligible = not disallowed and not disclosure_changed
    return eligible, reasons


def main(argv=None):
    ap = argparse.ArgumentParser(description="Data-refresh fast-lane eligibility signal (hb#765)")
    ap.add_argument("--product", required=True, choices=sorted(ALLOWED_FILES))
    ap.add_argument("--base-ref", default="HEAD~1", help="git ref for the pre-refresh state (default HEAD~1)")
    ap.add_argument("--head-ref", default="HEAD", help="git ref for the post-refresh state (default HEAD)")
    args = ap.parse_args(argv)

    try:
        eligible, reasons = check(args.product, args.base_ref, args.head_ref)
    except subprocess.CalledProcessError as e:
        print(f"[fastlane-eligible] ERROR running git: {e} -- failing closed (NOT-ELIGIBLE)")
        for ln in (e.stderr or "").splitlines():
            print(f"[fastlane-eligible]   {ln}")
        return 2

    for ln in reasons:
        print(f"[fastlane-eligible] {ln}")
    verdict = "ELIGIBLE" if eligible else "NOT-ELIGIBLE"
    print(f"[fastlane-eligible] VERDICT: {verdict} (product={args.product}, base={args.base_ref})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
