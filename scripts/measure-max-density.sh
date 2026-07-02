#!/usr/bin/env bash
# Turnkey single-node max-density saturation probe for the #3868 Max Density column.
#
# Runs harness.scenarios.density_probe: packs warm sandboxes onto ONE pinned
# runtime-capable node until Ready plateaus against an Unschedulable backlog,
# then prints the probe report JSON and — on a saturated verdict — the two env
# exports the canonical warmpool_cold_start re-fire needs to emit
# density_per_vcpu (publication rides the canonical schema-validated fire; this
# probe never publishes a number itself).
#
# This script hardcodes NO cluster identity. The CALLER must export KUBECONFIG
# pointing at the target cluster BEFORE running. Run from the repo root.
#
#   export KUBECONFIG=/path/to/admin.kubeconfig
#   bash scripts/measure-max-density.sh                 # gVisor (default)
#   bash scripts/measure-max-density.sh kata            # Kata (nested-virt pool)
#
# Optional env overrides (sane defaults below / in the module):
#   BENCH_CLUSTER_SUBSTRATE          consistency guard (default gke-sandbox;
#                                    use gke-kata when probing kata)
#   DENSITY_PROBE_TARGET_NODE        pin a specific node (default: first capable)
#   DENSITY_PROBE_REPLICA_CEILING    oversubscription ceiling (default 120)
#   DENSITY_PROBE_HOLD_S             plateau hold window seconds (default 90)
#   DENSITY_PROBE_POLL_S             poll interval seconds (default 10)
#   DENSITY_PROBE_TIMEOUT_S          overall ceiling (default 1200)
#   BENCH_NAMESPACE                  target namespace (default: default)
set -euo pipefail

RUNTIME="${1:-gvisor}"

if [[ -z "${KUBECONFIG:-}" ]]; then
  echo "KUBECONFIG is not set — export it pointing at the target cluster first" >&2
  exit 2
fi

case "$RUNTIME" in
  gvisor) DEFAULT_SUBSTRATE="gke-sandbox" ;;
  kata|kata-*) DEFAULT_SUBSTRATE="gke-kata" ;;
  *)
    echo "usage: $0 [gvisor|kata]   (unknown runtime class: $RUNTIME)" >&2
    exit 2
    ;;
esac

export DENSITY_PROBE_RUNTIME_CLASS="$RUNTIME"
export BENCH_CLUSTER_SUBSTRATE="${BENCH_CLUSTER_SUBSTRATE:-$DEFAULT_SUBSTRATE}"

OUT="density-probe-${RUNTIME}-$(date -u +%Y%m%dT%H%M%SZ).json"

echo "==> density probe: runtime=$RUNTIME substrate=$BENCH_CLUSTER_SUBSTRATE" >&2
echo "==> report will be teed to $OUT" >&2

python3 -m harness.scenarios.density_probe | tee "$OUT"
