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

- `python3` (3.9+) — the harness and renderer are standard-library only, no
  `pip install`.
- `kubectl`, and a cluster your context points at. For the portable path:
  [`kind`](https://kind.sigs.k8s.io/) + a container runtime (Docker/Podman).

## Steps

```bash
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
