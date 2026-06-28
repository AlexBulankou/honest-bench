# Portable sandbox benchmark harness

A stranger can `git clone` this repo and reproduce every cell of the sandbox
benchmark table on a vanilla cluster — a local `kind` cluster, or their own GKE /
GKE-Sandbox. The table is **honest by construction**: every number is
machine-rendered from `sandbox/results/latest.json`, which this harness writes.
There are no hand-typed numbers.

## Run it

```bash
# 1. bring up any cluster (kind is the zero-cost default)
kind create cluster
# 2. install the OSS controller from upstream main (see the recipe/ at repo root)
# 3. run the suite (writes sandbox/results/latest.json)
python3 -m sandbox.harness.run
```

The harness reads whatever `KUBECONFIG` points at — it never pins a cluster name.
Set `BENCH_CLUSTER_SUBSTRATE` to `kind` (default), `gke`, or `gke-sandbox` so the
results banner records the substrate a number was measured on.

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

## Two portability caveats (non-uniform across products)

1. **gVisor does not run on vanilla kind.** Isolation cells need `runsc` on the
   node, which kind lacks, so on a `kind` run they render
   `pending (requires-gvisor-runtime)` instead of a false FAIL. Full isolation
   badges require the `gke-sandbox` substrate.
2. **kind perf ≠ GKE perf.** kind is single-node; its cold-start / restore numbers
   are not GKE numbers. `results/latest.json` carries `cluster_substrate` and the
   render banner stamps it, so a kind number is never mis-read as a GKE result.

## Files

- `results_schema.py` — closed-schema emitter (the safety guard); pure, offline-testable.
- `scenario_map.py` — MVP cell → scenario module map, with substrate gating.
- `run.py` — the in-process suite loop; writes `results/latest.json`.
- `test_results_schema.py` — offline tests (`python3 -m sandbox.harness.test_results_schema`).
- `scenarios/` — the stripped scenario bodies (ported from the in-cluster runner).
