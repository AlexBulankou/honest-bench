#!/usr/bin/env bash
# deploy-ttfe-webhook.sh — deploy the upstream TTFE-true mutating webhook onto whatever
# cluster your kubectl context points at, so ClaimStartupLatency (ms-precision t0) can be
# measured. Stamps `agents.x-k8s.io/webhook-first-observed-at` on SandboxClaim CREATE.
#
# WHY THIS EXISTS: the literal-TTFE basis derives t0 from `creationTimestamp` (second-
# truncated) → TTFE is an UPPER BOUND, and at the <1s North Star bar every rung's literal
# p95 upper bound sits over 1s ⇒ `no-compliant-rung` ⇒ honest-empty. The webhook gives a
# ms-precision t0 ⇒ the harness `true_ttfe` basis, which may clear 1s at a rung ⇒ an honest
# positive rate instead of honest-empty. See honest-bench#5396.
#
# HONEST BY CONSTRUCTION: the webhook is the UNMODIFIED upstream example
# (kubernetes-sigs/agent-sandbox examples/webhook-inject-timestamp, merged asbx#761), vendored
# under recipe/ttfe-webhook/ at a pinned commit (see recipe/ttfe-webhook/SOURCE.md). The image
# is pre-built from that pinned source and pinned by digest-tag — no per-fire build, no fork.
#
# ONE SCRIPT, TWO CALLERS (honest-bench#5396):
#   - the ephemeral gVisor refresh (cloudbuild-refresh-gke-sandbox.yaml), between
#     install-controller-from-main.sh and harness.run; and
#   - a manual apply against the persistent sandbox-scenarios-cluster kata-microvm-pool
#     before the Kata unique-image-cold cell fires.
#
# PRECONDITION: the agent-sandbox controller is already installed (registers the SandboxClaim
# CRD + the agent-sandbox-system namespace). Run install-controller-from-main.sh FIRST.
#
# Usage:
#   recipe/deploy-ttfe-webhook.sh            # install cert-manager + apply to current context
#   recipe/deploy-ttfe-webhook.sh --dry-run  # print what would apply, no cluster writes
#
# Env overrides:
#   CERT_MANAGER_VERSION   cert-manager release tag to install (default below)
#   WEBHOOK_NAMESPACE      namespace the webhook deploys into (default agent-sandbox-system)
#   ROLLOUT_TIMEOUT        kubectl rollout/wait timeout (default 180s)

set -euo pipefail

CERT_MANAGER_VERSION="${CERT_MANAGER_VERSION:-v1.21.0}"
WEBHOOK_NAMESPACE="${WEBHOOK_NAMESPACE:-agent-sandbox-system}"
ROLLOUT_TIMEOUT="${ROLLOUT_TIMEOUT:-180s}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENDOR="$HERE/ttfe-webhook"

MODE="apply"
case "${1:-}" in
  "")        MODE="apply" ;;
  --apply)   MODE="apply" ;;
  --dry-run) MODE="dry-run" ;;
  *) echo "usage: $0 [--dry-run|--apply]" >&2; exit 2 ;;
esac

log() { echo "[deploy-ttfe-webhook] $*"; }
die() { echo "[deploy-ttfe-webhook] ERROR: $*" >&2; exit 1; }

command -v kubectl >/dev/null 2>&1 || die "kubectl is required"
for f in cert-manager-resources.yaml cert-manager-rbac.yaml mutating-webhook-configuration.yaml webhook-deployment.yaml; do
  [ -f "$VENDOR/$f" ] || die "missing vendored manifest: $VENDOR/$f"
done

CM_URL="https://github.com/cert-manager/cert-manager/releases/download/${CERT_MANAGER_VERSION}/cert-manager.yaml"

if [ "$MODE" = "dry-run" ]; then
  log "[dry-run] would install cert-manager ${CERT_MANAGER_VERSION} from ${CM_URL}"
  log "[dry-run] would apply: cert-manager-rbac.yaml, cert-manager-resources.yaml, webhook-deployment.yaml, mutating-webhook-configuration.yaml (ns=${WEBHOOK_NAMESPACE})"
  exit 0
fi

# Precondition: controller-owned namespace + SandboxClaim CRD must already exist.
kubectl get namespace "$WEBHOOK_NAMESPACE" >/dev/null 2>&1 \
  || die "namespace $WEBHOOK_NAMESPACE not found — run recipe/install-controller-from-main.sh first"
kubectl get crd sandboxclaims.extensions.agents.x-k8s.io >/dev/null 2>&1 \
  || die "SandboxClaim CRD not found — run recipe/install-controller-from-main.sh first"

# 1. cert-manager (the webhook's Certificate/Issuer + caBundle injection depend on it).
log "installing cert-manager ${CERT_MANAGER_VERSION}"
kubectl apply -f "$CM_URL"
log "waiting for cert-manager rollout"
for d in cert-manager cert-manager-webhook cert-manager-cainjector; do
  kubectl -n cert-manager rollout status "deploy/$d" --timeout="$ROLLOUT_TIMEOUT"
done
# leaderelection Role/RoleBinding the example ships (some cert-manager builds need it).
kubectl apply -f "$VENDOR/cert-manager-rbac.yaml"

# 2. self-signed Issuer + Certificate → produces the webhook-certs secret.
log "applying Issuer + Certificate"
kubectl apply -f "$VENDOR/cert-manager-resources.yaml"
log "waiting for webhook Certificate to be Ready"
kubectl -n "$WEBHOOK_NAMESPACE" wait --for=condition=Ready certificate/webhook-certificate --timeout="$ROLLOUT_TIMEOUT"

# 3. webhook Deployment + Service (pinned image).
log "applying webhook Deployment + Service"
kubectl apply -f "$VENDOR/webhook-deployment.yaml"
kubectl -n "$WEBHOOK_NAMESPACE" rollout status deploy/webhook-deployment --timeout="$ROLLOUT_TIMEOUT"

# 4. MutatingWebhookConfiguration (cert-manager injects the caBundle via inject-ca-from).
log "applying MutatingWebhookConfiguration"
kubectl apply -f "$VENDOR/mutating-webhook-configuration.yaml"

log "done — SandboxClaim CREATE will now be stamped with agents.x-k8s.io/webhook-first-observed-at"
