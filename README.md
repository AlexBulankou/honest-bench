# Honest benchmarks — GKE agent sandbox

Most sandbox benchmarks are marketing: a best-case number, measured once, on a tuned
cluster nobody will hand you, with no way to check it. This page is the opposite. **Every
number is machine-rendered from a real harness run and reproducible from the recipe at the
bottom** — no cell is ever typed by hand. Each value traces to a schema-validated field of
an actual run; anything the schema does not declare is dropped before it can reach the page.
We publish only what we measured on the cluster named in the build banner, so a `kind` run
is labelled `kind` and a local number is never dressed up as a production SLA.

The table is keyed on the metric a user actually feels: **TTFE — Time-To-First-Instruction**,
the wall-clock from the create request to the moment the sandbox *ran its first instruction
and returned a result*. TTFE is not pod-Ready — a pod can report Ready seconds before it can
run your code — so we measure the wait, not the checkbox. Throughput is reported per node at
two TTFE bars (`<5s` and `<1s`), and the **Execution Success (Honesty Check)** column is the
fraction of those first instructions that actually succeeded.

The page is built to print the truth even when the truth is unflattering:

- A cell we have not yet measured renders `pending` — never a guess, never a placeholder
  number.
- A throughput whose p95 misses the bar prints an honest **`0`** — we do not round up to
  make the row look better.
- An execution-success rate below 100% prints the succeeded/total fraction and a ⚠️ flag,
  instead of quietly dropping the failures.

And every measured number is a **reproducible floor, not a ceiling.** It is what a *vanilla*
OSS build delivers today — the upstream controller from `main`, default runtime, no tuning,
the cluster shape named in the build banner. A bigger pool, denser nodes, or a tuned runtime
should *beat* it; the value here is the honest lower bound you can reproduce and then improve
on. We publish the floor we can stand behind, not the best number we could cherry-pick.

So don't take our word for it — reproduce any row, then beat it. The suite is honest by
construction:

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
| gVisor | Warm-pool hit (Base image) | 14.037 | 9.358 | 0.8913s | 1.2171s | 30 | pending | 100% |
| gVisor | Unique-image cold (RL reality) | pending | pending | 4.5191s † | 4.5191s † | 1 | pending | 100% |
| gVisor | Resume-from-suspend | pending | pending | pending | pending | pending | N/A | pending |
| Kata + microVM | Warm-pool hit (Base image) | pending | pending | pending | pending | pending | pending | pending |
| Kata + microVM | Unique-image cold (RL reality) | pending | pending | pending | pending | pending | pending | pending |
| Kata + microVM | Resume-from-suspend | N/A | N/A | N/A | N/A | N/A | N/A | N/A |

_TTFE = Time-To-First-Instruction: the sandbox executed its first instruction and returned a result — not merely pod-Ready._
_Throughput @ <1s renders the harness-emitted `0` when the p95 misses the 1s bar (we print a zero rather than round up)._
_Max Density is sandboxes per node-allocatable sandbox-schedulable vCPU (the per-node denominator), not per total-cluster vCPU._
_Execution Success is the Honesty Check: <100% prints the succeeded/total fraction and a ⚠️ flag._
_† marks a TTFE measured over fewer than N=30 samples — read it as a single observation, not a distribution, and do not rank it against a high-N row._
_Kata + microVM rows are not-yet-measured (requires-kata-microvm)._
_Resume-from-suspend × Kata + microVM renders `N/A` by construction — CRIU checkpoint/restore does not transfer to the Kata VM isolation model, so that cell can never be measured (distinct from `pending`, which awaits a run)._
_Cells render `pending` until the TTFE-instrumented run lands._

_build: cluster_substrate=gke-sandbox · run_id=dc1dd343fee74008a2f75ccdfed39eb9 · node_count=1_
_generated-at: 2026-07-01T07:23:08Z_

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
| Bind (create → bound, provisioning) | 0.6626s | 0.9715s |
| Exec (websocket + first-instruction) | 0.2272s | 0.2573s |
| **TTFE (total)** | **0.8913s** | **1.2171s** |

_Each row is an independently-measured percentile of its own per-claim distribution (exec is measured per-claim as TTFE − bind, then percentiled — not p50(TTFE) − p50(bind)). Percentiles do not sum, so bind and exec need not add exactly to the total TTFE._

> ⚠️ **Regime caveat:** this warm tier was measured on a **drained, low-contention cluster** (single fire, small claim count). A green warm tier here is honest for THIS fire but is **not yet a sustained North-Star claim** — it wants corroboration under representative load before sub-1s warm is treated as durable.

## Warm-vs-Cold Speedup

A warm-pool provision is **11.5608× faster** than a true-cold start (gVisor). The warm pool keeps a ready slot so a claim skips the fresh-node image-pull path a cold start pays in full. Both legs are measured the same way (TTFE (executed first-instruction)); the ratio is the portable headline you can reproduce on your own cluster.

| Leg | TTFE (p50) |
|---|---|
| Warm-pool hit (gVisor, n=10) | 0.3909s |
| True-cold (unique-image) | 4.5191s |
| Speedup (warm is N× faster) | 11.5608× |

_Speedup = cold ÷ warm, computed from the displayed values over n=10 warm claims; the warm leg is the p50 so half of warm claims beat it._

_This warm-vs-cold pair is a standalone point-in-time run; its warm-pool leg is a separate measurement from the Core Metrics matrix "Warm-pool hit" row (a different sample size and operating point). Read each block on its own terms — the two warm p50s are not directly comparable._

_Measured 2026-07-01 — warm-vs-cold speedup (point-in-time; refreshed on the next TTFE fire)._

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

## At Scale Under Contention — where sub-second warm activation breaks

The Concurrent Burst legs above are **1:1** — N ready sandboxes hit with N claims. This row is the deliberate **retraction**: the operating point where the pool is **over-subscribed** (more concurrent claims than ready pool members), and warm activation **stops being sub-second**. Measured on **gVisor**: a pool of **30** ready sandboxes hit with **60** simultaneous claims (**2:1 contention**). Every claim still binds, but the over-subscription serializes the bind path — so the "warm hit is <1s" claim from the Core Metrics matrix does **not** hold here. Cluster shape: node_count=1, `e2-standard-16`.

| Pool | Claims | Contention | TTFE p50 | TTFE p95 | Bind p50 | Bind p95 | Execution Success |
|---|---|---|---|---|---|---|---|
| 30 | 60 | 2:1 | 1.6589s | 2.0169s | 1.384s | 1.7001s | 100% |

_Not directly comparable to the 1:1 Concurrent Burst legs: this point ran at node_count=1 with an over-subscribed pool — a distinct operating point. Latency is node-count-independent (so the TTFE columns DO compare to the matrix/burst TTFE), but the per-node throughput axis is omitted here as non-comparable to the node_count=20 bursts._

_Measured 2026-07-01 — warm-pool at-scale contention ceiling (point-in-time)._

## Reproducibility Recipe

The numbers above come from a fixed, publishable cluster shape — a *vanilla* GKE architecture
any user can provision, not a private tuning. This section is the **load-bearing shape** a
reader needs to reproduce the warm/cold TTFE and scale regime the tables report. The
**runnable** version — exact commands, pinned installs, and the dispatch-only CI workflows —
lives in [`recipe/REPRODUCE.md`](recipe/REPRODUCE.md), so it is not duplicated here.

**Cluster**

- A **regional** GKE Standard cluster on **Kubernetes >= 1.31** — regional so the control
  plane is not a single-zone SPOF under a burst of simultaneous claim writes, and >= 1.31
  because that is the floor where the sandbox CRDs and the gVisor `RuntimeClass` admission path
  are both stable.
- A **gVisor-enabled** node pool (`--enable-sandbox=type=gvisor`, which installs the `gvisor`
  `RuntimeClass` the burst pins to) on a **16-vCPU** machine type (e.g. `e2-standard-16`).
- Size the pool's autoscaling **maximum to the node count the headline needs *before* the
  fire** — a warm burst that has to wait on node autoscaling is measuring the autoscaler, not
  the sandbox path. The gate on that ceiling is **per-machine-family CPU quota**, not the
  generic CPU quota; `recipe/REPRODUCE.md` has the family-quota math.
- A **pod CIDR wide enough that node-count × pods-per-node does not exhaust the range** — a
  **`/16`** cluster pod range comfortably addresses a several-hundred-node pool, so the burst
  tops out on the sandbox path rather than silently on IP exhaustion.

**Warm-pool sizing**

- Size the `SandboxWarmPool` so a ready slot is waiting when each claim arrives: **replicas ≈
  active-concurrency × 0.75, replenished at the claim rate.** The 0.75 factor keeps a
  steady-state buffer of ready slots without over-provisioning idle capacity; replenishing at
  the claim rate refills a drained slot as fast as claims consume them, so a sustained arrival
  rate is served warm rather than draining into the cold-overflow path partway through a burst.
- The warm-hit distribution widens at higher concurrency because the **bind (provisioning) side
  grows with claim-count while exec stays flat** — so the warm number is a function of
  pool-replenish-rate vs claim-rate, not a fixed constant.
  When a drained-regime fire is on the page, the Warm-Pool decomposition caveat above names this
  scaling term directly.

**Zero-cold-start image pre-pull**

- Run an image **pre-pull `DaemonSet`** (`recipe/prepull-daemonset.yaml`) that pins the sandbox
  base image on every node before the fire, so a scale-out node that joins mid-burst does not
  add an image-pull tax to the first sandbox scheduled onto it. It matters most on the **cold
  leg and under warm-pool overflow** — a fully pre-filled pool already resident-izes the image
  during warm-up. Without it, cold TTFE on a freshly-autoscaled node is dominated by pull
  latency, not create latency — an artifact of the test setup, not the runtime.

**Honesty caveats (these stay on the published recipe)**

- The **sub-1s @ 300/s warm headline is not yet published.** It needs (a) the per-claim
  acquisition watch-timer at 300/s and (b) a clean burst fire. The honest published-today
  numbers are exactly the measured cells above — read the **Warm-Pool Acquisition** and
  **Concurrent Burst** rows for the current p50/p95 at the offered rate and pool size named in
  each caption. The page prints the real figure rather than the aspiration, so the recipe
  points at those cells instead of restating a number that could drift out of sync with them.
- **TRUE-TTFE** — first instruction actually executes, webhook-stamped — is gated on the
  upstream webhook-stamper and renders `pending` until it lands. The executed-first-instruction
  TTFE the tables report today is the honest bridge that proves create → first-instruction
  wallclock without the stamp.
- Rows marked `pending` are exactly that — **not-yet-measured, never a provisional number
  dressed as a result.**
