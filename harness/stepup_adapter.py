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
"""

from __future__ import annotations


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

    return flat
