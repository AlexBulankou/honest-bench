"""hb#166 contract guards for the WORK_IN_PROGRESS.md pending-cell surface.

Four load-bearing invariants, all fail-closed:

  - no-dangling-pending: every `pending_reason` present in any product's
    results/latest.json has a matching WORK_IN_PROGRESS.md anchor. A new reason
    that render would print `pending (<reason>)` for, with no WIP entry, is a hard
    CI failure — the honesty contract (every pending links somewhere) cannot lapse
    silently.
  - link-integrity: every `WORK_IN_PROGRESS.md#<anchor>` link the renderer emits
    into README.md / DETAILS.md resolves to an anchor that actually exists on the
    generated WIP page (no broken in-page jump).
  - no-unlinked-pending: no rendered table DATA cell carries a bare `pending` /
    `N/A` that is NOT wrapped in a link — the backstop that catches an emission
    site a future change forgets to route through link_pending().
  - public-safety: the WIP page carries no bare `#<n>` GitHub auto-link (which
    would resolve to a non-existent honest-bench issue). Internal tracking is
    referenced only as `a#<n>` prose (PRIVATE repo) or as real `hb#`/full-URL
    public links.

Runnable bare (`python3 render/test_wip_links.py`) and under pytest.
"""
import glob
import json
import os
import re

from generate import build_readme, build_details, _repo_root
from schema import PENDING_REASONS
from wip import WIP_CATALOG, build_work_in_progress, NA_BY_CONSTRUCTION, WORK_IN_PROGRESS_FILE


def _latest_json_paths():
    root = _repo_root()
    return sorted(glob.glob(os.path.join(root, "sandbox*", "results", "latest.json")))


def _pending_reasons_in_results():
    reasons = set()
    for path in _latest_json_paths():
        with open(path) as fh:
            data = json.load(fh)
        for sc in data.get("scenarios", []):
            if sc.get("outcome") == "pending" and sc.get("pending_reason"):
                reasons.add(sc["pending_reason"])
    return reasons


def _wip_anchors():
    """Anchor ids actually emitted on the generated WIP page (`<a id="...">`)."""
    return set(re.findall(r'<a id="([a-z0-9-]+)"></a>', build_work_in_progress()))


def test_catalog_covers_pending_enum():
    # The synthetic na-by-construction anchor is catalog-only (never an enum member);
    # every real enum member must be catalogued.
    missing = set(PENDING_REASONS) - set(WIP_CATALOG)
    assert not missing, f"pending reasons with no WIP catalog entry: {sorted(missing)}"
    extra = set(WIP_CATALOG) - set(PENDING_REASONS) - {NA_BY_CONSTRUCTION}
    assert not extra, f"WIP catalog entries not in the enum (nor na-by-construction): {sorted(extra)}"


def test_no_dangling_pending_reason():
    reasons = _pending_reasons_in_results()
    anchors = _wip_anchors()
    dangling = {r for r in reasons if r not in anchors}
    assert not dangling, (
        "pending_reason(s) present in results/latest.json with no WORK_IN_PROGRESS.md "
        f"anchor: {sorted(dangling)}. Add a WIP_CATALOG entry (render/wip.py) and regen."
    )


def test_rendered_wip_links_resolve():
    anchors = _wip_anchors()
    pat = re.compile(re.escape(WORK_IN_PROGRESS_FILE) + r"#([a-z0-9-]+)")
    broken = set()
    for page in (build_readme(), build_details()):
        for anchor in pat.findall(page):
            if anchor not in anchors:
                broken.add(anchor)
    assert not broken, (
        f"README/DETAILS link to WORK_IN_PROGRESS.md#<anchor> that does not exist: "
        f"{sorted(broken)}"
    )


# A markdown table DATA row: starts with `|`, is not the `|---|` separator. We only scan
# data rows (matrix, dual-cell, etc.) — prose paragraphs and legend bullets are exempt by
# design (hb#166 links DATA cells, never prose).
_SEP_RE = re.compile(r"^\|[\s:|-]+\|$")
# A bare pending/N/A token NOT already inside a markdown link. `pending` / `pending (x)` / `N/A`
# preceded by `[` is linked; anything else in a data cell is a dangling bare token.
_BARE_TOKEN_RE = re.compile(r"(?<!\[)\b(pending)\b(?!\]| \([a-z0-9-]+\)\])|(?<!\[)N/A(?!\])")


def _unlinked_pending_cells(page):
    bad = []
    for line in page.splitlines():
        if not line.startswith("|") or _SEP_RE.match(line):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        for cell in cells:
            # strip out fully-linked tokens, then look for any residual bare pending/N/A
            residual = re.sub(r"\[[^\]]*\]\(" + re.escape(WORK_IN_PROGRESS_FILE) + r"#[a-z0-9-]+\)", "", cell)
            if re.search(r"\bpending\b", residual) or re.search(r"\bN/A\b", residual):
                bad.append(cell)
    return bad


def test_no_unlinked_pending_data_cells():
    bad = _unlinked_pending_cells(build_readme()) + _unlinked_pending_cells(build_details())
    assert not bad, (
        "rendered table DATA cell(s) carry a bare, UNLINKED pending/N/A token — route the "
        f"emission site through link_pending()/wip_link() (render/wip.py):\n  " + "\n  ".join(bad)
    )


def test_wip_page_has_no_bare_github_autolink():
    # Public-safety (hb#166 + a4z1 amendment): honest-bench is PUBLIC; a bare `#<n>` GitHub
    # auto-links to a non-existent honest-bench issue. Internal tracking is `a#<n>` prose;
    # public tracking is `hb#` or a full github.com URL. So no bare `#<digits>` may appear
    # except immediately after `hb` or `a` (i.e. hb#132 / a#3097).
    page = build_work_in_progress()
    bare = re.findall(r"(?<![A-Za-z0-9])#\d+", page)
    assert not bare, (
        f"WORK_IN_PROGRESS.md contains bare `#<n>` GitHub auto-link(s): {bare}. Use `a#<n>` "
        "(internal, private repo) or `hb#<n>` / a full URL (public) instead."
    )


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok: {fn.__name__}")
    print(f"test_wip_links: all {len(fns)} assertions passed")


if __name__ == "__main__":
    _run_all()
