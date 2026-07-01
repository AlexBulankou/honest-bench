"""Closed-schema allow-list for the public honest-benchmarks render (Layer-1 PII guard).

This is the PRIMARY guard: the renderer emits ONLY field-names + types declared here.
Anything not in the schema is DROPPED, so no harness free-text can reach the public page
by construction. The Layer-2 denylist scanners (2a public / 2b our-side codename) are the
belt-and-suspenders backstop, not the primary defense.
"""

import re

PRODUCTS = {"sandbox", "sandbox-kata", "substrate"}

# cluster-substrate VALUE is allowed (generic GKE feature names); the internal
# scenario/demo cluster NAMES are never a value here (Layer-2 denies those by pattern).
CLUSTER_SUBSTRATES = {"kind", "gke", "gke-sandbox", "gke-kata"}

OUTCOMES = {"PASS", "FAIL", "pending"}

# native_digest_cold image-cache posture (#3885/#3894). "cold-provision" = the honest
# upper bound on a node that may already have the image layers cached; "cold-pull" = the
# same path on a guaranteed-empty node, so the number also includes the full layer
# download. A closed enum mirroring the harness side (COLD_START_MODE_ENUM in
# harness/results_schema.py). The render guard is SECONDARY — the harness emitter
# fail-closes on a typo'd value — so here an out-of-enum value is simply dropped, and an
# absent value renders no label (graceful degradation on the empty-provenance seed).
COLD_START_MODES = {"cold-provision", "cold-pull"}

# badge_scope (#3905) is a per-SCENARIO closed enum qualifying what a security-isolation
# PASS actually asserts. "control-plane" = the policy/runtime-class was admitted and
# correctly targeted (NOT that data-plane traffic was enforced); "enforced" = data-plane
# enforcement was actually exercised. It renders as a suffix on the scenario's PASS cell
# (e.g. "PASS (control-plane)"), so the public badge cannot over-claim enforcement. This
# replaces the interim hardcoded "(control-plane)" baked into SCENARIO_LABELS: the
# qualifier is now data-driven (carried per-cell from the harness), so a cell can move
# control-plane → enforced by emitting the new value, no label edit. Absent ⇒ no suffix
# (graceful degradation); out-of-enum ⇒ dropped at render (the emitter fail-closes first).
BADGE_SCOPES = {"control-plane", "enforced"}

# badge_construction (#3950) is a per-SCENARIO closed enum, ORTHOGONAL to badge_scope, that
# names WHICH NetworkPolicy mechanism a security-isolation cell actually measured — so an
# `enforced` scope can never be read as a guarantee it does not make. "standard-np" = a
# standard networking.k8s.io/v1 NetworkPolicy the harness built with explicit label
# propagation (the podSelector actually binds the tenant pods, so a data-plane breach is
# observable); "managed-np" = the managed gke-sandbox NetworkPolicy, whose podSelector may
# select zero pods (an inert breach — a PASS against it asserts admission, not data-plane
# blocking). It renders as a SECOND suffix term on the PASS cell ONLY when badge_scope is
# also present (e.g. "PASS (enforced, standard-np)"); it qualifies the enforcement claim and
# is meaningless alone, so construction-without-scope renders nothing. Absent ⇒ no second
# term (graceful degradation); out-of-enum ⇒ dropped at render (the emitter fail-closes
# first). This is the #3950-mandatory disclosure: a charter-#5 flip of the two NP cells from
# control-plane → enforced MUST carry the construction so the public badge discloses the
# standard-NP-with-label-propagation mechanism and never conflates it with managed-gke-sandbox NP.
BADGE_CONSTRUCTIONS = {"standard-np", "managed-np"}

# pending/FAIL cells render an ENUM reason only — never harness free-text.
PENDING_REASONS = {
    "requires-gvisor-runtime",
    # symmetric with requires-gvisor-runtime: a kata-family EMIT cell pends here when the
    # live substrate is not gke-kata (no kata runtime on the node). Emitted by the harness
    # (PENDING_REASON_ENUM in harness/results_schema.py); the cross-contract subset test
    # in harness/test_scenario_portability.py guards this set ⊇ the harness enum.
    "requires-kata-runtime",
    "requires-gke",
    "not-yet-measured",
    "upstream-blocked",
    # Goal-2.1 matrix: the Kata+microVM runtime rows are uniformly not-yet-measured
    # (tracked internally — the public page carries NO internal issue ref by PII fence).
    # Render-only seed footnote token (no harness emit path), distinct from the
    # requires-kata-runtime EMIT-cell pend above.
    "requires-kata-microvm",
}

# provenance: only these keys render, each validated by the predicate below.
_SHA256 = re.compile(r"^sha256:[0-9a-f]{12,64}$")
_GITSHA = re.compile(r"^[0-9a-f]{7,40}$")
_RUNID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_CRD = re.compile(r"^v[0-9]+((alpha|beta)[0-9]+)?$")
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
# image: registry/path:tag — no credentials, no internal AR project paths get through the
# 2a scanner anyway; here we just bound the shape.
_IMAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*:[A-Za-z0-9][A-Za-z0-9._-]*$")
# GCP machine type shape (a#3960 step-up): <family>-<class>[-<size>], e.g. e2-standard-16,
# n2d-standard-8, c3-highmem-4, e2-medium. The family/class names are PUBLIC GCP identifiers
# (not infra-secret), but we bound the shape tightly so free-text can never ride this field.
# A value that is not a recognizable GCP machine shape (e.g. a custom type) is dropped.
_MACHINE_TYPE = re.compile(
    r"^[a-z][a-z0-9]*-(standard|highmem|highcpu|micro|small|medium)(-[0-9]+)?$"
)

# DELIBERATELY NOT allow-listed: any GCP project / infra identifier (e.g. a `project`
# field). It is infra-noise irrelevant to a customer-facing benchmark page, so it stays
# off-page by construction — a `project` key in latest.json is dropped here even though the
# id itself is a public one. Do NOT add it for "completeness": the closed allow-list, not the
# 2a denylist scanner, is what keeps it off the public render.
PROVENANCE_FIELDS = {
    "cluster_substrate": lambda v: v in CLUSTER_SUBSTRATES,
    "controller_image": lambda v: isinstance(v, str) and bool(_IMAGE.match(v)),
    "controller_digest": lambda v: isinstance(v, str) and bool(_SHA256.match(v)),
    "crd_version": lambda v: isinstance(v, str) and bool(_CRD.match(v)),
    "suite_git_sha": lambda v: isinstance(v, str) and bool(_GITSHA.match(v)),
    "run_id": lambda v: isinstance(v, str) and bool(_RUNID.match(v)),
    "node_count": lambda v: isinstance(v, int) and 0 < v < 10000,
    "cold_start_mode": lambda v: v in COLD_START_MODES,
    # Goal-2.1 matrix: which isolation runtime this run measured (runtimeClassName). Drives
    # the matrix's Runtime column; absent ⇒ the renderer defaults to gvisor (today's only
    # live runtime — the gke-sandbox/runsc path).
    "runtime": lambda v: v in RUNTIME_LABELS,
}

# scenario internal-name -> public display label. A scenario whose name is not in this
# vocabulary is DROPPED (and counted), so an unexpected harness name can never render.
SCENARIO_LABELS = {
    # sandbox MVP
    "burst_create": "Burst create throughput",
    "warmpool_cold_start": "Warm-pool activation (hit)",
    "native_digest_cold": "Unique-image cold start",
    "suspend_resume": "Resume from suspend",
    # The two NetworkPolicy cells assert CONTROL-PLANE admission only (policy admitted +
    # correctly targeted), NOT data-plane enforcement — so a PASS here means "the policy was
    # accepted", not "traffic was actually blocked". The scope qualifier is now DATA-DRIVEN
    # via the per-cell badge_scope enum (#3905) — the harness emits "control-plane" on these
    # cells and the renderer suffixes "PASS (control-plane)", so the label stays plain here
    # and a cell can later move to "enforced" by emitting the new value (no label edit).
    # gvisor_canary carries no badge_scope — it is a genuine runtime-class enforcement check
    # (asserts pod.spec.runtimeClassName == gvisor, phase Running), so its PASS is unqualified.
    "cross_tenant_network_isolation": "Cross-tenant network isolation",
    "default_deny_egress": "Default-deny egress",
    "gvisor_canary": "gVisor isolation canary",
    # sandbox sustained-churn axis (#3868). Companion to burst_create: burst_create measures
    # cold-start throughput at a burst; session_turnover measures how fast a warm pool
    # replenishes a consumed slot under sustained claim/release churn. INERT vocabulary until
    # the scenario is registered in scenario_map + fired — a cell only renders when its name
    # appears in results, so adding this label makes no public-page difference on its own.
    "session_turnover": "Warm-pool refill (churn)",
    # substrate MVP
    "cold_reconcile": "Cold reconcile",
    "suspend_resume_carryover": "Suspend/resume carry-over",
    "agent_identity_podcert": "Agent-identity (Pod-Certificate)",
}

# sla metric-name -> display label. Unknown metric keys are dropped; values must be numeric.
METRIC_LABELS = {
    # HB headline (alex 2026-06-28): the count metric, not single-sandbox latency.
    # "X sandboxes ready in <1s" = the whole-burst count of claims against ONE warm
    # pool that cleared the sub-1s bar (NOT divided by node count); density is the
    # portable per-vCPU scale (count / total cluster vCPU).
    "sandboxes_ready_under_1s": "Sandboxes ready <1s",
    "density_per_vcpu": "Density /vCPU",
    "activation_ms": "Activation (ms)",
    "cold_start_ms": "Cold start (ms)",
    "reconcile_ms": "Reconcile (ms)",
    "resume_ms": "Resume (ms)",
    # session_turnover warm-pool refill axis (#3868): median replenishment latency + p90
    # tail. The p90 column matches the floor-not-ceiling framing — a tail number a reader
    # can reproduce and beat, not a best-case headline. Inert until the scenario fires.
    "refill_latency_ms": "Refill latency (ms)",
    "refill_p90_ms": "Refill p90 (ms)",
}

# The goal-column set. They render "(non-public)" by construction whenever the internal
# targets file is absent (it never ships to the public repo).
GOAL_COLUMNS = ("committed", "target", "north-star")
NON_PUBLIC = "(non-public)"

# Build-over-build throughput history (#3918). One row per distinct controller build
# (keyed by controller_digest), carrying the headline COUNT so the page can show the
# build-over-build trajectory alex's #1 directive asks for — not just the latest snapshot.
# Same closed-schema discipline as the per-product render: a history row renders ONLY these
# field-names, each validated by its predicate; anything else is dropped on read, so an
# accrual writer cannot smuggle free-text onto the public page. Every field here is already
# public-safe (digest/git-sha/run-id/date/generic-substrate/measured-numbers) — no PII,
# no internal cadence. A row missing any required field, or whose value fails its predicate,
# is dropped entirely (graceful: a malformed history file degrades to fewer trend rows,
# never to a leak).
HISTORY_FIELDS = {
    "generated_at": lambda v: isinstance(v, str) and bool(_ISO.match(v)),
    "controller_digest": lambda v: isinstance(v, str) and bool(_SHA256.match(v)),
    "suite_git_sha": lambda v: isinstance(v, str) and bool(_GITSHA.match(v)),
    "run_id": lambda v: isinstance(v, str) and bool(_RUNID.match(v)),
    "cluster_substrate": lambda v: v in CLUSTER_SUBSTRATES,
    "sandboxes_ready_under_1s": lambda v: isinstance(v, (int, float))
    and not isinstance(v, bool)
    and v >= 0,
    "density_per_vcpu": lambda v: isinstance(v, (int, float))
    and not isinstance(v, bool)
    and v >= 0,
    "n": lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 0,
}

# --- Goal 2.1: Core Benchmark Matrix (alex "Agent Sandbox Core Metrics Table") -----------
# The customer page is reframed from a per-scenario scorecard to the doc's exact 9-column
# matrix: rows are (runtime × activation-mode), columns are the throughput/TTFE/density/
# exec-success metrics. The honesty spine is TTFE (Time-To-First-Instruction = the sandbox
# actually executed the first instruction and returned a result) — NOT pod-Ready. Cells we
# have not yet measured render `pending`; throughput under a TTFE threshold the p95 misses
# renders an honest `0` (emitted by the harness, not decided here).
#
# Same closed-schema discipline as the per-scenario render: only the keys below render, each
# validated by its predicate; anything else is dropped before it can reach the public page.

# runtime label (rides in provenance as `runtime`): internal enum -> public display.
RUNTIME_LABELS = {
    "gvisor": "gVisor",
    "kata-microvm": "Kata + microVM",
}
# Ordered runtime rows for the matrix (doc order: gVisor first, Kata second).
MATRIX_RUNTIMES = ("gvisor", "kata-microvm")

# Ordered activation-mode rows: (scenario internal-name, public display label). The display
# labels are the doc's exact mode names. A mode with no measured scenario renders `pending`.
ACTIVATION_MODE_ROWS = (
    ("warmpool_cold_start", "Warm-pool hit (Base image)"),
    ("native_digest_cold", "Unique-image cold (RL reality)"),
    ("suspend_resume", "Resume-from-suspend"),
)

# Cross-row TTFE-comparability floor. The matrix stacks activation-mode rows whose N differs by
# orders of magnitude (warm-pool N can be hundreds; a cold or resume row can be N=1..few). A
# reader scanning the TTFE p50/p95 columns top-to-bottom is invited to compare them as if equal,
# but a single-sample p50 is not a distribution — so a low-N row can read FASTER than a
# high-N row purely from sampling, INVERTING the true relationship (e.g. warm-pool p50 over
# hundreds of samples vs a cold N=1 lucky draw). Rows whose N is below this floor get an
# explicit small-sample marker on their TTFE cells so cross-row comparison stays honest. The
# floor is a comparability heuristic, not a validity gate: the cell still renders its measured
# value; the marker just says "don't rank this against a high-N row".
TTFE_COMPARABILITY_MIN_N = 30

# Density is a per-RUNTIME property (holds across activation modes), not per-mode. The
# renderer sources it from whichever of these scenarios carries density_per_vcpu, applies it
# to the warm-pool + cold rows, and renders N/A on the resume row (matching the doc).
# Single canonical source = warmpool_cold_start (a4s2 emit lock, PR #28): Layer-2 emits
# density ONLY on warm-pool with the per-node-allocatable denominator (the 1.88 basis), so
# the 1.88 is sourced unambiguously. burst_create is intentionally NOT here — its old
# cluster-wide-capacity 0.45 must never shadow the corrected per-node number.
DENSITY_SOURCE_SCENARIOS = ("warmpool_cold_start",)

# Matrix metric keys (per-scenario sla_metrics) -> closed-schema predicate. exec_success_n
# is OPTIONAL (the numerator for the doc's "(1277/1376)" fraction); absent ⇒ render the bare
# percentage. All are non-negative numerics; exec_success_rate is a 0..1 fraction.
_nonneg = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0
MATRIX_METRIC_FIELDS = {
    "ttfe_p50_ms": _nonneg,
    "ttfe_p95_ms": _nonneg,
    # p99 completes the percentile spine (a#3960 item 4). INERT in the fixed 9-column matrix
    # render today (no p99 column) — carried so a per-step Pareto row can show the tail a
    # reader can reproduce and beat. The producer emits only p50/p95 on matrix cells, so
    # allow-listing p99 here does not change matrix output.
    "ttfe_p99_ms": _nonneg,
    "thpt_under_5s_per_node": _nonneg,
    "thpt_under_1s_per_node": _nonneg,
    "exec_success_rate": lambda v: isinstance(v, (int, float))
    and not isinstance(v, bool)
    and 0.0 <= v <= 1.0,
    "exec_success_n": lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 0,
    "density_per_vcpu": _nonneg,
}

# --- #3954: Burst-create TTFE corroboration block ----------------------------------------
# burst_create's headline "sandboxes ready <1s" is a POD-READY count — the weaker claim, since
# a pod can report Ready before it can run code. #3954 adds the literal TTFE corroboration:
# sandboxes_exec_under_1s = the count whose FIRST INSTRUCTION executed and returned a result in
# <1s (the stronger claim), and exec_success_rate = the exec round-trip success fraction. The
# corroboration SIGNAL is the gap (ready - exec): sandboxes that reported Ready but had not yet
# run code. The render block is INERT by construction — it fires ONLY when BOTH the pod-Ready and
# the executed-TTFE counts are present, so today's pre-#3954 data (ready-only) renders nothing and
# the public page is byte-unchanged until a #3954 fire emits sandboxes_exec_under_1s. Same closed-
# schema discipline: only these keys render, each validated; exec_success_n is the OPTIONAL
# numerator for the honesty-check fraction (absent ⇒ derived as round(rate*N)).
BURST_CORROBORATION_FIELDS = {
    "sandboxes_ready_under_1s": _nonneg,
    "sandboxes_exec_under_1s": _nonneg,
    "exec_success_rate": lambda v: isinstance(v, (int, float))
    and not isinstance(v, bool)
    and 0.0 <= v <= 1.0,
    "exec_success_n": lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 0,
}

# Scale-Proof (Linearity Check) second table. Proof that per-node throughput + density hold
# flat as the cluster grows. scale_points is the per-node sweep — each point carries
# node_count + density and (optional) per-node throughput; the two retention ratios are
# density_at_maxN/density_at_minN and thpt_at_maxN/thpt_at_minN.
def _scale_points_ok(v):
    if not isinstance(v, list) or not v:
        return False
    for p in v:
        if not isinstance(p, dict):
            return False
        nc, dn = p.get("node_count"), p.get("density")
        if not (isinstance(nc, int) and not isinstance(nc, bool) and 0 < nc < 10000):
            return False
        if not (isinstance(dn, (int, float)) and not isinstance(dn, bool) and dn >= 0):
            return False
        # throughput (per-node ready rate) is optional per point: the producer emits it so a
        # per-step throughput convergence subline can render, but older blocks omit it. When
        # present it must be a non-negative number (closed-schema: every field validated).
        if "throughput" in p:
            tp = p["throughput"]
            if not (isinstance(tp, (int, float)) and not isinstance(tp, bool) and tp >= 0):
                return False
    return True


SCALE_PROOF_FIELDS = {
    "scale_points": _scale_points_ok,
    "density_retention": _nonneg,
    "thpt_retention": _nonneg,
    # measured_at (#3952): ISO-8601 instant the sweep ran. Optional, carried forward
    # across the daily refresh so a point-in-time block is honestly dated apart from
    # the daily-refreshed top-level generated_at. Non-empty string only.
    "measured_at": lambda v: isinstance(v, str) and bool(v),
}

# --- #3954 sibling: warm-vs-cold speedup block (TOP-LEVEL warm_vs_cold object) -----------
# The harness warm_vs_cold classifier (harness/warm_vs_cold.py) composes the warm leg
# (burst TTFx p50) and the true-cold leg (native_digest_cold) into ONE honest headline a
# reader can quote: "warm provisioning is N times faster than cold." It emits an inner
# object under a TOP-LEVEL `warm_vs_cold` key (mirrors scale_proof), or omits it entirely
# when any honesty gate fails (semantic/runtime-class mismatch, corrupt leg, degenerate
# ratio). The render side is INERT until that object appears.
#
# Closed-schema discipline (Layer-1 PII guard): the block renders ONLY these field-names,
# each validated by its predicate; anything else is dropped on read. runtime_class is
# validated against the PUBLIC RUNTIME_LABELS enum (NOT a bare non-empty string) so an
# out-of-enum or free-text runtime can never reach the public page — it fails closed and
# drops the block. semantic is one of the two measured modes. All latencies/ratios are
# non-negative numerics; n_warm is the sample count (optional — render the bare headline
# when absent).
WARM_VS_COLD_FIELDS = {
    "warm_p50_ms": _nonneg,
    "cold_ms": _nonneg,
    "speedup": _nonneg,
    "semantic": lambda v: v in ("ttfi", "ttfe"),
    "runtime_class": lambda v: v in RUNTIME_LABELS,
    "n_warm": lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 0,
    # measured_at: ISO-8601 instant the two legs were measured. Optional, carried forward
    # across the daily refresh (same as scale_proof) so a point-in-time block is honestly
    # dated apart from the daily-refreshed top-level generated_at. Non-empty string only.
    "measured_at": lambda v: isinstance(v, str) and bool(v),
    # cold_start_mode (#4024): OPTIONAL closed-enum qualifying WHICH cold the cold leg
    # measured, so the public page never mislabels one cold semantic as the other.
    # "cold-pull" = a true-cold unique-image pull (native_digest_cold) — the locked
    # Framing-A leg; "cold-provision" = a warm-pool-overflow fresh-node provision off the
    # SHARED base image (image possibly node-cached), which must NOT claim unique-image.
    # Absent ⇒ the render falls back to the historical true-cold phrasing (byte-identical
    # to pre-#4024), so the existing locked block is unchanged.
    "cold_start_mode": lambda v: v in COLD_START_MODES,
}

# --- a#3960: Step-up backfill saturation Pareto ------------------------------------------
# The proven "300 sandboxes in <1s" story is a THROUGHPUT-SATURATION study, not single-
# sandbox latency: a SandboxWarmPool sustaining a creation RATE, swept step-by-step
# (10 -> 30 -> 100 -> ... sb/sec), each step held against a warm pool pre-sized by Little's
# law (warm = ceil(WPR * rate * SLD)). Per step we read TTFE p50/p95/p99 straight off the
# controller metric agent_sandbox_claim_startup_latency_ms (warm/cold labeled) + the ready
# rate, and classify the curve against the North Star (p95 < 500ms) / collapse (2000ms)
# bands. This block is the PUBLIC-safe, scrubbed mirror of the internal classifier shape
# (kb/sandbox/loadtest/stepup.py): only measured numbers + public GCP shape identifiers,
# never an internal cluster/namespace/project name.
#
# INERT until a step-up sweep result is emitted (no render_stepup wiring yet — the public
# page framing is tracked separately and needs real measured data first). Same closed-schema
# discipline: a Pareto block renders ONLY these field-names, each validated by its predicate;
# anything else is dropped on read, so an accrual writer cannot smuggle free-text onto the
# public page.

# Saturation verdicts (mirror of the internal classifier's closed set). A verdict not in
# this set drops the whole block (fail-closed) rather than render an unknown label.
STEPUP_VERDICTS = {
    "flat-through-sweep",  # every measured step stayed under the North Star
    "degrading",  # at least one step breached the North Star, none collapsed
    "saturated",  # at least one step crossed the collapse band
    "no-measured-steps",  # every step was unmeasured (infra/scrape failure, honest)
}


# One Pareto point per swept rate. offered_rate_per_s + ttfe_p95_ms are REQUIRED (the x-axis
# and the honesty spine); the rest are OPTIONAL (a partial Prometheus scrape yields a partial
# point honestly, never a fabricated 0). A point missing a required field, or whose value
# fails its predicate, drops the whole block — a malformed sweep degrades to no Pareto table,
# never to a leak or a fake curve.
def _stepup_points_ok(v):
    if not isinstance(v, list) or not v:
        return False
    for p in v:
        if not isinstance(p, dict):
            return False
        rate = p.get("offered_rate_per_s")
        if not (isinstance(rate, int) and not isinstance(rate, bool) and 0 < rate < 100000):
            return False
        p95 = p.get("ttfe_p95_ms")
        if not (isinstance(p95, (int, float)) and not isinstance(p95, bool) and p95 >= 0):
            return False
        # Optional numeric axes — if present they must be non-negative numbers; a bad value
        # invalidates the point (and thus the block) rather than rendering garbage.
        for opt in ("ready_per_s", "ttfe_p50_ms", "ttfe_p99_ms", "cost_usd_per_1k_ready"):
            if opt in p:
                ov = p[opt]
                if not (isinstance(ov, (int, float)) and not isinstance(ov, bool) and ov >= 0):
                    return False
    return True


# The controller-startup LOWER-BOUND proxy block (#3975). True TTFE (claim-admission ->
# first-reconcile -> Ready) has no upstream production stamp on current main, so the true-TTFE
# `pareto_points` table is honestly EMPTY while the gap is open. This SEPARATE block surfaces
# the controller-stamped startup latency (controller-first-observed -> Ready) as an explicit
# LOWER BOUND: it EXCLUDES the claim-admission->first-reconcile queueing lag, so it under-reports
# true TTFE. `lower_bound: true` is REQUIRED and load-bearing — render keys the fixed lower-bound
# caveat boilerplate off it, so the public page can never present this proxy as a true TTFE
# measurement. The free-text caveat the internal producer carries is render-owned and NEVER rides
# the public schema. Per point: offered_rate_per_s + controller_startup_p95_ms REQUIRED (the
# x-axis + proxy honesty spine); the p50/p99 percentiles + controller_ready_per_s OPTIONAL
# (partial scrape -> partial point honestly). A malformed point drops the whole block.
def _stepup_controller_ok(v):
    if not isinstance(v, dict):
        return False
    if v.get("lower_bound") is not True:
        return False
    pts = v.get("pareto_points")
    if not isinstance(pts, list) or not pts:
        return False
    for p in pts:
        if not isinstance(p, dict):
            return False
        rate = p.get("offered_rate_per_s")
        if not (isinstance(rate, int) and not isinstance(rate, bool) and 0 < rate < 100000):
            return False
        p95 = p.get("controller_startup_p95_ms")
        if not (isinstance(p95, (int, float)) and not isinstance(p95, bool) and p95 >= 0):
            return False
        for opt in ("controller_startup_p50_ms", "controller_startup_p99_ms", "controller_ready_per_s"):
            if opt in p:
                ov = p[opt]
                if not (isinstance(ov, (int, float)) and not isinstance(ov, bool) and ov >= 0):
                    return False
    # Optional proxy saturation verdict — same closed set as the true-TTFE verdict.
    if "verdict" in v and v["verdict"] not in STEPUP_VERDICTS:
        return False
    return True


# Optional positive-int rate field (a breach/saturation/flat rate). The internal classifier
# carries None when there is no breach; the emitter drops None, so the predicate only has to
# validate the present-and-positive case.
_pos_int = lambda v: isinstance(v, int) and not isinstance(v, bool) and 0 < v < 100000


# The operator-facing SATURATION POINT block (a#3960 #4030). alex's headline ask: the max
# sustained creation rate (offered sb/sec) holding TTFE p95 under the human "under 1s" (tight)
# and "under 5s" (loose) bars, split by leg — warm-pool hit vs cold-provision (node overflow).
# Distinct from the 500ms/2000ms methodology bands above (which stay on the characteristic-rate
# fields for the Pareto/study story). tight_ms + loose_ms are REQUIRED positive bar floats;
# basis is an OPTIONAL non-empty descriptor string. Each leg (warm/cold) is OPTIONAL; when
# present its max_rate_under_{tight,loose} is a positive int OR None (honest "no swept rate met
# that bar" — render prints em-dash, never a fabricated 0). At least one leg must carry at least
# one present rate, else the block is honest "nothing" and is dropped (fail-closed).
def _stepup_saturation_point_ok(v):
    if not isinstance(v, dict):
        return False
    for bar in ("tight_ms", "loose_ms"):
        b = v.get(bar)
        if not (isinstance(b, (int, float)) and not isinstance(b, bool) and b > 0):
            return False
    if "basis" in v and not (isinstance(v["basis"], str) and v["basis"]):
        return False
    legs = [l for l in ("warm", "cold") if l in v]
    if not legs:
        return False
    any_rate = False
    for l in legs:
        leg = v[l]
        if not isinstance(leg, dict):
            return False
        for k in ("max_rate_under_tight", "max_rate_under_loose"):
            if k in leg:
                rv = leg[k]
                if rv is None:
                    continue  # honest "bar unmet" — valid, renders em-dash
                if not (isinstance(rv, int) and not isinstance(rv, bool) and 0 < rv < 100000):
                    return False
                any_rate = True
    return any_rate


STEPUP_PARETO_FIELDS = {
    # The true-TTFE Pareto table. OMITTED (not emitted as []) when no step measured a true
    # TTFE warm p95 — the #3975 gap — in which case the controller_startup proxy below carries
    # the only table. When PRESENT it is non-empty by predicate; the emitter requires the union
    # of {pareto_points, controller_startup} to be non-empty (no all-empty stepup block).
    "pareto_points": _stepup_points_ok,
    "verdict": lambda v: v in STEPUP_VERDICTS,
    # The operator saturation-point headline (2×2 warm/cold × 1s/5s) — see above (#4030).
    "saturation_point": _stepup_saturation_point_ok,
    # The controller-startup LOWER-BOUND proxy block (#3975) — see _stepup_controller_ok.
    "controller_startup": _stepup_controller_ok,
    # The three characteristic rates (all optional — absent when the curve never crossed that
    # band). north_star_breach_rate = first rate with p95 >= 500ms; saturation_rate = first
    # rate with p95 >= 2000ms; max_flat_rate = highest rate still under the North Star.
    "north_star_breach_rate": _pos_int,
    "saturation_rate": _pos_int,
    "max_flat_rate": _pos_int,
    # Sweep parameters (Little's-law inputs) — public-safe scalars.
    "sld_s": _nonneg,  # sandbox life duration (s)
    "wpr": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and 0.0 <= v <= 1.0,
    "node_count": lambda v: isinstance(v, int) and not isinstance(v, bool) and 0 < v < 10000,
    "machine_type": lambda v: isinstance(v, str) and bool(_MACHINE_TYPE.match(v)),
    # measured_at: ISO-8601-ish instant the sweep ran (non-empty string), same as scale_proof.
    "measured_at": lambda v: isinstance(v, str) and bool(v),
}

# --- #3942: Kata + microVM activation latency block (TOP-LEVEL kata_activation object) ----
# The Kata+microVM fire measured pod-Ready / microVM-activation latency — NOT TTFE. The Core
# Metrics matrix above is keyed on TTFE (executed-first-instruction) BY CONTRACT, so dropping a
# Ready/activation number into a ttfe_* column would contradict the honesty spine — the Kata TTFE
# matrix cells therefore stay honestly `pending` (no Kata TTFE-exec was measured). This SEPARATE
# additive block surfaces the Ready/activation numbers we DO have, with the Ready-not-TTFE
# distinction carried IN the render caption (not just the schema key) so a reader cannot conflate
# it with the TTFE columns. Resume-from-suspend renders N/A upstream-blocked (CRIU resume not
# wired upstream) — a genuine upstream gap, not an unrun test.
#
# Same closed-schema discipline as warm_vs_cold/scale_proof/stepup: the block renders ONLY these
# field-names, each validated by its predicate; anything else is dropped on read, so an accrual
# writer cannot smuggle free-text (an internal node/cluster/pool name) onto the public page. The
# risky free-text fields are enum- or regex-bounded: hypervisor against a PUBLIC hypervisor enum,
# resume_status against a closed enum, kernels/version/image against tight shape regexes that
# forbid registry paths and arbitrary text.

# Public hypervisor names only — an internal/free-text value drops the whole block (fail-closed).
KATA_HYPERVISORS = {"Cloud Hypervisor", "QEMU", "Firecracker", "Dragonball", "Stratovirt"}
# Resume status is a closed enum, never free text. "upstream-blocked" = CRIU resume not wired
# upstream (a genuine upstream gap), the only state measured today.
KATA_RESUME_STATUSES = {"upstream-blocked"}
# Kernel release shape: MAJOR.MINOR.PATCH with an optional vendor suffix (e.g. 6.18.35 or
# 6.8.0-1054-gke). Tightly bounded so an internal node/pool name can never ride a kernel field.
_KERNEL = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z._-]+)?$")
# Kata version shape: bare semantic version (e.g. 3.32.0). No suffix, no free text.
_KATA_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
# Kata image shape: a PUBLIC base-image ref with NO registry path (forbids `/`) — e.g. debian:12,
# ubuntu:24.04. Deliberately stricter than provenance _IMAGE (which allows registry paths for the
# controller image): a base-image name is a short public tag, so forbidding `/` keeps any internal
# AR project path off the page by construction.
_KATA_IMAGE = re.compile(r"^[a-z0-9][a-z0-9._-]*:[A-Za-z0-9][A-Za-z0-9._-]*$")


# Per cold-start entry: image (required, public base-image shape) + ready_ms (required, nonneg
# total-to-Ready) + image_pull_ms (OPTIONAL nonneg). A non-empty list is required; a malformed
# entry (bad image shape, missing/invalid ready_ms, bad optional pull) drops the whole block —
# a malformed fire degrades to no Kata block, never to a leak or a partial lie.
def _kata_cold_ready_ok(v):
    if not isinstance(v, list) or not v:
        return False
    for e in v:
        if not isinstance(e, dict):
            return False
        img = e.get("image")
        if not (isinstance(img, str) and bool(_KATA_IMAGE.match(img))):
            return False
        rm = e.get("ready_ms")
        if not (isinstance(rm, (int, float)) and not isinstance(rm, bool) and rm >= 0):
            return False
        if "image_pull_ms" in e:
            pm = e["image_pull_ms"]
            if not (isinstance(pm, (int, float)) and not isinstance(pm, bool) and pm >= 0):
                return False
    return True


KATA_ACTIVATION_FIELDS = {
    # REQUIRED spine.
    "runtime_class": lambda v: v in RUNTIME_LABELS,
    "microvm_activation_ms": _nonneg,
    "warm_ready_ms": _nonneg,
    "cold_ready": _kata_cold_ready_ok,
    "guest_kernel": lambda v: isinstance(v, str) and bool(_KERNEL.match(v)),
    "host_kernel": lambda v: isinstance(v, str) and bool(_KERNEL.match(v)),
    # OPTIONAL.
    "warm_image": lambda v: isinstance(v, str) and bool(_KATA_IMAGE.match(v)),
    "hypervisor": lambda v: v in KATA_HYPERVISORS,
    "resume_status": lambda v: v in KATA_RESUME_STATUSES,
    "kata_version": lambda v: isinstance(v, str) and bool(_KATA_VERSION.match(v)),
    "n": lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 0,
    "measured_at": lambda v: isinstance(v, str) and bool(v),
}

# --- #4021: concurrent-burst sweep block (TOP-LEVEL concurrent_burst object) --------------
# The step-up Pareto / Scale Proof blocks above sweep the per-SECOND offered RATE. This block
# sweeps the orthogonal axis customers actually ask about for warm-pool sizing: a single
# all-at-once CONCURRENT burst of N claims against one warm pool (and the cold baseline), at
# two concurrency levels (e.g. 300, 500) × two activation modes (warm / cold). Each leg
# carries the SAME TTFE honesty spine as the Core Metrics matrix — executed-first-instruction
# p50/p95, the per-node throughput-under-bar rates, and the exec-success honesty check — so a
# reader compares the burst numbers to the matrix on identical terms. The headline finding it
# surfaces: warm-pool activation stays an order of magnitude faster than cold even when a
# 300/500-way burst serializes the controller bind+exec path past the sub-second bar.
#
# Same closed-schema discipline as warm_vs_cold/scale_proof/stepup/kata: only these field-names
# render, each validated by its predicate; anything else is dropped on read, so a fire result
# cannot smuggle free-text onto the public page. Each leg's spine (n, mode, ttfe_p50_ms,
# ttfe_p95_ms) is REQUIRED; the throughput + exec fractions are OPTIONAL (a missing one renders
# em-dash, never a fabricated 0). A non-empty legs list is REQUIRED; a malformed leg fails the
# whole block CLOSED (no partial-lie table). mode is a closed enum, never free text.
CONCURRENT_BURST_MODES = {"warm", "cold"}


def _concurrent_burst_leg_ok(v):
    if not isinstance(v, dict):
        return False
    nc = v.get("n")
    if not (isinstance(nc, int) and not isinstance(nc, bool) and 0 < nc < 100000):
        return False
    if v.get("mode") not in CONCURRENT_BURST_MODES:
        return False
    for req in ("ttfe_p50_ms", "ttfe_p95_ms"):
        if not _nonneg(v.get(req)):
            return False
    for opt in ("thpt_under_5s_per_node", "thpt_under_1s_per_node"):
        if opt in v and not _nonneg(v[opt]):
            return False
    if "exec_success_rate" in v:
        esr = v["exec_success_rate"]
        if not (isinstance(esr, (int, float)) and not isinstance(esr, bool) and 0.0 <= esr <= 1.0):
            return False
    return True


def _concurrent_burst_legs_ok(v):
    if not isinstance(v, list) or not v:
        return False
    return all(_concurrent_burst_leg_ok(leg) for leg in v)


CONCURRENT_BURST_FIELDS = {
    # REQUIRED: a non-empty list of per-leg cells (each: n, mode, ttfe_p50_ms, ttfe_p95_ms
    # required; thpt_under_{5s,1s}_per_node + exec_success_rate optional). A malformed leg
    # fails the whole block closed.
    "legs": _concurrent_burst_legs_ok,
    # OPTIONAL provenance scalars — public-safe.
    "node_count": lambda v: isinstance(v, int) and not isinstance(v, bool) and 0 < v < 10000,
    "machine_type": lambda v: isinstance(v, str) and bool(_MACHINE_TYPE.match(v)),
    "measured_at": lambda v: isinstance(v, str) and bool(v),
}

# --- #4083: warm-pool ACQUISITION-latency block (TOP-LEVEL warm_pool_acquisition object) ----
# The concurrent_burst / step-up blocks above report full TTFE (claim → executed-first-
# instruction). This block reports a DECOMPOSED SUB-PHASE of that path: the warm-pool
# ACQUISITION latency — the time from SandboxClaim requested to bound (a ready warm sandbox
# handed to the caller), measured per-claim by the step-up harness's acquisition watch-timer
# (#1043). It EXCLUDES the exec-attach + first-instruction round-trip that the TTFE legs
# include, so acquisition p95 is NOT comparable to the concurrent_burst/matrix TTFE columns —
# it is the earlier, isolated "how fast does the pool hand me a sandbox" number a warm-pool
# operator sizes against. Rendered as its own block with an explicit not-comparable caveat.
#
# Same closed-schema discipline as concurrent_burst/warm_vs_cold/scale_proof/stepup/kata: only
# these field-names render, each validated by its predicate; anything else is dropped on read,
# so a fire result cannot smuggle free-text onto the public page. The REQUIRED spine is
# runtime_class (PUBLIC enum, fail-closed) + acq_p50_ms + acq_p95_ms + n; the p99, the offered
# rate, the warm-pool size, the controller-startup lower-bound proxy, and the GCP shape
# scalars are OPTIONAL (a partial fire renders a partial-but-honest block, never a fabricated
# 0). controller_startup_p95_ms is an explicit LOWER-BOUND proxy (controller-first-observed →
# Ready, excludes the claim-admission → first-reconcile queueing lag) — render keys a fixed
# caveat off its presence, mirroring the step-up proxy discipline (#3975).
WARM_POOL_ACQUISITION_FIELDS = {
    # REQUIRED spine.
    "runtime_class": lambda v: v in RUNTIME_LABELS,
    "acq_p50_ms": _nonneg,
    "acq_p95_ms": _nonneg,
    "n": _pos_int,
    # OPTIONAL — decomposition + provenance, all public-safe.
    "acq_p99_ms": _nonneg,
    "offered_rate_per_s": _pos_int,
    "warmpool_size": _pos_int,
    "controller_startup_p95_ms": _nonneg,
    "machine_type": lambda v: isinstance(v, str) and bool(_MACHINE_TYPE.match(v)),
    "node_count": lambda v: isinstance(v, int) and not isinstance(v, bool) and 0 < v < 10000,
    "measured_at": lambda v: isinstance(v, str) and bool(v),
}
