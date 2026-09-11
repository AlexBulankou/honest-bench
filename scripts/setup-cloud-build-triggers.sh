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
#      trigger (no cluster, no GitHub token needed). Also needs Secret Accessor
#      on hb-refresh-gh-app-pem for the [5/8] render-autoheal trigger (least-
#      privilege reuse of the per-agent GitHub App PEM to mint a bot-identity
#      token for its PR-open step — no cluster access, unlike REFRESH_SA below).
#   3. REFRESH_SA — a DEDICATED least-privilege SA for the refresh trigger, with on
#      PROJECT: roles/container.admin + roles/iam.serviceAccountUser +
#      roles/compute.viewer + roles/logging.logWriter, plus Secret Accessor on
#      hb-refresh-github-token. logging.logWriter is required by the
#      CLOUD_LOGGING_ONLY logging mode + the step-0 logWriter preflight probe;
#      without it every fire dies at step 0 with an empty log.
#   4. Secret Manager secret `hb-refresh-github-token` — a narrow GitHub token
#      (contents:write + pull-requests:write on the repo) for the auto-refresh PR.
#   5. TRIGGER_REBAKER_SA — a DEDICATED SA for the [8/8] auto-rebake trigger
#      (hb#8239), scoped to exactly cloudbuild.builds.get + cloudbuild.builds.update
#      on PROJECT (Cloud Build has no triggers-specific IAM permission — a
#      BuildTrigger's get/patch rides the builds permission namespace) plus
#      Secret Accessor on hb-sdev-webhook (its notify step). No build-submit/
#      cluster access of any kind — narrower than CLOUDBUILD_SA/REFRESH_SA above.
#
# Edits to the trigger CONFIG files (cloudbuild-*.yaml) require re-running the
# matching `triggers update` below — inline-config is the trusted-ref boundary, so
# the live trigger and the repo file share one source and cannot drift.
set -euo pipefail

: "${PROJECT:?set PROJECT to the target GCP project id}"
: "${CLOUDBUILD_SA:?set CLOUDBUILD_SA to the offline-CI Cloud Build service account}"
: "${REFRESH_SA:?set REFRESH_SA to the dedicated GKE-refresh service account}"
: "${TRIGGER_REBAKER_SA:?set TRIGGER_REBAKER_SA to the dedicated trigger-rebake service account}"
OWNER="AlexBulankou"
REPO="honest-bench"

# `triggers create github --service-account` requires the FULL resource path
# (projects/<p>/serviceAccounts/<email>); a bare email is rejected with a bare
# INVALID_ARGUMENT (no field detail — an easy hour to lose). Accept either form:
# pass through anything already containing a slash, else expand the bare email.
sa_path() { case "$1" in */*) printf '%s' "$1";; *) printf 'projects/%s/serviceAccounts/%s' "$PROJECT" "$1";; esac; }
CLOUDBUILD_SA="$(sa_path "$CLOUDBUILD_SA")"
REFRESH_SA="$(sa_path "$REFRESH_SA")"
TRIGGER_REBAKER_SA="$(sa_path "$TRIGGER_REBAKER_SA")"

echo "==> [1/8] unit-tests PR gate (fires on PRs targeting main; FAIL-CLOSED merge gate)"
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

echo "==> [2/8] north-star cross-lane PASS->FAIL flip gate (fires on PRs targeting main; FAIL-CLOSED merge gate)"
# hb#623: blocks a PR whose latest.json flips the customer headline PASS->FAIL vs
# main's currently-merged latest.json (the cross-lane overwrite render.py's own
# delta caveat structurally can't see). Same offline CLOUDBUILD_SA as the unit
# gate (no cluster, no token — a git fetch of main + a stdlib-only checker).
# COMMENTS_DISABLED required (same /gcbrun default footgun as [1/8]). create-or-update
# re-bakes the inline-config (trusted-ref boundary). No post-merge twin: on push to
# main HEAD==origin/main, so the checker self-compares and can never see a flip — a
# post-merge flip trigger would be a guaranteed no-op, so it is deliberately omitted.
gcloud builds triggers create github --name=hb-north-star-flip-gate \
  --inline-config=cloudbuild-north-star-flip-gate.yaml \
  --repo-owner="$OWNER" --repo-name="$REPO" \
  --pull-request-pattern='^main$' \
  --comment-control=COMMENTS_DISABLED \
  --service-account="$CLOUDBUILD_SA" \
  --project="$PROJECT" \
  || gcloud builds triggers update github hb-north-star-flip-gate \
    --inline-config=cloudbuild-north-star-flip-gate.yaml \
    --repo-owner="$OWNER" --repo-name="$REPO" \
    --pull-request-pattern='^main$' \
    --comment-control=COMMENTS_DISABLED \
    --service-account="$CLOUDBUILD_SA" \
    --project="$PROJECT"

echo "==> [3/8] diagnostic-lineage merge gate (fires on PRs targeting main; FAIL-CLOSED merge gate)"
# hb#646: blocks a PR whose latest.json carries a diagnostic-lineage
# fork-build signature (fork_fix_count==0, or fork_sha==fork_base_upstream_sha —
# the hb#643/#644 shape that silently overwrote the validated production pin
# for ~1.9h). Unlike [2/8] this signature is intrinsically bad on the PR's own
# terms, so no origin/main fetch is needed — same offline CLOUDBUILD_SA (no
# cluster, no token — a stdlib-only checker on the PR's own tree). COMMENTS_DISABLED
# required (same /gcbrun default footgun as [1/8]/[2/8]). create-or-update
# re-bakes the inline-config (trusted-ref boundary). No post-merge twin, same
# rationale as [2/8]: on push to main the PR IS main, so a self-compare (if it
# needed one) or a self-check of an already-merged pin can never re-detect a
# signature that already passed at merge time — a post-merge trigger would be a
# guaranteed no-op.
gcloud builds triggers create github --name=hb-diagnostic-lineage-gate \
  --inline-config=cloudbuild-diagnostic-lineage-gate.yaml \
  --repo-owner="$OWNER" --repo-name="$REPO" \
  --pull-request-pattern='^main$' \
  --comment-control=COMMENTS_DISABLED \
  --service-account="$CLOUDBUILD_SA" \
  --project="$PROJECT" \
  || gcloud builds triggers update github hb-diagnostic-lineage-gate \
    --inline-config=cloudbuild-diagnostic-lineage-gate.yaml \
    --repo-owner="$OWNER" --repo-name="$REPO" \
    --pull-request-pattern='^main$' \
    --comment-control=COMMENTS_DISABLED \
    --service-account="$CLOUDBUILD_SA" \
    --project="$PROJECT"

echo "==> [4/8] unit-tests post-merge gate (fires on push to main)"
# Gates post-merge main so a bad merge is caught even if branch protection is not
# (yet) wired to require the PR check. create-or-note-exists (idempotent re-run).
gcloud builds triggers create github --name=hb-unit-tests-main \
  --inline-config=cloudbuild-unit-tests.yaml \
  --repo-owner="$OWNER" --repo-name="$REPO" \
  --branch-pattern='^main$' \
  --service-account="$CLOUDBUILD_SA" \
  --project="$PROJECT" \
  || echo "   (already exists — re-run with: gcloud builds triggers update github hb-unit-tests-main --inline-config=cloudbuild-unit-tests.yaml ...)"

echo "==> [5/8] render post-merge auto-heal (fires on push to main touching harness/scenarios/**; hb#846)"
# Structural fix for the hb#845 squash-merge caption-staleness class: a push that
# touches harness/scenarios/ re-runs render.generate() against main's true
# post-merge HEAD and opens a follow-up PR iff the regen diverges from what's
# already committed. Same offline CLOUDBUILD_SA as [1/8]/[4/8] — this trigger
# also needs zero cluster access (a git fetch + offline regen + one REST call) —
# now additionally granted Secret Accessor on hb-refresh-gh-app-pem (least-
# privilege: reuses the existing per-agent GitHub App PEM to mint a bot-identity
# token for the PR-open step, same mint path cloudbuild-refresh-gke-sandbox.yaml
# uses, rather than provisioning a second credential). --included-files scopes
# the trigger to the sole caption-input path so unrelated main pushes never
# re-run generate() (that broader gate is [4/8]'s job). create-or-update
# re-bakes the inline-config (trusted-ref boundary).
gcloud builds triggers create github --name=hb-render-autoheal \
  --inline-config=cloudbuild-render-autoheal.yaml \
  --repo-owner="$OWNER" --repo-name="$REPO" \
  --branch-pattern='^main$' \
  --included-files='harness/scenarios/**' \
  --service-account="$CLOUDBUILD_SA" \
  --project="$PROJECT" \
  || gcloud builds triggers update github hb-render-autoheal \
    --inline-config=cloudbuild-render-autoheal.yaml \
    --repo-owner="$OWNER" --repo-name="$REPO" \
    --branch-pattern='^main$' \
    --included-files='harness/scenarios/**' \
    --service-account="$CLOUDBUILD_SA" \
    --project="$PROJECT"

echo "==> [6/8] gke-sandbox refresh (MANUAL only — no branch/PR/schedule; spend-gated by invocation)"
# --branch is REQUIRED by gcloud whenever --repo is set on a manual trigger (API
# contract, not optional) — it only pins which ref is checked out as build
# context; the build STEPS still come from inline-config, so this is not a
# trusted-ref divergence. Confirmed live 2026-07-20: omitting it fails
# with "Missing required argument [REVISION]: --branch or --tag is required".
# NOTE: 'gcloud builds triggers update manual ... --inline-config=...' (the
# naive re-bake command) is BROKEN for this trigger — it PATCHes a
# {"build": {...}}-only body that the API rejects with a content-free 400
# INVALID_ARGUMENT no matter the --updateMask (confirmed live 2026-07-20:
# reproduced with --source-to-build-branch in place of the create-only
# --branch, and with a hand-minimized updateMask; both still 400).
# scripts/rebake-manual-trigger.sh is the confirmed-working full-resource-PATCH
# replacement — use it for every future re-bake of this trigger, not the line
# gcloud itself suggests.
gcloud builds triggers create manual --name=hb-refresh-gke-sandbox \
  --inline-config=cloudbuild-refresh-gke-sandbox.yaml \
  --repo="https://github.com/${OWNER}/${REPO}" \
  --repo-type=GITHUB \
  --branch=main \
  --service-account="$REFRESH_SA" \
  --project="$PROJECT" \
  || echo "   (already exists — re-run with: PROJECT=$PROJECT bash scripts/rebake-manual-trigger.sh hb-refresh-gke-sandbox cloudbuild-refresh-gke-sandbox.yaml)"

echo "==> [7/8] gke-kata cold true_ttfe refresh (MANUAL, ON-DEMAND — no branch/PR/schedule; spend-gated by invocation)"
# Same MANUAL shape as [6/8] and the same dedicated REFRESH_SA — this refresh runs
# against the PERSISTENT kata scenarios cluster (no ephemeral create/teardown), so
# it needs no extra IAM beyond container.admin + serviceAccountUser + compute.viewer
# + logging.logWriter + Secret Accessor already granted for the gVisor refresh. --branch pins only the
# checked-out ref; steps come from inline-config. The internal kata cluster name is
# NOT baked here — it is passed at fire time via the _CLUSTER substitution (kept out
# of the public config). Re-bake with scripts/rebake-manual-trigger.sh, NOT the
# broken `triggers update manual --inline-config` path (see [6/8] note).
gcloud builds triggers create manual --name=hb-refresh-gke-kata \
  --inline-config=cloudbuild-refresh-gke-kata.yaml \
  --repo="https://github.com/${OWNER}/${REPO}" \
  --repo-type=GITHUB \
  --branch=main \
  --service-account="$REFRESH_SA" \
  --project="$PROJECT" \
  || echo "   (already exists — re-run with: PROJECT=$PROJECT bash scripts/rebake-manual-trigger.sh hb-refresh-gke-kata cloudbuild-refresh-gke-kata.yaml)"

echo "==> [8/8] auto-rebake manual/frozen-inline triggers on merge (fires on push to main touching cloudbuild-*.yaml; hb#8239)"
# The last manual step in the trigger-drift fix cycle: check-hb-trigger-drift.py
# (read-only) ALARMS when a merged cloudbuild-*.yaml edit hasn't been re-baked
# into its live MANUAL/frozen-inline trigger yet; this trigger is the automated
# FIX, invoking scripts/rebake-manual-trigger.sh for every mapped pair so nobody
# has to remember the manual step. scripts/manual-trigger-rebake-map.py is the
# explicit file->trigger(s) mapping (kept in sync by hand with the upstream
# drift detector's own TRIGGERS dict, cross-repo). --included-files scopes this
# trigger to only fire on a cloudbuild-*.yaml change — cloudbuild-rebake-manual-
# triggers.yaml itself then diffs HEAD^..HEAD to figure out exactly which
# mapped trigger(s), if any, actually need a rebake (not every cloudbuild-*.yaml
# maps to a rebakeable trigger — see the map's own exclusions). Runs as the
# dedicated TRIGGER_REBAKER_SA (exactly cloudbuild.builds.get + .update — no
# build-submit/cluster/secret access beyond the notify webhook secret) rather
# than CLOUDBUILD_SA/REFRESH_SA above, since a get+patch-only identity is
# narrower than either. create-or-update re-bakes the inline-config (trusted-
# ref boundary) — this IS the trigger whose own job is re-baking OTHER
# triggers, so it re-bakes itself the same way on every re-run of this script.
gcloud builds triggers create github --name=hb-rebake-manual-triggers \
  --inline-config=cloudbuild-rebake-manual-triggers.yaml \
  --repo-owner="$OWNER" --repo-name="$REPO" \
  --branch-pattern='^main$' \
  --included-files='cloudbuild-*.yaml' \
  --service-account="$TRIGGER_REBAKER_SA" \
  --project="$PROJECT" \
  || gcloud builds triggers update github hb-rebake-manual-triggers \
    --inline-config=cloudbuild-rebake-manual-triggers.yaml \
    --repo-owner="$OWNER" --repo-name="$REPO" \
    --branch-pattern='^main$' \
    --included-files='cloudbuild-*.yaml' \
    --service-account="$TRIGGER_REBAKER_SA" \
    --project="$PROJECT"

cat <<EOF

Done. Fire the manual gVisor refresh with:
  gcloud builds triggers run hb-refresh-gke-sandbox --project=$PROJECT \\
    --substitutions=_POOL_REPLICAS=10,_MACHINE_TYPE=n2-standard-16,_REGION=us-central1

Fire the manual kata cold true_ttfe refresh with (COLLISION-ACK the shared cluster
first — a peer-authored, linked artifact confirming the window is clear):
  gcloud builds triggers run hb-refresh-gke-kata --project=$PROJECT \\
    --substitutions=_CLUSTER=<kata-scenarios-cluster>,_REGION=us-central1

NOTE — the kind (vanilla, no-gVisor) refresh workflow was NOT migrated. It is
deprecated by its own header (a kind run only DOWNGRADES the live gVisor headline
to a pending/kind number) and kind-in-Cloud-Build (docker-in-docker) is fragile
machinery for a strictly-inferior artifact. The gke-sandbox refresh above is the
real refresh path. If a free no-spend vanilla refresh is genuinely wanted, add a
fast-follow manual trigger pointed at a vanilla (no --sandbox) ephemeral cluster.
EOF
