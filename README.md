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

**Read TTFE down a column, not across rows.** Each activation-mode row carries its own sample size (the Samples (N) column) — they differ by orders of magnitude. A p50 over hundreds of samples and a p50 over one are not comparable: cross-row TTFE ranking is only meaningful between rows with similar N. Rows below N=30 are marked † on their TTFE cells.

| Runtime | Activation Mode | Throughput @ <5s TTFE (sb/s/node) | Throughput @ <1s TTFE (sb/s/node) | TTFE p50 | TTFE p95 | Samples (N) | Max Density (sb/vCPU) | Execution Success (Honesty Check) |
|---|---|---|---|---|---|---|---|---|
| gVisor | Warm-pool hit (Base image) | 19.967 | 0 | 1.396s | 1.7221s | 30 | pending | 100% |
| gVisor | Unique-image cold (RL reality) | pending | pending | 2.1276s † | 2.1276s † | 1 | pending | 100% |
| gVisor | Resume-from-suspend | pending | pending | 32.5161s † | 32.7104s † | 3 | N/A | 100% |
| Kata + microVM | Warm-pool hit (Base image) | pending | pending | pending | pending | pending | pending | pending |
| Kata + microVM | Unique-image cold (RL reality) | pending | pending | pending | pending | pending | pending | pending |
| Kata + microVM | Resume-from-suspend | pending | pending | pending | pending | pending | N/A | pending |

_TTFE = Time-To-First-Instruction: the sandbox executed its first instruction and returned a result — not merely pod-Ready._
_Throughput @ <1s renders the harness-emitted `0` when the p95 misses the 1s bar (we print a zero rather than round up)._
_Max Density is sandboxes per node-allocatable sandbox-schedulable vCPU (the per-node denominator), not per total-cluster vCPU._
_Execution Success is the Honesty Check: <100% prints the succeeded/total fraction and a ⚠️ flag._
_† marks a TTFE measured over fewer than N=30 samples — read it as a single observation, not a distribution, and do not rank it against a high-N row._
_Kata + microVM rows are not-yet-measured (requires-kata-microvm)._
_Cells render `pending` until the TTFE-instrumented run lands._

_build: cluster_substrate=gke-sandbox · controller_image=us-central1-docker.pkg.dev/k8s-staging-images/agent-sandbox/agent-sandbox-controller:latest-main · controller_digest=sha256:6edaf7b6b22d9dfaf6ab077cd1c6517acf5fc6cf96b1ad58fe83bcfd477977ec · crd_version=v1beta1 · suite_git_sha=fbc1a5d362f8a7befa3c3c2cb33703013cfb49b0 · run_id=247326d41d0749319d823948bc5fbaf0 · node_count=20_
_generated-at: 2026-07-01T04:18:36Z_

## Warm-vs-Cold Speedup

A warm-pool provision is **8.76812× faster** than a cold-provision start (warm-pool overflow) (gVisor). The warm pool keeps a ready slot so a claim skips the fresh-node provisioning path an overflow claim pays when the pool is exhausted — provisioning off the SHARED base image (one node-cacheable image, NOT a unique image per claim). Both legs are measured the same way (TTFE (executed first-instruction)); the ratio is the portable headline you can reproduce on your own cluster.

| Leg | TTFE (p50) |
|---|---|
| Warm-pool hit (gVisor) | 6.9s |
| Cold-provision (node overflow) | 60.5s |
| Speedup (warm is N× faster) | 8.76812× |

_Speedup = cold ÷ warm, computed from the displayed values over n=300 warm claims; the warm leg is the p50 so half of warm claims beat it._

_This warm-vs-cold pair is a standalone point-in-time run; its warm-pool leg is a separate measurement from the Core Metrics matrix "Warm-pool hit" row (a different sample size and operating point). Read each block on its own terms — the two warm p50s are not directly comparable._

_Measured 2026-06-29 — warm-vs-cold speedup (point-in-time; refreshed on the next TTFE fire)._

## Scale Proof (Linearity Check)

| Nodes Tested | Density Holds Flat? | Throughput Holds Flat? |
|---|---|---|
| 1 → 2 → 4 → 8 → 16 | ✅ Yes (0.63 → 0.63 → 0.63 → 0.63 → 0.63) | ⚠️ No |

_Per-step density retention: 1→2 ✅ 1 · 2→4 ✅ 1 · 4→8 ✅ 1 · 8→16 ✅ 1 — holds flat step-to-step._

_Measured 2026-06-29 — node-count linearity sweep (point-in-time; refreshed on the next multi-node sweep)._

## Kata + microVM Activation (pod-Ready — NOT TTFE)

These are **Kata + microVM pod-Ready / microVM-activation** latencies — the time to bring the guest microVM up and the pod Ready. They are **not TTFE** (the Core Metrics matrix's executed-first-instruction-and-returned-a-result metric), so they are **not comparable to the matrix TTFE columns**; the Kata TTFE cells there stay `pending` until a TTFE probe runs under Kata. Measured on hypervisor **Cloud Hypervisor**, Kata **3.32.0**, guest kernel `6.18.35`, host kernel `6.8.0-1054-gke`, n=3.

| Phase | Pod-Ready latency |
|---|---|
| microVM activation | 2s |
| Warm-pool hit (ubuntu:24.04) | 3s |
| Cold start — debian:12 (image pull 0.9s) | 3s |
| Cold start — ubuntu:24.04 (image pull 0.887s) | 5s |
| Snapshot resume | N/A — upstream-blocked (CRIU resume not wired, #3097) |

_Measured 2026-06-30 — Kata pod-Ready / microVM-activation (point-in-time; not TTFE)._

## Concurrent Burst — TTFE at N simultaneous claims

Each row is a **single all-at-once burst of N concurrent claims** (not a ramped per-second rate). TTFE is the same metric the Core Metrics matrix reports (executed-first-instruction-and-returned-a-result), so these columns **are comparable to the matrix TTFE columns**. *Warm pool* fires against a pre-provisioned pool of N ready sandboxes; *cold provision* starts from an empty pool (node-autoscaler + image-pull in the critical path). Measured on node_count=20, `e2-standard-16`.

| Concurrency (N) | Activation Mode | TTFE p50 | TTFE p95 | Throughput @ <5s/node | Throughput @ <1s/node | Execution Success |
|---|---|---|---|---|---|---|
| 300 | Warm pool | 6.8743s | 9.393s | 0.392 | 0 | 100% |
| 300 | Cold provision | 56.0294s | 58.4124s | 0 | 0 | 100% |
| 500 | Warm pool | 11.188s | 15.374s | 0.052 | 0 | 100% |
| 500 | Cold provision | 97.3988s | 99.8002s | 0 | 0 | 100% |

_Measured 2026-06-30 — concurrent-burst TTFE (point-in-time)._

## Warm-Pool Acquisition — how fast the pool hands you a sandbox

Acquisition latency on **gVisor**: the time from a `SandboxClaim` being **requested** to it being **bound** — a warm, ready sandbox handed back to the caller. This is a **decomposed sub-phase of TTFE**, not the whole thing: it stops at the moment you hold a ready sandbox and **excludes** the exec-attach + first-instruction round-trip the Concurrent Burst and Core Metrics tables measure — so these numbers are **not comparable** to those TTFE columns. It is the earlier, isolated question a warm-pool operator sizes against: *once my pool is warm, how quickly do I get a sandbox?* Measured under a sustained **300 claims/sec** offered load against a warm pool of **600**. Cluster shape: `n2-standard-16`.

| Sample (n) | Acquisition p50 | Acquisition p95 | Acquisition p99 |
|---|---|---|---|
| 600 | 2.93965s | 3.87844s | 4.00962s |

_Controller-startup lower bound (p95 **1.33812s**): controller-first-observed → Ready, which EXCLUDES the claim-admission → first-reconcile queueing lag — it UNDER-reports the true acquisition path, so treat it as a floor on the controller's own contribution, not a second acquisition measurement._

_Measured 2026-07-01 — warm-pool acquisition latency (point-in-time)._
