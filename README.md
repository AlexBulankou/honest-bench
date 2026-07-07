# Honest benchmarks — GKE agent sandbox

**How fast can your agent get a sandbox that has actually run its first instruction?** That is the
only question this page answers. The metric is **TTFE (Time-To-First-Instruction)** — the wall-clock
from "create this sandbox" to "it ran my first instruction and returned a result." Not pod-Ready (a
pod can look ready seconds before it can run your code) — the real wait.

**North Star:** a warm sandbox with **TTFE p95 under 1s** — the bar the scorecard below grades
against (a stricter **0.5s stretch bar** is tracked separately, below the scorecard). The **scale
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

**Throughput is dual — `per-node · per-cluster`.** Per-cluster figures here are a MEASURED cluster rate at 10 nodes; see the legend below for how to read the pair.
*gVisor per-cluster rates: derived from the literal exec-probe warm p95 — an UPPER bound on TTFE (includes exec setup overhead), so compliance at the bar is conservative; fills the <5s cell ONLY (the <1s cell stays honest-empty under this basis) — throughput is the acquisition rate: fulfilled (claim->bound)/s, steady-state, pending claims excluded; trust-gated per rung on agreement with the independent controller completion rate (divergent rungs are ineligible).*

| Runtime | Activation Mode | Throughput @ <5s TTFE (sb/s — node · cluster) | Throughput @ <1s TTFE (sb/s — node · cluster) | TTFE p50 | TTFE p95 | Execution Success (Honesty Check) |
|---|---|---|---|---|---|---|
| gVisor | Warm-pool hit (Base image) | 38.026 /node · 1.204 /cluster ⚠️ | 38.026 /node · [pending (trust-gate)](WORK_IN_PROGRESS.md#trust-gate) [#940](https://github.com/kubernetes-sigs/agent-sandbox/issues/940)→[#1087 in review](https://github.com/kubernetes-sigs/agent-sandbox/pull/1087) | 0.7556s (count=30) | 0.9222s (count=30) | 100% |
| gVisor | Unique-image cold (RL reality) | [pending (no-compliant-rung)](WORK_IN_PROGRESS.md#no-compliant-rung) [#751](https://github.com/kubernetes-sigs/agent-sandbox/issues/751)→[#761 open](https://github.com/kubernetes-sigs/agent-sandbox/pull/761) | 0 /node · 0 /cluster | 3.4518s (count=30) | 3.7599s (count=30) | 100% |
| gVisor | Resume-from-suspend | [pending (upstream-blocked)](WORK_IN_PROGRESS.md#upstream-blocked) [#873](https://github.com/kubernetes-sigs/agent-sandbox/issues/873)→[#893 in review](https://github.com/kubernetes-sigs/agent-sandbox/pull/893) | [pending (upstream-blocked)](WORK_IN_PROGRESS.md#upstream-blocked) [#873](https://github.com/kubernetes-sigs/agent-sandbox/issues/873)→[#893 in review](https://github.com/kubernetes-sigs/agent-sandbox/pull/893) | [pending (upstream-blocked)](WORK_IN_PROGRESS.md#upstream-blocked) [#873](https://github.com/kubernetes-sigs/agent-sandbox/issues/873)→[#893 in review](https://github.com/kubernetes-sigs/agent-sandbox/pull/893) | [pending (upstream-blocked)](WORK_IN_PROGRESS.md#upstream-blocked) [#873](https://github.com/kubernetes-sigs/agent-sandbox/issues/873)→[#893 in review](https://github.com/kubernetes-sigs/agent-sandbox/pull/893) | [pending (upstream-blocked)](WORK_IN_PROGRESS.md#upstream-blocked) [#873](https://github.com/kubernetes-sigs/agent-sandbox/issues/873)→[#893 in review](https://github.com/kubernetes-sigs/agent-sandbox/pull/893) |
| Kata + microVM | Warm-pool hit (Base image) | 18.624 /node · [pending (trust-gate)](WORK_IN_PROGRESS.md#trust-gate) [#940](https://github.com/kubernetes-sigs/agent-sandbox/issues/940)→[#1087 in review](https://github.com/kubernetes-sigs/agent-sandbox/pull/1087) | 18.624 /node · [pending (trust-gate)](WORK_IN_PROGRESS.md#trust-gate) [#940](https://github.com/kubernetes-sigs/agent-sandbox/issues/940)→[#1087 in review](https://github.com/kubernetes-sigs/agent-sandbox/pull/1087) | 0.733s (count=30) | 0.9628s (count=30) | 100% |
| Kata + microVM | Unique-image cold (RL reality) | [pending (no-compliant-rung)](WORK_IN_PROGRESS.md#no-compliant-rung) [#751](https://github.com/kubernetes-sigs/agent-sandbox/issues/751)→[#761 open](https://github.com/kubernetes-sigs/agent-sandbox/pull/761) | 0 /node · 0 /cluster | 2.8537s (count=30) | 3.1263s (count=30) | 100% |
| Kata + microVM | Resume-from-suspend | [N/A](WORK_IN_PROGRESS.md#na-by-construction) | [N/A](WORK_IN_PROGRESS.md#na-by-construction) | [N/A](WORK_IN_PROGRESS.md#na-by-construction) | [N/A](WORK_IN_PROGRESS.md#na-by-construction) | [N/A](WORK_IN_PROGRESS.md#na-by-construction) |

### Max Density (sandboxes per vCPU)

Density is per-**runtime** — constant across a runtime's activation-mode rows above, so it renders as a compact per-runtime sub-table here rather than a matrix column (a column would repeat each value down the mode rows and imply a mode-dependence that does not exist). Full methodology (per-vCPU denominator, saturation source) is in [DETAILS.md](DETAILS.md).

| Runtime | Max Density (sb/vCPU) |
|---|---|
| gVisor | 5.98 |
| Kata + microVM | 1.26 |

**How to read the cells**

- **TTFE** — Time-To-First-Instruction: wall-clock from asking for a sandbox until your agent's first instruction has run and returned a result — not merely pod-Ready.
- **p50 / p95** — median / worst-in-20; plan UX around p95. Read TTFE *down* a column, not across rows — activation-mode rows differ in sample size by orders of magnitude (each cell shows its own `(count=N)`), so only rows with similar N are comparable.
- **Warm-pool hit vs. Unique-image cold (RL reality)** — a warm-pool hit is served from a pre-started idle pool (startup already paid); the unique-image-cold row is a fresh sandbox on a never-pulled image — image pull + cold start on the critical path, the worst case a reinforcement-learning training loop actually hits.
- **Throughput `x /node · y /cluster`** — per-node is the engineering rate (comparable across runtimes); per-cluster is a MEASURED per-activation-mode rate at the node count named in the bold caption above the table — the per-cluster fire is separate from the per-node fire, so the build line's `node_count` (the per-node fire's shape) does not apply to it — never a per-node × N extrapolation.
- **honest `0`** — the measurement ran and could not hold the bar: the measured TTFE p95 misses that cell's SLO, so the SLO-compliant throughput is a real `0` (we print it rather than round up) — not "zero activity". A derived `0` inherits the sample basis of the p95 it reads, so a single-sample p95 yields a single-sample `0` carrying †.
- **measured `0` (floor-zero)** — the second zero provenance, distinct from the derived `0` above: here the SLO-rate fire itself RAN and emitted a stamped zero — at the lowest offered rate fired, the majority of samples missed the bar by a pre-declared margin even after granting every unevaluable sample a pass, so no compliant operating point exists at or above the floor. When this basis is in play the italic basis line above the table names it; a derived `0` instead reads off a measured TTFE p95 with no throughput fire behind it.
- **†** — measured over fewer than N=30 samples: read it as a single observation, not a distribution; do not rank it against a high-N row.
- **⚠️** — a miss flag: on Execution Success it marks <100% (and prints the succeeded/total fraction); on a per-cluster throughput figure it marks a rate below the cluster sizing target.
- **`pending`** — awaits its TTFE-instrumented run (a genuinely not-yet-run cell).
- **`pending (upstream-blocked)`** — the run DID land, but an upstream controller gap (the resume path's Suspended condition never clears) holds it; it graduates to a real number the moment the upstream fix lands, not merely when a run is scheduled. Tracked upstream: [agent-sandbox#873](https://github.com/kubernetes-sigs/agent-sandbox/issues/873) (issue, open) → fix [agent-sandbox#893](https://github.com/kubernetes-sigs/agent-sandbox/pull/893) (PR, in review).
- **`pending (cluster-fire)`** — the per-node figure is measured, but the per-cluster half awaits a schema-validated per-mode cluster-throughput fire (distinct from the whole-cluster Saturation ceiling in DETAILS, which measures the aggregate ceiling at overload, not these SLO-gated per-mode cells).
- **`pending (trust-gate)`** — the per-cluster SLO-rate fire RAN, but derivation was refused by the acquire/controller agreement gate (rel-diff tolerance 0.10) at every measured rung: the upstream controller startup-latency histogram double-records Ready transitions on stale-informer replays, inflating the controller leg ~1.7–2× on warm-pool-fulfilled paths (cold control legs PASS the same gate on both runtimes). Publishing honest-empty beats publishing a rate the gate can't trust. Tracked upstream: [agent-sandbox#940](https://github.com/kubernetes-sigs/agent-sandbox/issues/940) (issue, open) → fix [agent-sandbox#1087](https://github.com/kubernetes-sigs/agent-sandbox/pull/1087) (PR, in review).
- **`pending (no-compliant-rung)`** — the per-cluster SLO-rate fire RAN with the trust gate PASSING, but every measured rung's p95 (on the literal-TTFE upper-bound basis) sits over this cell's SLO bar — an SLO-gated rate can't be published as `0` from a finite ladder unless a pre-declared floor condition holds, and the true-TTFE basis that could tighten the bound has no production writer upstream yet. Tracked upstream: [agent-sandbox#751](https://github.com/kubernetes-sigs/agent-sandbox/issues/751) (issue, open) → fix [agent-sandbox#761](https://github.com/kubernetes-sigs/agent-sandbox/pull/761) (PR, open).
- **`N/A`** — `N/A` by construction: Resume-from-suspend × Kata + microVM can never be measured — CRIU checkpoint/restore does not transfer to the Kata VM isolation model — distinct from `pending`, which awaits a run.
- **Why a `pending` is not just printed as `0`** — a blunter display rule would print `0` for any cell that cannot show compliance; each pending flavor above documents why that would over-claim here: an upper-bound latency basis cannot prove a true miss (`no-compliant-rung`), a failed agreement gate cannot certify a rate in either direction (`trust-gate`), and a floor rung whose samples are majority-unevaluable cannot establish the negative claim (the floor-zero predicate's evaluability cap). Each such cell graduates — to a measured rate or a floor-zero `0` — the moment its condition clears.

_Kata + microVM rows are measured in a separate run on the kata node pool: cluster_substrate=gke-kata · node_count=2 · generated-at=2026-07-05T15:32:54Z._

_build: cluster_substrate=gke-sandbox · run_id=c95500d1be8f439fb3a149b68aab707a · node_count=1_
_generated-at: 2026-07-04T18:32:56Z_

## What this means for you

The tables above are the raw measurements. If you build *on* sandboxes but do not run the cluster yourself, here is what they mean in practice:

- **Keep a warm pool sized to demand and a new sandbox is ready in ~0.8s (~0.9s at the p95).** That is fast enough to put a fresh sandbox directly in a user-facing request path — no need to hide it behind a spinner or pre-allocate one per session.
- **A warm-pool hit is about 10× faster than starting cold (gVisor).** If start-up latency matters to you, the warm pool is the single biggest lever — size it for your steady demand and most claims never pay the cold path. (This ratio is the dedicated warm-vs-cold leg — a separate point-in-time measurement from the Core Metrics matrix rows above, so do not reproduce it by dividing the matrix cells.)
- **Big simultaneous bursts still work — 300 sandboxes asked for at once settled in ~6.9s.** But that is the pool-overflow regime: the wait climbs toward the cold-start number as claims outrun ready slots, so plan the pool around your steady rate, not your worst spike.
- **Rule of thumb for pool size:** start near your typical concurrent demand (≈0.75× of it) and tune from there. This is a planning heuristic, not one of the measured numbers above.
- **Both runtimes are measured — choose by isolation need.** In the measurements above, warm-pool latency is comparable between them; gVisor delivers the higher per-node throughput, while Kata + microVM puts each sandbox in its own VM for hardware-grade isolation. If unsure, start with gVisor and move only the workloads that need a VM boundary to Kata.
- **Do not design around suspend/resume yet.** gVisor resume is blocked upstream, and Kata resume is `N/A` by construction (checkpoint-restore does not transfer to the VM model) — treat it as unavailable until the gVisor cells show real numbers.
- **A cell marked `pending` is unmeasured, not bad.** It means that measurement has not run yet (or is blocked upstream) — never that the platform failed it.

### What wait should I budget?

Find the row closest to **your** load; the p50 is the wait to plan around. The **Scope** column is load-bearing: the first three rows are the **full** start→first-result wait (TTFE), directly comparable to one another; the last row is only the **pool hand-off** sub-phase (it stops the moment you hold a ready sandbox, before your code runs), so do **not** rank its number against the full-TTFE rows above it. Every number is measured, not modelled — an unmeasured row reads `pending`, never a guess.

| Your load pattern | Wait to budget (p50) | Scope |
|---|---|---|
| Steady trickle — warm pool keeps up with demand | ~0.8s | full start → first result |
| Bursty — pool oversubscribed 2:1 (60 claims / 30 ready) | ~1.7s | full start → first result |
| 300 sandboxes requested at once (1:1 pool) | ~6.9s | full start → first result |
| Sustained 300/sec churn | ~2.9s | pool hand-off only (before exec) |

### How close to the North Star?

The North Star is the bar in the spec doc: a warm-pool hit with TTFE p95 under 1s (spec doc: _Our North Star is < 1 second Time-To-First-Instruction_). This scorecard prints the measured distance to that target rather than leaving it implied. The 5s/1s throughput bars in the matrix above are today's operating envelope; the stricter 0.5s stretch bar (below) is an aspiration, not the North Star.

| Runtime | Warm-pool-hit TTFE p95 (measured) | North Star (p95 < 1s) |
|---|---|---|
| gVisor | 0.9222s (count=30) | ✅ met (0.0778s headroom) |
| Kata + microVM | 0.9628s (count=30) | ✅ met (0.0372s headroom) |

_An honest ❌ beats an implied pass: the page prints the measured distance to the target it misses, and a met bar prints its measured headroom. A miss that sits inside the sample spread is tagged `within sampling noise` (it stays a ❌ — the tag never flips a miss to a pass). An unmeasured runtime reads `pending` — never a guess. † marks a p95 measured over fewer than N=30 samples (a single observation, not a distribution)._

### Stretch bar — warm-pool TTFE p95 < 0.5s

Beyond the North Star, the page tracks a stricter 0.5s stretch target (landed via hb#148). It is an aspiration the runtimes are climbing toward — **not** the North Star, and a miss here is expected while the North Star is the live bar. The step-up curve's verdict grades sustained creation rate against this same stretch bar (reported in [DETAILS.md](DETAILS.md)).

| Runtime | Warm-pool-hit TTFE p95 (measured) | Stretch (p95 < 0.5s) |
|---|---|---|
| gVisor | 0.9222s (count=30) | ❌ not met (0.4222s above the bar) |
| Kata + microVM | 0.9628s (count=30) | ❌ not met (0.4628s above the bar) |

## Does it hold at cluster scale?

Four questions a bigger cluster raises: does throughput stay flat as you add nodes (**linearity**), what does a single all-at-once burst of N claims cost (**concurrency**), where does the whole-cluster warm hand-out rate saturate (**ceiling**), and what happens when the pool is over-subscribed (**contention**)? All below, on the same TTFE spine as the headline matrix.

### Linearity — throughput and density hold flat as nodes grow

| Nodes Tested | Density Holds Flat? | Throughput Holds Flat? |
|---|---|---|
| 1 → 2 → 4 → 8 → 16 | ✅ Yes (0.63 → 0.63 → 0.63 → 0.63 → 0.63) | ⚠️ No |

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

_Measured 2026-06-30 — concurrent-burst TTFE (point-in-time)._

### Saturation — the whole-cluster warm-hand-out ceiling

The Concurrent Burst legs above are small 1:1 warm bursts. This is the **saturation** ceiling: a **1:1 all-warm** fire — a pool of **600** ready sandboxes hit with **600** simultaneous claims (**not** over-subscribed), spread across **40** nodes on **gVisor**. Every claim has a ready warm pool member, yet at this scale the bind path itself saturates — so the whole-cluster warm hand-out rate collapses far below the per-node engineering rate, and the "warm hit is <1s" claim from the Core Metrics matrix does **not** hold here. Cluster shape: `n2-standard-16`.

At **40 nodes** the cluster sustains only **2.558 claims/sec under 5s** (**0/sec under 1s**) across the whole cluster, and TTFE degrades to **8.6308s p50** / **12.6103s p95**. This is the honest per-cluster hand-out ceiling — budget for it when your claim rate can outrun the bind path, not for the sub-second per-node warm hit. Full per-node/per-cluster and bind/exec decomposition is in the deep-dive appendix, [DETAILS.md](DETAILS.md).

_SLA ceiling: **not met** at this operating point — this row is the honest saturation limit, not a warm-hit guarantee. Every claim still bound and executed; the FAIL is the throughput collapse against the sizing floor, not a correctness failure._

_Measured 2026-07-02 — whole-cluster saturation ceiling (point-in-time)._

### Where it breaks — an over-subscribed pool

The Concurrent Burst legs above are **1:1** — N ready sandboxes hit with N claims. This is the deliberate **retraction**: the operating point where the pool is **over-subscribed** (more concurrent claims than ready pool members), and warm activation **stops being sub-second**. Measured on **gVisor**: a pool of **30** ready sandboxes hit with **60** simultaneous claims (**2:1 contention**). Every claim still binds, but the over-subscription serializes the bind path — so the "warm hit is <1s" claim from the Core Metrics matrix does **not** hold here. Cluster shape: node_count=1, `e2-standard-16`.

Under this contention, TTFE degrades to **1.6589s p50** / **2.0169s p95** — budget for that, not the sub-second warm hit, when your claim rate can outrun your pool. Full bind/exec decomposition is in the deep-dive appendix, [DETAILS.md](DETAILS.md).

_Measured 2026-07-01 — warm-pool at-scale contention ceiling (point-in-time)._

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
