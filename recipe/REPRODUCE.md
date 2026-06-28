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
`kubectl` context at such a cluster before step 2.

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
`--enable-sandbox=type=gvisor`, which installs a `gvisor` RuntimeClass), then:

```bash
BURST_CREATE_RUNTIME_CLASS=gvisor python3 -m harness.run
```

Every pod in the pool is then pinned to that RuntimeClass, so the published count
is real gVisor throughput; the build banner's `cluster_substrate` records the
substrate so a `kind` (runc) number is never mistaken for a `gke-sandbox` one.

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
