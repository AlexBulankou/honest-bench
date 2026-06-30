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

# badge_scope (#3905) — a per-SCENARIO closed enum qualifying what a security-isolation
# PASS asserts: "control-plane" = the policy/runtime-class was admitted and correctly
# targeted (NOT data-plane traffic enforcement); "enforced" = data-plane enforcement was
# actually exercised. It renders as a suffix on the scenario's PASS cell so the public
# badge cannot over-claim enforcement. Optional per-scenario — dropped when absent,
# fail-closed when present (a non-enum value is a misconfiguration, not a leak).
BADGE_SCOPE_ENUM = ("control-plane", "enforced")

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

# #3954 sibling warm-vs-cold — the emitter's INDEPENDENT copies of render's WARM_VS_COLD_FIELDS
# closed vocabularies (semantic = the two measured TTFx modes; runtime_class = the keys of render's
# RUNTIME_LABELS). The classifier (warm_vs_cold.classify_warm_vs_cold) only PARITY-checks
# runtime_class as a non-empty string — it does NOT enum-validate it — so this coercer is the
# fail-closed PII guard that keeps an out-of-enum or free-text runtime off the public page. A drift
# from render's set is caught by the cross-contract test, not papered over by a shared import. A
# value outside either set invalidates the whole warm_vs_cold block (-> None).
WARM_VS_COLD_SEMANTIC_ENUM = ("ttfi", "ttfe")
WARM_VS_COLD_RUNTIME_CLASS_ENUM = ("gvisor", "kata-microvm")

PROVENANCE_FIELDS = (
    "cluster_substrate",
    "controller_image",
    "controller_digest",
    "crd_version",
    "suite_git_sha",
    "run_id",
    "node_count",
    "cold_start_mode",
)
SCENARIO_FIELDS = ("name", "outcome", "pending_reason", "badge_scope", "n", "sla_metrics")

# sla_metric keys must be machine-readable metric names: lowercase alphanumerics
# separated by underscore or hyphen. No spaces, colons, slashes, or dots — a
# leaked path/DSN/host:port cannot pass this shape, so an excerpt smuggled in as
# an sla key is dropped. Underscores are permitted so render's canonical metric
# keys (activation_ms, cold_start_ms, …) pass; hyphen forms still pass too.
import re

_METRIC_KEY_RE = re.compile(r"^[a-z0-9]+(?:[_-][a-z0-9]+)*$")

# GCP machine-type shape — emitter-side bound (independent mirror of render/schema.py's
# _MACHINE_TYPE). The family/class tokens are PUBLIC GCP identifiers; we bound the shape so a
# free-text value can never ride the stepup machine_type field. A value that is not a
# recognizable GCP machine shape is dropped.
_MACHINE_TYPE_RE = re.compile(
    r"^[a-z][a-z0-9]*-(standard|highmem|highcpu|micro|small|medium)(-[0-9]+)?$"
)


def _coerce_sla_metrics(raw) -> dict:
    """Keep only {safe-key: finite-number}; drop everything else.

    This is the load-bearing leak-suspenders: an sla value MUST be numeric, so a
    raw failure_excerpt string stuffed into sla_metrics is dropped, never emitted.
    """
    out: dict = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        if not isinstance(k, str) or not _METRIC_KEY_RE.match(k):
            continue
        # bool is a subclass of int — exclude it; an sla metric is a measurement.
        if isinstance(v, bool) or not isinstance(v, Real):
            continue
        fv = float(v)
        if fv != fv or fv in (float("inf"), float("-inf")):  # NaN / inf
            continue
        out[k] = fv
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
        rate = p.get("offered_rate_per_s")
        if isinstance(rate, bool) or not isinstance(rate, int) or not (0 < rate < 100000):
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


def _coerce_saturation_point(raw, clean_nonneg):
    """Keep the closed operator saturation-point shape (#4030); None -> omit the key.

    Mirrors render/schema.py's _stepup_saturation_point_ok exactly. tight_ms + loose_ms are
    REQUIRED positive bar floats; basis is an OPTIONAL non-empty descriptor. Each leg (warm/
    cold) is OPTIONAL; its max_rate_under_{tight,loose} is kept only as a positive int — a None
    or absent value is DROPPED (the renderer prints em-dash, never a fabricated 0). At least one
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
            rv = lv.get(k)
            if not isinstance(rv, bool) and isinstance(rv, int) and 0 < rv < 100000:
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

    verdict is REQUIRED (unknown -> None). The two Pareto tables are BLOCK-LEVEL relaxed: the
    true-TTFE `pareto_points` is OMITTED (not emitted empty) when no step measured a true TTFE
    warm p95 — the #3975 gap — and the controller_startup LOWER-BOUND proxy block stands in. At
    least ONE of {pareto_points, controller_startup} must be present and valid; an all-empty
    sweep returns None (the table renders nothing rather than a partial lie). True-TTFE per-point:
    offered_rate_per_s (int 0<r<100000) + ttfe_p95_ms (nonneg, the honesty spine) required;
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
            rate = p.get("offered_rate_per_s")
            if isinstance(rate, bool) or not isinstance(rate, int) or not (0 < rate < 100000):
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

    # Block-level relaxation: emit only when at least one of the two Pareto tables is populated.
    # An all-empty sweep (no true-TTFE points AND no valid proxy block) is honest "nothing", -> None.
    if not clean_points and controller is None:
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

    # Optional positive-int characteristic rates (None in the source when no breach).
    for key in ("north_star_breach_rate", "saturation_rate", "max_flat_rate"):
        v = raw.get(key)
        if isinstance(v, bool) or not isinstance(v, int) or not (0 < v < 100000):
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
        elif isinstance(v, str) and v:
            out[f] = v
    return out


def build_results(scenario_outcomes, provenance, generated_at: str,
                  product: str = DEFAULT_PRODUCT, scale_proof=None,
                  stepup=None, warm_vs_cold=None) -> dict:
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
    return out
