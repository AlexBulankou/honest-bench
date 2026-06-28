"""Closed-schema allow-list for the public honest-benchmarks render (Layer-1 PII guard).

This is the PRIMARY guard: the renderer emits ONLY field-names + types declared here.
Anything not in the schema is DROPPED, so no harness free-text can reach the public page
by construction. The Layer-2 denylist scanners (2a public / 2b our-side codename) are the
belt-and-suspenders backstop, not the primary defense.
"""

import re

PRODUCTS = {"sandbox", "substrate"}

# cluster-substrate VALUE is allowed (generic GKE feature names); the internal
# scenario/demo cluster NAMES are never a value here (Layer-2 denies those by pattern).
CLUSTER_SUBSTRATES = {"kind", "gke", "gke-sandbox"}

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

# pending/FAIL cells render an ENUM reason only — never harness free-text.
PENDING_REASONS = {
    "requires-gvisor-runtime",
    "requires-gke",
    "not-yet-measured",
    "upstream-blocked",
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
