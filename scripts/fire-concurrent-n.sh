#!/usr/bin/env bash
# Turnkey concurrent-N warm/cold TTFE fire for the #4021 scale benchmark.
#
# Fires the warmpool_cold_start scenario as a single-scenario run at a chosen
# concurrency N, in either warm-pool mode (every claim served from a fully warm
# pool) or cold mode (POOL_REPLICAS=0, every claim overflows to a cold-provision),
# with the create->first-instruction TTFE probe armed (BENCH_TTFE_EXEC=1, gVisor).
# Prints the matrix-cell numbers (thpt_under_5s/1s_per_node, ttfe_p50/p95_ms,
# exec_success_rate, n) and tees the full result to a timestamped JSON file.
#
# This script hardcodes NO cluster identity. The CALLER must export KUBECONFIG
# pointing at the target cluster (the admin kubeconfig — pods/exec RBAC is needed
# for the TTFE probe) BEFORE running. Run from the repo root.
#
#   export KUBECONFIG=/path/to/admin.kubeconfig
#   bash scripts/fire-concurrent-n.sh warm 300
#   bash scripts/fire-concurrent-n.sh cold 300
#   bash scripts/fire-concurrent-n.sh warm 500
#   bash scripts/fire-concurrent-n.sh cold 500
#
# Optional env overrides (sane defaults below):
#   BENCH_NODE_COUNT                 per-node throughput denominator (default 20)
#   BENCH_NAMESPACE                  target namespace (default: default)
#   FIRE_TIMEOUT_S                   warmup + per-claim-bind ceiling (default 900)
#   BENCH_DENSITY_MAX_CONCURRENT     optional density numerator (omitted if unset)
#   BENCH_DENSITY_ALLOCATABLE_VCPU_PER_NODE  optional density basis (omitted if unset)
set -euo pipefail

MODE="${1:-}"
N="${2:-}"
if [[ "$MODE" != "warm" && "$MODE" != "cold" ]] || ! [[ "$N" =~ ^[0-9]+$ ]]; then
  echo "usage: $0 <warm|cold> <N>   (e.g. $0 warm 300)" >&2
  exit 2
fi

if [[ "$MODE" == "warm" ]]; then
  POOL_REPLICAS="$N"
else
  POOL_REPLICAS=0
fi

TIMEOUT_S="${FIRE_TIMEOUT_S:-900}"
NODE_COUNT="${BENCH_NODE_COUNT:-20}"
NS="${BENCH_NAMESPACE:-default}"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="concurrent-n-${MODE}-${N}-${TS}.json"

export BENCH_CLUSTER_SUBSTRATE=gke-sandbox
export WARMPOOL_COLD_START_RUNTIME_CLASS=gvisor
export BENCH_TTFE_EXEC=1
export BENCH_NODE_COUNT="$NODE_COUNT"
export BENCH_NAMESPACE="$NS"
export WARMPOOL_COLD_START_POOL_REPLICAS="$POOL_REPLICAS"
export WARMPOOL_COLD_START_CLAIM_COUNT="$N"
export WARMPOOL_COLD_START_WARMUP_TIMEOUT_S="$TIMEOUT_S"
export WARMPOOL_COLD_START_BIND_TIMEOUT_S="$TIMEOUT_S"

echo "=== #4021 concurrent-N fire: mode=${MODE} N=${N} (POOL_REPLICAS=${POOL_REPLICAS}) ==="
echo "    substrate=gke-sandbox runtime=gvisor TTFE=on node_count=${NODE_COUNT} ns=${NS} timeout=${TIMEOUT_S}s"
echo "    KUBECONFIG=${KUBECONFIG:-<unset! export it first>}"
echo "    result -> ${OUT}"
echo

python3 - "$OUT" "$MODE" "$N" <<'PY'
import json, sys
from harness.scenarios import warmpool_cold_start as w

out_path, mode, n = sys.argv[1], sys.argv[2], sys.argv[3]
outcome, excerpt, cells = w.run("warmpool-cold-start")
record = {
    "scenario": "warmpool-cold-start",
    "mode": mode,
    "n_requested": int(n),
    "outcome": outcome,
    "cells": cells,
    "excerpt": excerpt,
}
with open(out_path, "w") as f:
    json.dump(record, f, indent=2, sort_keys=True)
    f.write("\n")
print("OUTCOME:", outcome)
print("CELLS:", json.dumps(cells, indent=2, sort_keys=True))
print("\n(excerpt — latency summary, written to file)")
print(excerpt)
PY

echo
echo "=== done: ${MODE}-${N} -> ${OUT} ==="
