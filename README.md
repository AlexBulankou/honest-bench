# Honest benchmarks — GKE agent sandbox

This table is **machine-rendered, never hand-entered**. A measured cell traces to a
schema-validated field of a real harness run; anything the schema does not declare is
dropped before it can reach this page. We publish only what we actually measured on the
cluster named in each build banner — a `kind` run is labelled `kind`, so a local number
is never presented as a production SLA.

A cell we have not yet measured renders `pending (<reason>)` — never a guess and never a
false PASS/FAIL. The **sandbox** table below is measured against a live GKE cluster — the
`cluster_substrate=gke-sandbox` build banner names the cluster, and the burst-create
headline was measured with the gVisor (runsc) runtime. The two NetworkPolicy cells are
qualified `(control-plane)` because a PASS there asserts the policy was admitted and
correctly targeted, not that data-plane traffic was enforced.

Every measured number here is a **reproducible floor, not a ceiling.** It is what a
*vanilla* OSS build — the upstream controller from `main`, default runtime, no tuning,
the cluster shape named in the build banner — delivers today. A bigger pool, denser nodes,
or a tuned runtime should *beat* it; the value on the page is the honest lower bound you can
reproduce from the recipe below and then improve on. We publish the floor we can stand
behind, not the best number we could cherry-pick.

- **Measured (N)** is the value we observed, with the sample size.
- **Committed / Target / North-Star** render `(non-public)` here by construction — internal
  goal numbers never ship to this repo.

Reproduce any row yourself — then beat it. The suite is honest by construction:

```
bash recipe/install-controller-from-main.sh   # OSS controller from upstream main
python3 -m harness.run                        # run the portable suite (cluster=kind)
python3 -m render.generate                    # regenerate this table
bash scripts/check-public-safety.sh           # fail-closed public-safety scan
```

## sandbox

| Scenario | Measured (N) | Committed | Target | North-Star |
|---|---|---|---|---|
| Burst create throughput | PASS · Density /vCPU 0.45, Sandboxes ready <1s 9 (n=10) | (non-public) | (non-public) | (non-public) |
| Warm-pool activation (hit) | PASS · Activation (ms) 553.24 (n=5) | (non-public) | (non-public) | (non-public) |
| Unique-image cold start | PASS · Cold start (ms) 1670.59 (cold-provision) (n=1) | (non-public) | (non-public) | (non-public) |
| Resume from suspend | pending (upstream-blocked) (n=0) | (non-public) | (non-public) | (non-public) |
| gVisor isolation canary | PASS (n=0) | (non-public) | (non-public) | (non-public) |
| Cross-tenant network isolation | PASS (control-plane) (n=0) | (non-public) | (non-public) | (non-public) |
| Default-deny egress | PASS (control-plane) (n=0) | (non-public) | (non-public) | (non-public) |

_build: cluster_substrate=gke-sandbox · controller_image=us-central1-docker.pkg.dev/k8s-staging-images/agent-sandbox/agent-sandbox-controller:latest-main · controller_digest=sha256:6edaf7b6b22d9dfaf6ab077cd1c6517acf5fc6cf96b1ad58fe83bcfd477977ec · crd_version=v1beta1 · suite_git_sha=c88d857 · run_id=a0e4f0ffae12440a826ac40a277f21f3 · node_count=3_
_generated-at: 2026-06-28T14:42:40Z_

## Throughput — build-over-build

The headline COUNT — sandboxes ready in <1s in a single 1.0s burst against one warm
pool — tracked across distinct controller builds (oldest first). **Δ** is the change in
COUNT vs the prior build; the first build is the baseline. Drive this COUNT up.

| Build (controller digest) | Date | Sandboxes ready <1s | Δ | Density /vCPU | n |
|---|---|---|---|---|---|
| `sha256:6edaf7b6b22d…` | 2026-06-28 | 9 | — | 0.45 | 10 |
