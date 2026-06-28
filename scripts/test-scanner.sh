#!/usr/bin/env bash
# test-scanner.sh — positive + negative self-test for check-public-safety.sh.
# The known-bad tokens are assembled at RUNTIME from fragments so that no forbidden
# pattern appears literally in this committed file — otherwise this test would itself
# trip the very scanner (2a) and the our-side codename gate (2b) it exists to verify.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SCANNER="$HERE/check-public-safety.sh"
sl=/ ; at=@   # fragments used to build forbidden literals without embedding them

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

# No-arg whole-tree gate: the bare invocation (as CI calls it) must scan the real tracked
# tree, not be a silent no-op. Positive: a no-arg run in a git repo whose tracked tree
# contains a forbidden token MUST trip. Negative: an empty git repo MUST be refused (the
# gate is meant to run inside a populated repo, so zero files is suspicious, not "clean").
sandbox_repo="$(mktemp -d)"; trap 'rm -f "$bad" "$good"; rm -rf "$sandbox_repo"' EXIT
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
