"""Offline tests for verify-upstream-freshness.py (hb#181).

Covers the correctness-critical PURE logic only — the declared-vs-live
normalization matrix (`_expected_ok`) and the unique-ref dedup
(`_iter_unique_refs`). These make NO network calls, so they are safe in the CI
unit-test gate; the live-fetch path (`_fetch_live`) is deliberately not exercised
here (it is operator-invoked, network-dependent, and out of the offline gate's
scope by design).

Stdlib-only + self-running via the __main__ guard, matching the repo convention
(CI runs each test module with bare `python3 <file>`; pytest is intentionally
absent from harness/requirements.txt). The script under test has a hyphenated
filename, so it is loaded by path via importlib.
"""

import importlib.util
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(_HERE, "verify-upstream-freshness.py")

_spec = importlib.util.spec_from_file_location("verify_upstream_freshness", _SCRIPT)
vuf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vuf)


def _check(name, cond):
    if not cond:
        raise AssertionError("FAIL: %s" % name)


def test_expected_ok_open():
    # declared open matches a live-open issue and a live-open PR of the same kind
    _check("open-issue OK", vuf._expected_ok("issue", "open", "open", False, False))
    _check("open-pr OK", vuf._expected_ok("pr", "open", "open", True, False))
    # declared open must NOT match a closed live ref
    _check("open vs closed drift", not vuf._expected_ok("issue", "open", "closed", False, False))


def test_expected_ok_in_review():
    # in-review is a PR-only status: matches a live open PR
    _check("in-review open-pr OK", vuf._expected_ok("pr", "in-review", "open", True, False))
    # NOT an issue, NOT a closed/merged PR
    _check("in-review vs issue drift", not vuf._expected_ok("pr", "in-review", "open", False, False))
    _check("in-review vs merged drift", not vuf._expected_ok("pr", "in-review", "closed", True, True))


def test_expected_ok_merged():
    _check("merged OK", vuf._expected_ok("pr", "merged", "closed", True, True))
    # a closed-but-UNmerged PR is drift against declared merged
    _check("merged vs closed-unmerged drift", not vuf._expected_ok("pr", "merged", "closed", True, False))
    # an open PR is drift against declared merged
    _check("merged vs open drift", not vuf._expected_ok("pr", "merged", "open", True, False))


def test_expected_ok_closed():
    # declared closed matches a closed issue and a closed-unmerged PR
    _check("closed-issue OK", vuf._expected_ok("issue", "closed", "closed", False, False))
    _check("closed-unmerged-pr OK", vuf._expected_ok("pr", "closed", "closed", True, False))
    # a merged PR is NOT plain 'closed' (it must be declared 'merged')
    _check("closed vs merged drift", not vuf._expected_ok("pr", "closed", "closed", True, True))
    # an open ref is drift against declared closed
    _check("closed vs open drift", not vuf._expected_ok("issue", "closed", "open", False, False))


def test_expected_ok_kind_mismatch():
    # kind disagreement is drift regardless of open/closed agreement
    _check("declared-issue live-pr drift", not vuf._expected_ok("issue", "open", "open", True, False))
    _check("declared-pr live-issue drift", not vuf._expected_ok("pr", "open", "open", False, False))


def test_iter_unique_refs_dedup():
    classes = {
        "a": {"refs": [{"repo": "r/x", "number": 1}, {"repo": "r/x", "number": 2}]},
        "b": {"refs": [{"repo": "r/x", "number": 1}, {"repo": "r/x", "number": 3}]},
    }
    out = list(vuf._iter_unique_refs(classes))
    nums = [ref["number"] for ref, _ in out]
    _check("dedup count == 3", len(out) == 3)
    _check("json order preserved", nums == [1, 2, 3])
    # ref #1 appears in both classes -> both recorded
    classes_for_1 = next(cs for ref, cs in out if ref["number"] == 1)
    _check("shared ref carries both classes", classes_for_1 == ["a", "b"])


def test_live_desc_formatting():
    _check("issue desc", vuf._live_desc("open", False, False) == "open (issue)")
    _check("pr desc", vuf._live_desc("open", True, False) == "open (PR)")
    _check("merged desc", vuf._live_desc("closed", True, True) == "closed (PR, merged)")


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print("ok — %d test function(s) passed" % len(tests))


if __name__ == "__main__":
    _run_all()
