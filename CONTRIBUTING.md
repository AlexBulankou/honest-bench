# Contributing

Thanks for your interest in improving these benchmarks. This page covers the
principles the repository is built on and the mechanics of adding a scenario or
running the suite.

## The honesty contract

These benchmarks are **honest by construction**. Two rules make that true, and
every contribution must preserve them:

1. **Only measured results are published.** Every number on the public page
   comes from a real end-to-end run against a real controller build. There are
   no hand-entered figures and no aspirational targets in the published table —
   goal columns render `(non-public)` because the internal targets file never
   ships here. If a capability is not yet measurable in this environment, it
   renders as `pending` with an explicit reason, never as a pass.

2. **The render is a closed allow-list.** The renderer emits *only* the
   field names, scenario names, metric names, and provenance keys declared in
   `render/schema.py`. Anything not in that schema is dropped (and the drop is
   counted). This is the primary guard that keeps the public page to exactly the
   declared vocabulary — so adding a new field to the page is a deliberate,
   reviewed edit to the schema, not a side effect of an emitter change.

If you find a result on the page you cannot reproduce with the recipe in the
README, that is a bug — please open an issue.

## Repository layout

```
README.md                 generated benchmark page (do not hand-edit)
recipe/                    portable controller-from-upstream installer
render/                    closed-schema renderer + tests
  schema.py                the allow-list (scenarios, metrics, provenance)
  render.py                render a product's results into a table
  generate.py              aggregate all products into README.md
sandbox/  substrate/       per-product results/ + harness/
  results/latest.json      latest measured run (machine-written)
  harness/                 scenario runner + result schema
```

## Running the suite

The renderer and its tests are dependency-free — standard-library Python 3 only:

```
python3 render/test_render.py          # closed-schema render unit tests
python3 render/test_cross_contract.py  # emitter <-> render vocabulary guard
python3 -m render.generate             # regenerate README.md from results/
```

`generate.py` rewrites `README.md` in place; a clean run leaves it
byte-identical when the inputs are unchanged.

The harness has its own tests, each run as a module from the repo root:

```
python3 -m harness.test_results_schema   # closed emit-schema coercion guard
python3 -m harness.test_run_merge        # additive seed/measured merge rules
python3 -m harness.test_metrics          # pure TTFE metrics-derivation core
```

To reproduce a full run, follow the recipe in the README: install the controller
from upstream `main` with `recipe/install-controller-from-main.sh`, then run the
harness against your cluster. On a vanilla `kind` cluster, scenarios that require
a sandboxed runtime render `pending` rather than a false failure.

## Adding a scenario

1. **Declare it in the schema first.** Add the scenario's internal name and its
   public display label to `SCENARIO_LABELS` in `render/schema.py`. If it emits a
   latency metric, add the metric key and label to `METRIC_LABELS`. Until a name
   is in the schema, the renderer will drop it.
2. **Write the harness scenario.** Its `run()` returns
   `(outcome, excerpt, sla_metrics)`. `outcome` must be one of the values in
   `OUTCOMES`; a `pending` outcome carries a reason from `PENDING_REASONS`.
   Emit only metrics declared in the schema.
3. **Render honestly.** If the scenario cannot run in a given environment, return
   `pending` with the matching reason — do not return a pass. If it exercises a
   known, tracked upstream gap, return `pending (upstream-blocked)` so the page
   reflects "not yet shipped," not a regression. Reserve `FAIL` for a genuine,
   unexpected failure of a capability that is expected to work.
4. **Add a cross-contract case.** Extend `render/test_cross_contract.py` so the
   new scenario renders as its public label with zero rows dropped.

## Pull requests

- Keep the published `README.md` a generated artifact — regenerate it with
  `python3 -m render.generate` rather than editing it by hand.
- Run the test commands above before opening a PR.
- Each change is reviewed before merge.

## Code of conduct

Be respectful and constructive. Assume good faith, keep discussion focused on the
work, and help newcomers reproduce results.
