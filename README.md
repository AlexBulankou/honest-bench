# Honest benchmarks — GKE agent sandbox

This page is **machine-rendered, never hand-entered**. Every cell traces to a
schema-validated field of a real harness run; anything the schema does not declare is
dropped before it can reach this page. We publish only what we actually measured on the
cluster named in the build banner — a `kind` run is labelled `kind`, so a local number is
never presented as a production SLA.

The Core Metrics table is keyed on **TTFE — Time-To-First-Instruction**: the wall-clock
from the create request to the moment the sandbox *executed its first instruction and
returned a result*. TTFE is not pod-Ready — a pod can report Ready before it can run your
code, so we measure the thing a user actually waits for. Throughput is reported per node at
two TTFE bars (`<5s` and `<1s`); the **Execution Success (Honesty Check)** column is the
fraction of first-instructions that actually succeeded.

This page prints the truth even when it is unflattering:

- A cell we have not yet measured renders `pending` — never a guess, never a false number.
- A throughput whose p95 misses the bar prints an honest **`0`** — we do not round up.
- An execution-success rate below 100% prints the succeeded/total fraction and a ⚠️ flag.

Every measured number here is a **reproducible floor, not a ceiling.** It is what a
*vanilla* OSS build — the upstream controller from `main`, default runtime, no tuning,
the cluster shape named in the build banner — delivers today. A bigger pool, denser nodes,
or a tuned runtime should *beat* it; the value on the page is the honest lower bound you can
reproduce from the recipe below and then improve on. We publish the floor we can stand
behind, not the best number we could cherry-pick.

Reproduce any row yourself — then beat it. The suite is honest by construction:

```
bash recipe/install-controller-from-main.sh   # OSS controller from upstream main
python3 -m harness.run                        # run the portable suite (cluster=kind)
python3 -m render.generate                    # regenerate this table
bash scripts/check-public-safety.sh           # fail-closed public-safety scan
```

## Agent Sandbox — Core Metrics

| Runtime | Activation Mode | Throughput @ <5s TTFE (sb/s/node) | Throughput @ <1s TTFE (sb/s/node) | TTFE p50 | TTFE p95 | Samples (N) | Max Density (sb/vCPU) | Execution Success (Honesty Check) |
|---|---|---|---|---|---|---|---|---|
| gVisor | Warm-pool hit (Base image) | 0 | 0 | 6.8111s | 7.5602s | 10 | pending | 100% |
| gVisor | Unique-image cold (RL reality) | pending | pending | 2.2963s | 2.2963s | 1 | pending | 100% |
| gVisor | Resume-from-suspend | pending | pending | 32.4912s | 32.4912s | 1 | N/A | 100% |
| Kata + microVM | Warm-pool hit (Base image) | pending | pending | pending | pending | pending | pending | pending |
| Kata + microVM | Unique-image cold (RL reality) | pending | pending | pending | pending | pending | pending | pending |
| Kata + microVM | Resume-from-suspend | pending | pending | pending | pending | pending | N/A | pending |

_TTFE = Time-To-First-Instruction: the sandbox executed its first instruction and returned a result — not merely pod-Ready._
_Throughput @ <1s renders the harness-emitted `0` when the p95 misses the 1s bar (we print a zero rather than round up)._
_Max Density is sandboxes per node-allocatable sandbox-schedulable vCPU (the per-node denominator), not per total-cluster vCPU._
_Execution Success is the Honesty Check: <100% prints the succeeded/total fraction and a ⚠️ flag._
_Kata + microVM rows are not-yet-measured (requires-kata-microvm)._
_Cells render `pending` until the TTFE-instrumented run lands._

_build: cluster_substrate=gke-sandbox · controller_image=us-central1-docker.pkg.dev/k8s-staging-images/agent-sandbox/agent-sandbox-controller:latest-main · controller_digest=sha256:6edaf7b6b22d9dfaf6ab077cd1c6517acf5fc6cf96b1ad58fe83bcfd477977ec · crd_version=v1beta1 · suite_git_sha=df6aa6b1c73a67b96cc8ebd5552f3b1f19bb4552 · run_id=5d00636a3f754842b1aff7157da52e82 · node_count=1_
_generated-at: 2026-06-29T02:05:10Z_

## Scale Proof (Linearity Check)

| Nodes Tested | Density Holds Flat? | Throughput Holds Flat? |
|---|---|---|
| 1 → 2 → 4 | ✅ Yes (5.18 → 5.18 → 5.18) | ⚠️ No |

_Measured 2026-06-29 — node-count linearity sweep (point-in-time; refreshed on the next multi-node sweep)._
