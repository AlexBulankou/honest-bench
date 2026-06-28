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
