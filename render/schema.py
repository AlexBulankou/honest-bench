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

# warm_regime (#103/#111) is the cluster-contention regime a warm-tier result was measured
# under. "drained" = a low-contention, drained cluster (single fire, few claims) — a green
# warm tier is honest THIS fire but not yet a sustained North-Star claim; "under-load" =
# measured under representative contention. Data-keyed (not static prose) so the caveat
# cannot rot: once an under-load fire clears the bar, the drained caveat stops rendering by
# construction. A closed set mirroring the harness side (WARM_REGIME_ENUM). The render guard
# is SECONDARY — the harness emitter fail-closes on a typo'd value — so here an out-of-enum
# value is dropped, and an absent value renders no caveat (graceful degradation).
WARM_REGIMES = {"drained", "under-load"}

# warm_scaling_term (#4137) names WHICH term drives warm-hit TTFE growth as claim-count
# climbs on the drained regime. The #119 N=30 fire showed warm-hit TTFE p50 tripling with N
# (bind 233→663→792ms across N5→N30→N35) while exec stayed flat (202→242ms) — i.e. the
# scaling is in provisioning/bind concurrency on the fixed drained node-set, not in the exec
# channel. This closed enum lets the drained caveat NAME that term on the page itself (not
# just the PR body), so a reader understands *why* the warm-hit distribution straddles 1s at
# higher N. Data-keyed like WARM_REGIMES: it ONLY renders alongside a drained caveat (it
# qualifies that caveat), and it is a closed set so the attribution can't rot into free-text.
# A closed set mirroring the harness side (WARM_SCALING_TERM_ENUM). The render guard is
# SECONDARY — the harness emitter fail-closes on a typo'd value — so here an out-of-enum
# value is dropped, and an absent value renders no scaling clause (graceful degradation).
WARM_SCALING_TERMS = {"bind-concurrency"}

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
    # A cell whose run DID land but whose number is a node-pool topology artifact, not a
    # runtime property: N concurrent microVM boots contend for the single pool node's
    # vCPUs, stalling the marginal replica (same root-cause class as the burst_create
    # kata exclusion in harness/scenario_map.py). A representative number needs a pool
    # sized for N concurrent warms — a deliberate spend action, not a re-run. Render-only
    # (committed-artifact reclassification path, PR-documented + peer-reviewed); no
    # harness emit path.
    "pool-topology-constrained",
    # hb#132 dual-throughput: the per-node throughput half of a cell has landed, but the
    # validated per-cluster@X figure awaits OUR own schema-validated saturation fire. The
    # cluster half renders `pending (cluster-fire)` until that fire carries the
    # thpt_*_per_cluster fields. Added to the harness PENDING_REASON_ENUM too (so a future
    # below-bar / in-progress cluster leg can EMIT it), guarded by the cross-contract subset
    # test in harness/test_scenario_portability.py.
    "cluster-fire",
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
    # warm_regime (#103/#111): the cluster-contention regime the warm tier was measured
    # under; drives the drained-cluster caveat under the warm bind/exec decomposition.
    "regime": lambda v: v in WARM_REGIMES,
    # warm_scaling_term (#4137): names the term driving warm-hit TTFE growth with claim-count
    # on the drained regime; renders as a clause ON the drained caveat (only when regime is
    # drained), so the page explains why warm-hit straddles 1s at higher N.
    "warm_scaling_term": lambda v: v in WARM_SCALING_TERMS,
    # Goal-2.1 matrix: which isolation runtime this run measured (runtimeClassName). Drives
    # the matrix's Runtime column; absent ⇒ the renderer defaults to gvisor (today's only
    # live runtime — the gke-sandbox/runsc path).
    "runtime": lambda v: v in RUNTIME_LABELS,
    # Node machine shape (same tight GCP-shape regex the per-block provenance uses).
    # Rendered only where explicitly listed (the kata separate-run footnote) — the main
    # build banner's explicit key list does not include it, so the primary banner is
    # unchanged by this addition.
    "machine_type": lambda v: isinstance(v, str) and bool(_MACHINE_TYPE.match(v)),
    # vCPU-footprint axis (#3868): the per-sandbox DECLARED cpu/mem request (millicores +
    # MiB, whole non-negative ints) the density figures were measured under — a run-level
    # property of the runtime, not a per-scenario measurement. Rendered only where explicitly
    # listed (the vCPU-footprint DETAILS section), like machine_type — so the main banner is
    # unchanged. `bool` is excluded because bool is an int subclass (True would pass an int
    # predicate); a footprint is a count, never a flag.
    "sandbox_cpu_request_m": lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 0,
    "sandbox_mem_request_mib": lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 0,
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
    # suspend_resume administrative-suspend latency axis (#3868): median suspend latency +
    # p90 tail. Measures the operatingMode=Suspended patch → terminal-Suspended cost — an
    # ADMINISTRATIVE (operator-driven) suspend, NOT an idle/auto-suspend (upstream has none).
    # Inert until the scenario emits a completed suspend leg.
    "suspend_latency_ms": "Suspend latency (ms)",
    "suspend_p90_ms": "Suspend p90 (ms)",
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

# hb#174: closed vocabulary for thpt_slo_basis — which measured basis produced a derived
# per-cluster SLO triple. Independent mirror of the harness enum (slo_rate.SLO_BASIS_ENUM);
# drift is caught by the cross-contract test, not by an import.
SLO_BASIS_VALUES = frozenset(
    (
        "true_ttfe",
        "literal_ttfe_upper_bound+controller_completed",
        "literal_ttfe_upper_bound+acq_fulfilled",
    )
)

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
    # hb#132 dual-throughput: the validated per-cluster@X companions to the per-node figures.
    # OPTIONAL — a cell whose per-node half has landed renders `<node> /node · pending
    # (cluster-fire)` until OUR schema-validated saturation fire carries these. INERT until
    # then (no matrix cell shows a cluster number without them). thpt_cluster_node_count is the
    # X in the "@X nodes" caption; a MEASURED per-cluster figure, never a node×X extrapolation.
    "thpt_under_5s_per_cluster": _nonneg,
    "thpt_under_1s_per_cluster": _nonneg,
    "thpt_cluster_node_count": _nonneg,
    # hb#174: which measured basis produced the per-cluster triple. Closed 3-value enum —
    # the render-side mirror of harness slo_rate.SLO_BASIS_ENUM / results_schema.SLO_BASIS_ENUM
    # (render never imports the harness; a cross-contract test asserts the three copies match).
    # true_ttfe is the default basis and renders nothing extra; the two literal upper-bound
    # bases key the per-runtime disclosure caption in render_matrix.
    "thpt_slo_basis": lambda v: v in SLO_BASIS_VALUES,
    # hb#174 sign-off (c): MIN warm-exec sample count across the rungs credited by a
    # literal-basis derivation — the harness floor (>= 20) makes sub-20 unreachable from a
    # valid producer, but the render predicate only asserts a positive int (the render-side
    # job is the coarse-p95 caption when 20 <= n < 100, not re-enforcing the floor).
    "thpt_slo_n_exec_ok": lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 1,
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

# --- inch #1: warm-pool TTFE decomposition block (warmpool_cold_start sla_metrics) --------
# The warm-pool-hit TTFE is create->first-instruction-result; it splits into BIND
# (create->bound, i.e. provisioning) + EXEC (websocket setup + the first-instruction
# round-trip). When the warm-hit p50/p95 sits above the <1s North Star, this block shows
# WHERE the time lives: a bind_p50 near ttfe_p50 means provisioning dominates (a real
# controller/clone target); a small bind_p50 with a large exec_p50 means the exec channel
# (websocket setup) dominates (a harness/product artifact, not a controller regression).
#
# HONESTY: bind, exec, and TTFE are each an INDEPENDENTLY-MEASURED percentile of its own
# per-claim distribution — exec is measured per-claim as (ttfe_ms - bind_ms) for the SAME
# claim, then percentiled by the producer, NOT derived here as p50(ttfe)-p50(bind)
# (percentiles do not subtract linearly). The render displays the three measured rows as-is;
# they need NOT sum (bind_p50 + exec_p50 != ttfe_p50 in general), and the footnote says so.
#
# The render block is INERT by construction — it fires ONLY when ALL of the bind, exec, AND
# TTFE percentile pairs are present, so today's pre-decomposition data renders nothing and the
# public page is byte-unchanged until a fire emits bind_p50_ms/exec_p50_ms. Same closed-schema
# discipline: only these keys render, each validated. Diagnostic-only — adds a block, changes
# no existing cell.
WARM_BIND_FIELDS = {
    "bind_p50_ms": _nonneg,
    "bind_p95_ms": _nonneg,
    "exec_p50_ms": _nonneg,
    "exec_p95_ms": _nonneg,
    "ttfe_p50_ms": _nonneg,
    "ttfe_p95_ms": _nonneg,
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


# Stepup-family offered rate: any positive finite number < 100000 (hb#189). Fractional rungs
# are first-class producer-side (kata 0.5/1.5 per_s; sub-refill credit ladders need midpoints
# like 1.5), and the harness normalizes at ingest (integral -> int, fractional -> float), so
# render only validates — it accepts both without normalizing.
def _stepup_rate_ok(rate):
    if isinstance(rate, bool) or not isinstance(rate, (int, float)):
        return False
    return rate == rate and 0 < rate < 100000


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
        if not _stepup_rate_ok(p.get("offered_rate_per_s")):
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
        if not _stepup_rate_ok(p.get("offered_rate_per_s")):
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
# present its max_rate_under_{tight,loose} is a positive rate (int, or fractional float on a
# fractional ladder — hb#189) OR None (honest "no swept rate met
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
                if not _stepup_rate_ok(rv):
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
    # These are values FROM the swept ladder, so a fractional ladder yields fractional
    # characteristic rates — same relaxed predicate as the per-point rate (hb#189).
    "north_star_breach_rate": _stepup_rate_ok,
    "saturation_rate": _stepup_rate_ok,
    "max_flat_rate": _stepup_rate_ok,
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

# Session-turnover (warm-pool refill under sustained churn) block — SCENARIO cell
# `session_turnover`. Measures the full claim → use → release → reclaim loop: after each claim is
# released the controller must REPLENISH the warm pool, and this block reports how long that refill
# takes under sustained cycling. refill_latency_ms (median) is the REQUIRED spine — the INERT gate —
# so the block renders nothing until a real fire emits a completed-cycle measurement; refill_p90_ms
# (the tail) is OPTIONAL. Every value is a GENUINELY-MEASURED latency from the scenario's per-cycle
# refill samples — no fabricated 0, no recommendation without a fire behind it. Same closed-schema
# discipline: only these keys render, each validated; anything else in sla_metrics is dropped on read.
# NOTE: the cycle count `n` is NOT here — the scenario emits it under the reserved "n" key, which the
# run loop LIFTS out of sla_metrics to the top-level scenario field (harness/run.py) before coercion,
# so the renderer reads it from the scenario dict's top-level `n`, not from sla_metrics.
SESSION_TURNOVER_FIELDS = {
    "refill_latency_ms": _nonneg,
    "refill_p90_ms": _nonneg,
}

# Administrative-suspend latency block — SCENARIO cell `suspend_resume`. Measures the wall-clock
# cost of a DELIBERATE administrative suspend: the operatingMode=Suspended patch return → terminal
# Suspended state (backing Pod released + the Suspended condition observed). This is the real
# response time of the cost-lever an operator pulls to reclaim a Sandbox's compute. It is NOT an
# idle/auto-suspend latency — upstream agent-sandbox has NO idle-timeout / activity-reclaim path;
# operatingMode is the closed Running;Suspended enum, toggled only by an explicit patch. The render
# side carries that capability note as a static line so nobody reads an auto-suspend into the number.
# suspend_latency_ms (median) is the REQUIRED spine — the INERT gate — so the block renders nothing
# until a real fire emits a completed suspend leg; suspend_p90_ms (the tail) is OPTIONAL (emitted
# only when the fire ran n>=2 cycles). Every value is a GENUINELY-MEASURED latency from the
# scenario's per-cycle suspend samples — no fabricated 0, no note without a fire behind the number.
# Same closed-schema discipline: only these keys render, each validated; anything else in sla_metrics
# (the resume TTFE pair, pending_reason, n) is dropped on read of THIS block.
SUSPEND_LATENCY_FIELDS = {
    "suspend_latency_ms": _nonneg,
    "suspend_p90_ms": _nonneg,
}

# --- at-scale-under-contention RETRACTION block (TOP-LEVEL at_scale_contention object) -------
# The concurrent_burst block above reports 1:1 warm bursts (N ready sandboxes hit with N
# claims). This block is the deliberate COUNTER-POINT: a single measured operating point where
# the warm pool is OVER-SUBSCRIBED (more concurrent claims than pool members), so the "warm hit
# is sub-second" claim from the Core Metrics matrix does NOT hold. It publishes the ceiling
# honestly rather than only the flattering 1:1 numbers. pool_size + claim_count are REQUIRED (the
# contention ratio is render-DERIVED from them, never a free-text field); ttfe_p50/p95_ms are the
# REQUIRED latency spine (node-count-INDEPENDENT, so comparable to the matrix/burst TTFE columns).
# bind_p50/p95_ms + exec_p50/p95_ms + exec_success_rate decompose the path; node_count +
# machine_type + measured_at are provenance. There is DELIBERATELY no per-node throughput field:
# this point was measured at node_count=1, so a per-node throughput would invite a dishonest
# cross-block comparison against concurrent_burst's node_count=20 legs — latency is
# node-count-independent, per-node throughput is not, so only latency crosses the block boundary.
# Same closed-schema discipline as every block above: only these field-names render, each
# validated by its predicate; anything else is dropped on read. runtime_class validates against
# the PUBLIC RUNTIME_LABELS enum (fail-closed on an out-of-enum runtime).
AT_SCALE_CONTENTION_FIELDS = {
    # REQUIRED spine: the retraction point — warm activation under an over-subscribed pool.
    "runtime_class": lambda v: v in RUNTIME_LABELS,
    "pool_size": _pos_int,
    "claim_count": _pos_int,
    "ttfe_p50_ms": _nonneg,
    "ttfe_p95_ms": _nonneg,
    # OPTIONAL — bind/exec decomposition + provenance, all public-safe.
    "bind_p50_ms": _nonneg,
    "bind_p95_ms": _nonneg,
    "exec_p50_ms": _nonneg,
    "exec_p95_ms": _nonneg,
    "exec_success_rate": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and 0.0 <= v <= 1.0,
    "node_count": lambda v: isinstance(v, int) and not isinstance(v, bool) and 0 < v < 10000,
    "machine_type": lambda v: isinstance(v, str) and bool(_MACHINE_TYPE.match(v)),
    "measured_at": lambda v: isinstance(v, str) and bool(v),
}

# --- cluster-scale SATURATION block (TOP-LEVEL cluster_saturation object) --------------------
# The third cluster-scale question, distinct from the two above. concurrent_burst reports 1:1
# warm bursts at small N; at_scale_contention reports the OVER-subscribed (claims > pool) ceiling
# at node_count=1. This block reports a 1:1 ALL-WARM fire (pool == claim, NOT over-subscribed)
# driven to CLUSTER SATURATION — a large claim burst spread across many nodes, where the bind path
# saturates even though every claim has a ready warm pool member. It is the honest ceiling for
# "how fast can the whole cluster hand out warm sandboxes at once", carrying the MEASURED
# per-cluster throughput triple (never a per-node × N extrapolation, which is fiction above the
# controller reconcile ceiling): thpt_under_{5s,1s}_per_cluster are gated on thpt_cluster_node_count
# in the SAME object (the coupled-triple rule the matrix uses — a per-cluster figure with no
# measurement size to disclose is meaningless). node_count is REQUIRED here (unlike
# at_scale_contention's node_count=1 point) because a per-cluster throughput is only meaningful
# against the node count it was measured at. ttfe_p50/p95_ms are the latency spine
# (node-count-INDEPENDENT, so comparable to the matrix/burst TTFE columns). outcome is carried so
# the FAIL ceiling is headlined honestly, not softened into a green number. runtime_class validates
# against the PUBLIC RUNTIME_LABELS enum (fail-closed). Same closed-schema discipline as every
# block above: only these field-names render, each validated by its predicate; anything else is
# dropped on read.
CLUSTER_SATURATION_FIELDS = {
    # REQUIRED spine: the saturation operating point + its measured per-cluster throughput triple.
    "runtime_class": lambda v: v in RUNTIME_LABELS,
    "pool_size": _pos_int,
    "claim_count": _pos_int,
    "node_count": lambda v: isinstance(v, int) and not isinstance(v, bool) and 0 < v < 10000,
    "ttfe_p50_ms": _nonneg,
    "ttfe_p95_ms": _nonneg,
    "thpt_under_5s_per_cluster": _nonneg,
    "thpt_under_1s_per_cluster": _nonneg,
    "thpt_cluster_node_count": _nonneg,
    # OPTIONAL — per-node throughput halves, bind/exec decomposition, outcome + provenance.
    "thpt_under_5s_per_node": _nonneg,
    "thpt_under_1s_per_node": _nonneg,
    "bind_p50_ms": _nonneg,
    "bind_p95_ms": _nonneg,
    "exec_p50_ms": _nonneg,
    "exec_p95_ms": _nonneg,
    "exec_success_rate": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and 0.0 <= v <= 1.0,
    "outcome": lambda v: v in OUTCOMES,
    "run_id": lambda v: isinstance(v, str) and bool(_RUNID.match(v)),
    "machine_type": lambda v: isinstance(v, str) and bool(_MACHINE_TYPE.match(v)),
    "measured_at": lambda v: isinstance(v, str) and bool(v),
}


# --- #4086 sibling: provisioning-rate-sweep block (TOP-LEVEL provisioning_rate_sweep object) ---
# The honest reconcile-throughput ceiling: for each offered warm-pool provisioning rate
# (sandboxes/sec), what fraction of the pool reached Ready WITHIN pool_warm_timeout. This is a
# THIRD, distinct axis — NOT the step-up TTFE Pareto (per-claim latency at a fixed pool) and NOT
# at_scale_contention (claim:pool ratio). It measures convergence-vs-offered-rate, so folding it
# into either of those would falsely imply same-regime measurement (an honesty violation, #4086).
# Produced only by the heavy, manual, collision-acked reconcile rate-sweep (out-of-process); the
# daily single-node auto-refresh never produces one, so the render side is INERT until the object
# appears and the block is carried forward across the daily refresh (mirrors scale_proof #3952).
#
# Closed-schema discipline (Layer-1 PII guard): the block renders ONLY these field-names, each
# validated by its predicate; anything else is dropped on read. runtime_class is validated against
# the PUBLIC RUNTIME_LABELS enum (NOT a bare non-empty string) so an out-of-enum/free-text runtime
# can never reach the public page. Each rate point requires offered_rate_per_s (positive int) and
# ready_pct (0..100); warmpool_size / elapsed_s / timeout_s / converged are optional per-point
# color. ceiling_low_per_s / ceiling_high_per_s bound the converge→over-subscribe knee.
def _rate_points_ok(v):
    if not isinstance(v, list) or not v:
        return False
    for p in v:
        if not isinstance(p, dict):
            return False
        rate = p.get("offered_rate_per_s")
        if not (isinstance(rate, int) and not isinstance(rate, bool) and 0 < rate < 100000):
            return False
        pct = p.get("ready_pct")
        if not (isinstance(pct, (int, float)) and not isinstance(pct, bool) and 0.0 <= pct <= 100.0):
            return False
        # warm-pool target for this rate (rate x pool_warm_timeout). Optional per point: present
        # so the table can show the target the pool was sized to, older blocks may omit it.
        wps = p.get("warmpool_size")
        if wps is not None and not (isinstance(wps, int) and not isinstance(wps, bool) and 0 < wps < 100000000):
            return False
        # convergence wall-clock seconds + timeout ceiling: optional color, non-negative when present.
        for k in ("elapsed_s", "timeout_s"):
            if k in p:
                x = p[k]
                if not (isinstance(x, (int, float)) and not isinstance(x, bool) and x >= 0):
                    return False
        # did this rate reach 100% Ready within timeout? Optional bool.
        conv = p.get("converged")
        if conv is not None and not isinstance(conv, bool):
            return False
    return True


PROVISIONING_RATE_SWEEP_FIELDS = {
    "rate_points": _rate_points_ok,
    "runtime_class": lambda v: v in RUNTIME_LABELS,
    # bounded converge->over-subscribe knee: ceiling is in (ceiling_low_per_s, ceiling_high_per_s].
    # Optional (the table alone proves the ceiling); non-negative numerics when present.
    "ceiling_low_per_s": _nonneg,
    "ceiling_high_per_s": _nonneg,
    # measured_at: ISO-8601 instant the sweep ran. Optional, carried forward across the daily
    # refresh (same as scale_proof) so a point-in-time block is honestly dated apart from the
    # daily-refreshed top-level generated_at. Non-empty string only.
    "measured_at": lambda v: isinstance(v, str) and bool(v),
}
