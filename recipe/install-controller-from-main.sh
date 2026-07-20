#!/usr/bin/env bash
# install-controller-from-main.sh — install the OSS agent-sandbox controller, built from
# upstream main, onto whatever cluster your kubectl context points at (a local `kind`
# cluster for the portable suite).
#
# This is the first step of the reproduce recipe in the top-level README. It is honest by
# construction: it installs the SAME upstream controller the benchmarks measure — no private
# fork, no internal image. The controller image is the prebuilt per-main-commit image that
# upstream kubernetes-sigs/agent-sandbox publishes to the public Kubernetes staging registry,
# and the manifests are fetched from upstream main, so "build from main" here means "pull the
# published main image + apply the matching upstream manifests" with no Go/ko toolchain needed.
#
# Usage:
#   recipe/install-controller-from-main.sh            # apply to the current kubectl context
#   recipe/install-controller-from-main.sh --dry-run  # fetch + render only, no cluster writes
#
# The caller owns the kubectl context. For the portable suite that is a local kind cluster:
#   kind create cluster
#   recipe/install-controller-from-main.sh
#
# A `kind` cluster has no gVisor runtime, so the gVisor-isolation scenario reports
# `pending (requires-gvisor-runtime)` rather than a false FAIL — that is the honest result on
# this substrate, and the build banner labels every number `cluster_substrate=kind`.

set -euo pipefail

UPSTREAM_REPO="kubernetes-sigs/agent-sandbox"
# Public Kubernetes staging registry — the upstream-published controller image, not a fork.
STAGING_PREFIX="us-central1-docker.pkg.dev/k8s-staging-images/agent-sandbox"
# Floating to newest main, matching "built from main". Pin to a vYYYYMMDD-...-main tag and the
# matching commit SHA for a reproducible install.
UPSTREAM_REF="${UPSTREAM_REF:-main}"
IMAGE_TAG="${IMAGE_TAG:-latest-main}"

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

# 2. substitute ko:// image placeholders with the published staging image ----
# ko names the image after the cmd basename; the staging registry publishes under the same
# basename. Generic map: ko://<anything>/cmd/<name> -> <STAGING_PREFIX>/<name>:<IMAGE_TAG>.
MANIFEST_DIR="${SRC}/k8s"
log "substituting ko:// -> ${STAGING_PREFIX}/<cmd>:${IMAGE_TAG}"
while IFS= read -r -d '' f; do
  sed -i -E "s#ko://[^[:space:]\"']*/cmd/([A-Za-z0-9._-]+)#${STAGING_PREFIX//#/\\#}/\1:${IMAGE_TAG}#g" "$f"
done < <(find "$MANIFEST_DIR" -name '*.yaml' -print0)
# Check only actual `image:` fields, not any mention of the string "ko://" — upstream's
# k8s/kustomization.yaml carries a prose comment describing the ko:// release-tooling mechanism
# ("... with the ko:// image replaced by the published controller image.") that is not itself a
# placeholder and never gets (or needs) substitution. A bare `grep 'ko://'` false-positives on
# that comment even when every real image: field substituted cleanly.
if grep -rEqn '^\s*image:\s*ko://' "$MANIFEST_DIR"; then
  grep -rEn '^\s*image:\s*ko://' "$MANIFEST_DIR" >&2
  die "unsubstituted ko:// image: references remain (cmd-basename map missed one)"
fi
log "controller image(s) after substitution:"
grep -rh 'image:[[:space:]]*'"${STAGING_PREFIX}" "$MANIFEST_DIR" | sed 's/^[[:space:]]*/    /' | sort -u || true

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
find "$MANIFEST_DIR" -maxdepth 1 -name '*.yaml' ! -name 'extensions.controller.yaml' -print0 \
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

log "installed agent-sandbox controller from upstream ${UPSTREAM_REF} (image tag ${IMAGE_TAG}, sha ${RESOLVED_SHA})"
log "next: python3 -m harness.run   # run the portable suite (substrate=kind)"
