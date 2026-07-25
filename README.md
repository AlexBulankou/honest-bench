# Honest benchmarks — GKE agent sandbox

**How fast can your agent get a sandbox that has actually run its first instruction?** That is the
only question this page answers. The metric is **TTFE (Time-To-First-Instruction)** — the wall-clock
from "create this sandbox" to "it ran my first instruction and returned a result." Not pod-Ready (a
pod can look ready seconds before it can run your code) — the real wait.

**North Star:** a warm sandbox with **TTFE p95 under 1s** — the bar the caption under the matrix
grades each runtime against (a stricter **0.5s stretch bar** is graded on the same line). The **scale
target** is to hold **sub-1s at 300+ creations/sec**, on a stock GKE cluster you can provision
yourself.

Two runtimes, two isolation trade-offs:
- **gVisor** — a user-space kernel intercepting syscalls; near-container speed, strong isolation.
- **Kata** — each sandbox in its own tiny VM; hardware-grade isolation, higher activation cost.

Every number below is **machine-rendered from a real harness run and reproducible** — no cell is
typed by hand, and each is a **floor, not a ceiling** (what a *vanilla* OSS build delivers today;
a bigger pool or denser nodes should beat it). Reproduce the whole page in four commands:

```bash
kind create cluster                                # 0. portable path — a free local cluster
bash recipe/install-controller-from-main.sh        # 1. the OSS controller, built from upstream main
python3 -m harness.run                             # 2. run the suite -> sandbox/results/latest.json
python3 -m render.generate && git diff README.md   # 3. re-render this page + diff the result
```

First run `pip install -r harness/requirements.txt`; the gVisor/Kata rows need a matching GKE node
pool — full recipe in [`recipe/REPRODUCE.md`](recipe/REPRODUCE.md), deep-dive tables in
[DETAILS.md](DETAILS.md). Cells shown as *pending* link to
[WORK_IN_PROGRESS.md](WORK_IN_PROGRESS.md) for their reason class; the upstream half of each
blocker — diagnosis plus file-ready patches and comments — is hand-maintained in
[UPSTREAM_BLOCKERS.md](UPSTREAM_BLOCKERS.md).

## Agent Sandbox — Core Metrics

**Throughput is dual — `per-node · per-cluster`.** Per-cluster figures are measured per runtime at DIFFERENT node counts — gVisor at 4 nodes; Kata + microVM at 5 nodes — so they are NOT comparable across runtimes here (different X); see the legend below.
**[`***Z`]** *gVisor per-cluster rates: a measured ZERO, not an absence: the controller cold-start floor exceeds BOTH bars at every offered rate (rate-independent), so no compliant operating point exists — the zero is the sandbox cold-start floor, not an acquire-path miss (the acquire-side latency is clean sub-second at every rung). Corroborated by a controller-MEASURED (trusted) rung whose cold p50 is also over both bars, so it is never asserted from the controller-untrusted floor rung alone.*
**[`***K`]** *Kata + microVM per-cluster rates: neither a compliant rate nor an honest zero: a measurement was taken, but the true TTFE p95 is bounded in a bracket that STRADDLES the bar — the lower-bound proxy does not breach the bar (so no honest-zero) and the upper-bound literal exec-probe does not clear it (so no positive rate), leaving the claim unresolved by construction. Distinct from a pending cell: the measurement exists, the bar is provably unresolvable at this operating point, not merely unmeasured.*

| Runtime | Activation Mode | Throughput @ <5s TTFE (sb/s — node · cluster) | Throughput @ <1s TTFE (sb/s — node · cluster) | TTFE p50 | TTFE p95 | Execution Success (Honesty Check) |
|---|---|---|---|---|---|---|
| gVisor | Warm-pool hit (Base image) ⚠️ FAIL | 6.214 /node · 7.727 /cluster ⚠️ | 0 /node · 7.727 /cluster ⚠️ | 3.3247s (count=30) | 4.8563s (count=30) | 100% |
| gVisor | Unique-image cold (RL reality) | 0 /node · 0 /cluster ⚠️***Z | 0 /node · 0 /cluster ⚠️***Z | 4.5097s (count=30) | 5.0137s (count=30) | 100% |
| gVisor | Resume-from-suspend | [pending (upstream-blocked)](WORK_IN_PROGRESS.md#upstream-blocked) [#873](https://github.com/kubernetes-sigs/agent-sandbox/issues/873)→[#1150 in review](https://github.com/kubernetes-sigs/agent-sandbox/pull/1150) | [pending (upstream-blocked)](WORK_IN_PROGRESS.md#upstream-blocked) [#873](https://github.com/kubernetes-sigs/agent-sandbox/issues/873)→[#1150 in review](https://github.com/kubernetes-sigs/agent-sandbox/pull/1150) | [pending (upstream-blocked)](WORK_IN_PROGRESS.md#upstream-blocked) [#873](https://github.com/kubernetes-sigs/agent-sandbox/issues/873)→[#1150 in review](https://github.com/kubernetes-sigs/agent-sandbox/pull/1150) | [pending (upstream-blocked)](WORK_IN_PROGRESS.md#upstream-blocked) [#873](https://github.com/kubernetes-sigs/agent-sandbox/issues/873)→[#1150 in review](https://github.com/kubernetes-sigs/agent-sandbox/pull/1150) | [pending (upstream-blocked)](WORK_IN_PROGRESS.md#upstream-blocked) [#873](https://github.com/kubernetes-sigs/agent-sandbox/issues/873)→[#1150 in review](https://github.com/kubernetes-sigs/agent-sandbox/pull/1150) |
| Kata + microVM | Warm-pool hit (Base image) | 9.132 /node · 0.835 /cluster ⚠️ | 0 /node · [pending (cluster-fire)](WORK_IN_PROGRESS.md#cluster-fire) | 1.7883s (count=30) | 2.445s (count=30) | 100% |
| Kata + microVM | Unique-image cold (RL reality) | unk.***K | 0 /node · 0 /cluster | 3.2562s (count=30) | 3.4949s (count=30) | 100% |
| Kata + microVM | Resume-from-suspend | [N/A](WORK_IN_PROGRESS.md#na-by-construction) | [N/A](WORK_IN_PROGRESS.md#na-by-construction) | [N/A](WORK_IN_PROGRESS.md#na-by-construction) | [N/A](WORK_IN_PROGRESS.md#na-by-construction) | [N/A](WORK_IN_PROGRESS.md#na-by-construction) |

_⚠️ **Scenario FAIL:** **gVisor** Warm-pool hit (Base image) — the row above carries a real measurement whose own scenario outcome is **FAIL** (SLA not met), not a passing warm hit. The numbers are honest data, disclosed as a miss rather than dropped or greened; a later refresh whose scenario returns to PASS clears this._

### Max Density (sandboxes per vCPU)

Density is per-**runtime** — constant across a runtime's activation-mode rows above, so it renders as a compact per-runtime sub-table here rather than a matrix column (a column would repeat each value down the mode rows and imply a mode-dependence that does not exist). Full methodology (per-vCPU denominator, saturation source) is in [DETAILS.md](DETAILS.md).

| Runtime | Max Density (sb/vCPU) |
|---|---|
| gVisor | 5.98 |
| Kata + microVM | 1.26 |

**Reading the cells** — TTFE is Time-To-First-Instruction (wall-clock until your agent's first instruction returns, not merely pod-Ready). Read TTFE p50/p95 *down* a column, not across rows — activation-mode rows differ in sample size by orders of magnitude (each cell shows its own `(count=N)`). `†` marks a sub-N sample (a single observation, not a distribution); `⚠️` is a miss flag (sub-100% Execution Success, or a per-cluster rate below the sizing target); `pending` (and its `(upstream-blocked)` / `(cluster-fire)` / `(trust-gate)` / `(no-compliant-rung)` flavors) means the cell has no publishable figure yet.

Full cell-decoding key — TTFE basis, honest vs. measured zeros, the dual per-node · per-cluster throughput pair, the certification-floor `≥` figures, every `pending` flavor, and the published-with-caveat tag classes — is in [DETAILS.md](DETAILS.md#how-to-read-the-core-metrics-cells).

_Kata + microVM rows are measured in a separate run on the kata node pool: cluster_substrate=gke-kata · node_count=1 · generated-at=2026-07-24T21:02:55Z._

_build: cluster_substrate=gke-sandbox · controller_digest=sha256:d2490a0134fa81311cf6d576849d98436f618f9fb49a2826ae3b916c284f955e · suite_git_sha=512b1bf70af5cb5d5ae1b736e52833597942a6dc · run_id=8564d405229f42f2929615927d7699fd · node_count=1_
_generated-at: 2026-07-25T07:42:40Z_

_**North Star** — warm-pool-hit TTFE p95 < 1s (the spec doc bar): gVisor 4.8563s (count=30) ❌ not met (3.8563s above the bar) ⚠️ **scenario FAIL**; Kata + microVM 2.445s (count=30) ❌ not met (1.445s above the bar). An honest ❌ prints the measured gap to the bar (tagged `within sampling noise` when the miss sits inside the sample spread — it stays a ❌, the tag never flips a miss to a pass); `pending` = unmeasured (never a guess); † marks a p95 over fewer than N=30 samples._

_**Stretch bar** — warm-pool-hit TTFE p95 < 0.5s (an aspiration above the North Star, not the North Star itself; the step-up curve grades sustained creation-rate against it — see [DETAILS.md](DETAILS.md)): gVisor 4.8563s (count=30) ❌ not met (4.3563s above the bar) ⚠️ **scenario FAIL**; Kata + microVM 2.445s (count=30) ❌ not met (1.945s above the bar)._

> ⚠️ **Scenario FAIL:** the warm-pool-hit scenario's own outcome is **FAIL** for **gVisor** — the p95 above is a real measurement that MISSED its SLA, not a passing warm hit. It is still graded against the bar and carried forward as the refresh baseline honestly (an SLA-failing number is disclosed, never softened into a green cell); a later refresh whose scenario returns to PASS clears this.

> ⚠️ **Warm/cold separation below gate:** the warm-pool separation ratio (fastest cold start ÷ slowest warm-pool hit) is below the 1.8x gate for **gVisor** (warm count=30): 0.719x (slowest warm 4.24242s vs fastest cold 3.04981s); **Kata + microVM** (warm count=30): 1.02x (slowest warm 2.20717s vs fastest cold 2.25611s) — at ~1x the warm and cold populations overlap, so the published warm tier is not demonstrably faster than a unique-image cold start. The warm row clears the N=30 floor, so this is not a small-sample artifact. The cause is not asserted here (the pool may be under-delivering ready replicas, blending genuinely-cold claims into the warm tier); a later refresh whose ratio returns to the gate clears this.

> ⚠️ **Refresh delta:** **gVisor** regressed by 3.4336s (1.4227s → 4.8563s, 3.4x). A swing this large, or a bar-crossing flip, between consecutive published runs is flagged for a second look before trusting it as a substrate signal — check for a machine-class change, a node-count change, a node-image change, a broken measurement, or a real regression/fix.

> ℹ️ **Regime note:** every automated refresh since **2026-07-20** measures a brand-new, single-node ephemeral CI cluster with an empty containerd cache per run — a deliberately cold pull (see "Reproduce it" below). Numbers published **before 2026-07-20** (e.g. the 2026-07-04 baseline) were instead measured on a long-lived, pre-warmed internal cluster, not by this repo's own CI. If you're comparing today's cold-start figures against an older citation of this page and see a large jump, that's this regime switch — not a code or controller regression.

## What this means for you

The tables above are the raw measurements. If you build *on* sandboxes but do not run the cluster yourself, here is what they mean in practice:

- **Keep a warm pool sized to demand and a new sandbox is ready quickly** — a claim against a ready pool skips the fresh-node startup path. The exact wait to budget is in the operating envelope below once that measurement lands.
- **A warm-pool hit is about 2.9× faster than starting cold (gVisor).** If start-up latency matters to you, the warm pool is the single biggest lever — size it for your steady demand and most claims never pay the cold path. (This ratio is the dedicated warm-vs-cold leg — a separate point-in-time measurement from the Core Metrics matrix rows above, so do not reproduce it by dividing the matrix cells.)
- **Big simultaneous bursts still work — 300 sandboxes asked for at once settled in ~6.9s.** But that is the pool-overflow regime: the wait climbs toward the cold-start number as claims outrun ready slots, so plan the pool around your steady rate, not your worst spike.
- **Rule of thumb for pool size:** start near your typical concurrent demand (≈0.75× of it) and tune from there. This is a planning heuristic, not one of the measured numbers above.
- **Both runtimes are measured — choose by isolation need.** In the measurements above, warm-pool latency is comparable between them; gVisor delivers the higher per-node throughput, while Kata + microVM puts each sandbox in its own VM for hardware-grade isolation. If unsure, start with gVisor and move only the workloads that need a VM boundary to Kata.
- **Do not design around suspend/resume yet.** gVisor resume is blocked upstream, and Kata resume is `N/A` by construction (checkpoint-restore does not transfer to the VM model) — treat it as unavailable until the gVisor cells show real numbers.
- **A cell marked `pending` is unmeasured, not bad.** It means that measurement has not run yet (or is blocked upstream) — never that the platform failed it.

### What wait should I budget?

Find the row closest to **your** load; the p50 is the wait to plan around. The **Scope** column is load-bearing: the first three rows are the **full** start→first-result wait (TTFE), directly comparable to one another; the last row is only the **pool hand-off** sub-phase (it stops the moment you hold a ready sandbox, before your code runs), so do **not** rank its number against the full-TTFE rows above it. Every number is measured, not modelled — an unmeasured row reads `pending`, never a guess.

| Your load pattern | Wait to budget (p50) | Scope |
|---|---|---|
| Steady trickle — warm pool keeps up with demand | [pending](WORK_IN_PROGRESS.md#not-yet-measured) | full start → first result |
| Bursty — pool oversubscribed 2:1 (60 claims / 30 ready) | ~1.7s | full start → first result |
| 300 sandboxes requested at once (1:1 pool) | ~6.9s | full start → first result |
| Sustained 300/sec churn | ~2.9s | pool hand-off only (before exec) |

## Does it hold at cluster scale?

Four questions a bigger cluster raises: does throughput stay flat as you add nodes (**linearity**), what does a single all-at-once burst of N claims cost (**concurrency**), where does the whole-cluster warm hand-out rate saturate (**ceiling**), and what happens when the pool is over-subscribed (**contention**)? All below, on the same TTFE spine as the headline matrix.

### Linearity — throughput and density hold flat as nodes grow

| Nodes Tested | Density Holds Flat? | Throughput Holds Flat? |
|---|---|---|
| 1 → 2 → 4 → 8 → 16 | ✅ Yes (1× · 0.63 → 0.63 → 0.63 → 0.63 → 0.63) | ⚠️ No (0.15×) |

_The density values in this row are the per-node density retained at each node count (a linearity series — does per-node density stay flat as the cluster grows?), not the absolute Max Density per vCPU (reported separately in DETAILS)._

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
| 30 | Warm pool | 2.06969s | 2.9976s | — | — | 100% |
| 30 | Cold provision | 12.3171s | 13.1484s | — | — | 100% |

_Measured 2026-06-30 — concurrent-burst TTFE (point-in-time)._

> ℹ️ **Measurement regime:** this burst ran on a long-lived, **pre-warmed cluster** (warm containerd cache). Fires on or after 2026-07-20 run on cold ephemeral CI clusters and are **not directly comparable** to this baseline.

### Saturation — the whole-cluster warm-hand-out ceiling

The Concurrent Burst legs above are small 1:1 warm bursts. This is the **saturation** ceiling: a **1:1 all-warm** fire — a pool of **600** ready sandboxes hit with **600** simultaneous claims (**not** over-subscribed), spread across **40** nodes on **gVisor**. Every claim has a ready warm pool member, yet at this scale the bind path itself saturates — so the whole-cluster warm hand-out rate collapses far below the per-node engineering rate, and the "warm hit is <1s" claim from the Core Metrics matrix does **not** hold here. Cluster shape: `n2-standard-16`.

At **40 nodes** the cluster sustains only **2.558 claims/sec under 5s** (**0/sec under 1s**) across the whole cluster, and TTFE degrades to **8.6308s p50** / **12.6103s p95**. This is the honest per-cluster hand-out ceiling — budget for it when your claim rate can outrun the bind path, not for the sub-second per-node warm hit. Full per-node/per-cluster and bind/exec decomposition is in the deep-dive appendix, [DETAILS.md](DETAILS.md).

_SLA ceiling: **not met** at this operating point — this row is the honest saturation limit, not a warm-hit guarantee. Every claim still bound and executed; the FAIL is the throughput collapse against the sizing floor, not a correctness failure._

_Measured 2026-07-02 — whole-cluster saturation ceiling (point-in-time)._

### Where it breaks — an over-subscribed pool

The Concurrent Burst legs above are **1:1** — N ready sandboxes hit with N claims. This is the deliberate **retraction**: the operating point where the pool is **over-subscribed** (more concurrent claims than ready pool members), and warm activation **stops being sub-second**. Measured on **gVisor**: a pool of **30** ready sandboxes hit with **60** simultaneous claims (**2:1 contention**). Every claim still binds, but the over-subscription serializes the bind path — so the "warm hit is <1s" claim from the Core Metrics matrix does **not** hold here. Cluster shape: node_count=1, `e2-standard-16`.

Under this contention, TTFE degrades to **1.6589s p50** / **2.0169s p95** — budget for that, not the sub-second warm hit, when your claim rate can outrun your pool. Full bind/exec decomposition is in the deep-dive appendix, [DETAILS.md](DETAILS.md).

_Measured 2026-07-01 — warm-pool at-scale contention ceiling (point-in-time)._

## Throughput — build-over-build

The headline COUNT — sandboxes ready in <1s in a single 1.0s burst against one warm
pool — tracked across distinct controller builds (oldest first). **Δ** is the change in
COUNT vs the prior build; the first build is the baseline. Drive this COUNT up.

| Build (controller digest) | Date | Sandboxes ready <1s | Δ | Density /vCPU | n |
|---|---|---|---|---|---|
| `sha256:6edaf7b6b22d…` | 2026-06-28 | 9 | — | 0.45 | 10 |

## Which storage class should you pick?

Per-class results from a controlled storage-config fire (fixed workload). An unmeasured class renders `pending`; the per-row sample count is the trust gate.

| Storage class | Samples (n) | Payload p50 | Pass rate |
|---|---|---|---|
| Ephemeral (node-local) | 3 | 64 MiB | 100% |
| Persistent disk | 3 | 64 MiB | 100% |
| Snapshot-restored | 3 | 64.51 MiB † | 100% |

_Measured 2026-07-07 — storage-config axis (point-in-time); each class carried an identical controlled write, W = 64 MiB._

† Payload p50 is not measured the same way across classes, so the column is not a like-for-like byte comparison. Ephemeral and persistent-disk write a fixed pattern to a mount and count the **allocated writable-fs blocks** (`du`). The snapshot class instead counts the **checkpoint-artifact object bytes**: a snapshot captures process memory, not the writable-fs layer, so its identical W lives in an incompressible in-memory buffer (a zero-filled buffer would be dropped by the checkpointer's zero-page optimization and never appear in the artifact), and the artifact bytes include checkpoint overhead beyond W. Same controlled W per class; different bytes counted.

## Reproduce it

Every number above comes from a *vanilla* GKE cluster you can provision yourself — no private
tuning. Full runnable steps (commands, pinned installs, dispatch-only CI) are in
[`recipe/REPRODUCE.md`](recipe/REPRODUCE.md). The one rule worth copying into your own setup:
**size the warm pool to your active concurrency** — keep a ready slot waiting for each claim
(replicas ≈ **0.75 × active concurrency**, replenished at the claim rate), or a sustained burst
drains into the slow cold-start path partway through. The cluster shape it needs (a **gVisor** node
pool on a
16-vCPU machine like `e2-standard-16`, the `gvisor` `RuntimeClass` the burst pins to, a `/16` pod
CIDR, and a pre-pull `DaemonSet` for nodes that join mid-burst) is spelled out there too.
When a drained-regime fire is on the page, the Warm-Pool decomposition in
[DETAILS.md](DETAILS.md) names the scaling term directly.

**Honesty:** a row marked `pending` is not-yet-measured — never a provisional number dressed as a
result. The **sub-1s @ 300/s warm headline is not yet published**; the honest published-today
figures are the measured cells above (Core Metrics + **Concurrent Burst**) plus the
**Warm-Pool Acquisition** decomposition in [DETAILS.md](DETAILS.md) — the recipe points at those
cells rather than restate a number that could drift out of sync. TRUE-TTFE (webhook-stamped
first-instruction) stays `pending` until the upstream stamper lands.
