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
| Executed first-instruction <1s (TTFE, stronger claim) | 10 |
| Ready-but-not-yet-run (gap) | 0 |
| Execution success (Honesty Check) | 100% |

_Pod-Ready ≥ executed-TTFE by construction; the gap is the over-claim a pod-Ready headline would hide._

## Warm-Hit TTFE — Bind vs Exec Decomposition

Warm-hit TTFE (create → first-instruction result) splits into **bind** (create → bound, i.e. provisioning the pool member) and **exec** (websocket setup + the first-instruction round-trip). This block shows *where* a warm-hit above the <1s target lives — a large bind points at provisioning (a controller/clone target); a large exec points at the exec channel (a harness/product artifact, not a controller regression).

| Stage | p50 | p95 |
|---|---|---|
| Bind (create → bound, provisioning) | 0.4135s | 0.6854s |
| Exec (websocket + first-instruction) | 0.2189s | 0.298s |
| **TTFE (total)** | **0.6317s** | **0.9454s** |

_Each row is an independently-measured percentile of its own per-claim distribution (exec is measured per-claim as TTFE − bind, then percentiled — not p50(TTFE) − p50(bind)). Percentiles do not sum, so bind and exec need not add exactly to the total TTFE._

> ⚠️ **Regime caveat:** this warm tier was measured on a **drained, low-contention cluster** (single fire, small claim count). A green warm tier here is honest for THIS fire but is **not yet a sustained North-Star claim** — it wants corroboration under representative load before sub-1s warm is treated as durable.

## Warm-vs-Cold Speedup

A warm-pool provision is **7.28251× faster** than a true-cold start (gVisor). The warm pool keeps a ready slot so a claim skips the fresh-node image-pull path a cold start pays in full. Both legs are measured the same way (TTFE (executed first-instruction)); the ratio is the portable headline you can reproduce on your own cluster.

| Leg | TTFE (p50) |
|---|---|
| Warm-pool hit (gVisor, n=30) | 0.7005s |
| True-cold (unique-image) | 5.1014s |
| Speedup (warm is N× faster) | 7.28251× |

_Speedup = cold ÷ warm, computed from the displayed values over n=30 warm claims; the warm leg is the p50 so half of warm claims beat it._

_This warm-vs-cold pair is a standalone point-in-time run; its warm-pool leg is a separate measurement from the Core Metrics matrix "Warm-pool hit" row (an independent run at its own operating point, refreshed on its own cadence). Read each block on its own terms — the two warm p50s are not directly comparable._

_Measured 2026-07-02 — warm-vs-cold speedup (point-in-time; refreshed on the next TTFE fire)._

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

## Warm-Pool Acquisition — how fast the pool hands you a sandbox

Acquisition latency on **gVisor**: the time from a `SandboxClaim` being **requested** to it being **bound** — a warm, ready sandbox handed back to the caller. This is a **decomposed sub-phase of TTFE**, not the whole thing: it stops at the moment you hold a ready sandbox and **excludes** the exec-attach + first-instruction round-trip the Concurrent Burst and Core Metrics tables measure — so these numbers are **not comparable** to those TTFE columns. It is the earlier, isolated question a warm-pool operator sizes against: *once my pool is warm, how quickly do I get a sandbox?* Measured under a sustained **300 claims/sec** offered load against a warm pool of **600**. Cluster shape: `n2-standard-16`.

| Sample (n) | Acquisition p50 | Acquisition p95 | Acquisition p99 |
|---|---|---|---|
| 600 | 2.93965s | 3.87844s | 4.00962s |

_Controller-startup lower bound (p95 **1.33812s**): controller-first-observed → Ready, which EXCLUDES the claim-admission → first-reconcile queueing lag — it UNDER-reports the true acquisition path, so treat it as a floor on the controller's own contribution, not a second acquisition measurement._

_Measured 2026-07-01 — warm-pool acquisition latency (point-in-time)._

## Max Density (sandboxes per vCPU)

Max Density is sandboxes per node-allocatable sandbox-schedulable vCPU (the per-node denominator), not per total-cluster vCPU. An unmeasured runtime renders `pending`.

| Runtime | Max Density (sb/vCPU) |
|---|---|
| gVisor | 5.98 |
| Kata + microVM | pending |

## At Scale Under Contention — where sub-second warm activation breaks

The Concurrent Burst legs on the headline page are **1:1** — N ready sandboxes hit with N claims. This is the deliberate **retraction**: the operating point where the pool is **over-subscribed** (more concurrent claims than ready pool members), and warm activation **stops being sub-second**. Measured on **gVisor**: a pool of **30** ready sandboxes hit with **60** simultaneous claims (**2:1 contention**). Every claim still binds, but the over-subscription serializes the bind path — so the "warm hit is <1s" claim from the Core Metrics matrix does **not** hold here. Cluster shape: node_count=1, `e2-standard-16`.

| Pool | Claims | Contention | TTFE p50 | TTFE p95 | Bind p50 | Bind p95 | Execution Success |
|---|---|---|---|---|---|---|---|
| 30 | 60 | 2:1 | 1.6589s | 2.0169s | 1.384s | 1.7001s | 100% |

_Not directly comparable to the 1:1 Concurrent Burst legs: this point ran at node_count=1 with an over-subscribed pool — a distinct operating point. Latency is node-count-independent (so the TTFE columns DO compare to the matrix/burst TTFE), but the per-node throughput axis is omitted here as non-comparable to the node_count=20 bursts._

_Measured 2026-07-01 — warm-pool at-scale contention ceiling (point-in-time)._
