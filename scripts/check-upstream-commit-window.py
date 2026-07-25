#!/usr/bin/env python3
"""check-upstream-commit-window.py — surface candidate upstream commits for a
fire-to-fire regression window (hb#471 follow-up).

honest-bench measures against an ephemeral per-fire cluster (#3868) that installs
the upstream `agent-sandbox-controller:latest-main` FRESH every fire
(`recipe/install-controller-from-main.sh`) — a floating tag, not a pin. So when a
metric regresses between two fires, there are (at least) two live confounds
riding along for free, independent of whatever the harness itself measured:

  1. the GKE node image can float underneath the fire (RAPID channel,
     `min_master_version` unpinned — see `provenance.node_image`), and
  2. the upstream controller CODE can have moved underneath the fire too (same
     git ref `main`, different content — see `provenance.controller_digest`).

Before crediting a regression to either confound (or ruling one out, the way a
regression write-up should), the honest step is to ask "did anything actually
land upstream in this exact window?" — a same-tag-different-content rebuild is
silent by construction; nothing in `latest.json` says what changed. Doing this by
hand (hb#471: manually diffing two fires' `generated_at` timestamps, then
`curl`-ing the public GitHub commits API for that window) is a repeatable
diagnostic, not a one-off — so this script mechanizes exactly that lookup for the
next regression triage.

This tool does NOT decide real-vs-artifact. It surfaces the raw candidate list —
every upstream commit that landed in the window — so a human/agent reviewer can
apply the judgment call themselves (e.g. reading each candidate PR's own stated
measured impact, as hb#471's review did to rule out asbx#1254 by its effect being
an improvement, not a regression). Silence (an empty window) is itself a useful,
honest signal: it means neither fire's regression can be attributed to upstream
code movement, strengthening a node-image (or harness-side) explanation by
elimination.

Two distinct clusters need two distinct correlation MODES, since neither always
has both a timestamp window and two comparable SHAs:

  - **Timestamp-window mode** (`--since/--until` or `--old-results/--new-results`)
    — the ephemeral ate-sandbox regime, where each fire pulls a floating tag
    (`:latest-main`) fresh, so there is no stable "old SHA" to name going in —
    only the two fires' `generated_at` timestamps bound the search.
  - **Ref-window mode** (`--old-ref/--new-ref`) — the persistent-cluster regime,
    where a per-fire record pins the upstream SHA it ran against directly (e.g.
    substrate's `substrate_sha`/`upstream_head` in an e2e-history store), so the
    window is exactly `old_ref..new_ref` via GitHub's compare API — no date math,
    no clock-skew risk between the harness host and GitHub's server clock.

Both modes emit the same candidate-commit shape, so a caller in either regime
gets the same triage output.

Public upstream repos only, so this queries the GitHub API **unauthenticated** —
no credential, no per-org app install, nothing fleet-side required or leaked
(same posture as verify-upstream-freshness.py). A GITHUB_TOKEN in the environment
is used only to raise the anonymous rate limit if one happens to be present.

Usage:
  # explicit window
  check-upstream-commit-window.py --repo kubernetes-sigs/agent-sandbox \\
      --since 2026-07-25T07:42:40Z --until 2026-07-25T23:15:33Z

  # derived from two results snapshots (e.g. two checked-out latest.json revisions)
  check-upstream-commit-window.py --repo kubernetes-sigs/agent-sandbox \\
      --old-results /tmp/old-latest.json --new-results /tmp/new-latest.json

  # ref-window mode (persistent-cluster regime — two pinned upstream SHAs)
  check-upstream-commit-window.py --repo <owner/name> \\
      --old-ref <sha-or-branch> --new-ref <sha-or-branch>

  # machine-readable
  check-upstream-commit-window.py --repo <owner/name> --since ... --until ... --json

Exit codes (fail-closed — a verifier that cannot fetch must never report "clean"):
  0  fetched successfully (candidate list may be empty — that is a valid, honest
     result: no upstream commit landed in the window)
  1  bad usage (missing/conflicting window args, unparseable results files)
  2  the commits could not be fetched (network / rate-limit / API error) — UNKNOWN,
     not "no candidates"

stdlib-only + self-contained, matching the repo's script/test convention (CI runs
modules with bare `python3 <file>`; requests is intentionally absent from
harness/requirements.txt). Like verify-upstream-freshness.py, this is NOT wired
into the CI unit-test gate — it makes live network calls and is an
operator/agent-invoked triage tool, not a build-blocking check.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def _extract_window(results, label):
    """Pull (generated_at, controller_digest) out of a loaded results dict.

    `results` is shaped like `<product>/results/latest.json`: `generated_at` at
    the top level, `controller_digest` nested under `provenance`. Both are
    optional in the schema (best-effort provenance capture), so a missing
    `generated_at` is a hard usage error (there is no window without it) while a
    missing `controller_digest` degrades to `None` (still useful — the window
    lookup does not need it, only the digest-changed annotation does).
    """
    if not isinstance(results, dict):
        raise ValueError("%s: not a JSON object" % label)
    generated_at = results.get("generated_at")
    if not isinstance(generated_at, str) or not generated_at:
        raise ValueError("%s: missing/invalid 'generated_at'" % label)
    prov = results.get("provenance")
    digest = prov.get("controller_digest") if isinstance(prov, dict) else None
    return generated_at, digest


def _window_from_results(old_path, new_path):
    """Return (since, until, old_digest, new_digest) from two results JSON files.

    Ordering is by `generated_at`, not by which file was passed as old/new — a
    reviewer diffing two fires may not know upfront which is chronologically
    earlier, so this sorts rather than trusting arg order.
    """
    with open(old_path, encoding="utf-8") as f:
        old = json.load(f)
    with open(new_path, encoding="utf-8") as f:
        new = json.load(f)
    old_at, old_digest = _extract_window(old, old_path)
    new_at, new_digest = _extract_window(new, new_path)
    if new_at < old_at:
        old_at, new_at = new_at, old_at
        old_digest, new_digest = new_digest, old_digest
    return old_at, new_at, old_digest, new_digest


def _fetch_commits_between_refs(repo, old_ref, new_ref, token=None):
    """Return the list of commit JSON objects strictly between two refs via
    GitHub's compare API (`old_ref...new_ref`), paginating same as `_fetch_commits`.

    Unlike the timestamp-window endpoint, `compare` names the two endpoints
    directly — no clock-skew risk, and it works even when `old_ref`/`new_ref`
    are branch names rather than SHAs. Raises on any fetch failure, same
    contract as `_fetch_commits`.

    Caveat (PR #472 review): GitHub's compare API caps at 250 commits
    total regardless of pagination — a window wider than that silently
    truncates. Fine for any realistic fire-to-fire window; would need a
    different endpoint (or chunked compares) for a >250-commit range."""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "honest-bench-upstream-commit-window",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = "Bearer %s" % token
    commits = []
    page = 1
    while True:
        url = (
            "https://api.github.com/repos/%s/compare/%s...%s"
            "?per_page=100&page=%d" % (repo, old_ref, new_ref, page)
        )
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.load(resp)
        batch = data.get("commits") or []
        if not batch:
            break
        commits.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return commits


def _fetch_commits(repo, since, until, token=None):
    """Return the list of commit JSON objects on `repo`'s default branch in
    (since, until], paginating the public commits endpoint. Raises on any
    fetch failure — the caller distinguishes "fetched, zero results" (a valid
    empty candidate list) from "could not fetch" (UNKNOWN).

    Caveat (PR #472 review): this endpoint is default-branch-only —
    correct for our `main`-tracking regime, but a commit merged to a
    non-default branch in the window won't surface here."""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "honest-bench-upstream-commit-window",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = "Bearer %s" % token
    commits = []
    page = 1
    while True:
        url = (
            "https://api.github.com/repos/%s/commits"
            "?since=%s&until=%s&per_page=100&page=%d" % (repo, since, until, page)
        )
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=20) as resp:
            batch = json.load(resp)
        if not isinstance(batch, list) or not batch:
            break
        commits.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return commits


def _format_commit(c):
    sha = c.get("sha", "")[:12]
    commit = c.get("commit") or {}
    author = commit.get("author") or {}
    date = author.get("date", "?")
    message = commit.get("message", "").split("\n", 1)[0]
    url = c.get("html_url", "")
    return {"sha": sha, "date": date, "message": message, "url": url}


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Surface upstream commits landing in a fire-to-fire regression window."
    )
    ap.add_argument("--repo", required=True, help="owner/name of the public upstream repo")
    ap.add_argument("--since", help="ISO8601 window start (exclusive on GitHub's API)")
    ap.add_argument("--until", help="ISO8601 window end")
    ap.add_argument("--old-results", help="path to the earlier fire's results JSON")
    ap.add_argument("--new-results", help="path to the later fire's results JSON")
    ap.add_argument("--old-ref", help="earlier upstream SHA/branch (ref-window mode)")
    ap.add_argument("--new-ref", help="later upstream SHA/branch (ref-window mode)")
    ap.add_argument("--json", action="store_true", help="emit a machine-readable JSON report")
    args = ap.parse_args(argv)

    explicit = bool(args.since or args.until)
    from_results = bool(args.old_results or args.new_results)
    from_refs = bool(args.old_ref or args.new_ref)
    modes_given = sum([explicit, from_results, from_refs])
    if modes_given > 1:
        print("error: pass exactly one of --since/--until, --old-results/--new-results, "
              "or --old-ref/--new-ref", file=sys.stderr)
        return 1

    old_digest = new_digest = None
    window_desc = None
    if from_results:
        if not (args.old_results and args.new_results):
            print("error: --old-results and --new-results must be given together", file=sys.stderr)
            return 1
        try:
            since, until, old_digest, new_digest = _window_from_results(
                args.old_results, args.new_results
            )
        except (OSError, ValueError, json.JSONDecodeError) as e:
            print("error: %s" % e, file=sys.stderr)
            return 1
        window_desc = "[%s, %s]" % (since, until)
    elif explicit:
        if not (args.since and args.until):
            print("error: --since and --until must be given together", file=sys.stderr)
            return 1
        since, until = args.since, args.until
        window_desc = "[%s, %s]" % (since, until)
    elif from_refs:
        if not (args.old_ref and args.new_ref):
            print("error: --old-ref and --new-ref must be given together", file=sys.stderr)
            return 1
        window_desc = "%s...%s" % (args.old_ref, args.new_ref)
    else:
        print("error: must pass --since/--until, --old-results/--new-results, "
              "or --old-ref/--new-ref", file=sys.stderr)
        return 1

    token = os.environ.get("GITHUB_TOKEN") or None
    try:
        if from_refs:
            raw_commits = _fetch_commits_between_refs(args.repo, args.old_ref, args.new_ref, token=token)
        else:
            raw_commits = _fetch_commits(args.repo, since, until, token=token)
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError, ValueError, TimeoutError) as e:
        if args.json:
            print(json.dumps({
                "repo": args.repo, "window": window_desc, "result": "UNKNOWN",
                "error": "%s: %s" % (type(e).__name__, e),
            }, indent=2))
        else:
            print("UNKNOWN — could not fetch commits for %s in %s: %s: %s"
                  % (args.repo, window_desc, type(e).__name__, e))
        return 2

    commits = [_format_commit(c) for c in raw_commits]
    digest_changed = bool(old_digest and new_digest and old_digest != new_digest)

    if args.json:
        print(json.dumps({
            "repo": args.repo, "window": window_desc,
            "old_controller_digest": old_digest, "new_controller_digest": new_digest,
            "controller_digest_changed": digest_changed,
            "commit_count": len(commits), "commits": commits,
        }, indent=2))
    else:
        print("upstream candidate-confound window: %s  %s" % (args.repo, window_desc))
        if old_digest or new_digest:
            print("controller_digest: %s -> %s (%s)"
                  % (old_digest, new_digest, "CHANGED" if digest_changed else "unchanged"))
        print()
        if not commits:
            print("RESULT: 0 upstream commits landed in this window.")
            print("  No upstream code-movement candidate — strengthens a non-upstream")
            print("  explanation (node image, harness/probe artifact) by elimination.")
        else:
            print("RESULT: %d upstream commit(s) landed in this window — triage candidates:"
                  % len(commits))
            for c in commits:
                print("  %s  %s  %s" % (c["sha"], c["date"], c["message"]))
                print("      %s" % c["url"])
            print()
            print("  Not auto-classified: read each candidate's own stated measured impact")
            print("  before crediting/ruling it out (hb#471 precedent: a commit whose own PR")
            print("  claims an improvement rules itself out as the cause of a regression).")

    return 0


if __name__ == "__main__":
    sys.exit(main())
