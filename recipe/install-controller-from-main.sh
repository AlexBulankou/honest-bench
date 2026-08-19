#!/usr/bin/env bash
# install-controller-from-main.sh — install the agent-sandbox controller onto whatever
# cluster your kubectl context points at (a local `kind` cluster for the portable suite).
#
# This is the first step of the reproduce recipe in the top-level README. By default it is
# honest by construction: it installs the SAME upstream controller the benchmarks measure —
# no private fork, no internal image. The controller image is the prebuilt per-main-commit
# image that upstream kubernetes-sigs/agent-sandbox publishes to the public Kubernetes staging
# registry, and the manifests are fetched from upstream main, so "build from main" here means
# "pull the published main image + apply the matching upstream manifests" with no Go/ko
# toolchain needed.
#
# BUILD_MODE=source (epic #6669 WS4(b)) is the opt-in fork-build path: instead of pulling a
# prebuilt image, it ko-builds the controller from the requested UPSTREAM_REPO@UPSTREAM_REF
# (a fork owner/repo + branch/tag/SHA carrying staged fixes with no upstream-published image
# yet) and pushes to a caller-owned registry (KO_DOCKER_REPO) — mirroring the ko-build-from-
# source pattern already proven in substrate's demo upgrade-loop (k8s/substrate/cronjob.yaml
# KO_DOCKER_REPO). Default behavior (BUILD_MODE unset / "prebuilt") is unchanged.
#
# Usage:
#   recipe/install-controller-from-main.sh            # apply to the current kubectl context
#   recipe/install-controller-from-main.sh --dry-run  # fetch + render only, no cluster writes
#
#   # fork-build path (WS4(b)): build+push from a fork instead of pulling upstream's image
#   UPSTREAM_REPO=<fork-owner>/agent-sandbox UPSTREAM_REF=<fork-branch> \
#     BUILD_MODE=source KO_DOCKER_REPO=us-central1-docker.pkg.dev/<project>/<repo> \
#     recipe/install-controller-from-main.sh
#
# The caller owns the kubectl context. For the portable suite that is a local kind cluster:
#   kind create cluster
#   recipe/install-controller-from-main.sh
#
# A `kind` cluster has no gVisor runtime, so the gVisor-isolation scenario reports
# `pending (requires-gvisor-runtime)` rather than a false FAIL — that is the honest result on
# this substrate, and the build banner labels every number `cluster_substrate=kind`.

set -euo pipefail

# Overridable (WS4(b)) so a fork-build fire can point at a fork owner/repo + a caller-owned
# mirror registry without editing this file — defaults are the real upstream, unchanged.
UPSTREAM_REPO="${UPSTREAM_REPO:-kubernetes-sigs/agent-sandbox}"
# Public Kubernetes staging registry — the upstream-published controller image, not a fork.
# Only consulted in BUILD_MODE=prebuilt (the default); BUILD_MODE=source builds+pushes to
# KO_DOCKER_REPO instead and never reads this value.
STAGING_PREFIX="${STAGING_PREFIX:-us-central1-docker.pkg.dev/k8s-staging-images/agent-sandbox}"
# Floating to newest main, matching "built from main". Pin to a vYYYYMMDD-...-main tag and the
# matching commit SHA for a reproducible install.
UPSTREAM_REF="${UPSTREAM_REF:-main}"
IMAGE_TAG="${IMAGE_TAG:-latest-main}"
# "prebuilt" (default): pull the already-published upstream staging image — no toolchain
# needed. "source": ko-build the controller from the fetched UPSTREAM_REPO@UPSTREAM_REF tree
# and push to KO_DOCKER_REPO — the fork-validation path (WS4(b)), for fixes staged in a fork
# with no upstream-published image yet.
BUILD_MODE="${BUILD_MODE:-prebuilt}"
case "$BUILD_MODE" in
  prebuilt|source) ;;
  *) echo "[install-controller-from-main] ERROR: BUILD_MODE must be 'prebuilt' or 'source' (got: ${BUILD_MODE})" >&2; exit 2 ;;
esac

MODE="apply"
case "${1:-}" in
  "")        MODE="apply" ;;
  --dry-run) MODE="dry-run" ;;
  --apply)   MODE="apply" ;;
  *) echo "usage: $0 [--dry-run|--apply]" >&2; exit 2 ;;
esac

log() { echo "[install-controller-from-main] $*"; }
die() { echo "[install-controller-from-main] ERROR: $*" >&2; exit 1; }

for bin in curl tar sed find; do
  command -v "$bin" >/dev/null 2>&1 || die "missing required binary: $bin"
done
if [ "$MODE" = "apply" ]; then
  command -v kubectl >/dev/null 2>&1 || die "kubectl is required for --apply"
fi
if [ "$BUILD_MODE" = "source" ]; then
  command -v ko >/dev/null 2>&1 || die "BUILD_MODE=source requires the 'ko' binary (https://ko.build) on PATH"
  [ -n "${KO_DOCKER_REPO:-}" ] || die "BUILD_MODE=source requires KO_DOCKER_REPO (the registry to push the fork build to, e.g. us-central1-docker.pkg.dev/<project>/<repo>)"
  # A ko build inherently builds+pushes real images — there is no side-effect-free way to
  # preview a fork build the way prebuilt-mode's --dry-run previews manifest substitution.
  # Refuse rather than silently pushing on a flag whose name promises "no writes".
  [ "$MODE" = "dry-run" ] && die "BUILD_MODE=source has no --dry-run (a ko build always builds+pushes); use BUILD_MODE=prebuilt --dry-run to preview manifest substitution only"
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# 1. fetch upstream manifests at the requested ref ---------------------------
TARBALL_URL="https://github.com/${UPSTREAM_REPO}/archive/${UPSTREAM_REF}.tar.gz"
log "fetching ${UPSTREAM_REPO}@${UPSTREAM_REF}"
curl -fsSL "$TARBALL_URL" -o "${WORK}/src.tar.gz" || die "tarball fetch failed: $TARBALL_URL"
tar -xzf "${WORK}/src.tar.gz" -C "$WORK"
SRC="$(find "$WORK" -maxdepth 1 -type d -name 'agent-sandbox-*' | head -1)"
[ -n "$SRC" ] && [ -d "${SRC}/k8s" ] || die "extracted tree has no k8s/ dir — upstream layout changed"
RESOLVED_SHA="${SRC##*-}"
log "resolved tree: $(basename "$SRC")"

# WS3 (epic #6669, "stamp-the-pin-per-fire"): if the caller sets HB_RESOLVED_SHA_OUT,
# write the resolved upstream sha to that path so a parent process (e.g.
# cb-measure-gke-sandbox.sh's non-fork path) can stamp BENCH_UPSTREAM_REF from it
# — RESOLVED_SHA is resolved only inside this subprocess, so a bare export wouldn't
# propagate. Additive + omit-when-absent: unset env is a no-op for existing callers.
if [ -n "${HB_RESOLVED_SHA_OUT:-}" ]; then
  printf '%s' "$RESOLVED_SHA" > "$HB_RESOLVED_SHA_OUT" \
    && log "wrote resolved sha to HB_RESOLVED_SHA_OUT=${HB_RESOLVED_SHA_OUT}" \
    || log "WARN: failed to write HB_RESOLVED_SHA_OUT=${HB_RESOLVED_SHA_OUT} (non-fatal)"
fi

# 2. resolve ko:// image placeholders to a real, pullable image --------------
MANIFEST_DIR="${SRC}/k8s"
if [ "$BUILD_MODE" = "source" ]; then
  log "BUILD_MODE=source — building controller image(s) from ${UPSTREAM_REPO}@${UPSTREAM_REF} via ko (push target: ${KO_DOCKER_REPO})"
  [ -f "${SRC}/go.mod" ] || die "no go.mod at tree root — cannot ko-build from source (upstream layout changed?)"
  # Generic — unlike prebuilt mode's /cmd/<name> map, a fork build has no staging-registry
  # naming convention to lean on, so every distinct ko:// import path found in the manifests
  # is built individually and substituted with its own resolved ref.
  mapfile -t KO_IMPORT_PATHS < <(grep -rhoE 'ko://[^[:space:]"'"'"']+' "$MANIFEST_DIR" | sort -u)
  [ "${#KO_IMPORT_PATHS[@]}" -gt 0 ] || die "no ko:// image references found under ${MANIFEST_DIR} — nothing to build"
  export KO_DOCKER_REPO
  for ko_path in "${KO_IMPORT_PATHS[@]}"; do
    import_path="${ko_path#ko://}"
    log "ko building ${import_path}"
    # ko prints build progress to stderr and the final pushed image ref as the last stdout
    # line; `--bare` skips the platform-suffixed tagging ko otherwise appends.
    # Capture the FULL output (not `| tail -1`) so a ko failure is visible: under
    # `set -euo pipefail`, piping to tail masks ko's non-zero exit AND discards its
    # diagnostics, so the script would die at the assignment with an empty log (that
    # was the WS4 go-toolchain-missing red — ko failed instantly with nothing printed).
    if ! ko_out="$(cd "$SRC" && ko build --bare "$import_path" 2>&1)"; then
      echo "$ko_out" >&2
      die "ko build for ${import_path} failed (see ko output above)"
    fi
    built_ref="$(printf '%s\n' "$ko_out" | tail -1)"
    case "$built_ref" in
      "${KO_DOCKER_REPO}"*) ;;
      *) echo "$ko_out" >&2; die "ko build for ${import_path} did not return an image ref under ${KO_DOCKER_REPO} (see ko output above)" ;;
    esac
    log "  -> ${built_ref}"
    esc_ko_path=$(printf '%s' "$ko_path" | sed -e 's/[&\]/\\&/g')
    esc_built_ref=$(printf '%s' "$built_ref" | sed -e 's/[&\]/\\&/g')
    while IFS= read -r -d '' f; do
      sed -i "s#${esc_ko_path}#${esc_built_ref}#g" "$f"
    done < <(find "$MANIFEST_DIR" -name '*.yaml' -print0)
  done
  log "controller image(s) after source build:"
  grep -rh 'image:[[:space:]]*'"${KO_DOCKER_REPO}" "$MANIFEST_DIR" | sed 's/^[[:space:]]*/    /' | sort -u || true
else
  # ko names the image after the cmd basename; the staging registry publishes under the same
  # basename. Generic map: ko://<anything>/cmd/<name> -> <STAGING_PREFIX>/<name>:<IMAGE_TAG>.
  log "substituting ko:// -> ${STAGING_PREFIX}/<cmd>:${IMAGE_TAG}"
  while IFS= read -r -d '' f; do
    sed -i -E "s#ko://[^[:space:]\"']*/cmd/([A-Za-z0-9._-]+)#${STAGING_PREFIX//#/\\#}/\1:${IMAGE_TAG}#g" "$f"
  done < <(find "$MANIFEST_DIR" -name '*.yaml' -print0)
  log "controller image(s) after substitution:"
  grep -rh 'image:[[:space:]]*'"${STAGING_PREFIX}" "$MANIFEST_DIR" | sed 's/^[[:space:]]*/    /' | sort -u || true
fi
# Check only actual `image:` fields, not any mention of the string "ko://" — upstream's
# k8s/kustomization.yaml carries a prose comment describing the ko:// release-tooling mechanism
# ("... with the ko:// image replaced by the published controller image.") that is not itself a
# placeholder and never gets (or needs) substitution. A bare `grep 'ko://'` false-positives on
# that comment even when every real image: field substituted cleanly.
if grep -rEqn '^\s*image:\s*ko://' "$MANIFEST_DIR"; then
  grep -rEn '^\s*image:\s*ko://' "$MANIFEST_DIR" >&2
  die "unsubstituted ko:// image: references remain (build/substitution missed one)"
fi

CRD_DIR="${MANIFEST_DIR}/crds"
[ -d "$CRD_DIR" ] || die "no crds/ dir under k8s/ — upstream layout changed"

if [ "$MODE" = "dry-run" ]; then
  log "DRY-RUN — rendered manifests under ${MANIFEST_DIR} (no cluster writes)"
  log "CRDs:";                     find "$CRD_DIR" -name '*.yaml' -printf '    %f\n'
  log "top-level k8s manifests:";  find "$MANIFEST_DIR" -maxdepth 1 -name '*.yaml' -printf '    %f\n'
  if command -v kubectl >/dev/null 2>&1 && kubectl version --client >/dev/null 2>&1; then
    kubectl apply --dry-run=client -f "$CRD_DIR" >/dev/null 2>&1 \
      && log "client dry-run of CRDs: OK" \
      || log "client dry-run skipped (no reachable context)"
  fi
  exit 0
fi

# 3. apply — CRDs first, then RBAC + controllers -----------------------------
log "applying CRDs"
kubectl apply -f "$CRD_DIR"

# Upstream ships two top-level controller Deployments that share the name
# `agent-sandbox-controller`: the base controller (core reconcilers only) and the extensions
# controller (a strict superset that adds `--extensions`, which is what starts the
# SandboxClaim / SandboxWarmPool / SandboxTemplate reconcilers the suite exercises). Apply
# order decides which wins the same-name overwrite, so apply every other manifest first and the
# extensions controller LAST — deterministically the winner — else claim-creating scenarios
# would time out against a controller that only reconciles kind=Sandbox.
EXT_CONTROLLER="${MANIFEST_DIR}/extensions.controller.yaml"
[ -f "$EXT_CONTROLLER" ] \
  || die "expected extensions.controller.yaml in upstream k8s/ — upstream layout changed; re-check the same-name-Deployment overwrite before applying."
log "applying RBAC + base controller manifests (extensions controller applied last)"
# kustomization.yaml is a kustomize-only config file, not a real API resource — it lives at
# the same top level as the real manifests (for the all-in-one `kubectl kustomize`/GitOps
# consumer path) but `kubectl apply -f` on it always fails with "no matches for kind
# Kustomization". Exclude it alongside extensions.controller.yaml, else `xargs` sees one
# failing invocation, returns 123, and `set -e` aborts the whole install after this point.
find "$MANIFEST_DIR" -maxdepth 1 -name '*.yaml' ! -name 'extensions.controller.yaml' ! -name 'kustomization.yaml' -print0 \
  | xargs -0 -I{} kubectl apply -f {}
log "applying extensions controller LAST (deterministic --extensions winner)"
kubectl apply -f "$EXT_CONTROLLER"

# Post-apply assertion: the live Deployment must carry --extensions, else the
# claim/warmpool/template reconcilers silently never start.
log "verifying live controller carries --extensions"
LIVE_ARGS="$(kubectl get deploy agent-sandbox-controller -n agent-sandbox-system \
  -o jsonpath='{.spec.template.spec.containers[0].args}' 2>/dev/null || true)"
case "$LIVE_ARGS" in
  *--extensions*) log "OK — live controller args include --extensions" ;;
  *) die "live agent-sandbox-controller is missing --extensions (args=${LIVE_ARGS:-<none>}) — the SandboxClaim/WarmPool/Template reconcilers will not start. Refusing to report a successful install." ;;
esac

if [ "$BUILD_MODE" = "source" ]; then
  log "installed agent-sandbox controller built from ${UPSTREAM_REPO}@${UPSTREAM_REF} (sha ${RESOLVED_SHA}, pushed to ${KO_DOCKER_REPO})"
else
  log "installed agent-sandbox controller from upstream ${UPSTREAM_REF} (image tag ${IMAGE_TAG}, sha ${RESOLVED_SHA})"
fi
# Lightweight provenance signal (WS4(c), #6669) — repo/ref/sha/mode is what a results renderer
# needs to stamp `fork@<sha> (+N fixes over upstream@<sha>)`; the "+N fixes" delta itself needs
# a repo-compare call against a known upstream baseline, which is a rendering-time concern (it
# has nothing to do with installing the controller) and is left to the consumer of this line.
log "provenance: mode=${BUILD_MODE} repo=${UPSTREAM_REPO} ref=${UPSTREAM_REF} sha=${RESOLVED_SHA}"
log "next: python3 -m harness.run   # run the portable suite (substrate=kind)"
