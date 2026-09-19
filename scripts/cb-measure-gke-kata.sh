#!/usr/bin/env bash
# Extracted from cloudbuild-refresh-gke-kata.yaml's `measure` step (was an
# inline `args: [-c, |...]` block scalar). The inline script grew past Cloud
# Build's per-arg 10000-character limit once the hb#8751 warm-sweep
# passthrough block landed (10704 chars at extraction time, confirmed via
# `python3 -c "import yaml; ..."` measuring the loaded step's args string)
# and the trigger stopped firing at all — "invalid .steps field: build step
# 1 arg 1 too long (max: 10000)" on ANY invocation, not just a
# substitution-specific one. Same failure class, same fix as the gVisor
# sibling's earlier extraction (scripts/cb-measure-gke-sandbox.sh): keep the
# logic, move it out of the YAML so the step's own `args` collapses to a
# short file reference.
#
# Because this now runs as a real script (not YAML text CB's substitution
# engine scans), CB substitutions (${_CLUSTER} etc.) can no longer be
# dereferenced inline here — they must be resolved at the step-config level
# via `env:` and read here as plain shell env vars (HB_CLUSTER / HB_REGION;
# BENCH_MACHINE_TYPE was already an env var pre-extraction, reused as-is).
# Likewise, the doubled `$$` that CB-embedded scripts require (to survive
# the substitution scanner) is gone — single `$` is correct here, same as
# any normal bash script.
set -euo pipefail

CLUSTER="$HB_CLUSTER"
MACHINE_TYPE="$BENCH_MACHINE_TYPE"
REGION="$HB_REGION"
if [ -z "$CLUSTER" ]; then
  echo "==> FATAL: _CLUSTER substitution is empty. Pass the kata scenarios cluster name at fire time:"
  echo "==>   gcloud builds triggers run hb-refresh-gke-kata --project=<PROJECT> --substitutions=_CLUSTER=<cluster>,_MACHINE_TYPE=<machine-type>,_REGION=us-central1"
  echo "==> (the internal cluster name is intentionally not shipped in this public config.)"
  exit 1
fi
if [ -z "$MACHINE_TYPE" ]; then
  echo "==> FATAL: _MACHINE_TYPE substitution is empty. Pass the kata pool's machine type at fire time:"
  echo "==>   gcloud builds triggers run hb-refresh-gke-kata --project=<PROJECT> --substitutions=_CLUSTER=<cluster>,_MACHINE_TYPE=<machine-type>,_REGION=us-central1"
  echo "==> (hb#835: without this stamp, a rendered cell can't be compared against a different rig's cell without drifting silently.)"
  exit 1
fi

# Kill the background node sampler on any exit path. There is NO cluster
# teardown here — the standing cluster is never provisioned or deleted by
# this build (the deliberate MINUS vs. the ephemeral gVisor refresh).
cleanup() {
  kill "${NODE_SAMPLER_PID:-}" 2>/dev/null || true
}
trap cleanup EXIT

# kubeconfig isolated to /workspace so we never touch a shared path.
export KUBECONFIG=/workspace/hb-refresh.kubeconfig

echo "==> authenticating to the persistent kata scenarios cluster (NO create — standing cluster)"
gcloud container clusters get-credentials "$CLUSTER" --region "$REGION"

# READ-ONLY precondition: the standing controller must already be
# installed. This build never installs or mutates the operator on the
# shared cluster — it only asserts the fulfilment chain is present before
# firing real claims against it.
echo "==> read-only precondition: SandboxClaim CRD + agent-sandbox-controller Deployment present"
kubectl get crd sandboxclaims.extensions.agents.x-k8s.io >/dev/null
kubectl -n agent-sandbox-system get deploy agent-sandbox-controller >/dev/null

# COLD-HONESTY GUARD (the pre-fire assertion #5459 exists to automate): a
# genuine cold true_ttfe requires the kata pool scaled to ZERO resident
# nodes, so the first claim pays the real VM-boot + kata-runtime-init cost
# against an empty containerd cache. A non-zero count means either a warm
# cache (would understate cold ttfe) OR a concurrent workload (a collision
# on the shared cluster) — both are fatal. Keyed on the PUBLIC upstream
# kata-containers node label, never the internal pool name.
echo "==> cold-honesty guard: asserting 0 resident kata nodes before trusting 'cold'"
kata_nodes=$(kubectl get nodes -l katacontainers.io/kata-runtime=true --no-headers 2>/dev/null | wc -l | tr -d ' ')
if [ "$kata_nodes" != "0" ]; then
  echo "==> FATAL: $kata_nodes resident kata node(s) — pool is not cold (warm cache or a concurrent workload)."
  echo "==> A cold true_ttfe refresh requires the kata pool scaled to 0. Confirm the collision-ack window is clear and the pool has drained, then re-fire."
  kubectl get nodes -l katacontainers.io/kata-runtime=true || true
  exit 1
fi
echo "==> cold-honesty guard passed: kata pool at 0 nodes"

# Harness deps. The cloud-sdk image's python3 is PEP-668 externally-managed,
# so --break-system-packages is required (container is ephemeral — safe).
pip install --quiet --no-cache-dir --break-system-packages -r harness/requirements.txt

# Background node-count sampler (diagnostic): the kata pool autoscales 0->N
# under the sweep + graduation burst; sampling its node count across the
# whole measure phase lets a future "warm slower than cold" anomaly be
# attributed to a reactive scale-up landing inside the measured window,
# straight from the build log. Read-only, keyed on the public kata label,
# best-effort (a sampler hiccup must never fail the measure step). Each
# sample is streamed as it's taken (prefixed kata-node-sample) so a late
# log-sink gap can't lose the whole series.
NODE_SAMPLE_LOG=/workspace/kata-node-count-sample.log
: > "$NODE_SAMPLE_LOG"
( while true; do
    sample="kata-node-sample $(date -u +%FT%TZ) nodes=$(kubectl get nodes -l katacontainers.io/kata-runtime=true --no-headers 2>/dev/null | wc -l | tr -d ' ')"
    echo "$sample" || true
    printf '%s\n' "$sample" >> "$NODE_SAMPLE_LOG" 2>/dev/null || true
    sleep 3
  done ) &
NODE_SAMPLER_PID=$!

# Step 1 — honest kata cold step-up sweep -> BENCH_SLO_SWEEP record. Run
# with PYTHONPATH=/workspace so the script's `from harness... import` lines
# resolve (running `python3 scripts/x.py` puts scripts/ on sys.path[0], not
# the repo root).
echo "==> firing honest kata cold true_ttfe sweep"
KATA_SWEEP_OUT=/workspace/kata-cold-ttfe-sweep.json PYTHONPATH=/workspace \
  python3 scripts/kata_cold_ttfe_sweep.py

# Step 1b — honest kata UNIQUE-IMAGE cold step-up sweep -> a
# second, independent BENCH_SLO_SWEEP record. KATA_UNIQUE_IMAGE_SWEEP_OUT
# is set in env: above; PYTHONPATH=/workspace for the same reason as
# Step 1 (repo-root imports). Reuses WARMPOOL_COLD_START_RUNTIME_CLASS
# (kata-clh, set in env: above) for its runtime class -- the script
# itself only setdefault()s that var, so the graduation shape's value
# wins. Coldness here does NOT depend on the 0-resident-node guard above
# (a node may still be draining down from Step 1 by this point) -- each
# claim's image tag is unique and never pulled before, so containerd on
# ANY node pays a real cold pull regardless of node residency; Step 1's
# own claims/warmpool/template are already deleted by its own `finally`
# cleanup before this starts.
echo "==> firing honest kata UNIQUE-IMAGE cold true_ttfe sweep"
PYTHONPATH=/workspace python3 scripts/kata_unique_image_cold_ttfe_sweep.py

# Optional external warm TTFE sweep passthrough (hb#8751). If provided,
# overrides BENCH_SLO_SWEEP_WARMPOOL_COLD_START with the warm sweep.
if [ -n "${HB_WARMPOOL_COLD_START_SWEEP_B64:-}" ]; then
  SLO_SWEEP_OUT=/workspace/kata-warm-ttfe-sweep.json
  if echo "$HB_WARMPOOL_COLD_START_SWEEP_B64" | base64 -d >"$SLO_SWEEP_OUT" 2>/dev/null \
      && python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$SLO_SWEEP_OUT" 2>/dev/null; then
    export BENCH_SLO_SWEEP_WARMPOOL_COLD_START="$SLO_SWEEP_OUT"
    echo "==> external warm TTFE sweep wired in as BENCH_SLO_SWEEP_WARMPOOL_COLD_START=$SLO_SWEEP_OUT"
  else
    echo "==> WARNING: HB_WARMPOOL_COLD_START_SWEEP_B64 set but failed to decode to valid JSON — leaving BENCH_SLO_SWEEP_WARMPOOL_COLD_START pointing at the cold sweep" >&2
    rm -f "$SLO_SWEEP_OUT"
  fi
fi

# Step 2 — full graduation refresh, merging BOTH sweeps' cold true_ttfe
# triples (BENCH_SLO_SWEEP_WARMPOOL_COLD_START + _NATIVE_DIGEST_COLD,
# set in env: above) into their respective, distinct cells.
echo "==> running sandbox-kata harness (full graduation shape + sweep merge)"
python3 -m harness.run --product sandbox-kata

kill "$NODE_SAMPLER_PID" 2>/dev/null || true
echo "==> kata pool node count over the measure window (diagnostic recap):"
cat "$NODE_SAMPLE_LOG" 2>/dev/null || echo "(no samples captured)"

# hb#889 (#6669 follow-up): harness.run's build_provenance() only
# reads node_count from BENCH_NODE_COUNT (unlike gVisor's #615 fix, this
# file never set it), so kata refreshes always shipped the harness's static
# "1" default -- root cause of #6669's FAIL misread as "1-node starvation".
# gVisor's pre-run env-var pattern can't transfer: this pool is forced to 0
# by the cold-honesty guard above, then scales 0->N DURING this same
# `harness.run` call, whose build_provenance() only runs at the very end --
# so a pre-run env var would report the pre-scale floor (0), which the
# harness's own guard rejects, falling back to the same misleading "1". The
# sampler above already has the value that matters: the PEAK pool size
# reached during the burst. Patch it into the results file post-hoc,
# fail-open (never fabricate on a missing/non-numeric peak).
KATA_PEAK_NODE_COUNT=$(awk -F'nodes=' '{print $2}' "$NODE_SAMPLE_LOG" 2>/dev/null | sort -n | tail -1 || true)
if echo "$KATA_PEAK_NODE_COUNT" | grep -Eq '^[1-9][0-9]*$'; then
  echo "==> patching provenance.node_count -> $KATA_PEAK_NODE_COUNT (peak observed during measure window, hb#889)"
  python3 - "$KATA_PEAK_NODE_COUNT" <<'PYEOF'
import json, pathlib, sys

peak = int(sys.argv[1])
path = pathlib.Path("sandbox-kata/results/latest.json")
data = json.loads(path.read_text())
provenance = data.get("provenance")
if isinstance(provenance, dict) and provenance.get("cluster_substrate") == "gke-kata":
    provenance["node_count"] = peak
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    print(f"==> provenance.node_count patched to {peak}")
else:
    print("==> WARNING: provenance missing or cluster_substrate != gke-kata -- leaving node_count untouched", file=sys.stderr)
PYEOF
else
  echo "==> WARNING: no valid peak node-count sample captured ('$KATA_PEAK_NODE_COUNT') -- leaving provenance.node_count at harness default (hb#889)" >&2
fi

# hb#6669: fail-closed INFRA-not-test gate (#4420, "reopen loudly").
# A prescale timeout (hb#835 lever-2) discloses via sla_metrics
# .lever2_prescale_degraded instead of crashing -- unread, a starved
# rig still publishes a plausible FAIL (the #6669 node_count=1
# misread). On a hit: revert latest.json (harness.run already wrote
# it above, so open-pr's diff would otherwise still fire) and FAIL
# this build outright -- INFRA, not a substrate signal (#137/#2440),
# and a red build in history beats a silent green skip.
LEVER2_DEGRADED=$(python3 -c "import json; d = json.load(open('sandbox-kata/results/latest.json')); print(','.join(s['name'] for s in d.get('scenarios', []) if isinstance(s, dict) and isinstance(s.get('sla_metrics'), dict) and s['sla_metrics'].get('lever2_prescale_degraded')))")
if [ -n "$LEVER2_DEGRADED" ]; then
  git checkout -- sandbox-kata/results/latest.json
  echo "==> FATAL: lever2_prescale_degraded set for: $LEVER2_DEGRADED -- pool never reached prescale target (oversubscribed rig, not a substrate regression). latest.json reverted, no PR will open; re-fire once pool is confirmed at target." >&2
  exit 1
else
  echo "==> rendering README/DETAILS from results"
  python3 -m render.generate
fi

echo "==> public-safety gate (fail-closed)"
bash scripts/check-public-safety.sh
