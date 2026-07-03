# Work in progress — pending & absent cells

Every `pending`, `N/A`, or otherwise-absent cell on this benchmark's pages links here, to the entry for **why** it is absent and **when** it graduates. This is the honesty contract: an absent cell is never a silent gap — it names its reason class, its status, and either a date or the named gate it waits on.

Each entry declares: **What** (the metric), **Why absent** (not-yet-measured vs gated vs N/A-by-construction), **In flight** (who/what is working it), **ETA** (a date or a named gate — never “soon”), and **Trace** (tracking links). An ETA is a commitment or a named gate; a slipped ETA is updated here, not deleted.

_Anchors and the entry set are generated from the closed pending-reason enum — this page is machine-rendered, not hand-maintained._

<a id="not-yet-measured"></a>

## Awaiting its measurement fire (`pending`)

- **What:** A cell whose metric simply has not been measured yet — a genuinely not-yet-run cell (for example, a throughput figure before its TTFE fire has run).
- **Why absent:** **not-yet-measured.** No blocker — the measurement fire has not run for this cell. A bare `pending` (with no reason in parentheses) is always this class.
- **In flight:** Yes — picked up by the standing TTFE / throughput refresh cadence.
- **ETA:** Next scheduled refresh fire. The page regenerates from the fire's results with no hand-entry, so the cell fills the moment its fire lands.
- **Trace:** Standing refresh cadence; traceability tracked in [hb#166](https://github.com/AlexBulankou/honest-bench/issues/166).

<a id="cluster-fire"></a>

## Per-cluster throughput awaits a saturation fire (`cluster-fire`)

- **What:** The per-**cluster** sustained creation throughput — how many sandboxes/sec the whole cluster holds under that row's SLO bar. It is the second half of each dual throughput cell (`per-node · per-cluster`); the per-node half has already landed.
- **Why absent:** **not-yet-measured.** The per-node engineering rate is measured, but the validated per-cluster figure needs its own schema-validated cluster-saturation fire. We refuse to print a per-node × N extrapolation — that fiction breaks above the controller reconcile ceiling — so the cluster half stays `pending (cluster-fire)` until a real per-mode cluster fire lands the `thpt_*_per_cluster` fields.
- **In flight:** Yes — the per-activation-mode cluster-throughput fire that emits the per-cluster fields is the deliverable that graduates these halves.
- **ETA:** Gated on the per-mode cluster-throughput fire ([hb#132](https://github.com/AlexBulankou/honest-bench/issues/132)).
- **Trace:** [hb#132](https://github.com/AlexBulankou/honest-bench/issues/132) (dual per-node + per-cluster throughput).

<a id="upstream-blocked"></a>

## Resume-from-suspend is blocked upstream (`upstream-blocked`)

- **What:** TTFE and throughput for the **resume-from-suspend** activation mode — restore a previously-suspended sandbox and run the first instruction.
- **Why absent:** **Gated (upstream).** The run itself lands, but an upstream controller gap holds graduation: on gVisor the suspended condition never clears. This is a known upstream gap, NOT an unrun or failed cell. (The Kata + microVM resume cell is a separate story — `na-by-construction`, because this CRIU-based metric does not transfer to the Kata VM isolation model.)
- **In flight:** Yes — tracked upstream in the agent-sandbox controller: [agent-sandbox#873](https://github.com/kubernetes-sigs/agent-sandbox/issues/873) (issue, open) → fix [agent-sandbox#893](https://github.com/kubernetes-sigs/agent-sandbox/pull/893) (PR, in review). No honest-bench-side measurement can graduate it until the upstream fix lands.
- **ETA:** Gated on the upstream agent-sandbox resume-graduation fix. There is no honest-bench-side date — the cell graduates to a real number the moment upstream lands, not when a run is scheduled.
- **Trace:** Upstream agent-sandbox controller (resume graduation): [agent-sandbox#873](https://github.com/kubernetes-sigs/agent-sandbox/issues/873) (issue, open) → fix [agent-sandbox#893](https://github.com/kubernetes-sigs/agent-sandbox/pull/893) (PR, in review).

<a id="requires-gvisor-runtime"></a>

## Needs a gVisor run (`requires-gvisor-runtime`)

- **What:** A gVisor-family cell whose measurement requires the live node to run the gVisor runtime.
- **Why absent:** **not-yet-measured (runtime-gated).** A single run measures one runtime; this run measured a different one, so the gVisor cell pends until a gVisor run fills it.
- **In flight:** Yes — covered by the standing gVisor refresh run.
- **ETA:** Next gVisor refresh run.
- **Trace:** Standing refresh cadence ([hb#166](https://github.com/AlexBulankou/honest-bench/issues/166)).

<a id="requires-kata-runtime"></a>

## Needs a Kata run (`requires-kata-runtime`)

- **What:** A Kata-family cell whose measurement requires the live node to run the Kata runtime.
- **Why absent:** **not-yet-measured (runtime-gated).** Symmetric with the gVisor case: this run measured a different runtime, so the Kata cell pends until a Kata run fills it.
- **In flight:** Yes — covered by the standing Kata refresh run on the Kata node pool.
- **ETA:** Next Kata refresh run.
- **Trace:** Standing refresh cadence ([hb#166](https://github.com/AlexBulankou/honest-bench/issues/166)).

<a id="requires-gke"></a>

## Needs a GKE cluster (`requires-gke`)

- **What:** A cell whose measurement requires a GKE cluster (the substrate these numbers are measured on).
- **Why absent:** **not-yet-measured (environment-gated).** The measurement pends until it runs on a GKE cluster.
- **In flight:** Yes — part of the standing refresh cadence.
- **ETA:** Next refresh run on a GKE cluster.
- **Trace:** Standing refresh cadence ([hb#166](https://github.com/AlexBulankou/honest-bench/issues/166)).

<a id="requires-kata-microvm"></a>

## Kata + microVM rows not yet measured (`requires-kata-microvm`)

- **What:** The Kata + microVM runtime rows, where a Kata+microVM measurement has not yet run.
- **Why absent:** **not-yet-measured.** The Kata + microVM matrix rows are uniformly awaiting their measurement; the public page carries no internal issue ref for them by the PII fence.
- **In flight:** Yes — tracked internally; graduates as Kata+microVM fires land.
- **ETA:** Next Kata + microVM refresh run.
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
