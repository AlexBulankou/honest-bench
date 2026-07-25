"""Offline tests for check-upstream-commit-window.py (hb#471 follow-up).

Covers the correctness-critical PURE logic only — window extraction/validation
from results JSON, chronological ordering, and commit formatting. These make NO
network calls, so they are safe in the CI unit-test gate; the live-fetch paths
(`_fetch_commits`, `_fetch_commits_between_refs`) are deliberately not exercised
here (operator/agent-invoked, network-dependent, out of the offline gate's scope
by design — same posture as test_verify_upstream_freshness.py).

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
_SCRIPT = os.path.join(_HERE, "check-upstream-commit-window.py")

_spec = importlib.util.spec_from_file_location("check_upstream_commit_window", _SCRIPT)
cucw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cucw)


def _check(name, cond):
    if not cond:
        raise AssertionError("FAIL: %s" % name)


def test_extract_window_basic():
    results = {"generated_at": "2026-07-25T07:42:40Z",
               "provenance": {"controller_digest": "sha256:aaa"}}
    at, digest = cucw._extract_window(results, "label")
    _check("generated_at extracted", at == "2026-07-25T07:42:40Z")
    _check("digest extracted", digest == "sha256:aaa")


def test_extract_window_missing_digest_ok():
    results = {"generated_at": "2026-07-25T07:42:40Z"}
    at, digest = cucw._extract_window(results, "label")
    _check("generated_at extracted without provenance", at == "2026-07-25T07:42:40Z")
    _check("missing digest degrades to None", digest is None)


def test_extract_window_missing_generated_at_raises():
    threw = False
    try:
        cucw._extract_window({}, "label")
    except ValueError:
        threw = True
    _check("missing generated_at raises ValueError", threw)


def test_extract_window_not_a_dict_raises():
    threw = False
    try:
        cucw._extract_window(["not", "a", "dict"], "label")
    except ValueError:
        threw = True
    _check("non-dict results raises ValueError", threw)


def _write_json(tmpdir, name, obj):
    path = os.path.join(tmpdir, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    return path


def test_window_from_results_orders_chronologically():
    # pass the LATER fire as "old" and the EARLIER fire as "new" — the function
    # must still return (earlier, later) by generated_at, not by arg order.
    with tempfile.TemporaryDirectory() as tmpdir:
        later = _write_json(tmpdir, "later.json", {
            "generated_at": "2026-07-25T23:15:33Z",
            "provenance": {"controller_digest": "sha256:new"},
        })
        earlier = _write_json(tmpdir, "earlier.json", {
            "generated_at": "2026-07-25T07:42:40Z",
            "provenance": {"controller_digest": "sha256:old"},
        })
        since, until, old_digest, new_digest = cucw._window_from_results(later, earlier)
        _check("since is the earlier timestamp", since == "2026-07-25T07:42:40Z")
        _check("until is the later timestamp", until == "2026-07-25T23:15:33Z")
        _check("old_digest tracks the earlier fire", old_digest == "sha256:old")
        _check("new_digest tracks the later fire", new_digest == "sha256:new")


def test_window_from_results_missing_field_raises():
    with tempfile.TemporaryDirectory() as tmpdir:
        good = _write_json(tmpdir, "good.json", {"generated_at": "2026-07-25T07:42:40Z"})
        bad = _write_json(tmpdir, "bad.json", {"no_generated_at": True})
        threw = False
        try:
            cucw._window_from_results(good, bad)
        except ValueError:
            threw = True
        _check("missing generated_at in either file raises", threw)


def test_format_commit():
    raw = {
        "sha": "b13f6c01b4ab1234567890abcdef1234567890ab",
        "commit": {
            "author": {"date": "2026-07-25T15:17:51Z"},
            "message": "fix: use Patch for status writes\n\nlonger body here",
        },
        "html_url": "https://github.com/kubernetes-sigs/agent-sandbox/commit/b13f6c0",
    }
    out = cucw._format_commit(raw)
    _check("sha truncated to 12", out["sha"] == "b13f6c01b4ab")
    _check("date preserved", out["date"] == "2026-07-25T15:17:51Z")
    _check("message is first line only", out["message"] == "fix: use Patch for status writes")
    _check("url preserved", out["url"] == raw["html_url"])


def test_format_commit_missing_fields_degrades_gracefully():
    out = cucw._format_commit({})
    _check("empty sha", out["sha"] == "")
    _check("unknown date", out["date"] == "?")
    _check("empty message", out["message"] == "")
    _check("empty url", out["url"] == "")


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print("ok — %d test function(s) passed" % len(tests))


if __name__ == "__main__":
    _run_all()
