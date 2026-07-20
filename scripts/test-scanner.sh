#!/usr/bin/env bash
# test-scanner.sh — positive + negative self-test for check-public-safety.sh.
# The known-bad tokens are assembled at RUNTIME from fragments so that no forbidden
# pattern appears literally in this committed file — otherwise this test would itself
# trip the very scanner (2a) and the our-side codename gate (2b) it exists to verify.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SCANNER="$HERE/check-public-safety.sh"
sl=/ ; at=@ ; pound='#' ; four=4   # fragments used to build forbidden literals without embedding them

bad="$(mktemp)"; good="$(mktemp)"
trap 'rm -f "$bad" "$good"' EXIT

{
  printf 'go%sSYNTHETIC-FAKE-LINK\n' "$sl"
  printf 'b%s9999999\n' "$sl"
  printf 'cl%s9999999\n' "$sl"
  printf 'synthetic.google%s.com\n' "plex"            # assembles an internal corp-host shape
  printf 'postgres:%s%suser:pw@host/db\n' "$sl" "$sl" # assembles a DSN shape
  printf 'ya29.%s\n' "SYNTHETICACCESSTOKEN"
  printf 'nobody%sexample.com\n' "$at"                # assembles an email-address shape
} > "$bad"

if "$SCANNER" "$bad" >/dev/null 2>&1; then
  echo "FAIL: scanner did NOT trip on the known-bad fixture (exit 0)"; exit 1
fi
echo "ok: scanner tripped on known-bad fixture (exit 1 as expected)"

cat > "$good" <<'CLEAN'
product: sandbox
cluster_substrate: gke-sandbox
runtimeClassName: gvisor
controller_image: registry.k8s.io/agent-sandbox-controller:latest-main
controller_digest: sha256:0123456789abcdef
crd_version: v1beta1
PASS Warm-pool activation (hit) n=20
project: synthetic-public-project-id
CLEAN
if ! "$SCANNER" "$good" >/dev/null 2>&1; then
  echo "FAIL: scanner tripped on the clean fixture (should pass)"; "$SCANNER" "$good" || true; exit 1
fi
echo "ok: scanner passed clean fixture (exit 0 as expected)"

# EXCLUDABLE_PATTERNS self-test: a#NNNN internal-issue ref + a4<letter><digit> agent-id.
bad_internal="$(mktemp)"; bad_agentid="$(mktemp)"
trap 'rm -f "$bad" "$good" "$bad_internal" "$bad_agentid"' EXIT
printf 'see internal tracker a%s1234 for context\n' "$pound" > "$bad_internal"
if "$SCANNER" "$bad_internal" >/dev/null 2>&1; then
  echo "FAIL: scanner did NOT trip on an a#NNNN internal-issue reference"; exit 1
fi
echo "ok: scanner tripped on a#NNNN internal-issue reference (exit 1 as expected)"

printf 'fixed by a%ss1 last week\n' "$four" > "$bad_agentid"
if "$SCANNER" "$bad_agentid" >/dev/null 2>&1; then
  echo "FAIL: scanner did NOT trip on an a4<letter><digit> agent-id mention"; exit 1
fi
echo "ok: scanner tripped on a4<letter><digit> agent-id mention (exit 1 as expected)"

# Exclusion allowlist: the 4 documented convention files carry these same shapes by design
# and must NOT trip, while an identical body under a non-excluded name still does.
excl_dir="$(mktemp -d)"
trap 'rm -f "$bad" "$good" "$bad_internal" "$bad_agentid"; rm -rf "$excl_dir"' EXIT
mkdir -p "$excl_dir/render"
printf 'internal tracking a%s1234, fleet agent a%ss1\n' "$pound" "$four" > "$excl_dir/WORK_IN_PROGRESS.md"
printf 'internal tracking a%s1234, fleet agent a%ss1\n' "$pound" "$four" > "$excl_dir/render/wip.py"
printf 'internal tracking a%s1234, fleet agent a%ss1\n' "$pound" "$four" > "$excl_dir/not-excluded.md"
if ! ( cd "$excl_dir" && "$SCANNER" WORK_IN_PROGRESS.md >/dev/null 2>&1 ); then
  echo "FAIL: scanner tripped on excluded WORK_IN_PROGRESS.md"; exit 1
fi
echo "ok: scanner did not trip on excluded WORK_IN_PROGRESS.md (exit 0 as expected)"
if ! ( cd "$excl_dir" && "$SCANNER" render/wip.py >/dev/null 2>&1 ); then
  echo "FAIL: scanner tripped on excluded render/wip.py"; exit 1
fi
echo "ok: scanner did not trip on excluded render/wip.py (exit 0 as expected)"
if ( cd "$excl_dir" && "$SCANNER" not-excluded.md >/dev/null 2>&1 ); then
  echo "FAIL: scanner did NOT trip on the same body under a non-excluded filename"; exit 1
fi
echo "ok: scanner tripped on the same body under a non-excluded filename (exit 1 as expected)"

# No-arg whole-tree gate: the bare invocation (as CI calls it) must scan the real tracked
# tree, not be a silent no-op. Positive: a no-arg run in a git repo whose tracked tree
# contains a forbidden token MUST trip. Negative: an empty git repo MUST be refused (the
# gate is meant to run inside a populated repo, so zero files is suspicious, not "clean").
sandbox_repo="$(mktemp -d)"
trap 'rm -f "$bad" "$good" "$bad_internal" "$bad_agentid"; rm -rf "$excl_dir" "$sandbox_repo"' EXIT
git -C "$sandbox_repo" init -q
if ( cd "$sandbox_repo" && "$SCANNER" >/dev/null 2>&1 ); then
  echo "FAIL: no-arg passed on an EMPTY git tree (should be refused)"; exit 1
fi
echo "ok: no-arg refused empty git tree (exit 1 as expected)"

cp "$bad" "$sandbox_repo/leaky.txt"
git -C "$sandbox_repo" add leaky.txt
if ( cd "$sandbox_repo" && "$SCANNER" >/dev/null 2>&1 ); then
  echo "FAIL: no-arg did NOT trip on a tracked tree containing a forbidden token"; exit 1
fi
echo "ok: no-arg tripped on a tracked tree with a forbidden token (exit 1 as expected)"

echo "test-scanner: all assertions passed"
