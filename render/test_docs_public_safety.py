"""Standing-docs public-safety guard: no internal resource names in committed markdown.

The render-output tests (test_render.py) already assert the generated page never
carries internal cluster names / project ids — but the hand-maintained standing
docs (UPSTREAM_BLOCKERS*.md, WORK_IN_PROGRESS.md, ...) had no equivalent guard,
and scripts/check-public-safety.sh deliberately holds only GENERIC structural
patterns (per its header, specific names are the our-side 2b gate's job). That
gap let an internal dev-cluster name sit in UPSTREAM_BLOCKERS_DETAIL.md prose
(caught in PR #204 review). This module closed it for the tracked *.md set —
originally via a hardcoded forbidden-name tuple, but holding those literals in
this PUBLIC repo was itself the leak the guard existed to prevent (same finding
as test_render.py's public-safety fences, PR #327). This module now delegates
to check-public-safety.sh (structural patterns only, live today) and holds no
real-name literal; the specific-name gap it leaves is closed by the forthcoming
Secret-Manager-backed tree-wide specific-name scan (additive, Cloud Build).

Stdlib-only + self-running via the __main__ guard, matching the repo's test
convention (the CI unit-tests gate runs each module with `python3 <file>`).
"""

import os
import subprocess

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _tracked_markdown():
    out = subprocess.run(
        ["git", "ls-files", "*.md"],
        cwd=_REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout
    files = [ln for ln in out.splitlines() if ln.strip()]
    assert files, "no tracked .md files found — discovery broken (run inside the repo)"
    return files


def test_tracked_markdown_passes_generic_safety_scan():
    # No real-name literal lives here: this shells out to the existing structural
    # scanner (internal shortlinks, internal hosts, DSNs, OAuth tokens, private
    # keys, emails) rather than holding a denylist in this public file. Specific
    # internal resource names are the forthcoming Cloud Build tree-wide scan's job.
    files = _tracked_markdown()
    result = subprocess.run(
        ["bash", os.path.join(_REPO_ROOT, "scripts", "check-public-safety.sh"), *files],
        cwd=_REPO_ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        "check-public-safety.sh flagged committed markdown:\n" + result.stdout + result.stderr
    )


def test_discovery_covers_the_blockers_docs():
    # The guard is only as good as its file set — assert the docs that motivated
    # it are actually in scope, so a future move/rename can't silently drop them.
    files = set(_tracked_markdown())
    for must in ("UPSTREAM_BLOCKERS.md", "UPSTREAM_BLOCKERS_DETAIL.md", "README.md"):
        assert must in files, f"{must} not in tracked-markdown scan set"


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok: {fn.__name__}")
    print(f"test_docs_public_safety: all {len(fns)} tests passed")


if __name__ == "__main__":
    _run_all()
