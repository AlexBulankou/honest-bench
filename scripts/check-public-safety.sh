#!/usr/bin/env bash
# check-public-safety.sh — gate-zero scanner for the public honest-benchmarks repo (Layer 2a).
# Flags ONLY generic structural leak-patterns that are safe to name in a public file.
# The internal codename denylist is NOT here (that is the our-side 2b pre-publish gate) —
# shipping codenames in this file would leak the very list it guards.
# Fail-closed: any match -> non-zero exit. Usage:
#   check-public-safety.sh                   # NO ARGS: scan the full git-tracked tree
#   check-public-safety.sh <file>...         # scan named files
#   check-public-safety.sh --staged          # scan git staged files (pre-commit)
#
# The no-arg form is the load-bearing one for CI: a bare invocation (as the auto-refresh
# Action calls it) MUST be a real whole-repo gate, not a silent no-op over zero files.
# `git ls-files` is cwd-scoped, so it is the full public tree in the published repo and the
# bench-repo subtree when run from the staging tree — the right set in both contexts.
set -euo pipefail

# pattern|human-readable reason  (extended regex, case-sensitive unless noted)
PATTERNS=(
  'go/[A-Za-z0-9/_-]+|internal go/ shortlink'
  '\bb/[0-9]{4,}|buganizer b/ link'
  '\bcl/[0-9]{4,}|internal cl/ changelist'
  '[A-Za-z0-9.-]*\.googleplex\.com|internal *.googleplex.com host'
  'paste\.googleplex|internal paste link'
  # NOTE: SPECIFIC internal resource names (individual cluster names, obs-host pod names,
  # project ids) are intentionally NOT enumerated here. Per the header, this 2a file holds
  # only GENERIC structural patterns; specific names are the our-side pre-publish gate's
  # job. Listing the literal names in this PUBLIC file would ship the very strings it
  # guards — and the scanner cannot scan itself, so it could never catch that self-leak.
  'postgres(ql)?://|database DSN'
  'ya29\.[A-Za-z0-9_-]+|OAuth access token'
  'BEGIN [A-Z ]*PRIVATE KEY|private key block'
  '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}|email address (no emails belong in public results)'
)

# Patterns that additionally carry a set of file exclusions (documented conventions where
# the pattern's referent is intentionally rendered, not leaked). pattern|reason|exclude-glob
# The exclude-glob is a `case` glob matched against the scanned path; empty = no exclusion.
EXCLUDABLE_PATTERNS=(
  'a#[0-9]+|internal-repo issue reference (a#NNNN — private AlexBulankou/a tracker)|WORK_IN_PROGRESS.md:UPSTREAM_BLOCKERS.md:UPSTREAM_BLOCKERS_DETAIL.md:render/wip.py'
  '\ba4[a-z][0-9]\b|fleet agent-id mention (a4<letter><digit>)|WORK_IN_PROGRESS.md:UPSTREAM_BLOCKERS.md:UPSTREAM_BLOCKERS_DETAIL.md:render/wip.py'
)

_excluded() {
  local f="$1" globs="$2" g
  local IFS=':'
  for g in $globs; do
    case "$f" in "$g") return 0;; esac
  done
  return 1
}

scan_target() {
  local f="$1" hit=0
  [ -f "$f" ] || return 0
  case "$f" in *check-public-safety.sh) return 0;; esac  # don't scan self (regex substrings)
  for entry in "${PATTERNS[@]}"; do
    local pat="${entry%%|*}" reason="${entry#*|}"
    if grep -nE "$pat" "$f" >/tmp/_cps_hits 2>/dev/null; then
      while IFS= read -r line; do
        echo "FORBIDDEN [$reason]: $f:$line"
        hit=1
      done < /tmp/_cps_hits
    fi
  done
  for entry in "${EXCLUDABLE_PATTERNS[@]}"; do
    local pat="${entry%%|*}"
    local rest="${entry#*|}"
    local reason="${rest%%|*}"
    local globs="${rest#*|}"
    _excluded "$f" "$globs" && continue
    if grep -nE "$pat" "$f" >/tmp/_cps_hits 2>/dev/null; then
      while IFS= read -r line; do
        echo "FORBIDDEN [$reason]: $f:$line"
        hit=1
      done < /tmp/_cps_hits
    fi
  done
  return $hit
}

rc=0
if [ "${1:-}" = "--staged" ]; then
  while IFS= read -r f; do scan_target "$f" || rc=1; done \
    < <(git diff --cached --name-only --diff-filter=ACM)
elif [ "$#" -eq 0 ]; then
  # No args: fail-closed whole-tree scan. An empty tree is itself suspicious (the gate is
  # meant to run inside a populated repo), so refuse rather than print a hollow "clean".
  scanned=0
  while IFS= read -r f; do scanned=1; scan_target "$f" || rc=1; done < <(git ls-files)
  if [ "$scanned" -eq 0 ]; then
    echo "check-public-safety: BLOCKED — no git-tracked files found to scan (run inside the repo)."
    exit 1
  fi
else
  for f in "$@"; do scan_target "$f" || rc=1; done
fi

if [ "$rc" -ne 0 ]; then
  echo "---"
  echo "check-public-safety: BLOCKED — remove the flagged content before it lands in the public repo."
  exit 1
fi
echo "check-public-safety: clean"
exit 0
