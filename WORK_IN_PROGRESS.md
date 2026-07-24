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
- **Why absent (Kata `<1s`, current state as of #359):** **measured, and honestly missed — not unmeasured.** This is a separate harness step from the routine `hb-refresh-gke-kata` per-node TTFE refresh: `scripts/kata_cold_ttfe_sweep.py` fires real cold `SandboxClaims` on the persistent scenarios cluster and derives a webhook-corroborated true-TTFE basis via `harness.slo_rate`/`harness.ttfe_stamp`. That fire already ran (2026-07-23, #359) and replaced the prior weaker acquire-side-uncorroborated derivation (`≥0.133/cluster`, from the 07-08 warm ladder fire, #241) with the stronger true-TTFE basis on the `<5s` cell (now `0.822/cluster`). On the SAME fire, the representative cold rung measured `ttfe_p95=2475ms` — over the `<1s` bar — so the `<1s` cluster half was atomically dropped rather than carried forward on the retired weaker basis (transition-guard idiom: no stale acq-basis key survives a downgrade). The `pending (cluster-fire)` label is therefore an honest "this rung didn't qualify," not "nobody has looked yet."
- **In flight:** The next `kata_cold_ttfe_sweep.py` re-fire is the mechanism that could graduate this cell — either with a rung/config that clears `<1s`, or by corroborating that the Kata microVM cold-start floor is architecturally over 1s at every rate (which would let this convert to an honest measured-floor marker, the Kata analogue of gVisor's `***Z` zero-corroborated class, instead of staying open-ended `pending`).
- **Why NOT blocked:** Not gated by [agent-sandbox#940](https://github.com/kubernetes-sigs/agent-sandbox/issues/940) (the warm-pool controller-histogram trust gate, see `#trust-gate` below) — that gate is a warm-pool-specific defect and does not apply to this cold-path true-TTFE derivation; #359's fire produced a controller-webhook-corroborated basis cleanly. Not gated by any rung-ladder defect either — the ladder ran and reported honestly.
- **ETA:** No scheduled date; needs another manually-invoked, collision-acked fire of `scripts/kata_cold_ttfe_sweep.py` (shared cluster). [hb#132](https://github.com/AlexBulankou/honest-bench/issues/132) shipped the dual per-node/per-cluster mechanism (closed 2026-07-11; gVisor's per-cluster cells already use it) — the mechanism itself is not the gap here, a qualifying measurement is.
- **Trace:** [hb#132](https://github.com/AlexBulankou/honest-bench/issues/132) (dual per-node + per-cluster throughput, closed/shipped); [#359](https://github.com/AlexBulankou/honest-bench/pull/359) (2026-07-23 true-TTFE adoption fire that produced the current honest-miss state, hb#5396 box-4).

<a id="trust-gate"></a>

## SLO-rate fire ran; derivation refused by the trust gate (`trust-gate`)

- **What:** A warm-pool per-**cluster** SLO-rate cell whose measurement fire DID run, but whose per-mode derivation was refused: the controller-side rate leg disagreed with the acquisition-side leg beyond the pre-declared tolerance (rel-diff > 0.10) at every measured rung, on both runtimes.
- **Why absent:** **Gated (upstream, trust).** The two independent rate legs must agree before a number publishes; on the warm-pool path they do not — the controller startup-latency histogram double-records Ready transitions on stale-informer replays, inflating the controller leg ~1.7–2×. Cold-path control legs PASS the same gate on both runtimes, pinning the defect to the warm-pool path. The cell is honest-empty rather than publish a number whose cross-check fails.
- **In flight:** Yes — tracked upstream in the agent-sandbox controller: [agent-sandbox#940](https://github.com/kubernetes-sigs/agent-sandbox/issues/940) (issue, open) → fix [agent-sandbox#1087](https://github.com/kubernetes-sigs/agent-sandbox/pull/1087) (PR, in review) → fix [agent-sandbox#1114](https://github.com/kubernetes-sigs/agent-sandbox/pull/1114) (PR, merged). Internal tracking a#4364 (gate exposure) / a#4277 (no tuning to avoid honest-empty).
- **ETA:** Gated on the upstream histogram record-once fix. The cell graduates the moment a post-fix fire passes the agreement gate — no honest-bench-side date.
- **Trace:** Upstream agent-sandbox controller (histogram double-record): [agent-sandbox#940](https://github.com/kubernetes-sigs/agent-sandbox/issues/940) (issue, open) → fix [agent-sandbox#1087](https://github.com/kubernetes-sigs/agent-sandbox/pull/1087) (PR, in review) → fix [agent-sandbox#1114](https://github.com/kubernetes-sigs/agent-sandbox/pull/1114) (PR, merged). Internal tracking a#4364.

<a id="no-compliant-rung"></a>

## SLO-rate fire ran; no rung met the bar (`no-compliant-rung`)

- **What:** A cold-start per-**cluster** SLO-rate cell whose measurement fire DID run with the trust gate PASSING, but where every measured rung's p95 sits over the cell's SLO bar on the only available (literal upper-bound) basis.
- **Why absent:** **not-yet-graduated (basis-gated).** An SLO-gated rate cannot be published as 0 from a finite ladder — a lower untested rate could still comply — so "no compliant rung ⇒ pend, never 0". The literal TTFE basis is an UPPER bound (it includes probe scheduling overhead); the tighter true-TTFE basis has no production writer upstream, so the cell may yet fill once that lands.
- **In flight:** Yes — the true-TTFE annotation writer is tracked upstream: [agent-sandbox#751](https://github.com/kubernetes-sigs/agent-sandbox/issues/751) (issue, closed) → fix [agent-sandbox#761](https://github.com/kubernetes-sigs/agent-sandbox/pull/761) (PR, merged). Internal tracking a#3975 (basis fallback).
- **ETA:** Gated on the upstream true-TTFE writer, or a future fire whose literal-basis p95 clears the bar at some measured rate.
- **Trace:** Upstream agent-sandbox (end-to-end TTFE measurability): [agent-sandbox#751](https://github.com/kubernetes-sigs/agent-sandbox/issues/751) (issue, closed) → fix [agent-sandbox#761](https://github.com/kubernetes-sigs/agent-sandbox/pull/761) (PR, merged). Internal tracking a#3975 / a#4364.

<a id="upstream-blocked"></a>

## Resume-from-suspend is blocked upstream (`upstream-blocked`)

- **What:** TTFE and throughput for the **resume-from-suspend** activation mode — restore a previously-suspended sandbox and run the first instruction.
- **Why absent:** **Gated (upstream).** The run itself lands, but an upstream controller gap holds graduation: on gVisor the suspended condition never clears. This is a known upstream gap, NOT an unrun or failed cell. (The Kata + microVM resume cell is a separate story — `na-by-construction`, because this CRIU-based metric does not transfer to the Kata VM isolation model.)
- **In flight:** Yes — tracked upstream in the agent-sandbox controller: [agent-sandbox#873](https://github.com/kubernetes-sigs/agent-sandbox/issues/873) (issue, open) → fix [agent-sandbox#1150](https://github.com/kubernetes-sigs/agent-sandbox/pull/1150) (PR, in review). No honest-bench-side measurement can graduate it until the upstream fix lands.
- **ETA:** Gated on the upstream agent-sandbox resume-graduation fix — [agent-sandbox#1150](https://github.com/kubernetes-sigs/agent-sandbox/pull/1150) is OPEN, in review (last activity 2026-07-22; not yet merged as of 2026-07-24). No honest-bench-side date — the cell graduates to a real number the moment that PR merges and a fresh resume probe run lands, not when a run is scheduled.
- **Trace:** Upstream agent-sandbox controller (resume graduation): [agent-sandbox#873](https://github.com/kubernetes-sigs/agent-sandbox/issues/873) (issue, open) → fix [agent-sandbox#1150](https://github.com/kubernetes-sigs/agent-sandbox/pull/1150) (PR, in review).

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
