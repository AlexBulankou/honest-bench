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
a bigger pool or denser nodes should beat it). Reproduce the whole page from a GKE cluster in
three commands (cluster setup — a gVisor/Kata node pool — is in
[`recipe/REPRODUCE.md`](recipe/REPRODUCE.md)):

```bash
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

**Throughput is dual — `per-node · per-cluster`.** Per-cluster figures here are a MEASURED cluster rate at 5 nodes; see the legend below for how to read the pair. (This is a different `node_count` than the one printed in each build's provenance banner below the table — that one describes the per-node fire's shape, not this per-cluster measurement.)

| Runtime | Activation Mode | Throughput @ <5s TTFE (sb/s — node · cluster) | Throughput @ <1s TTFE (sb/s — node · cluster) | TTFE p50 | TTFE p95 | Execution Success (Honesty Check) |
|---|---|---|---|---|---|---|
| gVisor | Warm-pool hit (Base image) | 13.217 /node · [pending (cluster-fire)](WORK_IN_PROGRESS.md#cluster-fire) | 13.217 /node · [pending (cluster-fire)](WORK_IN_PROGRESS.md#cluster-fire) | 0.5432s (count=5) † | 0.59s (count=5) † | 100% |
| gVisor | Unique-image cold (RL reality) | [pending](WORK_IN_PROGRESS.md#not-yet-measured) | 0 /node · 0 /cluster | 3.7694s (count=1) † | 3.7694s (count=1) † | 100% |
| gVisor | Resume-from-suspend | [pending](WORK_IN_PROGRESS.md#not-yet-measured) | 0 /node · 0 /cluster | 4.3387s (count=1) † | 4.3387s (count=1) † | 100% |
| Kata + microVM | Warm-pool hit (Base image) | 14.012 /node · 0.694 /cluster ⚠️ | 0 /node · 0 /cluster | 1.4399s (count=30) | 1.6844s (count=30) | 100% |
| Kata + microVM | Unique-image cold (RL reality) | [pending](WORK_IN_PROGRESS.md#not-yet-measured) | 0 /node · 0 /cluster | 3.1986s (count=30) | 3.5684s (count=30) | 100% |
| Kata + microVM | Resume-from-suspend | [N/A](WORK_IN_PROGRESS.md#na-by-construction) | [N/A](WORK_IN_PROGRESS.md#na-by-construction) | [N/A](WORK_IN_PROGRESS.md#na-by-construction) | [N/A](WORK_IN_PROGRESS.md#na-by-construction) | [N/A](WORK_IN_PROGRESS.md#na-by-construction) |

### Max Density (sandboxes per vCPU)

Density is per-**runtime** — constant across a runtime's activation-mode rows above, so it renders as a compact per-runtime sub-table here rather than a matrix column (a column would repeat each value down the mode rows and imply a mode-dependence that does not exist). Full methodology (per-vCPU denominator, saturation source) is in [DETAILS.md](DETAILS.md).

| Runtime | Max Density (sb/vCPU) |
|---|---|
| gVisor | [pending](WORK_IN_PROGRESS.md#not-yet-measured) |
| Kata + microVM | 1.26 |

**Reading the cells** — TTFE is Time-To-First-Instruction (wall-clock until your agent's first instruction returns, not merely pod-Ready). Read TTFE p50/p95 *down* a column, not across rows — activation-mode rows differ in sample size by orders of magnitude.

| Symbol | Meaning |
|---|---|
| `(count=N)` | Sample size for that cell — compare down a column, not across rows |
| `†` | Sub-N sample: a single observation, not a distribution |
| `⚠️` | Miss flag: sub-100% Execution Success, or a per-cluster rate below the sizing target |
| `pending` | No publishable figure yet — a genuinely not-yet-run cell; the labeled flavors (`cluster-fire`, etc.) are catalogued in [WORK_IN_PROGRESS.md](WORK_IN_PROGRESS.md) |
| plain `0` | DERIVED zero: implied by that row's TTFE p95 exceeding the column's bar, no throughput fire behind it — can flip to a real rate on a latency improvement alone |
| caveat-tagged floor-zero | MEASURED zero from an actual throughput fire — needs the cold-start floor itself to move (see the full key) |

Full cell-decoding key — TTFE basis, honest vs. measured zeros, the dual per-node · per-cluster throughput pair, the certification-floor `≥` figures, every `pending` flavor, and the published-with-caveat tag classes — is in [DETAILS.md](DETAILS.md#how-to-read-the-core-metrics-cells).

_Kata + microVM rows are measured in a separate run on the kata node pool: cluster_substrate=gke-kata · node_count=1 · generated-at=2026-08-31T15:22:54Z._

_build: cluster_substrate=gke-sandbox · controller_image=us-central1-docker.pkg.dev/k8s-staging-images/agent-sandbox/agent-sandbox-controller:v20260818-v0.5.5-34-gac864a6-main · controller_digest=sha256:2926730e6554f61119d78f99f05c5fd007fc1bd9d5a3ebdd20b3fac4ab1ba76c · crd_version=v1beta1 · suite_git_sha=6f6ad9fe4a325ac99282c70ac732e77fb8d247b6 · run_id=084134ae616c4a34a5fa0fd3b906233d · node_count=1_
_generated-at: 2026-09-04T15:45:28Z_

_**North Star** — warm-pool-hit TTFE p95 < 1s (the spec doc bar): gVisor 0.59s (count=5) † ✅ met (0.41s headroom); Kata + microVM 1.6844s (count=30) ❌ not met (0.6844s above the bar). An honest ❌ prints the measured gap to the bar (tagged `within sampling noise` when the miss sits inside the sample spread — it stays a ❌, the tag never flips a miss to a pass); `pending` = unmeasured (never a guess); † marks a p95 over fewer than N=30 samples._

_**Stretch bar** — warm-pool-hit TTFE p95 < 0.5s (an aspiration above the North Star, not the North Star itself; the step-up curve grades sustained creation-rate against it — see [DETAILS.md](DETAILS.md)): gVisor 0.59s (count=5) † ❌ not met (0.09s above the bar); Kata + microVM 1.6844s (count=30) ❌ not met (1.1844s above the bar)._

> ⚠️ **Machine class unknown:** this sandbox-family run did not stamp `machine_type`, so a rig change relative to the previously published run cannot be ruled out. Treat any delta as possibly machine-class-confounded until a matched-rig run is published.

### Known anomalies

| Anomaly | Status |
|---|---|
| Scenario FAIL | [✅ clear](DETAILS.md#scenario-fail) |
| Warm-slower-than-cold | [✅ clear](DETAILS.md#warm-slower-than-cold) |
| Warm-cold separation below gate | [✅ clear](DETAILS.md#warm-cold-separation-below-gate) |
| Cold-tier stall inflates separation ratio | [✅ clear](DETAILS.md#cold-tier-stall-inflates-separation-ratio) |
| Same-build separation-ratio variance | [⚠️ ACTIVE](DETAILS.md#same-build-separation-ratio-variance) |
| Single-fire separation verdict defensibility | [✅ clear](DETAILS.md#single-fire-separation-verdict-defensibility) |
| Mixed rig within this run | [✅ clear](DETAILS.md#mixed-rig-within-this-run) |
| Regime note | [ℹ️ standing note](DETAILS.md#regime-note) |
| Refresh cadence | [ℹ️ standing note](DETAILS.md#refresh-cadence) |

**What do these numbers mean for you?** Plain-English guidance — picking a runtime, sizing a
warm pool, what wait to budget, and why a `pending` cell is unmeasured-not-bad — is in the
deep-dive appendix, [DETAILS.md](DETAILS.md#what-this-means-for-you).

### What wait should I budget?

Find the row closest to **your** load; the p50 is the wait to plan around. The **Scope** column is load-bearing: the first three rows are the **full** start→first-result wait (TTFE), directly comparable to one another; the last row is only the **pool hand-off** sub-phase (it stops the moment you hold a ready sandbox, before your code runs), so do **not** rank its number against the full-TTFE rows above it. Every number is measured, not modelled — an unmeasured row reads `pending`, never a guess.

| Your load pattern | Wait to budget (p50) | Scope |
|---|---|---|
| Steady trickle — warm pool keeps up with demand | ~0.5s | full start → first result |
| Bursty — pool oversubscribed (more claims than ready pool) | [pending](WORK_IN_PROGRESS.md#not-yet-measured) | full start → first result |
| Hundreds of sandboxes requested at once (1:1 pool) | [pending](WORK_IN_PROGRESS.md#not-yet-measured) | full start → first result |
| Sustained high-rate churn | [pending](WORK_IN_PROGRESS.md#not-yet-measured) | pool hand-off only (before exec) |

## Throughput — build-over-build

The headline COUNT — sandboxes ready in <1s in a single 1.0s burst against one warm
pool — tracked across distinct controller builds (oldest first). **Δ** is the change in
COUNT vs the prior build; the first build is the baseline. Drive this COUNT up.

| Build (controller digest) | Date | Sandboxes ready <1s | Δ | Density /vCPU (this build) | n | Outcome |
|---|---|---|---|---|---|---|
| `sha256:6edaf7b6b22d…` | 2026-06-28 | 9 | — | 0.45 | 10 | PASS |
| `sha256:4e36a61c6bdc…` | 2026-07-25 | 2 | -7 † | 0.0416667 | 10 | FAIL |
| `sha256:41c8c6bcabe4…` | 2026-08-16 | 10 | +8 † | 0.208333 | 10 | PASS |
| `sha256:1c884bd5d9d7…` | 2026-08-25 | 4 | -6 † | 0.03125 | 10 | FAIL |
| `sha256:cd69601f8fd3…` | 2026-08-25 | 4 | +0 † | 0.0625 | 10 | FAIL |
| `sha256:7606cc6ac7fa…` | 2026-08-26 | 5 | +1 † | 0.078125 | 10 | FAIL |
| `sha256:7a5240ff698b…` | 2026-08-26 | 1 | -4 † | 0.015625 | 10 | FAIL |
| `sha256:822bea792403…` | 2026-08-28 | 4 | +3 † | 0.0625 | 10 | FAIL |
| `sha256:f73072367103…` | 2026-08-31 | 9 | +5 † | 0.140625 | 10 | PASS |
| `sha256:671770254af1…` | 2026-09-02 | 5 | -4 † | 0.078125 | 10 | FAIL |
| `sha256:cc11470d4fa7…` | 2026-09-03 | 10 | +5 † | 0.15625 | 10 | PASS |
| `sha256:7a24a6db095f…` | 2026-09-03 | 8 | -2 † | 0.0625 | 10 | PASS |
| `sha256:7a24a6db095f…` | 2026-09-03 | 2 | -6 † | 0.015625 | 10 | FAIL |

_† Δ spans a build whose burst sampled fewer than N=30 claims — too few to rank build-over-build; the swing may be sampling noise, not a real move._

_A FAIL Outcome means that build's burst did not clear the delivery-ratio SLA — the COUNT is still the real, measured number, not fabricated or estimated._

_Density /vCPU (this build) is this single burst's own per-vCPU figure — a different, typically much smaller measurement than the Max Density table above (peak across scenarios), not a build-over-build regression._

_ℹ️ **Root-caused regression (hb#737):** the trailing FAIL streak above is a confirmed upstream regression, not an open mystery — a zero-confound bisect isolated **agent-sandbox#1454** (confirmed, compounding) on top of a partial prior contribution from **agent-sandbox#1078**, both in the create/bind reconcile hot path. No fix is ours to ship; this cell stays honestly RED until the upstream fixes land._

_⚠️ The most recent fire (2026-09-04) measured a headline COUNT of 10 but is not reflected above — its build is not yet accrued into the trend. The trend is not advanced past 2026-09-03; fix the fire's provenance capture and this caveat clears on the next accrual._

```
Throughput — build-over-build (sandboxes ready <1s)

2026-06-28 ██████████████████ 9
2026-07-25 ████ 2 (FAIL)
2026-08-16 ████████████████████ 10
2026-08-25 ████████ 4 (FAIL)
2026-08-25 ████████ 4 (FAIL)
2026-08-26 ██████████ 5 (FAIL)
2026-08-26 ██ 1 (FAIL)
2026-08-28 ████████ 4 (FAIL)
2026-08-31 ██████████████████ 9
2026-09-02 ██████████ 5 (FAIL)
2026-09-03 ████████████████████ 10
2026-09-03 ████████████████ 8
2026-09-03 ████ 2 (FAIL)
```

## Which storage class should you pick?

Per-class results from a controlled storage-config fire (fixed workload). An unmeasured class renders `pending`; the per-row sample count is the trust gate.

| Storage class | Samples (n) | Payload p50 | Pass rate |
|---|---|---|---|
| Ephemeral (node-local) | 3 | 64 MiB | 100% |
| Persistent disk | 3 | 64 MiB | 100% |
| Snapshot-restored | 3 | 64.51 MiB † | 100% |

_Measured 2026-07-07 — storage-config axis (point-in-time); each class carried an identical controlled write, W = 64 MiB._

† Payload p50 is not measured the same way across classes, so the column is not a like-for-like byte comparison. Ephemeral and persistent-disk write a fixed pattern to a mount and count the **allocated writable-fs blocks** (`du`). The snapshot class instead counts the **checkpoint-artifact object bytes**: a snapshot captures process memory, not the writable-fs layer, so its identical W lives in an incompressible in-memory buffer (a zero-filled buffer would be dropped by the checkpointer's zero-page optimization and never appear in the artifact), and the artifact bytes include checkpoint overhead beyond W. Same controlled W per class; different bytes counted.

## How is TTFE measured?

```mermaid
flowchart LR
    A["Claim<br/>(request a sandbox)"] --> B["Bind<br/>(pool assigns + provisions)"]
    B --> C["Exec-probe<br/>(websocket + first-instruction round-trip)"]
    C --> D["Webhook TTFE stamp<br/>(executor reports the true first-instruction timestamp)"]
    D --> E["Closed-schema render<br/>(results/latest.json &rarr; this page)"]
```

TTFE (Time To First Execution) is the **webhook-stamped** timestamp in step D — not pod-Ready,
which only proves the sandbox exists, not that it ran your code (see **Burst Create — TTFE
Corroboration** in [DETAILS.md](DETAILS.md) for the two claims side by side). Step E is the same
closed-schema render every table on this page goes through: a result only reaches `results/latest.json`
by clearing schema validation, so nothing between the probe and the page can silently drop or
reshape a number.

## Reproduce it

Every number above comes from a *vanilla* GKE cluster you can provision yourself — no private
tuning. The full recipe — runnable steps (commands, pinned installs, dispatch-only CI), the exact
cluster shape it needs, and the one sizing rule worth copying (**size the warm pool to your active
concurrency**) — lives in [`recipe/REPRODUCE.md`](recipe/REPRODUCE.md).
When a drained-regime fire is on the page, the Warm-Pool decomposition in [DETAILS.md](DETAILS.md)
names the scaling term directly.

**Honesty:** a row marked `pending` is not-yet-measured — never a provisional number dressed as a
result. The **sub-1s @ 300/s warm headline is not yet published**; today's honest figures are the
measured cells above (Core Metrics + **Concurrent Burst**) plus the **Warm-Pool Acquisition**
decomposition in [DETAILS.md](DETAILS.md). TRUE-TTFE (webhook-stamped first-instruction) is now
the live measurement basis for the warm-pool figures above ([asbx#761](https://github.com/kubernetes-sigs/agent-sandbox/pull/761)
merged) — cells publish on `thpt_slo_basis: "true_ttfe"`, not a proxy.
