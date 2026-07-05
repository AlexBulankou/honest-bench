"""Standing-docs public-safety guard: no internal resource names in committed markdown.

The render-output tests (test_render.py) already assert the generated page never
carries internal cluster names / project ids — but the hand-maintained standing
docs (UPSTREAM_BLOCKERS*.md, WORK_IN_PROGRESS.md, ...) had no equivalent guard,
and scripts/check-public-safety.sh deliberately holds only GENERIC structural
patterns (per its header, specific names are the our-side 2b gate's job). That
gap let an internal dev-cluster name sit in UPSTREAM_BLOCKERS_DETAIL.md prose
(caught in PR #204 review). This module closes it for the tracked *.md set,
reusing the same forbidden-token list test_render.py already ships — so it adds
no new string surface to the public repo.

Stdlib-only + self-running via the __main__ guard, matching the repo's test
convention (the CI unit-tests gate runs each module with `python3 <file>`).
"""

import os
import subprocess

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Same token set as test_render.py's render-output guards. Bare names only —
# no descriptions — so this file stays as low-signal as the existing test list.
FORBIDDEN = (
    "sandbox-scenarios-cluster",
    "substrate-demo-cluster",
    "alexbu-gke-dev-d",
    "postgres-obs-0",
    "googleplex",
)


def _tracked_markdown():
    out = subprocess.run(
        ["git", "ls-files", "*.md"],
        cwd=_REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout
    files = [ln for ln in out.splitlines() if ln.strip()]
    assert files, "no tracked .md files found — discovery broken (run inside the repo)"
    return files


def test_tracked_markdown_has_no_internal_resource_names():
    hits = []
    for rel in _tracked_markdown():
        path = os.path.join(_REPO_ROOT, rel)
        with open(path, encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                for tok in FORBIDDEN:
                    if tok in line:
                        hits.append(f"{rel}:{lineno}: {tok}")
    assert not hits, "internal resource names in public docs:\n" + "\n".join(hits)


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
