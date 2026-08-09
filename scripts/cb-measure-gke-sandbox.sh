#!/usr/bin/env bash
# Extracted from cloudbuild-refresh-gke-sandbox.yaml's `measure` step (was an
# inline `args: [-c, |...]` block scalar). The inline script grew past Cloud
# Build's per-arg 10000-character limit (10837 chars at extraction time,
# confirmed via `gcloud builds triggers describe ... --format=json`) and the
# trigger stopped firing at all — "invalid .steps field: build step 1 arg 1
# too long (max: 10000)" on ANY invocation, not just a substitution-specific
# one. This file is the fix: keep the logic, move it out of the YAML so the
# step's own `args` collapses to a short file reference.
#
# Because this now runs as a real script (not YAML text CB's substitution
# engine scans), CB substitutions (${_REGION} etc.) can no longer be
# dereferenced inline here — they must be resolved at the step-config level
# via `env:`/`secretEnv:` and read here as plain shell env vars. The step's
# `env:` block sets HB_REGION / HB_KEEP_CLUSTER_ON_FAILURE / BENCH_MACHINE_TYPE
# for exactly this reason. Likewise, the doubled `$$` that CB-embedded scripts
# require (to survive the substitution scanner) is gone — single `$` is
# correct here, same as any normal bash script.
set -euo pipefail

REGION="$HB_REGION"
# Short, valid GKE cluster name (<=40 chars, starts with a letter). BUILD_ID
# is a 36-char UUID — too long to prefix — so key on a timestamp; a manual
# trigger fires one at a time, so collision is not a concern.
CLUSTER="hb-gvisor-ci-$(date +%s)"

# suite_git_sha provenance (hb#439): the honest-bench commit this fire measured
# against. Doesn't depend on the cluster, so capture it up front. CB checks out
# a full clone into the workspace (confirmed by cloudbuild-unit-tests.yaml's own
# `git rev-parse --git-dir` probe), so this always resolves to the real commit
# — never an empty default. Feeds harness.run's build_provenance() the same
# way BENCH_MACHINE_TYPE etc. do (plain env var, read at measure time).
export BENCH_SUITE_GIT_SHA=$(git rev-parse HEAD 2>/dev/null || echo "")
echo "==> suite_git_sha=$BENCH_SUITE_GIT_SHA"

# Guaranteed teardown: fires on normal exit, `set -e` abort, or SIGTERM
# (build timeout). The CB-native stand-in for `if: always()`. On a
# FAILED measure step, _KEEP_CLUSTER_ON_FAILURE=1 (hb#311) skips the
# delete so the cluster stays live for fast iteration — a successful
# exit always tears down regardless of the flag.
cleanup() {
  local exit_code=$?
  kill "${NODE_SAMPLER_PID:-}" 2>/dev/null || true
  if [ "$exit_code" -ne 0 ] && [ "$HB_KEEP_CLUSTER_ON_FAILURE" = "1" ]; then
    echo "==> [trap] measure FAILED (exit $exit_code) and _KEEP_CLUSTER_ON_FAILURE=1 — leaving cluster $CLUSTER live for debugging."
    echo "==> [trap] manual reap when done: gcloud container clusters delete $CLUSTER --region $REGION --quiet"
    return 0
  fi
  echo "==> [trap] tearing down ephemeral cluster $CLUSTER"
  # Loud on failure (hb#382 follow-up): a bare `|| true` here used to
  # swallow a mid-build IAM-strip 403 with zero build-time signal --
  # the ONLY backstop was the age-based orphan reaper, up to ~90min
  # later. Capture the real exit code and, on failure, (a) print a
  # greppable ERROR line in the build log and
  # (b) best-effort POST an immediate alert through the same #s-dev
  # webhook sdev-delta-notify uses below, so a leak is known at
  # build-failure time instead of on the reaper's next sweep. The
  # delete's exit code is deliberately NOT re-raised as the step's
  # own exit status: cleanup() runs inside an EXIT trap, so
  # returning nonzero from it cannot change the build's
  # already-determined exit code anyway (bash locks that in before
  # running EXIT traps), and trying to would only risk masking the
  # real measure-step failure this trap is reporting on.
  # `local del_exit=$?` inside `if ! cmd; then` would capture the
  # NEGATED test's status (always 0), not cmd's real exit -- so the
  # delete's actual code is captured via `||` BEFORE the `if` runs.
  local del_exit=0
  gcloud container clusters delete "$CLUSTER" --region "$REGION" --quiet || del_exit=$?
  if [ "$del_exit" -ne 0 ]; then
    echo "==> [trap] ERROR: teardown FAILED (exit $del_exit) for cluster $CLUSTER in $REGION -- likely LEAKED and still billing. Manual reap: gcloud container clusters delete $CLUSTER --region $REGION --quiet"
    if [ -n "${HB_SDEV_WEBHOOK:-}" ]; then
      curl -sS -X POST -H "Content-Type: application/json" \
        -d "{\"text\": \"honest-bench hb-refresh-gke-sandbox: teardown FAILED (exit $del_exit) for cluster $CLUSTER in $REGION -- likely leaked and still billing. Backstopped by the age-based orphan reaper (~90min), but check now: gcloud container clusters delete $CLUSTER --region $REGION --quiet\"}" \
        "${HB_SDEV_WEBHOOK}" >/dev/null || echo "==> [trap] WARNING: #s-dev alert post also failed (non-fatal) -- relying on the orphan reaper backstop"
    fi
  fi
}
trap cleanup EXIT

# kubeconfig isolated to /workspace so we never touch a shared path.
export KUBECONFIG=/workspace/hb-refresh.kubeconfig

# Harness deps. The cloud-sdk image's python3 is PEP-668 externally-managed,
# so --break-system-packages is required (container is ephemeral — safe).
pip install --quiet --no-cache-dir --break-system-packages -r harness/requirements.txt

# `--sandbox` is NOT a `clusters create` flag at all (only `node-pools
# create` has it — confirmed via `gcloud container clusters create --help`,
# zero gVisor-related flags). #304's fix only corrected the flag's
# syntax (space -> =) while the real defect was targeting the wrong
# resource, so the trigger kept failing "unrecognized arguments" even
# after that merge. Fix: create the cluster with a plain default pool,
# then add a dedicated gVisor-sandboxed node pool (mirrors the
# the persistent internal cluster's terraform: gVisor is per-nodepool, not
# per-cluster). Harness pods request runtimeClassName=gvisor and land
# on the sandboxed pool via its auto-applied taint/toleration.
# --enable-network-policy (Calico) mirrors the persistent internal
# cluster's terraform config. Without it the control-plane isolation
# badges (cross_tenant_network_isolation, default_deny_egress) read
# FAIL — not a flake, a real enforcement gap: NetworkPolicy objects
# admit cleanly but nothing enforces them. See #314.
echo "==> creating ephemeral cluster $CLUSTER in $REGION"
gcloud container clusters create "$CLUSTER" \
  --region "$REGION" \
  --release-channel rapid \
  --machine-type "$BENCH_MACHINE_TYPE" \
  --node-locations "$REGION-a" \
  --num-nodes 1 \
  --enable-autoscaling --min-nodes 1 --max-nodes 3 \
  --enable-network-policy \
  --no-enable-basic-auth --no-issue-client-certificate

echo "==> adding gVisor-sandboxed node pool to $CLUSTER"
gcloud container node-pools create hb-gvisor-pool \
  --cluster "$CLUSTER" \
  --region "$REGION" \
  --machine-type "$BENCH_MACHINE_TYPE" \
  --node-locations "$REGION-a" \
  --num-nodes 1 \
  --enable-autoscaling --min-nodes 1 --max-nodes 3 \
  --sandbox=type=gvisor

gcloud container clusters get-credentials "$CLUSTER" --region "$REGION"

# Background node-count sampler (hb#319 diagnostic): warmpool_cold_start asks
# for 30 resident + 40 claims on hb-gvisor-pool's 1-3 node autoscale ceiling —
# the leading hypothesis for hb#318's "warm slower than cold" anomaly is that
# the burst saturates the 1-node floor and a reactive GKE scale-up (VM boot +
# gVisor runtime init + kubelet join) lands inside the measured bind-time
# window, smearing "warm" and "cold" together. This can't be reconstructed
# post-hoc (the cluster is ephemeral, no retained autoscaler event history),
# so sample hb-gvisor-pool's node count across the WHOLE measure phase and
# print it at the end — confirms or rules out the ceiling directly from the
# next fire's build log, no separate debugging fire required. Best-effort:
# a sampler hiccup must never fail the measure step over a diagnostic.
#
# Stream EACH sample line to stdout as it's taken (prefixed hb319-sample,
# easy to grep out of the full step log), not only the end-of-window `cat`
# below. The first live fire (hb#319) lost the trailing `cat` dump to a
# build-infra log-sink hiccup with the file itself still intact but the
# one-shot end-of-window print never landing in the persisted log — spreading
# the same data across ~20min of small, already-streamed lines makes a
# single late-window log-sink gap survivable (only that gap's samples are
# lost, not the whole series). The file + final `cat` stay as a convenience
# recap; the per-sample echo is now the durable copy.
NODE_SAMPLE_LOG=/workspace/hb319-node-count-sample.log
: > "$NODE_SAMPLE_LOG"
( while true; do
    sample="hb319-sample $(date -u +%FT%TZ) nodes=$(kubectl get nodes -l cloud.google.com/gke-nodepool=hb-gvisor-pool --no-headers 2>/dev/null | wc -l | tr -d ' ')"
    echo "$sample" || true
    printf '%s\n' "$sample" >> "$NODE_SAMPLE_LOG" 2>/dev/null || true
    sleep 3
  done ) &
NODE_SAMPLER_PID=$!

# Node-image / gVisor runsc version provenance (hb#317, mirrors
# machine_type's hb#313 pattern). These can only be resolved at
# runtime against the live cluster (unlike BENCH_MACHINE_TYPE, which is
# a static substitution known at submit time), so they're captured here
# as inline `export`s consumed by the harness.run invocation below rather
# than as static `env:` list entries.
echo "==> capturing node-image / runsc version for provenance"
gvisor_node=$(kubectl get nodes -l cloud.google.com/gke-nodepool=hb-gvisor-pool \
  -o jsonpath='{.items[0].metadata.name}')
# Node image: kubeletVersion carries the GKE build suffix (e.g.
# v1.31.1-gke.1846000) that maps 1:1 to a node image release — a raw
# osImage string (e.g. "Container-Optimized OS from Google") is too
# generic to diff across GKE releases, so kubeletVersion is the proxy.
export BENCH_NODE_IMAGE=$(kubectl get node "$gvisor_node" \
  -o jsonpath='{.status.nodeInfo.kubeletVersion}' 2>/dev/null || echo "")
# runsc has no K8s-API-visible version — it's a binary on the node's host
# filesystem (GKE ships it at /home/kubernetes/bin/runsc). `kubectl debug
# node/...` is the standard no-SSH way to chroot into a node's root fs;
# the debug pod it creates is torn down along with the whole ephemeral
# cluster by this step's EXIT trap. Best-effort: a failure here must
# never fail the measure step over a nice-to-have provenance field
# (fail open, not fail closed — this is metadata, not a benchmark
# result). NOTE: the exact chroot path may need adjustment on the first
# live fire (hb#311's "fire-as-linter" cost applies here too).
export BENCH_RUNSC_VERSION=$(kubectl debug "node/$gvisor_node" \
  --image=busybox -q -- chroot /host /home/kubernetes/bin/runsc --version \
  2>/dev/null | head -1 | awk '{print $NF}' || echo "")
echo "==> node_image=$BENCH_NODE_IMAGE runsc_version=$BENCH_RUNSC_VERSION"

echo "==> installing OSS controller from upstream main"
bash recipe/install-controller-from-main.sh

# controller_digest provenance (hb#439, mirrors the node-image/runsc pattern
# above): the resolved sha256 image digest of the LIVE controller container,
# not the floating `:latest-main` tag install-controller-from-main.sh applies
# — accrue_history.py keys the build-over-build history off this digest, so a
# same-tag-different-content rebuild upstream must still be distinguishable.
# `svc/agent-sandbox-controller` already exists as the stable lookup handle
# for this Deployment (reused from scripts/gvisor_warm_ttfe_sweep.py's own
# CTRL_NS/CTRL_SVC), so read its selector rather than hardcoding a label that
# could drift from upstream's manifest.
#
# This capture is LOAD-BEARING, not best-effort. install-controller-from-main.sh
# applies the base controller then the extensions controller LAST, both under the
# same Deployment name — a rolling update. Reading an arbitrary items[0] pod races
# the rollover: it can hit the still-Terminating base pod (wrong digest) or a
# not-yet-started replica whose .status.containerStatuses[].imageID is still empty
# (empty digest). hb#3918 (#447) makes an empty-digest-with-a-measured-COUNT a HARD
# fail (rc=3) at the accrual step downstream — the correct honest behavior, but it
# means a flaky capture REDs every refresh. So poll the NEWEST pod (the just-applied
# extensions replica) until imageID resolves to a shape-valid sha256 digest.
echo "==> capturing controller image digest for provenance"
kubectl -n agent-sandbox-system rollout status deploy/agent-sandbox-controller --timeout=180s || true
CTRL_SELECTOR=$(kubectl get svc agent-sandbox-controller -n agent-sandbox-system \
  -o jsonpath='{range $k,$v := .spec.selector}{$k}={$v},{end}' 2>/dev/null | sed 's/,$//' || true)
# imageID is `<repo>@sha256:<hex>` once resolved — strip everything up to and
# including the last `@` to leave the bare `sha256:<hex>` digest the schema
# expects (render/schema.py's _SHA256). Newest pod = the extensions replica that
# won the rolling update; older Terminating base pods sort earlier and are skipped.
BENCH_CONTROLLER_DIGEST=""
CONTROLLER_POD=""
for ((attempt=1; attempt<=12; attempt++)); do
  CONTROLLER_POD=$(kubectl get pods -n agent-sandbox-system -l "$CTRL_SELECTOR" \
    --sort-by=.metadata.creationTimestamp -o name 2>/dev/null | tail -1 | sed 's#^pod/##' || echo "")
  if [ -n "$CONTROLLER_POD" ]; then
    CONTROLLER_IMAGE_ID=$(kubectl get pod "$CONTROLLER_POD" -n agent-sandbox-system \
      -o jsonpath='{.status.containerStatuses[0].imageID}' 2>/dev/null || echo "")
    CAND=$(echo "$CONTROLLER_IMAGE_ID" | sed -E 's/^.*@//')
    if echo "$CAND" | grep -Eq '^sha256:[0-9a-f]{64}$'; then
      BENCH_CONTROLLER_DIGEST="$CAND"; break
    fi
  fi
  echo "==> controller digest not yet resolved (attempt $attempt/12, pod=${CONTROLLER_POD:-<none>}) — retrying in 5s"
  sleep 5
done
export BENCH_CONTROLLER_DIGEST
echo "==> controller_digest=${BENCH_CONTROLLER_DIGEST:-<empty>} (pod=${CONTROLLER_POD:-<none>})"

# hb#5396: deploy the upstream TTFE-true mutating webhook (asbx#761) so
# SandboxClaim CREATE is stamped with agents.x-k8s.io/webhook-first-observed-at,
# giving the harness a ms-precision t0 for ClaimStartupLatency (the true_ttfe
# basis). Runs AFTER the controller install (registers the SandboxClaim CRD +
# the agent-sandbox-system namespace the webhook needs) and BEFORE harness.run
# (the claims it measures must already be webhook-stamped).
echo "==> deploying TTFE-true webhook (asbx#761, hb#5396)"
bash recipe/deploy-ttfe-webhook.sh

echo "==> running sandbox harness (gke-sandbox / gVisor)"
python3 -m harness.run --product sandbox

kill "$NODE_SAMPLER_PID" 2>/dev/null || true
echo "==> hb-gvisor-pool node count over the measure window (hb#319 diagnostic, recap — the durable copy is the hb319-sample lines streamed above):"
cat "$NODE_SAMPLE_LOG" 2>/dev/null || echo "(no samples captured)"

# hb#3918/hb#439: upsert this build's burst_create COUNT into the build-over-build
# history BEFORE rendering, so render_trend has the just-written row available.
# Sole-writer contract (accrue_history.py), two outcomes on a non-write:
#   CASE 1 (exit 0, quiet) — latest.json carries no measurable burst_create cell (PASS or
#     FAIL — #546), so there is genuinely nothing to chart; a scenario-less fire never pollutes.
#   CASE 2 (exit 3, LOUD) — a COUNT *was* measured but the provenance fields above didn't
#     resolve (e.g. BENCH_CONTROLLER_DIGEST flaked to ""), so the row can't anchor to a
#     build. That is a trust-surface silent-degrade of alex's #1 throughput trend, so it
#     fails the step deliberately (the EXIT trap still tears the cluster down) rather than
#     freezing the trend while the fire reports green. Fix the digest capture, don't mask it.
echo "==> accruing build-over-build throughput history (hb#3918/hb#439)"
python3 -m render.accrue_history sandbox

echo "==> rendering README/DETAILS from results"
python3 -m render.generate

echo "==> public-safety gate (fail-closed)"
bash scripts/check-public-safety.sh
