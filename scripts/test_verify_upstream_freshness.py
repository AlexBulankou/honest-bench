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
import json
import os
import tempfile

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


def _write_results(base_sha):
    """Write a temp results file carrying provenance.fork_base_upstream_sha; return its abs path.

    _refresh_commit_distance joins cfg["results"] with _REPO_ROOT, but os.path.join returns an
    absolute path unchanged, so an absolute temp path bypasses the repo root cleanly.
    """
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        prov = {} if base_sha is None else {"fork_base_upstream_sha": base_sha}
        json.dump({"provenance": prov}, f)
    return path


def test_refresh_commit_distance_builds_stamp_on_success():
    # With a live fetch stubbed, _refresh_commit_distance reads the CURRENT fork base from the
    # results file and emits a stamp keyed on it — the single-source-of-truth base, never config.
    path = _write_results("deadbeefcafe")
    try:
        raw = {
            "_meta": {
                "fork_upstreams": {
                    "sandbox": {
                        "repo": "kubernetes-sigs/agent-sandbox",
                        "branch": "main",
                        "results": path,
                    }
                }
            }
        }
        orig = vuf._fetch_commits_behind
        vuf._fetch_commits_behind = lambda repo, base, branch, token=None: 42
        try:
            out = vuf._refresh_commit_distance(raw)
        finally:
            vuf._fetch_commits_behind = orig
        _check("has sandbox key", "sandbox" in out)
        _check("base_sha from results", out["sandbox"]["base_sha"] == "deadbeefcafe")
        _check("commits_behind from fetch", out["sandbox"]["commits_behind"] == 42)
        _check("repo carried", out["sandbox"]["upstream_repo"] == "kubernetes-sigs/agent-sandbox")
        _check("branch carried", out["sandbox"]["upstream_branch"] == "main")
        _check("checked_at present", bool(out["sandbox"]["checked_at"]))
    finally:
        os.unlink(path)


def test_refresh_commit_distance_raises_on_missing_base():
    # A results file with no fork_base_upstream_sha ⇒ raise (fail-closed): we must never stamp a
    # distance we cannot anchor to a concrete base.
    path = _write_results(None)
    try:
        raw = {
            "_meta": {
                "fork_upstreams": {
                    "sandbox": {
                        "repo": "kubernetes-sigs/agent-sandbox",
                        "branch": "main",
                        "results": path,
                    }
                }
            }
        }
        raised = False
        try:
            vuf._refresh_commit_distance(raw)
        except ValueError:
            raised = True
        _check("missing base raises ValueError", raised)
    finally:
        os.unlink(path)


def test_refresh_commit_distance_propagates_fetch_failure():
    # A fetch failure must PROPAGATE so the --update-stamp caller writes nothing (fail-closed);
    # a partial/stale distance must never land.
    path = _write_results("abc123")
    try:
        raw = {
            "_meta": {
                "fork_upstreams": {
                    "sandbox": {
                        "repo": "kubernetes-sigs/agent-sandbox",
                        "branch": "main",
                        "results": path,
                    }
                }
            }
        }
        orig = vuf._fetch_commits_behind

        def _boom(repo, base, branch, token=None):
            raise vuf.urllib.error.URLError("network down")

        vuf._fetch_commits_behind = _boom
        raised = False
        try:
            vuf._refresh_commit_distance(raw)
        except vuf.urllib.error.URLError:
            raised = True
        finally:
            vuf._fetch_commits_behind = orig
        _check("fetch failure propagates", raised)
    finally:
        os.unlink(path)


def test_refresh_commit_distance_empty_when_no_fork_upstreams():
    # No fork_upstreams configured ⇒ empty dict, no network calls attempted.
    _check("empty meta -> {}", vuf._refresh_commit_distance({"_meta": {}}) == {})
    _check("absent meta -> {}", vuf._refresh_commit_distance({}) == {})


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print("ok — %d test function(s) passed" % len(tests))


if __name__ == "__main__":
    _run_all()
