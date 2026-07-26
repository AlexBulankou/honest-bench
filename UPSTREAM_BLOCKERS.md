# Upstream blockers — active asks only

Active upstream blockers that need action now, ordered **substrate-first** (substrate is the primary product: private GA in September, public GA in October; the sandbox runtime moves to maintenance mode). Completed and superseded items live in **[Recently landed](#recently-landed-archive)** and are dropped on the next refresh — this page stays work-to-do-only by standing discipline.

**Engagement model:** upstream approvers are engaged directly through existing maintainer relationships. We do not open new issues/PRs in the upstream repos; reference patches are staged and offered only if a maintainer asks. Engineering depth for every item: [**UPSTREAM_BLOCKERS_DETAIL.md**](UPSTREAM_BLOCKERS_DETAIL.md). Machine-readable link states: [`render/upstream_links.json`](render/upstream_links.json) — updated in the same commit as any state change here, and re-swept against the public GitHub API on each refresh.

_Link states last verified live against the public GitHub API: **2026-07-26**._

## Substrate (primary product — GA-critical)

| # | What it blocks | Where | Act now |
|---|---|---|---|
| S1 | **`ListActors` bricks after any proto field rename on a long-lived cluster.** A record written under an older Actor/Worker field name fails the post-rename strict decoder, so the actor-listing API returns an error. Three rename events have already shipped (#227, #370, #455); each is a live trigger. A GA cluster is long-lived by definition — this is a **GA blocker**. | fix [substrate#356](https://github.com/agent-substrate/substrate/pull/356) (open since 2026-07-02; maintainer asked the author to resolve conflicts 2026-07-24 — `mergeable_state=dirty`) | **Get #356 approved + merged before GA.** It is the durable structural fix (binary-protobuf record encoding); a maintainer nudge landed 07-24 but the PR has merge conflicts blocking it as-is — track for the author's rebase. |
| S2 | **Actors wedge on an untagged `runsc` exit-128.** An exit-128 on the checkpoint/restore path is returned untagged/retriable, so the actor never reaches terminal `CRASHED` and never releases its worker — it retries forever. This bites on **both** sides of the golden-snapshot round-trip: the fscheckpoint/**suspend** take (`TestExternalVolumeLifecycle`, a missing pause-container external-volume mount annotation) and the **restore** spec-validation (`TestPlatformMetricsEmitted` — `"Mounts" does not match across checkpoint restore`, a DurableDir mount-Options normalization asymmetry). Both are current causes of the substrate demo e2e RED (detail [§U8](UPSTREAM_BLOCKERS_DETAIL.md#u8-external-volume-golden-timeout) suspend-side, [§U9](UPSTREAM_BLOCKERS_DETAIL.md#u9-metrics-restore-128-alreadyexists) restore-side). The regression is git-pinned to a single introducing commit — `9300387` (#405 "per-actor external volume flow"), which flipped the app-container durable-dir mount from nil `Options` to `["bind","rw"]` (inside the measurement-pinned window `2c327172`→`aa1d14a7`) and is the shared root of both the suspend- and restore-side triggers; for each trigger the regression-site == the turn-key fix-site — so a GA cluster, which ships the golden-snapshot round-trip by definition, hits this deterministically: **this is a GA blocker.** | [substrate#50](https://github.com/agent-substrate/substrate/issues/50) | **Raise the checkpoint-failure-classification gap with the maintainers.** Terminal-crash classification + auto worker-release already landed (#353, #475 merged); the missing piece is tagging the exit-128 path terminal so those clean paths fire on both the suspend and restore sides. The two triggers each also have a distinct substrate-side mount-spec fix staged in the detail sections. No upstream fix filed yet. |

## Sandbox (maintenance mode — shepherd in-flight fixes to merge)

Sandbox moves to maintenance; the default customer recommendation shifts to substrate. Only in-flight upstream fixes to track to approval remain:

| # | What it blocks | Where | Act now |
|---|---|---|---|
| X1 | **Resume reliability can't be validated** — a resumed sandbox reports `Suspended` forever. | [asbx#873](https://github.com/kubernetes-sigs/agent-sandbox/issues/873) → fix [asbx#1150](https://github.com/kubernetes-sigs/agent-sandbox/pull/1150) (in review) | **Track #1150 to approval.** It implements the persistent `Suspended` condition; reviewer requested, not yet approved. |
| X2 | **Startup-latency histogram overcounts under burst** — undermines throughput cross-checks built on it. | [asbx#940](https://github.com/kubernetes-sigs/agent-sandbox/issues/940) → fix [asbx#1087](https://github.com/kubernetes-sigs/agent-sandbox/pull/1087) (in review) | **Track #1087 to approval** (low priority). The suspend/resume re-record leg already merged (#1114); #1087 is the more targeted stale-informer-replay fix, still open and not owner-approved. |
| X3 | **5–7% of snapshot restores are blank-but-Ready** — readiness does not gate on "restore verified". | [asbx#952](https://github.com/kubernetes-sigs/agent-sandbox/issues/952) | Watch only — data point already shared upstream; no pending action until a maintainer responds. |

## Platform (GKE / gVisor — for alex, no OSS approver)

Not an OSS repo, so there is no PR to track — evidence is parked for alex to hand to a Google engineering contact or file at [issuetracker.google.com](https://issuetracker.google.com). Full diagnosis + ready-to-paste report body: [detail page](UPSTREAM_BLOCKERS_DETAIL.md#g1-gvisor-premature-exit).

| # | What it blocks | One-line diagnosis |
|---|---|---|
| G1 | The untrusted-code-execution sandbox scenario — 100% `TIMED_OUT` since 2026-06-29 (0 PASS). | A `RuntimeClass: gvisor` sandbox's PID 1 exits on its own with a clean `exitCode: 0` after a few seconds instead of the commanded duration — no OOM, no kubelet kill. Root-caused to the gVisor/runsc runtime layer, pool-wide, reproduces across node-image builds. |

## Recently landed (archive)

Resolved upstream; kept as a breadcrumb, dropped on the next refresh.

- **True-TTFE measurement basis** — webhook-inject-timestamp example merged ([asbx#761](https://github.com/kubernetes-sigs/agent-sandbox/pull/761), closes [asbx#751](https://github.com/kubernetes-sigs/agent-sandbox/issues/751)); adopted on the Kata cold measurement path.
- **Warm-pool claim requeue** — bounded requeue merged ([asbx#1108](https://github.com/kubernetes-sigs/agent-sandbox/pull/1108)), removing the exponential-backoff tail-latency inflation.
- **Histogram suspend/resume re-record leg** — persisted-annotation guard merged ([asbx#1114](https://github.com/kubernetes-sigs/agent-sandbox/pull/1114)); the internal falsification target is empirically cleared (the residual replay-leg fix is #1087, still tracked as X2).
- **Substrate actor terminal-crash + worker-release** — [substrate#353](https://github.com/agent-substrate/substrate/pull/353) (terminal `CRASHED` classification) and [substrate#475](https://github.com/agent-substrate/substrate/pull/475) (auto worker-release on crash) merged; the residual exit-128 classification gap is tracked as S2.
- **Substrate golden-actor bring-up (`WaitGoldenActor` wedge)** — structurally unreachable at the current build after the `SNAPSHOT_TYPE_UNSPECIFIED` enum removal ([substrate#370](https://github.com/agent-substrate/substrate/pull/370)) and synchronous in-txn snapshot-info write ([substrate#227](https://github.com/agent-substrate/substrate/pull/227)).
