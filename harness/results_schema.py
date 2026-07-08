"""Closed-schema results emitter — the primary public-safety guard.

The honest-benchmark README is machine-rendered from `results/latest.json`.
This module is the *only* writer of that file. It is allow-list-by-construction:
`build_results` copies a fixed set of known field-names+types out of whatever the
scenario loop produced and DROPS everything else, so a scenario that accidentally
surfaces an internal string (a DSN, a cluster name, a raw failure excerpt) cannot
reach the public table. The `scripts/check-public-safety.sh` scanner (2a) is the
backstop; this emitter is the suspenders' belt.

Pure + side-effect-free: no I/O, no cluster, no clock. Offline-testable.
"""

from __future__ import annotations

import re
from numbers import Real

# Closed product vocabulary — render/schema.py's PRODUCTS shares this set. A product
# outside it is a real bug (fail-closed), not a drop. DEFAULT_PRODUCT keeps the
# sandbox suite the zero-arg default so existing callers are unchanged.
PRODUCT_ENUM = ("sandbox", "sandbox-kata", "substrate")
DEFAULT_PRODUCT = "sandbox"

# Closed value-sets. A value outside the set is a real bug, not a leak, so the
# emitter fails closed (raises) rather than silently dropping the field.
# Canonical render vocabulary (render/schema.py is the display contract): PASS/FAIL
# render uppercase, pending lowercase. The scenario bodies return mixed case, so we
# canonicalize on the way in via _OUTCOME_CANON.
OUTCOME_ENUM = ("PASS", "FAIL", "pending")
_OUTCOME_CANON = {"pass": "PASS", "fail": "FAIL", "pending": "pending"}
CLUSTER_SUBSTRATE_ENUM = ("kind", "gke", "gke-sandbox", "gke-kata")
# pending_reason is a FIXED enum — never free text. A pending cell says exactly
# why it is pending, drawn only from this set. Kebab-case to match render's
# PENDING_REASONS exactly (a reason outside render's set renders nothing).
PENDING_REASON_ENUM = (
    "requires-gvisor-runtime",
    "requires-kata-runtime",
    "requires-gke",
    "not-yet-measured",
    "upstream-blocked",
    # hb#132 dual-throughput: a cell's per-node throughput half has landed but the
    # validated per-cluster@X figure awaits its own schema-validated saturation fire.
    # Render pends the cluster half here today; the harness carries the enum so a
    # future below-bar / in-progress cluster leg can EMIT it. Kept in sync with
    # render/schema.py PENDING_REASONS (cross-contract subset test in
    # test_scenario_portability.py).
    "cluster-fire",
)
# Cold-start image-cache posture (#3885). A CLOSED enum, not free text, so the
# render page can honestly label which cold start the published cold_start_ms
# represents: "cold-provision" = controller reconcile + schedule + container
# start on a node that may have the layers cached (the honest upper bound on a
# warm-cached node); "cold-pull" = the same path on a node guaranteed empty of
# the image (e.g. a freshly-created kind cluster), so the measurement also
# includes full layer download. Conservative default is cold-provision (claims
# less); the refresh Action upgrades to cold-pull only where the empty cache is
# proven. Optional in provenance — dropped when absent, fail-closed when present.
COLD_START_MODE_ENUM = ("cold-provision", "cold-pull")

# warm_regime (#103/#111) — a CLOSED enum qualifying the cluster-contention
# regime a warm-tier result was measured under, so a sub-1s warm claim is not
# over-read. "drained" = a low-contention, drained cluster (single fire, few
# claims): a green warm tier THIS fire is honest but is NOT yet a sustained
# North-Star claim and wants corroboration under representative load.
# "under-load" = measured under representative contention, so a green warm tier
# is a durable claim. Data-keyed (not static prose) precisely so the caveat
# cannot rot: once an under-load fire clears the bar, the drained caveat stops
# rendering by construction. Optional in provenance — dropped when absent,
# fail-closed when present.
WARM_REGIME_ENUM = ("drained", "under-load")

# warm_scaling_term (#4137) — a closed enum naming WHICH term drives warm-hit TTFE growth
# as claim-count climbs on the drained regime. The #119 N=30 fire showed warm-hit TTFE p50
# tripling with N via BIND (233→663→792ms across N5→N30→N35) while EXEC stayed flat
# (202→242ms) — i.e. provisioning/bind concurrency on the fixed drained node-set, not the
# exec channel. It qualifies the drained caveat (renders only alongside it), so the public
# page NAMES the scaling term rather than leaving it in the PR body. Data-keyed like
# WARM_REGIME_ENUM: a closed set precisely so the attribution can't rot into free-text.
# Optional in provenance — dropped when absent, fail-closed when present.
WARM_SCALING_TERM_ENUM = ("bind-concurrency",)

# badge_scope (#3905) — a per-SCENARIO closed enum qualifying what a security-isolation
# PASS asserts: "control-plane" = the policy/runtime-class was admitted and correctly
# targeted (NOT data-plane traffic enforcement); "enforced" = data-plane enforcement was
# actually exercised. It renders as a suffix on the scenario's PASS cell so the public
# badge cannot over-claim enforcement. Optional per-scenario — dropped when absent,
# fail-closed when present (a non-enum value is a misconfiguration, not a leak).
BADGE_SCOPE_ENUM = ("control-plane", "enforced")

# badge_construction (#3950) — an ORTHOGONAL per-SCENARIO closed enum disclosing WHICH
# NetworkPolicy mechanism a security-isolation PASS measured: "standard-np" = a standard
# networking.k8s.io/v1 NetworkPolicy built with explicit label propagation, whose podSelector
# binds the tenant pods so a data-plane breach is OBSERVABLE; "managed-np" = a managed
# gke-sandbox NP whose podSelector may select zero pods, so a breach is inert (#2082). It
# renders as a SECOND suffix term on the PASS cell, ONLY alongside a badge_scope — `PASS
# (enforced, standard-np)` — so a future enforced-flip cannot read as a managed-NP guarantee.
# Optional per-scenario — dropped when absent, fail-closed when present (a non-enum value is a
# misconfiguration, not a leak, mirroring badge_scope). Mirrors render/schema.py's
# BADGE_CONSTRUCTIONS; a drift is caught by the cross-contract test, not a shared import.
BADGE_CONSTRUCTION_ENUM = ("standard-np", "managed-np")

# a#3960 step-up saturation verdicts — the emitter's INDEPENDENT copy of render's
# STEPUP_VERDICTS (the two modules deliberately keep separate vocabularies; a drift is
# caught by the cross-contract test, not papered over by a shared import). A verdict outside
# this set invalidates the whole stepup block (-> None) so an unknown label never reaches
# the page.
STEPUP_VERDICT_ENUM = (
    "flat-through-sweep",
    "degrading",
    "saturated",
    "no-measured-steps",
)

# hb#174 SLO-basis stamp — the emitter's INDEPENDENT copy of harness/slo_rate.py's
# SLO_BASIS_ENUM (same deliberate no-shared-import posture as STEPUP_VERDICT_ENUM;
# a drift is caught by the cross-contract test). `thpt_slo_basis` names which measured
# basis produced a derived per-cluster SLO triple, so render can caption a literal-TTFE
# upper-bound cell distinctly from a true-TTFE one. It is the ONLY non-numeric value
# allowed through _coerce_sla_metrics, via an explicit enum-gated carve-out — a value
# outside this set is a real bug (fail-closed raise), never a silent drop or a leak.
SLO_BASIS_ENUM = (
    "true_ttfe",
    "literal_ttfe_upper_bound+controller_completed",
    "literal_ttfe_upper_bound+acq_fulfilled",
    # hb#214 part 1 (DRAFT): the pre-declared floor-rate honest-ZERO basis — always
    # paired with thpt_slo_floor_zero=1 + a 0.0 cluster rate (pairing enforced in
    # _coerce_sla_metrics, fail-closed both directions).
    "literal_ttfe_upper_bound+floor_zero_margin",
    # hb#230 (alex doctrine flip, 2026-07-08): the UNCORROBORATED acq-side basis —
    # gated on acq_p95_s with the controller cross-check DROPPED, so single-source.
    # Consulted only after the corroborated bases derive nothing; emits the Class A
    # *** caveat at render. Rides the enum-gated thpt_slo_basis carve-out (the '+'
    # is fine — the value is never key-regex-validated, only enum-membership-checked).
    "acq_fulfilled+acq_p95_uncorroborated",
    # hb#230 Fork 4 (alex doctrine flip, 2026-07-08): the COLD-START honest-ZERO basis
    # — the controller cold-start floor exceeds BOTH bars at every rate, so 0 is honest
    # at both. Paired with thpt_slo_floor_zero=1 + 0.0 legs (pairing enforced below).
    # Distinct from the warm floor_zero_margin basis: over the controller cold-start
    # distribution, fills BOTH bars, trusted-rung-corroborated.
    "controller_cold_floor_zero_corroborated",
)

# #3954 sibling warm-vs-cold — the emitter's INDEPENDENT copies of render's WARM_VS_COLD_FIELDS
# closed vocabularies (semantic = the two measured TTFx modes; runtime_class = the keys of render's
# RUNTIME_LABELS). The classifier (warm_vs_cold.classify_warm_vs_cold) only PARITY-checks
# runtime_class as a non-empty string — it does NOT enum-validate it — so this coercer is the
# fail-closed PII guard that keeps an out-of-enum or free-text runtime off the public page. A drift
# from render's set is caught by the cross-contract test, not papered over by a shared import. A
# value outside either set invalidates the whole warm_vs_cold block (-> None).
WARM_VS_COLD_SEMANTIC_ENUM = ("ttfi", "ttfe")
WARM_VS_COLD_RUNTIME_CLASS_ENUM = ("gvisor", "kata-microvm")

# #4021 concurrent-burst block — the emitter's INDEPENDENT copy of render's CONCURRENT_BURST_MODES
# closed vocabulary. A leg is a single all-at-once burst of N concurrent claims; mode distinguishes
# a warm-pool hit from a cold provision. A free-text mode fails the whole block closed. A drift from
# render's set is caught by the cross-contract test, not papered over by a shared import.
CONCURRENT_BURST_MODE_ENUM = ("warm", "cold")

# #3942 Kata+microVM activation block — the emitter's INDEPENDENT copies of render's
# KATA_ACTIVATION closed vocabularies. This block publishes Kata pod-Ready / microVM-activation
# latency (NOT TTFE — the matrix TTFE cells for Kata stay honestly pending). hypervisor is a
# PUBLIC hypervisor name only (a free-text value drops the whole block). resume_status is a
# closed enum — "upstream-blocked" means CRIU resume is not wired upstream (#3097), a genuine
# upstream gap, the only state measured today. A drift from render's set is caught by the
# cross-contract test, not papered over by a shared import.
KATA_HYPERVISOR_ENUM = ("Cloud Hypervisor", "QEMU", "Firecracker", "Dragonball", "Stratovirt")
KATA_RESUME_STATUS_ENUM = ("upstream-blocked",)
# Kernel release shape (mirror of render/schema.py _KERNEL): MAJOR.MINOR.PATCH + optional vendor
# suffix (6.18.35 / 6.8.0-1054-gke). Tightly bounded so an internal node/pool name cannot ride a
# kernel field.
_KATA_KERNEL_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z._-]+)?$")
# Kata version shape (mirror of _KATA_VERSION): bare semantic version (3.32.0), no suffix.
_KATA_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
# Kata image shape (mirror of _KATA_IMAGE): a PUBLIC base-image ref with NO registry path
# (forbids `/`) — debian:12, ubuntu:24.04. Stricter than provenance image refs so an internal
# registry/project path can never ride an image field.
_KATA_IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*:[A-Za-z0-9][A-Za-z0-9._-]*$")

# #3942/#830 sandbox matrix runtime — the emitter's INDEPENDENT copy of render's
# MATRIX_RUNTIMES closed vocabulary (== the keys of render's RUNTIME_LABELS). render's
# render_matrix selects the measured runtime column from provenance.runtime; the emit
# path derives it product-side (sandbox->gvisor, sandbox-kata->kata-microvm) with a
# BENCH_MATRIX_RUNTIME override. A value outside this set is a misconfiguration (typo'd
# override), so _coerce_provenance fails closed on it rather than dropping — a mislabeled
# runtime must never publish a wrong measured column. A drift from render's set is caught
# by the cross-contract test, not papered over by a shared import.
MATRIX_RUNTIME_ENUM = ("gvisor", "kata-microvm")

PROVENANCE_FIELDS = (
    "cluster_substrate",
    "controller_image",
    "controller_digest",
    "crd_version",
    "suite_git_sha",
    "run_id",
    "node_count",
    "cold_start_mode",
    "regime",
    "warm_scaling_term",
    "runtime",
)
SCENARIO_FIELDS = (
    "name",
    "outcome",
    "pending_reason",
    "badge_scope",
    "badge_construction",
    "n",
    "sla_metrics",
)

# sla_metric keys must be machine-readable metric names: lowercase alphanumerics
# separated by underscore or hyphen. No spaces, colons, slashes, or dots — a
# leaked path/DSN/host:port cannot pass this shape, so an excerpt smuggled in as
# an sla key is dropped. Underscores are permitted so render's canonical metric
# keys (activation_ms, cold_start_ms, …) pass; hyphen forms still pass too.
_METRIC_KEY_RE = re.compile(r"^[a-z0-9]+(?:[_-][a-z0-9]+)*$")

# GCP machine-type shape — emitter-side bound (independent mirror of render/schema.py's
# _MACHINE_TYPE). The family/class tokens are PUBLIC GCP identifiers; we bound the shape so a
# free-text value can never ride the stepup machine_type field. A value that is not a
# recognizable GCP machine shape is dropped.
_MACHINE_TYPE_RE = re.compile(
    r"^[a-z][a-z0-9]*-(standard|highmem|highcpu|micro|small|medium)(-[0-9]+)?$"
)

# run-id shape — emitter-side mirror of render/schema.py's _RUNID. A run id is an opaque
# provenance token (a UUID/hex/slug); we bound the shape so a free-text value can never ride
# the cluster_saturation run_id field into the public page. Present-but-non-matching ⇒ dropped.
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _coerce_sla_metrics(raw) -> dict:
    """Keep only {safe-key: finite-number}; drop everything else.

    This is the load-bearing leak-suspenders: an sla value MUST be numeric, so a
    raw failure_excerpt string stuffed into sla_metrics is dropped, never emitted.

    ONE explicit carve-out (hb#174): the key `thpt_slo_basis` carries a CLOSED-ENUM
    string (which measured basis produced the derived per-cluster SLO triple). It is
    enum-gated fail-closed — present-but-non-enum RAISES (a real bug, matching the
    module's closed-value-set posture), so the numeric-only guard is never weakened
    to "any string on this key". Free text still cannot ride sla_metrics.
    """
    out: dict = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        if not isinstance(k, str) or not _METRIC_KEY_RE.match(k):
            continue
        if k == "thpt_slo_basis":
            if v not in SLO_BASIS_ENUM:
                raise ValueError(
                    f"sla_metrics.thpt_slo_basis {v!r} not in {SLO_BASIS_ENUM}"
                )
            out[k] = v
            continue
        # bool is a subclass of int — exclude it; an sla metric is a measurement.
        if isinstance(v, bool) or not isinstance(v, Real):
            continue
        fv = float(v)
        if fv != fv or fv in (float("inf"), float("-inf")):  # NaN / inf
            continue
        out[k] = fv
    # hb#214 part 1 (DRAFT) — floor-zero pairing guard, fail-closed BOTH directions.
    # A 0.0 PER-CLUSTER SLO rate is publishable ONLY as the pre-declared floor-zero
    # verdict (stamp thpt_slo_floor_zero=1); a bare per-cluster 0.0 is exactly the
    # fabricated-0 class the honesty spine forbids, and a stamp without its 0.0 is an
    # inconsistent producer. Either shape RAISES (a real bug, matching the module's
    # closed-value posture) rather than silently dropping — a silent drop would let
    # the record publish with the dishonest half removed.
    # Deliberately NARROW: the per-NODE keys are excluded because a bare per-node 0.0
    # is the hb#142 honest measured-0 class (a real fire whose p95 missed the bar,
    # over a real per-node denominator — live latest.json carries several) — a
    # different quantity from the SLO-sweep per-cluster rates this guard protects.
    zero_rate = any(
        out.get(rk) == 0.0
        for rk in (
            "thpt_under_5s_per_cluster",
            "thpt_under_1s_per_cluster",
        )
    )
    has_stamp = out.get("thpt_slo_floor_zero") == 1.0
    if zero_rate and not has_stamp:
        raise ValueError(
            "sla_metrics: 0.0 per-cluster SLO rate without thpt_slo_floor_zero=1 "
            "(bare per-cluster zero is a fabricated-0; only the hb#214 floor-zero "
            "predicate may emit 0.0, and it always stamps)"
        )
    # The predicate emits the 5s pair together (exactly-0 is the one case where the
    # per-node and per-cluster denominators are interchangeable), so a stamp must be
    # accompanied by BOTH 5s legs at 0.0 — a stamp with only one leg is a producer
    # that dropped half the pair, exactly the swallowed-figure shape hb#214 closes.
    if has_stamp and not (
        out.get("thpt_under_5s_per_cluster") == 0.0
        and out.get("thpt_under_5s_per_node") == 0.0
    ):
        raise ValueError(
            "sla_metrics: thpt_slo_floor_zero=1 without BOTH 5s legs at 0.0 "
            "(thpt_under_5s_per_cluster AND thpt_under_5s_per_node; "
            "inconsistent floor-zero pairing)"
        )
    # hb#230 Fork 4 — the COLD-START floor-zero fills BOTH bars (a cold floor over the
    # 5s bar is a fortiori over the 1s bar). When the 1s per-cluster leg PARTICIPATES
    # (is zeroed under the stamp), its per-node partner must be zeroed too — the same
    # swallowed-figure guard as the 5s pair, applied conditionally so the warm 5s-only
    # floor-zero (which omits the 1s legs entirely) stays inert. A stamp with a 0.0 1s
    # per-cluster leg but a non-zero/absent 1s per-node leg is the dropped-half shape.
    if has_stamp and out.get("thpt_under_1s_per_cluster") == 0.0 and (
        out.get("thpt_under_1s_per_node") != 0.0
    ):
        raise ValueError(
            "sla_metrics: thpt_slo_floor_zero=1 with 1s per-cluster leg at 0.0 but "
            "1s per-node leg not at 0.0 (thpt_under_1s_per_node; inconsistent "
            "cold-floor-zero 1s pairing)"
        )
    return out


def _coerce_scenario(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise TypeError(f"scenario entry must be a dict, got {type(raw).__name__}")

    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("scenario.name must be a non-empty string")

    outcome = raw.get("outcome")
    if isinstance(outcome, str):
        outcome = _OUTCOME_CANON.get(outcome.lower(), outcome)
    if outcome not in OUTCOME_ENUM:
        raise ValueError(
            f"scenario {name!r}: outcome {raw.get('outcome')!r} not in {OUTCOME_ENUM}"
        )

    out = {"name": name, "outcome": outcome}

    # pending_reason is meaningful ONLY when outcome == pending; drop it otherwise.
    if outcome == "pending":
        reason = raw.get("pending_reason")
        if reason not in PENDING_REASON_ENUM:
            raise ValueError(
                f"scenario {name!r}: pending requires pending_reason in "
                f"{PENDING_REASON_ENUM}, got {reason!r}"
            )
        out["pending_reason"] = reason

    # badge_scope is optional and per-scenario; when present it MUST be in the closed
    # enum (fail-closed — a non-enum value is a misconfiguration, not a leak, mirroring
    # cold_start_mode). Dropped silently when absent so non-isolation cells stay clean.
    scope = raw.get("badge_scope")
    if scope is not None:
        if scope not in BADGE_SCOPE_ENUM:
            raise ValueError(
                f"scenario {name!r}: badge_scope {scope!r} not in {BADGE_SCOPE_ENUM}"
            )
        out["badge_scope"] = scope

    # badge_construction is optional and per-scenario; when present it MUST be in the closed
    # enum (fail-closed, same posture as badge_scope). Dropped silently when absent.
    construction = raw.get("badge_construction")
    if construction is not None:
        if construction not in BADGE_CONSTRUCTION_ENUM:
            raise ValueError(
                f"scenario {name!r}: badge_construction {construction!r} not in "
                f"{BADGE_CONSTRUCTION_ENUM}"
            )
        out["badge_construction"] = construction

    # Flip-time coupling guard (#4051): an "enforced" data-plane PASS MUST carry a
    # badge_construction. Without it the cell renders a bare `PASS (enforced)` that misreads as a
    # managed-gke-sandbox-NP guarantee — the exact over-claim badge_construction exists to close
    # (#3950/#2082). control-plane scope needs no construction (it asserts admission, not which
    # NP mechanism was exercised), and a construction with no scope is harmless (render shows it
    # only alongside a scope) so it is not forced the other way.
    if out.get("badge_scope") == "enforced" and "badge_construction" not in out:
        raise ValueError(
            f"scenario {name!r}: badge_scope 'enforced' requires a badge_construction in "
            f"{BADGE_CONSTRUCTION_ENUM} (over-claim guard, #4051)"
        )

    n = raw.get("n")
    if isinstance(n, bool):
        n = None
    if isinstance(n, int):
        out["n"] = n
    elif isinstance(n, float) and n.is_integer():
        out["n"] = int(n)

    sla = _coerce_sla_metrics(raw.get("sla_metrics"))
    if sla:
        out["sla_metrics"] = sla

    return out


def _coerce_scale_proof(raw):
    """Keep the closed top-level scale_proof shape; return None to omit the key.

    The Scale Proof (Linearity Check) table is rendered from a TOP-LEVEL
    `scale_proof` object (NOT a per-scenario sla_metrics — a list value cannot ride
    sla_metrics, which _coerce_sla_metrics drops). This mirrors render/schema.py's
    SCALE_PROOF_FIELDS exactly so emitter and renderer share one contract:
      - scale_points: list of {node_count:int (0<nc<10000), density:float>=0,
        throughput:float>=0 (OPTIONAL — carried so render's per-step throughput
        convergence subline can show; mirrors render/schema.py _scale_points_ok)}
      - density_retention / thpt_retention: optional nonneg floats

    Allow-list-by-construction like the scenario/provenance coercers: only the
    known fields+types survive, everything else is dropped. scale_points is
    REQUIRED — an absent/empty/malformed points list returns None so no
    scale_proof key is emitted (the table renders nothing rather than a partial
    lie). The two retentions are optional: thpt_retention has NO per-point render
    fallback, so dropping it renders the throughput column pending (honest), never
    a fabricated ratio.
    """
    if not isinstance(raw, dict):
        return None
    points = raw.get("scale_points")
    if not isinstance(points, list) or not points:
        return None
    clean_points = []
    for p in points:
        if not isinstance(p, dict):
            return None
        nc, dn = p.get("node_count"), p.get("density")
        if isinstance(nc, bool) or not isinstance(nc, int) or not (0 < nc < 10000):
            return None
        if isinstance(dn, bool) or not isinstance(dn, Real):
            return None
        fdn = float(dn)
        if fdn != fdn or fdn in (float("inf"), float("-inf")) or fdn < 0:
            return None
        cp = {"node_count": nc, "density": fdn}
        # throughput (per-node ready rate) is OPTIONAL per point and mirrors
        # render/schema.py _scale_points_ok: the producer (scale_slope.py) emits it so
        # render's per-step throughput convergence subline can show, but it must SURVIVE
        # ingestion to reach the renderer. Dropping it here silently blanks that subline
        # on every real fire (the canonical {1,2,4} sweep is exactly the >=3-point case
        # render._per_step_retention_line activates on). When present it must be a
        # non-negative real (bool/NaN/inf rejected, like density) — an invalid value
        # fails the whole block closed, never a partial point.
        if "throughput" in p:
            tp = p["throughput"]
            if isinstance(tp, bool) or not isinstance(tp, Real):
                return None
            ftp = float(tp)
            if ftp != ftp or ftp in (float("inf"), float("-inf")) or ftp < 0:
                return None
            cp["throughput"] = ftp
        clean_points.append(cp)
    out = {"scale_points": clean_points}
    for key in ("density_retention", "thpt_retention"):
        v = raw.get(key)
        if isinstance(v, bool) or not isinstance(v, Real):
            continue
        fv = float(v)
        if fv != fv or fv in (float("inf"), float("-inf")) or fv < 0:
            continue
        out[key] = fv
    # measured_at (#3952): the ISO-8601 instant the sweep ran. Optional + carried
    # forward across the daily single-node refresh, so the published Scale Proof
    # block does not auto-decay; the date makes a carried (point-in-time) block
    # honestly distinct from the daily-refreshed top-level generated_at. Allow-list
    # a non-empty string only — anything else is dropped (render shows no subline).
    ma = raw.get("measured_at")
    if isinstance(ma, str) and ma:
        out["measured_at"] = ma
    return out


def _coerce_offered_rate(rate):
    """Normalize a stepup-family offered_rate_per_s; None to reject (hb#189).

    Fractional rungs are first-class producer-side (kata 0.5/1.5 per_s; sub-refill
    credit ladders need midpoints like 1.5), so the true-TTFE and controller-proxy
    gates accept any positive finite Real < 100000 — matching the literal_ttfe leg's
    existing posture. Two normalize rules keep honesty + history intact:

      - integral -> strict int (2.0 -> 2): existing integer-rung records round-trip
        byte-identically; downstream strict-int consumers of integer rungs are
        unaffected.
      - fractional -> float, NEVER floored: 1.5 -> 1 would alias the measurement onto
        a real rate-1 rung and misattribute it.
    """
    if isinstance(rate, bool) or not isinstance(rate, Real):
        return None
    frate = float(rate)
    if frate != frate or not (0 < frate < 100000):
        return None
    return int(frate) if frate == int(frate) else frate


def _coerce_controller_startup(raw, clean_nonneg):
    """Coerce the controller-startup LOWER-BOUND proxy block (#3975); None to omit it.

    True TTFE has no upstream production stamp on current main, so the true-TTFE pareto is
    honestly empty while the gap is open. This SEPARATE block carries the controller-stamped
    startup latency (controller-first-observed -> Ready) as an explicit LOWER BOUND — it
    EXCLUDES the claim-admission->first-reconcile queueing lag, so it under-reports true TTFE.

    `lower_bound` MUST be exactly True (load-bearing: render keys the fixed lower-bound caveat
    boilerplate off it; an absent/false flag drops the whole block so the proxy can never render
    unmarked). The internal producer's free-text caveat is render-owned and NEVER carried here.
    pareto_points must be a non-empty list; per point offered_rate_per_s + controller_startup_p95_ms
    are required (x-axis + proxy honesty spine), the p50/p99 + controller_ready_per_s optional
    (dropped on a bad value -> honest partial point). Optional proxy verdict from the same closed
    set. Any malformed point returns None (the proxy table degrades to nothing, never a fake curve).
    """
    if not isinstance(raw, dict):
        return None
    if raw.get("lower_bound") is not True:
        return None
    pts = raw.get("pareto_points")
    if not isinstance(pts, list) or not pts:
        return None
    clean = []
    for p in pts:
        if not isinstance(p, dict):
            return None
        rate = _coerce_offered_rate(p.get("offered_rate_per_s"))
        if rate is None:
            return None
        p95 = clean_nonneg(p.get("controller_startup_p95_ms"))
        if p95 is None:
            return None
        cp = {"offered_rate_per_s": rate, "controller_startup_p95_ms": p95}
        for opt in ("controller_startup_p50_ms", "controller_startup_p99_ms", "controller_ready_per_s"):
            if opt in p:
                ov = clean_nonneg(p[opt])
                if ov is not None:
                    cp[opt] = ov
        clean.append(cp)
    out = {"lower_bound": True, "pareto_points": clean}
    verdict = raw.get("verdict")
    if verdict in STEPUP_VERDICT_ENUM:
        out["verdict"] = verdict
    return out


def _coerce_literal_ttfe(raw, clean_nonneg):
    """Coerce the literal-TTFE UPPER-BOUND leg (hb#174); None to omit it.

    The exec-probe literal TTFE: every sample includes exec websocket-setup overhead, so
    it OVER-reads true TTFE — a rung compliant at an SLO bar under this basis is
    conservatively/provably compliant. That polarity is exactly why this leg (unlike the
    controller-startup LOWER-bound proxy) is eligible to fill SLO cells downstream
    (slo_rate owns the pick + `thpt_slo_basis` stamp).

    `upper_bound` MUST be exactly True (load-bearing: render keys the upper-bound caveat
    off it, and slo_rate consults the leg only when the polarity flag is present — an
    absent/false flag drops the whole block so the leg can never render or derive
    unmarked). `includes_exec_setup_overhead` is carried only when exactly True. The
    producer's free-text caveat is render-owned and NEVER carried here.

    pareto_points must be a non-empty list. Per point: `offered_rate_per_s` is a
    positive finite Real < 100000, because kata rungs are fractional (0.5/1.5 per_s)
    and first-class where the SLO knee sits at ~2-3/s (since hb#189 the true-TTFE and
    controller points accept the same Real range via _coerce_offered_rate; this leg
    keeps its historical always-float coercion). `literal_warm_p95_ms` (nonneg) is the required honesty
    spine. Optional per-point measured values are dropped-per-field on a bad value
    (honest partial point): warm/cold p50/p95/p99 percentiles + the two measured rate
    candidates (`acq_fulfilled_per_s`, `controller_completed_per_s` — NAMESPACED, never
    aliased onto true-TTFE names). Sampling-disclosure ints (`literal_every_n`,
    `literal_warm_n_exec_ok`, `literal_cold_n_exec_ok`) kept as nonneg ints. Optional
    verdict from the same closed set. Any malformed point returns None (the leg degrades
    to nothing, never a fake curve).
    """
    if not isinstance(raw, dict):
        return None
    if raw.get("upper_bound") is not True:
        return None
    pts = raw.get("pareto_points")
    if not isinstance(pts, list) or not pts:
        return None
    clean = []
    for p in pts:
        if not isinstance(p, dict):
            return None
        rate = p.get("offered_rate_per_s")
        if isinstance(rate, bool) or not isinstance(rate, Real):
            return None
        frate = float(rate)
        if frate != frate or not (0 < frate < 100000):
            return None
        p95 = clean_nonneg(p.get("literal_warm_p95_ms"))
        if p95 is None:
            return None
        cp = {"offered_rate_per_s": frate, "literal_warm_p95_ms": p95}
        for opt in (
            "literal_warm_p50_ms", "literal_warm_p99_ms",
            "literal_cold_p50_ms", "literal_cold_p95_ms", "literal_cold_p99_ms",
            "acq_fulfilled_per_s", "controller_completed_per_s",
        ):
            if opt in p:
                ov = clean_nonneg(p[opt])
                if ov is not None:
                    cp[opt] = ov
        # literal_warm_n_over_bar_5s + literal_warm_n_unknown: hb#214 part 1 (DRAFT)
        # floor-zero count contract — over-margined-bar known samples (producer counts
        # against THRESHOLD_5S_MS * HONEST_ZERO_BAR_MARGIN) + unknown-sentinel samples
        # (0 until upstream #1087 lands). Both nonneg ints; the derive fails closed
        # when either is absent, so a pre-contract record can never fire the zero.
        for opt in (
            "literal_every_n", "literal_warm_n_exec_ok", "literal_cold_n_exec_ok",
            "literal_warm_n_over_bar_5s", "literal_warm_n_unknown",
        ):
            v = p.get(opt)
            if not isinstance(v, bool) and isinstance(v, int) and v >= 0:
                cp[opt] = v
        clean.append(cp)
    out = {"upper_bound": True, "pareto_points": clean}
    if raw.get("includes_exec_setup_overhead") is True:
        out["includes_exec_setup_overhead"] = True
    verdict = raw.get("verdict")
    if verdict in STEPUP_VERDICT_ENUM:
        out["verdict"] = verdict
    return out


def _coerce_saturation_point(raw, clean_nonneg):
    """Keep the closed operator saturation-point shape (#4030); None -> omit the key.

    Mirrors render/schema.py's _stepup_saturation_point_ok exactly. tight_ms + loose_ms are
    REQUIRED positive bar floats; basis is an OPTIONAL non-empty descriptor. Each leg (warm/
    cold) is OPTIONAL; its max_rate_under_{tight,loose} is a rate FROM the swept ladder, so it
    normalizes via _coerce_offered_rate (integral -> int, fractional -> float — hb#189) — a
    None, absent, or malformed value is DROPPED (the renderer prints em-dash, never a
    fabricated 0). At least one
    leg must carry at least one present rate, else the block is honest "nothing" -> None.
    """
    if not isinstance(raw, dict):
        return None
    tight = clean_nonneg(raw.get("tight_ms"))
    loose = clean_nonneg(raw.get("loose_ms"))
    if not tight or not loose:
        return None
    out = {"tight_ms": tight, "loose_ms": loose}
    basis = raw.get("basis")
    if isinstance(basis, str) and basis:
        out["basis"] = basis
    any_rate = False
    for leg in ("warm", "cold"):
        lv = raw.get(leg)
        if not isinstance(lv, dict):
            continue
        cleaned = {}
        for k in ("max_rate_under_tight", "max_rate_under_loose"):
            rv = _coerce_offered_rate(lv.get(k))
            if rv is not None:
                cleaned[k] = rv
                any_rate = True
        out[leg] = cleaned
    if not any_rate:
        return None
    return out


def _coerce_stepup(raw):
    """Keep the closed top-level step-up Pareto shape; return None to omit the key.

    The a#3960 throughput-saturation study renders from a TOP-LEVEL `stepup` object
    (a list-bearing value cannot ride per-scenario sla_metrics, which _coerce_sla_metrics
    drops). Mirrors render/schema.py's STEPUP_PARETO_FIELDS exactly so emitter and renderer
    share one contract. PUBLIC-safe by construction: only measured numbers + a bounded GCP
    machine shape survive — never an internal cluster/namespace/project name.

    verdict is REQUIRED (unknown -> None). The Pareto tables are BLOCK-LEVEL relaxed: the
    true-TTFE `pareto_points` is OMITTED (not emitted empty) when no step measured a true TTFE
    warm p95 — the #3975 gap — and the controller_startup LOWER-BOUND proxy and/or the
    literal_ttfe UPPER-BOUND leg (hb#174) stand in. At least ONE of {pareto_points,
    controller_startup, literal_ttfe} must be present and valid; an all-empty sweep returns
    None (the table renders nothing rather than a partial lie). True-TTFE per-point:
    offered_rate_per_s (positive finite Real < 100000, normalized via _coerce_offered_rate:
    integral -> int, fractional -> float — hb#189) + ttfe_p95_ms (nonneg, the honesty spine)
    required;
    ready_per_s / ttfe_p50_ms / ttfe_p99_ms / cost_usd_per_1k_ready optional (dropped on a bad
    value, so a partial Prometheus scrape yields a partial point honestly, never a fabricated 0).
    The characteristic rates + Little's-law params are optional sweep-level scalars.
    """
    if not isinstance(raw, dict):
        return None

    def _clean_nonneg(x):
        if isinstance(x, bool) or not isinstance(x, Real):
            return None
        fx = float(x)
        if fx != fx or fx in (float("inf"), float("-inf")) or fx < 0:
            return None
        return fx

    # True-TTFE Pareto — block-level optional (#3975). A malformed point still hard-fails the
    # whole sweep, but an absent/empty list is tolerated when the controller_startup proxy carries
    # the table. clean_points stays [] in that case and the key is omitted from the output below.
    points = raw.get("pareto_points")
    clean_points = []
    if isinstance(points, list):
        for p in points:
            if not isinstance(p, dict):
                return None
            rate = _coerce_offered_rate(p.get("offered_rate_per_s"))
            if rate is None:
                return None
            p95 = _clean_nonneg(p.get("ttfe_p95_ms"))
            if p95 is None:
                return None
            cp = {"offered_rate_per_s": rate, "ttfe_p95_ms": p95}
            for opt in ("ready_per_s", "ttfe_p50_ms", "ttfe_p99_ms", "cost_usd_per_1k_ready"):
                if opt in p:
                    ov = _clean_nonneg(p[opt])
                    if ov is not None:
                        cp[opt] = ov
            clean_points.append(cp)

    controller = _coerce_controller_startup(raw.get("controller_startup"), _clean_nonneg)
    literal = _coerce_literal_ttfe(raw.get("literal_ttfe"), _clean_nonneg)

    # Block-level relaxation: emit only when at least one of the three Pareto tables is
    # populated. An all-empty sweep (no true-TTFE points AND no valid proxy block AND no
    # literal upper-bound leg) is honest "nothing", -> None.
    if not clean_points and controller is None and literal is None:
        return None

    verdict = raw.get("verdict")
    if verdict not in STEPUP_VERDICT_ENUM:
        return None

    out = {"verdict": verdict}
    if clean_points:
        out["pareto_points"] = clean_points
    saturation_point = _coerce_saturation_point(raw.get("saturation_point"), _clean_nonneg)
    if saturation_point is not None:
        out["saturation_point"] = saturation_point
    if controller is not None:
        out["controller_startup"] = controller
    if literal is not None:
        out["literal_ttfe"] = literal

    # Optional characteristic rates (None in the source when no breach). Values FROM the swept
    # ladder, so a fractional ladder yields fractional characteristic rates — normalized via
    # _coerce_offered_rate (integral -> int, fractional -> float — hb#189).
    for key in ("north_star_breach_rate", "saturation_rate", "max_flat_rate"):
        v = _coerce_offered_rate(raw.get(key))
        if v is None:
            continue
        out[key] = v

    # Optional Little's-law params (public-safe scalars).
    sld = _clean_nonneg(raw.get("sld_s"))
    if sld is not None:
        out["sld_s"] = sld
    wpr = raw.get("wpr")
    if not isinstance(wpr, bool) and isinstance(wpr, Real):
        fwpr = float(wpr)
        if fwpr == fwpr and 0.0 <= fwpr <= 1.0:
            out["wpr"] = fwpr
    nc = raw.get("node_count")
    if not isinstance(nc, bool) and isinstance(nc, int) and 0 < nc < 10000:
        out["node_count"] = nc
    mt = raw.get("machine_type")
    if isinstance(mt, str) and _MACHINE_TYPE_RE.match(mt):
        out["machine_type"] = mt
    ma = raw.get("measured_at")
    if isinstance(ma, str) and ma:
        out["measured_at"] = ma
    return out


def _coerce_warm_vs_cold(raw):
    """Keep the closed top-level warm-vs-cold speedup shape; return None to omit the key.

    The #3954-sibling warm-vs-cold headline ("warm provisioning is N times faster than cold")
    renders from a TOP-LEVEL `warm_vs_cold` object (mirrors scale_proof / stepup — a nested object
    cannot ride per-scenario sla_metrics). The harness classifier
    (warm_vs_cold.classify_warm_vs_cold) composes the warm leg (burst TTFx p50) and the true-cold
    leg (native_digest_cold) into the inner object, or returns {} on any honesty gate. This coercer
    is the closed-schema PII guard mirroring render/schema.py's WARM_VS_COLD_FIELDS exactly so
    emitter and renderer share one contract — making the warm_vs_cold.py:38 `build_results(
    warm_vs_cold=...)` contract real.

    All five spine fields are REQUIRED. warm_p50_ms / cold_ms must be strictly > 0 (a 0-leg is a
    degenerate ratio — render's _clean_warm_vs_cold drops the block on warm<=0 or cold<=0; mirror
    that here). speedup is non-negative. semantic is one of the two measured modes. runtime_class is
    enum-validated against the PUBLIC runtime set — the classifier only parity-checks it as a
    non-empty string, so THIS is the fail-closed guard that keeps an out-of-enum or free-text
    runtime off the public page. n_warm is OPTIONAL (sample count; dropped on a bad value -> render
    the bare headline). Any missing/invalid required field returns None (the block renders nothing
    rather than a partial lie).
    """
    if not isinstance(raw, dict):
        return None

    def _clean_nonneg(x):
        if isinstance(x, bool) or not isinstance(x, Real):
            return None
        fx = float(x)
        if fx != fx or fx in (float("inf"), float("-inf")) or fx < 0:
            return None
        return fx

    warm = _clean_nonneg(raw.get("warm_p50_ms"))
    cold = _clean_nonneg(raw.get("cold_ms"))
    # Strictly positive: a 0 leg is a degenerate ratio (mirrors render's positivity gate).
    if warm is None or warm <= 0 or cold is None or cold <= 0:
        return None
    speedup = _clean_nonneg(raw.get("speedup"))
    if speedup is None:
        return None
    semantic = raw.get("semantic")
    if semantic not in WARM_VS_COLD_SEMANTIC_ENUM:
        return None
    runtime_class = raw.get("runtime_class")
    if runtime_class not in WARM_VS_COLD_RUNTIME_CLASS_ENUM:
        return None

    out = {
        "warm_p50_ms": warm,
        "cold_ms": cold,
        "speedup": speedup,
        "semantic": semantic,
        "runtime_class": runtime_class,
    }
    n_warm = raw.get("n_warm")
    if not isinstance(n_warm, bool) and isinstance(n_warm, int) and n_warm >= 0:
        out["n_warm"] = n_warm
    # measured_at: the ISO-8601 instant the two legs were measured. Optional + carried
    # forward across the daily refresh (same as scale_proof / stepup), so a carried
    # (point-in-time) block stays honestly dated against the daily-refreshed top-level
    # generated_at instead of silently aliasing today. Allow-list a non-empty string
    # only — anything else is dropped (render shows no dated subline).
    ma = raw.get("measured_at")
    if isinstance(ma, str) and ma:
        out["measured_at"] = ma
    # cold_start_mode (#4024): OPTIONAL closed-enum mirroring render/schema.py's
    # WARM_VS_COLD_FIELDS, so a back-filled cold-provision object survives this emitter-side
    # PII guard instead of being silently stripped. Absent ⇒ omitted (render falls back to the
    # true-cold default phrasing). Present-but-invalid ⇒ INVALIDATE the whole block (return
    # None), matching render's _clean_warm_vs_cold fail-closed guard so a typo'd mode never
    # publishes as true-cold. (A drift from render's set is caught by the cross-contract test.)
    csm = raw.get("cold_start_mode")
    if csm is not None:
        if csm not in COLD_START_MODE_ENUM:
            return None
        out["cold_start_mode"] = csm
    return out


def _coerce_kata_activation(raw):
    """Keep the closed top-level Kata+microVM activation shape; return None to omit the key.

    The #3942 Kata block publishes pod-Ready / microVM-activation latency — explicitly NOT TTFE
    (the matrix TTFE cells for Kata stay honestly pending; render's caption restates this so a
    reader cannot conflate the two). It renders from a TOP-LEVEL `kata_activation` object (mirrors
    scale_proof / stepup / warm_vs_cold — a nested/list-bearing value cannot ride per-scenario
    sla_metrics). This coercer is the closed-schema PII guard mirroring render/schema.py's
    KATA_ACTIVATION_FIELDS exactly so emitter and renderer share one contract.

    REQUIRED spine: runtime_class (enum-validated against the public runtime set), microvm_activation_ms
    + warm_ready_ms (nonneg), cold_ready (non-empty list of {image: public base-image ref with NO
    registry path, ready_ms: nonneg, image_pull_ms?: nonneg}), guest_kernel + host_kernel (bounded
    kernel-release shape — so an internal node/pool name cannot ride a kernel field). Any
    missing/invalid required field returns None (the block renders nothing rather than a partial lie).

    OPTIONAL: warm_image (public image shape), hypervisor (public hypervisor enum), resume_status
    ("upstream-blocked" — CRIU resume not wired upstream, #3097), kata_version (bare semver), n
    (sample count), measured_at (ISO-8601). A present-but-invalid optional enum/shape INVALIDATES the
    whole block (return None) — a typo'd hypervisor / a registry-path image / a free-text resume must
    never publish, matching the warm_vs_cold fail-closed posture. (A drift from render's set is caught
    by the cross-contract test.)
    """
    if not isinstance(raw, dict):
        return None

    def _clean_nonneg(x):
        if isinstance(x, bool) or not isinstance(x, Real):
            return None
        fx = float(x)
        if fx != fx or fx in (float("inf"), float("-inf")) or fx < 0:
            return None
        return fx

    runtime_class = raw.get("runtime_class")
    if runtime_class not in WARM_VS_COLD_RUNTIME_CLASS_ENUM:
        return None
    microvm = _clean_nonneg(raw.get("microvm_activation_ms"))
    if microvm is None:
        return None
    warm_ready = _clean_nonneg(raw.get("warm_ready_ms"))
    if warm_ready is None:
        return None

    # cold_ready: non-empty list of public-image-ref entries. Any malformed entry fails the whole
    # block closed (mirrors scale_points), so a partial/leaky list never publishes.
    points = raw.get("cold_ready")
    if not isinstance(points, list) or not points:
        return None
    clean_cold = []
    for e in points:
        if not isinstance(e, dict):
            return None
        img = e.get("image")
        if not (isinstance(img, str) and bool(_KATA_IMAGE_RE.match(img))):
            return None
        rm = _clean_nonneg(e.get("ready_ms"))
        if rm is None:
            return None
        ce = {"image": img, "ready_ms": rm}
        if "image_pull_ms" in e:
            pm = _clean_nonneg(e["image_pull_ms"])
            if pm is None:
                return None
            ce["image_pull_ms"] = pm
        clean_cold.append(ce)

    guest_kernel = raw.get("guest_kernel")
    if not (isinstance(guest_kernel, str) and bool(_KATA_KERNEL_RE.match(guest_kernel))):
        return None
    host_kernel = raw.get("host_kernel")
    if not (isinstance(host_kernel, str) and bool(_KATA_KERNEL_RE.match(host_kernel))):
        return None

    out = {
        "runtime_class": runtime_class,
        "microvm_activation_ms": microvm,
        "warm_ready_ms": warm_ready,
        "cold_ready": clean_cold,
        "guest_kernel": guest_kernel,
        "host_kernel": host_kernel,
    }

    # OPTIONAL fields — a present-but-invalid value INVALIDATES the whole block (fail-closed),
    # so a typo'd hypervisor / registry-path image / free-text resume never publishes.
    if "warm_image" in raw:
        wi = raw["warm_image"]
        if not (isinstance(wi, str) and bool(_KATA_IMAGE_RE.match(wi))):
            return None
        out["warm_image"] = wi
    if "hypervisor" in raw:
        hv = raw["hypervisor"]
        if hv not in KATA_HYPERVISOR_ENUM:
            return None
        out["hypervisor"] = hv
    if "resume_status" in raw:
        rs = raw["resume_status"]
        if rs not in KATA_RESUME_STATUS_ENUM:
            return None
        out["resume_status"] = rs
    if "kata_version" in raw:
        kv = raw["kata_version"]
        if not (isinstance(kv, str) and bool(_KATA_VERSION_RE.match(kv))):
            return None
        out["kata_version"] = kv
    if "n" in raw:
        n = raw["n"]
        if isinstance(n, bool) or not isinstance(n, int) or n < 0:
            return None
        out["n"] = n
    if "measured_at" in raw:
        ma = raw["measured_at"]
        if not (isinstance(ma, str) and ma):
            return None
        out["measured_at"] = ma
    return out


def _coerce_concurrent_burst(raw):
    """Keep the closed top-level concurrent-burst shape (#4021); return None to omit the key.

    The #4021 block reports a single ALL-AT-ONCE burst of N concurrent claims (the complement to
    the per-second rate the matrix/step-up report), warm-pool vs cold-provision, on the SAME TTFE
    spine as the Core Metrics matrix. It renders from a TOP-LEVEL `concurrent_burst` object (a
    list-bearing value cannot ride per-scenario sla_metrics). This coercer mirrors render/schema.py's
    CONCURRENT_BURST_FIELDS exactly so emitter and renderer share one contract (a drift is caught by
    the cross-contract test).

    REQUIRED: a non-empty `legs` list; each leg's spine (n: int 0<n<100000, mode: warm|cold enum,
    ttfe_p50_ms + ttfe_p95_ms: nonneg) required; thpt_under_5s_per_node / thpt_under_1s_per_node
    (nonneg) + exec_success_rate (0..1) optional per leg. Any malformed leg fails the whole block
    CLOSED (no partial-lie table). OPTIONAL provenance scalars: node_count (int), machine_type
    (bounded GCP shape), measured_at (ISO-8601 string) — a present-but-invalid one is dropped.
    """
    if not isinstance(raw, dict):
        return None

    def _clean_nonneg(x):
        if isinstance(x, bool) or not isinstance(x, Real):
            return None
        fx = float(x)
        if fx != fx or fx in (float("inf"), float("-inf")) or fx < 0:
            return None
        return fx

    legs = raw.get("legs")
    if not isinstance(legs, list) or not legs:
        return None
    clean_legs = []
    for leg in legs:
        if not isinstance(leg, dict):
            return None
        n = leg.get("n")
        if isinstance(n, bool) or not isinstance(n, int) or not (0 < n < 100000):
            return None
        mode = leg.get("mode")
        if mode not in CONCURRENT_BURST_MODE_ENUM:
            return None
        p50 = _clean_nonneg(leg.get("ttfe_p50_ms"))
        p95 = _clean_nonneg(leg.get("ttfe_p95_ms"))
        if p50 is None or p95 is None:
            return None
        cl = {"n": n, "mode": mode, "ttfe_p50_ms": p50, "ttfe_p95_ms": p95}
        for opt in ("thpt_under_5s_per_node", "thpt_under_1s_per_node"):
            if opt in leg:
                ov = _clean_nonneg(leg[opt])
                if ov is None:
                    return None
                cl[opt] = ov
        if "exec_success_rate" in leg:
            esr = leg["exec_success_rate"]
            if isinstance(esr, bool) or not isinstance(esr, Real):
                return None
            fesr = float(esr)
            if fesr != fesr or not (0.0 <= fesr <= 1.0):
                return None
            cl["exec_success_rate"] = fesr
        clean_legs.append(cl)

    out = {"legs": clean_legs}
    nc = raw.get("node_count")
    if not isinstance(nc, bool) and isinstance(nc, int) and 0 < nc < 10000:
        out["node_count"] = nc
    mt = raw.get("machine_type")
    if isinstance(mt, str) and _MACHINE_TYPE_RE.match(mt):
        out["machine_type"] = mt
    ma = raw.get("measured_at")
    if isinstance(ma, str) and ma:
        out["measured_at"] = ma
    return out


def _coerce_warm_pool_acquisition(raw):
    """Keep the closed top-level warm-pool ACQUISITION-latency shape (#4083); None ⇒ omit the key.

    The #4083 block reports a DECOMPOSED sub-phase of TTFE: the SandboxClaim requested → bound
    latency (a ready warm sandbox handed back), measured per-claim by the step-up harness's
    acquisition watch-timer (#1043). It EXCLUDES the exec-attach + first-instruction round-trip the
    concurrent_burst/matrix TTFE legs include, so acquisition p95 is NOT comparable to those TTFE
    columns. It renders from a TOP-LEVEL `warm_pool_acquisition` object (mirrors
    scale_proof/stepup/warm_vs_cold/kata_activation/concurrent_burst — a scalar-bundle top-level
    block). This coercer mirrors render/schema.py's WARM_POOL_ACQUISITION_FIELDS exactly so emitter
    and renderer share one contract (a drift is caught by the cross-contract test).

    REQUIRED spine: runtime_class (enum-validated against the public runtime set, fail-closed),
    acq_p50_ms + acq_p95_ms (nonneg), n (int 0<n<100000). Any missing/invalid required field returns
    None (the block renders nothing rather than a partial lie). OPTIONAL decomposition/provenance:
    acq_p99_ms + controller_startup_p95_ms (nonneg), offered_rate_per_s + warmpool_size (pos int),
    machine_type (bounded GCP shape), node_count (int 0<v<10000), measured_at (ISO-8601). A
    present-but-invalid optional value is DROPPED (not fail-closed) — mirroring the render cleaner's
    per-field drop, so a partial fire renders a partial-but-honest block, never a fabricated 0.
    """
    if not isinstance(raw, dict):
        return None

    def _clean_nonneg(x):
        if isinstance(x, bool) or not isinstance(x, Real):
            return None
        fx = float(x)
        if fx != fx or fx in (float("inf"), float("-inf")) or fx < 0:
            return None
        return fx

    runtime_class = raw.get("runtime_class")
    if runtime_class not in WARM_VS_COLD_RUNTIME_CLASS_ENUM:
        return None
    acq_p50 = _clean_nonneg(raw.get("acq_p50_ms"))
    if acq_p50 is None:
        return None
    acq_p95 = _clean_nonneg(raw.get("acq_p95_ms"))
    if acq_p95 is None:
        return None
    n = raw.get("n")
    if isinstance(n, bool) or not isinstance(n, int) or not (0 < n < 100000):
        return None

    out = {
        "runtime_class": runtime_class,
        "acq_p50_ms": acq_p50,
        "acq_p95_ms": acq_p95,
        "n": n,
    }

    # OPTIONAL — a present-but-invalid value is DROPPED per-field (mirrors the render cleaner),
    # never fabricated, so a partial fire renders a partial-but-honest block.
    for opt in ("acq_p99_ms", "controller_startup_p95_ms"):
        if opt in raw:
            ov = _clean_nonneg(raw[opt])
            if ov is not None:
                out[opt] = ov
    for opt in ("offered_rate_per_s", "warmpool_size"):
        v = raw.get(opt)
        if not isinstance(v, bool) and isinstance(v, int) and 0 < v < 100000:
            out[opt] = v
    nc = raw.get("node_count")
    if not isinstance(nc, bool) and isinstance(nc, int) and 0 < nc < 10000:
        out["node_count"] = nc
    mt = raw.get("machine_type")
    if isinstance(mt, str) and _MACHINE_TYPE_RE.match(mt):
        out["machine_type"] = mt
    ma = raw.get("measured_at")
    if isinstance(ma, str) and ma:
        out["measured_at"] = ma
    return out


def _coerce_at_scale_contention(raw):
    """Keep the closed top-level at-scale-contention RETRACTION shape (#810); None ⇒ omit the key.

    The #810 block is the deliberate RETRACTION of the sub-second-at-scale claim: the operating
    point where the warm pool is OVER-SUBSCRIBED (more concurrent claims than ready pool members)
    and warm activation stops being sub-second. It renders from a TOP-LEVEL `at_scale_contention`
    object (mirrors scale_proof/.../warm_pool_acquisition — a scalar-bundle top-level block). This
    coercer mirrors render/schema.py's AT_SCALE_CONTENTION_FIELDS exactly so emitter and renderer
    share one contract (a drift is caught by the cross-contract test).

    REQUIRED spine: runtime_class (enum-validated against the public runtime set, fail-closed),
    pool_size + claim_count (pos int), ttfe_p50_ms + ttfe_p95_ms (nonneg). Any missing/invalid
    required field returns None (the block renders nothing rather than a partial lie). OPTIONAL
    decomposition/provenance: bind_p50_ms + bind_p95_ms + exec_p50_ms + exec_p95_ms (nonneg),
    exec_success_rate (0..1), node_count (int 0<v<10000), machine_type (bounded GCP shape),
    measured_at (ISO-8601). A present-but-invalid optional value is DROPPED (not fail-closed) —
    mirroring the render cleaner's per-field drop, so a partial fire renders a partial-but-honest
    block, never a fabricated 0. Per-node throughput is DELIBERATELY ABSENT from the schema: this
    point ran at node_count=1, non-comparable to the node_count=20 concurrent-burst per-node axis.
    """
    if not isinstance(raw, dict):
        return None

    def _clean_nonneg(x):
        if isinstance(x, bool) or not isinstance(x, Real):
            return None
        fx = float(x)
        if fx != fx or fx in (float("inf"), float("-inf")) or fx < 0:
            return None
        return fx

    runtime_class = raw.get("runtime_class")
    if runtime_class not in WARM_VS_COLD_RUNTIME_CLASS_ENUM:
        return None
    pool_size = raw.get("pool_size")
    if isinstance(pool_size, bool) or not isinstance(pool_size, int) or not (0 < pool_size < 100000):
        return None
    claim_count = raw.get("claim_count")
    if isinstance(claim_count, bool) or not isinstance(claim_count, int) or not (0 < claim_count < 100000):
        return None
    ttfe_p50 = _clean_nonneg(raw.get("ttfe_p50_ms"))
    if ttfe_p50 is None:
        return None
    ttfe_p95 = _clean_nonneg(raw.get("ttfe_p95_ms"))
    if ttfe_p95 is None:
        return None

    out = {
        "runtime_class": runtime_class,
        "pool_size": pool_size,
        "claim_count": claim_count,
        "ttfe_p50_ms": ttfe_p50,
        "ttfe_p95_ms": ttfe_p95,
    }

    # OPTIONAL — a present-but-invalid value is DROPPED per-field (mirrors the render cleaner),
    # never fabricated, so a partial fire renders a partial-but-honest block.
    for opt in ("bind_p50_ms", "bind_p95_ms", "exec_p50_ms", "exec_p95_ms"):
        if opt in raw:
            ov = _clean_nonneg(raw[opt])
            if ov is not None:
                out[opt] = ov
    esr = raw.get("exec_success_rate")
    if not isinstance(esr, bool) and isinstance(esr, Real) and 0.0 <= float(esr) <= 1.0:
        out["exec_success_rate"] = esr
    nc = raw.get("node_count")
    if not isinstance(nc, bool) and isinstance(nc, int) and 0 < nc < 10000:
        out["node_count"] = nc
    mt = raw.get("machine_type")
    if isinstance(mt, str) and _MACHINE_TYPE_RE.match(mt):
        out["machine_type"] = mt
    ma = raw.get("measured_at")
    if isinstance(ma, str) and ma:
        out["measured_at"] = ma
    return out


def _coerce_cluster_saturation(raw):
    """Keep the closed top-level cluster-saturation ceiling shape (hb#132); None ⇒ omit the key.

    The hb#132 block is the third cluster-scale question: a 1:1 ALL-WARM fire (pool == claim, NOT
    over-subscribed) driven to CLUSTER SATURATION across many nodes, where the bind path saturates
    even though every claim has a ready warm pool member. Distinct from at_scale_contention (the
    OVER-subscribed ceiling at node_count=1) and concurrent_burst (small-N 1:1 bursts). It renders
    from a TOP-LEVEL `cluster_saturation` object. This coercer mirrors render/schema.py's
    CLUSTER_SATURATION_FIELDS exactly so emitter and renderer share one contract (a drift is caught
    by the cross-contract test).

    REQUIRED spine: runtime_class (enum-validated, fail-closed), pool_size + claim_count (pos int),
    node_count (int 0<v<10000 — REQUIRED here, unlike at_scale_contention, because a per-cluster
    throughput is only meaningful against the node count it was measured at), ttfe_p50_ms +
    ttfe_p95_ms (nonneg), and the measured per-cluster throughput triple thpt_under_5s_per_cluster +
    thpt_under_1s_per_cluster + thpt_cluster_node_count (nonneg — the coupled-triple rule: a
    per-cluster figure with no measurement size to disclose is meaningless). Any missing/invalid
    required field returns None (the block renders nothing rather than a partial lie). OPTIONAL:
    per-node throughput halves, bind/exec decomposition (nonneg), exec_success_rate (0..1), outcome
    (canonicalized to the closed enum so the FAIL ceiling headlines honestly), run_id (bounded
    shape), machine_type (bounded GCP shape), measured_at (ISO-8601). A present-but-invalid optional
    value is DROPPED (mirrors the render cleaner's per-field drop), never fail-closed.
    """
    if not isinstance(raw, dict):
        return None

    def _clean_nonneg(x):
        if isinstance(x, bool) or not isinstance(x, Real):
            return None
        fx = float(x)
        if fx != fx or fx in (float("inf"), float("-inf")) or fx < 0:
            return None
        return fx

    runtime_class = raw.get("runtime_class")
    if runtime_class not in WARM_VS_COLD_RUNTIME_CLASS_ENUM:
        return None
    pool_size = raw.get("pool_size")
    if isinstance(pool_size, bool) or not isinstance(pool_size, int) or not (0 < pool_size < 100000):
        return None
    claim_count = raw.get("claim_count")
    if isinstance(claim_count, bool) or not isinstance(claim_count, int) or not (0 < claim_count < 100000):
        return None
    node_count = raw.get("node_count")
    if isinstance(node_count, bool) or not isinstance(node_count, int) or not (0 < node_count < 10000):
        return None
    ttfe_p50 = _clean_nonneg(raw.get("ttfe_p50_ms"))
    if ttfe_p50 is None:
        return None
    ttfe_p95 = _clean_nonneg(raw.get("ttfe_p95_ms"))
    if ttfe_p95 is None:
        return None
    thpt_5s_cluster = _clean_nonneg(raw.get("thpt_under_5s_per_cluster"))
    if thpt_5s_cluster is None:
        return None
    thpt_1s_cluster = _clean_nonneg(raw.get("thpt_under_1s_per_cluster"))
    if thpt_1s_cluster is None:
        return None
    thpt_cluster_nc = _clean_nonneg(raw.get("thpt_cluster_node_count"))
    if thpt_cluster_nc is None:
        return None

    out = {
        "runtime_class": runtime_class,
        "pool_size": pool_size,
        "claim_count": claim_count,
        "node_count": node_count,
        "ttfe_p50_ms": ttfe_p50,
        "ttfe_p95_ms": ttfe_p95,
        "thpt_under_5s_per_cluster": thpt_5s_cluster,
        "thpt_under_1s_per_cluster": thpt_1s_cluster,
        "thpt_cluster_node_count": thpt_cluster_nc,
    }

    # OPTIONAL — a present-but-invalid value is DROPPED per-field (mirrors the render cleaner),
    # never fabricated, so a partial fire renders a partial-but-honest block.
    for opt in (
        "thpt_under_5s_per_node", "thpt_under_1s_per_node",
        "bind_p50_ms", "bind_p95_ms", "exec_p50_ms", "exec_p95_ms",
    ):
        if opt in raw:
            ov = _clean_nonneg(raw[opt])
            if ov is not None:
                out[opt] = ov
    esr = raw.get("exec_success_rate")
    if not isinstance(esr, bool) and isinstance(esr, Real) and 0.0 <= float(esr) <= 1.0:
        out["exec_success_rate"] = esr
    outcome = raw.get("outcome")
    if isinstance(outcome, str):
        outcome = _OUTCOME_CANON.get(outcome.lower(), outcome)
        if outcome in OUTCOME_ENUM:
            out["outcome"] = outcome
    rid = raw.get("run_id")
    if isinstance(rid, str) and _RUN_ID_RE.match(rid):
        out["run_id"] = rid
    mt = raw.get("machine_type")
    if isinstance(mt, str) and _MACHINE_TYPE_RE.match(mt):
        out["machine_type"] = mt
    ma = raw.get("measured_at")
    if isinstance(ma, str) and ma:
        out["measured_at"] = ma
    return out


def _coerce_provisioning_rate_sweep(raw):
    """Keep the closed top-level provisioning_rate_sweep shape (#4086); None ⇒ omit the key.

    The #4086 block is the honest reconcile-throughput ceiling: for each offered warm-pool
    provisioning rate (sandboxes/sec) it publishes what fraction of the pool reached Ready
    WITHIN pool_warm_timeout. It renders from a TOP-LEVEL `provisioning_rate_sweep` object
    (mirrors scale_proof — a list-bearing top-level block, which cannot ride sla_metrics). This
    coercer mirrors render/schema.py's PROVISIONING_RATE_SWEEP_FIELDS exactly so emitter and
    renderer share one contract (a drift is caught by the cross-contract test).

    REQUIRED spine: rate_points (non-empty list; each point needs offered_rate_per_s pos int +
    ready_pct 0..100). Any missing/invalid required field returns None (the block renders nothing
    rather than a partial lie). OPTIONAL per-point color: warmpool_size (pos int), elapsed_s /
    timeout_s (nonneg), converged (bool). OPTIONAL top-level: runtime_class (enum-validated,
    fail-closed like at_scale_contention), ceiling_low_per_s / ceiling_high_per_s (nonneg),
    measured_at (ISO-8601, carried across the daily refresh like scale_proof). A present-but-invalid
    per-point value fails the WHOLE block closed (a bad point is never rendered as a partial row);
    a present-but-invalid OPTIONAL top-level value is dropped per-field (partial-but-honest).
    """
    if not isinstance(raw, dict):
        return None

    def _clean_nonneg(x):
        if isinstance(x, bool) or not isinstance(x, Real):
            return None
        fx = float(x)
        if fx != fx or fx in (float("inf"), float("-inf")) or fx < 0:
            return None
        return fx

    points = raw.get("rate_points")
    if not isinstance(points, list) or not points:
        return None
    clean_points = []
    for p in points:
        if not isinstance(p, dict):
            return None
        rate = p.get("offered_rate_per_s")
        if isinstance(rate, bool) or not isinstance(rate, int) or not (0 < rate < 100000):
            return None
        pct = p.get("ready_pct")
        if isinstance(pct, bool) or not isinstance(pct, Real):
            return None
        fpct = float(pct)
        if fpct != fpct or fpct in (float("inf"), float("-inf")) or not (0.0 <= fpct <= 100.0):
            return None
        cp = {"offered_rate_per_s": rate, "ready_pct": fpct}
        wps = p.get("warmpool_size")
        if wps is not None:
            if isinstance(wps, bool) or not isinstance(wps, int) or not (0 < wps < 100000000):
                return None
            cp["warmpool_size"] = wps
        for k in ("elapsed_s", "timeout_s"):
            if k in p:
                kv = _clean_nonneg(p[k])
                if kv is None:
                    return None
                cp[k] = kv
        conv = p.get("converged")
        if conv is not None:
            if not isinstance(conv, bool):
                return None
            cp["converged"] = conv
        clean_points.append(cp)

    out = {"rate_points": clean_points}
    rc = raw.get("runtime_class")
    if rc in WARM_VS_COLD_RUNTIME_CLASS_ENUM:
        out["runtime_class"] = rc
    for key in ("ceiling_low_per_s", "ceiling_high_per_s"):
        kv = _clean_nonneg(raw.get(key))
        if kv is not None:
            out[key] = kv
    ma = raw.get("measured_at")
    if isinstance(ma, str) and ma:
        out["measured_at"] = ma
    return out


def _coerce_provenance(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise TypeError("provenance must be a dict")

    substrate = raw.get("cluster_substrate")
    if substrate not in CLUSTER_SUBSTRATE_ENUM:
        raise ValueError(
            f"provenance.cluster_substrate {substrate!r} not in "
            f"{CLUSTER_SUBSTRATE_ENUM} — a kind number must never be read as a "
            f"GKE SLA result, so the substrate is mandatory and closed-set"
        )

    out = {"cluster_substrate": substrate}
    for f in PROVENANCE_FIELDS:
        if f == "cluster_substrate":
            continue
        v = raw.get(f)
        if v is None:
            continue
        if f == "node_count":
            if isinstance(v, bool) or not isinstance(v, int):
                continue
            out[f] = v
        elif f == "cold_start_mode":
            # Closed-enum, fail-closed: a non-enum value is a misconfiguration
            # (typo'd env), not a leak — surface it by raising rather than
            # silently dropping, so a mislabeled cold-start mode never publishes.
            if v not in COLD_START_MODE_ENUM:
                raise ValueError(
                    f"provenance.cold_start_mode {v!r} not in {COLD_START_MODE_ENUM}"
                )
            out[f] = v
        elif f == "regime":
            # Closed-enum, fail-closed (mirrors cold_start_mode): a non-enum
            # value is a misconfiguration, not a leak — raise rather than drop
            # so a mislabeled contention regime never publishes.
            if v not in WARM_REGIME_ENUM:
                raise ValueError(
                    f"provenance.regime {v!r} not in {WARM_REGIME_ENUM}"
                )
            out[f] = v
        elif f == "warm_scaling_term":
            # Closed-enum, fail-closed (mirrors regime): a non-enum value is a
            # misconfiguration, not a leak — raise rather than drop so a
            # mislabeled scaling term never publishes.
            if v not in WARM_SCALING_TERM_ENUM:
                raise ValueError(
                    f"provenance.warm_scaling_term {v!r} not in {WARM_SCALING_TERM_ENUM}"
                )
            out[f] = v
        elif f == "runtime":
            # Closed-enum, fail-closed (mirrors cold_start_mode): render's matrix
            # selects the measured runtime column from this field, so a non-enum
            # value is a misconfiguration (typo'd BENCH_MATRIX_RUNTIME), not a
            # leak — raise rather than drop so a mislabeled runtime never publishes
            # a wrong measured column.
            if v not in MATRIX_RUNTIME_ENUM:
                raise ValueError(
                    f"provenance.runtime {v!r} not in {MATRIX_RUNTIME_ENUM}"
                )
            out[f] = v
        elif isinstance(v, str) and v:
            out[f] = v
    return out


def build_results(scenario_outcomes, provenance, generated_at: str,
                  product: str = DEFAULT_PRODUCT, scale_proof=None,
                  stepup=None, warm_vs_cold=None, kata_activation=None,
                  concurrent_burst=None, warm_pool_acquisition=None,
                  at_scale_contention=None, cluster_saturation=None,
                  provisioning_rate_sweep=None) -> dict:
    """Assemble the closed-schema results dict.

    `scenario_outcomes` is the loop's per-scenario dicts (any extra keys dropped);
    `provenance` carries the substrate-aware run context; `generated_at` is an
    ISO-8601 UTC string supplied by the caller (kept out of here so this stays
    pure/clockless and unit-testable); `product` selects the closed product label
    (defaults to sandbox so existing callers are unchanged) and is validated against
    PRODUCT_ENUM fail-closed — a non-enum product is a misconfiguration, not a leak.

    `scale_proof` is the OPTIONAL top-level Scale Proof (Linearity Check) object
    (defaults None so existing callers are unchanged). When supplied it passes
    through `_coerce_scale_proof`; the key is emitted only when a valid non-empty
    scale_points list survives — a malformed/empty object omits the key entirely
    (the table renders nothing rather than a partial lie).

    `stepup` is the OPTIONAL top-level Step-Up Pareto object (defaults None so
    existing callers are unchanged; a#3960 item 4). When supplied it passes through
    `_coerce_stepup`; the key is emitted only when a valid non-empty pareto_points
    list + a known verdict survive — same partial-lie-omission contract as
    scale_proof.

    `warm_vs_cold` is the OPTIONAL top-level warm-vs-cold speedup object (#3954
    sibling; defaults None so existing callers are unchanged). When supplied it
    passes through `_coerce_warm_vs_cold`; the key is emitted only when the five
    required spine fields survive (strictly-positive legs, enum semantic +
    runtime_class) — same partial-lie-omission contract as scale_proof/stepup. The
    inner object is produced by warm_vs_cold.classify_warm_vs_cold; this makes the
    `build_results(warm_vs_cold=...)` contract documented there real.

    `kata_activation` is the OPTIONAL top-level Kata+microVM activation object (#3942; defaults
    None so existing callers are unchanged). When supplied it passes through
    `_coerce_kata_activation`; the key is emitted only when the required spine survives (enum
    runtime_class, nonneg activation/warm legs, a non-empty public-image cold_ready list, bounded
    kernel shapes) — same partial-lie-omission contract as scale_proof/stepup/warm_vs_cold. This
    block publishes pod-Ready / microVM-activation latency, NOT TTFE (Kata TTFE matrix cells stay
    pending).
    """
    if not isinstance(generated_at, str) or not generated_at:
        raise ValueError("generated_at must be a non-empty ISO-8601 UTC string")
    if product not in PRODUCT_ENUM:
        raise ValueError(f"product {product!r} not in {PRODUCT_ENUM}")

    out = {
        "product": product,
        "generated_at": generated_at,
        "provenance": _coerce_provenance(provenance),
        "scenarios": [_coerce_scenario(s) for s in scenario_outcomes],
    }
    cleaned_scale_proof = _coerce_scale_proof(scale_proof)
    if cleaned_scale_proof is not None:
        out["scale_proof"] = cleaned_scale_proof
    cleaned_stepup = _coerce_stepup(stepup)
    if cleaned_stepup is not None:
        out["stepup"] = cleaned_stepup
    cleaned_warm_vs_cold = _coerce_warm_vs_cold(warm_vs_cold)
    if cleaned_warm_vs_cold is not None:
        out["warm_vs_cold"] = cleaned_warm_vs_cold
    cleaned_kata_activation = _coerce_kata_activation(kata_activation)
    if cleaned_kata_activation is not None:
        out["kata_activation"] = cleaned_kata_activation
    cleaned_concurrent_burst = _coerce_concurrent_burst(concurrent_burst)
    if cleaned_concurrent_burst is not None:
        out["concurrent_burst"] = cleaned_concurrent_burst
    cleaned_warm_pool_acquisition = _coerce_warm_pool_acquisition(warm_pool_acquisition)
    if cleaned_warm_pool_acquisition is not None:
        out["warm_pool_acquisition"] = cleaned_warm_pool_acquisition
    cleaned_at_scale_contention = _coerce_at_scale_contention(at_scale_contention)
    if cleaned_at_scale_contention is not None:
        out["at_scale_contention"] = cleaned_at_scale_contention
    cleaned_cluster_saturation = _coerce_cluster_saturation(cluster_saturation)
    if cleaned_cluster_saturation is not None:
        out["cluster_saturation"] = cleaned_cluster_saturation
    cleaned_provisioning_rate_sweep = _coerce_provisioning_rate_sweep(provisioning_rate_sweep)
    if cleaned_provisioning_rate_sweep is not None:
        out["provisioning_rate_sweep"] = cleaned_provisioning_rate_sweep
    return out
