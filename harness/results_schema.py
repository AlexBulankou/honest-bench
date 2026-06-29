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
PRODUCT_ENUM = ("sandbox", "substrate")
DEFAULT_PRODUCT = "sandbox"

# Closed value-sets. A value outside the set is a real bug, not a leak, so the
# emitter fails closed (raises) rather than silently dropping the field.
# Canonical render vocabulary (render/schema.py is the display contract): PASS/FAIL
# render uppercase, pending lowercase. The scenario bodies return mixed case, so we
# canonicalize on the way in via _OUTCOME_CANON.
OUTCOME_ENUM = ("PASS", "FAIL", "pending")
_OUTCOME_CANON = {"pass": "PASS", "fail": "FAIL", "pending": "pending"}
CLUSTER_SUBSTRATE_ENUM = ("kind", "gke", "gke-sandbox")
# pending_reason is a FIXED enum — never free text. A pending cell says exactly
# why it is pending, drawn only from this set. Kebab-case to match render's
# PENDING_REASONS exactly (a reason outside render's set renders nothing).
PENDING_REASON_ENUM = (
    "requires-gvisor-runtime",
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
      - scale_points: list of {node_count:int (0<nc<10000), density:float>=0}
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
        clean_points.append({"node_count": nc, "density": fdn})
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
                  product: str = DEFAULT_PRODUCT, scale_proof=None) -> dict:
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
    return out
