# Portable benchmark harness

A stranger can `git clone` this repo and reproduce every cell of a product's
benchmark table on their own GKE / GKE-Sandbox cluster. The table is **honest by
construction**: every number is
machine-rendered from `<product>/results/latest.json`, which this harness writes.
There are no hand-typed numbers.

`--product` selects the scenario suite (default: `sandbox`); its results land in
`<product>/results/latest.json`. An unregistered product fails loud rather than
overwriting a hand-seeded results file.

## Run it

```bash
# 1. bring up a GKE cluster with a gVisor/Kata node pool (see the recipe/ at repo root)
# 2. install the OSS controller from upstream main (see the recipe/ at repo root)
# 3. run the suite (writes sandbox/results/latest.json)
python3 -m harness.run                 # default product: sandbox
python3 -m harness.run --product substrate
```

The harness reads whatever `KUBECONFIG` points at — it never pins a cluster name.
Set `BENCH_CLUSTER_SUBSTRATE` to `gke` or `gke-sandbox` (`gke-kata` for Kata) so the
results banner records the substrate a number was measured on. Set it explicitly: an
unset value falls back to a local-cluster label and would mislabel a GKE run.

## Design: subtraction, not rewrite

The scenario logic is sound; only its dependencies were internal. The harness
keeps the scenario bodies and strips four internal bindings:

| Internal binding | Portable replacement |
|---|---|
| observation-DB write | aggregate per-scenario dicts in memory → `results/latest.json`; no DB |
| pinned cluster context | whatever `KUBECONFIG` the runner finds; substrate read live |
| CronJob + per-scenario Job fan-out | one in-process loop (`run.py`) over the MVP cells |
| internal registry image | the OSS controller built/pulled from upstream `main` per recipe |

Each scenario keeps its `run(name) -> (outcome, excerpt, sla_metrics)` contract.
`sla_metrics` is the machine-readable matrix source the README render consumes.
`excerpt` is read for PASS/FAIL classification **only** and is never written to
`results.json`.

## Honest by construction — the closed-schema guard

`results_schema.build_results` is the single writer of `results/latest.json` and is
**allow-list by construction**: it copies a fixed set of known field-names and
types and drops everything else. A scenario that accidentally surfaces an internal
string cannot reach the public table:

- only the closed scenario fields (`name`, `outcome`, `pending_reason`, `n`,
  `sla_metrics`) and provenance fields are emitted; any other key is dropped;
- `outcome` is restricted to `pass | fail | pending`; an unknown value fails closed;
- `pending_reason` is a fixed enum, never free text;
- `sla_metrics` values must be finite numbers and keys must be plain metric names
  (`[a-z0-9_-]`, matching render's canonical `activation_ms`-style keys), so a
  string excerpt or a `host:port` / path / DSN cannot pass.

This is the primary public-safety guard; the repo-level `check-public-safety.sh`
scanner is the backstop.

## Isolation requires a sandbox-enabled GKE node pool

Isolation cells (the gVisor/Kata TTFE rows) need `runsc`/Kata on the node, so they
only produce real numbers on a `gke-sandbox` (or `gke-kata`) node pool. On a cluster
without the sandbox runtime they render `pending (requires-gvisor-runtime)` instead of
a false FAIL. `results/latest.json` carries `cluster_substrate` and the render banner
stamps it, so every number is attributed to the substrate it was measured on.

## Files

- `results_schema.py` — closed-schema emitter (the safety guard); pure, offline-testable.
- `scenario_map.py` — per-product cell → scenario module map, with substrate gating.
- `run.py` — the in-process suite loop; writes `<product>/results/latest.json`.
- `test_results_schema.py` — offline tests (`python3 -m harness.test_results_schema`).
- `scenarios/` — the stripped scenario bodies (ported from the in-cluster runner).
