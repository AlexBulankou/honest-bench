# Honest benchmarks — GKE agent sandbox

Most sandbox benchmarks are marketing — one best-case number, measured once, on a cluster
you will never get, with no way to check it. This page is the opposite: **every number is
machine-rendered from a real harness run and reproducible from the recipe at the bottom.** No
cell is ever typed by hand, and anything the schema does not declare is dropped before it can
reach the page.

We measure the one thing you actually feel — **TTFE (Time-To-First-Instruction)**: the
wall-clock from "create this sandbox" to "it ran my first instruction and returned a result."
Not pod-Ready — a pod can look ready seconds before it can run your code — but the real wait.

Every number is a **reproducible floor, not a ceiling**: what a *vanilla* OSS build delivers
today (upstream controller from `main`, default runtime, no tuning). A bigger pool or denser
nodes should beat it. And the page prints the truth when it is unflattering — an unmeasured
cell reads `pending` (never a guess), a throughput that misses its bar prints an honest `0`,
and any execution failures show as a ⚠️ fraction instead of being quietly dropped.

Don't take our word for it — reproduce any row, then beat it:

```
bash recipe/install-controller-from-main.sh   # OSS controller from upstream main
python3 -m harness.run                        # run the portable suite (cluster=kind)
python3 -m render.generate                    # regenerate this page
bash scripts/check-public-safety.sh           # fail-closed public-safety scan
```

The headline numbers are all here. The corroboration and decomposition tables — the
working behind them — live in the deep-dive appendix, [DETAILS.md](DETAILS.md).

## Agent Sandbox — Core Metrics

**Read TTFE down a column, not across rows.** Activation-mode rows differ in sample size by orders of magnitude, and a p50 over hundreds of samples and a p50 over one are not comparable: cross-row TTFE ranking is only meaningful between rows with similar sample counts. Rows measured over fewer than N=30 samples are marked † on their TTFE cells.

**Throughput is dual — `per-node · per-cluster`.** The per-node figure is the engineering rate (comparable across runtimes); the per-cluster figure is a MEASURED cluster saturation rate (never a per-node × N extrapolation). Cluster halves render `pending (cluster-fire)` until our own schema-validated saturation fire lands them.

| Runtime | Activation Mode | Throughput @ <5s TTFE (sb/s — node · cluster) | Throughput @ <1s TTFE (sb/s — node · cluster) | TTFE p50 | TTFE p95 | Execution Success (Honesty Check) |
|---|---|---|---|---|---|---|
| gVisor | Warm-pool hit (Base image) | 33.824 /node · pending (cluster-fire) | 32.696 /node · pending (cluster-fire) | 0.6317s | 0.9454s | 100% |
| gVisor | Unique-image cold (RL reality) | pending | pending | 4.5191s † | 4.5191s † | 100% |
| gVisor | Resume-from-suspend | pending (upstream-blocked) | pending (upstream-blocked) | pending (upstream-blocked) | pending (upstream-blocked) | pending (upstream-blocked) |
| Kata + microVM | Warm-pool hit (Base image) | 16.798 /node · pending (cluster-fire) | 15.678 /node · pending (cluster-fire) | 0.6303s | 0.9867s | 100% |
| Kata + microVM | Unique-image cold (RL reality) | pending | pending | 4.8274s † | 4.8274s † | 100% |
| Kata + microVM | Resume-from-suspend | N/A | N/A | N/A | N/A | N/A |

_TTFE = Time-To-First-Instruction: the sandbox executed its first instruction and returned a result — not merely pod-Ready._
_Throughput @ <1s renders the harness-emitted `0` when the p95 misses the 1s bar (we print a zero rather than round up)._
_Throughput cells are dual — `per-node · per-cluster`. The per-node figure is the engineering rate; the per-cluster figure is a MEASURED cluster saturation rate, never a per-node × N extrapolation. The cluster half renders `pending (cluster-fire)` until our own schema-validated saturation fire lands it; a landed figure below the cluster sizing target carries ⚠️._
_Execution Success is the Honesty Check: <100% prints the succeeded/total fraction and a ⚠️ flag._
_† marks a TTFE measured over fewer than N=30 samples — read it as a single observation, not a distribution, and do not rank it against a high-N row._
_Kata + microVM rows are measured in a separate run on the kata node pool: cluster_substrate=gke-kata · node_count=2 · generated-at=2026-07-02T04:51:48Z._
_Resume-from-suspend × Kata + microVM renders `N/A` by construction — CRIU checkpoint/restore does not transfer to the Kata VM isolation model, so that cell can never be measured (distinct from `pending`, which awaits a run)._
_A bare `pending` cell awaits its TTFE-instrumented run. A `pending (upstream-blocked)` cell is different: that run DID land, but an upstream controller gap (the resume path's Suspended condition never clears) holds it — the cell graduates to a real number the moment the upstream fix lands, not merely when a run is scheduled._

_build: cluster_substrate=gke-sandbox · run_id=dc1dd343fee74008a2f75ccdfed39eb9 · node_count=1_
_generated-at: 2026-07-01T07:23:08Z_

## Operating Envelope — what wait should I budget?

Find the row closest to **your** load; the p50 is the wait to plan around. The **Scope** column is load-bearing: the first three rows are the **full** start→first-result wait (TTFE), directly comparable to one another; the last row is only the **pool hand-off** sub-phase (it stops the moment you hold a ready sandbox, before your code runs), so do **not** rank its number against the full-TTFE rows above it. Every number is measured, not modelled — an unmeasured row reads `pending`, never a guess.

| Your load pattern | Wait to budget (p50) | Scope |
|---|---|---|
| Steady trickle — warm pool keeps up with demand | ~0.6s | full start → first result |
| Bursty — pool oversubscribed 2:1 (60 claims / 30 ready) | ~1.7s | full start → first result |
| 300 sandboxes requested at once (1:1 pool) | ~6.9s | full start → first result |
| Sustained 300/sec churn | ~2.9s | pool hand-off only (before exec) |

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

## Scale Proof (Linearity Check)

| Nodes Tested | Density Holds Flat? | Throughput Holds Flat? |
|---|---|---|
| 1 → 2 → 4 → 8 → 16 | ✅ Yes (0.63 → 0.63 → 0.63 → 0.63 → 0.63) | ⚠️ No |

_Per-step density retention: 1→2 ✅ 1 · 2→4 ✅ 1 · 4→8 ✅ 1 · 8→16 ✅ 1 — holds flat step-to-step._

_Measured 2026-06-29 — node-count linearity sweep (point-in-time; refreshed on the next multi-node sweep)._

## Concurrent Burst — TTFE at N simultaneous claims

Each row is a **single all-at-once burst of N concurrent claims** (not a ramped per-second rate). TTFE is the same metric the Core Metrics matrix reports (executed-first-instruction-and-returned-a-result), so these columns **are comparable to the matrix TTFE columns**. *Warm pool* fires against a pre-provisioned pool of N ready sandboxes; *cold provision* starts from an empty pool (node-autoscaler + image-pull in the critical path). Measured on node_count=20, `e2-standard-16`.

| Concurrency (N) | Activation Mode | TTFE p50 | TTFE p95 | Throughput @ <5s/node | Throughput @ <1s/node | Execution Success |
|---|---|---|---|---|---|---|
| 300 | Warm pool | 6.8743s | 9.393s | 0.392 | 0 | 100% |
| 300 | Cold provision | 56.0294s | 58.4124s | 0 | 0 | 100% |
| 500 | Warm pool | 11.188s | 15.374s | 0.052 | 0 | 100% |
| 500 | Cold provision | 97.3988s | 99.8002s | 0 | 0 | 100% |

_Measured 2026-06-30 — concurrent-burst TTFE (point-in-time)._

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
