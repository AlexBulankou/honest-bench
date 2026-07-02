# Honest benchmarks — GKE agent sandbox

Most sandbox benchmarks are marketing: one best-case number, measured once, on a cluster you
will never get. This page is the opposite — **every number is machine-rendered from a real
harness run, and reproducible** (the exact steps are in **Reproduce it** at the bottom). No cell
is typed by hand; anything the schema does not declare is dropped before it reaches the page.

We measure the one thing you actually feel — **TTFE (Time-To-First-Instruction)**: the wall-clock
from "create this sandbox" to "it ran my first instruction and returned a result." Not pod-Ready
(a pod can look ready seconds before it can run your code) — the real wait.

Every number is a **reproducible floor, not a ceiling** — what a *vanilla* OSS build delivers
today (upstream controller from `main`, default runtime, no tuning); a bigger pool or denser nodes
should beat it. The page also prints the truth when it is unflattering: an unmeasured cell reads
`pending` (never a guess), a throughput that misses its bar prints an honest `0`, and execution
failures show as a ⚠️ fraction rather than being quietly dropped. The working behind these
headline tables lives in the deep-dive appendix, [DETAILS.md](DETAILS.md).

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

A warm-pool provision is **7.28251× faster** than a true-cold start (gVisor) — both legs measured the same way (TTFE (executed first-instruction)). Full leg-by-leg table and the cross-block caveats are in the deep-dive appendix, [DETAILS.md](DETAILS.md).

_Measured 2026-07-02 — warm-vs-cold speedup (point-in-time; refreshed on the next TTFE fire)._

## Does it hold at cluster scale?

Two questions a bigger cluster raises: does throughput stay flat as you add nodes (**linearity**), and what does a single all-at-once burst of N claims cost (**concurrency**)? Both below, on the same TTFE spine as the headline matrix.

### Linearity — throughput and density hold flat as nodes grow

| Nodes Tested | Density Holds Flat? | Throughput Holds Flat? |
|---|---|---|
| 1 → 2 → 4 → 8 → 16 | ✅ Yes (0.63 → 0.63 → 0.63 → 0.63 → 0.63) | ⚠️ No |

_Per-step density retention: 1→2 ✅ 1 · 2→4 ✅ 1 · 4→8 ✅ 1 · 8→16 ✅ 1 — holds flat step-to-step._

_Measured 2026-06-29 — node-count linearity sweep (point-in-time; refreshed on the next multi-node sweep)._

### Concurrent burst — TTFE at N simultaneous claims

Each row is a **single all-at-once burst of N concurrent claims** (not a ramped per-second rate). TTFE is the same metric the Core Metrics matrix reports (executed-first-instruction-and-returned-a-result), so these columns **are comparable to the matrix TTFE columns**. *Warm pool* fires against a pre-provisioned pool of N ready sandboxes; *cold provision* starts from an empty pool (node-autoscaler + image-pull in the critical path). Measured on node_count=20, `e2-standard-16`.

| Concurrency (N) | Activation Mode | TTFE p50 | TTFE p95 | Throughput @ <5s/node | Throughput @ <1s/node | Execution Success |
|---|---|---|---|---|---|---|
| 300 | Warm pool | 6.8743s | 9.393s | 0.392 | 0 | 100% |
| 300 | Cold provision | 56.0294s | 58.4124s | 0 | 0 | 100% |
| 500 | Warm pool | 11.188s | 15.374s | 0.052 | 0 | 100% |
| 500 | Cold provision | 97.3988s | 99.8002s | 0 | 0 | 100% |

_Measured 2026-06-30 — concurrent-burst TTFE (point-in-time)._

## Where it breaks today (honest limits)

The Concurrent Burst legs above are **1:1** — N ready sandboxes hit with N claims. This is the deliberate **retraction**: the operating point where the pool is **over-subscribed** (more concurrent claims than ready pool members), and warm activation **stops being sub-second**. Measured on **gVisor**: a pool of **30** ready sandboxes hit with **60** simultaneous claims (**2:1 contention**). Every claim still binds, but the over-subscription serializes the bind path — so the "warm hit is <1s" claim from the Core Metrics matrix does **not** hold here. Cluster shape: node_count=1, `e2-standard-16`.

Under this contention, TTFE degrades to **1.6589s p50** / **2.0169s p95** — budget for that, not the sub-second warm hit, when your claim rate can outrun your pool. Full bind/exec decomposition is in the deep-dive appendix, [DETAILS.md](DETAILS.md).

_Measured 2026-07-01 — warm-pool at-scale contention ceiling (point-in-time)._

## Reproduce it

Every number above comes from a *vanilla* GKE architecture you can provision yourself — no
private tuning. The **runnable** version (exact commands, pinned installs, dispatch-only CI)
lives in [`recipe/REPRODUCE.md`](recipe/REPRODUCE.md); the load-bearing cluster shape is:

- **Cluster** — a regional GKE Standard cluster on **Kubernetes ≥ 1.31** with a **gVisor**-enabled
  node pool (`--enable-sandbox=type=gvisor`, which installs the `gvisor` `RuntimeClass` the burst
  pins to) on a **16-vCPU** machine type (e.g. `e2-standard-16`). Set the pool's autoscaling max
  to the node count the headline needs *before* the fire, on a **`/16`** pod CIDR, so the burst
  tops out on the sandbox path — not the autoscaler or IP exhaustion.
- **Warm pool** — size the `SandboxWarmPool` so a ready slot waits for each claim (replicas ≈
  active-concurrency × 0.75, replenished at the claim rate); otherwise a sustained burst drains
  into the cold-overflow path partway through. When a drained-regime fire is on the page, the
  Warm-Pool decomposition (in [DETAILS.md](DETAILS.md)) names the scaling term directly.
- **Zero-cold-start** — run an image pre-pull **`DaemonSet`** (`recipe/prepull-daemonset.yaml`) so
  a node that joins mid-burst adds no image-pull tax to the first sandbox scheduled onto it.

**Honesty:** a row marked `pending` is not-yet-measured — never a provisional number dressed as a
result. The **sub-1s @ 300/s warm headline is not yet published**; the honest published-today
figures are exactly the measured cells above (Core Metrics + **Concurrent Burst**) plus the
**Warm-Pool Acquisition** decomposition in [DETAILS.md](DETAILS.md) — the recipe points at those
cells rather than restate a number that could drift out of sync. TRUE-TTFE (webhook-stamped
first-instruction) stays `pending` until the upstream stamper lands.
