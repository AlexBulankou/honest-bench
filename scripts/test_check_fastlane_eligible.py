#!/usr/bin/env python3
"""Tests for the data-refresh fast-lane eligibility signal (hb#765).

Dependency-free save for `git` itself: `python3 scripts/test_check_fastlane_eligible.py`
(exit 0 = pass). Auto-discovered by the offline unit-tests gate (find . -name 'test_*.py').
Exercises the checker against a REAL temp git repo (not mocked) since its two gates
(changed-file allow-list, disclosure fingerprint) are both defined in terms of git refs.
"""

import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check_fastlane_eligible as g  # noqa: E402


def _git(args, cwd):
    subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True, check=True)


def _write(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _init_repo(tmp):
    _git(["init", "-q"], tmp)
    _git(["config", "user.email", "test@localhost"], tmp)
    _git(["config", "user.name", "test"], tmp)


README_BASE = """# Sandbox

## Known anomalies
none currently.

## Resolved (archive)
- old thing, cleared.
"""

DETAILS_BASE = "latency: 12ms\n"


def _seed(tmp, readme=README_BASE, details=DETAILS_BASE, extra_files=None):
    _init_repo(tmp)
    _write(os.path.join(tmp, "README.md"), readme)
    _write(os.path.join(tmp, "DETAILS.md"), details)
    os.makedirs(os.path.join(tmp, "sandbox", "results"), exist_ok=True)
    _write(os.path.join(tmp, "sandbox", "results", "latest.json"), '{"v": 1}\n')
    for rel, content in (extra_files or {}).items():
        full = os.path.join(tmp, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        _write(full, content)
    _git(["add", "-A"], tmp)
    _git(["commit", "-q", "-m", "seed"], tmp)


def _commit_worktree_changes(tmp, msg="refresh"):
    _git(["add", "-A"], tmp)
    _git(["commit", "-q", "-m", msg], tmp)


def _run_check(tmp, product="sandbox"):
    cwd = os.getcwd()
    try:
        os.chdir(tmp)
        return g.check(product, "HEAD~1", "HEAD")
    finally:
        os.chdir(cwd)


def main():
    failures = []

    def expect(cond, msg):
        if not cond:
            failures.append(msg)

    # disclosure_fingerprint -----------------------------------------------
    expect(g.disclosure_fingerprint(None) is None, "None text -> None fingerprint")
    a = "line one\nno markers here\nline three\n"
    b = "line one\ndifferent metric value here\nline three\n"
    expect(g.disclosure_fingerprint(a) == g.disclosure_fingerprint(b),
           "metric-only churn outside marked lines must NOT change the fingerprint")
    c = "line one\n⚠️ a new caveat\nline three\n"
    expect(g.disclosure_fingerprint(a) != g.disclosure_fingerprint(c),
           "a new marker-glyph line must change the fingerprint")
    d_active = "status: ACTIVE\n"
    d_cleared = "status: cleared\n"
    expect(g.disclosure_fingerprint(d_active) != g.disclosure_fingerprint(d_cleared),
           "ACTIVE vs cleared token must change the fingerprint")

    # section-body coverage
    sec_a = "# Title\n\n## Known anomalies\nfoo bar baz\n\n## Other\nunrelated\n"
    sec_b = "# Title\n\n## Known anomalies\nfoo bar QUUX\n\n## Other\nunrelated\n"
    expect(g.disclosure_fingerprint(sec_a) != g.disclosure_fingerprint(sec_b),
           "a prose-only change inside ## Known anomalies (no marker glyph) must still change the fingerprint")

    # Case 1: pure metric-value refresh, no disclosure change -> ELIGIBLE ---
    with tempfile.TemporaryDirectory() as tmp:
        _seed(tmp)
        _write(os.path.join(tmp, "sandbox", "results", "latest.json"), '{"v": 2}\n')
        _write(os.path.join(tmp, "DETAILS.md"), DETAILS_BASE.replace("12ms", "13ms"))
        _commit_worktree_changes(tmp)
        eligible, reasons = _run_check(tmp)
        expect(eligible, "boring numbers-only refresh should be ELIGIBLE: %r" % (reasons,))

    # Case 2: the #762 counter-example -- flips a cleared anomaly to ACTIVE
    # and moves a live caveat to the archive -- must be NOT-ELIGIBLE even
    # though only allow-listed files changed.
    with tempfile.TemporaryDirectory() as tmp:
        _seed(tmp, readme="""# Sandbox

## Known anomalies
- flaky-thing: cleared

## Resolved (archive)
""")
        _write(os.path.join(tmp, "README.md"), """# Sandbox

## Known anomalies
- flaky-thing: ACTIVE

## Resolved (archive)
""")
        _commit_worktree_changes(tmp)
        eligible, reasons = _run_check(tmp)
        expect(not eligible, "a cleared->ACTIVE flip must be NOT-ELIGIBLE (the #762 near-miss): %r" % (reasons,))

    # Case 3: disallowed file rides along (e.g. a script edit) -> NOT-ELIGIBLE
    with tempfile.TemporaryDirectory() as tmp:
        _seed(tmp, extra_files={"scripts/measure.sh": "echo old\n"})
        _write(os.path.join(tmp, "scripts", "measure.sh"), "echo new\n")
        _commit_worktree_changes(tmp)
        eligible, reasons = _run_check(tmp)
        expect(not eligible, "an incidental script edit must be NOT-ELIGIBLE: %r" % (reasons,))
        expect(any("non-allow-listed" in r for r in reasons), "reason should name the disallowed path")

    # Case 4: unknown product -> fail closed -----------------------------
    with tempfile.TemporaryDirectory() as tmp:
        _seed(tmp)
        _write(os.path.join(tmp, "sandbox", "results", "latest.json"), '{"v": 2}\n')
        _commit_worktree_changes(tmp, msg="noop")
        eligible, reasons = _run_check(tmp, product="not-a-real-product")
        expect(not eligible, "unknown product must fail closed")

    # Case 5: no files changed vs base -> trivially eligible ---------------
    with tempfile.TemporaryDirectory() as tmp:
        _seed(tmp)
        # second commit with literally no diff (allowed -- git permits empty commits with --allow-empty)
        _git(["commit", "-q", "--allow-empty", "-m", "empty"], tmp)
        eligible, reasons = _run_check(tmp)
        expect(eligible, "no changed files vs base-ref should be trivially eligible: %r" % (reasons,))

    # Case 6: sandbox-kata product, smaller allow-list ----------------------
    with tempfile.TemporaryDirectory() as tmp:
        _init_repo(tmp)
        _write_kata = os.path.join(tmp, "sandbox-kata", "results")
        _write(os.path.join(tmp, "README.md"), README_BASE)
        _write(os.path.join(tmp, "DETAILS.md"), DETAILS_BASE)
        os.makedirs(_write_kata, exist_ok=True)
        _write(os.path.join(_write_kata, "latest.json"), '{"v": 1}\n')
        _git(["add", "-A"], tmp)
        _git(["commit", "-q", "-m", "seed"], tmp)
        _write(os.path.join(_write_kata, "latest.json"), '{"v": 2}\n')
        _commit_worktree_changes(tmp)
        eligible, reasons = _run_check(tmp, product="sandbox-kata")
        expect(eligible, "sandbox-kata numbers-only refresh should be ELIGIBLE: %r" % (reasons,))

    # main() wiring: --product/--base-ref/--head-ref plumb through, exit 0 always ---
    with tempfile.TemporaryDirectory() as tmp:
        _seed(tmp)
        _write(os.path.join(tmp, "sandbox", "results", "latest.json"), '{"v": 2}\n')
        _commit_worktree_changes(tmp)
        cwd = os.getcwd()
        try:
            os.chdir(tmp)
            rc = g.main(["--product", "sandbox", "--base-ref", "HEAD~1", "--head-ref", "HEAD"])
            expect(rc == 0, "main() always exits 0 on a clean run regardless of verdict (advisory, never blocks)")
        finally:
            os.chdir(cwd)

    # main() error path: bad base-ref -> exit 2, fail closed ----------------
    with tempfile.TemporaryDirectory() as tmp:
        _seed(tmp)
        cwd = os.getcwd()
        try:
            os.chdir(tmp)
            rc = g.main(["--product", "sandbox", "--base-ref", "not-a-real-ref"])
            expect(rc == 2, "a bad git ref must fail closed with exit 2")
        finally:
            os.chdir(cwd)

    if failures:
        print("FAIL:")
        for f in failures:
            print("  -", f)
        return 1
    print("check_fastlane_eligible: all cases pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
