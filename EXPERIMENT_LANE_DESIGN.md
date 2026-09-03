# Experiment lane for off-default-env fires (design note)

Status: **proposed** — no code yet. Written in response to a fleet-lead's ask-3 following
up on the sized-pool diagnostic fire.
This note describes the shape; implementation is a follow-up once the design is agreed.

## The gap this closes

A fire that sets an off-default env knob (e.g. `WARMPOOL_POOL_REPLICAS=45` for a
pool-capacity diagnostic sweep, distinct from the committed matrix's default pool sizing)
currently has exactly two outcomes when it reaches `check_cell_downgrade` (hb#206):

1. **Refused.** The guard sees the off-default row's shape diverge from the committed
   cell and treats it as a downgrade candidate. Before hb#801 this silently
   discarded the freshly computed numbers; after hb#801 the numbers survive in the build
   log, but the fire still produces **no durable, queryable artifact** — recovering them
   again means re-reading a specific build's log, or re-firing.
2. **Force-published via `BENCH_ALLOW_CELL_DOWNGRADE=1`.** This makes the guard publish,
   but it publishes **into the same cell the committed matrix's default-config fires
   write to** — so the public page's "what does default config achieve" number gets
   silently replaced by a non-default-env measurement until the next default-config fire
   overwrites it back. That is exactly the trust-surface degrade the #4420 doctrine and
   hb#206's own guard exist to prevent, just relocated one level up: the *guard* stops
   the accidental case, but the *deliberate* bypass has no home of its own to land in
   that isn't the committed matrix.

Neither outcome gives an off-default fire a legitimate place to live. The fix is a third
lane: off-default fires get their own store, keyed by `measured_with`, that is never read
by the render path and never compared against by the downgrade guard as if it were a
candidate replacement for a canonical cell.

## Design

### 1. Partition key: `measured_with` vs. the documented default set

Every scenario already MAY emit `measured_with` (hb#723, `harness/results_schema.py`) —
a dict of the env-var knobs that gated which `sla_metrics` it produced this fire. Define,
per scenario, a **documented default set** — the `measured_with` shape (possibly empty)
that the committed matrix's cell for that scenario is measured under. A fresh row is:

- **canonical** — `measured_with` is absent, or exactly matches the scenario's documented
  default set (same keys, same values) — eligible to compare against / replace the
  committed matrix cell, same as today.
- **experiment** — `measured_with` contains a key outside the documented default set, or
  a documented key with a different value (e.g. `WARMPOOL_POOL_REPLICAS=45` when the
  default cell was measured at the standing pool size) — routed to the experiment lane
  instead of compared against the canonical cell at all.

This mirrors `check_cell_downgrade`'s own `mw_suffix` diagnostic today (which already
prints `measured_with` differences in its downgrade lines) — the new logic promotes that
same comparison from "annotate the downgrade line" to "decide which lane the row belongs
in."

### 2. Storage: `sandbox/results/experiments.jsonl`

A new append-only JSONL store, sibling to `sandbox/results/history.jsonl` (same
append-on-every-fire shape, same `run_id`/`controller_digest`/`suite_git_sha`/
`generated_at`/`cluster_substrate` provenance fields) plus the full `measured_with` dict
and the scenario's `sla_metrics`/`outcome`/`n` as measured. Never mutated in place, never
read by `render/generate.py`. This is a pure accrual store — an agent (or a future report)
can query it by `measured_with` fingerprint, but it carries zero authority over what the
public page shows.

### 3. `check_cell_downgrade`'s role narrows, doesn't grow

The guard's job stays exactly what hb#206 built it for: refuse a canonical-row downgrade.
The new logic is a **pre-filter** ahead of it, not a change to the guard itself — a fresh
row whose `measured_with` doesn't match the scenario's documented default is diverted to
`experiments.jsonl` before it ever reaches `check_cell_downgrade`'s comparison. It literally
cannot flag as a downgrade of the canonical cell, because it was never a candidate to
replace that cell in the first place. `BENCH_ALLOW_CELL_DOWNGRADE=1` keeps its existing,
narrower meaning: "deliberately downgrade the *canonical* cell" — it stops being the
mechanism an off-default diagnostic fire has to reach for just to get its numbers recorded
anywhere.

### 4. Rendering is unaffected

`README.md`/`DETAILS.md` keep rendering exclusively from `latest.json`'s canonical rows,
unchanged. A reader of the public page never sees experiment-lane data mixed into the
default-config numbers — that boundary is the whole point of this design.

### 5. Adoption gap to flag (not solved here)

Today only a few scenarios (`burst_create`, `warmpool_cold_start`, `native_digest_cold`)
emit `measured_with` at all, and none of them currently tag pool-sizing knobs
(`WARMPOOL_POOL_REPLICAS`/`WARMPOOL_CLAIM_COUNT`/`NUM_NODES`) into it — the
diagnostic fire's own `measured_with` would need those keys added at the scenario level
before this partition has anything to key on for that specific class of sweep. That's
scenario-level follow-up work, not part of this note's scope.

### Open questions (for whoever picks up the implementation)

- Where does the "documented default set" per scenario live — a constant next to each
  scenario's `SLA_METRICS` declaration, or a single allow-list table in
  `harness/results_schema.py`? Leaning toward the latter (one place to audit, mirrors how
  `BADGE_CONSTRUCTION_ENUM` and friends are already centralized there).
- Retention/pruning for `experiments.jsonl` — unbounded append is fine at current fire
  volume; revisit if it grows unwieldy.
- No public-page surface for experiment data is proposed here. If a future need arises
  (e.g. a capacity-sweep appendix), that's a separate ask against a stable store this
  design already provides.

## Non-goals

- This does not change anything about how the committed matrix is measured, gated, or
  published today for **canonical** fires — zero behavior change for the common case.
  It doesn't touch the `resume_probe_ceiling_ms`-style fail-open provenance fields either.
- This is not a general experiment-tracking platform — it is the minimal store needed so
  an off-default diagnostic fire's numbers land somewhere durable and queryable instead of
  forcing a choice between "silently lost" and "silently overwrites the committed matrix."
