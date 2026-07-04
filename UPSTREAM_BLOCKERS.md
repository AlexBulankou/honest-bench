# Upstream blockers — executive view

One screen: **what's blocked in product terms, where to engage upstream, and a ready-to-paste comment.** You never need to open a PR — staged patches are parked on the maintainer's forks as reference material, offered only if a maintainer asks. Each row is self-contained: forward it to the team as-is. Engineering depth (full diagnoses, file-ready bodies, patch provenance, dup-search evidence): [**UPSTREAM_BLOCKERS_DETAIL.md**](UPSTREAM_BLOCKERS_DETAIL.md).

_Freshness: refreshed on every blocker state change; link states hand-verified at each refresh. Last verified: 2026-07-04 (all 7 filed anchors re-checked live — agent-sandbox #873/#893/#751/#761/#952 and substrate #50/#353 all still open, no state change). Machine-readable mirror: [`render/upstream_links.json`](render/upstream_links.json) (feeds the per-cell links on the rendered pages) — update it in the same PR as any blocker-state change here._

---

## Engage now — upvote + one comment each (4 items)

| # | What it blocks (why it matters) | Where to engage | Your move |
|---|---|---|---|
| 1 | **Resume-from-suspend numbers are unpublishable.** A resumed sandbox permanently reports `Suspended`, so resume reliability (previously measured ~93%) can't be validated — the entire gVisor × Resume row of the metrics table is on hold. | [agent-sandbox#873](https://github.com/kubernetes-sigs/agent-sandbox/issues/873) *(issue, open)* → fix [PR #893](https://github.com/kubernetes-sigs/agent-sandbox/pull/893) *(in review; implements the accepted KEP-119)* | 👍 both · paste **comment 1** on #873 |
| 2 | **True time-to-first-execution is unmeasurable.** The end-to-end latency metric records nothing on any stock install, so the North-Star "&lt;1s TTFE" can only be published as an upper bound. | [agent-sandbox#751](https://github.com/kubernetes-sigs/agent-sandbox/issues/751) *(issue, open)* → example fix [PR #761](https://github.com/kubernetes-sigs/agent-sandbox/pull/761) *(open)* | 👍 both · paste **comment 2** on #751 |
| 3 | **"Ready" can mean an empty sandbox.** 5–7% of snapshot restores serve a blank pod marked Ready — restore-success can't be asserted for any snapshot-backed number. | [agent-sandbox#952](https://github.com/kubernetes-sigs/agent-sandbox/issues/952) *(design issue, open)* | 👍 · paste **comment 3** on #952 |
| 4 | **Actors wedge permanently mid-suspend** (hit 3× in 2 days; each needed manual recovery) — suspend/resume reliability on substrate. | [substrate#50](https://github.com/agent-substrate/substrate/issues/50) *(issue, open)* → fix [PR #353](https://github.com/agent-substrate/substrate/pull/353) *(in review)* | 👍 both · paste **comment 4** on PR #353 |

**Comment 1** — on [agent-sandbox#873](https://github.com/kubernetes-sigs/agent-sandbox/issues/873):

> We're publishing a GKE agent-sandbox benchmark and this is the one defect holding the entire resume-from-suspend row: a resumed sandbox keeps reporting Suspended, so resume reliability can't be validated. The direction in #893 looks right to us — is there a review ETA? (We have a regression test staged if useful.)

**Comment 2** — on [agent-sandbox#751](https://github.com/kubernetes-sigs/agent-sandbox/issues/751):

> Without a production writer for this annotation, end-to-end claim startup latency is unmeasurable on any stock deployment — our public benchmark can only publish an upper bound for time-to-first-execution. Support for landing this; we also have an in-tree webhook draft if maintainers would prefer that over example manifests.

**Comment 3** — on [agent-sandbox#952](https://github.com/kubernetes-sigs/agent-sandbox/issues/952):

> Data point: across ~800 restores per path we see 5–7% of snapshot-backed sandboxes served blank-but-Ready. Until readiness gates on "restore verified", no restore-backed metric is trustworthy. Happy to share the measurement detail.

**Comment 4** — on [substrate PR #353](https://github.com/agent-substrate/substrate/pull/353) (and 👍 [#50](https://github.com/agent-substrate/substrate/issues/50)):

> We've hit this exact wedge 3× in 2 days; every recovery required manually recycling the worker. The terminal CRASHED classification here fixes the retry-forever loop — worth also releasing the worker assignment on terminal failure so the actor re-runs on a fresh worker (that's the manual recovery that works today).

---

## Not yet filed upstream (4 items) — forward the blurb, or say `file them`

No upstream issue exists for these yet (dup-search verified). Full file-ready bodies are in the [detail page](UPSTREAM_BLOCKERS_DETAIL.md); either forward a blurb to the team or say **`file them`** and the exact text gets staged for whoever files.

| # | One-line forward blurb | Detail |
|---|---|---|
| 5 | agent-sandbox warm-pool claims stall under concurrency — an internal retry rides exponential backoff, inflating tail latency (p95 ~9s at N=300 burst). A tested fix is staged as a reference patch. | [§S4](UPSTREAM_BLOCKERS_DETAIL.md#s4-adoption-completion-cache-race) |
| 6 | substrate golden-actor bring-up treats a transient state as fatal — templates wedge forever, and the activation-latency metric has zero clean records ever. | [§U1](UPSTREAM_BLOCKERS_DETAIL.md#u1-snapshot-type-unspecified) |
| 7 | substrate's new DurableDir feature broke snapshotting for simple actors (none of their volumes are durable) — the e2e suite has been red since Jun 27. | [§U2](UPSTREAM_BLOCKERS_DETAIL.md#u2-data-snapshot-zero-ddv) |
| 8 | substrate bricks its actor-listing API after any proto field rename on a long-lived cluster — a forward-compat gap whose second trigger is already in review. | [§U3](UPSTREAM_BLOCKERS_DETAIL.md#u3-ateredis-strict-protojson) |

---

## Reference patches (nobody needs to open these)

Parked on the maintainer's forks; offer only if a maintainer asks ("we have a tested patch staged"):

- [`agent-sandbox:adoption-completion-bounded-requeue`](https://github.com/AlexBulankou/agent-sandbox/tree/adoption-completion-bounded-requeue) — item 5, tested, ready.
- [`agent-sandbox:resume-clears-suspended-condition`](https://github.com/AlexBulankou/agent-sandbox/tree/resume-clears-suspended-condition) — item 1, historical (superseded by the KEP-119 direction; kept for reference).
