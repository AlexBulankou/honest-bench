#!/usr/bin/env bash
# rebake-manual-trigger.sh — re-bake a Cloud Build MANUAL trigger's inline-config
# after a merge to main changes the underlying cloudbuild-*.yaml (the trusted-ref
# boundary: merging to main updates the repo file but NOT the live trigger).
#
# `gcloud builds triggers update manual <name> --inline-config=<file>` is BROKEN
# for this trigger's shape (a sourceToBuild-based manual trigger, i.e. one created
# with --repo/--repo-type/--branch rather than a bare inline-only manual trigger):
# it PATCHes the API with a body containing ONLY `{"build": {...}}` and Cloud
# Build's REST API rejects it with a content-free `400 INVALID_ARGUMENT` no matter
# what --updateMask is supplied (confirmed live 2026-07-20 against
# hb-refresh-gke-sandbox — same command with `--source-to-build-branch` in place
# of the create-only `--branch`, and with a hand-minimized updateMask, both
# reproduced the identical 400). The fix confirmed live: PATCH the FULL trigger
# resource (GET it, splice in the new `build`, PATCH the whole object back with NO
# updateMask) — Cloud Build accepts a full-resource replace where it rejects the
# CLI's partial one. This script is that workaround, so the next re-bake is a
# one-liner instead of a repeat multi-hour diagnosis.
#
# Usage:
#   PROJECT=<gcp-project> bash scripts/rebake-manual-trigger.sh <trigger-name> <inline-config-file>
#
# Prerequisites: gcloud + curl + python3 with PyYAML (`pip install pyyaml`), and
# ADC/gcloud auth with Cloud Build trigger-admin on PROJECT. If the pod's default
# `gcloud auth` identity lacks that permission but user ADC has it (the
# AGENTS.md "capability self-check" case), route through it explicitly:
#   export CLOUDSDK_AUTH_ACCESS_TOKEN=$(gcloud auth application-default print-access-token)
set -euo pipefail

: "${PROJECT:?set PROJECT to the target GCP project id}"
TRIGGER="${1:?usage: rebake-manual-trigger.sh <trigger-name> <inline-config-file>}"
CONFIG="${2:?usage: rebake-manual-trigger.sh <trigger-name> <inline-config-file>}"
[ -f "$CONFIG" ] || { echo "ERROR: inline-config file not found: $CONFIG" >&2; exit 1; }

TOKEN="${CLOUDSDK_AUTH_ACCESS_TOKEN:-$(gcloud auth print-access-token)}"
BASE="https://cloudbuild.googleapis.com/v1/projects/${PROJECT}/triggers/${TRIGGER}"

tmp_full="$(mktemp)"; tmp_merged="$(mktemp)"; tmp_resp="$(mktemp)"
trap 'rm -f "$tmp_full" "$tmp_merged" "$tmp_resp"' EXIT

echo "==> fetching live trigger $TRIGGER"
get_status="$(curl -sS -o "$tmp_full" -w '%{http_code}' -H "Authorization: Bearer $TOKEN" "$BASE")"
if [ "$get_status" != "200" ]; then
  echo "ERROR: GET $TRIGGER failed (HTTP $get_status):" >&2
  cat "$tmp_full" >&2
  exit 1
fi

echo "==> splicing $CONFIG into the fetched resource"
python3 - "$tmp_full" "$CONFIG" "$tmp_merged" <<'PY'
import json, sys, yaml

full_path, config_path, out_path = sys.argv[1:4]
with open(full_path) as f:
    full = json.load(f)
with open(config_path) as f:
    build = yaml.safe_load(f)
full["build"] = build
with open(out_path, "w") as f:
    json.dump(full, f)
PY

echo "==> PATCHing full resource back (no updateMask — see header for why)"
patch_status="$(curl -sS -o "$tmp_resp" -w '%{http_code}' -X PATCH \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  --data @"$tmp_merged" "$BASE")"

if [ "$patch_status" != "200" ]; then
  echo "ERROR: rebake FAILED (HTTP $patch_status):" >&2
  cat "$tmp_resp" >&2
  exit 1
fi

echo "==> rebake OK: $TRIGGER now matches $CONFIG"
