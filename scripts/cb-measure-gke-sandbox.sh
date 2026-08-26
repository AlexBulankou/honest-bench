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
# Ephemeral-cluster node floor (#6762 / hb#615 follow-up). Parameterized so the
# S27 controlled re-fire can vary node-count ALONE — fire at floor=1 vs floor=2,
# holding machine_type + controller-build constant — to isolate whether the
# #6743 warm-pool improvement came from the node-floor bump (#615) or a same-run
# controller-build change. Default 2 preserves the #6743 `min-nodes=2` pre-scale
# fix documented at the sampler block below, so a normal refresh is unchanged.
# MAX defaults to floor+1 (the historical 2->3 headroom) but can be pinned via
# HB_MAX_NODES so a clean single-variable fire holds max constant across both
# floors (e.g. HB_MAX_NODES=3 with HB_NUM_NODES 1 vs 2). The live
# BENCH_NODE_COUNT provenance capture (~line 343) still auto-reads the real pool
# size, so this control and that stamp can never disagree.
NUM_NODES="${HB_NUM_NODES:-2}"
MAX_NODES="${HB_MAX_NODES:-}"
if ! echo "$MAX_NODES" | grep -Eq '^[1-9][0-9]*$' || [ "$MAX_NODES" -lt "$NUM_NODES" ]; then
  MAX_NODES=$((NUM_NODES + 1))
fi
echo "==> ephemeral node floor=$NUM_NODES max=$MAX_NODES (HB_NUM_NODES/HB_MAX_NODES override)"

echo "==> creating ephemeral cluster $CLUSTER in $REGION"
gcloud container clusters create "$CLUSTER" \
  --region "$REGION" \
  --release-channel rapid \
  --machine-type "$BENCH_MACHINE_TYPE" \
  --node-locations "$REGION-a" \
  --num-nodes "$NUM_NODES" \
  --enable-autoscaling --min-nodes "$NUM_NODES" --max-nodes "$MAX_NODES" \
  --enable-network-policy \
  --no-enable-basic-auth --no-issue-client-certificate

echo "==> adding gVisor-sandboxed node pool to $CLUSTER"
gcloud container node-pools create hb-gvisor-pool \
  --cluster "$CLUSTER" \
  --region "$REGION" \
  --machine-type "$BENCH_MACHINE_TYPE" \
  --node-locations "$REGION-a" \
  --num-nodes "$NUM_NODES" \
  --enable-autoscaling --min-nodes "$NUM_NODES" --max-nodes "$MAX_NODES" \
  --sandbox=type=gvisor

gcloud container clusters get-credentials "$CLUSTER" --region "$REGION"

# Background node-count sampler (hb#319 diagnostic): warmpool_cold_start asks
# for 30 resident + 40 claims on hb-gvisor-pool. hb#318's "warm slower than
# cold" anomaly was root-caused (#6743) to single-node oversubscription: the
# pool floored at min-nodes=1, so 30 warm gVisor sandboxes + a 40-claim burst
# contended a single n2-standard-16, and a reactive GKE scale-up (VM boot +
# gVisor runtime init + kubelet join) could not land inside the ~9s burst
# window — smearing "warm" and "cold" together. The fix is the min-nodes=2
# PRE-SCALE above (reactive autoscale can't win a ~9s race; the floor has to
# be there before the burst). This sampler now serves as the regression guard:
# it should show a STEADY node count with no mid-burst scale event. It can't be
# reconstructed post-hoc (the cluster is ephemeral, no retained autoscaler
# event history), so sample hb-gvisor-pool's node count across the WHOLE measure
# phase and print it — confirms the floor held directly from the fire's build
# log, no separate debugging fire required. Best-effort: a sampler hiccup must
# never fail the measure step over a diagnostic.
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
# cluster by this step's EXIT trap (plus an explicit best-effort delete
# below). Best-effort throughout: a failure at any step here must never
# fail the measure step over a nice-to-have provenance field (fail open,
# not fail closed — this is metadata, not a benchmark result).
#
# hb#672: `kubectl debug`'s `--attach` flag defaults to FALSE unless
# `-i`/`-it` is passed, and neither was passed here — so the prior
# `$(kubectl debug ... -q -- ...)` form never streamed the container's
# stdout to the capture at all; it captured kubectl's OWN (here,
# -q-suppressed) status output, which is empty. That's why
# BENCH_RUNSC_VERSION was blank on 11/11 observed fires, not a flake.
# Fix: create the pod un-attached (the default), recover its
# kubectl-generated name from kubectl's own creation message (so `-q`
# must NOT be passed here), wait for it to reach a terminal phase, then
# pull the real command output via `kubectl logs`.
runsc_debug_create_out=$(kubectl debug "node/$gvisor_node" \
  --image=busybox -- chroot /host /home/kubernetes/bin/runsc --version \
  2>/dev/null || echo "")
runsc_debug_pod=$(echo "$runsc_debug_create_out" \
  | awk '/^Creating debugging pod/ {print $4}')
if [[ -n "$runsc_debug_pod" ]]; then
  kubectl wait --for=jsonpath='{.status.phase}'=Succeeded \
    "pod/$runsc_debug_pod" --timeout=60s >/dev/null 2>&1 || true
  export BENCH_RUNSC_VERSION=$(kubectl logs "$runsc_debug_pod" 2>/dev/null \
    | head -1 | awk '{print $NF}' || echo "")
  kubectl delete pod "$runsc_debug_pod" --ignore-not-found >/dev/null 2>&1 || true
else
  export BENCH_RUNSC_VERSION=""
fi
echo "==> node_image=$BENCH_NODE_IMAGE runsc_version=$BENCH_RUNSC_VERSION"

# --- controller install: prebuilt-upstream (default) or fork-source (opt-in) ---
# epic #6669 WS4. Default path (HB_FORK_BUILD unset/empty) is byte-for-byte the
# prior behavior — the standing daily hb-refresh-gke-sandbox fire is unchanged,
# so this block is INERT when the flag is absent. The operator opts into a
# fork-validation fire by setting HB_FORK_BUILD=1 (plus the HB_FORK_* inputs
# below) in the manual trigger's step env; then install-controller-from-main.sh
# ko-builds the controller from the fork instead of pulling upstream's prebuilt
# image, and the fork provenance is stamped so the renderer composes
# `source=fork@<sha> (+N fixes over upstream@<base>)` (harness build_provenance
# → render._fork_provenance_str).
#
# Fail-closed (trust surface, #4420): a REQUESTED fork build with any required
# input missing ABORTS the step rather than silently installing the upstream
# prebuilt — a fork fire that quietly measured upstream would still stamp a fork
# provenance that doesn't match what actually ran, which is exactly the silent
# information-downgrade the trust-surface idiom forbids. Better a loud abort.
if [ "${HB_FORK_BUILD:-}" = "1" ]; then
  echo "==> [fork-build] HB_FORK_BUILD=1 — building controller from fork source (epic #6669 WS4)"
  # KO_DOCKER_REPO is derived from the build's $PROJECT_ID at the step-config
  # level (CB substitutions can't be dereferenced in this extracted script — see
  # the header note) and passed in as HB_FORK_KO_DOCKER_REPO, so no literal
  # project name lands in this public-repo file.
  : "${HB_FORK_UPSTREAM_REPO:?HB_FORK_BUILD=1 requires HB_FORK_UPSTREAM_REPO (fork owner/repo, e.g. <fork-owner>/agent-sandbox)}"
  : "${HB_FORK_UPSTREAM_REF:?HB_FORK_BUILD=1 requires HB_FORK_UPSTREAM_REF (the fork integration branch/tag/sha)}"
  : "${HB_FORK_KO_DOCKER_REPO:?HB_FORK_BUILD=1 requires HB_FORK_KO_DOCKER_REPO (project AR push target; set via \$PROJECT_ID in the step env)}"
  : "${HB_FORK_SHA:?HB_FORK_BUILD=1 requires HB_FORK_SHA (fork HEAD sha, for provenance)}"
  : "${HB_FORK_BASE_UPSTREAM_SHA:?HB_FORK_BUILD=1 requires HB_FORK_BASE_UPSTREAM_SHA (upstream base sha the fork rebases on, for provenance)}"
  : "${HB_FORK_FIX_COUNT:?HB_FORK_BUILD=1 requires HB_FORK_FIX_COUNT (# staged fixes over the base, for provenance)}"

  # Fire-time diagnostic-lineage guard (hb#648 follow-up, hb#650 incident).
  # hb#648 added a PR-merge-time gate (scripts/check_diagnostic_lineage.py) that
  # blocks a committed latest.json carrying a diagnostic-shaped fork-build
  # signature (fork_fix_count==0 and/or fork_sha==fork_base_upstream_sha — a
  # "clean-upstream" reproduction, not a validated fix pin). That gate only
  # fires at merge time, on whatever PR happens to be open — it can't stop the
  # underlying `gcloud builds triggers run hb-refresh-gke-sandbox
  # --substitutions=...` dispatch from ever happening against the PRODUCTION
  # trigger in the first place. hb#650 proved the gap live: a manual dispatch
  # with fork_sha==fork_base_upstream_sha=="dfb50895..." and fork_fix_count=0
  # ran straight through this same script and opened a real "auto-refresh
  # headline" PR 7 minutes before the merge gate's trigger existed. Closing the
  # gap at the SOURCE (refuse before any cluster spend, not just before merge)
  # is the fail-closed half of the trust-surface idiom (#4420) the merge gate
  # already covers the reopen-loudly half of.
  #
  # Override: HB_FORK_ALLOW_DIAGNOSTIC=1 — for a deliberate, reviewed
  # diagnostic-reproduction fire (e.g. an #6890-style investigation rerun)
  # where publishing the diagnostic shape as a real PR is intentional and will
  # be caught/reviewed at merge time by the hb#648 gate as before.
  if [ "$HB_FORK_SHA" = "$HB_FORK_BASE_UPSTREAM_SHA" ] || [ "$HB_FORK_FIX_COUNT" = "0" ]; then
    if [ "${HB_FORK_ALLOW_DIAGNOSTIC:-}" != "1" ]; then
      echo "ERROR: [fork-build] diagnostic-lineage signature on this dispatch:" >&2
      [ "$HB_FORK_SHA" = "$HB_FORK_BASE_UPSTREAM_SHA" ] && \
        echo "  fork_sha == fork_base_upstream_sha == '$HB_FORK_SHA' (self-referential, not a real fork)" >&2
      [ "$HB_FORK_FIX_COUNT" = "0" ] && \
        echo "  fork_fix_count == 0 (a clean-upstream reproduction, not a validated fix pin)" >&2
      echo "  Refusing to dispatch against the production hb-refresh-gke-sandbox trigger." >&2
      echo "  Set HB_FORK_ALLOW_DIAGNOSTIC=1 if this diagnostic fire is deliberate." >&2
      exit 1
    fi
    echo "==> [fork-build] HB_FORK_ALLOW_DIAGNOSTIC=1 override — proceeding despite diagnostic-lineage signature"
  fi

  # install-controller-from-main.sh reads these as env overrides (BUILD_MODE=source).
  export BUILD_MODE=source
  export UPSTREAM_REPO="$HB_FORK_UPSTREAM_REPO"
  export UPSTREAM_REF="$HB_FORK_UPSTREAM_REF"
  export KO_DOCKER_REPO="$HB_FORK_KO_DOCKER_REPO"

  # harness.run build_provenance() reads these (omit-when-absent); on the default
  # path they stay unset, so the public page renders no source= leg (INERT).
  export BENCH_UPSTREAM_REF="$HB_FORK_UPSTREAM_REF"
  export BENCH_FORK_SHA="$HB_FORK_SHA"
  export BENCH_FORK_BASE_UPSTREAM_SHA="$HB_FORK_BASE_UPSTREAM_SHA"
  export BENCH_FORK_FIX_COUNT="$HB_FORK_FIX_COUNT"

  # BUILD_MODE=source needs BOTH a go toolchain AND ko, and the CB builder base
  # image (gcr.io/google.com/cloudsdktool/cloud-sdk) ships NEITHER. ko itself
  # shells out to `go build` to compile the controller, so installing ko alone is
  # insufficient — a ko-only image dies at `ko build` (that was the 2nd WS4 red:
  # ko installed fine, then `ko build` failed instantly because go was absent).
  # Install the go toolchain first (prebuilt tarball), then the ko release binary.
  # go1.26.4 matches the fork go.mod's `toolchain` directive so `go build` needs no
  # network toolchain fetch; GOTOOLCHAIN stays default (auto) so a future go.mod
  # bump self-heals by auto-downloading the required toolchain.
  if ! command -v go >/dev/null 2>&1; then
    echo "==> [fork-build] installing go toolchain (ko build requires it)"
    GO_VERSION=1.26.4
    curl -sSfL "https://go.dev/dl/go${GO_VERSION}.linux-amd64.tar.gz" \
      | tar -xz -C /usr/local
    # NB: point PATH at the toolchain bin dir via $GOROOT, not a literal path — a
    # literal path with a "go" segment immediately before the bin dir trips
    # check-public-safety.sh's internal-shortlink scanner as a false positive;
    # $GOROOT/bin avoids the flagged substring without touching the shared regex.
    GOROOT="/usr/local/go"
    export GOROOT
    export PATH="$GOROOT/bin:$PATH"
    command -v go >/dev/null 2>&1 || { echo "ERROR: [fork-build] go install failed" >&2; exit 1; }
  fi
  if ! command -v ko >/dev/null 2>&1; then
    echo "==> [fork-build] installing ko (BUILD_MODE=source requires it)"
    KO_VERSION=0.19.1
    curl -sSfL "https://github.com/ko-build/ko/releases/download/v${KO_VERSION}/ko_${KO_VERSION}_Linux_x86_64.tar.gz" \
      | tar -xz -C /usr/local/bin ko
    command -v ko >/dev/null 2>&1 || { echo "ERROR: [fork-build] ko install failed" >&2; exit 1; }
  fi
  echo "==> [fork-build] repo=$UPSTREAM_REPO ref=$UPSTREAM_REF ko_repo=$KO_DOCKER_REPO fork_sha=$HB_FORK_SHA base=$HB_FORK_BASE_UPSTREAM_SHA fixes=$HB_FORK_FIX_COUNT"
else
  echo "==> installing OSS controller from upstream main"
fi

# WS3 (epic #6669, "stamp-the-pin-per-fire"): capture the upstream sha the install
# resolves so the NON-FORK path can stamp BENCH_UPSTREAM_REF from it. The fork path
# already exports BENCH_UPSTREAM_REF (+BENCH_FORK_SHA) above; a plain upstream fire
# otherwise omits the pin even though install-controller-from-main.sh resolves a real
# sha. RESOLVED_SHA lives only inside that subprocess, so it hands the value back via
# this file. Set unconditionally (harmless for the fork path, which ignores it).
HB_RESOLVED_SHA_OUT="$(mktemp)"; export HB_RESOLVED_SHA_OUT

# hb#678: opt-in digest pin for a deliberate same-digest replicate-fire batch — pins the
# controller image install-controller-from-main.sh pulls BY DIGEST instead of the floating
# :latest-main tag, so repeat fires install a byte-identical image regardless of what upstream
# republishes in between. Only meaningful on the non-fork (BUILD_MODE=prebuilt) path — a fork
# build always ko-builds a fresh image from source, so there is no floating tag to pin.
if [ -n "${HB_PIN_CONTROLLER_DIGEST:-}" ]; then
  if [ "${HB_FORK_BUILD:-}" = "1" ]; then
    echo "ERROR: HB_PIN_CONTROLLER_DIGEST is not compatible with HB_FORK_BUILD=1 (fork builds always build a fresh image from source, there is no floating tag to pin)" >&2
    exit 1
  fi
  export PIN_IMAGE_DIGEST="$HB_PIN_CONTROLLER_DIGEST"
  echo "==> [hb#678] pinning controller image to digest: $PIN_IMAGE_DIGEST"
fi

bash recipe/install-controller-from-main.sh

# Non-fork path only: stamp BENCH_UPSTREAM_REF from the captured sha. Shape-validate
# as a git sha (schema render/schema.py _UPSTREAM_REF accepts a 40-hex sha) before
# exporting; a missing/blank/malformed capture leaves the pin unset (omit-when-absent,
# never guessed) rather than stamping garbage.
if [ "${HB_FORK_BUILD:-}" != "1" ]; then
  HB_RESOLVED_SHA="$(cat "$HB_RESOLVED_SHA_OUT" 2>/dev/null || true)"
  if echo "$HB_RESOLVED_SHA" | grep -Eq '^[0-9a-f]{7,40}$'; then
    export BENCH_UPSTREAM_REF="$HB_RESOLVED_SHA"
    echo "==> upstream_ref pin: BENCH_UPSTREAM_REF=$BENCH_UPSTREAM_REF (resolved from upstream main)"
  else
    echo "==> upstream_ref pin: no shape-valid sha captured — leaving BENCH_UPSTREAM_REF unset (page renders no source= leg)"
  fi
fi
rm -f "$HB_RESOLVED_SHA_OUT"

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

# BENCH_NODE_COUNT provenance (follow-up to #615): harness.run build_provenance() reads
# node_count from this env var and silent-defaults to 1 when it is unset (see
# run.py's finalize verify: "the fix is to set BENCH_NODE_COUNT correctly").
# This script never set it, so EVERY fire stamped node_count=1 in latest.json's
# provenance — including the post-#615 fires whose ephemeral cluster + gVisor
# pool are physically 2 nodes. That is a trust-surface mis-stamp: the published
# rig reports a topology it never ran on, and it doubly-confounded the #613
# warm-pool refresh comparison (the reader could not tell node-count 1->2 apart
# from a same-run controller-build change). Capture the LIVE gVisor-pool node
# count here — the pre-burst floor the warm pool is provisioned on, scoped to the
# same hb-gvisor-pool selector the #319 sampler uses so the two agree; a
# mid-burst reactive autoscale is a separate regression signal that sampler
# catches, not this provenance field. Guard the value: a non-positive/empty
# capture would re-poison the very provenance this fixes, so on a bad read leave
# BENCH_NODE_COUNT unset (harness keeps its default) and log loudly rather than
# export a bogus count. The trailing `|| true` is load-bearing: this runs at top
# level under `set -euo pipefail`, so a kubectl *error* (non-zero exit, not just
# empty output) would otherwise abort the whole measure script here before the
# empty/non-numeric guard below ever runs — `|| true` lets a bad read fall
# through to that guard instead of killing the fire (unlike the #319 sampler's
# copy of this pipeline, which is insulated inside a backgrounded subshell).
BENCH_NODE_COUNT=$(kubectl get nodes -l cloud.google.com/gke-nodepool=hb-gvisor-pool \
  --no-headers 2>/dev/null | wc -l | tr -d ' ' || true)
if echo "$BENCH_NODE_COUNT" | grep -Eq '^[1-9][0-9]*$'; then
  export BENCH_NODE_COUNT
  echo "==> node_count=$BENCH_NODE_COUNT (hb-gvisor-pool pre-burst floor — provenance, #615 follow-up)"
else
  echo "==> WARNING: node_count capture returned '${BENCH_NODE_COUNT:-<empty>}' — leaving BENCH_NODE_COUNT unset; provenance will silent-default (#615 follow-up)" >&2
  unset BENCH_NODE_COUNT
fi

# gVisor Max Density re-measure (hb#730, opt-in via _HB_DENSITY_PROBE substitution
# / HB_DENSITY_PROBE env, default empty = INERT — same "diagnostic add-on, standing
# fire unaffected" shape as _HB_PIN_CONTROLLER_DIGEST / _ALLOW_CELL_DOWNGRADE above).
# harness/scenarios/density_probe.py packs ONE already-provisioned hb-gvisor-pool
# node to plateau via a hostname-pinned SandboxWarmPool — it cannot trigger
# cluster-autoscaler scale-out (the pin names an existing node; a new node can never
# satisfy it) and self-cleans in a `finally` block regardless of verdict — which now
# also WAITS (DENSITY_PROBE_TEARDOWN_WAIT_S, default 180s) for the cascade-deleted
# probe pods to actually leave the target node before returning, not just for the
# delete calls to be issued (hb#731 fix: an issued-but-not-yet-terminated delete let
# burst_create's own WarmPool race dozens of still-terminating probe pods for the
# same node, degrading its claim-binding latency past the 1.0s TTFI ceiling and
# tripping the cell-downgrade guard on exec_success_rate/sandboxes_exec_under_1s —
# see honest-bench#730). Runs here, sequentially BEFORE the warmpool_cold_start
# burst below, so its pool is fully torn down (no node contention) before the burst
# fire claims capacity on the same node. On anything other than a clean "saturated"
# verdict, leave the two
# BENCH_DENSITY_* envs unset (same non-fabrication posture as the BENCH_NODE_COUNT
# guard above) — the harness/render path already treats a fire with no fresh
# density_per_vcpu as a normal, honest non-measurement (WORK_IN_PROGRESS.md
# "not-yet-measured"), never a downgrade of a value that was never produced here.
if [ "${HB_DENSITY_PROBE:-}" = "1" ]; then
  echo "==> hb#730: running gVisor Max Density saturation probe (hb-gvisor-pool)"
  DENSITY_PROBE_STDOUT="$(mktemp)"
  # density_probe.py's main() prints ONLY the JSON report to stdout and sends all
  # logging.info/error output to stderr (basicConfig's default StreamHandler) — the
  # split is deliberate so stdout stays pure-JSON. Capture stdout ALONE to the
  # tempfile below; let stderr stream straight to the build log unredirected (both
  # for live visibility and so a 2>&1 merge never corrupts the JSON we're about to
  # parse — a merged stream broke json.load() even on a genuine saturated verdict,
  # silently leaving BENCH_DENSITY_* unset every time (caught in hb#731 review).
  if DENSITY_PROBE_RUNTIME_CLASS=gvisor python3 -m harness.scenarios.density_probe \
      >"$DENSITY_PROBE_STDOUT"; then
    DENSITY_ENV="$(python3 -c '
import json, sys
try:
    report = json.load(open(sys.argv[1]))
    env = report.get("canonical_fire_env") or {}
    mc = env.get("BENCH_DENSITY_MAX_CONCURRENT")
    vpn = env.get("BENCH_DENSITY_ALLOCATABLE_VCPU_PER_NODE")
    if mc and vpn:
        print(f"{mc}\t{vpn}")
except Exception:
    pass
' "$DENSITY_PROBE_STDOUT" || true)"
    if [ -n "$DENSITY_ENV" ]; then
      BENCH_DENSITY_MAX_CONCURRENT="$(echo "$DENSITY_ENV" | cut -f1)"
      BENCH_DENSITY_ALLOCATABLE_VCPU_PER_NODE="$(echo "$DENSITY_ENV" | cut -f2)"
      export BENCH_DENSITY_MAX_CONCURRENT BENCH_DENSITY_ALLOCATABLE_VCPU_PER_NODE
      echo "==> density probe saturated: BENCH_DENSITY_MAX_CONCURRENT=$BENCH_DENSITY_MAX_CONCURRENT BENCH_DENSITY_ALLOCATABLE_VCPU_PER_NODE=$BENCH_DENSITY_ALLOCATABLE_VCPU_PER_NODE"
    else
      echo "==> WARNING: density probe exited 0 but no canonical_fire_env parsed — leaving BENCH_DENSITY_* unset (no fabricated density)" >&2
    fi
  else
    echo "==> WARNING: density probe did not saturate or failed — leaving BENCH_DENSITY_* unset (no density measured this fire). Probe stdout (JSON report; stderr/logs already streamed above):" >&2
    cat "$DENSITY_PROBE_STDOUT" >&2 || true
  fi
fi

# External true-TTFE warm step-up sweep, opt-in via
# _WARMPOOL_COLD_START_SWEEP_B64 substitution / HB_WARMPOOL_COLD_START_SWEEP_B64 env,
# default empty = INERT (same "diagnostic add-on, standing fire unaffected" shape as
# HB_DENSITY_PROBE above). gvisor_warm_ttfe_sweep.py fires offline against a
# persistent, shared standing GKE cluster (a different cluster than this ephemeral
# one), so its record is handed across as base64 rather than run inline. Decode it to a
# workspace file and export BENCH_SLO_SWEEP_WARMPOOL_COLD_START from it — harness/run.py's
# merge_slo_sweeps() reads that env var and fails closed (leaves the prior committed
# triple untouched via carry_prior_cluster_triples()) on anything empty/unreadable/
# malformed/runtime-mismatched, so a bad decode here is never a fabrication risk, only
# a missed opportunity to refresh the triple this fire.
if [ -n "${HB_WARMPOOL_COLD_START_SWEEP_B64:-}" ]; then
  SLO_SWEEP_OUT=/workspace/gvisor-warm-ttfe-sweep.json
  if echo "$HB_WARMPOOL_COLD_START_SWEEP_B64" | base64 -d >"$SLO_SWEEP_OUT" 2>/dev/null \
      && python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$SLO_SWEEP_OUT" 2>/dev/null; then
    export BENCH_SLO_SWEEP_WARMPOOL_COLD_START="$SLO_SWEEP_OUT"
    echo "==> external warm TTFE sweep wired in as BENCH_SLO_SWEEP_WARMPOOL_COLD_START=$SLO_SWEEP_OUT"
  else
    echo "==> WARNING: HB_WARMPOOL_COLD_START_SWEEP_B64 set but failed to decode to valid JSON — leaving BENCH_SLO_SWEEP_WARMPOOL_COLD_START unset (no fabricated triple; the prior committed triple carries forward via carry_prior_cluster_triples())" >&2
    rm -f "$SLO_SWEEP_OUT"
  fi
fi

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

# #6890 item 3: append-only, one row per fire (see accrue_warmpool_separation.py's docstring
# for why this is NOT an upsert-by-digest store like history.jsonl above). Same two-outcome
# non-write contract as accrue_history above:
#   CASE 1 (exit 0, quiet) — latest.json's warmpool_cold_start cell never computed the gate
#     metric, so there is genuinely nothing to chart; a scenario-less fire never pollutes.
#   CASE 2 (exit 3, LOUD) — a ratio *was* measured but a provenance field can't anchor it to a
#     build, so the row can't be trusted. Fails the step deliberately (the EXIT trap still tears
#     the cluster down) rather than freezing the same-build-variance trend while the fire is green.
echo "==> accruing warmpool separation-ratio measurement history (#6890 item 3)"
python3 -m render.accrue_warmpool_separation sandbox

echo "==> rendering README/DETAILS from results"
python3 -m render.generate

echo "==> public-safety gate (fail-closed)"
bash scripts/check-public-safety.sh
