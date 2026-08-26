# Reproduce these benchmarks

Don't take our numbers on faith. Every published cell comes from the commands
below, run against the **same upstream controller** the table measures — no
private fork, no internal image, no hand-entered figures. If you follow this
recipe and get a different number, that is a bug worth an issue.

The recommended path is a GKE cluster with a gVisor (or Kata) node pool — the
isolation rows this page exists to show only produce real numbers on a sandbox
runtime. Point your `kubectl` context at such a cluster **and** set
`BENCH_CLUSTER_SUBSTRATE` to match (`gke-sandbox` for gVisor, `gke-kata` for Kata,
`gke` for a plain node pool) before step 2 — the harness does not auto-detect the
substrate from the cluster yet, so that env var is what selects which cells run and
stamps the build banner (see the headline-cell section below for the full
`gke-sandbox` invocation). On a cluster without a sandbox runtime, isolation
scenarios report `pending (requires-gvisor-runtime)` rather than a false FAIL.

## Prerequisites

- `python3` (3.9+). The renderer (`render/`) is standard-library only, but the
  harness scenario bodies talk to the cluster via the official Kubernetes client,
  so install the one runtime dep first: `pip install -r harness/requirements.txt`
  (declared minimal — `kubernetes` and nothing else; the `harness.run` loop and
  the pure `results_schema`/`scenario_map` seam are themselves stdlib-only).
- `kubectl`, and a GKE cluster your context points at. For the isolation rows, the
  cluster needs a gVisor (`--enable-sandbox=type=gvisor`) or Kata node pool — see
  the headline-cell section below.

## Steps

```bash
# install the one harness runtime dep first (applies to every path below; the
# renderer is stdlib-only but the scenario bodies need the Kubernetes client).
# Skip this and `python3 -m harness.run` crashes with ModuleNotFoundError.
pip install -r harness/requirements.txt

# 0. point kubectl at a GKE cluster with a gVisor/Kata node pool, and set
#    BENCH_CLUSTER_SUBSTRATE to match (see the headline-cell section below).

# 1. install the OSS controller, built from upstream main, onto your context.
#    (this bare invocation APPLIES to the cluster; pass --dry-run to fetch +
#    render the manifests without writing to it.)
bash recipe/install-controller-from-main.sh

# 2. run the portable suite — writes sandbox/results/latest.json
python3 -m harness.run

# 3. regenerate the README table from the results you just produced
python3 -m render.generate

# 4. compare: your regenerated README table vs the published one
git diff README.md
```

If your run matched ours, step 4 shows only provenance differences (your run id,
digest, timestamp, and `cluster_substrate`) — the measured cells line up.

## Reproducible (pinned) installs

`install-controller-from-main.sh` floats to the newest upstream `main` build by
default. For a byte-for-byte reproducible install, pin both the manifest ref and
the image tag to the values in the build banner of the row you're checking:

```bash
UPSTREAM_REF=<git-sha-from-banner> IMAGE_TAG=<vYYYYMMDD-...-main-from-banner> \
  bash recipe/install-controller-from-main.sh
```

## Validating a staged fork fix (fork-build path)

If you're testing a fix that has no upstream-published image yet — staged on a fork
branch — `BUILD_MODE=source` ko-builds the controller from that fork tree instead of
pulling a prebuilt image, and pushes to a registry you own:

```bash
UPSTREAM_REPO=<fork-owner>/agent-sandbox UPSTREAM_REF=<fork-branch> \
  BUILD_MODE=source KO_DOCKER_REPO=us-central1-docker.pkg.dev/<project>/<repo> \
  bash recipe/install-controller-from-main.sh
```

Requires the `ko` binary (https://ko.build) on `PATH` and push access to
`KO_DOCKER_REPO`. There is no `--dry-run` for this mode — a `ko build` always builds
and pushes; use the default `BUILD_MODE=prebuilt --dry-run` to preview manifest
substitution only.

## The headline cell: burst-create throughput

`burst_create` answers the headline question — **how many sandboxes go Ready in
under one second, in a burst, against a warm pool**. It provisions a
`SandboxWarmPool` of K slots, waits for it to fill, fires K `SandboxClaim`s, and
measures each claim's time-to-first-instruction (Ready+bound) from its own create
time. It publishes two numbers:

- **`sandboxes_ready_under_1s`** — the count of claims that cleared the sub-1s bar
  (a count, not a rate: each TTFI is per-claim, so a serial create loop never eats
  a later claim's budget).
- **`density_per_vcpu`** — that count divided by the cluster's total capacity vCPU,
  so the magnitude is comparable across runner hardware.

It runs on any substrate (`requires_substrate=None`), so even a plain `gke` node
pool measures it for real. Tunables (all env, all optional):

```bash
BURST_CREATE_POOL_REPLICAS=10      # warm slots == claims fired (raise on a big cluster)
BURST_CREATE_TTFI_CEILING_S=1.0    # the sub-1s bar
BURST_CREATE_MIN_QUALIFIED_RATIO=0.8  # PASS iff count >= ceil(K * ratio)
BURST_CREATE_RUNTIME_CLASS=gvisor  # pin the burst to a RuntimeClass (see below)
BURST_CREATE_WARMUP_TIMEOUT_S=240  # pool-fill budget; raise for a large gVisor burst
BURST_CREATE_BIND_TIMEOUT_S=180    # per-claim bind ceiling (cold tail measured, not cut)
```

To reproduce a **gVisor-isolated** throughput number, point `kubectl` at a cluster
whose nodes have the gVisor runtime (e.g. a GKE node pool created with
`--enable-sandbox=type=gvisor`, which installs a `gvisor` RuntimeClass), then set
**both** env vars:

```bash
BENCH_CLUSTER_SUBSTRATE=gke-sandbox BURST_CREATE_RUNTIME_CLASS=gvisor \
  python3 -m harness.run
```

`BENCH_CLUSTER_SUBSTRATE` is load-bearing and not optional here. The harness does
**not** auto-detect the substrate from your cluster yet (that is a planned
integration seam); today the build banner's `cluster_substrate` is exactly what
you set in this env var. So if you point `kubectl` at a gke-sandbox cluster but
leave `BENCH_CLUSTER_SUBSTRATE` unset, it falls back to a local-cluster default and
a real gVisor run is published mislabelled — under-claiming, but still a wrong
banner.

Setting `BENCH_CLUSTER_SUBSTRATE=gke-sandbox` also arms a consistency guard: the
burst-create cell refuses to publish a `gke-sandbox`-labelled result unless
`BURST_CREATE_RUNTIME_CLASS=gvisor` is set (it crash-fails fast with that
message), and it reads back the live backing pods to confirm each one actually
landed on the `gvisor` RuntimeClass — so a `gvisor` RuntimeClass that silently
fell back to runc is caught rather than shipped as a gVisor headline.

With both set, every pod in the pool is pinned to that RuntimeClass, the read-back
confirms it, and the build banner's `cluster_substrate=gke-sandbox` records the
substrate — so a plain runc number is never mistaken for a `gke-sandbox` one,
and a `gke-sandbox` number is provably gVisor, not just labelled.

## The scale headline: concurrent-N warm vs cold TTFE

`burst_create` above answers the sub-1s **count** question against a warm pool.
The scale headline answers a different one — **at a fixed concurrency N, what is
the create-to-first-instruction (TTFE) latency distribution, warm vs cold** — and
publishes the per-node-throughput and TTFE-percentile columns of the Core Metrics
matrix. The driver is `scripts/fire-concurrent-n.sh`:

```bash
# the caller exports KUBECONFIG first — the script hardcodes no cluster identity.
# the TTFE probe execs into each backing pod, so this must be a kubeconfig with
# pods/exec RBAC (an admin kubeconfig), pointed at a gVisor cluster.
export KUBECONFIG=/path/to/admin.kubeconfig

bash scripts/fire-concurrent-n.sh warm 300   # warm pool of 300, 300 claims
bash scripts/fire-concurrent-n.sh cold 300   # pool replicas 0 — every claim cold-provisions
bash scripts/fire-concurrent-n.sh warm 500
bash scripts/fire-concurrent-n.sh cold 500
```

`warm` sizes the warm pool to N so every claim is served from a ready slot; `cold`
sets the pool to zero so every claim overflows to a cold provision — that is the
warm-vs-cold contrast, measured on the same path at the same N rather than across
two different scenarios. The fire arms the TTFE exec probe (`BENCH_TTFE_EXEC=1`)
and pins the burst to the `gvisor` RuntimeClass, with the same read-back guard the
burst-create section describes (a `gvisor` class that silently fell back to runc is
caught, not shipped).

Each run prints the matrix-cell numbers — `thpt_under_5s_per_node`,
`thpt_under_1s_per_node`, `ttfe_p50_ms`, `ttfe_p95_ms`, `exec_success_rate`, and
`n` — and tees the full result to a timestamped `concurrent-n-<mode>-<N>-<ts>.json`.
Those map one-to-one onto the matrix columns (Throughput @&lt;5s TTFE, Throughput
@&lt;1s TTFE, TTFE p50, TTFE p95, Samples N, Execution Success), so you can read a
published cell straight off your own fire's stdout. The driver writes that
timestamped file, **not** `sandbox/results/latest.json` — it is a measurement
probe, deliberately decoupled from the published page; folding a fresh fire into
`latest.json` (then regenerating with step 3) is a separate, reviewed step so a
single ad-hoc fire never silently rewrites a headline.

Tunables (env, optional): `BENCH_NODE_COUNT` (per-node throughput denominator,
default 20), `BENCH_NAMESPACE` (default `default`), `FIRE_TIMEOUT_S` (warmup +
per-claim-bind ceiling, default 900s — raise for a large cold burst).

## Cluster shape for the warm-pool scale headline

The fire commands above presume a cluster that can actually *serve* a few hundred
warm claims at sub-second TTFE. The fire is the easy part; the **cluster shape**
is what makes the headline reproducible rather than autoscaler-bound. None of the
shape below is a private tuning — it is the vanilla architecture any GKE user can
provision, and it is exactly what the published headline's build banner records.
The numbers a given shape *achieves* are filled in from a real fire (see the
placeholder block at the end of this section); the shape itself is the recipe.

**Control plane.** A **regional GKE cluster on >= 1.31** — regional so the control
plane is not a single-zone SPOF under a burst of N simultaneous claim writes, and
>= 1.31 because that is the floor where the sandbox CRDs (`v1beta1`) and the
gVisor `RuntimeClass` admission path are both stable.

**Node pool.** A gVisor-enabled pool —
`--enable-sandbox=type=gvisor`, which installs the `gvisor` `RuntimeClass` the
burst pins to — on a **16-vCPU machine type** (the build banner records exactly
which one, so a reproduced number is comparable to ours). Size the pool's
autoscaling **maximum** to the node count the headline needs *before* the fire —
a warm-pool burst that has to wait on node autoscaling is measuring the
autoscaler, not the sandbox path, and that cold tail is exactly what the warm pool
exists to remove. The per-node sandbox density (sandboxes per node-allocatable
sandbox-schedulable vCPU) is published in the Core Metrics matrix; divide the
target concurrency by that density to size the pool's node ceiling.

The gate on that node ceiling is **per-machine-family CPU quota, not the generic
CPU quota** — `node_ceiling × 16` vCPU must fit under the quota for the *specific*
machine family you pick (e.g. the `N2_CPUS` / `E2_CPUS` regional quota for an
`n2-standard-16` / `e2-standard-16` pool), and raising generic CPU does not lift a
per-family cap. So pick a family whose adjustable quota covers the headline's node
count: a few-hundred-node scale-headline pool needs a family quota in the
thousands of vCPU, while the smaller published cells fit comfortably inside a
modest one. If your preferred family's quota is capped below the node ceiling,
either request an increase on that family or switch to a 16-vCPU family that
already has the headroom — the architecture is identical, only the family-quota
math changes.

**Pod networking.** A **pod CIDR wide enough that `node_count × pods-per-node`
does not exhaust the range** — a **`/16` cluster pod range** comfortably addresses
a several-hundred-node pool at the default per-node pod allocation. A pod range
sliced too thin caps the node count *below* the headline's needed fan-out, so the
burst silently tops out on IP exhaustion rather than on the sandbox path — another
confound the shape removes up front.

**Warm-pool sizing.** Size the `SandboxWarmPool` so a ready slot is waiting when
each claim arrives: **replicas ≈ active-concurrency × 0.75, replenished at the
claim rate.** The 0.75 factor keeps a steady-state buffer of ready slots without
over-provisioning idle capacity; "replenished at the claim rate" means the pool
controller refills a drained slot as fast as claims consume them, so a sustained
arrival rate is served warm rather than draining the pool into the cold-overflow
path partway through the burst. (Set the pool to the same N the fire uses for a
fully-warm headline; set it to zero for the cold-contrast leg, exactly as the
`fire-concurrent-n.sh warm|cold N` driver above does.)

**Zero-cold-start image pre-pull.** Run an **image pre-pull `DaemonSet`**
(`prepull-daemonset.yaml` in this directory — substitute your build banner's base
image) that pulls the sandbox **base image** onto every node before the burst. It
matters most on the **cold leg and under warm-pool overflow**: a *fully* pre-filled
warm pool already resident-izes the base image during warm-up, so a claim served
straight from a ready slot pays no image pull on the critical path even without the
DaemonSet (that pull is folded into warm-up, timed separately from claim→ready).
The DaemonSet earns its place the moment a burst drains the pool faster than it
replenishes and spills onto a node that never pulled the image — there the pull
lands squarely on claim→ready and inflates the tail. Pre-pulling every node ahead
of the fire removes that confound, so the warm number stays a *warm* number under
overflow and the cold-contrast leg measures the activation path, not a containerd
cache miss. (If your base image is **distroless**, the DaemonSet's `/bin/true`
no-op will not exist, so the pod shows not-Running even though the pull succeeded —
the kubelet fetches the image before running the command either way. Verify the
pull by the node image cache, e.g. `crictl images` on the node, not by pod status.
See the caveat comment in `prepull-daemonset.yaml`.)

```
# placeholder — measured numbers filled post-fire.
# achieved sustained throughput : TODO sb/s            (target: 300 sb/s)
# warm-pool claim->ready p95    : TODO ms              (target: < 500 ms; doc ideal)
# TTFE p95 (executed first-instr): TODO s              (target: < 1 s)
# node_count / pool replicas    : TODO / TODO
# build banner (substrate / image digest / suite sha)  : TODO
```

Everything above is architecture-shape only; the achieved figures come from a real
gke-sandbox fire and land in the build banner + the Core Metrics / Concurrent
Burst tables on the published page, never hand-entered here.

## The matrix cluster cell: per-mode SLO-gated cluster rate

The Core Metrics matrix throughput cells carry two halves: a per-node rate and a
**per-cluster rate at X nodes**. The cluster half is an **SLO-gated rate** — the
sustained creation rate (sandboxes/sec across the whole cluster) at which the
mode's **p95 TTFE stays inside the bar** (5s or 1s) — *not* saturation
throughput. A saturation fire (push until the knee) answers "where does it
fall over"; the matrix cell answers "how fast can you go while still meeting
the latency bar". One number cannot serve both questions, so the cluster half
is produced by a **per-mode step-up sweep**, derived per bar independently.

**The fire (spend-gated — a real multi-node cluster at the published X).** For
each activation-mode row you want to fill, run a step-up sweep *through that
mode's activation path* (warm-pool hit, cold create, resume-from-suspend) at
increasing offered rates, holding each rung long enough to measure a stable
`ready_per_s` and `ttfe_p95_ms`. The CL2 step-up driver described in the
headline section produces exactly this shape; the sweep record is the nested
`BENCH_STEPUP_RESULT` form:

```json
{
  "params": {"cluster_nodes": 40},
  "true_ttfe_webhook_stamped_claims": 200,
  "pareto": [
    {"offered_rate_per_s": 10,  "ready_per_s": 9.8,  "ttfe_p95_ms": 850.0},
    {"offered_rate_per_s": 30,  "ready_per_s": 28.4, "ttfe_p95_ms": 3200.0},
    {"offered_rate_per_s": 100, "ready_per_s": 41.0, "ttfe_p95_ms": 12610.3}
  ]
}
```

The top-level `pareto`'s `ttfe_p95_ms` is the **true-TTFE basis** — an
ms-precision `t0` taken from the `agents.x-k8s.io/webhook-first-observed-at`
annotation the asbx#761 mutating webhook stamps on each SandboxClaim CREATE
(deploy recipe: `recipe/deploy-ttfe-webhook.sh` + `recipe/ttfe-webhook/SOURCE.md`).
Without the webhook, `t0` falls back to the second-truncated `creationTimestamp`,
which is a literal **upper bound** — see the literal-basis legs below. The
`true_ttfe_webhook_stamped_claims` field is the load-bearing corroboration for
that basis and is described next.

As with the concurrent-N driver, the caller exports `KUBECONFIG` first and the
sweep writes a timestamped record file, **not** `sandbox/results/latest.json` —
the fire is a measurement probe, decoupled from the published page.

**The derivation (zero additional fire).** Point the harness at each mode's
sweep record via `BENCH_SLO_SWEEP_<SCENARIO>` and re-run the suite step:

```bash
# one env var per matrix row; set only the ones you swept.
BENCH_SLO_SWEEP_WARMPOOL_COLD_START=warm-sweep-40n.json \
BENCH_SLO_SWEEP_NATIVE_DIGEST_COLD=cold-sweep-40n.json \
BENCH_SLO_SWEEP_SUSPEND_RESUME=resume-sweep-40n.json \
  python3 -m harness.run --product sandbox
```

For each bar (5s, 1s) independently, the derivation (`harness/slo_rate.py`)
takes the **max measured `ready_per_s` among rungs whose own `ttfe_p95_ms` is
within the bar** and merges the coupled triple —
`thpt_under_5s_per_cluster` + `thpt_under_1s_per_cluster` +
`thpt_cluster_node_count` — into that scenario's metrics. The honesty spine:

- **Measured rate only** — `ready_per_s`, never the offered rate, is credited.
- **Per-bar independent fill** — a sweep whose lowest rung clears 5s but not 1s
  publishes the 5s half and leaves the 1s half `pending`; the two bars'
  boundary rates generally differ, which is exactly why a single boundary fire
  cannot honestly fill both (and why this sweep derivation is the preferred
  producer over the direct-emit leg).
- **No compliant rung ⇒ pending, never 0** — a sweep that never probed below
  the boundary proves nothing about it.
- **`cluster_nodes` required** — the render pins "at X nodes" from
  `thpt_cluster_node_count`; a record without it pends the whole cell rather
  than printing a rate the page cannot caption.
- The env vars are read under `--product sandbox` **and** `--product
  sandbox-kata` (both own a matrix cluster half) and are default-off: with none
  set, the run's emit is byte-identical to today's.
- **One product per shell** — the `BENCH_SLO_SWEEP_*` namespace is shared
  across products and a sweep record carries no runtime discriminator, so a
  stale gVisor env var left set across a following `--product sandbox-kata` run
  (or vice-versa) would silently cross-merge one runtime's rate into the
  other's cell. Derive each runtime in a fresh shell, or `unset` the vars
  between products.

A previously-published triple is carried forward across later env-less runs
(same do-not-auto-decay posture as the scale-proof block), so one reviewed
sweep fire per mode keeps the cell filled until a fresh fire supersedes it.

### The true-TTFE basis read-back guard (hb#5396)

The true-TTFE `pareto` above is only honest if the webhook was actually live in
the fired window. The harness enforces this with a **fail-closed read-back
guard**, so a stale scrape or a partial deploy can never silently publish a
literal upper bound dressed as the true ms-precision basis:

- **The producer stamps a count.** The step-up sweep record MUST carry a
  top-level `true_ttfe_webhook_stamped_claims` — the number of claims in the
  fired window carrying the webhook annotation, read straight off the headline
  metric `agent_sandbox_claim_startup_latency_ms`'s `_count` (that histogram is
  `.Observe()`d only for annotation-bearing claims, so its count *is* the
  webhook-stamped population).
- **The guard requires corroboration.** The true-TTFE basis is trusted only
  when that count is a finite, non-bool integer `>= 1`. A populated true-TTFE
  `pareto` with the count **absent or `< 1`** is treated as a stale-scrape /
  partial-deploy artifact: the true-TTFE basis is **discarded** and the harness
  falls through to the literal bases (`literal_ttfe_upper_bound+*`).
- **Why (encode-then-merge / transition-guard doctrine).** The stamp and the
  pareto it corroborates are written by the same fire; publishing the pareto
  without the corroborating count is exactly the silent trust-downgrade the
  guard exists to refuse — it fails loud (drops to the honest literal upper
  bound) rather than rendering an unverified number as fresh.

Concretely: when box-3's CL2 sweep fires against the webhook-deployed cluster,
its record carries both the true-TTFE `pareto` **and**
`true_ttfe_webhook_stamped_claims` (the `_count` at scrape time). Omit the
count and the whole true-TTFE cell honestly reverts to the literal bases.

**Producer of both fields (offline PHASE-B assembler).** The fire captures a
cumulative Prometheus scrape at each rung boundary (`metrics-step-*.txt`); the
canonical producer `harness.ttfe_stamp.build_true_ttfe_stamp` turns those
captured scrape TEXTs into exactly the `{pareto,
true_ttfe_webhook_stamped_claims}` stamp above — per-rung true-TTFE p95 from the
HEADLINE_METRIC increment (`prom_ttfe.ttfe_by_launch_type_delta`) paired with the
driver's offered/ready rates, and the webhook-stamped count off the same metric's
`_count`. It is pure/offline (operates on captured text, no cluster/clock), so it
runs at derive time and is fully unit-tested; `rungs_from_boundary_scrapes` pairs
the N+1 boundary snapshots into the N rung windows. A rung whose selected
`launch_type` did not measure is dropped from the pareto (never a fake 0), and the
count is `None` (measured=False) until the webhook is live on the fired cluster —
so the same code is dead-by-construction pre-deploy and auto-populates once the
webhook stamps its first claim.

## Refreshing published cells: the canonical per-product command

A **wholesale refresh** (re-running the suite to update `latest.json`) must carry
the full env block below, not a bare `python3 -m harness.run`. The reason is a
knob-scoping property that has bitten three times: several published cells'
**sample size is set by env knobs, not by a default** — so a refresh fire that
omits a knob silently *downgrades* a previously-graduated cell rather than
reproducing it. Concretely:

- The warm cell's published `n` is the number of **warm pool members**
  (`WARMPOOL_COLD_START_POOL_REPLICAS`, default 5) — the TTFE emit covers exactly
  the pool, **not** the claim count. A refresh without the pool/claim knobs resets
  a graduated warm row from n=30 back to n=5 and re-introduces the † marker.
- The cold cell's `n` is `NATIVE_DIGEST_COLD_SAMPLES` (default 1) — same failure
  shape.
- The `suspend_resume` cell's `n` is `SUSPEND_RESUME_CYCLE_COUNT` (default 1) —
  same failure shape (hb#592: a refresh omitting it reds on `check_n_regression`
  because the committed cell is n=30, graduated in #517).
- Without `BENCH_TTFE_EXEC=1` the TTFE columns are not armed at all.

The graduation shape published on the page is **pool=30 / claims=40** (a 1.33:1
claims:pool ratio, so the pool is fully consumed with overflow exercised) and,
for the gVisor product, **200 cold samples** (bumped from 30 by PR#736,
2026-08-25 — the Kata product's cold-sample shape is unchanged at 30, see its
copy-paste block below). Copy-paste, per product, in a **fresh shell each** (the
`BENCH_SLO_SWEEP_*` cross-product caveat above applies to refreshes too):

```bash
# gVisor product — full suite, all knobs at the published graduation shape.
BENCH_CLUSTER_SUBSTRATE=gke-sandbox \
BURST_CREATE_RUNTIME_CLASS=gvisor \
WARMPOOL_COLD_START_RUNTIME_CLASS=gvisor \
WARMPOOL_COLD_START_POOL_REPLICAS=30 \
WARMPOOL_COLD_START_CLAIM_COUNT=40 \
WARMPOOL_COLD_START_WARMUP_TIMEOUT_S=600 \
WARMPOOL_COLD_START_BIND_TIMEOUT_S=600 \
SUSPEND_RESUME_RUNTIME_CLASS=gvisor \
SUSPEND_RESUME_CYCLE_COUNT=30 \
NATIVE_DIGEST_COLD_RUNTIME_CLASS=gvisor \
NATIVE_DIGEST_COLD_SAMPLES=200 \
BENCH_TTFE_EXEC=1 \
  python3 -m harness.run
```

```bash
# Kata product — same shape, kata RuntimeClass, scoped to the kata scenarios.
BENCH_CLUSTER_SUBSTRATE=gke-kata \
WARMPOOL_COLD_START_RUNTIME_CLASS=kata-clh \
WARMPOOL_COLD_START_POOL_REPLICAS=30 \
WARMPOOL_COLD_START_CLAIM_COUNT=40 \
WARMPOOL_COLD_START_WARMUP_TIMEOUT_S=600 \
WARMPOOL_COLD_START_BIND_TIMEOUT_S=600 \
NATIVE_DIGEST_COLD_RUNTIME_CLASS=kata-clh \
NATIVE_DIGEST_COLD_SAMPLES=30 \
BENCH_TTFE_EXEC=1 \
  python3 -m harness.run --product sandbox-kata
```

If a refresh renders a lower `n` (or a reappearing †) on a cell that was
previously graduated, treat it as a mis-fired refresh — re-run with the block
above — rather than committing the downgrade.

## Reproduce in CI (no laptop required)

The gVisor and Kata GKE headlines each run as a **manual Cloud Build trigger**
(the fleet rule is Cloud Build ONLY — no GitHub Actions), so you can reproduce a
headline without a local cluster and read the build log to see every command:

- **gke-sandbox (gVisor) path** — [`cloudbuild-refresh-gke-sandbox.yaml`](../cloudbuild-refresh-gke-sandbox.yaml).
  Provisions a **fresh, ephemeral** GKE node pool with `--sandbox type=gvisor`,
  installs the same upstream-main controller as the steps above, fires the
  burst-create headline under `runtimeClassName=gvisor` (the read-back guard
  above confirms every backing pod landed on gVisor), renders, runs the
  public-safety fail-closed check, opens a PR with the result, and tears the
  cluster down in a bash `EXIT` trap (the CB-native equivalent of a workflow's
  `if: always()` teardown). A fresh node pool means an empty containerd cache — a
  genuine cold pull, not a warmed-runner artifact. Fire it:

  ```bash
  gcloud builds triggers run hb-refresh-gke-sandbox --project=<PROJECT> \
    --substitutions=_POOL_REPLICAS=10,_MACHINE_TYPE=n2-standard-16,_REGION=us-central1
  ```

  The published graduation shape's other two N-bearing knobs — `NATIVE_DIGEST_COLD_SAMPLES`
  and `SUSPEND_RESUME_CYCLE_COUNT` (see "Refreshing published cells" above) — are
  also fire-time substitutions on this trigger (`_NATIVE_DIGEST_COLD_SAMPLES`
  default `"200"`, `_SUSPEND_RESUME_CYCLE_COUNT` default `"30"` — the two
  defaults DIVERGED 2026-08-25 (PR#736 bumped the gVisor cold-sample committed
  shape 30->200; resume did not move), each byte-identical to its own committed
  shape), alongside `_NUM_NODES`/`_MAX_NODES` (node-pool sizing — gVisor
  pool pods are CPU/mem-tiny, so the real ceiling at high pool/claim counts is
  GKE's default max-pods-per-node, not resources) and `_BUILD_TIMEOUT` (default
  `"3600s"`, caps the whole create→measure→render→PR→teardown lifecycle). Example
  raising warm pool to 200 with matching node headroom:

  ```bash
  gcloud builds triggers run hb-refresh-gke-sandbox --project=<PROJECT> \
    --substitutions=_WARMPOOL_POOL_REPLICAS=200,_WARMPOOL_CLAIM_COUNT=266,_NUM_NODES=5,_MAX_NODES=7
  ```

  Cold samples are cheap (~4s each) so a large `_NATIVE_DIGEST_COLD_SAMPLES` fire
  costs only minutes of extra wall-clock. Resume cycles are not: `suspend_resume.py`'s
  loop is strictly sequential with no checkpointing (each cycle chains off the
  prior cycle's resumed Pod, results only finalize after the full loop), so a large
  `_SUSPEND_RESUME_CYCLE_COUNT` fire needs a correspondingly larger `_BUILD_TIMEOUT`
  and accepts that a mid-run failure loses every accumulated sample.
- **gke-kata (Kata + microVM) path** — [`cloudbuild-refresh-gke-kata.yaml`](../cloudbuild-refresh-gke-kata.yaml).
  Same shape, `kata-clh` RuntimeClass, scoped to the Kata scenarios. Fire it:

  ```bash
  gcloud builds triggers run hb-refresh-gke-kata --project=<PROJECT>
  ```

**Manual is the spend-arm.** Each GKE refresh binds to a trigger with **no
branch/PR/schedule wiring**, so a real GKE cluster is never spun unattended: it
fires only on an explicit `gcloud builds triggers run`, and it opens a PR for a
human to review before merge — a cron (spend) or auto-merged headline shift can
never happen.

**Reproducible by anyone with a GCP project.** Auth is a dedicated
least-privilege **Cloud Build service account** passed on the trigger — no
Workload-Identity-Federation secrets, no long-lived keys. The three former GitHub
repo secrets (`GCP_WIF_PROVIDER` / `GCP_SERVICE_ACCOUNT` / `GCP_PROJECT`) are
retired. Point [`scripts/setup-cloud-build-triggers.sh`](../scripts/setup-cloud-build-triggers.sh)
at your own project to create the triggers; the service account it uses needs, on
the target project, `roles/container.admin`, `roles/iam.serviceAccountUser`,
`roles/compute.viewer`, `roles/logging.logWriter`, and Secret Accessor on the
refresh GitHub-token secret. `roles/logging.logWriter` is required because the
refresh builds run with `options.logging: CLOUD_LOGGING_ONLY` (a custom-SA build
cannot use the default GCS log bucket) and the first build step is a functional
`gcloud logging write` preflight probe that fail-fasts before the ~25-min cluster
spend if the SA cannot write to Cloud Logging — omit it and every fire dies at
step 0 with an empty log.

## Reading the output

- **Measured cells** are real latencies / outcomes from your run.
- **`pending (<reason>)`** means the scenario could not be measured on your
  substrate (e.g. `requires-gvisor-runtime` on a non-sandbox cluster) or is gated
  on a tracked upstream gap (`upstream-blocked`). It is never a silent pass.
- **Goal columns render `(non-public)`** by construction — the internal targets
  file does not ship in this repo, so the renderer has nothing to fill them with.
- A malformed or unexpected field in `results/latest.json` is **dropped** by the
  closed-schema renderer, not displayed — so the page can only ever show the
  declared vocabulary.

## The other product

The `sandbox` harness above is the first portable suite, and it is currently the
**only** product on the public page: the top-level `README.md` is sandbox-only by
deliberate choice (alex, 2026-06-28) — substrate is an internal data-engine, not a
co-equal published table. The `substrate/` product's harness + schema-validated
`substrate/results/latest.json` stay in-tree, and the cross-contract guard still
validates the substrate emitter↔render contract against them, but regenerating the
README (step 3) will **not** produce a substrate table — re-adding the substrate
entry to `render/generate.py`'s `_PRODUCTS` tuple is the single switch that would.
Until substrate publishes, the sandbox steps 1-2 above are what you run end-to-end.
