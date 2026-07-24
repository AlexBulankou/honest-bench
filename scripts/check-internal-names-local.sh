#!/usr/bin/env bash
# check-internal-names-local.sh — local mirror of cloudbuild-unit-tests.yaml's
# `specific-name-scan` step (Layer 2b). Run this BEFORE pushing to catch an
# internal cluster/project name locally instead of round-tripping through CI.
#
# This is a convenience check for a4 agents pushing from a pod with user ADC
# (GOOGLE_APPLICATION_CREDENTIALS pointing at an authorized_user cred with
# secretmanager.versions.access on `honest-bench-internal-name-denylist`) —
# it is NOT wired as a git hook (no .git/hooks/ file is committed to the repo,
# so there is nothing to install), and it is not a substitute for the CI gate,
# which remains authoritative and fail-closed regardless of whether this ran.
#
# Same redaction contract as the CI step: matched name/content is NEVER
# printed, only file:line. Same denylist source (Secret Manager, never a
# tracked file — see #5286 for why the denylist itself must not live in this
# public repo). Same fail-closed posture: an empty/unfetchable denylist is a
# hard failure, not a silent skip.
#
# Usage:
#   scripts/check-internal-names-local.sh            # whole git-tracked tree
#   scripts/check-internal-names-local.sh --staged    # git staged files only
set -uo pipefail

SECRET="honest-bench-internal-name-denylist"
# Resolved at runtime, never hardcoded — the project id is itself one of the
# denylisted names, so a literal here would be exactly the self-leak this
# script exists to prevent. `gcloud config get-value project` reads whatever
# the ambient environment already has configured.
PROJECT="$(gcloud config get-value project 2>/dev/null)"
if [ -z "$PROJECT" ]; then
  echo "check-internal-names-local: could not resolve the active gcloud project — failing closed." >&2
  exit 1
fi

if [ -z "${GOOGLE_APPLICATION_CREDENTIALS:-}" ]; then
  echo "check-internal-names-local: GOOGLE_APPLICATION_CREDENTIALS not set — this check needs" >&2
  echo "  user ADC with secretmanager.versions.access on $SECRET (see AGENTS.md capability" >&2
  echo "  self-check). Skipping is not an option for a real push — run from an a4 pod, or" >&2
  echo "  rely on the CI gate (slower round-trip)." >&2
  exit 1
fi

token="$(gcloud auth application-default print-access-token 2>/dev/null)"
if [ -z "$token" ]; then
  echo "check-internal-names-local: failed to mint an access token — failing closed." >&2
  exit 1
fi

payload="$(curl -s -H "Authorization: Bearer $token" \
  "https://secretmanager.googleapis.com/v1/projects/${PROJECT}/secrets/${SECRET}/versions/latest:access")"
names="$(printf '%s' "$payload" | python3 -c '
import sys, json, base64
try:
    d = json.load(sys.stdin)
    print(base64.b64decode(d["payload"]["data"]).decode())
except Exception:
    sys.exit(1)
' 2>/dev/null)"
if [ -z "$names" ]; then
  echo "check-internal-names-local: could not fetch/decode the denylist — failing closed." >&2
  echo "  (Secret Manager access, network, or ADC token issue — see stderr above.)" >&2
  exit 1
fi

mapfile -t denylist < <(printf '%s' "$names" | tr ',' '\n' \
  | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' | grep -v '^$')
if [ "${#denylist[@]}" -eq 0 ]; then
  echo "check-internal-names-local: denylist fetched but empty — failing closed." >&2
  exit 1
fi
echo "check-internal-names-local: ${#denylist[@]} name(s) loaded from Secret Manager."

if [ "${1:-}" = "--staged" ]; then
  files_args=(--cached)
else
  files_args=()
fi

hits=0
for name in "${denylist[@]}"; do
  if [ "${#files_args[@]}" -gt 0 ]; then
    matches="$(git diff "${files_args[@]}" -G "$name" --name-only 2>/dev/null | while read -r f; do
      [ -f "$f" ] && grep -F -n -I -- "$name" "$f" 2>/dev/null | sed "s|^|${f}:|"
    done)"
  else
    matches="$(git grep -F -n -I -- "$name" 2>/dev/null || true)"
  fi
  [ -z "$matches" ] && continue
  while IFS= read -r m; do
    [ -z "$m" ] && continue
    loc="$(printf '%s' "$m" | cut -d: -f1-2)"
    echo "FORBIDDEN internal name at ${loc} (name+content redacted)" >&2
    hits=$((hits + 1))
  done <<< "$matches"
done

if [ "$hits" -ne 0 ]; then
  echo "check-internal-names-local: ${hits} forbidden-name occurrence(s) — see file:line above." >&2
  echo "  This is the same gate CI runs as specific-name-scan — fix here before pushing." >&2
  exit 1
fi
echo "check-internal-names-local: clean — no forbidden internal names in tree."
