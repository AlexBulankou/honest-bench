"""Flatten the step-up sweep's NESTED producer shape into the FLAT shape the
closed-schema emitter (`results_schema._coerce_stepup`) reads — no cluster, no I/O.

The CL2 step-up backfill producer assembles a sweep result as a NESTED record::

    {"method": "stepup-backfill",
     "params":     {sld_s, wpr, runtime_class, north_star_p95_ms, collapse_p95_ms,
                    machine_type, cluster_nodes},
     "steps":      [...],
     "saturation": {verdict, north_star_breach_rate, saturation_rate, max_flat_rate,
                    measured_steps, unmeasured_steps, north_star_ms, collapse_ms},
     "pareto":     [{offered_rate_per_s, ready_per_s, ttfe_p95_ms, ttfe_p50_ms, ttfe_p99_ms}]}

`_coerce_stepup`, by contrast, reads a FLAT record: `pareto_points` + `verdict` at the
top level, with the characteristic rates and the Little's-law scalars as top-level
optionals. This adapter is the one-way bridge between the two shapes. It is a pure
flatten — it renames two keys and lifts two nested objects to the top level:

  - ``pareto``            -> ``pareto_points``   (per-point keys already match the schema)
  - ``params.cluster_nodes`` -> ``node_count``   (the schema's name for the same scalar)
  - ``saturation.{verdict, north_star_breach_rate, saturation_rate, max_flat_rate}`` -> top level
  - ``params.{sld_s, wpr, machine_type}``                                            -> top level

It does NOT validate, clamp, or fabricate — `_coerce_stepup` is the single closed-schema
gate and is tolerant of `None`/absent for every optional, so a shakeout run whose
`cluster_nodes` (and/or `machine_type`) is `None` flows straight through and the schema
simply omits `node_count` (and `machine_type`). The adapter therefore preserves the
honesty spine: it never turns a missing scalar into a fabricated value, it just relabels.

The verdict vocabulary is identical on both sides (the 4-value set locked in the schema),
so no remap is needed; an unknown verdict is left for `_coerce_stepup` to reject.

`enrich_pareto_cost` is the optional second pass that lights up the item-4 COST axis:
after flattening, it stamps each Pareto point's `cost_usd_per_1k_ready` from that point's
measured `ready_per_s` and the record's `node_count` + `machine_type` (via `cost.py`),
keeping the same honesty spine — a None cost (unknown price, no measured throughput)
leaves the key absent rather than fabricating one.
"""

from __future__ import annotations

from . import cost as _cost


def stepup_nested_to_flat(rec):
    """Relabel a nested step-up sweep record into the flat `_coerce_stepup` shape.

    Returns a flat dict ready to hand to `results_schema._coerce_stepup`. A non-dict
    input yields an empty dict (which `_coerce_stepup` rejects to `None`, emitting no
    stepup block — the honest "nothing measured" path rather than a partial lie).
    """
    if not isinstance(rec, dict):
        return {}

    params = rec.get("params")
    params = params if isinstance(params, dict) else {}
    sat = rec.get("saturation")
    sat = sat if isinstance(sat, dict) else {}

    flat = {
        "pareto_points": rec.get("pareto"),
        "verdict": sat.get("verdict"),
        "north_star_breach_rate": sat.get("north_star_breach_rate"),
        "saturation_rate": sat.get("saturation_rate"),
        "max_flat_rate": sat.get("max_flat_rate"),
        "sld_s": params.get("sld_s"),
        "wpr": params.get("wpr"),
        "node_count": params.get("cluster_nodes"),
        "machine_type": params.get("machine_type"),
    }

    measured_at = rec.get("measured_at")
    if measured_at:
        flat["measured_at"] = measured_at

    # Lift the controller-startup LOWER-BOUND proxy block (#3975). The producer keeps it nested
    # alongside an EMPTY true-TTFE `pareto` while the true-TTFE gap is open; this is the bridge to
    # the flat shape `_coerce_controller_startup` reads. Pure relabel, same honesty spine as the
    # outer flatten: rename `pareto` -> `pareto_points`, lift `saturation.verdict` -> `verdict`,
    # carry `lower_bound` verbatim. The producer's free-text `caveat` is RENDER-OWNED and is
    # DELIBERATELY NOT copied — the public schema carries only the `lower_bound` boolean, off which
    # render emits its fixed lower-bound boilerplate. A non-dict block is omitted (the coercer then
    # sees no proxy and, with an empty true-TTFE pareto, drops the whole stepup — honest "nothing").
    cs = rec.get("controller_startup")
    if isinstance(cs, dict):
        cs_flat = {
            "lower_bound": cs.get("lower_bound"),
            "pareto_points": cs.get("pareto"),
        }
        cs_sat = cs.get("saturation")
        if isinstance(cs_sat, dict):
            cs_flat["verdict"] = cs_sat.get("verdict")
        flat["controller_startup"] = cs_flat

    # Lift the literal-TTFE UPPER-BOUND leg (hb#174). Same pure-relabel bridge as the
    # controller_startup lift: `pareto` -> `pareto_points`, `saturation.verdict` ->
    # `verdict`, polarity flags (`upper_bound`, `includes_exec_setup_overhead`) carried
    # verbatim. The producer's per-rung keys stay NAMESPACED (`literal_warm_p95_ms`,
    # `acq_fulfilled_per_s`, `controller_completed_per_s`) — never aliased onto the
    # true-TTFE names; slo_rate owns the basis pick + stamp downstream. The producer's
    # free-text `caveat` is RENDER-OWNED and deliberately NOT copied (render keys its
    # fixed upper-bound boilerplate off the booleans). Non-dict block => omitted.
    lt = rec.get("literal_ttfe")
    if isinstance(lt, dict):
        lt_flat = {
            "upper_bound": lt.get("upper_bound"),
            "includes_exec_setup_overhead": lt.get("includes_exec_setup_overhead"),
            "pareto_points": lt.get("pareto"),
        }
        lt_sat = lt.get("saturation")
        if isinstance(lt_sat, dict):
            lt_flat["verdict"] = lt_sat.get("verdict")
        flat["literal_ttfe"] = lt_flat

    return flat


def enrich_pareto_cost(flat, *, usd_per_node_hour=None):
    """Populate `cost_usd_per_1k_ready` on each Pareto point of a flat record.

    The step-up item-4 cost axis: the schema reserved `cost_usd_per_1k_ready` as an
    optional Pareto-point field and `cost.cost_usd_per_1k_ready` computes it, but until
    something joins the two the field rendered permanently ``pending``. This is that
    join in the adapter path — it walks the flattened `pareto_points` and stamps each
    point's measured `ready_per_s` against the record's cluster shape (`node_count` +
    `machine_type`, both already lifted by `stepup_nested_to_flat`), with an optional
    explicit `usd_per_node_hour` that overrides the machine_type list-price fallback.

    Honesty spine (mirrors the cost helper's own posture): the cost is written ONLY when
    `cost_usd_per_1k_ready` returns a real number. A None result — unknown machine_type
    with no explicit rate, a non-positive/None `ready_per_s`, a missing/non-positive
    `node_count` — leaves the key ABSENT (honest ``pending``), never a fabricated 0 or a
    guessed cost. Mutates and returns `flat` in place; a non-list `pareto_points` (the
    nothing-measured path) is a no-op.
    """
    points = flat.get("pareto_points")
    if not isinstance(points, list):
        return flat

    node_count = flat.get("node_count")
    machine_type = flat.get("machine_type")

    for pt in points:
        if not isinstance(pt, dict):
            continue
        c = _cost.cost_usd_per_1k_ready(
            pt.get("ready_per_s"),
            node_count=node_count,
            usd_per_node_hour=usd_per_node_hour,
            machine_type=machine_type,
        )
        if c is not None:
            pt["cost_usd_per_1k_ready"] = c

    return flat
