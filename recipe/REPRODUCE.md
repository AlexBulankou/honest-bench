# Reproduce these benchmarks

Don't take our numbers on faith. Every published cell comes from the commands
below, run against the **same upstream controller** the table measures — no
private fork, no internal image, no hand-entered figures. If you follow this
recipe and get a different number, that is a bug worth an issue.

The default path is a local `kind` cluster: free, and reproducible on a
GitHub-hosted runner. A `kind` cluster has no gVisor runtime, so isolation
scenarios that need one report `pending (requires-gvisor-runtime)` — that is the
honest result on this substrate, and the build banner labels every number
`cluster_substrate=kind`. To reproduce the `gke` / `gke-sandbox` rows, point your
`kubectl` context at such a cluster **and** set `BENCH_CLUSTER_SUBSTRATE` to match
before step 2 — the harness does not auto-detect the substrate from the cluster
yet, so that env var is what selects which cells run and stamps the build banner
(see the headline-cell section below for the full `gke-sandbox` invocation).

## Prerequisites

- `python3` (3.9+). The renderer (`render/`) is standard-library only, but the
  harness scenario bodies talk to the cluster via the official Kubernetes client,
  so install the one runtime dep first: `pip install -r harness/requirements.txt`
  (declared minimal — `kubernetes` and nothing else; the `harness.run` loop and
  the pure `results_schema`/`scenario_map` seam are themselves stdlib-only).
- `kubectl`, and a cluster your context points at. For the portable path:
  [`kind`](https://kind.sigs.k8s.io/) + a container runtime (Docker/Podman).

## Steps

```bash
# install the one harness runtime dep first (applies to every path below; the
# renderer is stdlib-only but the scenario bodies need the Kubernetes client).
# Skip this and `python3 -m harness.run` crashes with ModuleNotFoundError.
pip install -r harness/requirements.txt

# 0. (portable path) create a local cluster
kind create cluster

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

It runs on any substrate (`requires_substrate=None`), so the portable `kind` path
above measures it for real. Tunables (all env, all optional):

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
leave `BENCH_CLUSTER_SUBSTRATE` unset, it defaults to `kind` and a real gVisor
run is published mislabelled `cluster_substrate=kind` — under-claiming, but still
a wrong banner.

Setting `BENCH_CLUSTER_SUBSTRATE=gke-sandbox` also arms a consistency guard: the
burst-create cell refuses to publish a `gke-sandbox`-labelled result unless
`BURST_CREATE_RUNTIME_CLASS=gvisor` is set (it crash-fails fast with that
message), and it reads back the live backing pods to confirm each one actually
landed on the `gvisor` RuntimeClass — so a `gvisor` RuntimeClass that silently
fell back to runc is caught rather than shipped as a gVisor headline.

With both set, every pod in the pool is pinned to that RuntimeClass, the read-back
confirms it, and the build banner's `cluster_substrate=gke-sandbox` records the
substrate — so a `kind` (runc) number is never mistaken for a `gke-sandbox` one,
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
burst pins to — on **`e2-standard-16`** nodes (16 vCPU each; the same machine type
the build banner names, so a reproduced number is comparable to ours). Size the
pool's autoscaling **maximum** to the node count the headline needs *before* the
fire — a warm-pool burst that has to wait on node autoscaling is measuring the
autoscaler, not the sandbox path, and that cold tail is exactly what the warm pool
exists to remove. The per-node sandbox density (sandboxes per node-allocatable
sandbox-schedulable vCPU) is published in the Core Metrics matrix; divide the
target concurrency by that density to size the pool's node ceiling.

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

**Zero-cold-start image pre-pull.** Run an **image pre-pull `DaemonSet`** that
pulls the sandbox **base image** onto every node before the burst. A warm-pool
slot still pays a one-time image pull the first time the base image lands on a
fresh node; pre-pulling on every node ahead of the fire removes that pull from the
critical path, so the warm leg measures the activation path and not a containerd
cache miss. This is what makes the warm number a *warm* number — provisioning off
one node-cacheable shared base image, never a unique image per claim.

```
# placeholder — measured numbers filled post-fire (a4s1's lane).
# achieved sustained throughput : TODO sb/s            (target: 300 sb/s)
# warm-pool claim->ready p95    : TODO ms              (target: < 500 ms; doc ideal)
# TTFE p95 (executed first-instr): TODO s              (target: < 1 s)
# node_count / pool replicas    : TODO / TODO
# build banner (substrate / image digest / suite sha)  : TODO
```

Everything above is architecture-shape only; the achieved figures come from a real
gke-sandbox fire and land in the build banner + the Core Metrics / Concurrent
Burst tables on the published page, never hand-entered here.

## Reproduce in CI (no laptop required)

The same two paths above also run as **dispatch-only** GitHub Actions, so you can
reproduce a headline from a fork without a local cluster — and read the run log to
see every command:

- **kind path** — [`.github/workflows/refresh.yml`](../.github/workflows/refresh.yml).
  `workflow_dispatch` runs the whole portable suite (steps 1-3 above) on a
  GitHub-hosted runner and opens a PR with the regenerated result. No secrets, no
  cloud account — the kind cluster is created on the runner itself.
- **gke-sandbox path** — [`.github/workflows/refresh-gke-sandbox.yml`](../.github/workflows/refresh-gke-sandbox.yml).
  `workflow_dispatch` provisions a **fresh, ephemeral** GKE node pool with
  `--sandbox type=gvisor`, installs the same upstream-main controller, fires the
  burst-create headline under `runtimeClassName=gvisor` (the read-back guard
  above confirms every backing pod landed on gVisor), opens a PR with the result,
  and tears the cluster down in an `always()` step. A fresh node pool means an
  empty containerd cache — a genuine cold pull, not a warmed-runner artifact.

Both schedules are **disabled by design**: an unattended kind run must never
downgrade the live gVisor headline, and a real GKE cluster must never spin on a
cron (spend) or auto-merge a headline shift. Every refresh is manual and opens a
PR for a human to review before merge.

The gke-sandbox workflow is reproducible by **anyone with a GCP project** — point
it at your own by adding three repo secrets, then dispatch it:

| Secret | Value |
|---|---|
| `GCP_WIF_PROVIDER` | a Workload Identity Federation provider resource name (keyless OIDC auth — no long-lived key stored) |
| `GCP_SERVICE_ACCOUNT` | the service account the provider impersonates (needs `container.admin` to create/delete the cluster) |
| `GCP_PROJECT` | your GCP project id |

Without those three secrets the workflow is **inert** — a dispatch fails fast at
the auth step, so it can never spin a cluster (or incur spend) unattended.

## Reading the output

- **Measured cells** are real latencies / outcomes from your run.
- **`pending (<reason>)`** means the scenario could not be measured on your
  substrate (e.g. `requires-gvisor-runtime` on `kind`) or is gated on a tracked
  upstream gap (`upstream-blocked`). It is never a silent pass.
- **Goal columns render `(non-public)`** by construction — the internal targets
  file does not ship in this repo, so the renderer has nothing to fill them with.
- A malformed or unexpected field in `results/latest.json` is **dropped** by the
  closed-schema renderer, not displayed — so the page can only ever show the
  declared vocabulary.

## The other product

The `sandbox` harness above is the first portable suite. The `substrate/` product
currently publishes its results through the same closed-schema renderer (step 3
renders both products into the single top-level `README.md`); its portable harness
lands next. Until then, regenerating the README (step 3) reproduces the substrate
table from its committed `substrate/results/latest.json`, and the sandbox steps
1-2 are what you run end-to-end.
