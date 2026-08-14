# Work in progress — pending & absent cells

Every `pending`, `N/A`, or otherwise-absent cell on this benchmark's pages links here, to the entry for **why** it is absent and **when** it graduates. This is the honesty contract: an absent cell is never a silent gap — it names its reason class, its status, and either a date or the named gate it waits on.

Each entry declares: **What** (the metric), **Why absent** (not-yet-measured vs gated vs N/A-by-construction), **In flight** (who/what is working it), **ETA** (a date or a named gate — never “soon”), and **Trace** (tracking links). An ETA is a commitment or a named gate; a slipped ETA is updated here, not deleted.

_Anchors and the entry set are generated from the closed pending-reason enum — this page is machine-rendered, not hand-maintained._

<a id="not-yet-measured"></a>

## Awaiting its measurement fire (`pending`)

- **What:** A cell whose metric simply has not been measured yet — a genuinely not-yet-run cell (for example, a throughput figure before its TTFE fire has run).
- **Why absent:** **not-yet-measured.** No blocker — the measurement fire has not run for this cell. A bare `pending` (with no reason in parentheses) is always this class.
- **In flight:** Yes — filled by a manually-invoked TTFE / throughput refresh run. There is no automatic recurring cadence: the run is triggered by hand and publishes its own results on completion.
- **ETA:** No scheduled date — pends until the refresh run is invoked by hand; the page regenerates from its results with no hand-entry.
- **Trace:** Refresh mechanism tracked in [hb#166](https://github.com/AlexBulankou/honest-bench/issues/166).

<a id="cluster-fire"></a>

## Per-cluster throughput awaits a saturation fire (`cluster-fire`)

- **What:** The per-**cluster** sustained creation throughput — how many sandboxes/sec the whole cluster holds under that row's SLO bar. It is the second half of each dual throughput cell (`per-node · per-cluster`); the per-node half has already landed.
- **Why absent:** **not-yet-measured**, except where noted below. The per-node engineering rate is measured, but the validated per-cluster figure needs its own schema-validated cluster-saturation fire. We refuse to print a per-node × N extrapolation — that fiction breaks above the controller reconcile ceiling — so the cluster half stays `pending (cluster-fire)` until a real per-mode cluster fire lands the `thpt_*_per_cluster` fields. **Kata `<1s` cell, current state:** that fire is a separate harness step from the routine per-node TTFE refresh — [hb#359](https://github.com/AlexBulankou/honest-bench/issues/359) (2026-07-23) ran it, and adopted a webhook-corroborated true-TTFE basis on the `<5s` cell (0.822/cluster at the time; a later refresh ([hb#425](https://github.com/AlexBulankou/honest-bench/issues/425)) moved it to the current 0.835/cluster — see the live Core Metrics table for today's figure, replacing the retired acquire-side-uncorroborated ≥0.133/cluster). On that SAME fire the representative cold rung measured ttfe_p95=2475ms — over the `<1s` bar — so the `<1s` cluster half was atomically dropped rather than carried forward on the retired weaker basis (no stale acq-basis key survives a downgrade). This is measured and honestly missed, not unmeasured: `pending (cluster-fire)` here means "this rung didn't qualify," not "nobody has looked yet." Not gated by [agent-sandbox#940](https://github.com/kubernetes-sigs/agent-sandbox/issues/940) (the warm-pool-only controller-histogram trust gate below) or by any rung-ladder defect — the ladder ran cleanly and reported honestly.
- **In flight:** Yes — the per-activation-mode cluster-throughput fire that emits the per-cluster fields is the deliverable that graduates these halves. For the Kata `<1s` cell specifically, the next `scripts/kata_cold_ttfe_sweep.py` re-fire is what could graduate it — either a rung/config that clears `<1s`, or corroboration that the Kata microVM cold-start floor is architecturally over 1s at every rate (which would convert this to an honest measured-floor marker instead of staying open-ended `pending`).
- **ETA:** No open blocker — [hb#132](https://github.com/AlexBulankou/honest-bench/issues/132) shipped the dual per-node/per-cluster mechanism (closed 2026-07-11; gVisor's per-cluster cells already use it). The Kata warm-pool-hit `<1s` cell needs another manually-invoked, collision-acked fire of `scripts/kata_cold_ttfe_sweep.py` (shared cluster) — no scheduled date, next time the fire is run; the mechanism is not the gap, a qualifying measurement is.
- **Trace:** [hb#132](https://github.com/AlexBulankou/honest-bench/issues/132) (dual per-node + per-cluster throughput, closed/shipped); [hb#359](https://github.com/AlexBulankou/honest-bench/issues/359) (2026-07-23 true-TTFE adoption fire that produced the current honest-miss state on the Kata `<1s` cell; internal tracking a#5396 box-4).

<a id="overshoot-inconclusive"></a>

## Enforcement overshoot ran but did not classify (`overshoot-inconclusive`)

- **What:** The resource-limit-enforcement cell: does the runtime CONFINE a sandbox to its declared memory footprint? The probe declares a small limit, then has the sandbox attempt a controlled over-limit allocation — an enforced runtime OOM-kills it at the limit (PASS), an unenforced one lets the over-allocation succeed (FAIL).
- **Why absent:** **armed-but-inconclusive.** The probe ran on the live runtime, but the sandbox reached a terminal state that is neither a clean cgroup OOM-kill nor a clean over-limit survival — an unclassifiable exit. The cell degrades here rather than fabricate an enforcement badge or report a false breach: an armed probe that cannot read its own signal is honest-empty, not a verdict.
- **In flight:** Yes — the controlled-overshoot confinement probe is built and arms on the coordinated substrate fire; a re-fire on the live runtime resolves the cell to PASS/FAIL. Internal tracking a#5634.
- **ETA:** Next coordinated substrate fire of the enforcement probe.
- **Trace:** Confinement-enforcement axis (the declared-vs-enforced density-honesty backstop). Internal tracking a#5634 / a#3868.

<a id="requires-gvisor-runtime"></a>

## Needs a gVisor run (`requires-gvisor-runtime`)

- **What:** A gVisor-family cell whose measurement requires the live node to run the gVisor runtime.
- **Why absent:** **not-yet-measured (runtime-gated).** A single run measures one runtime; this run measured a different one, so the gVisor cell pends until a gVisor run fills it.
- **In flight:** Yes — filled by a manually-invoked gVisor refresh run (no automatic recurring cadence).
- **ETA:** No scheduled date — fires on manual invocation of the refresh run.
- **Trace:** Refresh mechanism tracked in [hb#166](https://github.com/AlexBulankou/honest-bench/issues/166).

<a id="requires-kata-runtime"></a>

## Needs a Kata run (`requires-kata-runtime`)

- **What:** A Kata-family cell whose measurement requires the live node to run the Kata runtime.
- **Why absent:** **not-yet-measured (runtime-gated).** Symmetric with the gVisor case: this run measured a different runtime, so the Kata cell pends until a Kata run fills it.
- **In flight:** Yes — filled by a manually-invoked Kata refresh run on the Kata node pool (no automatic recurring cadence).
- **ETA:** No scheduled date — fires on manual invocation of the refresh run.
- **Trace:** Refresh mechanism tracked in [hb#166](https://github.com/AlexBulankou/honest-bench/issues/166).

<a id="requires-gke"></a>

## Needs a GKE cluster (`requires-gke`)

- **What:** A cell whose measurement requires a GKE cluster (the substrate these numbers are measured on).
- **Why absent:** **not-yet-measured (environment-gated).** The measurement pends until it runs on a GKE cluster.
- **In flight:** Yes — filled by a manually-invoked refresh run on a GKE cluster (no automatic recurring cadence).
- **ETA:** No scheduled date — fires on manual invocation of the refresh run.
- **Trace:** Refresh mechanism tracked in [hb#166](https://github.com/AlexBulankou/honest-bench/issues/166).

<a id="requires-kata-microvm"></a>

## Kata + microVM rows not yet measured (`requires-kata-microvm`)

- **What:** The Kata + microVM runtime rows, where a Kata+microVM measurement has not yet run.
- **Why absent:** **not-yet-measured.** The Kata + microVM matrix rows are uniformly awaiting their measurement; the public page carries no internal issue ref for them by the PII fence.
- **In flight:** Yes — tracked internally; filled by a manually-invoked Kata + microVM refresh run (no automatic recurring cadence).
- **ETA:** No scheduled date — fires on manual invocation of the refresh run.
- **Trace:** Internal tracking (no public issue by the PII fence).

<a id="pool-topology-constrained"></a>

## Needs a pool sized for N concurrent warms (`pool-topology-constrained`)

- **What:** A cell whose run DID land, but whose number is a node-pool topology artifact — N concurrent microVM boots contend for a single pool node's vCPUs, stalling the marginal replica — rather than a runtime property.
- **Why absent:** **Gated (spend).** A representative figure needs a node pool sized for N concurrent warms, which is a deliberate spend action, not a re-run of the existing pool.
- **In flight:** Not scheduled — spend-gated pending a deliberate pool-sizing decision.
- **ETA:** Gated on provisioning a larger pool (a deliberate spend decision).
- **Trace:** Internal tracking (spend decision).

<a id="na-by-construction"></a>

## N/A by construction — structurally impossible (`N/A`)

- **What:** The resume-from-suspend × Kata + microVM cell (and any cell rendered `N/A`).
- **Why absent:** **N/A by construction.** CRIU checkpoint/restore does not transfer to the Kata VM isolation model, so this cell can NEVER be measured. This is distinct from `pending`, which awaits a run that is at least possible.
- **In flight:** None — there is nothing to measure.
- **ETA:** None. This is not a pending measurement and carries no ETA — it will never graduate to a number (an honest `N/A` beats an implied future measurement).
- **Trace:** None — structural, not tracked.

## Resolved (archive)

The reason classes below no longer back any live pending cell on this benchmark's pages — each was resolved upstream. Kept here (not deleted) so a historical result file still carrying the old reason string resolves to an entry instead of a dangling link.

<a id="trust-gate"></a>

## Warm-pool SLO-rate cluster cells graduated — upstream fix landed (`trust-gate`, RESOLVED)

- **What:** A warm-pool per-**cluster** SLO-rate cell whose measurement fire DID run, but whose per-mode derivation was formerly refused: the controller-side rate leg disagreed with the acquisition-side leg beyond the pre-declared tolerance (rel-diff > 0.10) at every measured rung, on both runtimes.
- **Why absent:** **Resolved upstream — archive entry, no live cell.** This class formerly gated warm-pool per-cluster SLO-rate cells: the controller startup-latency histogram double-recorded Ready transitions on stale-informer replays, inflating the controller leg ~1.7–2× and failing the acquire/controller agreement gate. Both upstream legs (the suspend/resume re-record guard and the targeted stale-informer-replay fix) have since merged, and the histogram-vs-acquire cross-check now PASSES against a post-fix build (ratios 1.000 / 0.9375, within the 0.10 tolerance) — so no matrix cell currently renders this class, and the entry is retained as a schema/catalog archive. The warm-pool cells graduate to a `≥`-floor figure on the literal-TTFE basis, not a bare measured rate — that further graduation needs the separate true-TTFE (webhook) basis, tracked under `no-compliant-rung`.
- **In flight:** Resolved — both upstream controller legs have merged: [agent-sandbox#940](https://github.com/kubernetes-sigs/agent-sandbox/issues/940) (issue, closed) → fix [agent-sandbox#1087](https://github.com/kubernetes-sigs/agent-sandbox/pull/1087) (PR, merged) → fix [agent-sandbox#1114](https://github.com/kubernetes-sigs/agent-sandbox/pull/1114) (PR, merged). A fresh fire against a post-fix build confirmed the agreement-gate cross-check passes, so the warm-pool cells are no longer trust-gate-capped.
- **ETA:** Graduated — both upstream legs merged and a post-fix fire confirmed the agreement gate passes. No outstanding date.
- **Trace:** Upstream agent-sandbox controller (histogram double-record, resolved): [agent-sandbox#940](https://github.com/kubernetes-sigs/agent-sandbox/issues/940) (issue, closed) → fix [agent-sandbox#1087](https://github.com/kubernetes-sigs/agent-sandbox/pull/1087) (PR, merged) → fix [agent-sandbox#1114](https://github.com/kubernetes-sigs/agent-sandbox/pull/1114) (PR, merged). Internal tracking a#4364 (gate exposure, closed out).

<a id="no-compliant-rung"></a>

## Cold-start per-cluster SLO-rate cells graduated — upstream fix landed (`no-compliant-rung`, RESOLVED)

- **What:** A cold-start per-**cluster** SLO-rate cell whose measurement fire DID run with the trust gate PASSING, but where every measured rung's p95 formerly sat over the cell's SLO bar on the only available (literal upper-bound) basis.
- **Why absent:** **Resolved upstream — archive entry, no live cell.** This class formerly gated cold-start per-cluster SLO-rate cells on the sizing side (an SLO-gated rate cannot be published as 0 from a finite ladder without a pre-declared floor condition, and the tighter true-TTFE basis had no production writer upstream). The true-TTFE webhook-inject-timestamp example has since merged upstream and was adopted on the Kata cold measurement path, and both formerly-gated cold cells have graduated independently — so no matrix cell currently renders this class, and the entry is retained as a schema/catalog archive.
- **In flight:** Resolved — the true-TTFE webhook example has merged upstream: [agent-sandbox#751](https://github.com/kubernetes-sigs/agent-sandbox/issues/751) (issue, closed) → fix [agent-sandbox#761](https://github.com/kubernetes-sigs/agent-sandbox/pull/761) (PR, merged). Both formerly-gated cold cells have graduated independently.
- **ETA:** Graduated — the upstream true-TTFE writer merged and the affected cold cells filled independently. No outstanding date.
- **Trace:** Upstream agent-sandbox (end-to-end TTFE measurability, resolved): [agent-sandbox#751](https://github.com/kubernetes-sigs/agent-sandbox/issues/751) (issue, closed) → fix [agent-sandbox#761](https://github.com/kubernetes-sigs/agent-sandbox/pull/761) (PR, merged). Internal tracking a#3975 (basis fallback, closed out).

<a id="upstream-blocked"></a>

## Resume-from-suspend graduated — upstream fix landed (`upstream-blocked`, RESOLVED)

- **What:** TTFE and throughput for the **resume-from-suspend** activation mode — restore a previously-suspended sandbox and run the first instruction.
- **Why absent:** **Resolved upstream — archive entry, no live cell.** This class formerly gated the gVisor resume cell: the run landed, but an upstream controller gap (the suspended condition never cleared on resume) held graduation. That fix has since merged upstream, and the gVisor resume cell has graduated to measured numbers, so no matrix cell currently renders this class — the entry is retained as a schema/catalog archive. (The Kata + microVM resume cell is a separate story — `na-by-construction`, because this CRIU-based metric does not transfer to the Kata VM isolation model.)
- **In flight:** Resolved — the upstream agent-sandbox controller fix has merged: [agent-sandbox#873](https://github.com/kubernetes-sigs/agent-sandbox/issues/873) (issue, closed) → fix [agent-sandbox#1150](https://github.com/kubernetes-sigs/agent-sandbox/pull/1150) (PR, merged). A fresh resume probe against a build carrying the fix confirmed the suspended condition clears on resume, and the gVisor resume matrix row now carries real numbers.
- **ETA:** Graduated — the upstream resume-graduation fix merged and a fresh resume probe run landed, so the gVisor resume cell has flipped from pending to a measured number. No outstanding date.
- **Trace:** Upstream agent-sandbox controller (resume graduation, resolved): [agent-sandbox#873](https://github.com/kubernetes-sigs/agent-sandbox/issues/873) (issue, closed) → fix [agent-sandbox#1150](https://github.com/kubernetes-sigs/agent-sandbox/pull/1150) (PR, merged).
