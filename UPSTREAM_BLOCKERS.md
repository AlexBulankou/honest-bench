# Upstream blockers — working page for direct approver engagement

**Model (alex, 2026-07-07):** alex engages upstream approvers directly; this page is the single interface — kept concise and current (a4z1 curates). **No new filings in the agent-sandbox / substrate repos from our side**; reference patches stay parked on alex's forks and are offered only if a maintainer asks. Engineering depth per item: [**UPSTREAM_BLOCKERS_DETAIL.md**](UPSTREAM_BLOCKERS_DETAIL.md). Machine-readable mirror: [`render/upstream_links.json`](render/upstream_links.json) — update it in the same commit as any blocker-state change here.

_Last verified live: 2026-07-07 ~17:05Z (all 9 anchors open; #940 👍 landed; comments 3–5 not yet posted)._

## Live asks (5)

| # | What it blocks | Where | Ask now |
|---|---|---|---|
| 1 | **gVisor resume row (5 cells):** a resumed sandbox reports `Suspended` forever, so resume reliability (~93% previously measured) can't be validated. | [asbx#873](https://github.com/kubernetes-sigs/agent-sandbox/issues/873) → fix [PR #893](https://github.com/kubernetes-sigs/agent-sandbox/pull/893) | **#893 is stalled**: author inactive since 05-29 (~5.6w), unit presubmit failing. Ask an approver to **adopt or supersede**; our regression test is on offer (07-03 comment on #873, no response). |
| 2 | **True TTFE unmeasurable** → the <1s North Star publishes only as an upper bound; 2 cold cells stuck at `no-compliant-rung`. | [asbx#751](https://github.com/kubernetes-sigs/agent-sandbox/issues/751) → fix [PR #761](https://github.com/kubernetes-sigs/agent-sandbox/pull/761) | #761 approved 05-08, **held since 05-20; lgtm auto-dropped on the 06-05 rebase**; only red check = autogen presubmit. Ask: **hold-cancel + re-lgtm** (igooch authored it — can lift his own hold, cannot self-lgtm; barney-s or aditya-shantanu re-lgtm). |
| 3 | **5–7% of snapshot restores are blank-but-Ready** → no restore-backed metric is trustworthy. | [asbx#952](https://github.com/kubernetes-sigs/agent-sandbox/issues/952) | 👍 + paste **comment 3** below. |
| 4 | **Actors wedge permanently mid-suspend** (3× in 2 days, manual recovery each time). | [substrate#50](https://github.com/agent-substrate/substrate/issues/50) → fix [PR #353](https://github.com/agent-substrate/substrate/pull/353) (converging, mergeable-clean) | **Before #353 merges:** paste **comment 4** on the PR — worker-release-on-terminal-failure is still an in-code TODO in its `crash.go`. 👍 both. |
| 5 | **Controller startup-histogram overcounts (~1.7–2× at fire scale)** → warm per-cluster throughput cells trust-gated on both runtimes. | [asbx#940](https://github.com/kubernetes-sigs/agent-sandbox/issues/940) → fix [PR #1087](https://github.com/kubernetes-sigs/agent-sandbox/pull/1087) | 👍 done (07-07). Paste **comment 5** on #940 (tested replay-leg finding + flap-test blindness), then ask an approver for **`/ok-to-test` on #1087 — its CI has never run**. Do **not** endorse #1087 as-is: our tested finding shows the revised tip still double-records. |

Posted for the record (no longer asks): comment 1 → #873 and comment 2 → #751, both 2026-07-03.

**Comment 3** — on [asbx#952](https://github.com/kubernetes-sigs/agent-sandbox/issues/952):

> Data point: across ~800 restores per path we see 5–7% of snapshot-backed sandboxes served blank-but-Ready. Until readiness gates on "restore verified", no restore-backed metric is trustworthy. Happy to share the measurement detail.

**Comment 4** — on [substrate PR #353](https://github.com/agent-substrate/substrate/pull/353):

> We've hit this exact wedge 3× in 2 days; every recovery required manually recycling the worker. The terminal CRASHED classification here fixes the retry-forever loop — worth also releasing the worker assignment on terminal failure so the actor re-runs on a fresh worker (that's the manual recovery that works today).

**Comment 5** — on [asbx#940](https://github.com/kubernetes-sigs/agent-sandbox/issues/940):

> Data point at benchmark fire scale: this overcount goes far beyond the +2/+3-of-500 reported here. Under a scale-from-0 burst we measured the histogram `_count` at ~1.7–2.05× the true Ready-transition count (45 records for 22 claims in one window; 51 for 30 in another) — enough to fail any throughput cross-check built on it. We confirmed the mechanism you suspected: a stale informer snapshot replays the not-Ready→Ready edge past the `oldReady` early-return, and the timing predicate's UpdateFunc repopulates the first-observed entry for the same UID, so the post-record cleanup doesn't stop the replay. The direction in #1087 looks right to us — but we've now tested the stale-informer-replay leg against the revised tip and it still double-records: a regression test that hands the reconciler a stale snapshot taken before the first Ready reconcile (no Ready status, no persisted annotation — what a stale cache serves on a watch replay) observes histogram sample count 2 vs expected 1, and the annotation re-stamp's merge patch carries no optimistic lock so the stale-base write also succeeds. One related review point: the PR's flap tests assert `CollectAndCount == 1`, which counts *series*, not observations — a second `Observe()` on the same label set leaves it at 1, so those tests pass on the double-recording path; asserting histogram sample count instead would catch it. We have a tested record-once variant (UID-keyed, first-observer-wins, cleaned up on delete) staged as reference material if a comparison is useful, plus the failing regression test itself.

## Worth raising in conversation (4) — nothing filed, per the no-filings rule

No upstream issue exists for these (dup-search verified). Raise directly if the conversation opens; full technical bodies in the [detail page](UPSTREAM_BLOCKERS_DETAIL.md).

| # | One-line blurb | Detail |
|---|---|---|
| 6 | agent-sandbox warm-pool claims stall under concurrency — internal retry rides exponential backoff, inflating tail latency (p95 ~9s at N=300 burst); tested fix staged as reference. (Adjacent-but-distinct: asbx#1059, #478 — cross-link, don't dup.) | [§S4](UPSTREAM_BLOCKERS_DETAIL.md#s4-adoption-completion-cache-race) |
| 7 | substrate golden-actor bring-up treats a transient state as fatal — templates wedge forever; activation-latency metric has zero clean records ever. | [§U1](UPSTREAM_BLOCKERS_DETAIL.md#u1-snapshot-type-unspecified) |
| 8 | substrate DurableDir broke snapshotting for simple actors — e2e suite red since Jun 27 (~8 new FAILs/day). | [§U2](UPSTREAM_BLOCKERS_DETAIL.md#u2-data-snapshot-zero-ddv) |
| 9 | substrate bricks its actor-listing API after any proto field rename on a long-lived cluster — second trigger already in review. | [§U3](UPSTREAM_BLOCKERS_DETAIL.md#u3-ateredis-strict-protojson) |

## Reference patches (offer only if a maintainer asks)

- [`agent-sandbox:adoption-completion-bounded-requeue`](https://github.com/AlexBulankou/agent-sandbox/tree/adoption-completion-bounded-requeue) — item 6, tested, ready.
- [`agent-sandbox:resume-clears-suspended-condition`](https://github.com/AlexBulankou/agent-sandbox/tree/resume-clears-suspended-condition) — item 1, historical (superseded by KEP-119 direction).
