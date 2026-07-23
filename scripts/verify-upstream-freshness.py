#!/usr/bin/env python3
"""verify-upstream-freshness.py — live cross-check of the upstream-blocker refs (hb#181).

`render/upstream_links.json` mirrors the hand-maintained UPSTREAM_BLOCKERS.md and
declares, per pending-reason class, the upstream issue/PR refs that gate each
honest-empty cell — plus a hand-stamped `_meta.last_verified` date. The stamp is
the honesty contract for the blockers surface: it asserts "every ref's declared
open/in-review/merged/closed state was checked against reality on this date." Until
now that check was done by eye. A blocker surface that silently goes stale — a ref
that says "in review" long after its PR merged, or "open" after an issue closed —
misleads every reader about whether a gate has actually cleared. That is the classic
degrade-quietly failure a trust surface must never do.

This tool mechanizes the check. For every ref in the JSON it fetches the live state
from the PUBLIC GitHub REST API and compares:

  - kind (issue vs pr) against `pull_request` presence, and
  - declared status against the live (state, is-PR, merged) shape:
      open      <-> live state == open
      in-review <-> live open PR
      merged    <-> live closed PR with merged_at set
      closed    <-> live closed (issue closed, or PR closed-not-merged)

All refs point at public upstream OSS repos, so the API is queried
**unauthenticated** — no credential, no per-org app install, nothing fleet-side is
required or leaked. A GITHUB_TOKEN in the environment is used only to raise the
anonymous rate limit if one happens to be present; it is never required.

Exit codes (fail-closed — a verifier that cannot verify must never report "fresh"):
  0  every ref matches live state (surface is fresh)
  1  at least one ref drifted (declared state != live state)
  2  a ref could not be fetched (network / rate-limit / API error) — UNKNOWN, not fresh

Usage:
  verify-upstream-freshness.py                 # human report, exit per above
  verify-upstream-freshness.py --json          # machine-readable report on stdout
  verify-upstream-freshness.py --update-stamp  # on a fully-fresh run, bump
                                               #   _meta.last_verified to today (UTC)

stdlib-only + self-contained, matching the repo's test/script convention (CI runs
modules with bare `python3 <file>`; requests/pytest are intentionally absent from
harness/requirements.txt). This script is NOT wired into the CI unit-test gate: it
makes live network calls, which would make that gate flaky and externally
dependent. It is an operator-invoked freshness sweep — the deterministic
replacement for eyeballing before re-stamping `last_verified`.
"""

import argparse
import datetime
import json
import os
import sys
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
_RENDER_DIR = os.path.join(_REPO_ROOT, "render")
_LINKS_PATH = os.path.join(_RENDER_DIR, "upstream_links.json")

# Reuse the render module's fail-loud loader + public-repo allow-list rather than
# re-parsing the JSON, so schema drift is caught in exactly one place.
sys.path.insert(0, _RENDER_DIR)
import upstream_links  # noqa: E402


def _fetch_live(repo, number, token=None):
    """Return (state, is_pr, merged) for repo#number, or raise on any failure.

    The issues endpoint serves both issues and PRs; a PR carries a
    `pull_request` object, and `pull_request.merged_at` is set once merged.
    """
    url = "https://api.github.com/repos/%s/issues/%d" % (repo, number)
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "honest-bench-upstream-freshness",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = "Bearer %s" % token
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.load(resp)
    pr = data.get("pull_request")
    is_pr = pr is not None
    merged = bool(pr.get("merged_at")) if is_pr else False
    return data["state"], is_pr, merged


def _expected_ok(declared_kind, declared_status, state, is_pr, merged):
    """True iff the declared (kind, status) matches the live (state, is_pr, merged)."""
    # kind must agree first — a declared issue that is live a PR (or vice-versa)
    # is itself drift, independent of open/closed.
    kind_ok = (declared_kind == "pr") == is_pr
    if not kind_ok:
        return False
    if declared_status == "open":
        return state == "open"
    if declared_status == "in-review":
        return state == "open" and is_pr
    if declared_status == "merged":
        return state == "closed" and is_pr and merged
    if declared_status == "closed":
        # issue closed, or PR closed-without-merge
        return state == "closed" and not (is_pr and merged)
    return False


def _live_desc(state, is_pr, merged):
    return "%s (%s%s)" % (state, "PR" if is_pr else "issue", ", merged" if merged else "")


def _iter_unique_refs(classes):
    """Yield (ref, [classes...]) once per unique (repo, number), preserving JSON order."""
    order = []
    index = {}
    for cls, entry in classes.items():
        for ref in entry["refs"]:
            key = (ref["repo"], ref["number"])
            if key not in index:
                index[key] = {"ref": ref, "classes": []}
                order.append(key)
            index[key]["classes"].append(cls)
    for key in order:
        yield index[key]["ref"], index[key]["classes"]


def main(argv=None):
    ap = argparse.ArgumentParser(description="Cross-check upstream_links.json refs against live GitHub.")
    ap.add_argument("--json", action="store_true", help="emit a machine-readable JSON report")
    ap.add_argument(
        "--update-stamp",
        action="store_true",
        help="on a fully-fresh run, bump _meta.last_verified to today (UTC)",
    )
    args = ap.parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN") or None
    classes = upstream_links.CLASSES
    stamp = upstream_links.META.get("last_verified")

    results = []
    drift = False
    unknown = False
    for ref, ref_classes in _iter_unique_refs(classes):
        entry = {
            "repo": ref["repo"],
            "number": ref["number"],
            "kind": ref["kind"],
            "declared_status": ref["status"],
            "classes": ref_classes,
            "url": upstream_links.ref_url(ref),
        }
        try:
            state, is_pr, merged = _fetch_live(ref["repo"], ref["number"], token=token)
        except (urllib.error.URLError, urllib.error.HTTPError, KeyError, ValueError, TimeoutError) as e:
            entry["result"] = "UNKNOWN"
            entry["error"] = "%s: %s" % (type(e).__name__, e)
            unknown = True
            results.append(entry)
            continue
        ok = _expected_ok(ref["kind"], ref["status"], state, is_pr, merged)
        entry["live"] = _live_desc(state, is_pr, merged)
        entry["result"] = "OK" if ok else "DRIFT"
        if not ok:
            drift = True
        results.append(entry)

    if drift:
        code = 1
    elif unknown:
        code = 2
    else:
        code = 0

    if args.json:
        print(json.dumps({"last_verified": stamp, "exit": code, "refs": results}, indent=2))
    else:
        print("upstream_links.json — last_verified: %s" % stamp)
        print("checked %d unique ref(s) against live GitHub (unauthenticated)\n"
              % len(results))
        for e in results:
            if e["result"] == "UNKNOWN":
                print("  [%s] #%d  declared=%-10s  live=UNKNOWN (%s)  *** COULD NOT FETCH ***"
                      % (",".join(e["classes"]), e["number"], e["declared_status"], e["error"]))
            else:
                flag = "OK" if e["result"] == "OK" else "*** DRIFT ***"
                print("  [%s] #%d  declared=%-10s  live=%-22s  %s"
                      % (",".join(e["classes"]), e["number"], e["declared_status"], e["live"], flag))
        print()
        if code == 0:
            print("RESULT: FRESH — every declared ref matches live state.")
        elif code == 1:
            print("RESULT: DRIFT — update upstream_links.json (and re-render) to match live state.")
        else:
            print("RESULT: UNKNOWN — at least one ref could not be fetched; NOT certified fresh.")

    if args.update_stamp:
        if code != 0:
            print("\n--update-stamp refused: run is not fully fresh (exit=%d)." % code, file=sys.stderr)
            return code
        today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        with open(_LINKS_PATH, encoding="utf-8") as f:
            raw = json.load(f)
        prev = raw.get("_meta", {}).get("last_verified")
        raw.setdefault("_meta", {})["last_verified"] = today
        with open(_LINKS_PATH, "w", encoding="utf-8") as f:
            json.dump(raw, f, indent=2, ensure_ascii=False)
            f.write("\n")
        print("\nlast_verified: %s -> %s (stamped)" % (prev, today))

    return code


if __name__ == "__main__":
    sys.exit(main())
