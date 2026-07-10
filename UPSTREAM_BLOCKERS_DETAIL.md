# Upstream blockers — engineering detail (appendix)

> **This is the engineering appendix.** The page to read (and forward) is [**UPSTREAM_BLOCKERS.md**](UPSTREAM_BLOCKERS.md) — one screen, product language, upvote+comment moves. This appendix carries the full diagnoses, file-ready bodies, patch provenance, and dup-search evidence backing every row there. Maintained by s-dev; refreshed whenever a blocker moves.

This page inventories every upstream defect currently holding an honest-bench metric (or the trust in one): **what** is blocked, the **diagnosis** already done, and the maintainer-facing **action** prepared to copy-paste quality. Every action is a **GH issue** — new, or adopted-existing where the dup-search found the same defect already filed — or **GH issue + PR** when a fix is staged, in which case a DCO-signed branch is already pushed to the maintainer's fork and opening the PR is one click. See the conventions below. Unlike [README.md](README.md) and [WORK_IN_PROGRESS.md](WORK_IN_PROGRESS.md), which are machine-rendered from harness runs, this page is **hand-maintained** — owned by the s-dev crew and refreshed whenever a blocker moves (filed, merged, superseded, rebased). Absent cells on the rendered pages link to [WORK_IN_PROGRESS.md](WORK_IN_PROGRESS.md) for their reason class; this page carries the upstream half of that story.

House fence (this page ships verbatim in a public repo): `AlexBulankou/a` is a **private** repo, so internal issue refs render as plain "internal tracking a#NNNN" prose — never a link, never a bare `#NNNN`. Public honest-bench issues render as normal hb#NNN links. Upstream projects are named in plain English, with real links to their public issues/PRs.

_Last hand-refresh: 2026-07-03 (rev 2 — issue-first actions, fork-ready branches, verified dup-search)._

## Conventions (rev 2026-07-03)

1. **Every blocker files as a GitHub issue — `Action` is `GH issue` or `GH issue + PR`.** A PR never ships without its issue; the issue is the durable record even when a fix is attached. Where the dup-search found the SAME defect already filed upstream, the action is `GH issue — use existing #N`: comment the evidence there, never file a duplicate.
2. **No fork creation — the maintainer's standing forks are used and agent-prepared.** [`AlexBulankou/agent-sandbox`](https://github.com/AlexBulankou/agent-sandbox) and [`AlexBulankou/substrate`](https://github.com/AlexBulankou/substrate) are the forks. For every `GH issue + PR` row, agents sync the fork's `main` to upstream, cut a clean branch from the upstream tip, apply the staged patch DCO-signed (authored as the maintainer, no bot co-author), and push. The Steps block then reduces to: file the issue → open the compare link → Create PR.
3. **Dup-search before filing.** Each section carries a `Related upstream` list from a verified duplicate-search (issues + PRs, open + closed, adversarially re-checked) dated at the refresh. A SAME hit re-points the whole row at the existing issue; RELATED hits are linked in the filed body.

**Fork state at this refresh (2026-07-03):** both forks synced to upstream `main` (agent-sandbox → `0be472b`); branches pushed: [`adoption-completion-bounded-requeue`](https://github.com/AlexBulankou/agent-sandbox/tree/adoption-completion-bounded-requeue) (§S4 — READY), [`resume-clears-suspended-condition`](https://github.com/AlexBulankou/agent-sandbox/tree/resume-clears-suspended-condition) (§S1 — PARKED, superseded in direction by KEP 119; do not file).

**Tip-advance note (2026-07-07):** upstream agent-sandbox `main` advanced `0be472b` → `9239c0f` (2026-07-05). Staged-patch re-sweep vs `9239c0f`: the a#3641, a#3957 and both a#4064 patches still `git apply --check` clean; **the a#1347 patch (PodTemplate immutability) no longer applies — rebase required.** Upstream extracted `PodTemplate` + `VolumeClaimTemplates` out of `SandboxSpec` into a shared `SandboxBlueprint` struct inlined into both `SandboxSpec` and `SandboxTemplateSpec`, so a CEL marker on the shared field would freeze the template type too. Rebase direction settled: **per-type immutability** — a CEL transition rule at the Sandbox usage site (Deployment-template-mutable / Pod-spec-immutable precedent), leaving `SandboxTemplate` editable. Forks not re-synced yet; per-section apply-clean claims below are as-of `0be472b` unless noted.

---

## Sandbox upstream blockers

Target project: [kubernetes-sigs/agent-sandbox](https://github.com/kubernetes-sigs/agent-sandbox). Fork: [`AlexBulankou/agent-sandbox`](https://github.com/AlexBulankou/agent-sandbox), synced to upstream `main` = `0be472b` at this refresh. Dup-search 2026-07-03 (S1–S4) + 2026-07-04 (S5): **S1, S2, S3 and S5 all have existing upstream anchors — the only NEW sandbox issue to file remains S4's** (S5 adopts existing [#940](https://github.com/kubernetes-sigs/agent-sandbox/issues/940); comment there, never file a duplicate). The one `GH issue + PR` row (S4) has its DCO-signed branch already pushed to the fork; its Steps block is file-issue + one-click PR.

| # | What | Action | Steps |
|---|---|---|---|
| S1 | `Suspended=True` never clears on resume → the entire **gVisor × Resume-from-suspend** README row (all 5 cells) renders [`pending (upstream-blocked)`](WORK_IN_PROGRESS.md#upstream-blocked); prior 92.8% resume exec-success (N=1376) retracted pending this. Internal tracking a#4099. | **GH issue — use existing [#873](https://github.com/kubernetes-sigs/agent-sandbox/issues/873)** (same defect; fix in flight: PR [#893](https://github.com/kubernetes-sigs/agent-sandbox/pull/893) implementing the **merged** KEP-119 revision [#890](https://github.com/kubernetes-sigs/agent-sandbox/pull/890) — persist + flip-to-False). Our staged remove-style patch is **superseded in direction — do not file it**; ✅ comment posted on #873 **2026-07-03** (evidence + regression-test offer). **#893 review round active 2026-07-07** (stall broke): reviewer flagged a resume-detect stickiness gap — we confirmed it with a failing two-reconcile unit test at head `1d38ad65` and validated the one-line fix (details in §S1); expect one author revision before merge. | [→ §S1 file-ready material](#s1-resume-suspended-condition) |
| S2 | True-TTFE is **null-by-construction** everywhere: the startup-latency histogram's anchor annotation has no production writer upstream, so SLO-sweep pareto reads `[]` and `ready_per_s` reads null every fire ([hb#174](https://github.com/AlexBulankou/honest-bench/issues/174)). Internal tracking a#3975. | **GH issue — use existing [#751](https://github.com/kubernetes-sigs/agent-sandbox/issues/751)** (same defect: the anchor annotation is never set, histogram empty) with example-webhook PR [#761](https://github.com/kubernetes-sigs/agent-sandbox/pull/761) open ("Fixes #751"). ✅ Support comment posted on #751 **2026-07-03**. The staged **in-tree** stamper PR stays optional + decision-gated ([hb#175](https://github.com/AlexBulankou/honest-bench/issues/175)'s literal-TTFE upper bound remains the honest interim). | [→ §S2 file-ready material](#s2-ttfe-webhook-stamper) |
| S3 | Snapshot-backed sandbox goes `Ready=True` before restore is verified → blank pods served as "restored" (7.45% / 5.24% real-flap rates on the two paths); restore-success confidence is unassertable for every snapshot-restore-backed number. Internal tracking a#2614. | **GH issue — use existing [#952](https://github.com/kubernetes-sigs/agent-sandbox/issues/952)** (verified open + still the right anchor) — prepared evidence **comment** ready; no patch by design (the ask is a design decision: restore-verified readiness gate before `Ready=True`). | [→ §S3 file-ready material](#s3-restore-readiness-handshake) |
| S4 | SandboxClaim adoption-completion bounces through the informer cache with a non-nil error → **exponential-backoff amplifier** (5ms·(2^K − 1)) inflating exactly the concurrent warm-claim tails the README reports. Layer-2 only — Layer-1 resolved live by upstream [#975](https://github.com/kubernetes-sigs/agent-sandbox/issues/975). Internal tracking a#3641. | **RESOLVED UPSTREAM — [#1108](https://github.com/kubernetes-sigs/agent-sandbox/pull/1108) merged 07-08** (bounded requeue via sentinel→`RequeueAfter`, off the exponential path — the exact fix the staged patch proposed); reference patch RETIRED, do not file. Remits on the next build-from-main operator rebuild. | [→ §S4 file-ready material](#s4-adoption-completion-cache-race) |
| S5 | Controller startup-latency histogram **double-records Ready transitions** on stale-informer replays — ~1.7–2.05× overcount at fire scale, so the controller rate leg fails the acq/controller agreement gate (rel-diff tolerance 0.10) and the literal-TTFE basis can't credit; affected Kata SLO cells publish honest-empty. **2026-07-06 fire narrowed the exposure surface:** warm-pool-fulfilled legs FAIL the gate on **both** runtimes (rel-diff ~0.44 gVisor warm / 0.55–1.0 Kata warm, incl. one rung with controller event-count 0 over a 400s+ window) while cold no-warm-pool legs PASS on both (0.004/0.001 gVisor cold, 0.000/0.032 Kata cold) — the defect is **warm-pool-path-specific across runtimes**, and a warm-pool-fulfilled load leg is the discriminating repro for #1087 review. Internal tracking a#4364. | **GH issue — use existing [#940](https://github.com/kubernetes-sigs/agent-sandbox/issues/940)** (same defect — reporter suspected the same stale-status replay; we confirmed it at code level; self-assigned upstream 2026-06-05; **fix PR [#1087](https://github.com/kubernetes-sigs/agent-sandbox/pull/1087) opened 2026-07-05, in review**) — comment fire-scale evidence + confirmed mechanism there, endorse + track #1087. Staged record-once patch demoted to **direction-check-needed** — diff against the #1087 tip before ever offering it (mirror of S1's #873→#893 supersession). | [→ §S5 file-ready material](#s5-controller-histogram-double-record) |

<a id="s1-resume-suspended-condition"></a>

### §S1 — Suspended condition never clears on resume

**Internal tracking a#4099 · GH issue — use existing [#873](https://github.com/kubernetes-sigs/agent-sandbox/issues/873) · `controllers/sandbox_controller.go`**

> **⚠ Direction superseded (dup-search 2026-07-03).** Upstream tracks this exact defect as [#873](https://github.com/kubernetes-sigs/agent-sandbox/issues/873) *(open — "Fix suspended condition persistence")*, with fix PR [#893](https://github.com/kubernetes-sigs/agent-sandbox/pull/893) in flight implementing the **merged** KEP-119 revision ([#890](https://github.com/kubernetes-sigs/agent-sandbox/pull/890)): the Suspended condition must **persist and flip to False** on resume (preserving `lastTransitionTime`), **not** be removed. The staged patch below removes the condition (mirrors the `Finished` cleanup) — filing it would contradict the merged KEP. **Do not file the PR.** The branch [`resume-clears-suspended-condition`](https://github.com/AlexBulankou/agent-sandbox/tree/resume-clears-suspended-condition) stays parked on the fork for reference. Action: ✅ **DONE 2026-07-03** — comment posted on #873 (endorses #893, offers the regression test adapted to assert `Suspended=False`, not absent); awaiting the assignee's fix branch. **Stall check (2026-07-06):** #893 head `1d38ad65`, no commits since 2026-05-29 (~5.5 weeks; the 2026-06-16 activity was CI-bot re-runs, not commits), `presubmit-agent-sandbox-unit-test` FAILING (`mergeable_state: unstable`, tide pending, all other checks green), no assignee response to the review-ETA ask — the PR is not self-graduating. The offered regression test is the natural unstick if the assignee re-engages; do **not** file our parked remove-style patch (still contradicts merged KEP-119). **Review round (2026-07-07):** the stall broke — an upstream reviewer flagged a resume-detect **stickiness gap** in `computeSuspendedCondition`: the predicate matches `Status==True || Reason==PodTerminating` but not the `PodResuming` state it itself writes, so a second `pod==nil` reconcile (create failure, quota wait, webhook latency) silently regresses the reason to `PodProvisioning`. We independently confirmed this at head `1d38ad65` with a two-reconcile unit test (fails at head as predicted; a one-line predicate extension — also match `Reason==PodResuming` — makes it pass with zero change to the existing condition-table tests). Internal tracking a#4255. Net: the finding is real, the fix is one line, our banked build+vet verification of the PR remains current at the same head; expect one author revision before merge — re-verify at the new tip when it lands.

**Related upstream (dup-search 2026-07-03):** [#873](https://github.com/kubernetes-sigs/agent-sandbox/issues/873) *(open, SAME — adopt)* · [#893](https://github.com/kubernetes-sigs/agent-sandbox/pull/893) *(open PR, KEP-119 implementation — would close the defect)* · [#890](https://github.com/kubernetes-sigs/agent-sandbox/pull/890) *(merged KEP revision — mandates flip-to-False)* · [#793](https://github.com/kubernetes-sigs/agent-sandbox/issues/793) / [#1067](https://github.com/kubernetes-sigs/agent-sandbox/issues/1067) *(open, same file — rebase risks)*

**What's blocked**

- The README "Agent Sandbox — Core Metrics" matrix row **gVisor × Resume-from-suspend** — all 5 cells (Throughput @ <5s TTFE, Throughput @ <1s TTFE, TTFE p50, TTFE p95, Execution Success) render [`pending (upstream-blocked)`](WORK_IN_PROGRESS.md#upstream-blocked). The README legend names this exact gap ("the resume path's Suspended condition never clears") and tells users "Do not design around suspend/resume yet."
- The previously measured resume exec-success (~92.8%, N=1376) is **retracted** pending this fix: with a stale `Suspended=True` alongside `Ready=True`, the resume-readiness signal is untrustworthy, so no honest resume number can graduate.
- The cells graduate **the moment the fix lands upstream** — not when a run is scheduled.

**Diagnosis**

- The condition-write loop in `reconcileChildResources` upserts each freshly-computed condition via `meta.SetStatusCondition`, and `RemoveStatusCondition`s only `Finished` — never `Suspended`.
- `computeSuspendedCondition` returns `nil` once the sandbox is resumed, and `SetStatusCondition` only upserts → a prior-leg `Suspended=True` survives resume forever.

**Prepared artifact**

- Canonical: the DCO-ready patch `resume-clears-suspended-condition.patch` staged on the s-dev pod (path in the Steps block). `git apply --check` CLEAN vs upstream tip `0be472b`, re-swept 2026-07-03. No sign-off yet — the `--signoff` in the Steps adds it.
- The same fix also exists as branch `a4s2/resume-clears-suspended-condition` on the pod's clone (`/home/agent/agent-sandbox-clone`); the `.patch` is the canonical artifact.
- Verification already done: `go build` / `go test ./controllers/` (full package) / `go vet` / `gofmt -l` all clean; falsifiability confirmed by guard-neutralize (neutralizing only the fix's guard flips the new test to FAIL, restoring it flips back to PASS).
- **Optional Part-2** (`resume-retry-on-conflict.patch`, branch `a4s2/resume-inch-fix`): retry-on-conflict for the status write — an optimization, not a correctness fix, with no deterministic test. Ships **separately**; recommendation is to land Part-1 alone.
- Rebase-if: upstream [#793](https://github.com/kubernetes-sigs/agent-sandbox/issues/793) or [#1067](https://github.com/kubernetes-sigs/agent-sandbox/issues/1067) merges first (same file).

**Steps**

```bash
# copy the fenced comment below into a file, then:
gh issue comment 873 --repo kubernetes-sigs/agent-sandbox --body-file s1-873-comment.md
```

**Comment body for [#873](https://github.com/kubernetes-sigs/agent-sandbox/issues/873) (copy-paste)**

````markdown
### Independent confirmation + benchmark-side evidence

We hit this exact defect measuring resume-from-suspend for a public benchmark ([honest-bench](https://github.com/AlexBulankou/honest-bench)). Mechanism as observed on current `main` (`0be472b`): `reconcileChildResources` upserts each freshly-computed condition via `meta.SetStatusCondition` and `RemoveStatusCondition`s only `Finished`; `computeSuspendedCondition` returns `nil` once the sandbox is resumed, and `SetStatusCondition` only upserts — so a prior-leg `Suspended=True` survives resume indefinitely, alongside `Ready=True`.

Impact on our side: we **retracted** a previously-measured resume exec-success figure (~92.8%, N=1376) because the resume-readiness signal is untrustworthy while a resumed sandbox still advertises `Suspended=True` — the benchmark's entire gVisor × resume-from-suspend row renders `pending (upstream-blocked)` until this lands.

Direction: agree with the revised KEP 119 (#890) — persist + flip-to-False beats removal (`kubectl wait --for=condition=Suspended=False` usability, transition history preserved) — and #893 looks like the right implementation. Happy to contribute a regression test if useful: a Running-mode Sandbox pre-seeded with a stale `Suspended=True` (Reason `PodTerminated`) plus a Running/Ready backing pod; one reconcile; assert `Suspended` transitions to `False` (not removed) AND `Ready=True`. Fails on current `main`, passes under #893's contract.
````

**Historical — superseded, do NOT file (retained for reference only; the KEP-119 direction replaces it)**

**PR title (copy-paste)**

```
fix(controller): clear stale Suspended condition on resume
```

**PR body (copy-paste)**

````markdown
## Problem
`reconcileChildResources` computes the fresh condition set via `computeConditions`, then in a loop upserts each one with `meta.SetStatusCondition`. After the loop it explicitly `RemoveStatusCondition`s `Finished` when that condition is absent from the freshly-computed set — but there is **no equivalent cleanup for `Suspended`**.

`computeSuspendedCondition` returns `nil` once the sandbox is resumed (`OperatingMode=Running`), so a resumed sandbox has no `Suspended` condition in the freshly-computed set. Because `meta.SetStatusCondition` only upserts (never removes), a `Suspended=True` left over from a prior suspend leg **survives resume forever**. A fully-operational resumed Sandbox therefore perpetually advertises `Suspended=True` alongside `Ready=True` — a contradictory, misleading status that any consumer keying off `Suspended` (dashboards, readiness gates, the honest-bench resume probe) reads wrong.

## Fix
Mirror the existing `Finished` cleanup: track whether the computed set contains a `Suspended` condition (`hasSuspended`) and `RemoveStatusCondition` it when absent. Minimal, symmetric with the code immediately above it.

## Test
`TestSandboxReconcile_ResumeClearsStaleSuspendedCondition` — a Running-mode Sandbox pre-seeded with a stale `Suspended=True` (Reason `PodTerminated`, ObservedGeneration 1) plus a Running/Ready backing pod + headless svc; one `r.Reconcile`; asserts `FindStatusCondition(Suspended) == nil` AND `Ready=True`. Fails without the fix (stale Suspended persists), passes with it. Modeled on the existing `TestSandboxReconcile_ConditionsDoNotAccumulate` fake-client harness.
````

**Page-side notes (not part of the paste body)**

- The staged draft's "Why it matters (honest-bench context)" section is deliberately excluded from the PR body above — it is internal framing, not maintainer-facing.
- Patch base `da28b99`; intervening upstream #964/#990 edits `computeReadyCondition` — orthogonal to this patch's Suspended-clear in `reconcileChildResources`; no textual or semantic conflict (verified).

<a id="s2-ttfe-webhook-stamper"></a>

### §S2 — True-TTFE histogram has no production writer (exec-probe stamper dead)

**Internal tracking a#3975 (closed 2026-06-29 by directive; patch remains staged, unfiled) · [hb#174](https://github.com/AlexBulankou/honest-bench/issues/174) (open) · prepared upstream PR — DECISION FIRST**

**What's blocked**

- **TTFE-true everywhere in honest-bench.** Precisely ([hb#174](https://github.com/AlexBulankou/honest-bench/issues/174)):
  - the Phase-2 SLO-sweep derivation reads `pareto: []` **by construction** — the producer's `measured` flag gates on true-TTFE warm p95 (`agent_sandbox_claim_startup_latency_ms`), which nothing upstream writes, so every step reads `measured=false` and all rungs drop, every fire, until the stamper lands;
  - `ready_per_s: null` — its query rates the `_count` of the same dead metric family;
  - the matrix per-cluster throughput halves (both Throughput columns, cluster half, all warm-pool rows — currently `pending (cluster-fire)`) can land on the true-TTFE basis only after the stamper.
- Today's honest interim: the literal-TTFE exec-probe **upper bound** basis cascade ([hb#175](https://github.com/AlexBulankou/honest-bench/issues/175), merged — one-basis-per-triple, fail-closed) plus the controller-startup **lower-bound** proxy (internal tracking a#3977; banned from `ttfe_*` keys).

**Diagnosis**

- `recordClaimStartupLatency` early-returns when the anchor annotation `agents.x-k8s.io/webhook-first-observed-at` is absent.
- The annotation's only writers upstream are unit-test fixtures; the registered SandboxClaim webhook is the v1alpha1↔v1beta1 **conversion** webhook, not a mutating defaulter — there is no webhook to install (verified correction to the earlier "not installed on our cluster" framing).
- So the histogram is empty on **every deployment of upstream main** — null-by-construction, not an environment gap.

**Action — GH issue: use existing [#751](https://github.com/kubernetes-sigs/agent-sandbox/issues/751) — ✅ support comment POSTED 2026-07-03; in-tree stamper PR stays decision-gated**

The dup-search (2026-07-03) found the gap already filed upstream: [#751](https://github.com/kubernetes-sigs/agent-sandbox/issues/751) *(open)* — "`agents.x-k8s.io/webhook-first-observed-at` annotation is never set, leaving ClaimStartupLatency empty" — quotes the exact skip-guard this section diagnoses, and maintainers confirm no webhook ships today. Open PR [#761](https://github.com/kubernetes-sigs/agent-sandbox/pull/761) ("Fixes #751") adds a **documentation example** of the mutating webhook (user-applied manifests), not an in-tree production writer. So the upstream conversation **already exists** — commenting evidence + supporting #751/#761 does not reverse the 2026-06-29 "skip upstream conversations" call. What remains decision-gated is only whether to ALSO offer our staged **in-tree** stamper (operator-managed `MutatingWebhookConfiguration`, reusing the self-generated CA) as an upstream PR alternative to #761's example. If that is greenlit: the staged patch base is **STALE** (`985d1dd`, 2026-06-29) vs current tip `0be472b` — `git apply --check` + rebuild before signing (this one was **not** in the 2026-07-03 apply-clean re-sweep).

**Related upstream (dup-search 2026-07-03):** [#751](https://github.com/kubernetes-sigs/agent-sandbox/issues/751) *(open, SAME — adopt)* · [#761](https://github.com/kubernetes-sigs/agent-sandbox/pull/761) *(open PR — example-webhook docs, "Fixes #751")* · [#540](https://github.com/kubernetes-sigs/agent-sandbox/pull/540) *(merged — origin of the gap: shipped the annotation consumer, deliberately no writer; note it names the annotation `webhook-first-seen-at`)* · [#565](https://github.com/kubernetes-sigs/agent-sandbox/pull/565) *(open PR — different metric/annotation: client-side `client-first-requested-at`)* · [#940](https://github.com/kubernetes-sigs/agent-sandbox/issues/940) *(open — controller-histogram overcount, distinct defect; now tracked as [§S5](#s5-controller-histogram-double-record))* · [#1051](https://github.com/kubernetes-sigs/agent-sandbox/issues/1051) *(open — label-cardinality cleanup on the same histograms)*

**Steps**

```bash
# copy the fenced comment below into a file, then:
gh issue comment 751 --repo kubernetes-sigs/agent-sandbox --body-file s2-751-comment.md
```

**Comment body for [#751](https://github.com/kubernetes-sigs/agent-sandbox/issues/751) (copy-paste)**

````markdown
### Confirmed from an independent deployment + benchmark impact

Independent confirmation on a stock deployment of current `main`: with no cloud-provider webhook installed, `agent_sandbox_claim_startup_latency_ms` records zero samples on every fire — `recordClaimStartupLatency` hits the missing-annotation early-return on every claim, so the histogram is empty **by construction**, not as an environment gap. (The registered SandboxClaim webhook is the v1alpha1↔v1beta1 conversion webhook, not a mutating defaulter — there is nothing to "enable".)

Benchmark impact: for a public Agent-Sandbox benchmark ([honest-bench](https://github.com/AlexBulankou/honest-bench)) this makes true end-to-end TTFE unmeasurable by construction — we currently publish a literal-TTFE **upper bound** from an exec-probe instead, with the true-TTFE cells marked blocked on this issue.

+1 to #761's example webhook. Also worth considering: an **in-tree** production writer — a `CustomDefaulter` on SandboxClaim wired into the existing generic-webhook builder, with a programmatic `MutatingWebhookConfiguration` the operator self-applies (reusing the same self-generated CA + caBundle-injection pattern `tls.go` already uses for the conversion configs), `failurePolicy: Ignore` + `SideEffects: None` so an observability webhook can never gate the create path. We have a compile-tested draft (4 files / +228, 3/3 defaulter unit tests) if maintainers would take this in-tree rather than as example manifests.
````

**Prepared artifact**

- Full patch (4 files / +228: new `extensions/controllers/sandboxclaim_webhook.go` + test, `cmd/agent-sandbox-controller/main.go`, `cmd/agent-sandbox-controller/tls.go`) + paste-ready PR body, both staged on the s-dev pod (paths in the Steps block).
- Compile-verified in-pod against `985d1dd`: `go build` exit 0, `go vet` exit 0, 3/3 defaulter unit tests PASS.

**Steps — optional in-tree stamper PR (only if greenlit; STALE base, re-check first)**

```bash
# agents re-verify + push the branch to the fork on greenlight (see Conventions);
# manual fallback:
# 1) pull the full patch + PR body off the pod
kubectl exec a4-a4s1-0 -n a4 -c agent -- \
  cat /home/agent/work/.a4/share/3975-webhook-stamper.patch.md > 3975-webhook-stamper.patch.md
kubectl exec a4-a4s1-0 -n a4 -c agent -- \
  cat /home/agent/work/.a4/share/3975-patch-comment.md > 3975-pr-body.md

# 2) STALE-BASE check first — base was 985d1dd (06-29), tip has moved (0be472b+):
#    extract the diff from the .patch.md, then on a fresh upstream-main checkout:
git apply --check 3975-webhook-stamper.patch   # re-apply-check + rebuild before signing

# 3) if clean: branch, apply, sign, push to your fork, open PR
git checkout -b sandboxclaim-webhook-stamper upstream/main
git am --signoff 3975-webhook-stamper.patch
git push -u origin sandboxclaim-webhook-stamper
```

**Suggested PR title** (adjust as needed)

```
fix(extensions): stamp webhook-first-observed-at at admission so claim startup latency records
```

**PR body (as staged — flagged: file only after re-deciding; the staged doc carries the full inline diff at the marked point)**

````markdown
Closes the dead-histogram gap: `agent_sandbox_claim_startup_latency_ms` (the true end-to-end claim-creation → Sandbox-Ready TTFE) is `.Observe()`d only when a SandboxClaim already carries `agents.x-k8s.io/webhook-first-observed-at`, but **nothing upstream writes that annotation** — so `recordClaimStartupLatency` hits the missing-annotation early-return on every claim and the histogram reads empty (`dataItems: null`). The companion proxy `agent_sandbox_claim_controller_startup_latency_ms` populates fine because the controller self-stamps `controller-first-observed-at`; the *true* TTFE just lacks its production writer.

The fix supplies that writer at the earliest server-side observation point — a **mutating admission webhook** that stamps `webhook-first-observed-at` before the claim is persisted. Chose admission-time (a `CustomDefaulter`) over a controller-loop stamp because the controller only sees the object after it lands in etcd and after informer lag, which would bias the very latency the histogram is meant to measure; admission is the true t0 for a claim.

**Shape:** one new `SandboxClaimDefaulter` (`extensions/controllers/sandboxclaim_webhook.go`) wired into the existing generic-webhook builder, plus a programmatic `MutatingWebhookConfiguration` the operator self-applies — there is **no static webhook manifest upstream** (the operator already self-generates its CA and injects the caBundle into the CRD conversion configs in `tls.go`; this reuses the same caPEM and the same Get→Create/Patch idempotency pattern). 4 files / +228:
- **new** `extensions/controllers/sandboxclaim_webhook.go` — the defaulter (idempotent: stamps only when absent, so a client-supplied value or re-admission is preserved; pure observability, never rejects or mutates spec).
- **new** `extensions/controllers/sandboxclaim_webhook_test.go` — 3 unit tests (stamps-when-absent with a fixed clock; preserves-existing; nil-clock-uses-time.Now within `[before,after]`).
- `cmd/agent-sandbox-controller/main.go` — `.WithDefaulter(&extensionscontrollers.SandboxClaimDefaulter{})` on the existing `ctrl.NewWebhookManagedBy(mgr, &extensionsv1beta1.SandboxClaim{}).Complete()` chain (which already registers the conversion webhook — `Complete()` registers both), plus the `ensureSandboxClaimMutatingWebhook(...)` call gated on `extensions`.
- `cmd/agent-sandbox-controller/tls.go` — `ensureSandboxClaimMutatingWebhook` building the config.

**failurePolicy: Ignore is load-bearing, not incidental.** The stamp is pure observability — a webhook outage must degrade the *metric* (a skipped sample) and never block SandboxClaim creation. A `Fail` policy here would let an observability webhook gate the core create path; `Ignore` + `SideEffects: None` + `timeoutSeconds: 5` keeps the data-plane impact at zero.

[full diff against clean HEAD `985d1dd` follows in the source file — main.go hook (+12), tls.go ensureSandboxClaimMutatingWebhook (+80), sandboxclaim_webhook.go defaulter (+59); test file (+78) in the attached patch]

**Compile-verified in-pod** (unlike a typical alex-id surface, this one was buildable against a local clone of the operator): against clean HEAD `985d1dd`, `go build ./cmd/agent-sandbox-controller/... ./extensions/controllers/...` → exit 0; `go vet ./extensions/controllers/` → exit 0; `go test ./extensions/controllers/ -run TestSandboxClaimDefaulter -v` → 3/3 PASS. All referenced symbols exist at HEAD: `ctrl.NewWebhookManagedBy(...).WithDefaulter(...)` (controller-runtime v0.24.1 generic webhook API), `asmetrics.WebhookAnnotation`, `extensionsv1beta1.SandboxClaim`, the `tempClient`/`caPEM`/`webhookServiceName`/`webhookNamespace` locals in `main()`, and `clientgoscheme` registers `admissionregistration/v1` on the tempClient's scheme.

**Suggested upstream e2e check:** create a SandboxClaim with no annotations, assert it comes back carrying `agents.x-k8s.io/webhook-first-observed-at` (RFC3339Nano), drive it to Ready, and assert `agent_sandbox_claim_startup_latency_ms` now has a non-null sample — the deterministic repro of the empty-histogram gap.
````

**Page-side notes (not part of the paste body)**

- The bracketed `[full diff …]` line marks where the staged source doc inlines the complete diff — retrieve the full doc from the pod (Steps step 1) rather than pasting only this preview; the PR itself shows the diff, so trim that line when pasting.
- The compile-verification claims are pinned to `985d1dd` — refresh them (or drop the paragraph) after the stale-base re-check.

<a id="s3-restore-readiness-handshake"></a>

### §S3 — Snapshot-restore state-carryover flap: Ready before restore is verified

**Internal tracking a#2614 (flap) / a#2621 (durable quant stores) · prepared COMMENT on upstream [kubernetes-sigs/agent-sandbox#952](https://github.com/kubernetes-sigs/agent-sandbox/issues/952) (verified open 2026-07-03) · no patch by design**

**What's blocked**

- **Restore-success confidence for every snapshot-restore-backed number**: the internal §10 (plain save→restore cycle) and §11 (warm-pool snapshot instant-on) scenarios today, and any future snapshot-backed warm-pool / instant-on row on this benchmark — a `Ready=True` sandbox may carry **none** of the snapshot state, so Execution-Success / state-carryover honesty cannot be asserted for restore-path cells until the readiness↔restore handshake exists upstream.
- Quantified real-flap rates (non-real classes excluded — excluding them is load-bearing; a naive pass/fail rate over-reads ~7×):
  - warm-pool + snapshot path: **7.45%** blank-served (60/805, most recent 2026-06-30 — **live, not fixed**);
  - plain save→restore round-trip: **5.24%** (42/802, most recent 2026-06-21).

**Diagnosis**

- `computeReadyCondition` gates `Ready` purely on `podReady` + `svcReady` — zero snapshot/restore/checkpoint references anywhere under `controllers/`; the recent #964/#990 rework of `computeReadyCondition` still added no restore signal.
- Restore is driven client-side (`gke_extensions/snapshots/`), out-of-band from the controller — no handshake.
- The warm pool faithfully trusts `Ready` (`isSandboxReady` → `ReadyReplicas`), so it serves blank pods. Warm-pool composition is **exonerated** — the plain path flaps at comparable severity, so the defect is the **shared** restore/readiness seam.

**Action**

Comment on the open upstream design issue [#952](https://github.com/kubernetes-sigs/agent-sandbox/issues/952) ("Correct time at which we can mark a resume pod as ready"), broadening it with the blank-served evidence + rates. **No patch by design** — the ask is a design decision: a restore-verified readiness gate before `Ready=True`, which belongs to the maintainers' readiness/restore handshake design, not to a drive-by fix.

**Related upstream (dup-search 2026-07-03):** [#952](https://github.com/kubernetes-sigs/agent-sandbox/issues/952) *(open, SAME — the anchor; verified: its body already describes PodRestored being set only after background restore completes while resume is marked done on Pod-Ready)* · [#415](https://github.com/kubernetes-sigs/agent-sandbox/pull/415) *(merged — client-side `is_restored_from_snapshot` polls the `PodRestored` pod condition: proves the restore-verified signal exists pod-side but is opt-in client polling, not a readiness gate — strengthens the ask)* · [#397](https://github.com/kubernetes-sigs/agent-sandbox/pull/397) *(closed unmerged — prior premature-Ready gate attempt for PodIP; same mechanism family)* · [#248](https://github.com/kubernetes-sigs/agent-sandbox/issues/248) *(open — pause/resume state-carryover test ask)* · [#990](https://github.com/kubernetes-sigs/agent-sandbox/pull/990) *(merged — reworked `computeReadyCondition` with zero restore awareness: the gap was revisited and left open)*

**Steps**

```bash
# copy the fenced comment below into a file, then:
gh issue comment 952 --repo kubernetes-sigs/agent-sandbox --body-file 952-comment.md
```

**Comment body (copy-paste)**

````markdown
# Snapshot-backed Sandbox marked Ready before restore is verified — blank pod served as "restored" (broadens #952)

## Summary

A snapshot-backed Sandbox can be marked `Ready=True` and **served** while its checkpoint state was **not** actually restored — the runtime cold-started a fresh blank container instead of restoring the checkpoint, but the controller's readiness signal cannot tell the difference. The consumer (a warm pool, or a direct claim) then hands out a pod that passed its readiness probe but carries **none** of the snapshot's in-memory or on-disk state. This is the concrete failure mode behind #952's framing, with new evidence that it is a property of the **shared restore path**, not of warm-pool composition.

## Root cause — restore and readiness live in two layers with no handshake

The restore mechanism and the readiness signal are in different layers that never exchange a "restore verified" signal:

- **Readiness (Go controller).** `controllers/sandbox_controller.go:313` `computeReadyCondition(sandbox, err, svc, pod)` gates `Ready` purely on pod- and service-level signals:
  - `podReady` = pod `Running` + `PodReady` condition True + non-empty `PodIPs` (`sandbox_controller.go:362-366`)
  - `svcReady` = service exists when required (`sandbox_controller.go:389-392`)
  - `if podReady && svcReady` → `Ready=True` (`sandbox_controller.go:399`).

  It **never inspects snapshot restoration.** There is zero `snapshot` / `restore` / `checkpoint` / `readinessGate` reference anywhere under `controllers/` or `internal/` on current `main` — the controller layer has no restore concept at all. Note this is not a stale-code artifact: the current tip (`0be472b`, "update computeReadyCondition and add printer columns to Sandbox CRD", #964/#990) **modified `computeReadyCondition` itself** — adding an `isSuspended` early-return and `PodSucceeded`/`PodFailed` handling — yet still added no restore-verified signal. The readiness gate was recently revisited and the restore-awareness gap was left open.

- **Restore (client extension).** The GCS + runsc-checkpoint restore is driven client-side, out-of-band from the controller, under `clients/python/agentic-sandbox-client/k8s_agent_sandbox/gke_extensions/snapshots/` (`sandbox_with_snapshot_support.py`, `snapshot_engine.py`, `podsnapshot_client.py`).

Because the two layers do not handshake, a fresh (blank) container and a genuinely-restored container are **indistinguishable** at the readiness layer: both reach `PodReady` and a non-empty PodIP, so both flip the Sandbox to `Ready=True`.

## Why this surfaces as a blank warm-served pod

The warm-pool controller trusts that same Ready condition: `extensions/controllers/sandboxwarmpool_controller.go` computes `Status.ReadyReplicas` from `isSandboxReady(&sb)` (used at lines 137 / 169 / 206-207; `ReadyReplicas` set at line 173). So when a snapshot-backed warm pod cold-starts blank, it still passes readiness → the Sandbox is marked `Ready` → the warm pool counts it ready and serves it. The warm pool is a faithful **consumer** of the readiness signal; it is not the source of the defect.

## New evidence: it's the shared restore path, not warm-pool composition

Two independent scenarios exercise the same restore path and both exhibit the same blank-vs-restored severity:

1. **Warm-pool + snapshot composition** — claim a slot from a snapshot-backed warm pool; assert the served pod carries the snapshot's pre-loaded nonce. Observed the warm pool serve a **blank** pod (fresh state) while bind latency stayed instant — the composition silently dropped restorable state.
2. **Plain snapshot save→teardown→restore round-trip** (no warm pool at all) — assert in-memory counter + on-disk state survive the round-trip. This flapped at the **same** blank-vs-restored severity with no warm-pool component in the path.

Because the plain (warm-pool-free) round-trip flaps at comparable severity, warm-pool composition is **exonerated** — the non-determinism lives in the shared restore / readiness seam itself, which is exactly why this broadens #952 beyond the warm-pool case.

**Severity (real-flap rate; non-real classes — stub / infra / orphan / gap-probe — excluded; continuous observation, n=845 real restores per path as of 2026-07-02):**

| Path | blank-served rate | most-recent blank-served |
|---|---|---|
| warm-pool + snapshot | 7.45% (60 / 805 exercised) | 2026-06-30 |
| plain save→restore round-trip | 5.24% (42 / 802 exercised) | 2026-06-21 |

**The flap is live, not fixed.** An earlier read of this data (2026-06-27) saw both paths clean for ~6 days and could not distinguish a genuine fix from dormancy. Fresh data resolves that ambiguity: the **warm-pool path recurred on 2026-06-30** (3 blank-served restores) after that clean window, so the defect is demonstrably still reproducing — not fixed. This is consistent with the root cause above being **still present in current `main`** (the readiness path has no restore signal to gate on): the flap recurs whenever restore timing slips, and the intervening clean stretches are best read as **environmental / timing** (restore happening to win the race), not a code fix.

**A recency divergence worth watching (not yet conclusive).** Over the full window the two paths flap at a comparable rate (7.45% vs 5.24%), which is the basis for treating this as a shared-restore-path property rather than a warm-pool-composition artifact. But the *most-recent* blank-served event is more recent on the warm-pool path (2026-06-30) than on the plain path (2026-06-21, ~12 days quiet). A single 3-event burst is too small to conclude the warm-pool path carries exposure *beyond* the shared seam — the aggregate rates remain comparable — so the exoneration above still holds at the aggregate level; the divergence is flagged for continued observation. Going forward, dedup-on-change controller-digest capture will attribute the next shift to a specific controller image.

## Proposed direction (the #952 handshake)

Introduce a restore-verified signal the controller can gate on for snapshot-backed sandboxes — e.g. a controller- or runtime-injected **readiness gate / pod condition** set only after checkpoint carryover is verified — so `computeReadyCondition` requires "restore-verified" before flipping `Ready=True` on a sandbox that was created from a snapshot. The fix belongs at the readiness/restore handshake; gating downstream (in the consumer or in a test) would only mask the product bug.

## Reproduce

1. Snapshot a Sandbox with known in-memory + on-disk state.
2. Tear it down; restore from the snapshot (directly, or via a snapshot-backed warm pool slot claim).
3. Observe that the restored Sandbox reaches `Ready=True` regardless of whether the checkpoint actually carried; on a cold-start-blank outcome the pod is `Ready` and served but carries fresh (not restored) state.
4. The blank outcome is intermittent (timing-dependent), so reproduction needs repeated round-trips, not a single run.
````

**Page-side notes (not part of the paste body)**

- Source symbols/paths verified against upstream `main` `0be472b` on 2026-07-03; #952 verified open the same day. Re-verify both at posting time.

<a id="s4-adoption-completion-cache-race"></a>

### §S4 — SandboxClaim adoption-completion cache-race (exponential-backoff amplifier)

> **RESOLVED UPSTREAM (2026-07-08) — reference patch RETIRED, do not file.** Upstream [#1108](https://github.com/kubernetes-sigs/agent-sandbox/pull/1108) ("bounded requeue for adoption-completion cache lag instead of exponential backoff", merged `eb8ffd3`) landed the exact fix this section proposed — sentinel→`RequeueAfter` off the exponential-failure path — in `extensions/controllers/sandboxclaim_controller.go` + test. The deployed build-from-main controller picks it up on the next operator rebuild, so the tail-latency inflation and the adoption-completion e2e flake remit there. **Deploy state (2026-07-09): DEPLOYED — remission clock started.** The live build-from-main controller was refreshed 2026-07-09 to source commit `81c13d8` (a direct descendant of `eb8ffd3`, verified 1 commit ahead via the upstream compare API), so it now carries #1108. The remission-confirmation clock (≥3 clean adoption/TTL e2e fires on a build ≥ `eb8ffd3`) has **started at 0/≥3** — the next scheduled adoption/TTL e2e fires on this build are candidate remission fires 1–3, and any green fire *before* the refresh remains a normal intermittent pass (not remission-fire-1). Gate to close the internal tracking item: ≥3 consecutive clean fires accrue on the post-#1108 build. The diagnosis and prepared-artifact detail below are retained as the historical record of the independently-reached fix; they are no longer an action.

**Internal tracking a#3641 (Layer-2 only — Layer-1 resolved live by upstream [#975](https://github.com/kubernetes-sigs/agent-sandbox/issues/975)) · RESOLVED UPSTREAM by [#1108](https://github.com/kubernetes-sigs/agent-sandbox/pull/1108) · `extensions/controllers/sandboxclaim_controller.go` + test**

**What's blocked**

- No single matrix cell is held by this one — it is a **tail-latency + trust** blocker:
  - warm-pool claim adoption under concurrency accrues controller-runtime per-item exponential backoff (5ms·(2^K − 1)) on every cache-lag pass, inflating exactly the concurrent warm-claim tails the README reports ("Concurrent burst" table, N=300 Warm-pool row: p95 9.393s, Throughput@<1s = 0; and the sustained-300/sec pool hand-off line);
  - it flaps the upstream sandbox e2e suite (bounded waits: obs-annotation ~13s, TTL-after-finished ~74s) that guards the bench substrate — our durable e2e history shows assertion-fails 06-26 → 07-01 before the 07-02 full-suite PASS.
- **Layer split:** the deterministic Layer-1 (`spec.replicas` 422) is **resolved live** by upstream [#975](https://github.com/kubernetes-sigs/agent-sandbox/issues/975); this row covers only the residual Layer-2 cache-race.

**Diagnosis**

- `completeAdoption` patches ownership server-side; the caller then **deliberately returns a non-nil error** ("triggered adoption completion… retry") to force a cache re-read; the item is never `Forget`'d, so K consecutive "failures" compound exponentially.
- The naive fix (`return sandbox, nil`) is **refuted** — it breaks the tested duplicate-adoption protection (`TestSandboxClaimPreventsDuplicateAdoptionDuringCacheLag`).
- Correct fix keeps the deferral and changes only the requeue **mechanism**: sentinel `errAdoptionTriggeredRetry` → nil error + `RequeueAfter=immediateRequeueDelay` (1 ms), resetting K.

**Prepared artifact**

- Canonical: DCO-ready patch `3641-adoption-completion-cache-race.patch` staged on the s-dev pod (path in the Steps block); `git format-patch` off `da28b99`, `git apply --check` CLEAN vs current tip `0be472b`, re-swept 2026-07-03. (No live clone branch — the `.patch` is the canonical artifact.)
- Verification already done: `gofmt -l` clean, `go build ./extensions/controllers/...` exit 0, `go vet` exit 0, full `extensions/controllers` suite PASSES including the updated duplicate-adoption test; falsifiability probe confirmed (gating off the sentinel conversion flips the test to FAIL).
- **Rebase-if (MOOT post-#1108):** the pre-supersession plan was to rebase on upstream PR [#1072](https://github.com/kubernetes-sigs/agent-sandbox/issues/1072) ("tolerate AlreadyExists in SandboxClaim createSandbox", same file, different facet) if it merged first. With [#1108](https://github.com/kubernetes-sigs/agent-sandbox/pull/1108) merged the requeue-mechanism fix is upstream; #1072 remains a distinct adjacent facet, not a rebase dependency for this (now-retired) patch.

**Related upstream (dup-search 2026-07-03 — no existing report of this defect; new issue is correct):** [#1059](https://github.com/kubernetes-sigs/agent-sandbox/issues/1059) *(open — intra-reconcile retry-count knob in `adoptSandboxFromCandidates`; adjacent path)* · [#1042](https://github.com/kubernetes-sigs/agent-sandbox/issues/1042) *(open — cold-start `createSandbox` AlreadyExists race; same file, different path)* · [#1072](https://github.com/kubernetes-sigs/agent-sandbox/issues/1072) *(open PR — fixes #1042; adjacent facet)* · [#527](https://github.com/kubernetes-sigs/agent-sandbox/issues/527) *(open — redundant-reconcile churn, addressed via event predicates in #532)* · [#975](https://github.com/kubernetes-sigs/agent-sandbox/pull/975) *(merged — resolved the deterministic Layer-1)*

**Steps**

```bash
# 1) file the issue (title + body below):
gh issue create --repo kubernetes-sigs/agent-sandbox \
  --title "SandboxClaim adoption-completion look-again requeue rides the exponential failure rate-limiter (cache-lag retry compounds to multi-second backoff under concurrency)" \
  --body-file s4-issue.md

# 2) open the PR from the ready branch (DCO-signed, based on upstream main @ 0be472b):
#    https://github.com/kubernetes-sigs/agent-sandbox/compare/main...AlexBulankou:agent-sandbox:adoption-completion-bounded-requeue?expand=1
#    Paste the prepared PR body below and add a first line: "Fixes #<issue from step 1>".
```

**Issue body (copy-paste → s4-issue.md)**

````markdown
When a `SandboxClaim` adopts a warm-pool `Sandbox`, the controller completes the adoption with a direct API-server patch and then **deliberately returns a non-nil error** ("triggered adoption completion… retry") so the next reconcile re-reads the sandbox from the informer cache. The deferral itself is load-bearing (tested duplicate-adoption protection during cache lag) — but signalling it with a non-nil error routes every look-again through the workqueue's `ItemExponentialFailureRateLimiter`, and the item is never `Forget`'d between passes, so K cache-lag re-reads compound to `5ms·(2^K − 1)` of backoff.

Under concurrency (e.g. four claims adopting from one warm pool) the per-claim cache-lag windows overlap, K grows, and the backoff reaches multi-second stalls — inflating exactly the concurrent warm-claim tail latencies and pushing bounded e2e waits (observability-annotation read ~13s; TTL-after-finished ~74s) past their windows. Observable signature: repeated `"Triggered adoption completion for sandbox, retry"` per claim with exponentially-spaced retry log lines.

Proposed fix (PR to follow from `AlexBulankou:adoption-completion-bounded-requeue`): keep the first-pass deferral, change only the requeue **mechanism** — a sentinel `errAdoptionTriggeredRetry` mapped in Reconcile to `ctrl.Result{RequeueAfter: immediateRequeueDelay}, nil` (the 1 ms constant already exists). Nil error ⇒ the item is `Forget`'d each pass, so each look-again costs a bounded 1 ms instead of exponential backoff; the tested duplicate-adoption protection is preserved (only the retry *cost* changes, not the deferral).

Related: #1059 (intra-reconcile retry-count knob, adjacent), #1042 / #1072 (cold-start create race, same file), #527 (redundant-reconcile churn), #975 (resolved the deterministic Layer-1 of our original report).
````

**Suggested PR title** (adjust as needed)

```
fix(sandboxclaim): take the adoption-completion look-again requeue off the exponential-failure path
```

**PR body (copy-paste)**

````markdown
## Summary

When a `SandboxClaim` adopts a warm-pool `Sandbox`, the controller completes the adoption with a direct API-server patch and then **deliberately returns an error to force a requeue**, so that the *next* reconcile re-reads the sandbox and observes it as controlled-by-claim. Because the re-read comes from the controller's **informer cache**, which updates asynchronously after the patch, the next reconcile frequently still sees the sandbox as controlled-by-`SandboxWarmPool` and **re-enters adoption completion, re-patching the same object** — repeating until the cache catches up. The churn is intermittent and worse under concurrency; it delays claims reaching steady state and can push downstream bounded-wait assertions (TTL-after-finished cleanup, observability annotation) past their windows.

## Root cause — adoption completion is verified through the cache, not the in-memory object

The adoption path already holds a fully-correct, server-persisted object after the patch, but throws it away and re-reads from cache:

- **The patch is a direct API write.** `completeAdoption` (`extensions/controllers/sandboxclaim_controller.go:817`) mutates the in-memory sandbox — clearing the warm-pool owner reference and setting the claim as controller via `controllerutil.SetControllerReference(claim, adopted, r.Scheme)` (`:839`) — then persists it with `r.Patch(ctx, adopted, client.MergeFrom(originalAdopted))` (`:904`). On success the in-memory `adopted` object both carries the new ownerRef (set before the patch) and the fresh resourceVersion (written back by `Patch`), so it already satisfies `metav1.IsControlledBy(adopted, claim)`. Note `adopted` is the same `*v1beta1.Sandbox` pointer the caller passed in — `completeAdoption` mutates it in place, so on return the caller already holds the correct, server-persisted object.

- **The caller discards that object and forces a cache re-read.** After a successful `completeAdoption(ctx, claim, sandbox)` (`:1305`), the caller does not use the now-correct `sandbox` it still holds; it returns an error to requeue: `return nil, fmt.Errorf("triggered adoption completion for sandbox %s, retry", sbName)` (`:1321`, with the adjacent comment *"If succeeded, return error to retry so next reconcile sees it controlled by us!"*).

- **The next reconcile re-`Get`s from the informer cache and can short-circuit only if the cache is fresh.** The follow-up reconcile re-reads the sandbox with `r.Get(ctx, …, sandbox)` (`:1273`) and returns cleanly only when `metav1.IsControlledBy(sandbox, claim)` is true (`:1274`). If the cache has not yet observed the ownerRef patch, the controllerRef still reads `Kind == "SandboxWarmPool"` (`:1290`), so the code **re-enters `completeAdoption` and re-patches** — and returns the retry error again. This loops until the informer cache reflects the write.

## Why it flakes intermittently and worse under load

The patch is synchronous against the API server; the informer cache is updated asynchronously via watch. An immediate (rate-limited) requeue frequently re-reads *before* the watch has delivered the update, so adoption completion re-triggers K times per claim, where K is the number of cache-convergence cycles. With a warm-pool exercise running **four concurrent claims**, the per-claim cache-lag windows overlap and the reconcile queue churns longer before every claim settles to controlled-by-claim.

**The amplifier — why K re-reads become a multi-second wall-clock delay.** The re-read is not free-running: the retry value is a genuine non-nil error. It is **not** in `suppressErrors` (only the metadata/ownership/env/VCT sentinels are), so it does not take the quiet `return result, nil` path; and it is **not** `ErrWarmPoolNotFound` / `ErrTemplateNotFound`, so it does not take the friendly fixed `RequeueAfter: 1*time.Minute` path. It falls through to the tail `return result, reconcileErr` as a real error. When Reconcile returns a non-nil error, controller-runtime requeues the item through the workqueue's default rate limiter — `ItemExponentialFailureRateLimiter` (base 5ms, doubling per **consecutive** failure for the same item key, `MaxOf` a 10qps/100 token bucket). Because the controller never `Forget`s the key between retries (each pass returns the error again), each adoption-completion retry counts as another consecutive failure, so the per-claim requeue delay grows **exponentially**: 5ms → 10ms → 20ms → 40ms … ≈ `5ms·(2^K − 1)` total. Low K (uncontended) settles sub-100ms and reads green; high K under the simultaneous `claim00`–`claim03` contention reaches multi-second backoff and blows the bounded e2e wait windows (observability-annotation read ~13s; TTL-after-finished `WaitForObjectNotFound` ~74s). The backoff is **per-claim-key**, so when cache lag is bad for one adopting claim it is bad for all four at once — matching the observed *both-tests-fail-together* signature.

The observable signature is repeated `"Triggered adoption completion for sandbox, retry"` across all claims, with `Reconciler error  error="triggered adoption completion for sandbox …, retry"`. A deterministic schema rejection it is not — the timing dependence, and the exponential spacing of the retry log lines, are the tell.

## Impact

Downstream consumers that gate on a terminal state inside a bounded wait can time out while the claim is still churning toward steady state — e.g. a TTL-after-finished path waiting for the `SandboxClaim` to be deleted, or an observability-annotation read on the warm-pool-backed path. The defect is in the adoption-completion control flow, not in those consumers.

## Proposed direction — take the look-again requeue off the exponential-failure path

> **Correction (verified locally in the clone, 2026-07-01):** an earlier draft of this report proposed *"continue with the in-memory post-mutation object — replace the retry error with `return sandbox, nil`"* as the preferred fix. **That fix is refuted.** It breaks a deliberate, tested invariant: `TestSandboxClaimPreventsDuplicateAdoptionDuringCacheLag` (`sandboxclaim_controller_test.go`) asserts that the *first* reconcile pass, when it triggers adoption but the cache still shows the warm-pool owner, must **defer** — it must NOT finalize the claim's status with the adopted sandbox and must NOT adopt an *extra* warm sandbox during the lag. The retry-through-cache is therefore **intentional duplicate-adoption protection**, not an oversight: returning the sandbox directly on the first pass would finalize during cache lag and defeat that protection. Returning `return sandbox, nil` makes the test fail (`expected reconcile to fail with cache lag error, but it succeeded`). Likewise the APIReader variant, by making the first pass succeed, would erode the same protection. Any fix that *returns an error* AND *escapes the exponential limiter* is impossible (they are mutually exclusive: controller-runtime rate-limits every non-nil Reconcile error). So the correct fix must keep the first-pass deferral while changing only the requeue *mechanism* from error-with-backoff to nil-with-bounded-delay.

The defect is not the requeue itself — the requeue is load-bearing duplicate-adoption protection during cache lag. The defect is that the requeue is signalled by a **non-nil error**, which routes through the exponential failure rate limiter and, because the item is never `Forget`d between the recurring retries, compounds to `5ms·(2^K − 1)`. The fix removes the *amplifier* while preserving the *deferral*:

- **(shipped, in the accompanying patch) Signal the look-again with a sentinel, convert it to a bounded nil-error requeue.** Introduce a sentinel `errAdoptionTriggeredRetry` and return it (wrapped, `fmt.Errorf("%w: sandbox %s", …)`) from the retry site instead of the ad-hoc `fmt.Errorf`. Add a Reconcile branch (mirroring the `ErrWarmPoolNotFound` special-case) that maps `errors.Is(reconcileErr, errAdoptionTriggeredRetry)` to `ctrl.Result{RequeueAfter: immediateRequeueDelay}, nil` — the `immediateRequeueDelay = time.Millisecond` constant already exists for exactly this fast-deterministic-requeue purpose. Returning a **nil** error lets controller-runtime `Forget` the item (resetting K), so each look-again costs a bounded 1ms requeue instead of `5ms·(2^K − 1)` exponential backoff. The first-pass deferral is unchanged: status is still not finalized and the extra warm sandbox is still not adopted during cache lag (the sandbox is `nil` on this Reconcile path, so no finalization occurs) — only the requeue *cost* changes. This is why `TestSandboxClaimPreventsDuplicateAdoptionDuringCacheLag`'s status-not-finalized and extra-not-adopted assertions are **preserved**; only its first-pass assertion is updated from *"returns the cache-lag error"* to *"returns nil with a positive `RequeueAfter`"*, matching the new contract.

- **(rejected) Return the in-memory object (`return sandbox, nil`).** Refuted above — defeats the tested first-pass duplicate-adoption protection during cache lag.

- **(rejected) APIReader cache-bypassing read before requeue.** Same problem — by making the first pass succeed it erodes the deferral the test enforces.

The shipped fix removes the timing-dependent **timeout** (the exponential backoff that blew the bounded e2e wait windows) **without** weakening the duplicate-adoption protection. It caps the amplifier over the K cache re-reads; K itself is unchanged because those re-reads *are* the protection.

## Reproduce

1. Create a snapshot/warm-pool-backed `SandboxClaim` that adopts a warm `Sandbox`.
2. Drive several claims concurrently (e.g. four) against one warm pool so reconciles interleave and the informer cache lags behind the adoption patches.
3. Observe repeated `"Triggered adoption completion for sandbox, retry"` per claim and a reconcile queue that takes multiple passes per claim to settle to controlled-by-claim.
4. The churn is timing-dependent and amplified by concurrency, so reproduction needs concurrent claims, not a single adoption.
````

**Page-side notes (not part of the paste body)**

- Line numbers in the body are pinned to `a2ff59b` as a convenience; the **stable anchors** are the function names and the verbatim log string (`"Triggered adoption completion for sandbox, retry"`). Re-verify line numbers against then-current `main` at filing time.
- Local verification (2026-07-01): `git apply --check` clean vs `da28b99` (2 files, 38 controller + 20 test insertions); build/vet/gofmt clean; full `extensions/controllers` suite PASSES under the new contract; falsifiability probe flips the test to FAIL when the sentinel conversion is gated off.

<a id="s5-controller-histogram-double-record"></a>

### §S5 — Controller startup-latency histogram double-records Ready transitions (stale-informer replay)

**Internal tracking a#4364 · comment on existing [#940](https://github.com/kubernetes-sigs/agent-sandbox/issues/940) · `extensions/controllers/sandboxclaim_controller.go`**

**What's blocked**

- The controller-side startup-latency histogram `_count` is the **controller rate leg** of the acq/controller agreement gate that guards the literal-TTFE published basis (rel-diff tolerance 0.10). With the double-record inflating the controller leg ~1.7–2.05×, the gate reads a **false mismatch** even on a clean fire — one Kata fire produced a deceptive ~0.27 rel-diff purely from replayed records — so the literal basis can't credit and the affected **Kata SLO cells publish honest-empty**.
- This is a **trust** blocker, not a matrix-cell-specific one: any cross-check built on the histogram `_count` (throughput validation, rate agreement, cell crediting) inherits the overcount. Our benchmark-side harness fix (counting boundary transitions instead of raw records) restores *our* leg, but the upstream histogram stays wrong for every consumer.

**Diagnosis**

- `recordCreationLatencyMetric` is guarded only by an `oldReady` early-return. A **stale informer snapshot replays the not-Ready→Ready edge** (its `oldStatus` predates the recorded transition), so a later reconcile re-enters the record path after metrics were already emitted and records the same claim again.
- The deferred `observedTimes` cleanup does not stop the replay: the timing predicate's **UpdateFunc repopulates the first-observed entry for the same UID** (`LoadOrStore`), re-arming the record path between the first record and the replay.
- Event multiplicity is nondeterministic and grows with load — measured 45 histogram records for 22 claims in one window and 51 for 30 in another (scale-from-0 burst). The +2/+3-of-500 in #940's original report is the same defect at low contention.
- **Cross-runtime cold-path controls (2026-07-06 fire):** the acq/controller disagreement reproduces on the **warm-pool-fulfilled path only** — gVisor warm rel-diff ~0.44 across three rungs and Kata warm 0.55–1.0 (one rung's controller leg recorded **zero** events over a 412s window), while cold no-warm-pool legs PASS the 0.10 gate on **both** runtimes (gVisor 0.004/0.001; Kata 0.000/0.032). The replay/fidelity defect is warm-pool-path-specific, not a runtime artifact — a warm-pool-fulfilled load leg is the discriminating repro to exercise when validating #1087.

**Related upstream (dup-search 2026-07-04):** [#940](https://github.com/kubernetes-sigs/agent-sandbox/issues/940) *(open, SAME — adopt; kind/bug, priority/important-soon; reporter's suspected mechanism — "a subsequent reconcile still sees an old status snapshot where the claim was not Ready yet and records the same claim again" — is exactly what we confirmed at code level; **fix PR [#1087](https://github.com/kubernetes-sigs/agent-sandbox/pull/1087) opened 2026-07-05 — open, in review, fixes the readiness-flap double-record**)* · [#1087](https://github.com/kubernetes-sigs/agent-sandbox/pull/1087) *(open, in review — the fix PR; tip revised 2026-07-05: persists a first-Ready annotation + drains the first-observed entry when it is set, covering the genuine-flap and controller-restart legs. **Diffed against our staged patch 2026-07-05 — the stale-informer-replay leg is still open:** the annotation guard reads the same possibly-stale cached object as the `oldReady` guard, metrics are observed before the stamp persists, and the merge-patch carries no optimistic lock — raise this in review)* · [#522](https://github.com/kubernetes-sigs/agent-sandbox/pull/522) *(merged — introduced the metric)* · [#533](https://github.com/kubernetes-sigs/agent-sandbox/pull/533) *(merged — moved timing into event predicates / first-observed tracking, the layer the replay defeats)* · [#546](https://github.com/kubernetes-sigs/agent-sandbox/pull/546) *(merged — fixed stale `observedTimes` for reused SandboxClaim names; adjacent staleness class)* · [#527](https://github.com/kubernetes-sigs/agent-sandbox/issues/527) *(open — redundant reconciles under scale, the replay's supply side)* · [#245](https://github.com/kubernetes-sigs/agent-sandbox/issues/245) *(metrics design — treats histogram `_count` as a throughput signal, the assumption this defect breaks)* · [#1051](https://github.com/kubernetes-sigs/agent-sandbox/issues/1051) *(open — label-cardinality cleanup on the same histograms; independent)*

**Prepared artifact**

- Tested **record-once gate** patch staged internally (base upstream `main` = `0be472b`, build-green): a UID-keyed `sync.Map` on the reconciler; `LoadOrStore` first-observer-wins gate placed after the existing `oldReady` guard and before label derivation; `DeleteFunc` cleanup kept **separate** from the `observedTimes` drain so the timing-predicate refill cannot re-arm it; keyed by UID (not NamespacedName) so delete+recreate under the same name records fresh. Controller restarts remain covered by the `oldReady` guard (post-restart cache already shows Ready).
- A durable alternative (persist a recorded-marker on the object, e.g. annotation-consume) is documented for maintainers if they prefer surviving controller restarts over an in-memory gate; a middle variant (NamespacedName-keyed) was considered and rejected (name-reuse aliasing).
- **Superseded-in-flight (2026-07-05): upstream fix PR [#1087](https://github.com/kubernetes-sigs/agent-sandbox/pull/1087) is open + in review.** **Direction-check DONE (2026-07-05; RE-RUN 2026-07-07 against tip `35944a176`, and again 2026-07-08 against the current tip `91a870a116c9` — finding unchanged):** #1087 hardens the transition guard with a persisted first-Ready annotation (covers genuine readiness flaps + controller restarts) but does **not** close the stale-informer-replay leg — the annotation guard reads the same possibly-stale cache as the `oldReady` guard, records metrics *before* the stamp persists, and patches without an optimistic lock, so a replayed edge can still double-record and can never be turned into retry-then-skip (histograms can't be un-observed). The UID-keyed record-once gate is therefore the gap-closing comparison to raise in #1087 review (paste-ready review note staged, internal tracking a#4364). Do **not** open a competing PR.

**Steps**

```bash
# No new issue — #940 IS this defect (dup-search 2026-07-04). Engage there:
# 1) 👍 https://github.com/kubernetes-sigs/agent-sandbox/issues/940
#    and 👍 https://github.com/kubernetes-sigs/agent-sandbox/pull/1087 (the fix PR)
# 2) paste the comment body below on #940 — it endorses + tracks #1087
# 3) do NOT open a competing PR; staged-patch diff vs the #1087 tip is DONE
#    (2026-07-05) — replay leg UNCOVERED; raise it in #1087 review
#    (paste-ready review note staged, internal tracking a#4364)
```

**Comment body (copy-paste → on #940)**

````markdown
Data point at benchmark fire scale: this overcount goes far beyond the +2/+3-of-500 reported here. Under a scale-from-0 burst we measured the histogram `_count` at ~1.7–2.05× the true Ready-transition count (45 records for 22 claims in one window; 51 for 30 in another) — enough to fail any throughput cross-check built on it.

We confirmed the mechanism suspected in the report: a stale informer snapshot replays the not-Ready→Ready edge past the `oldReady` early-return in `recordCreationLatencyMetric` (the replayed event's old status predates the recorded transition), and the timing predicate's UpdateFunc repopulates the first-observed entry for the same UID (`LoadOrStore`), so the deferred cleanup doesn't stop the replay from recording again. Multiplicity is nondeterministic and grows with reconcile pressure, which is why low-contention runs only show +2/+3.

The direction in #1087 looks right to us — one thing worth double-checking in review is that it also holds against the stale-informer-replay leg above (the UpdateFunc repopulation is what defeats a transition-guard-only fix at our scale). We have a tested record-once variant staged (UID-keyed `sync.Map`, `LoadOrStore` first-observer-wins gate after the `oldReady` guard, delete-event cleanup kept separate from the first-observed drain so the predicate refill can't re-arm it) as reference material if a comparison is useful.
````

**Page-side notes (not part of the paste body)**

- #940 state (open) + fix PR #1087 state (open, non-draft, in review, created 2026-07-05T13:24:18Z) verified 2026-07-05 via the public API; #1087 tip at the diff was `3ef57cab` (single commit, revised 2026-07-05). **Re-verified 2026-07-08: tip is now `91a870a116c9`** (revision landed 07-08, one commit on top of `35944a176`: it delivers the reviewer-requested minimal Reconcile-level Patch-failure test — `TestReconcilePropagatesAnnotationPatchError`, a fail-once-then-succeed fake client asserting the Patch failure surfaces as a Reconcile error and the retry-backfill doesn't double-count — plus a comment-only reword of the `backfillFirstReadyAnnotation` docstring and removal of the two unused annotation-name constants from `internal/metrics/metrics.go`; **no controller-logic change**). **Direction check RE-RUN against `91a870a116c9` (2026-07-08): the stale-informer-replay leg is STILL uncovered** — the revision is test/docs/dead-code only, so the annotation guard still reads the same possibly-stale cached object, metrics are still observed before the stamp persists, and the merge-patch still carries no optimistic lock — the 2026-07-05 staged review note stands unchanged. **Review threads are now resolved** (the Reconcile-level-test ask was the last open one and this revision answers it), but **no lgtm/approved labels yet** and **CI on the new tip is not yet green** (checked 2026-07-08 ~17:10Z: lint-go passed, e2e-test pending, benchmarks-kops-gcp errored — likely infra-flake given the test-only diff, but confirm a re-run passes). So the remaining merge gates are: approval + a green re-run on `91a870a116c9`. Re-verify both states + the tip at posting time — a further tip revision re-opens the direction check.
- The staged patch is verified against upstream `main` `0be472b` (build-green); it is **not** pushed to the fork — per house convention offer-on-ask, and the comment already offers it.
- Benchmark-side, the harness now counts boundary *transitions* rather than raw histogram records (internal tracking a#4364's harness fix, merged), so our controller leg is replay-tolerant — the upstream fix is still required for the histogram itself to be trustworthy for anyone else.

---

## Substrate upstream blockers

Target project: [agent-substrate/substrate](https://github.com/agent-substrate/substrate). Fork: [`AlexBulankou/substrate`](https://github.com/AlexBulankou/substrate), synced to upstream `main` at this refresh. All five rows are **GH issue** actions (no branches pushed anywhere, per the no-filings rule). **Tested reference patches now exist for U2 and U5** — both re-cut 2026-07-09 onto upstream `f1b69e9` (post the ObjectRef API refactor, `89f821f`/`f1b69e9`: `ActorRef`→`ObjectRef`, `Delete` methods return the object directly). U5's f1b69e9 cut needed one line of adaptation (its promote-test worker ref `ActorRef`→`ObjectRef`); all 7 files otherwise applied clean, full `ateapi` suite green 16.8s. It layers on the earlier [#411](https://github.com/agent-substrate/substrate/pull/411) suspend-path rewrite exactly as the prior `de5d1d9` cut did (merges cleanly WITH #411's atespace-aware locks + worker-ownership check — union test file). U2's f1b69e9 cut applied clean with zero fixes. Staged locally, offered only if a maintainer asks. All five rows re-verified STILL HOLD at `f1b69e9` (2026-07-09, post ObjectRef refactor) — U1/U2 anchors byte-held, U5's suspend path unchurned by the refactor, U3 strengthened (see its note). **Re-verified again at `992a3b30` (2026-07-10, "Rework `Create` endpoints to align with API style guide", ahead_by 1) — a lightweight tip-bump: every pinned behavior is byte-identical and both staged patches (U2, U5) `git apply --check` clean. The Create rework wraps the request payload in `Actor{Metadata: ResourceMetadata{…}}` / `Atespace{Metadata: …}`, which shifted the controller create-path line numbers (U1/U5 pins below carry the corrected `992a3b30` lines) but changed no pinned text; U3's ateredis sites and U5's `workflow_suspend.go` are unchurned by the rework.** **Re-verified once more at `86cd169` (2026-07-10, "ensure image oci config is included when building actor oci spec", [#301](https://github.com/agent-substrate/substrate/pull/301), ahead_by 1) — #301 touched only three OCI-build files (`memorypullcache/memorypullcache.go`, `cmd/atelet/oci.go`, `cmd/atelet/oci_test.go`; +110/-19), none of which is a pinned U1/U2/U3/U5 surface: every pin holds byte-identical at the `992a3b30` line numbers and both staged patches (U2, U5) still `git apply --check` clean.** U1's requeue patch is locally validated but its behavioral proof needs a cluster build+run (internal tracking a#2691). Dup-search 2026-07-03: **U4 is already filed upstream as [#50](https://github.com/agent-substrate/substrate/issues/50); its fix PR [#353](https://github.com/agent-substrate/substrate/pull/353) merged 2026-07-08 — U4 comments there instead of filing.** One sitting clears all five; suggested order: **U2 → U1 → U5 → U3 (new filings) → U4 (comment)**.

| # | What | Action | Steps |
|---|---|---|---|
| U1 | Golden-actor reconcile **hard-errors on the transient not-yet-snapshotted state** instead of requeueing (pre-#370: `SNAPSHOT_TYPE_UNSPECIFIED`; post-#370: unpopulated `SnapshotInfo.data` oneof — **survives [#370](https://github.com/agent-substrate/substrate/pull/370) 1:1**) → the activation-latency SLA store has **zero clean records ever** (metric honestly PENDING by construction); compounds with the manifest-migration stranding of pre-migration goldens. Internal tracking a#3210. | **GH issue** — file-ready below with suggested fix (requeue on unpopulated oneof, or populate `external` before the gate); dup-search: no existing report. | [→ §U1 file-ready text](#u1-snapshot-type-unspecified) |
| U2 | DATA-scope golden snapshot **hard-fails on zero durable-dir volumes** (regression in upstream #295) → golden-snapshot e2e leg deterministically RED since 2026-06-27 (45 of 46 fires FAIL as of 07-03). Internal tracking a#3842. | **GH issue** — file-ready below; recommends a caller-side scope gate (admission validation) as the primary fix; explains why a naive callee no-op just moves the brick to restore. Dup-search: no existing report. | [→ §U2 file-ready text](#u2-data-snapshot-zero-ddv) |
| U3 | Strict `protojson.Unmarshal` of persisted rows **bricks `ListActors`** across any proto field rename on a long-lived cluster; **second trigger now LIVE**: upstream [#370](https://github.com/agent-substrate/substrate/pull/370) (reserves `SnapshotInfo.type`) merged 2026-07-08 while the structural fix [#356](https://github.com/agent-substrate/substrate/pull/356) is still open. Internal tracking a#2921. | **GH issue** — file-ready below; proposes `DiscardUnknown` on read paths; the post-#370 addendum's condition is **MET** — include it. Dup-search: no existing report of the decode-brick itself. | [→ §U3 file-ready text](#u3-ateredis-strict-protojson) |
| U4 | `runsc checkpoint` against an exited/destroyed container **retried forever** (exit 128 → permanent SUSPENDING wedge); 3 independent occurrences in 2 days, 3/3 recovered only by manual worker recycle. Internal tracking a#4189. | **GH issue — use existing [#50](https://github.com/agent-substrate/substrate/issues/50)** ("Actor stuck in STATUS_SUSPENDING" — the identical exit-128 wedge, verified from its logs) — comment our 3-occurrence evidence there; fix in flight: PR [#353](https://github.com/agent-substrate/substrate/pull/353) (terminal `CRASHED` classification during checkpoint — matches our fix #1); add the recycle-not-retry follow-through as a #353 comment. | [→ §U4 file-ready text](#u4-checkpoint-exit-128) |
| U5 | Golden snapshot **registered with no `manifest.json`** — a crash mid-checkpoint-upload is laundered into a *successful* suspend (dangling-worker skip returns success; `FinalizeSuspended` promotes the unverified `InProgressSnapshot`; controller pins it with only a type check) → **every warm resume fails permanently**. Publish-side sibling of U1/U2, which brick the only remediation (regenerating the golden). Internal tracking a#4478. | **GH issue** — file-ready below; fix class is **verify-manifest-before-promote** (single choke point). [#353](https://github.com/agent-substrate/substrate/pull/353) (merged 07-08) improves legibility only; manifest-written-last (`9360791`, 06-25) narrows the window but the promote gate is still missing at HEAD. [#411](https://github.com/agent-substrate/substrate/pull/411) (merged 07-09) rewrote the suspend path (atespace-aware locks + worker-ownership check) but the promote gate is still absent. Tested reference patch staged @ `f1b69e9` (post-#411 + post ObjectRef refactor); re-verified `git apply --check` clean at `992a3b30` and again at `86cd169` (07-10, OCI-config fix #301 — unrelated surface). Dup-search: no existing report. | [→ §U5 file-ready text](#u5-nonatomic-manifest-publish) |

<a id="u1-snapshot-type-unspecified"></a>

### §U1 — Golden-actor reconcile hard-errors on the transient not-yet-snapshotted state

**Internal tracking a#3210 · file-ready issue for agent-substrate/substrate**

**What's blocked**

- **Activation-latency SLA** (internal tracking a#2998): the daily golden-actor probe has **never** produced a clean first-attempt Ready — the durable latency store has zero records (verified 2026-07-03), so the substrate health report's golden-actor activation-latency check stays honestly PENDING **by construction**.
- The snapshot-correctness payload (internal tracking a#3637) rides the same golden-actor path this bug bricks.
- **Impact widened 2026-07-02:** the snapshot `manifest.json` format migration stranded pre-migration goldens (unresumable, no backfill), and the only remediation — a fresh golden re-commit — is exactly the path this bug bricks. The two defects compound.
- Pre-existing brick (not a regression). A requeue-on-transient fix is locally patch-validated internally (applies clean, no new imports, behaviorally sound against the phase machine; re-cut to the post-#370 oneof form 2026-07-09), but behavioral proof needs a cluster build+run via fork-push capability (internal tracking a#2691) — so today this files as an **issue with a suggested fix**, not a PR.
- **Survives #370 1:1 (2026-07-09):** upstream [#370](https://github.com/agent-substrate/substrate/pull/370) (merged 2026-07-08, `b66b5c20`) deleted the `SnapshotType` enum and rewrote the gate surface to a `SnapshotInfo.data` protobuf oneof — the gate is now `GetLatestSnapshotInfo().GetExternal() == nil` at `actortemplate_controller.go:154-155` @ `af1f005`, and the transient state is the **unpopulated oneof** (`GetData() == nil`), the exact analogue of the deleted `SNAPSHOT_TYPE_UNSPECIFIED`. Same error site, same wedge.

**Related upstream (dup-search 2026-07-03 — no existing report; new issue is correct):** [#189](https://github.com/agent-substrate/substrate/pull/189) *(open PR — per-template `GoldenSnapshotWait`; same flow, doesn't touch the hard-error)* · [#362](https://github.com/agent-substrate/substrate/issues/362) *(open — post-checkpoint upload-failure fallback; different failure point)* · [#16](https://github.com/agent-substrate/substrate/issues/16) *(open — ActorTemplate lifecycle/mutability design)* · [#370](https://github.com/agent-substrate/substrate/pull/370) *(merged 2026-07-08 — rewrote the gate surface to a oneof; this bug survives 1:1)*

**Steps**

Copy the title + fenced body below into a new issue at agent-substrate/substrate:

```bash
gh issue create --repo agent-substrate/substrate \
  --title "golden-actor reconcile hard-errors on a transient not-yet-snapshotted state instead of requeueing" \
  --body-file u1-body.md
```

**Issue body (copy-paste)**

````markdown
Repo: agent-substrate/substrate · first observed on build-from-main sha bbafda0d; STILL PRESENT on the current build sha 2304dfe1 (controller image `…@sha256:2304dfe1…`) — unfixed across multiple advancing builds (private build-from-main dev cluster). Source line re-verified present (`actortemplate_controller.go:154-155`) at upstream `main` af1f005 as of 2026-07-09 (read-only checkout) — **post-#370**: PR #370 (merged 2026-07-08) deleted the `SnapshotType` enum and rewrote the gate to a `SnapshotInfo.data` oneof, but the bug survives 1:1 (same lines, same wedge; details in Source below). **The brick is exercised deterministically by the daily golden actor-probe** (`reconcile-probe-actor`): the durable in-repo `actor-probe-latency-history.jsonl` store has **zero clean records** — the gVisor golden-actor take has never once produced a clean first-attempt Ready, i.e. the take-gate has been failing on this `UNSPECIFIED` hard-error for its entire recorded history. This is a **pre-existing** brick, not a recent regression. The ate-controller log carries the matching `unexpected snapshot type for golden actor: SNAPSHOT_TYPE_UNSPECIFIED` on every probe reconcile. **Scope note — this is DISTINCT from the 2026-06-27 e2e-suite reds.** The six 37b5006 e2e `TestActorLifecycle` fires (03:17Z onward) fail on a *different, co-occurring* brick — the DATA-scope golden-suspend hard-error `no durable-dir volumes found for DATA snapshot` (`cmd/ateom-gvisor/main.go:271` @ af1f005; was `:273` at b755ae73), which fires at `actortemplate_controller.go:151` (`while suspending golden actor: %w`), one step *before* this take-gate at lines 154-155 ever runs. That second brick is NEW at 37b5006 (the demo `counter` declares zero durable-dir volumes and was PASSING pre-37b5006) and is filed separately (see the companion zero-durable-dir DATA-snapshot draft). Both are real and both should be filed; the e2e suite happens to surface the suspend-path brick first because the suspend fails before the take-gate is reached.

## Symptom
An ActorTemplate that declares `spec.snapshotsConfig.location: gs://…` (external/GCS golden snapshot) never reaches `status.phase=Ready`. `ate-controller` errors on every reconcile (exp-backoff, indefinitely):

```
ERROR Reconciler error controller=actortemplate ActorTemplate=<name>
  error="unexpected snapshot type for golden actor: SNAPSHOT_TYPE_UNSPECIFIED"
```

(That is the pre-#370 enum rendering, observed on builds ≤ b755ae73. Post-#370 builds render the `%T` of the unpopulated oneof instead — e.g. `unexpected snapshot type for golden actor: <nil>` — same error site, same wedge.)

The controller hard-error is the invariant across builds (observed on both bbafda0d and the current 2304dfe1). The `status`-field vantage varies by build: on the earlier bbafda0d build `status.goldenActorID` was assigned but the actor never went Ready; on the current 2304dfe1 build a fresh cold-take stalls at `WaitGoldenActor` with `goldenActorID` never assigned this run. Either way the actor never reaches Ready — the reconcile hard-error on the transient `UNSPECIFIED` snapshot type is the root cause, not the precise status-field state.

## Source
`cmd/atecontroller/internal/controllers/actortemplate_controller.go:154-155` @ `main` af1f005 (post-#370):
```go
if resp.GetActor().GetLatestSnapshotInfo().GetExternal() == nil {
    return ctrl.Result{}, fmt.Errorf("unexpected snapshot type for golden actor: %T",
        resp.GetActor().GetLatestSnapshotInfo().GetData())
}
```
Post-#370 proto shape (pkg/proto/ateapipb/ateapi.proto): `message SnapshotInfo { oneof data { ExternalSnapshotInfo external = 2; LocalSnapshotInfo local = 3; } reserved 1; reserved "type"; }` — the `SnapshotType` enum is deleted. The golden-actor reconcile requires the `external` oneof member; a not-yet-committed golden snapshot leaves the oneof **unpopulated** (`GetData() == nil`), the exact analogue of the pre-#370 zero-value `SNAPSHOT_TYPE_UNSPECIFIED` (which the pre-#370 gate `GetType() != SNAPSHOT_TYPE_EXTERNAL` rejected the same way).

## Why this is a bug, not a config error
- An unpopulated `SnapshotInfo.data` oneof (`GetData() == nil`) = "golden snapshot not yet externally committed", a **transient** state — but the reconcile returns a hard `error`, not `ctrl.Result{Requeue:true}` / a wait condition. The ActorTemplate is wedged permanently on a state that should self-resolve once the golden snapshot lands as `external`. (Pre-#370, this same transient state was the `SNAPSHOT_TYPE_UNSPECIFIED` zero value — #370 changed the encoding, not the semantics.)
- A plain actor (no gVisor checkpoint) with the same `snapshotsConfig` shape reaches Ready fine — so the gap is specific to the external/checkpoint golden-snapshot take path either not populating `SnapshotInfo.external` or not completing before the reconcile gates on it.
- This is a write/read disagreement on population: the write path and the read path disagree on whether the oneof is populated, and the reader treats not-yet-populated as fatal rather than not-yet-ready.

## Suggested fix (one of)
1. Treat the unpopulated oneof as not-ready → requeue with backoff (wait for the golden snapshot to commit) rather than erroring:
```go
info := resp.GetActor().GetLatestSnapshotInfo()
if info.GetData() == nil {
    return ctrl.Result{RequeueAfter: 10 * time.Second}, nil // transient: golden snapshot not yet committed
}
if info.GetExternal() == nil {
    return ctrl.Result{}, fmt.Errorf("unexpected snapshot type for golden actor: %T", info.GetData())
}
```
2. Ensure the golden-snapshot take path populates `SnapshotInfo.external` before the actor is reported back to the reconcile.

## Repro
Apply an ActorTemplate with `snapshotsConfig.location: gs://…` whose golden snapshot must be taken externally; watch `ate-controller` logs + `status.phase` (never Ready).

## Impact amplifier — compounds with the snapshot manifest.json format migration (2026-07-02 evidence)
The snapshot layout migrated to a manifest-based format (observed between 06-12 and 06-28 in our bucket): every fresh snapshot dir now carries 4 objects including a `manifest.json` that pins the runsc build (e.g. `gs://gvisor/releases/release/20260622/x86_64/runsc` + sha256), while a pre-migration golden dir has only the 3 image objects and **no manifest.json**. The current resume path hard-requires the manifest — resuming from a pre-migration golden fails with `storage: object doesn't exist` on `manifest.json`. Consequences:

- **Pre-migration goldens are silently unresumable.** No migration/backfill path shipped with the format change; an ActorTemplate whose recorded goldenSnapshot predates the migration stays Ready-looking but structurally cannot resume.
- **The only remediation is a fresh golden re-commit — which is exactly the path this bug bricks.** A new golden commit hits the unpopulated-oneof hard-error above (pre-#370: `SNAPSHOT_TYPE_UNSPECIFIED`), so operators of pre-migration templates have **no working path back to a resumable golden** until this is fixed. The two defects compound: the migration strands old goldens, and this bug blocks minting new ones.
- Synthesizing a manifest for an old golden is unsound by design — the manifest is a restore-compatibility record pinning the runsc build the snapshot was taken under, which is unknown for pre-migration dirs.
````

**Page-side notes (not part of the paste body)**

- Source pins re-verified at upstream `main` `af1f005` (2026-07-09, post-#370): same lines 154-155, now the oneof form (`GetExternal() == nil` gate; the take-gate previously moved 145-146 → 154-155 across the 37b5006 → b755ae73 advance). Re-verified again at `f1b69e9` (2026-07-09, post ObjectRef refactor): anchor byte-held, same lines 154-155 — that refactor's controller change was a 3-line call-site edit with no line shift. Re-verified once more at `992a3b30` (2026-07-10, "Rework `Create` endpoints"): take-gate text byte-identical but **line-shifted to `:161`** (the Create rework wraps the request in `Actor{Metadata: ResourceMetadata{…}}`, adding lines above the gate) — the earlier "no line shift" no longer holds at `992a3b30`; the text is unchanged. Re-verified once more at `86cd169` (2026-07-10, OCI-config fix [#301](https://github.com/agent-substrate/substrate/pull/301)): the take-gate is unchurned by #301 (OCI-build files only) — text byte-identical, line `:161` holds. **Re-verify line numbers at filing time.**
- The suggested-fix hunk (requeue-on-unpopulated-oneof; re-cut to the post-#370 oneof form 2026-07-09) is locally patch-validated internally — applies clean, well-formed Go, no new imports; phase not advanced on the new branch so a requeue self-resolves once the golden commits `external`. Behavioral proof (the daily golden take going clean) requires a cluster build+run; the repo has no pure unit test on this reconcile gate.
- The body's "filed separately" companion is §U2 — if U2 files first, add its upstream issue number where the body says "see the companion zero-durable-dir DATA-snapshot draft".

<a id="u2-data-snapshot-zero-ddv"></a>

### §U2 — DATA-scope golden snapshot hard-fails on zero durable-dir volumes

**Internal tracking a#3842 · file-ready issue for agent-substrate/substrate**

**What's blocked**

- The substrate golden DATA-snapshot e2e leg: `TestActorLifecycle`/`TestDurableDirLifecycle` deterministically RED since 2026-06-27 03:17Z — the first fire to build upstream 37b5006 ([#295](https://github.com/agent-substrate/substrate/issues/295), DurableDir support).
- Tally in the durable e2e history as of 2026-07-03: **45 FAILs of 46 fires** since onset, all `TestActorLifecycle` assertion-class.
- Reds the substrate health report's upgrade-loop section every 3h fire; **deliberately not re-pinned** — the red e2e IS the signal that #295 needs an upstream fix (a re-pin would mask the regression).
- Distinct from §U1: this suspend-path brick (controller line 151) fires **before** U1's take-gate (lines 154-155) is reached — **both** fixes are needed for a clean e2e + clean actor-probe.

**Related upstream (dup-search 2026-07-03 — no existing report; new issue is correct):** [#295](https://github.com/agent-substrate/substrate/pull/295) *(merged 2026-06-27T03:05Z — the regressing PR; merge time matches the e2e red onset)* · [#339](https://github.com/agent-substrate/substrate/issues/339) *(open — default snapshot scope for onCommit/onPause: its resolution interacts with the recommended caller-side scope gate)* · [#220](https://github.com/agent-substrate/substrate/issues/220) *(open — the feature request #295 implements)*

**Steps**

```bash
gh issue create --repo agent-substrate/substrate \
  --title "ateom-gvisor: DATA-scope golden snapshot hard-fails for actors with no durable-dir volumes (regression in #295)" \
  --body-file u2-body.md
```

**Issue body (copy-paste)**

````markdown
## Problem
An ActorTemplate whose containers declare **zero** durable-dir volumes but whose snapshot scope resolves to `Data` cannot reach `phase=Ready`: the golden-actor suspend RPC hard-fails and the template stays stuck in `WaitGoldenActor`.

Introduced by **37b5006 (DurableDir support, #295)** — `git log -S` confirms the error string first appears in that commit. The demo `counter` ActorTemplate (zero durable-dir volumes) deterministically reproduces it; the upstream demo e2e suite (`TestActorLifecycle`, `TestDurableDirLifecycle`) went red at the first build to include 37b5006 and has been 7/7 red since.

## Mechanism (source-confirmed)
`cmd/ateom-gvisor/main.go:267-275` (snapshot SAVE path):

```go
case ateompb.SnapshotScope_SNAPSHOT_SCOPE_DATA:
    var ddv []string
    for _, ctr := range req.GetSpec().GetContainers() {
        ddv = append(ddv, ctr.GetDurableDirVolumes()...)
    }
    if len(ddv) == 0 {
        return nil, fmt.Errorf("no durable-dir volumes found for DATA snapshot")  // <-- hard error
    }
    ...
```

Propagation to the controller (`cmd/atecontroller/internal/controllers/actortemplate_controller.go`, `PhaseWaitGoldenActor`):
- the error returns from `SuspendActor` and is wrapped at **line 151** (HEAD b755ae73; `if err != nil` at 150 → `return` at 151; was line 140 at 37b5006): `while suspending golden actor: %w` → ActorTemplate never leaves `WaitGoldenActor`.
- (Contrast internal tracking a#3210, which fails one step later at **lines 154-155** (holds at af1f005 post-#370; take-gate `if` at 154 → `return` at 155; was line 146 at 37b5006) — suspend *succeeds* but the `SnapshotInfo.data` oneof is unpopulated / `GetExternal()` is nil (pre-#370: `GetType()` was `SNAPSHOT_TYPE_UNSPECIFIED`). Distinct bricks, distinct failure points.)

## Save/restore asymmetry (sharpens the fix)
The RESTORE path tolerates zero durable-dir volumes — `main.go:387` (pause) and `main.go:416` (app containers) under `SNAPSHOT_SCOPE_DATA` just `cmdCreate`/`cmdStart` with no ddv requirement. Only the SAVE path requires `len(ddv) >= 1`. So a naive "make `len(ddv)==0` a no-op success" backstop would write an empty checkpoint dir that the restore side then tries to `--fs-restore-image-path` from — moving the brick to restore rather than removing it.

## Recommended fix (defense-in-depth)
1. **Primary (caller-side scope gate):** do not resolve golden snapshot scope to `Data` for a template whose containers declare no durable-dir volumes — fall back to `Full`, or reject at admission with a clear validation error (`SnapshotScope=Data requires at least one container.durableDirVolumes`). Cleanest: a CRD admission/validation check so the misconfiguration is caught before reconcile.
2. **Backstop (callee), only if paired with restore:** if `len(ddv)==0` is to be treated as success, the restore path must also short-circuit for the empty-DATA case (no `--fs-restore-image-path`), to preserve save/restore symmetry. Not recommended alone.

A TODO at the save switch already flags the DATA/FULL split as a temporary single-request workaround pending gVisor combined-snapshot support, so the caller-side gate is the lower-risk near-term fix.

## Evidence (read-only)
- `kb/substrate/health/e2e-history.jsonl`: PASS ≤ `9d57db04` → 7/7 FAIL from `37b5006`.
- Upgrade-loop Job pod logs (read-only): `demo_test.go:584` golden-Ready timeout (`WaitGoldenActor`).
- ate-controller logs (read-only): `no durable-dir volumes found for DATA snapshot`.
- Source @ HEAD b755ae73 (the HEAD behind the 06-30 e2e fires; re-verified read-only 2026-06-30): `cmd/ateom-gvisor/main.go:273` (byte-exact, unchanged from 37b5006); controller `actortemplate_controller.go:151` (wrap) / `:154-155` (internal tracking a#3210 sibling). (Line pins drifted 140/146 → 151/154-155 across the 37b5006 → fe48750 → b755ae73 advance; the SAVE-path hard-error string + its `PhaseWaitGoldenActor` context are otherwise unchanged.) Re-verified at `af1f005` (2026-07-09, post-#370): the SAVE-path hard-error drifted `main.go:273` → **`:271`** (string byte-exact); controller `:151` / `:154-155` unchanged.
````

**Page-side notes (not part of the paste body)**

- The staged draft's internal tracking preamble and trailing signature line are stripped above; the body's two sibling refs are rendered as "internal tracking a#3210" per this page's fence — **if §U1 files first, swap those for its upstream issue number** at filing time.
- The body's "7/7 red since" was the tally at draft time; the durable history reads **45 of 46 FAIL** as of 2026-07-03 — update the tally at filing time if desired.
- Line pins re-verified at `af1f005` (2026-07-09, post-#370): `main.go:273` → `:271` (string byte-exact); controller `:151` / `:154-155` hold. Re-verified again at `f1b69e9` (2026-07-09, post ObjectRef refactor): `main.go:271` string byte-exact, controller `:151` / `:154-155` hold (no line shift from the refactor). Re-verified once more at `992a3b30` (2026-07-10, "Rework `Create` endpoints to align with API style guide", ahead_by 1): `main.go:271` string byte-exact; the Create rework wraps the request payload in `Actor{Metadata: ResourceMetadata{…}}`, which shifted the controller create-path line numbers — but the pinned text is byte-identical and the staged patch below `git apply --check`s clean. Re-verified once more at `86cd169` (2026-07-10, OCI-config fix [#301](https://github.com/agent-substrate/substrate/pull/301), ahead_by 1): unchurned by #301 (OCI-build files only — `memorypullcache.go`/`cmd/atelet/oci.go`/`cmd/atelet/oci_test.go`) — `main.go:271` string byte-exact, controller pins hold, staged patch still `git apply --check` clean. **Re-verify line numbers at filing time.**
- **Tested reference patch staged (2026-07-09):** converter-side `resolveSnapshotScope` DATA→FULL downgrade + `slog.Warn` when the actor has zero durable-dir volumes — the recommended caller-side scope gate, fallback-to-Full variant. Originally re-cut onto `af1f005` (no API port needed); **current cut re-cut 2026-07-09 onto `f1b69e9` (post ObjectRef refactor) — applied clean, zero fixes**; full `controlapi` suite green; `git apply --check` clean; re-verified `git apply --check` clean at `992a3b30` (2026-07-10 — the Create-endpoint rework does not churn the converter-side scope path) and again at `86cd169` (2026-07-10, OCI-config fix #301 — unrelated OCI-build surface). Staged locally per the no-filings rule — attach as a proposed fix or offer on ask.

<a id="u3-ateredis-strict-protojson"></a>

### §U3 — ateredis strict protojson bricks ListActors across a proto field rename

**Internal tracking a#2921 · file-ready issue for agent-substrate/substrate**

**What's blocked**

- The `listactors-internal` e2e check on long-lived clusters: a proto field rename across a deploy (concretely upstream #227's `last_snapshot` → `latest_snapshot_info`) makes strict protojson reject pre-rename persisted rows → `ListActors` returns `code=Internal` on **every** call — 0-pass deterministic until rows are rewritten or the cluster recreated.
- A loop-side workaround currently holds our clusters green, so this is **forward-compat hardening** (not gating a live fire) — the upstream fix is the durable close.
- **Race RESOLVED — second brick instance LIVE at head (2026-07-09):** upstream PR [#370](https://github.com/agent-substrate/substrate/pull/370) (SnapshotType enum removal; `SnapshotInfo.type` → `reserved`) **merged 2026-07-08** (`b66b5c20`) while [#356](https://github.com/agent-substrate/substrate/pull/356) (binary protobuf encoding for ateredis rows — the structural fix) is **still open**. The #370-first = brick branch fired: any `DBActor` row persisted pre-#370 with a **non-zero** snapshot type carries `"type":"SNAPSHOT_TYPE_LOCAL|EXTERNAL"` (protojson emits non-zero enums by name), which the post-#370 strict decoder rejects (`unknown field "type"`) — population-selective, same narrow-probe trap as #227.

**Steps**

**Related upstream (dup-search 2026-07-03 — no existing report of the decode-brick; new issue is correct):** [#227](https://github.com/agent-substrate/substrate/pull/227) *(merged — the rename that first triggered it)* · [#370](https://github.com/agent-substrate/substrate/pull/370) *(**merged 2026-07-08** — `SnapshotInfo.type` → reserved: the **live** second trigger)* · [#307](https://github.com/agent-substrate/substrate/issues/307) *(open — binary-protobuf capacity change; explicitly waves off decode-compat as "hard cutover", which strengthens this ask)* · [#356](https://github.com/agent-substrate/substrate/pull/356) *(open — implements #307; the structural fix, not merged)*

#370 merged 2026-07-08 — the "filing addendum" condition is **MET**: include the addendum section (in the fence below) and cite #370 as the concrete live second trigger alongside #227.

```bash
gh issue create --repo agent-substrate/substrate \
  --title "ateredis: strict protojson.Unmarshal of persisted rows bricks ListActors across a proto field rename" \
  --body-file u3-body.md
```

**Issue body (copy-paste)**

````markdown
**What**

`cmd/ateapi/internal/store/ateredis/ateredis.go` reads persisted `DBActor` / `DBWorker`
JSON rows with a **strict** `protojson.Unmarshal(...)` (default options, no
`DiscardUnknown`). Every read site in that file — including `fetchActors`, the function
backing `ListActors` — decodes a row that may have been written by an **older** binary.

When a proto field is **renamed or removed** across a deploy, a row persisted under the
old schema carries a field the new binary's strict decoder does not recognize, so
`protojson.Unmarshal` fails `unknown field "<old>"` and the read returns `code=Internal`.
Because `fetchActors` is on the `ListActors` path, this bricks `ListActors` entirely —
not a single bad row, the whole call.

This bit us concretely after #227 (the pause/resume proto migration), which renamed
`last_snapshot` → `latest_snapshot_info`. A `DBActor` row persisted before #227 still
carried `last_snapshot`; the post-#227 api-server strict-decode-rejected it, and the
demo's core `ListActors` went **0-pass / all-fail, deterministically**.

**Why it matters**

- On **ephemeral CI** (kind) every persisted row is written by the *same* build that
  reads it, so the row schema and the decoder schema never diverge — strict decoding
  never trips, CI is green.
- On a **long-lived** cluster, rows persist across binary upgrades, so the first deploy
  that lands a field rename makes every pre-existing row undecodable. `ListActors` stays
  bricked until those rows are rewritten or the store is wiped — i.e. effectively a
  cluster recreate. It is a **deterministic brick, not a race** (no page-size or retry
  dodges it — the poisoned row is read on every call).

This is the same long-lived-vs-ephemeral skew class as `install-ate.sh`'s `ensure_crds`
get-and-skip — CI's clean-slate hides a forward/backward-compat gap that only a
persistent cluster exposes.

**Suggested fix**

Decode persisted rows with `DiscardUnknown: true` on the **read** paths, so a live row
carrying a renamed/removed field cannot brick reads. Keep **write** paths strict, so the
service still catches its own serialization bugs:

```go
// jsonUnmarshal decodes persisted (DBActor/DBWorker) JSON rows. DiscardUnknown lets
// reads tolerate fields removed/renamed in the proto across a deploy (e.g. the
// last_snapshot -> latest_snapshot_info migration in #227): a live row carrying the
// old field must not brick ListActors.
var jsonUnmarshal = protojson.UnmarshalOptions{DiscardUnknown: true}
```

then swap each persisted-row read from `protojson.Unmarshal(...)` to
`jsonUnmarshal.Unmarshal(...)`. (Marshal/write paths are untouched — they carry no
`protojson.Unmarshal(` call.) DiscardUnknown-on-read is the standard forward-compat
pattern for proto-JSON-at-rest; it is additive and idempotent.

**Repro**

1. Install ate on a long-lived cluster; create an Actor so a `DBActor` row persists.
2. Land a commit that renames a `DBActor`/`DBWorker` proto field (e.g. #227).
3. Redeploy the api-server against the same store → `ListActors` returns `code=Internal`
   (`unknown field "<old>"`) on every call until the pre-rename rows are gone.

## Filing addendum (CONDITION MET — #370 merged 2026-07-08; include this)

This is not a one-off: #370 (SnapshotType enum removal, `SnapshotInfo.type` →
`reserved`; merged `b66b5c20`) repeats the same shape — any `DBActor` row persisted
before #370 with a non-zero snapshot type carries `"type"`, which the post-#370 strict
decoder rejects, re-bricking `ListActors` on any long-lived store. #356 (binary protobuf
encoding for ateredis rows) fixes the class structurally; until it lands,
`DiscardUnknown` on the read paths is the minimal guard that makes schema evolution safe
for persisted rows.
````

**Page-side notes (not part of the paste body)**

- Re-verified at upstream `main` `f1b69e9` (2026-07-09, post ObjectRef refactor) — still unfixed, no fix drift, and the surface **grew**: the package moved to `cmd/ateapi/internal/store/ateredis/` (upstream reorg commit `b1fe163`), and the f1b69e9 Delete-returns-object refactor **added an 11th** persisted-row strict-decode site (Delete now unmarshals the current row to return it). All 11 sites are bare strict `protojson.Unmarshal` with zero `DiscardUnknown` anywhere in the pkg (`ateredis.go:140/:172/:207/:248/:341/:413/:441/:510/:556/:622/:809`; `fetchActors` at `:784` backing `ListActors` at `:673`). One more brick site = the report's blast radius widened, not narrowed. Re-verified again at `992a3b30` (2026-07-10, post "Rework `Create` endpoints"): the ateredis package is unchurned by the Create rework — all 11 strict-decode sites hold byte-exact at the **identical** line numbers, no shift. Re-verified once more at `86cd169` (2026-07-10, OCI-config fix [#301](https://github.com/agent-substrate/substrate/pull/301), ahead_by 1): the ateredis package is unchurned by #301 (OCI-build files only) — all 11 strict-decode sites hold byte-exact at the identical line numbers. Re-verify at filing time.
- The addendum's condition is **MET** (#370 merged 2026-07-08) — include it; the base body still stands alone if pasted without it.

<a id="u4-checkpoint-exit-128"></a>

### §U4 — Checkpoint of an exited/destroyed container retried forever (runsc exit 128)

**Internal tracking a#4189 · file-ready issue for agent-substrate/substrate**

**What's blocked**

- **Suspend/resume lifecycle reliability on full-plane installs**: an actor whose container exited or was destroyed wedges **permanently in SUSPENDING** — `runsc checkpoint` exit 128 can never succeed by retrying the same worker, and the controller exp-backoff retries forever. Golden actors additionally block their ActorTemplate from ever reaching Ready.
- **Three independent occurrences in 2 days** (2026-07-01/02) on a full-plane static-certs validation install: an e2e lane worker, plus two golden actors (one looped ~24h). 3/3 converged **only** via manual hosting-worker-pod recycle; none self-converged.
- Freshest of the four filings: source pinned at upstream `main` `8631ae3` (2026-07-01); citations independently co-reviewed 3/3 verbatim, and the clean suspend/resume contract was independently verified (in-RAM carryover), corroborating exit-128 as an anomalous missing-container wedge, not expected behavior.

**Related upstream (dup-search 2026-07-03):** [#50](https://github.com/agent-substrate/substrate/issues/50) *(open, SAME — adopt: its logs show the identical signature, `FetchSpec failed: loading container: file does not exist` → `runsc checkpoint: exit status 128` retried indefinitely, actor pinned STATUS_SUSPENDING)* · [#353](https://github.com/agent-substrate/substrate/pull/353) *(open PR, SAME-fix — puts the actor in terminal `CRASHED` when the runsc command fails during Suspend/Pause: exactly suggested-fix #1; omits only the recycle follow-through)* · [#292](https://github.com/agent-substrate/substrate/issues/292) *(open — CRASHED-state umbrella design; a maintainer comment already raises the retryable-vs-non-retryable runsc question)* · [#244](https://github.com/agent-substrate/substrate/issues/244) *(open — API-entry precondition validation, different defect)* · [#119](https://github.com/agent-substrate/substrate/issues/119) *(open — state-machine design; specifies the intended failure semantics this defect violates)*

**Steps**

```bash
# 1) comment our evidence on the existing issue (paste the prepared body below — it reads
#    as an evidence comment as-is; keep the source pins):
gh issue comment 50 --repo agent-substrate/substrate --body-file u4-50-comment.md

# 2) addendum on the in-flight fix PR #353 (terminal CRASHED classification — matches our
#    suggested fix #1); ask for the recycle-not-retry follow-through (our fix #2):
gh pr comment 353 --repo agent-substrate/substrate --body \
  "The terminal CRASHED classification here matches what we observe on a full-plane install: 3 independent runsc-checkpoint exit-128 wedges in 2 days, 3/3 recovered only by a manual worker recycle (evidence in #50). One follow-through worth considering: on terminal suspend failure, also release the worker assignment / re-run on a fresh worker — that is the recovery that works manually today. Happy to open a follow-up issue if preferred."
```

**Comment body for [#50](https://github.com/agent-substrate/substrate/issues/50) (copy-paste → u4-50-comment.md)**

````markdown
Repo: agent-substrate/substrate · observed at build-from-main on 2026-07-01/02; source verified present at upstream `main` 8631ae3.

## Symptom

An actor whose sandbox container has exited or been destroyed (worker restart mid-lifecycle, prior partial teardown, node event) gets wedged **permanently in SUSPENDING**. Every suspend attempt fails the same way and is retried indefinitely:

```
ERROR Reconciler error controller=actortemplate ActorTemplate=<name>
  error="while suspending golden actor: ... while checkpointing pause:
  while running `runsc checkpoint`: exit status 128"
```

`runsc checkpoint` exits 128 because the target container no longer exists in the runsc state dir — a condition that **can never self-resolve by retrying the same command against the same worker**. The controller treats it as a transient error (exp-backoff requeue), so the loop runs forever. Observed three independent times in two days on one install (a golden-actor take, a second golden-actor take on a different ActorTemplate, and a user-actor suspend); in every case the only recovery was manually deleting the hosting worker pod so the actor was re-run on a fresh worker.

## Source (main @ 8631ae3)

1. `cmd/ateom-gvisor/runsc.go` `cmdCheckpoint` (:107) — any `runsc checkpoint` failure returns a plain wrapped error; exit codes are not classified:
```go
err := cmd.Run()
if err != nil {
    return fmt.Errorf("while running `runsc checkpoint`: %w", err)
}
```
2. `cmd/ateom-gvisor/main.go` `CheckpointWorkload` FULL scope (:280-283) — wraps and propagates:
```go
if err := rcmd.cmdCheckpoint(ctx, "pause", checkpointPath); err != nil {
    return nil, fmt.Errorf("while checkpointing pause: %w", err)
}
```
3. `cmd/atecontroller/internal/controllers/actortemplate_controller.go` (:149-151) — SuspendActor error is a hard reconcile error → controller-runtime exp-backoff retry, same actor, same worker assignment, forever:
```go
resp, err := r.AteClient.SuspendActor(ctx, req)
if err != nil {
    return ctrl.Result{}, fmt.Errorf("while suspending golden actor: %w", err)
}
```

## Why this is a bug, not an operational error

- The failure is **deterministic and permanent** for the retried operation: a checkpoint of a container that no longer exists cannot succeed, no matter how many times it is retried against the same worker. Retry-with-backoff is the right posture for transient failures; here it converts a recoverable situation into a permanent wedge.
- Upstream already flags this call site as needing resilience work — `actortemplate_controller.go:142-144` (directly above the cited SuspendActor call) carries `// TODO: Need to be more resilient --- if suspendactor tells us conflict, ...`. That TODO targets the conflict case specifically; the exit-128 missing-container case is an adjacent-but-distinct hardening gap on the same suspend path.
- The system **already has the recovery primitive**: re-running the actor on a fresh worker converges cleanly every time (verified 3/3). Nothing invokes it — the actor stays bound to the broken worker and the loop never escalates.
- The wedge is user-visible and quota-relevant: the actor holds SUSPENDING state (and, for golden actors, blocks the ActorTemplate from ever reaching Ready), while the controller emits an unbounded error stream.

## Suggested fix (one of, or layered)

1. **Classify checkpoint-target-missing as terminal** at the ateom/atelet layer (runsc exit 128 + container absent from the runsc state dir) and surface it as a distinct, non-retryable error code to the api-server/controller.
2. **On terminal suspend failure, recycle rather than retry**: release the actor's worker assignment (or delete/recreate the sandbox on a fresh worker) so the existing re-run path converges — the exact remediation that works manually today.
3. At minimum, **cap the retry** and surface a distinct condition/Event so operators see "checkpoint target gone — actor needs re-placement" instead of an infinite identical error stream.

## Repro

(Synthetic — the observed occurrences arose organically from worker-lifecycle container loss, e.g. a worker restart mid-lifecycle; step 2 reproduces the same container-absent condition deterministically.)

1. Run an actor (FULL snapshot scope) on a worker; let it reach Running.
2. Destroy the actor's container out from under the plane (e.g. restart the worker pod's container, or `runsc delete` the sandbox on the worker) without going through DeleteActor.
3. Call SuspendActor (or let a golden-actor take reach its suspend step).
4. Observe: `runsc checkpoint` exit 128 on every attempt, controller exp-backoff retry forever, actor pinned SUSPENDING. Deleting the hosting worker pod (fresh worker, actor re-run) converges immediately.
````

**Page-side notes (not part of the paste body)**

- Source citations pinned at `main` `8631ae3` (2026-07-01) — `runsc.go:107-135`, `main.go:280-283`, `actortemplate_controller.go:149-151`. Re-verify line numbers at posting time.
- The body was drafted as a new-issue filing; posted as a #50 comment it reads as independent-reproduction evidence — prepend a one-liner like "Independent reproduction + source pins (3 occurrences / 2 days):" if desired.

<a id="u5-nonatomic-manifest-publish"></a>

### §U5 — Golden snapshot registered with no manifest.json (nonatomic publish)

**Internal tracking a#4478 · file-ready issue for agent-substrate/substrate**

**What's blocked**

- **The warm-resume path, permanently, once triggered**: a suspend that crashes mid-checkpoint-upload can end with an incomplete snapshot registered as `LatestSnapshotInfo` and pinned as the ActorTemplate's `Status.GoldenSnapshot` — after which **every warm resume from that golden hard-fails** ("while fetching snapshot manifest … object doesn't exist") with no self-heal.
- This is the **publish-side sibling of U1/U2**: it explains how a broken golden got *registered* in the first place, while U1/U2 brick the only remediation (regenerating the golden) — so the unrestorable state is permanent until both sides are fixed. Operational remediation on our cluster remains gated on U1.
- Observed live: a golden prefix created 2026-06-12 with all payload objects present and no `manifest.json` (versioned listing confirms never-written, not written-then-lost), degrading warm resume on every fire since.

**Steps**

```bash
# file as a new issue (dup-search 2026-07-03 + 2026-07-09: no existing report of the
# registration hole; #353 covers consumption-side legibility only):
gh issue create --repo agent-substrate/substrate \
  --title "Golden snapshot can be registered with no manifest.json: dangling-worker suspend skip + unverified InProgressSnapshot promote" \
  --body-file u5-nonatomic-manifest-publish.md
```

**Issue body (copy-paste → u5-nonatomic-manifest-publish.md)**

````markdown
Repo: agent-substrate/substrate · source pins verified at upstream `main` = d8f562d (2026-07-06).

## Summary

A suspend that crashes mid-checkpoint-upload can still end with the incomplete snapshot
registered as the actor's `LatestSnapshotInfo` — and then pinned as the ActorTemplate's
`Status.GoldenSnapshot`. Every subsequent warm resume from that golden hard-fails with:

```
while fetching snapshot manifest: ... storage: object doesn't exist
```

(the Restore-side error at `cmd/atelet/main.go:476-480`).

The publish is guarded at upload time (manifest written last) but **registration is never
gated on the manifest actually existing**, and one retry path converts a mid-upload crash
into a successful suspend.

## Observed evidence

On a long-lived demo cluster we found a golden snapshot prefix (created 2026-06-12) containing:

- `checkpoint.img.zstd`, `pages.img.zstd`, `pages_meta.img.zstd` — payload objects present
- **no `manifest.json`** — and, per a versioned bucket listing, **no deleted version either**:
  the manifest was never written, not written-then-lost
- the same bucket holds 763 `manifest.json` objects for other snapshots — manifest presence
  is clearly the norm

The ActorTemplate's `Status.GoldenSnapshot` points at that prefix, so every warm resume
(`WorkerPool` scale-up from golden) fails deterministically with the fetch error above.
There is no self-heal: nothing invalidates or regenerates the golden.

## Mechanism (source pins at d8f562d)

The suspend workflow (`cmd/ateapi/internal/controlapi/workflow_suspend.go`) is a 3-step
state machine, and the composite hole is:

1. **`PrepareSuspend` registers the snapshot location before any byte is uploaded**
   (lines 89-91): it computes `snapshotID`, sets
   `state.Actor.InProgressSnapshot = <location>/<actorID>/<snapshotID>`, and persists via
   `UpdateActor` — all prior to the checkpoint upload.

2. **`CallAteletSuspendStep.Execute` treats a vanished worker as success** (lines 112-114):

   ```go
   if errors.Is(err, ErrWorkerPodNotFound) {
       slog.Warn("Skipping suspend for dangling worker pod", ...)
       return nil
   }
   ```

   The intended case is "worker already gone, nothing to checkpoint". But if the worker
   dies *mid-checkpoint-upload* (after the `Checkpoint` RPC at line 141 was dispatched but
   before it returned), the workflow retry finds the pod gone and this branch returns
   `nil` — laundering an incomplete upload into a successful step.

3. **`FinalizeSuspendedStep` promotes unconditionally** (lines 198-207): on
   `InProgressSnapshot != ""` it builds `LatestSnapshotInfo` (external snapshot,
   `SnapshotUriPrefix = InProgressSnapshot`), clears `InProgressSnapshot`, sets
   STATUS_SUSPENDED, and persists (line 213). **No check that `manifest.json` — or any
   object at all — exists at the prefix.**

Upload-side, `uploadExternalCheckpoint` (`cmd/atelet/main.go:415`) uploads the payload
files **in parallel** (errgroup, `SendLocalFileToGCSWithZstd` at line 425) and the
manifest **last** (`SendBytesToGCS` at line 441, commented "written last, so a Restore on
any node is self-describing"). So a crash in the window between payload completion and
manifest write leaves exactly the observed shape: payloads present, manifest never written.

Controller-side, `PhaseWaitGoldenActor`
(`cmd/atecontroller/internal/controllers/actortemplate_controller.go`) gates only on
snapshot **type** (line 155) before pinning
`at.Status.GoldenSnapshot = ...SnapshotUriPrefix` (line 159) — the broken registration
propagates to the template with no content check.

End-to-end failure path:

```
payloads upload OK (parallel) → atelet/worker dies before manifest write
→ Checkpoint RPC never succeeds → workflow retries Execute
→ worker pod gone → ErrWorkerPodNotFound skip returns nil (success)
→ FinalizeSuspended promotes InProgressSnapshot → LatestSnapshotInfo
→ ActorTemplate controller pins it as Status.GoldenSnapshot (type-only gate)
→ every warm resume: "while fetching snapshot manifest ... object doesn't exist"
```

## Timing note — partially mitigated, hole remains at HEAD

The explicit manifest-written-last ordering landed in 9360791 (2026-06-25, "atelet:
manifest-driven micro-VM snapshots with sparse, overlapped transfer") — *after* our broken
2026-06-12 snapshot was created, so the historical instance may have had a different
upload-side ordering. But the registration hole is orthogonal to upload ordering and is
present at HEAD: manifest-last narrows the crash window, while the dangling-worker skip +
unverified promote still convert any crash inside that window into a registered-but-
unrestorable golden.

**Relation to PR #353 (CRASHED-actor detection, merged 2026-07-08)** — #353 improves the
*consumption-side legibility* of this failure: a golden restore that fails on an
invalid-sandbox-asset error (e.g. the missing manifest) will mark the actor
`STATUS_CRASHED` via `maybeCrashActor` instead of surfacing only a workflow error. That
is welcome, but it does not close this hole:

- registration is still unverified — `FinalizeSuspendedStep` still promotes
  `InProgressSnapshot` with no manifest check, and the `ErrWorkerPodNotFound` skip in
  `CallAteletSuspendStep` (the retry path that launders a mid-upload crash into success)
  is untouched;
- the ActorTemplate's `Status.GoldenSnapshot` is never invalidated, so every *new* actor
  scaled up from the pool restores from the same broken golden and crashes in turn —
  the failure becomes per-actor-legible but the template-level trap remains permanent.

So fix (1) below (verify-before-promote) is still the missing piece after #353.

## Suggested fixes

The invariant worth enforcing: **a snapshot's registration and its manifest must be one
atomic step — never "register now, verify never".**

1. **`FinalizeSuspendedStep`: verify before promote.** Before promoting
   `InProgressSnapshot` → `LatestSnapshotInfo`, stat `manifest.json` at the prefix (one
   small GCS read). Missing manifest ⇒ fail the step (retry / surface), don't promote.
   This is the single choke point — it closes the hole regardless of which upstream path
   produced the incomplete upload.

2. **`CallAteletSuspendStep`: don't return success on vanish with an unverified upload.**
   `ErrWorkerPodNotFound` should distinguish "already checkpointed" (manifest present)
   from "vanished mid-checkpoint" (manifest absent). In the latter case, clear
   `InProgressSnapshot` and fail/abort rather than returning `nil`.

3. *(Optional, defense-in-depth)* **ActorTemplate controller: manifest-presence gate**
   next to the existing type check at `actortemplate_controller.go:155`, so a bad
   registration can't become the template's golden.

Fix (1) alone is sufficient to prevent the unrestorable-golden state; (2) improves the
failure's legibility; (3) contains blast radius if a future path bypasses the workflow.
````

**Page-side notes (not part of the paste body)**

- Body pins originally at `main` `d8f562d` (2026-07-06); **re-verified 2026-07-09 at `de5d1d9` (post-#370 oneof refactor + post-[#411](https://github.com/agent-substrate/substrate/pull/411) suspend-path rewrite)** — current pins: `workflow_suspend.go:90` (`InProgressSnapshot` set), `:121-122` (`ErrWorkerPodNotFound` dangling-worker skip), `:144` (in-progress `SnapshotUriPrefix` use), `:165` (`FinalizeSuspendedStep.Execute`), `:203-211` (unverified promote block); `atelet/main.go:415/:425/:441/:476-480` and `actortemplate_controller.go:155/:159` unchanged (#411 confined to `cmd/ateapi/internal/controlapi/`). **Re-verified again 2026-07-09 at `f1b69e9` (post ObjectRef refactor):** `workflow_suspend.go` is unchurned by the refactor, so all suspend-path pins hold byte-exact; `actortemplate_controller.go` took only a 3-line call-site change with no line shift, so `:155/:159` still hold. **Re-verified once more 2026-07-10 at `992a3b30` ("Rework `Create` endpoints to align with API style guide", ahead_by 1):** `workflow_suspend.go` is still unchurned — every suspend-path pin holds byte-exact; the Create rework wraps the create-request payload in `Actor{Metadata: ResourceMetadata{…}}`, which shifted `actortemplate_controller.go`'s create-path line numbers, so the snapshot-type gate + publish that were at `:155/:159` are now at `:221`/`:222` (gate) / `:226` (publish) — text byte-identical, line-shifted only. **Re-verified once more 2026-07-10 at `86cd169` (OCI-config fix [#301](https://github.com/agent-substrate/substrate/pull/301), ahead_by 1):** unchurned by #301 (OCI-build files only — `memorypullcache.go`/`cmd/atelet/oci.go`/`cmd/atelet/oci_test.go`) — `workflow_suspend.go` suspend-path pins byte-exact, controller `:221`/`:222`/`:226` hold. The body above already says "external snapshot" rather than the removed enum name; **re-verify pins once more at filing time.**
- The #353 relation section is current as of 2026-07-08 (merged); the two residual-gap bullets were re-verified against the merged `crash.go` — worker-release-on-terminal-failure remains an in-code TODO upstream tracks under [#119](https://github.com/agent-substrate/substrate/issues/119).
- **Tested reference patch staged (re-cut 2026-07-09 onto `de5d1d9`, post-[#411](https://github.com/agent-substrate/substrate/pull/411)):** implements fixes (1) + (2) — GCS+S3 `SnapshotVerifier` (`ManifestExists(ctx, prefix)`), FinalizeSuspendedStep verify-before-promote gate (missing manifest ⇒ fail step, never promote), and CallAteletSuspendStep `ErrWorkerPodNotFound` disambiguation (no snapshot ⇒ safe skip; manifest present ⇒ completed, skip; manifest absent ⇒ clear `InProgressSnapshot` + fail; verifier error ⇒ fail-closed, keep registration for retry). #411 rewrote this exact suspend path (atespace-aware lock keys + a FinalizeSuspendedStep worker-ownership check, `Assignment.Actor.Atespace/Name == input`); the de5d1d9 cut **merges cleanly WITH it** — keeps #411's ownership check + lock keys and layers the verifier, promote gate, and disambiguation on top. Test file is the **union**: #411's miniredis `TestFinalizeSuspendedStep_ReleasesOnlyOwnWorker` (verifier-injected so it doesn't nil-deref under our gate) + our 5 verify tests ported to the `ResourceMetadata`/`SuspendInput.ActorName` shape (the promote-test worker `ActorRef` carries `Atespace` so #411's ownership check permits release). Full `controlapi` suite green (19.4s incl. envtest); `git diff origin/main` clean @ `de5d1d9`. **CURRENT CUT: re-cut 2026-07-09 onto `f1b69e9` (post ObjectRef refactor)** — one-line adaptation: the patch's own promote-test worker ref `ActorRef`→`ObjectRef` in `workflow_suspend_test.go`; all 7 files otherwise applied clean; full `ateapi` suite green 16.8s; re-verified `git apply --check` clean at `992a3b30` (2026-07-10 — the Create-endpoint rework does not conflict with the suspend-path verifier cut) and again at `86cd169` (2026-07-10, OCI-config fix #301 — unrelated OCI-build surface). Fix (3) deliberately omitted — single-choke-point patch stays minimal. Staged locally per the no-filings rule; attach as a proposed fix or offer on ask. (Prior cuts — `de5d1d9`, `af1f005`, `069ba08`, original vs `d8f562d` — retained as history.)

---

## Also staged upstream (improvements, not metric blockers)

Prepared and sign-ready, but holding no benchmark metric — file/land opportunistically:

- **Internal tracking a#3957** — sandbox router gVisor toleration (`sandbox_router.yaml`); patch staged sign-ready alongside the §S artifacts (same pod retrieval pattern as the Steps blocks above).
- **Internal tracking a#4064** — e2e-harness explicit-timeout + VCT-reject pre-wait (two patches; compile re-verified 2026-07-03).
- **Internal tracking a#1347** — PodTemplate immutability; patch git-tracked internally under `kb/sandbox/references/patches/`. **REBASE-REQUIRED as of tip `9239c0f`** (see the Tip-advance note up top: the shared-`SandboxBlueprint` extraction makes the original shared-field CEL marker over-broad; per-type rebase — Sandbox-side immutable, template-side mutable — in progress). Do not sign until the rebased patch re-checks clean.
- **agent-substrate/substrate status-discipline set** — [#1069](https://github.com/agent-substrate/substrate/issues/1069) (observedGeneration), [#1127](https://github.com/agent-substrate/substrate/issues/1127) / [#1128](https://github.com/agent-substrate/substrate/issues/1128) / [#1134](https://github.com/agent-substrate/substrate/issues/1134) (WorkerPool/ActorTemplate status + Events + print columns); clone branches staged; charter-gated.
- **Excluded:** internal tracking a#2082 (netpol template-ref-hash) — superseded by in-flight upstream [kubernetes-sigs/agent-sandbox#793](https://github.com/kubernetes-sigs/agent-sandbox/issues/793) / [#1067](https://github.com/kubernetes-sigs/agent-sandbox/issues/1067); do not sign.
