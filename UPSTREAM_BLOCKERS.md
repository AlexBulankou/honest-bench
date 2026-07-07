# Upstream blockers — executive view

One screen: **what's blocked in product terms, where to engage upstream, and a ready-to-paste comment.** You never need to open a PR — staged patches are parked on the maintainer's forks as reference material, offered only if a maintainer asks. Each row is self-contained: forward it to the team as-is. Engineering depth (full diagnoses, file-ready bodies, patch provenance, dup-search evidence): [**UPSTREAM_BLOCKERS_DETAIL.md**](UPSTREAM_BLOCKERS_DETAIL.md).

_Freshness: refreshed on every blocker state change; link states hand-verified at each refresh. Last verified: 2026-07-07 (all 9 filed anchors re-checked live — agent-sandbox #873/#893/#751/#761/#952/#940/#1087 and substrate #50/#353 all still open; no state flips). Engagement update: **comments 1 and 2 were posted upstream on 2026-07-03** (on #873 and #751); **item 1's fix [PR #893](https://github.com/kubernetes-sigs/agent-sandbox/pull/893) has stalled** — no commits since 2026-05-29 (~5.5 weeks; the 2026-06-16 activity was CI-bot re-runs, not commits), unit-test presubmit failing, no merge signal (see row 1); **#940's fix [PR #1087](https://github.com/kubernetes-sigs/agent-sandbox/pull/1087) tip REVISED to `35944a17`** (a 2026-07-06 07:22Z review-feedback commit — corrects the prior "tip unchanged / CI-bot noise" read; it adds an annotation **backfill** for the Patch-failure + mid-flap gap, stamping a sentinel value, with a regression test) — the **stale-informer-replay leg is CONFIRMED open — now TESTED, 2026-07-07** (a local regression test against tip `35944a17` double-records — histogram sample count 2 vs expected 1 — when the reconciler is handed a stale cache snapshot taken before the first Ready reconcile: the replayed object carries neither the Ready status nor the persisted annotation, so both guards pass, and the annotation re-stamp uses a non-optimistic-lock merge patch so the stale-base write also succeeds; all 12 of the tip's own subtests pass in the same run. Bonus finding: the tip's flap tests assert series count (`CollectAndCount`), which is blind to a second observation on the same label set — they pass on the double-recording path; review note ready to raise), and the 2026-07-06 fire added cross-runtime cold-path controls pinning the defect to the warm-pool path (row 5); **item 4's fix [substrate PR #353](https://github.com/agent-substrate/substrate/pull/353) is actively converging** — 3 new commits late 2026-07-06 (test-helper cache convergence, transient-GCS-error tagging, ListActors data-race fix), 18 review comments, mergeable-clean (row 4); items 3–5 remain open asks. Machine-readable mirror: [`render/upstream_links.json`](render/upstream_links.json) (feeds the per-cell links on the rendered pages) — update it in the same PR as any blocker-state change here._

---

## Engage now — upvote + one comment each (5 items · comments 1–2 posted 2026-07-03)

| # | What it blocks (why it matters) | Where to engage | Your move |
|---|---|---|---|
| 1 | **Resume-from-suspend numbers are unpublishable.** A resumed sandbox permanently reports `Suspended`, so resume reliability (previously measured ~93%) can't be validated — the entire gVisor × Resume row of the metrics table is on hold. | [agent-sandbox#873](https://github.com/kubernetes-sigs/agent-sandbox/issues/873) *(issue, open)* → fix [PR #893](https://github.com/kubernetes-sigs/agent-sandbox/pull/893) *(implements the accepted KEP-119 — **stalled** as of 2026-07-06: no commits since 2026-05-29, unit-test presubmit failing, no merge signal)* | ✅ **comment 1 posted** (2026-07-03) — regression test offered; no assignee response, PR stalled ~5.5 weeks. The offered regression test is the natural unstick if the assignee re-engages. Still: 👍 both |
| 2 | **True time-to-first-execution is unmeasurable.** The end-to-end latency metric records nothing on any stock install, so the North-Star "&lt;1s TTFE" can only be published as an upper bound. | [agent-sandbox#751](https://github.com/kubernetes-sigs/agent-sandbox/issues/751) *(issue, open)* → example fix [PR #761](https://github.com/kubernetes-sigs/agent-sandbox/pull/761) *(open)* | ✅ **comment 2 posted** (2026-07-03). Still: 👍 both |
| 3 | **"Ready" can mean an empty sandbox.** 5–7% of snapshot restores serve a blank pod marked Ready — restore-success can't be asserted for any snapshot-backed number. | [agent-sandbox#952](https://github.com/kubernetes-sigs/agent-sandbox/issues/952) *(design issue, open)* | 👍 · paste **comment 3** on #952 |
| 4 | **Actors wedge permanently mid-suspend** (hit 3× in 2 days; each needed manual recovery) — suspend/resume reliability on substrate. | [substrate#50](https://github.com/agent-substrate/substrate/issues/50) *(issue, open)* → fix [PR #353](https://github.com/agent-substrate/substrate/pull/353) *(in review — actively converging: 3 new commits 2026-07-06, mergeable-clean)* | 👍 both · paste **comment 4** on PR #353 |
| 5 | **Kata throughput numbers can't be cross-checked.** The controller-side startup-latency histogram double-records Ready transitions on stale-informer replays — at benchmark fire scale we measured ~1.7–2× overcount, so the controller rate leg disagrees with the acquisition leg and the agreement gate reads a false mismatch; the affected Kata cells publish honest-empty. 2026-07-06 fire narrowed it: warm-pool legs FAIL the gate on **both** runtimes (rel-diff ~0.44 gVisor / 0.55–1.0 Kata) while cold no-warm-pool legs PASS on both (0.004/0.001 and 0.000/0.032) — the warm-pool-fulfilled load leg is the discriminating repro. | [agent-sandbox#940](https://github.com/kubernetes-sigs/agent-sandbox/issues/940) *(issue, open — self-assigned upstream 2026-06-05)* → fix [PR #1087](https://github.com/kubernetes-sigs/agent-sandbox/pull/1087) *(open, in review — opened 2026-07-05; tip revised 2026-07-06 (`35944a17`, review feedback): annotation backfill now covers the Patch-failure + mid-flap gap too; stale-informer-replay leg CONFIRMED open — TESTED 2026-07-07: a local regression test on this tip double-records (sample count 2 vs 1) from a pre-annotation stale snapshot, while the tip's own flap tests are blind to it because they assert series count, not observations — raise both in review)* | 👍 both · paste **comment 5** on #940 |

**Comment 1** — ✅ posted on [agent-sandbox#873](https://github.com/kubernetes-sigs/agent-sandbox/issues/873) 2026-07-03 (kept for the record):

> We're publishing a GKE agent-sandbox benchmark and this is the one defect holding the entire resume-from-suspend row: a resumed sandbox keeps reporting Suspended, so resume reliability can't be validated. The direction in #893 looks right to us — is there a review ETA? (We have a regression test staged if useful.)

**Comment 2** — ✅ posted on [agent-sandbox#751](https://github.com/kubernetes-sigs/agent-sandbox/issues/751) 2026-07-03 (kept for the record):

> Without a production writer for this annotation, end-to-end claim startup latency is unmeasurable on any stock deployment — our public benchmark can only publish an upper bound for time-to-first-execution. Support for landing this; we also have an in-tree webhook draft if maintainers would prefer that over example manifests.

**Comment 3** — on [agent-sandbox#952](https://github.com/kubernetes-sigs/agent-sandbox/issues/952):

> Data point: across ~800 restores per path we see 5–7% of snapshot-backed sandboxes served blank-but-Ready. Until readiness gates on "restore verified", no restore-backed metric is trustworthy. Happy to share the measurement detail.

**Comment 4** — on [substrate PR #353](https://github.com/agent-substrate/substrate/pull/353) (and 👍 [#50](https://github.com/agent-substrate/substrate/issues/50)):

> We've hit this exact wedge 3× in 2 days; every recovery required manually recycling the worker. The terminal CRASHED classification here fixes the retry-forever loop — worth also releasing the worker assignment on terminal failure so the actor re-runs on a fresh worker (that's the manual recovery that works today).

**Comment 5** — on [agent-sandbox#940](https://github.com/kubernetes-sigs/agent-sandbox/issues/940):

> Data point at benchmark fire scale: this overcount goes far beyond the +2/+3-of-500 reported here. Under a scale-from-0 burst we measured the histogram `_count` at ~1.7–2.05× the true Ready-transition count (45 records for 22 claims in one window; 51 for 30 in another) — enough to fail any throughput cross-check built on it. We confirmed the mechanism you suspected: a stale informer snapshot replays the not-Ready→Ready edge past the `oldReady` early-return, and the timing predicate's UpdateFunc repopulates the first-observed entry for the same UID, so the post-record cleanup doesn't stop the replay. The direction in #1087 looks right to us — but we've now tested the stale-informer-replay leg against the revised tip and it still double-records: a regression test that hands the reconciler a stale snapshot taken before the first Ready reconcile (no Ready status, no persisted annotation — what a stale cache serves on a watch replay) observes histogram sample count 2 vs expected 1, and the annotation re-stamp's merge patch carries no optimistic lock so the stale-base write also succeeds. One related review point: the PR's flap tests assert `CollectAndCount == 1`, which counts *series*, not observations — a second `Observe()` on the same label set leaves it at 1, so those tests pass on the double-recording path; asserting histogram sample count instead would catch it. We have a tested record-once variant (UID-keyed, first-observer-wins, cleaned up on delete) staged as reference material if a comparison is useful, plus the failing regression test itself.

---

## Not yet filed upstream (4 items) — forward the blurb, or say `file them`

No upstream issue exists for these yet (dup-search verified). Full file-ready bodies are in the [detail page](UPSTREAM_BLOCKERS_DETAIL.md); either forward a blurb to the team or say **`file them`** and the exact text gets staged for whoever files.

| # | One-line forward blurb | Detail |
|---|---|---|
| 6 | agent-sandbox warm-pool claims stall under concurrency — an internal retry rides exponential backoff, inflating tail latency (p95 ~9s at N=300 burst). A tested fix is staged as a reference patch. | [§S4](UPSTREAM_BLOCKERS_DETAIL.md#s4-adoption-completion-cache-race) |
| 7 | substrate golden-actor bring-up treats a transient state as fatal — templates wedge forever, and the activation-latency metric has zero clean records ever. | [§U1](UPSTREAM_BLOCKERS_DETAIL.md#u1-snapshot-type-unspecified) |
| 8 | substrate's new DurableDir feature broke snapshotting for simple actors (none of their volumes are durable) — the e2e suite has been red since Jun 27. | [§U2](UPSTREAM_BLOCKERS_DETAIL.md#u2-data-snapshot-zero-ddv) |
| 9 | substrate bricks its actor-listing API after any proto field rename on a long-lived cluster — a forward-compat gap whose second trigger is already in review. | [§U3](UPSTREAM_BLOCKERS_DETAIL.md#u3-ateredis-strict-protojson) |

---

## Reference patches (nobody needs to open these)

Parked on the maintainer's forks; offer only if a maintainer asks ("we have a tested patch staged"):

- [`agent-sandbox:adoption-completion-bounded-requeue`](https://github.com/AlexBulankou/agent-sandbox/tree/adoption-completion-bounded-requeue) — item 6, tested, ready.
- [`agent-sandbox:resume-clears-suspended-condition`](https://github.com/AlexBulankou/agent-sandbox/tree/resume-clears-suspended-condition) — item 1, historical (superseded by the KEP-119 direction; kept for reference).
