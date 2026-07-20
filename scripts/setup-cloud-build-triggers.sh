#!/usr/bin/env bash
# Set up the honest-bench Cloud Build triggers (GHA->CB migration).
#
# Fleet rule: NO GitHub Actions on any repo — Cloud Build ONLY. This script
# creates the CB triggers that replace .github/workflows/*.yml (all deleted in the
# same PR). It is PARAMETRIZED (no project baked in), so it doubles as the public
# reproducibility path: point it at YOUR project + service accounts.
#
# Run ONCE, from a machine with ADC that has cloudbuild trigger-admin on PROJECT.
#
#   PROJECT=<your-project> \
#   CLOUDBUILD_SA=<offline-ci-sa-email> \
#   REFRESH_SA=<gke-refresh-sa-email> \
#     bash scripts/setup-cloud-build-triggers.sh
#
# PREREQUISITES (this script does NOT create them):
#   1. A Cloud Build GitHub App connection for AlexBulankou/honest-bench. The repo
#      has ZERO CB connections today, so trigger creation FAILS until this lands —
#      the migration's enabler and why the migration PR stays a draft (held) until
#      the connection is live.
#   2. CLOUDBUILD_SA — a low-privilege CB runtime SA for the OFFLINE unit-tests
#      trigger (no cluster, no GitHub token needed).
#   3. REFRESH_SA — a DEDICATED least-privilege SA for the refresh trigger, with on
#      PROJECT: roles/container.admin + roles/iam.serviceAccountUser +
#      roles/compute.viewer, plus Secret Accessor on hb-refresh-github-token.
#   4. Secret Manager secret `hb-refresh-github-token` — a narrow GitHub token
#      (contents:write + pull-requests:write on the repo) for the auto-refresh PR.
#
# Edits to the trigger CONFIG files (cloudbuild-*.yaml) require re-running the
# matching `triggers update` below — inline-config is the trusted-ref boundary, so
# the live trigger and the repo file share one source and cannot drift.
set -euo pipefail

: "${PROJECT:?set PROJECT to the target GCP project id}"
: "${CLOUDBUILD_SA:?set CLOUDBUILD_SA to the offline-CI Cloud Build service account}"
: "${REFRESH_SA:?set REFRESH_SA to the dedicated GKE-refresh service account}"
OWNER="AlexBulankou"
REPO="honest-bench"

# `triggers create github --service-account` requires the FULL resource path
# (projects/<p>/serviceAccounts/<email>); a bare email is rejected with a bare
# INVALID_ARGUMENT (no field detail — an easy hour to lose). Accept either form:
# pass through anything already containing a slash, else expand the bare email.
sa_path() { case "$1" in */*) printf '%s' "$1";; *) printf 'projects/%s/serviceAccounts/%s' "$PROJECT" "$1";; esac; }
CLOUDBUILD_SA="$(sa_path "$CLOUDBUILD_SA")"
REFRESH_SA="$(sa_path "$REFRESH_SA")"

echo "==> [1/3] unit-tests PR gate (fires on PRs targeting main; FAIL-CLOSED merge gate)"
# COMMENTS_DISABLED is REQUIRED — the `github` subcommand with --pull-request-pattern
# silently defaults to COMMENTS_ENABLED, gating every build behind /gcbrun. The flag
# is identical on create and update, so it survives the re-bake path below.
# create-or-update: `create` on a fresh repo (no trigger yet — bare `update` would fail
# with "trigger not found"); `update` re-bakes the inline-config on every re-run (the
# trusted-ref boundary — repo file and live trigger share one source, cannot drift).
gcloud builds triggers create github --name=hb-unit-tests \
  --inline-config=cloudbuild-unit-tests.yaml \
  --repo-owner="$OWNER" --repo-name="$REPO" \
  --pull-request-pattern='^main$' \
  --comment-control=COMMENTS_DISABLED \
  --service-account="$CLOUDBUILD_SA" \
  --project="$PROJECT" \
  || gcloud builds triggers update github hb-unit-tests \
    --inline-config=cloudbuild-unit-tests.yaml \
    --repo-owner="$OWNER" --repo-name="$REPO" \
    --pull-request-pattern='^main$' \
    --comment-control=COMMENTS_DISABLED \
    --service-account="$CLOUDBUILD_SA" \
    --project="$PROJECT"

echo "==> [2/3] unit-tests post-merge gate (fires on push to main)"
# Gates post-merge main so a bad merge is caught even if branch protection is not
# (yet) wired to require the PR check. create-or-note-exists (idempotent re-run).
gcloud builds triggers create github --name=hb-unit-tests-main \
  --inline-config=cloudbuild-unit-tests.yaml \
  --repo-owner="$OWNER" --repo-name="$REPO" \
  --branch-pattern='^main$' \
  --service-account="$CLOUDBUILD_SA" \
  --project="$PROJECT" \
  || echo "   (already exists — re-run with: gcloud builds triggers update github hb-unit-tests-main --inline-config=cloudbuild-unit-tests.yaml ...)"

echo "==> [3/3] gke-sandbox refresh (MANUAL only — no branch/PR/schedule; spend-gated by invocation)"
# --branch is REQUIRED by gcloud whenever --repo is set on a manual trigger (API
# contract, not optional) — it only pins which ref is checked out as build
# context; the build STEPS still come from inline-config, so this is not a
# trusted-ref divergence. Confirmed live 2026-07-20: omitting it fails
# with "Missing required argument [REVISION]: --branch or --tag is required".
gcloud builds triggers create manual --name=hb-refresh-gke-sandbox \
  --inline-config=cloudbuild-refresh-gke-sandbox.yaml \
  --repo="https://github.com/${OWNER}/${REPO}" \
  --repo-type=GITHUB \
  --branch=main \
  --service-account="$REFRESH_SA" \
  --project="$PROJECT" \
  || echo "   (already exists — re-run with: gcloud builds triggers update manual hb-refresh-gke-sandbox --inline-config=cloudbuild-refresh-gke-sandbox.yaml --branch=main ...)"

cat <<EOF

Done. Fire the manual gVisor refresh with:
  gcloud builds triggers run hb-refresh-gke-sandbox --project=$PROJECT \\
    --substitutions=_POOL_REPLICAS=10,_MACHINE_TYPE=n2-standard-16,_REGION=us-central1

NOTE — the kind (vanilla, no-gVisor) refresh workflow was NOT migrated. It is
deprecated by its own header (a kind run only DOWNGRADES the live gVisor headline
to a pending/kind number) and kind-in-Cloud-Build (docker-in-docker) is fragile
machinery for a strictly-inferior artifact. The gke-sandbox refresh above is the
real refresh path. If a free no-spend vanilla refresh is genuinely wanted, add a
fast-follow manual trigger pointed at a vanilla (no --sandbox) ephemeral cluster.
EOF
