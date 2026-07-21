# Honest benchmarks — deep-dive appendix

The corroboration and decomposition tables behind the headline page
([README.md](README.md)). Same rule: **every number is machine-rendered from a real
harness run — nothing here is typed by hand.** Start with the headline page; come here
when you want to see the working.

## Burst Create — TTFE Corroboration

The headline burst count is **pod-Ready** — but a pod can report Ready before it can run your code. TTFE is the stronger claim: the sandbox *executed its first instruction and returned a result*. This block corroborates the two; the **gap** is sandboxes that reported Ready but had not yet run code.

| Signal | Count |
|---|---|
| Pod-Ready <1s (weaker claim) | 10 |
| Executed first-instruction <1s (TTFE, stronger claim) | 3 |
| Ready-but-not-yet-run (gap) | 7 |
| Execution success (Honesty Check) | 100% |

_Pod-Ready ≥ executed-TTFE by construction; the gap is the over-claim a pod-Ready headline would hide._

## Warm-Hit TTFE — Bind vs Exec Decomposition

Warm-hit TTFE (create → first-instruction result) splits into **bind** (create → bound, i.e. provisioning the pool member) and **exec** (websocket setup + the first-instruction round-trip). This block shows *where* a warm-hit above the <1s target lives — a large bind points at provisioning (a controller/clone target); a large exec points at the exec channel (a harness/product artifact, not a controller regression).

| Stage | p50 | p95 |
|---|---|---|
| Bind (create → bound, provisioning) | 2.4709s | 3.4681s |
| Exec (websocket + first-instruction) | 0.5594s | 0.7884s |
| **TTFE (total)** | **2.9894s** | **4.0589s** |

_Each row is an independently-measured percentile of its own per-claim distribution (exec is measured per-claim as TTFE − bind, then percentiled — not p50(TTFE) − p50(bind)). Percentiles do not sum, so bind and exec need not add exactly to the total TTFE._

## Cold-Start TTFE — Provision vs Exec Decomposition

Cold-start TTFE (create → first-instruction result) splits into **provision** (create → Ready: controller reconcile + pod schedule + image pull + container start) and **exec** (websocket setup + the first-instruction round-trip on the already-Ready sandbox). For a cold start the provision is *expected* to dominate — a cold image pull is genuinely slow — so the signal to watch here is a large **exec**, which would point at the exec channel (a harness/product artifact), not the cold provision itself.

| Stage | p50 | p95 |
|---|---|---|
| Provision (create → Ready) | 3.7679s | 4.8694s |
| Exec (websocket + first-instruction) | 0.5597s | 0.6189s |
| **TTFE (total)** | **4.336s** | **5.4425s** |

_Each row is an independently-measured value against the same shared t0 (exec is the measured residual TTFE − provision, not a subtraction of percentiles). For the single-sample cold cell the p50 and p95 are the one measured sample._

## Warm-vs-Cold Speedup

A warm-pool provision is **3.75965× faster** than a true-cold start (gVisor). The warm pool keeps a ready slot so a claim skips the fresh-node image-pull path a cold start pays in full. Both legs are measured the same way (TTFE (executed first-instruction)); the ratio is the portable headline you can reproduce on your own cluster.

| Leg | TTFE (p50) |
|---|---|
| Warm-pool hit (gVisor, n=10) | 1.1533s |
| True-cold (unique-image) | 4.336s |
| Speedup (warm is N× faster) | 3.75965× |

_Speedup = cold ÷ warm, computed from the displayed values over n=10 warm claims; the warm leg is the p50 so half of warm claims beat it._

_This warm-vs-cold pair is a standalone point-in-time run; its warm-pool leg is a separate measurement from the Core Metrics matrix "Warm-pool hit" row (an independent run at its own operating point, refreshed on its own cadence). Read each block on its own terms — the two warm p50s are not directly comparable._

_Measured 2026-07-20 — warm-vs-cold speedup (point-in-time; refreshed on the next TTFE fire)._

## Kata + microVM Activation (pod-Ready — NOT TTFE)

These are **Kata + microVM pod-Ready / microVM-activation** latencies — the time to bring the guest microVM up and the pod Ready. They are **not TTFE** (the Core Metrics matrix's executed-first-instruction-and-returned-a-result metric), so they are **not comparable to the matrix TTFE columns**. For the Kata TTFE itself, read the matrix TTFE cells: they report it where a TTFE probe has run under Kata, and `pending` where one has not. Measured on hypervisor **Cloud Hypervisor**, Kata **3.32.0**, guest kernel `6.18.35`, host kernel `6.8.0-1054-gke`, n=3.

| Phase | Pod-Ready latency |
|---|---|
| microVM activation | 2s |
| Warm-pool hit (ubuntu:24.04) | 3s |
| Cold start — debian:12 (image pull 0.9s) | 3s |
| Cold start — ubuntu:24.04 (image pull 0.887s) | 5s |
| Snapshot resume | [N/A — CRIU checkpoint/restore does not transfer to the Kata VM model](WORK_IN_PROGRESS.md#na-by-construction) |

_Measured 2026-06-30 — Kata pod-Ready / microVM-activation (point-in-time; not TTFE)._

## Warm-Pool Acquisition — how fast the pool hands you a sandbox

Acquisition latency on **gVisor**: the time from a `SandboxClaim` being **requested** to it being **bound** — a warm, ready sandbox handed back to the caller. This is a **decomposed sub-phase of TTFE**, not the whole thing: it stops at the moment you hold a ready sandbox and **excludes** the exec-attach + first-instruction round-trip the Concurrent Burst and Core Metrics tables measure — so these numbers are **not comparable** to those TTFE columns. It is the earlier, isolated question a warm-pool operator sizes against: *once my pool is warm, how quickly do I get a sandbox?* Measured under a sustained **300 claims/sec** offered load against a warm pool of **600**. Cluster shape: `n2-standard-16`.

| Sample (n) | Acquisition p50 | Acquisition p95 | Acquisition p99 |
|---|---|---|---|
| 600 | 2.93965s | 3.87844s | 4.00962s |

_Controller-startup lower bound (p95 **1.33812s**): controller-first-observed → Ready, which EXCLUDES the claim-admission → first-reconcile queueing lag — it UNDER-reports the true acquisition path, so treat it as a floor on the controller's own contribution, not a second acquisition measurement._

_Measured 2026-07-01 — warm-pool acquisition latency (point-in-time)._

## Max Density (sandboxes per vCPU)

Max Density is sandboxes per node-allocatable sandbox-schedulable vCPU (the per-node denominator), not per total-cluster vCPU. This is the absolute per-vCPU figure — distinct from the linearity check's per-node density-retention series (a ratio across node counts), which uses a different denominator. An unmeasured runtime renders `pending`.

| Runtime | Max Density (sb/vCPU) |
|---|---|
| gVisor | 5.98 |
| Kata + microVM | 1.26 |

## At Scale Under Contention — where sub-second warm activation breaks

The Concurrent Burst legs on the headline page are **1:1** — N ready sandboxes hit with N claims. This is the deliberate **retraction**: the operating point where the pool is **over-subscribed** (more concurrent claims than ready pool members), and warm activation **stops being sub-second**. Measured on **gVisor**: a pool of **30** ready sandboxes hit with **60** simultaneous claims (**2:1 contention**). Every claim still binds, but the over-subscription serializes the bind path — so the "warm hit is <1s" claim from the Core Metrics matrix does **not** hold here. Cluster shape: node_count=1, `e2-standard-16`.

| Pool | Claims | Contention | TTFE p50 | TTFE p95 | Bind p50 | Bind p95 | Execution Success |
|---|---|---|---|---|---|---|---|
| 30 | 60 | 2:1 | 1.6589s | 2.0169s | 1.384s | 1.7001s | 100% |

_Not directly comparable to the 1:1 Concurrent Burst legs: this point ran at node_count=1 with an over-subscribed pool — a distinct operating point. Latency is node-count-independent (so the TTFE columns DO compare to the matrix/burst TTFE), but the per-node throughput axis is omitted here as non-comparable to the node_count=20 bursts._

_Measured 2026-07-01 — warm-pool at-scale contention ceiling (point-in-time)._

## Cluster Saturation — the whole-cluster warm-hand-out ceiling

The Concurrent Burst legs above are small 1:1 warm bursts. This is the **saturation** ceiling: a **1:1 all-warm** fire — a pool of **600** ready sandboxes hit with **600** simultaneous claims (**not** over-subscribed), spread across **40** nodes on **gVisor**. Every claim has a ready warm pool member, yet at this scale the bind path itself saturates — so the whole-cluster warm hand-out rate collapses far below the per-node engineering rate, and the "warm hit is <1s" claim from the Core Metrics matrix does **not** hold here. Cluster shape: `n2-standard-16`.

| Pool | Claims | Nodes | TTFE p50 | TTFE p95 | Throughput @ <5s | Throughput @ <1s | Bind p50 | Bind p95 | Execution Success |
|---|---|---|---|---|---|---|---|---|---|
| 600 | 600 | 40 | 8.6308s | 12.6103s | 0.064 /node · 2.558 /cluster | 0 /node · 0 /cluster | 8.1916s | 12.1372s | 100% |

_Per-cluster throughput MEASURED at **40 nodes** — never a per-node × N extrapolation (that fiction breaks above the controller reconcile ceiling). This is a **1:1 all-warm** operating point (pool == claim, not over-subscribed), distinct from the over-subscribed contention ceiling: the collapse here is the bind path saturating at cluster scale, not pool exhaustion. Latency is node-count-independent (so the TTFE columns DO compare to the matrix/burst TTFE)._

_SLA ceiling: **not met** at this operating point — the honest saturation limit. Execution success confirms every claim still bound and executed; the FAIL is the throughput collapse against the sizing floor, not a correctness failure._

_Measured 2026-07-02 — whole-cluster saturation ceiling (point-in-time)._

## Provisioning Rate Sweep — where warm-pool fill goes reconcile-bound

The warm-pool numbers elsewhere assume the pool is **already Ready**. This block measures the step before that: how fast the pool can be **provisioned** as a function of the **offered reconcile rate** (sandboxes requested per second). At each rate the pool is driven to a target size and we measure whether it reaches Ready **within the warm timeout**. Measured on **gVisor**.

| Offered reconcile rate | Warm-pool target | Ready within timeout |
|---|---|---|
| 100 sb/s | 1500 | ✅ 100% (converged ~301s) |
| 150 sb/s | 2250 | ❌ 42% (timeout 1125s) |
| 200 sb/s | 3000 | ❌ 21% (timeout 1880s) |

**Provisioning converges at ~100 sb/s; over-subscribed beyond ~(100, 150) sb/s** — monotonic degradation past the ceiling is **reconcile-bound** (the controller reconcile path is the ceiling), not node- or quota-bound.

_A distinct axis from the Concurrent Burst (claim:pool ratio) and Step-up (creation-rate TTFE) blocks: this measures provisioning **offered-rate** convergence, a separate regime — not directly comparable to those latency/throughput points._

_Measured 2026-07-01 — warm-pool provisioning rate sweep (point-in-time; refreshed on the next rate sweep)._

## Warm-Pool Turnover — Sustained-Churn Refill Latency

The matrix measures the **claim** side (a warm hit is sub-second). This block measures the **reclaim** side: after a claim is released, how long the controller takes to **replenish** the warm pool under sustained claim/release churn. A slow refill silently demotes later claims from warm to cold — the failure mode a fleet cycling sandboxes continuously actually hits.

| Refill latency | Value |
|---|---|
| Median (p50) (over 5 cycles) | 0.906935s |
| Tail (p90) | 1.08648s |

_Refill latency is measured per-cycle as the wall-clock from a claim release to the warm pool returning to full readiness; the median and tail are percentiles of the completed-cycle distribution._

## Administrative Suspend Latency

Suspend is the cost-lever for reclaiming a sandbox's compute while keeping its identity: an `operatingMode=Suspended` patch releases the backing Pod but preserves the CR, so a later `operatingMode=Running` patch resumes it. This block reports how fast that **administrative** suspend completes — from the patch to the terminal Suspended state (Pod released + the Suspended condition observed).

_Capability note: this is an **administrative** (operator- or user-driven) suspend. Upstream agent-sandbox exposes only the closed `operatingMode` enum (`Running`; `Suspended`) — there is **no idle-timeout, activity-reclaim, or auto-suspend** path, so this latency must not be read as an automatic scale-to-zero._

| Suspend latency | Value |
|---|---|
| Median (p50) | 2.3056s |

_Suspend latency is measured per-cycle as the wall-clock from the `operatingMode=Suspended` patch return to the terminal Suspended state; the median and tail are percentiles of the measured suspend distribution._
