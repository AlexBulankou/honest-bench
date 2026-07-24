"""hb#181: upstream_links mapping + formatter tests.

Covers the JSON loader's fail-loud validation, the URL rule (issue vs PR path),
and both formatters (compact cell suffix / fuller prose), including the
unmapped-class -> "" contract that keeps non-upstream pending cells unchanged.

Stdlib-only + self-running via the __main__ guard, matching the repo's test
convention (the CI unit-tests gate runs each module with `python3 <file>`, so a
pytest-native module would fail to import — pytest is intentionally NOT in
harness/requirements.txt).
"""

import json
import tempfile

import upstream_links
from upstream_links import (
    CLASSES,
    ref_url,
    upstream_cell_refs,
    upstream_prose_refs,
)


# --- test helpers (stdlib replacements for pytest fixtures) -------------------


def _assert_raises_assertion(fn):
    """Assert that calling fn() raises AssertionError (pytest.raises stand-in)."""
    try:
        fn()
    except AssertionError:
        return
    raise AssertionError(f"expected AssertionError from {fn!r}, none raised")


def _load_data(data):
    # run the REAL loader against a synthesized JSON file — no logic duplication.
    # Manually redirect _LINKS_PATH and restore it (monkeypatch stand-in).
    original = upstream_links._LINKS_PATH
    with tempfile.TemporaryDirectory() as d:
        p = f"{d}/upstream_links.json"
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        upstream_links._LINKS_PATH = p
        try:
            return upstream_links._load()
        finally:
            upstream_links._LINKS_PATH = original


# --- shipped-data invariants -------------------------------------------------


def test_required_class_present_with_issue_and_fix_pr():
    # the one class the matrix consumes today must exist, and must carry the
    # blocking issue first so the rendered arrow reads issue -> fix.
    refs = CLASSES["upstream-blocked"]["refs"]
    assert refs[0]["kind"] == "issue" and refs[0]["role"] == "blocks"
    assert any(r["role"] == "fix-in-flight" for r in refs)


def test_fix_partial_merged_role_loads_and_renders_as_fix():
    # regression for the fix-partial-merged role (dd63bb1/#1114 on trust-gate):
    # the role was added to the shipped JSON but omitted from _ROLES, crashing
    # the loader at import and breaking the whole render/freshness pipeline.
    # It is a "fix" role, so both formatters must show its status + fix affordance.
    assert "fix-partial-merged" in upstream_links._ROLES
    assert "fix-partial-merged" in upstream_links._FIX_ROLES
    ref = {
        "repo": "kubernetes-sigs/agent-sandbox",
        "number": 1114,
        "kind": "pr",
        "role": "fix-partial-merged",
        "status": "merged",
    }
    assert upstream_links._cell_token(ref) == (
        "[#1114 merged](https://github.com/kubernetes-sigs/agent-sandbox/pull/1114)"
    )
    assert upstream_links._prose_token(ref) == (
        "fix [agent-sandbox#1114](https://github.com/kubernetes-sigs/agent-sandbox/pull/1114)"
        " (PR, merged)"
    )
    # and the loader accepts it end-to-end
    data = {"classes": _required_classes(
        **{"trust-gate": {"refs": [_valid_ref(), ref]}}
    )}
    loaded = _load_data(data)
    assert loaded["classes"]["trust-gate"]["refs"][1]["role"] == "fix-partial-merged"


def test_all_shipped_refs_point_at_public_oss_repos():
    # public-safety: every ref in the shipped mapping is a public upstream OSS
    # repo — never an internal tracker (a#NNNN prose is wip.py's lane, not here).
    # The loader now enforces the same set at load time (_PUBLIC_REPOS); this
    # asserts the shipped data AND that the two allow-lists can't drift.
    allowed = {"kubernetes-sigs/agent-sandbox", "agent-substrate/substrate"}
    assert upstream_links._PUBLIC_REPOS == allowed
    for cls in CLASSES.values():
        for ref in cls["refs"]:
            assert ref["repo"] in allowed, ref


# --- URL rule ----------------------------------------------------------------


def test_ref_url_issue_vs_pr_paths():
    issue = {"repo": "kubernetes-sigs/agent-sandbox", "number": 873, "kind": "issue"}
    pr = {"repo": "kubernetes-sigs/agent-sandbox", "number": 1150, "kind": "pr"}
    assert ref_url(issue) == "https://github.com/kubernetes-sigs/agent-sandbox/issues/873"
    assert ref_url(pr) == "https://github.com/kubernetes-sigs/agent-sandbox/pull/1150"


# --- cell formatter ----------------------------------------------------------


def test_cell_refs_upstream_blocked_exact():
    # the exact suffix appended after the link_pending-wrapped token: leading
    # space, arrow-joined, fix ref labeled with its live status.
    assert upstream_cell_refs("upstream-blocked") == (
        " [#873](https://github.com/kubernetes-sigs/agent-sandbox/issues/873)"
        "→[#1150 in review](https://github.com/kubernetes-sigs/agent-sandbox/pull/1150)"
    )


def test_cell_refs_unmapped_and_none_are_empty():
    # non-upstream pending classes render unchanged — "" for anything unmapped.
    assert upstream_cell_refs("cluster-fire") == ""
    assert upstream_cell_refs("not-yet-measured") == ""
    assert upstream_cell_refs(None) == ""
    assert upstream_cell_refs("") == ""


# --- prose formatter ---------------------------------------------------------


def test_prose_refs_upstream_blocked_exact():
    assert upstream_prose_refs("upstream-blocked") == (
        "[agent-sandbox#873](https://github.com/kubernetes-sigs/agent-sandbox/issues/873)"
        " (issue, open)"
        " → fix [agent-sandbox#1150](https://github.com/kubernetes-sigs/agent-sandbox/pull/1150)"
        " (PR, in review)"
    )


def test_prose_refs_unmapped_is_empty():
    assert upstream_prose_refs("no-such-class") == ""
    assert upstream_prose_refs(None) == ""


def test_prose_refs_single_ref_class_has_no_arrow():
    # snapshot-restore-verify carries only the design issue (no fix PR yet).
    prose = upstream_prose_refs("snapshot-restore-verify")
    assert "→" not in prose
    assert prose == (
        "[agent-sandbox#952](https://github.com/kubernetes-sigs/agent-sandbox/issues/952)"
        " (issue, open)"
    )


# --- loader validation (fail-loud at import) ----------------------------------


def _valid_ref():
    return {
        "repo": "kubernetes-sigs/agent-sandbox",
        "number": 873,
        "kind": "issue",
        "role": "blocks",
        "status": "open",
    }


def _required_classes(**overrides):
    """A minimal classes dict carrying every _REQUIRED_CLASSES member, so a
    synthesized mapping fails on the property under test — never on the
    missing-required-class check (which the loader runs first)."""
    classes = {
        cls: {"refs": [_valid_ref()]}
        for cls in sorted(upstream_links._REQUIRED_CLASSES)
    }
    classes.update(overrides)
    return classes


def test_loader_accepts_minimal_valid_mapping():
    data = {"_meta": {}, "classes": _required_classes()}
    loaded = _load_data(data)
    assert loaded["classes"]["upstream-blocked"]["refs"][0]["number"] == 873


def test_loader_rejects_bad_ref_fields():
    mutations = [
        lambda r: r.update(kind="discussion"),
        lambda r: r.update(role="mentions"),
        lambda r: r.update(status="draft"),
        lambda r: r.update(repo="not-a-repo"),
        lambda r: r.update(repo="a/b/c"),
        # well-formed owner/repo but NOT on the public-OSS allow-list — the
        # loader-side gate (hb#182 follow-up), not just the shipped-data test.
        lambda r: r.update(repo="some-org/private-tracker"),
        lambda r: r.update(number="873"),
    ]
    for mutate in mutations:
        ref = _valid_ref()
        mutate(ref)
        _assert_raises_assertion(
            lambda ref=ref: _load_data(
                {"classes": _required_classes(**{"upstream-blocked": {"refs": [ref]}})},
            )
        )


def test_loader_rejects_missing_required_class_and_empty_refs():
    # each required class missing individually trips the missing-required check
    for cls in sorted(upstream_links._REQUIRED_CLASSES):
        classes = _required_classes()
        del classes[cls]
        _assert_raises_assertion(lambda classes=classes: _load_data({"classes": classes}))
    _assert_raises_assertion(
        lambda: _load_data(
            {"classes": _required_classes(**{"upstream-blocked": {"refs": []}})},
        )
    )
    _assert_raises_assertion(lambda: _load_data({"classes": {}}))


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok: {fn.__name__}")
    print(f"test_upstream_links: all {len(fns)} tests passed")


if __name__ == "__main__":
    _run_all()
