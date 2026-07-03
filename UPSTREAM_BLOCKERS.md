# Upstream blockers — file-ready

This page inventories every upstream defect currently holding an honest-bench metric (or the trust in one): **what** is blocked, the **diagnosis** already done, and the maintainer-facing **action** prepared to copy-paste quality — a file-ready issue body, a ready comment on an existing upstream issue, or a DCO-ready patch with the exact retrieval commands. Unlike [README.md](README.md) and [WORK_IN_PROGRESS.md](WORK_IN_PROGRESS.md), which are machine-rendered from harness runs, this page is **hand-maintained** — owned by the s-dev crew and refreshed whenever a blocker moves (filed, merged, superseded, rebased). Absent cells on the rendered pages link to [WORK_IN_PROGRESS.md](WORK_IN_PROGRESS.md) for their reason class; this page carries the upstream half of that story.

House fence (this page ships verbatim in a public repo): `AlexBulankou/a` is a **private** repo, so internal issue refs render as plain "internal tracking a#NNNN" prose — never a link, never a bare `#NNNN`. Public honest-bench issues render as normal hb#NNN links. Upstream projects are named in plain English, with real links to their public issues/PRs.

_Last hand-refresh: 2026-07-03._

---

## Sandbox upstream blockers

Target project: [kubernetes-sigs/agent-sandbox](https://github.com/kubernetes-sigs/agent-sandbox). PR-type rows below have a locally compiled, test-verified patch staged; the upstream project is filed under the maintainer's own identity (no bot pushes), so each Steps block gives the exact retrieve → `git am --signoff` → push sequence.

| # | What | Action | Steps |
|---|---|---|---|
| S1 | `Suspended=True` never clears on resume → the entire **gVisor × Resume-from-suspend** README row (all 5 cells) renders [`pending (upstream-blocked)`](WORK_IN_PROGRESS.md#upstream-blocked); prior 92.8% resume exec-success (N=1376) retracted pending this. Internal tracking a#4099. | Prepared upstream **PR** — `fix(controller): clear stale Suspended condition on resume` (15-line fix + 94-line test, compile/test/falsifiability verified; optional Part-2 perf companion ships separately). | [→ §S1 file-ready material](#s1-resume-suspended-condition) |
| S2 | True-TTFE is **null-by-construction** everywhere: the startup-latency histogram's anchor annotation has no production writer upstream, so SLO-sweep pareto reads `[]` and `ready_per_s` reads null every fire ([hb#174](https://github.com/AlexBulankou/honest-bench/issues/174)). Internal tracking a#3975. | **DECISION FIRST** — a 4-file/+228 mutating-webhook stamper PR is staged, but filing it reverses the 06-29 "skip upstream conversations, rush toward honest-bench results" call; [hb#175](https://github.com/AlexBulankou/honest-bench/issues/175)'s literal-TTFE upper bound is the current honest interim. | [→ §S2 file-ready material](#s2-ttfe-webhook-stamper) |
| S3 | Snapshot-backed sandbox goes `Ready=True` before restore is verified → blank pods served as "restored" (7.45% / 5.24% real-flap rates on the two paths); restore-success confidence is unassertable for every snapshot-restore-backed number. Internal tracking a#2614. | Prepared **comment** on the open upstream design issue [kubernetes-sigs/agent-sandbox#952](https://github.com/kubernetes-sigs/agent-sandbox/issues/952) — no patch by design; the ask is a design decision (restore-verified readiness gate before `Ready=True`). | [→ §S3 file-ready material](#s3-restore-readiness-handshake) |
| S4 | SandboxClaim adoption-completion bounces through the informer cache with a non-nil error → **exponential-backoff amplifier** (5ms·(2^K − 1)) inflating exactly the concurrent warm-claim tails the README reports. Layer-2 only — Layer-1 resolved live by upstream [#975](https://github.com/kubernetes-sigs/agent-sandbox/issues/975). Internal tracking a#3641. | Prepared upstream **PR** — sentinel `errAdoptionTriggeredRetry` → bounded 1 ms nil-error requeue; preserves the tested duplicate-adoption protection. Rebase if upstream [#1072](https://github.com/kubernetes-sigs/agent-sandbox/issues/1072) merges first. | [→ §S4 file-ready material](#s4-adoption-completion-cache-race) |

<a id="s1-resume-suspended-condition"></a>

### §S1 — Suspended condition never clears on resume

**Internal tracking a#4099 · prepared upstream PR · `controllers/sandbox_controller.go`**

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
# 1) pull the patch off the pod
kubectl exec a4-a4s2-0 -n a4 -c agent -- \
  cat /home/agent/work/.a4/upstream-drafts/resume-clears-suspended-condition.patch \
  > resume-clears-suspended-condition.patch

# 2) apply on a fresh checkout of your fork of kubernetes-sigs/agent-sandbox
git clone git@github.com:<your-fork>/agent-sandbox.git && cd agent-sandbox
git remote add upstream https://github.com/kubernetes-sigs/agent-sandbox.git
git fetch upstream main
git checkout -b resume-clears-suspended-condition upstream/main
git am --signoff ../resume-clears-suspended-condition.patch   # adds your DCO Signed-off-by

# 3) push to your fork, open the PR against kubernetes-sigs/agent-sandbox main
git push -u origin resume-clears-suspended-condition
```

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

**Action — DECISION FIRST**

Filing this reverses the 2026-06-29 "skip upstream conversations, rush toward honest-bench results" call. [hb#175](https://github.com/AlexBulankou/honest-bench/issues/175)'s literal-TTFE upper bound is the current honest interim, so nothing is dishonestly rendered while this stays unfiled — the question is whether true-TTFE is worth re-opening the upstream conversation. **File only after re-deciding.** Additionally: the staged patch base is **STALE** (upstream HEAD `985d1dd`, 2026-06-29) vs the current tip `0be472b` — run `git apply --check` and rebuild before signing (unlike the §S1/§S4 batch, this one was **not** in the 2026-07-03 apply-clean re-sweep).

**Prepared artifact**

- Full patch (4 files / +228: new `extensions/controllers/sandboxclaim_webhook.go` + test, `cmd/agent-sandbox-controller/main.go`, `cmd/agent-sandbox-controller/tls.go`) + paste-ready PR body, both staged on the s-dev pod (paths in the Steps block).
- Compile-verified in-pod against `985d1dd`: `go build` exit 0, `go vet` exit 0, 3/3 defaulter unit tests PASS.

**Steps** (only after re-deciding the 06-29 call)

```bash
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

**Internal tracking a#3641 (Layer-2 only — Layer-1 resolved live by upstream [#975](https://github.com/kubernetes-sigs/agent-sandbox/issues/975)) · prepared upstream PR · `extensions/controllers/sandboxclaim_controller.go` + test**

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
- **Rebase-if:** upstream PR [#1072](https://github.com/kubernetes-sigs/agent-sandbox/issues/1072) ("tolerate AlreadyExists in SandboxClaim createSandbox", open — same file, different facet; both needed, neither moot) merges first.

**Steps**

```bash
# 1) pull the patch off the pod
kubectl exec a4-a4s2-0 -n a4 -c agent -- \
  cat /home/agent/work/.a4/upstream-drafts/3641-adoption-completion-cache-race.patch \
  > 3641-adoption-completion-cache-race.patch

# 2) fresh checkout of your fork; rebase first if upstream #1072 merged (same file)
git fetch upstream main
git checkout -b adoption-completion-bounded-requeue upstream/main
git am --signoff ../3641-adoption-completion-cache-race.patch

# 3) check the patch's commit subject before pushing — if it carries a parenthesized
#    internal issue-number suffix (internal tracking a#3641), drop it: that number is
#    meaningless upstream
git commit --amend   # if needed
git push -u origin adoption-completion-bounded-requeue
```

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

---

## Substrate upstream blockers

Target project: [agent-substrate/substrate](https://github.com/agent-substrate/substrate). All four rows are **issue-type** — the substrate upstream is maintainer-driven, so these file as issues with suggested fixes (no prepared branches; the one near-PR fix, U1's requeue patch, is locally validated internally but its behavioral proof needs fork-push capability — internal tracking a#2691). One sitting files all four; suggested order: **U2 → U1 → U3 → U4** (new regression with active e2e red first, then the pre-existing keystone, then hardening, then the wedge).

| # | What | Action | Steps |
|---|---|---|---|
| U1 | Golden-actor reconcile **hard-errors on transient `SNAPSHOT_TYPE_UNSPECIFIED`** instead of requeueing → the activation-latency SLA store has **zero clean records ever** (metric honestly PENDING by construction); compounds with the manifest-migration stranding of pre-migration goldens. Internal tracking a#3210. | File-ready **issue** with suggested fix (requeue-on-UNSPECIFIED, or stamp EXTERNAL before the gate). | [→ §U1 file-ready text](#u1-snapshot-type-unspecified) |
| U2 | DATA-scope golden snapshot **hard-fails on zero durable-dir volumes** (regression in upstream #295) → golden-snapshot e2e leg deterministically RED since 2026-06-27 (45 of 46 fires FAIL as of 07-03). Internal tracking a#3842. | File-ready **issue** — recommends a caller-side scope gate (admission validation) as the primary fix; explains why a naive callee no-op just moves the brick to restore. | [→ §U2 file-ready text](#u2-data-snapshot-zero-ddv) |
| U3 | Strict `protojson.Unmarshal` of persisted rows **bricks `ListActors`** across any proto field rename on a long-lived cluster; imminent second trigger: upstream [#370](https://github.com/agent-substrate/substrate/issues/370) reserves `SnapshotInfo.type`. Internal tracking a#2921. | File-ready **issue** — proposes `DiscardUnknown` on read paths; carries a paste-ready addendum for the post-#370 case ([#356](https://github.com/agent-substrate/substrate/issues/356) is the structural fix; merge order decides). | [→ §U3 file-ready text](#u3-ateredis-strict-protojson) |
| U4 | `runsc checkpoint` against an exited/destroyed container **retried forever** (exit 128 → permanent SUSPENDING wedge); 3 independent occurrences in 2 days, 3/3 recovered only by manual worker recycle. Internal tracking a#4189. | File-ready **issue** — proposes terminal-classification of the checkpoint-target-missing case + recycle-not-retry (the recovery primitive already exists, nothing invokes it). | [→ §U4 file-ready text](#u4-checkpoint-exit-128) |

<a id="u1-snapshot-type-unspecified"></a>

### §U1 — Golden-actor reconcile hard-errors on SNAPSHOT_TYPE_UNSPECIFIED

**Internal tracking a#3210 · file-ready issue for agent-substrate/substrate**

**What's blocked**

- **Activation-latency SLA** (internal tracking a#2998): the daily golden-actor probe has **never** produced a clean first-attempt Ready — the durable latency store has zero records (verified 2026-07-03), so the substrate health report's golden-actor activation-latency check stays honestly PENDING **by construction**.
- The snapshot-correctness payload (internal tracking a#3637) rides the same golden-actor path this bug bricks.
- **Impact widened 2026-07-02:** the snapshot `manifest.json` format migration stranded pre-migration goldens (unresumable, no backfill), and the only remediation — a fresh golden re-commit — is exactly the path this bug bricks. The two defects compound.
- Pre-existing brick (not a regression). A requeue-on-UNSPECIFIED fix is locally patch-validated internally (applies clean, no new imports, behaviorally sound against the phase machine), but behavioral proof needs a cluster build+run via fork-push capability (internal tracking a#2691) — so today this files as an **issue with a suggested fix**, not a PR.

**Steps**

Copy the title + fenced body below into a new issue at agent-substrate/substrate:

```bash
gh issue create --repo agent-substrate/substrate \
  --title "golden-actor reconcile hard-errors on SNAPSHOT_TYPE_UNSPECIFIED instead of requeueing (transient state treated as fatal)" \
  --body-file u1-body.md
```

**Issue body (copy-paste)**

````markdown
Repo: agent-substrate/substrate · first observed on build-from-main sha bbafda0d; STILL PRESENT on the current build sha 2304dfe1 (controller image `…@sha256:2304dfe1…`) — unfixed across multiple advancing builds (substrate-demo-cluster, GKE alpha). Source line re-verified present (now `actortemplate_controller.go:154-155`) at upstream `main` b755ae73 — the latest HEAD behind the 2026-06-30 e2e fires — as of 2026-06-30 (read-only checkout). **The brick is exercised deterministically by the daily golden actor-probe** (`reconcile-probe-actor`): the durable in-repo `actor-probe-latency-history.jsonl` store has **zero clean records** — the gVisor golden-actor take has never once produced a clean first-attempt Ready, i.e. the take-gate has been failing on this `UNSPECIFIED` hard-error for its entire recorded history. This is a **pre-existing** brick, not a recent regression. The ate-controller log carries the matching `unexpected snapshot type for golden actor: SNAPSHOT_TYPE_UNSPECIFIED` on every probe reconcile. **Scope note — this is DISTINCT from the 2026-06-27 e2e-suite reds.** The six 37b5006 e2e `TestActorLifecycle` fires (03:17Z onward) fail on a *different, co-occurring* brick — the DATA-scope golden-suspend hard-error `no durable-dir volumes found for DATA snapshot` (`cmd/ateom-gvisor/main.go:273`), which fires at `actortemplate_controller.go:151` (`while suspending golden actor: %w`), one step *before* this take-gate at lines 154-155 ever runs. That second brick is NEW at 37b5006 (the demo `counter` declares zero durable-dir volumes and was PASSING pre-37b5006) and is filed separately (see the companion zero-durable-dir DATA-snapshot draft). Both are real and both should be filed; the e2e suite happens to surface the suspend-path brick first because the suspend fails before the take-gate is reached.

## Symptom
An ActorTemplate that declares `spec.snapshotsConfig.location: gs://…` (external/GCS golden snapshot) never reaches `status.phase=Ready`. `ate-controller` errors on every reconcile (exp-backoff, indefinitely):

```
ERROR Reconciler error controller=actortemplate ActorTemplate=<name>
  error="unexpected snapshot type for golden actor: SNAPSHOT_TYPE_UNSPECIFIED"
```

The controller hard-error is the invariant across builds (observed on both bbafda0d and the current 2304dfe1). The `status`-field vantage varies by build: on the earlier bbafda0d build `status.goldenActorID` was assigned but the actor never went Ready; on the current 2304dfe1 build a fresh cold-take stalls at `WaitGoldenActor` with `goldenActorID` never assigned this run. Either way the actor never reaches Ready — the reconcile hard-error on the transient `UNSPECIFIED` snapshot type is the root cause, not the precise status-field state.

## Source
`cmd/atecontroller/internal/controllers/actortemplate_controller.go`:
```go
if resp.GetActor().GetLatestSnapshotInfo().GetType() != ateapipb.SnapshotType_SNAPSHOT_TYPE_EXTERNAL {
    return ctrl.Result{}, fmt.Errorf("unexpected snapshot type for golden actor: %v",
        resp.GetActor().GetLatestSnapshotInfo().GetType())
}
```
`SnapshotType` (pkg/proto/ateapipb/ateapi.proto): `UNSPECIFIED=0`, `LOCAL`, `EXTERNAL`. The golden-actor reconcile requires `EXTERNAL`; the actor's `LatestSnapshotInfo` is the zero value (`UNSPECIFIED`) because the external golden snapshot has not been taken-and-typed yet.

## Why this is a bug, not a config error
- `UNSPECIFIED` = "golden snapshot not yet externally committed", a **transient** state — but the reconcile returns a hard `error`, not `ctrl.Result{Requeue:true}` / a wait condition. The ActorTemplate is wedged permanently on a state that should self-resolve once the golden snapshot lands as EXTERNAL.
- A plain actor (no gVisor checkpoint) with the same `snapshotsConfig` shape reaches Ready fine — so the gap is specific to the external/checkpoint golden-snapshot take path either not stamping `Type=EXTERNAL` or not completing before the reconcile gates on it.
- This is a strict proto-enum write/read disagreement: the write path and the read path disagree on whether the field is populated, and the reader treats the unpopulated zero value as fatal rather than not-yet-ready.

## Suggested fix (one of)
1. Treat `UNSPECIFIED` as not-ready → requeue with backoff (wait for the golden snapshot to commit) rather than erroring.
2. Ensure the golden-snapshot take path stamps `SnapshotType_SNAPSHOT_TYPE_EXTERNAL` before the actor is reported back to the reconcile.

## Repro
Apply an ActorTemplate with `snapshotsConfig.location: gs://…` whose golden snapshot must be taken externally; watch `ate-controller` logs + `status.phase` (never Ready).

## Impact amplifier — compounds with the snapshot manifest.json format migration (2026-07-02 evidence)
The snapshot layout migrated to a manifest-based format (observed between 06-12 and 06-28 in our bucket): every fresh snapshot dir now carries 4 objects including a `manifest.json` that pins the runsc build (e.g. `gs://gvisor/releases/release/20260622/x86_64/runsc` + sha256), while a pre-migration golden dir has only the 3 image objects and **no manifest.json**. The current resume path hard-requires the manifest — resuming from a pre-migration golden fails with `storage: object doesn't exist` on `manifest.json`. Consequences:

- **Pre-migration goldens are silently unresumable.** No migration/backfill path shipped with the format change; an ActorTemplate whose recorded goldenSnapshot predates the migration stays Ready-looking but structurally cannot resume.
- **The only remediation is a fresh golden re-commit — which is exactly the path this bug bricks.** A new golden commit hits the `SNAPSHOT_TYPE_UNSPECIFIED` hard-error above, so operators of pre-migration templates have **no working path back to a resumable golden** until this is fixed. The two defects compound: the migration strands old goldens, and this bug blocks minting new ones.
- Synthesizing a manifest for an old golden is unsound by design — the manifest is a restore-compatibility record pinning the runsc build the snapshot was taken under, which is unknown for pre-migration dirs.
````

**Page-side notes (not part of the paste body)**

- Source pins re-verified at upstream `main` `b755ae73` (2026-06-30): the take-gate moved lines 145-146 → 154-155 across the 37b5006 → b755ae73 advance. **Re-verify line numbers at filing time.**
- The suggested-fix hunk (requeue-on-UNSPECIFIED) is locally patch-validated internally — applies clean, well-formed Go, no new imports; phase not advanced on the new branch so a requeue self-resolves once the golden commits EXTERNAL. Behavioral proof (the daily golden take going clean) requires a cluster build+run; the repo has no pure unit test on this reconcile gate.
- The body's "filed separately" companion is §U2 — if U2 files first, add its upstream issue number where the body says "see the companion zero-durable-dir DATA-snapshot draft".

<a id="u2-data-snapshot-zero-ddv"></a>

### §U2 — DATA-scope golden snapshot hard-fails on zero durable-dir volumes

**Internal tracking a#3842 · file-ready issue for agent-substrate/substrate**

**What's blocked**

- The substrate golden DATA-snapshot e2e leg: `TestActorLifecycle`/`TestDurableDirLifecycle` deterministically RED since 2026-06-27 03:17Z — the first fire to build upstream 37b5006 ([#295](https://github.com/agent-substrate/substrate/issues/295), DurableDir support).
- Tally in the durable e2e history as of 2026-07-03: **45 FAILs of 46 fires** since onset, all `TestActorLifecycle` assertion-class.
- Reds the substrate health report's upgrade-loop section every 3h fire; **deliberately not re-pinned** — the red e2e IS the signal that #295 needs an upstream fix (a re-pin would mask the regression).
- Distinct from §U1: this suspend-path brick (controller line 151) fires **before** U1's take-gate (lines 154-155) is reached — **both** fixes are needed for a clean e2e + clean actor-probe.

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
- (Contrast internal tracking a#3210, which fails one step later at **lines 154-155** (HEAD b755ae73; type-gate `if` at 154 → `return` at 155; was line 146 at 37b5006) — suspend *succeeds* but `LatestSnapshotInfo().GetType()` is `SNAPSHOT_TYPE_UNSPECIFIED`. Distinct bricks, distinct failure points.)

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
- Source @ HEAD b755ae73 (the HEAD behind the 06-30 e2e fires; re-verified read-only 2026-06-30): `cmd/ateom-gvisor/main.go:273` (byte-exact, unchanged from 37b5006); controller `actortemplate_controller.go:151` (wrap) / `:154-155` (internal tracking a#3210 sibling). (Line pins drifted 140/146 → 151/154-155 across the 37b5006 → fe48750 → b755ae73 advance; the SAVE-path hard-error string + its `PhaseWaitGoldenActor` context are otherwise unchanged.)
````

**Page-side notes (not part of the paste body)**

- The staged draft's internal tracking preamble and trailing signature line are stripped above; the body's two sibling refs are rendered as "internal tracking a#3210" per this page's fence — **if §U1 files first, swap those for its upstream issue number** at filing time.
- The body's "7/7 red since" was the tally at draft time; the durable history reads **45 of 46 FAIL** as of 2026-07-03 — update the tally at filing time if desired.
- Line pins re-verified at `b755ae73` (2026-06-30); re-verify at filing time.

<a id="u3-ateredis-strict-protojson"></a>

### §U3 — ateredis strict protojson bricks ListActors across a proto field rename

**Internal tracking a#2921 · file-ready issue for agent-substrate/substrate**

**What's blocked**

- The `listactors-internal` e2e check on long-lived clusters: a proto field rename across a deploy (concretely upstream #227's `last_snapshot` → `latest_snapshot_info`) makes strict protojson reject pre-rename persisted rows → `ListActors` returns `code=Internal` on **every** call — 0-pass deterministic until rows are rewritten or the cluster recreated.
- A loop-side workaround currently holds our clusters green, so this is **forward-compat hardening** (not gating a live fire) — the upstream fix is the durable close.
- **New urgency (2026-07-02 sweep):** open upstream PR [#370](https://github.com/agent-substrate/substrate/issues/370) (SnapshotType enum removal; `SnapshotInfo.type` → `reserved`) is a concrete, imminent **second trigger** of the same brick class. Open PR [#356](https://github.com/agent-substrate/substrate/issues/356) (binary protobuf encoding for ateredis rows) is the structural fix — **merge order decides** whether a fresh brick fires (#356-first = safe, #370-first = brick).

**Steps**

Check the merge state of upstream #370 first — if it has merged, append the "Optional filing addendum" section (included in the fence below) and cite #370 as the concrete second trigger alongside #227.

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

## Optional filing addendum (use if #370 has merged by filing time)

This is not a one-off: #370 (SnapshotType enum removal, `SnapshotInfo.type` →
`reserved`) repeats the same shape — any `DBActor` row persisted before #370 carries
`"type"`, which the post-#370 strict decoder rejects, re-bricking `ListActors` on any
long-lived store. #356 (binary protobuf encoding for ateredis rows) fixes the class
structurally; until it lands, `DiscardUnknown` on the read paths is the minimal guard
that makes schema evolution safe for persisted rows.
````

**Page-side notes (not part of the paste body)**

- Re-verified at upstream `main` `b755ae73` (2026-06-30) — still unfixed, no drift: read paths still strict `protojson.Unmarshal` with zero `DiscardUnknown` anywhere in the ateredis pkg (`fetchActors` at `ateredis.go:746` backing `ListActors` at `:639`; persisted-row read at `:771`); the #227 field-12 rename still in `ateapi.proto:125`. Re-verify at filing time.
- Drop the "Optional filing addendum" section if #370 has **not** merged at filing time (the base body stands alone).

<a id="u4-checkpoint-exit-128"></a>

### §U4 — Checkpoint of an exited/destroyed container retried forever (runsc exit 128)

**Internal tracking a#4189 · file-ready issue for agent-substrate/substrate**

**What's blocked**

- **Suspend/resume lifecycle reliability on full-plane installs**: an actor whose container exited or was destroyed wedges **permanently in SUSPENDING** — `runsc checkpoint` exit 128 can never succeed by retrying the same worker, and the controller exp-backoff retries forever. Golden actors additionally block their ActorTemplate from ever reaching Ready.
- **Three independent occurrences in 2 days** (2026-07-01/02) on a full-plane static-certs validation install: an e2e lane worker, plus two golden actors (one looped ~24h). 3/3 converged **only** via manual hosting-worker-pod recycle; none self-converged.
- Freshest of the four filings: source pinned at upstream `main` `8631ae3` (2026-07-01); citations independently co-reviewed 3/3 verbatim, and the clean suspend/resume contract was independently verified (in-RAM carryover), corroborating exit-128 as an anomalous missing-container wedge, not expected behavior.

**Steps**

```bash
gh issue create --repo agent-substrate/substrate \
  --title "checkpoint against an exited/destroyed container is retried forever instead of being treated as terminal (runsc exit 128 → permanent SUSPENDING wedge)" \
  --body-file u4-body.md
```

**Issue body (copy-paste)**

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

- Source citations pinned at `main` `8631ae3` (2026-07-01) — `runsc.go:107-135`, `main.go:280-283`, `actortemplate_controller.go:149-151`. Re-verify line numbers at filing time.

---

## Also staged upstream (improvements, not metric blockers)

Prepared and sign-ready, but holding no benchmark metric — file/land opportunistically:

- **Internal tracking a#3957** — sandbox router gVisor toleration (`sandbox_router.yaml`); patch staged sign-ready alongside the §S artifacts (same pod retrieval pattern as the Steps blocks above).
- **Internal tracking a#4064** — e2e-harness explicit-timeout + VCT-reject pre-wait (two patches; compile re-verified 2026-07-03).
- **Internal tracking a#1347** — PodTemplate immutability; patch git-tracked internally under `kb/sandbox/references/patches/` (sign-ready; rebase-check before signing).
- **agent-substrate/substrate status-discipline set** — [#1069](https://github.com/agent-substrate/substrate/issues/1069) (observedGeneration), [#1127](https://github.com/agent-substrate/substrate/issues/1127) / [#1128](https://github.com/agent-substrate/substrate/issues/1128) / [#1134](https://github.com/agent-substrate/substrate/issues/1134) (WorkerPool/ActorTemplate status + Events + print columns); clone branches staged; charter-gated.
- **Excluded:** internal tracking a#2082 (netpol template-ref-hash) — superseded by in-flight upstream [kubernetes-sigs/agent-sandbox#793](https://github.com/kubernetes-sigs/agent-sandbox/issues/793) / [#1067](https://github.com/kubernetes-sigs/agent-sandbox/issues/1067); do not sign.
