"""DETAILS.md#<anchor> link-integrity guard (PR #570 follow-up, epic #6669 WS1).

DETAILS.md anchors are implicit GitHub heading-slugs (`## Foo Bar` -> `#foo-bar`), never
explicit `<a id>` tags (unlike WORK_IN_PROGRESS.md, guarded separately in
test_wip_links.py). Nothing previously asserted that a `DETAILS.md#<anchor>` link emitted
by README.md (e.g. the Known-anomalies table's 6 links, epic #6669 WS1) actually resolves
to a heading DETAILS.md renders — a renamed/removed heading would silently 404 in
production with no CI signal.

Runnable bare (`python3 render/test_details_links.py`) and under pytest.
"""
import re
from collections import Counter

from generate import build_details, build_readme

_DETAILS_LINK_RE = re.compile(r"\bDETAILS\.md#([a-z0-9-]+)\)")
_HEADING_RE = re.compile(r"^(#{2,6})\s+(.+?)\s*$", re.MULTILINE)


def _github_slug(heading_text):
    # Mirrors GitHub's own heading-anchor algorithm closely enough for this repo's headings
    # (plain ASCII prose, no inline code/links inside headings): strip markdown emphasis
    # markers, lowercase, drop anything not alnum/space/hyphen/underscore, spaces -> hyphens.
    text = re.sub(r"[`*_]", "", heading_text)
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9 _-]", "", text)
    text = re.sub(r"\s+", "-", text)
    return text


def _details_anchors():
    """Anchor ids GitHub would assign to every heading DETAILS.md actually renders.

    Duplicate heading text is disambiguated with GitHub's own `-1`, `-2`, ... suffixing.
    """
    counts = Counter()
    anchors = set()
    for _, text in _HEADING_RE.findall(build_details()):
        base = _github_slug(text)
        n = counts[base]
        counts[base] += 1
        anchors.add(base if n == 0 else f"{base}-{n}")
    return anchors


def test_details_links_resolve_to_real_headings():
    anchors = _details_anchors()
    broken = set()
    for page in (build_readme(), build_details()):
        for anchor in _DETAILS_LINK_RE.findall(page):
            if anchor not in anchors:
                broken.add(anchor)
    assert not broken, (
        f"README/DETAILS link(s) to DETAILS.md#<anchor> with no matching heading: "
        f"{sorted(broken)}"
    )


def test_details_has_no_duplicate_anchor_collision():
    # A silent duplicate-heading collision would make `_details_anchors()` itself paper over
    # a real ambiguity (two headings resolving to the same GitHub anchor, only one reachable
    # by a bare `#anchor` link) — fail loud rather than let the resolver mask it.
    counts = Counter()
    for _, text in _HEADING_RE.findall(build_details()):
        counts[_github_slug(text)] += 1
    dupes = {k: v for k, v in counts.items() if v > 1}
    assert not dupes, f"DETAILS.md headings collide on the same GitHub anchor: {dupes}"


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok: {fn.__name__}")
    print(f"test_details_links: all {len(fns)} assertions passed")


if __name__ == "__main__":
    _run_all()
