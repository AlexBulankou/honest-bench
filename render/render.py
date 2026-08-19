"""Closed-schema README renderer for the public honest-benchmarks repo.

Consumes a harness results JSON and emits a Markdown table + build banner + provenance
footer. It is generate-only: every cell traces to a schema-validated field of the input,
goal columns render "(non-public)" when the internal targets file is absent, and any field
not declared in schema.py is silently dropped. No hand-entered numbers, no free-text.

Usage:
  python3 render.py <results.json> [--targets <targets.json>]   # prints one product table
"""

import argparse
import datetime
import json
import math
import sys

from schema import (
    ACTIVATION_MODE_ROWS,
    AT_SCALE_CONTENTION_FIELDS,
    backfill_legacy_history_row,
    BADGE_CONSTRUCTIONS,
    BADGE_SCOPES,
    BURST_CORROBORATION_FIELDS,
    CLUSTER_SATURATION_FIELDS,
    CONCURRENT_BURST_FIELDS,
    DENSITY_SOURCE_SCENARIOS,
    GOAL_COLUMNS,
    HISTORY_FIELDS,
    KATA_ACTIVATION_FIELDS,
    LIST_PRICE_AS_OF,
    MATRIX_METRIC_FIELDS,
    MATRIX_RUNTIMES,
    METRIC_LABELS,
    NON_PUBLIC,
    OUTCOMES,
    PENDING_REASONS,
    PRODUCTS,
    PROVENANCE_FIELDS,
    PROVISIONING_RATE_SWEEP_FIELDS,
    RUNTIME_LABELS,
    SCALE_PROOF_FIELDS,
    SCENARIO_LABELS,
    SESSION_TURNOVER_FIELDS,
    STEPUP_PARETO_FIELDS,
    STORAGE_BASIS_DEFAULT,
    STORAGE_CLASS_FIELDS,
    STORAGE_CLASS_LABELS,
    STORAGE_CLASSES,
    storage_class_basis_ok,
    storage_payload_bytes_ok,
    SUSPEND_LATENCY_FIELDS,
    TTFE_COMPARABILITY_MIN_N,
    WARM_BIND_FIELDS,
    WARM_POOL_ACQUISITION_FIELDS,
    WARM_VS_COLD_FIELDS,
    WARMPOOL_SEPARATION_HISTORY_FIELDS,
    WARMPOOL_SEPARATION_MIN_RATIO,
    WARMPOOL_SEPARATION_VARIANCE_MIN_SPREAD,
    _ISO,
    _SHA256,
)
from wip import NA_BY_CONSTRUCTION, build_work_in_progress, link_pending, wip_link
from upstream_links import META as _UPSTREAM_META
from upstream_links import upstream_cell_refs, upstream_prose_refs
import warmpool_verdict

# #4137: the sentence appended to the drained-regime warm caveat that NAMES the term driving
# warm-hit TTFE growth with claim-count. Keyed by the schema WARM_SCALING_TERMS enum so the
# attribution is data-driven, not hand-entered free-text; the dict .get() below is what
# validates the emitted value against this closed vocabulary (an out-of-enum value renders no
# clause). Every WARM_SCALING_TERMS member MUST have an entry here — enforced by a sync test.
_WARM_SCALING_TERM_CLAUSE = {
    "bind-concurrency": (
        " The term that grows with claim-count here is **bind (provisioning) concurrency**, "
        "not exec: on this fixed drained node-set the per-claim bind time climbs as more "
        "claims contend for provisioning while exec stays flat — so the warm-hit distribution "
        "straddles 1s at higher N because of provisioning concurrency, not the exec channel."
    ),
}


def _clean_provenance(prov):
    """Return only schema-declared provenance keys whose values validate. Drops the rest."""
    if not isinstance(prov, dict):
        return {}
    out = {}
    for key, ok in PROVENANCE_FIELDS.items():
        if key in prov:
            try:
                if ok(prov[key]):
                    out[key] = prov[key]
            except (TypeError, ValueError):
                pass
    return out


def _fork_provenance_str(prov):
    """Compose the fork-build source string from three closed-schema parts, or "" when INERT.

    WS4(c), epic #6669. When these numbers were measured against a controller built from
    alex's fork (not the upstream-published image), build_provenance stamps three parts —
    ``fork_sha``, ``fork_base_upstream_sha``, and ``fork_fix_count`` — each validated by
    PROVENANCE_FIELDS before reaching here. The renderer assembles them into the display
    string ``fork@<sha> (+N fixes over upstream@<base>)`` so no space/paren-carrying blob has
    to survive the closed-schema allow-list.

    Returns "" (INERT — build banner byte-unchanged) unless ALL THREE parts are present AND
    ``fork_fix_count > 0``: a prebuilt-image (non-fork) fire stamps none of them, and a fork
    build that is even with its base (0 fixes ahead) has no fix-delta worth claiming, so
    neither renders a source line.
    """
    if not isinstance(prov, dict):
        return ""
    fork_sha = prov.get("fork_sha")
    base = prov.get("fork_base_upstream_sha")
    n = prov.get("fork_fix_count")
    if not (fork_sha and base and isinstance(n, int) and not isinstance(n, bool) and n > 0):
        return ""
    return f"fork@{fork_sha} (+{n} fixes over upstream@{base})"


def _machine_class_caveat(prov):
    """One-line caveat about this run's machine class, or "" when INERT.

    Two distinct emissions, both keyed off provenance:

    1. **Rig-unknown (fail-closed guard).** A sandbox-family run carries
       ``runtime`` in provenance; that is exactly the run where machine class is
       load-bearing. If such a run did NOT stamp ``machine_type`` (unknown rig —
       build_provenance omits it rather than guessing), returning "" would render
       the page with zero machine-class disclosure, which a reader reads as
       "rig unchanged" — a silent downgrade of a trust surface. So emit an
       explicit rig-unknown marker instead: the loss of the source fails loud
       (guard-then-fill), never quiet. Substrate runs carry no ``runtime`` and so
       never trip this guard.
    2. **Machine-class change.** When both ``machine_type`` and
       ``prior_machine_type`` are present and differ, emit the confounded-delta
       caveat. Both values are closed-schema-validated GCP machine shapes (never
       free text) cleaned by _clean_provenance before reaching here, so the caveat
       can safely quote them. build_provenance only stamps prior_machine_type when
       the rig actually changed, so a same-rig refresh needs no caveat and this
       stops rendering by construction the moment two consecutive runs share a
       machine class.
    """
    if not isinstance(prov, dict):
        return ""
    current = prov.get("machine_type")
    prior = prov.get("prior_machine_type")
    runtime = prov.get("runtime")
    # (1) Fail-closed guard: sandbox-family run (runtime present) with no stamped
    # machine_type. Do not silently render no caveat — mark the rig as unknown.
    if runtime and not current:
        return (
            "> ⚠️ **Machine class unknown:** this sandbox-family run did not stamp "
            "`machine_type`, so a rig change relative to the previously published run "
            "cannot be ruled out. Treat any delta as possibly machine-class-confounded "
            "until a matched-rig run is published."
        )
    # (2) Machine-class change between two stamped, differing rigs.
    if not current or not prior or current == prior:
        return ""
    return (
        f"> ⚠️ **Machine-class change:** this run measured on `{current}`; the previously "
        f"published run was on `{prior}`. Read any delta against the prior run as "
        "machine-class-confounded, not a substrate signal, until corroborated on a matched rig."
    )


def _stale_carry_forward_caveat(section, prov, *, label):
    """Stale-carry-forward disclosure for sections with no daily in-process producer, or "" when
    INERT (hb#612; Transition guards on trust surfaces, AGENTS.md #4420).

    at_scale_contention and concurrent_burst have no producer in the daily single-node
    auto-refresh — they are produced only by heavy, manual, collision-acked fires and carried
    forward byte-unchanged (harness/run.py's `carry_prior_*` functions) across every daily
    refresh in between, retaining their ORIGINAL `machine_type`/`measured_at`. Prior to this,
    neither section's renderer compared its own stamped `machine_type` against the CURRENT run's
    top-level `provenance.machine_type`, so a rig change since the section's last fire rendered
    silently as if still fresh — the section already prints its own `_Measured {date}_`/"Cluster
    shape" line, but nothing tied a stale rig to that date. That is exactly the "downgrades trust
    quietly" failure the trust-surface idiom forbids: the section's own freshness has silently
    regressed (still-valid rig -> now-stale rig) with no loud reopen.

    Returns "" (INERT) when either machine_type is absent or they match — nothing to disclose,
    mirroring `_machine_class_caveat`'s fail-closed shape (no claim without two comparable,
    validated values). Deliberately scoped to disclosure only: detecting an INTRA-run mixed-rig
    confound (comparing a section against sibling sections, not the top-level run) is #608's
    separate, still-open concern.
    """
    if not isinstance(section, dict) or not isinstance(prov, dict):
        return ""
    section_mt = section.get("machine_type")
    current_mt = prov.get("machine_type")
    if not section_mt or not current_mt or section_mt == current_mt:
        return ""
    return (
        f"> ⚠️ **Stale — no producer since rig change:** this {label} figure has no daily "
        f"producer; it is carried forward unchanged from its last fire, measured on "
        f"`{section_mt}`. This run measured the rest of the page on `{current_mt}`. Treat this "
        "section as a frozen snapshot from the prior rig, not a live signal for the current one, "
        "until a fresh fire republishes it on the current machine class."
    )


# (section-key, cleaner, human label) for every section that stamps its own schema-validated
# `machine_type` (render/schema.py) and can therefore be compared against its siblings within a
# single run. Referenced only inside `_mixed_rig_confound_caveat` below, which resolves these
# names at call time, not at module-load time — so it is safe for this tuple to reference
# cleaners defined much later in this file. `render_known_anomalies_detail` already establishes
# this same forward-reference pattern (calls `_clean_concurrent_burst` despite being defined
# hundreds of lines earlier in the file).
_MIXED_RIG_SECTIONS = (
    ("at_scale_contention", lambda r: _clean_at_scale_contention(r), "at-scale contention"),
    ("concurrent_burst", lambda r: _clean_concurrent_burst(r), "concurrent burst"),
    ("cluster_saturation", lambda r: _clean_cluster_saturation(r), "cluster saturation"),
    ("warm_pool_acquisition", lambda r: _clean_warm_pool_acquisition(r), "warm-pool acquisition"),
    ("stepup", lambda r: _clean_stepup(r), "step-up sweep"),
)


def _mixed_rig_confound_caveat(results):
    """Loud disclosure when per-section `machine_type` stamps disagree WITHIN a single run,
    or "" when INERT (hb#608; Transition guards on trust surfaces, AGENTS.md #4420).

    `_machine_class_caveat` and `_stale_carry_forward_caveat` both compare a value against a
    PRIOR run (inter-run) — a same-run mixed-rig confound slips through both: at_scale_contention
    and concurrent_burst are carried forward from whatever rig last fired a heavy manual
    measurement (harness/run.py's `carry_prior_*`), so a single published run can legitimately
    mix a freshly-measured `n2-standard-16` top-level/cluster_saturation/warm_pool_acquisition
    figure with an `e2-standard-16` at_scale_contention/concurrent_burst figure carried forward
    from weeks earlier, with nothing on the page saying so (hb#608's concrete example: hb#594's
    2026-07-01 23:51:53Z auto-refresh republished exactly this split silently for ~6 weeks).

    Collects every present, schema-validated `machine_type` — top-level provenance plus every
    section in `_MIXED_RIG_SECTIONS` — and, when two or more DISTINCT values are present, emits
    one caveat naming which sections sit on which machine class. This is independent of whether
    any single section's own value changed vs. its own prior fire (that axis is `_stale_carry_
    forward_caveat`'s); this check only asks whether the sections agree with each other RIGHT NOW.

    Returns "" (INERT) when fewer than two distinct machine classes are present across every
    section that stamped one — the common case where every section measured on the same rig, or
    at most one section stamped `machine_type` at all — mirroring the sibling checks' fail-closed
    shape (no claim without ≥2 comparable, validated values).
    """
    prov = _clean_provenance(results.get("provenance"))
    seen = {}
    top_mt = prov.get("machine_type") if isinstance(prov, dict) else None
    if top_mt:
        seen.setdefault(top_mt, []).append("top-level provenance")
    for _key, cleaner, label in _MIXED_RIG_SECTIONS:
        section = cleaner(results)
        if not isinstance(section, dict):
            continue
        mt = section.get("machine_type")
        if not mt:
            continue
        seen.setdefault(mt, []).append(label)
    if len(seen) < 2:
        return ""
    parts = "; ".join(f"`{mt}` ({', '.join(labels)})" for mt, labels in sorted(seen.items()))
    return (
        "> ⚠️ **Mixed rig within this run:** this run's sections were not all measured on the "
        f"same machine class — {parts}. Cross-section comparisons on this page may reflect "
        "hardware differences, not workload differences, until every section re-measures on one rig."
    )


def _clean_metrics(metrics):
    """Keep only known metric keys with numeric (non-bool) values."""
    if not isinstance(metrics, dict):
        return {}
    out = {}
    for key, val in metrics.items():
        if key in METRIC_LABELS and isinstance(val, (int, float)) and not isinstance(val, bool):
            out[key] = val
    return out


def _clean_scenarios(scenarios):
    """Map each row through the closed schema. Returns (rows, dropped_count).

    A row is dropped (not rendered) when its name is not in the scenario vocabulary or its
    outcome is not a known enum — exactly the cases where unexpected harness output could
    otherwise leak. Dropped rows are counted (a safe integer) so the drop is visible.
    """
    rows, dropped = [], 0
    if not isinstance(scenarios, list):
        return rows, dropped
    for s in scenarios:
        if not isinstance(s, dict):
            dropped += 1
            continue
        name = s.get("name")
        outcome = s.get("outcome")
        if name not in SCENARIO_LABELS or outcome not in OUTCOMES:
            dropped += 1
            continue
        reason = s.get("pending_reason")
        if reason is not None and reason not in PENDING_REASONS:
            reason = None  # drop unknown free-text reason, keep the row
        scope = s.get("badge_scope")
        if scope is not None and scope not in BADGE_SCOPES:
            scope = None  # drop unknown scope, keep the row (never render free-text)
        construction = s.get("badge_construction")
        if construction is not None and construction not in BADGE_CONSTRUCTIONS:
            construction = None  # drop unknown construction, keep the row
        n = s.get("n")
        n = n if isinstance(n, int) and not isinstance(n, bool) and n >= 0 else 0
        rows.append(
            {
                "label": SCENARIO_LABELS[name],
                "outcome": outcome,
                "pending_reason": reason,
                "badge_scope": scope,
                "badge_construction": construction,
                "n": n,
                "metrics": _clean_metrics(s.get("sla_metrics")),
            }
        )
    return rows, dropped


def _goal_cells():
    """Goal columns are (non-public) by construction in the public render.

    There is deliberately no targets-file input here: the internal targets never ship to
    the public repo, so the committed/target/north-star columns can only ever be (non-public).
    Keeping the targets out of this code path (rather than reading-then-suppressing) is what
    makes the guarantee structural instead of conditional.
    """
    return {c: NON_PUBLIC for c in GOAL_COLUMNS}


def _measured_cell(row, cold_start_mode=None):
    if row["outcome"] == "pending":
        reason = row["pending_reason"] or "not-yet-measured"
        return f"pending ({reason})"
    if row["outcome"] == "FAIL":
        reason = row["pending_reason"] or "not-yet-measured"
        return f"FAIL ({reason})"
    # badge_scope (#3905) qualifies what a security-isolation PASS asserts (control-plane
    # admission vs data-plane enforcement); suffix it on the PASS token so the badge cannot
    # over-claim. Absent ⇒ no suffix (graceful degradation). Applies to both the metric and
    # bare PASS forms. badge_construction (#3950) is an ORTHOGONAL second term naming WHICH
    # NetworkPolicy mechanism was measured (standard-np vs managed-np); it renders only
    # alongside a scope (it qualifies the enforcement claim and is meaningless alone), so an
    # `enforced` flip discloses the mechanism (e.g. "PASS (enforced, standard-np)") and can
    # never be read as a managed-gke-sandbox-NP guarantee it does not make.
    scope = row.get("badge_scope")
    construction = row.get("badge_construction")
    if scope and construction:
        pass_token = f"PASS ({scope}, {construction})"
    elif scope:
        pass_token = f"PASS ({scope})"
    else:
        pass_token = "PASS"
    if row["metrics"]:
        parts = []
        for k in sorted(row["metrics"]):
            part = f"{METRIC_LABELS[k]} {row['metrics'][k]:g}"
            # cold_start_mode (#3894) is run-level provenance describing the image-cache
            # posture of the cold-start measurement; surface it next to cold_start_ms so a
            # cold-pull number (which includes full layer download) is not misread as a
            # warm-cached cold-provision one. Absent ⇒ no label (graceful degradation).
            if k == "cold_start_ms" and cold_start_mode:
                part += f" ({cold_start_mode})"
            parts.append(part)
        return pass_token + " · " + ", ".join(parts)
    return pass_token


def render_product(results):
    """results: parsed dict. Goal columns always render (non-public) — see _goal_cells."""
    product = results.get("product")
    if product not in PRODUCTS:
        raise ValueError(f"unknown product (not in closed schema): {product!r}")

    prov = _clean_provenance(results.get("provenance"))
    # cold_start_mode rides in provenance (run-level) but renders on the cold_start_ms cell,
    # not the build banner (kept out of banner_order below to avoid double-rendering).
    cold_start_mode = prov.get("cold_start_mode")
    rows, dropped = _clean_scenarios(results.get("scenarios"))
    goals = _goal_cells()

    lines = [f"## {product}", ""]
    # scorecard table
    header = ["Scenario", "Measured (N)"] + [c.title() for c in GOAL_COLUMNS]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")
    for r in rows:
        cells = [
            r["label"],
            f"{_measured_cell(r, cold_start_mode=cold_start_mode)} (n={r['n']})",
            goals["committed"],
            goals["target"],
            goals["north-star"],
        ]
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")

    # build banner (only validated provenance fields)
    banner_order = [
        "cluster_substrate",
        "controller_image",
        "controller_digest",
        "crd_version",
        "suite_git_sha",
        # WS3 (epic #6669): the upstream agent-sandbox ref these numbers were measured AGAINST.
        # Stamped by build_provenance from BENCH_UPSTREAM_REF (omit-when-absent); records the
        # against-what pin so a reader can see exactly which upstream ref the numbers reflect,
        # complementing the page-level wall-clock STALE banner (render_stale_banner).
        "upstream_ref",
        "run_id",
        "node_count",
        # PR#313 review: stamp the node machine shape so a machine-class
        # change (e.g. an ephemeral CI cluster vs the persistent internal cluster)
        # is visible on the page, not silently folded into the same cluster_substrate label.
        "machine_type",
        # hb#317 follow-up: the gVisor runtime (runsc) version these numbers were produced
        # under — the runtime-side analogue of controller_image/digest. It was stamped by
        # build_provenance (from BENCH_RUNSC_VERSION) and schema-declared since hb#317 so it
        # survives _clean_provenance, but had no render leg, so it rendered nowhere — a
        # carried-but-invisible reproducibility qualifier. Surfaced here on the same
        # INERT-when-absent discipline as machine_type (the `if k in prov` guard below):
        # sandbox-family-only (gated on `runtime` in build_provenance), so it appears only on
        # a gVisor run that stamps it and is byte-unchanged on every run that does not.
        "runsc_version",
    ]
    banner = [f"{k}={prov[k]}" for k in banner_order if k in prov]
    # Fork-build source leg (WS4(c), epic #6669): appends "source=fork@<sha> (+N fixes over
    # upstream@<base>)" only on a fork-build fire that carries all three parts with a positive
    # fix count; INERT (byte-unchanged banner) on every prebuilt-image run.
    fork_str = _fork_provenance_str(prov)
    if fork_str:
        banner.append(f"source={fork_str}")
    if banner:
        lines.append("_build: " + " · ".join(banner) + "_")
    gen = results.get("generated_at")
    if isinstance(gen, str) and _ISO.match(gen):
        lines.append(f"_generated-at: {gen}_")
    if dropped:
        lines.append(f"_rows dropped by closed-schema guard: {dropped}_")
    lines.append("")
    return "\n".join(lines)


# WS3 (epic #6669) — page-level freshness self-declaration.
# Default staleness threshold: numbers older than this render a top-of-page STALE banner.
STALE_THRESHOLD_DAYS = 7


def _parse_generated_at(results):
    """Return the timezone-aware UTC datetime of a results dict's `generated_at`, or None if the
    key is missing / not a string / not the locked ISO shape / not a real calendar instant.

    Deliberately reuses schema `_ISO` for the shape gate (same contract the build banner uses),
    then strptime for the value gate — so an ISO-shaped-but-impossible stamp (e.g. month 13)
    is treated as unparseable (→ None → the fail-loud UNKNOWN path below), never silently
    accepted.
    """
    gen = results.get("generated_at") if isinstance(results, dict) else None
    if not (isinstance(gen, str) and _ISO.match(gen)):
        return None
    try:
        dt = datetime.datetime.strptime(gen, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    return dt.replace(tzinfo=datetime.timezone.utc)


def render_stale_banner(product_results, as_of, threshold_days=STALE_THRESHOLD_DAYS):
    """Return a top-of-page markdown STALE banner, or "" when the whole page is fresh.

    `product_results` is an iterable of (product_name, results_dict) for the products being
    published on the page. `as_of` is the committed freshness anchor — the date the upstream
    currency of these numbers was last verified (driven by `upstream_links.json`'s
    `_meta.last_verified`, advanced by `scripts/verify-upstream-freshness.py --update-stamp`).
    It is a REQUIRED, tz-aware `datetime` (or None). This function is intentionally PURE — it
    never reads the wall clock — so that `build_readme()` is a deterministic function of the
    committed data files and the byte-pinned golden test (`test_readme_fresh.py`) stays
    reproducible. A wall-clock anchor would (a) red the golden on every calendar day and
    (b) never actually fire, because the page only re-renders on a data-refresh fire (when the
    numbers are, by construction, ~0 days old).

    This is a TRUST-SURFACE guard (AGENTS.md "Transition guards on trust surfaces"): a measured
    page must never present a frozen number as if it were current, so the downgrade direction
    (stale / unverifiable) fails LOUD while the upgrade direction (fresh) stays silent:

      * fresh   — `as_of` is present and every product has a valid `generated_at` within
                  `threshold_days` of `as_of` → return "" (freshness is the silent state).
      * stale   — at least one product's `generated_at` is older than `threshold_days` before
                  `as_of` → loud banner naming the product(s) + age in days.
      * unknown — `as_of` is None (no committed verification anchor), OR at least one product's
                  `generated_at` is missing/malformed/impossible → loud "freshness UNVERIFIED"
                  banner (fail-closed: we cannot certify currency, so we say so).

    A page in BOTH stale and unknown states renders both lines.
    """
    if as_of is None:
        return (
            "> ⚠️ **FRESHNESS UNVERIFIED** — no upstream-verification anchor is committed "
            "(`upstream_links.json` `_meta.last_verified` is missing or malformed), so this page "
            "cannot self-certify how current these numbers are. Treat them as potentially stale; "
            "run `scripts/verify-upstream-freshness.py --update-stamp` to re-establish the anchor."
        )
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=datetime.timezone.utc)
    threshold = datetime.timedelta(days=threshold_days)
    unknown = []
    stale = []
    for name, results in product_results:
        dt = _parse_generated_at(results)
        if dt is None:
            unknown.append(name)
            continue
        age = as_of - dt
        if age > threshold:
            stale.append((name, age.days))
    if not unknown and not stale:
        return ""
    as_of_str = as_of.strftime("%Y-%m-%d")
    lines = []
    if stale:
        detail = ", ".join(f"**{name}** ({age}d old)" for name, age in stale)
        lines.append(
            f"> ⚠️ **STALE BENCHMARK DATA** — as of the last upstream-freshness check ({as_of_str}), "
            f"these numbers were last measured more than {threshold_days} days earlier: {detail}. "
            f"They may not reflect the current upstream agent-sandbox; see the `_generated-at:` and "
            f"`upstream_ref` stamps below for exactly what was measured and against which upstream ref."
        )
    if unknown:
        names = ", ".join(f"**{n}**" for n in unknown)
        lines.append(
            f"> ⚠️ **FRESHNESS UNVERIFIED** — the measurement timestamp for {names} is missing or "
            f"malformed, so this page cannot self-certify how current these numbers are. Treat "
            f"them as potentially stale."
        )
    return "\n>\n".join(lines)


def _parse_as_of(value):
    """Parse a committed `_meta.last_verified` (`YYYY-MM-DD`) into a tz-aware UTC datetime at
    00:00Z, or None if missing / not a string / not that exact shape / not a real calendar date.

    None flows through to `render_stale_banner`'s fail-loud UNVERIFIED path — an absent or
    malformed anchor is a downgrade that must surface, never a silent wall-clock fallback.
    """
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None
    return dt.replace(tzinfo=datetime.timezone.utc)


def resolve_default_as_of():
    """The page's committed freshness anchor: `upstream_links.json` `_meta.last_verified`,
    advanced by `scripts/verify-upstream-freshness.py --update-stamp`. Returns a tz-aware UTC
    datetime or None (→ fail-loud UNVERIFIED banner). Deterministic: reads only committed data.
    """
    return _parse_as_of(_UPSTREAM_META.get("last_verified") if isinstance(_UPSTREAM_META, dict) else None)


# WS3 (epic #6669) — commit-distance-behind-upstream-head staleness.
# A page can be CALENDAR-fresh (re-fired today, so render_stale_banner stays silent) yet built
# on a fork base many commits behind upstream HEAD — the measured numbers then don't reflect the
# current upstream code even though the timestamp looks current. Calendar age and commit distance
# are ORTHOGONAL freshness axes; this banner covers the second one.
#
# Commit distance requires a live compare against upstream, which a PURE renderer must never do
# (it would break the byte-pinned golden + reach the network at render time). So the split mirrors
# render_stale_banner exactly: scripts/verify-upstream-freshness.py --update-stamp computes the
# distance LIVE and stamps it into `upstream_links.json` `_meta.commit_distance[product]`; this
# renderer only READS that committed stamp. Same trust-surface idiom as the stale banner
# (AGENTS.md "Transition guards on trust surfaces"): the downgrade direction (behind / can't-
# certify) fails LOUD; the upgrade direction (within threshold) stays silent.
COMMIT_DISTANCE_THRESHOLD = 25


def resolve_fork_upstreams():
    """The committed product→upstream config (`_meta.fork_upstreams`), or {} if absent.

    A product listed here is one whose commit distance to upstream HEAD the page PROMISES to
    certify — so a listed product with no valid `_meta.commit_distance` stamp fails loud below
    (we claimed to track it but haven't). A product NOT listed makes no distance claim and stays
    silent. Deterministic: reads only committed data.
    """
    return _UPSTREAM_META.get("fork_upstreams", {}) if isinstance(_UPSTREAM_META, dict) else {}


def resolve_commit_distance():
    """The committed per-product distance stamps (`_meta.commit_distance`), or {} if absent.

    Written by `scripts/verify-upstream-freshness.py --update-stamp` from a live upstream compare;
    read here purely. Deterministic: reads only committed data.
    """
    return _UPSTREAM_META.get("commit_distance", {}) if isinstance(_UPSTREAM_META, dict) else {}


def render_commit_distance_banner(published, distance_stamp, fork_upstreams,
                                  threshold=COMMIT_DISTANCE_THRESHOLD):
    """Return a top-of-page markdown banner when a published product's fork base is too far behind
    (or its distance can't be certified against) upstream HEAD, or "" when every TRACKED product is
    within threshold.

    `published` is an iterable of (product_name, results_dict) for the products on the page.
    `distance_stamp` is `_meta.commit_distance` (product → {base_sha, commits_behind, upstream_repo,
    upstream_branch, checked_at}). `fork_upstreams` is `_meta.fork_upstreams` (product → {repo,
    branch, results}) — the set of products whose distance the page promises to certify.

    PURE (no wall clock, no network) so build_readme stays a deterministic function of committed
    data and the golden test stays reproducible.

    Per the trust-surface idiom, downgrade fails LOUD:

      * within-threshold — a valid stamp whose `base_sha` matches the product's CURRENT
                           `fork_base_upstream_sha` and whose `commits_behind` <= threshold
                           → silent (return "" if that holds for every tracked product).
      * behind           — a valid, base-matched stamp with `commits_behind` > threshold → loud
                           "STALE VS UPSTREAM" banner naming the product + distance.
      * unverified       — a tracked product with no stamp, a malformed stamp, or a stamp whose
                           `base_sha` no longer matches the product's current fork base (the fork
                           was re-based since the distance was last measured, so the stamp is for a
                           DIFFERENT base and can't certify the live one) → loud "UPSTREAM DISTANCE
                           UNVERIFIED" banner (guard-then-fill: we cannot certify, so we say so).

    A product only reaches this function if it is BOTH published AND in `fork_upstreams`; an
    untracked published product is silent (no claim made).
    """
    if not isinstance(fork_upstreams, dict) or not fork_upstreams:
        return ""
    if not isinstance(distance_stamp, dict):
        distance_stamp = {}
    behind = []
    unverified = []
    for name, results in published:
        if name not in fork_upstreams:
            continue  # untracked product — the page makes no distance claim about it
        current_base = None
        if isinstance(results, dict):
            prov = results.get("provenance")
            if isinstance(prov, dict):
                current_base = prov.get("fork_base_upstream_sha")
        stamp = distance_stamp.get(name)
        n = stamp.get("commits_behind") if isinstance(stamp, dict) else None
        stamped_base = stamp.get("base_sha") if isinstance(stamp, dict) else None
        repo = stamp.get("upstream_repo") if isinstance(stamp, dict) else None
        branch = stamp.get("upstream_branch") if isinstance(stamp, dict) else None
        checked_at = stamp.get("checked_at") if isinstance(stamp, dict) else None
        # A usable stamp needs an int commits_behind (bool is not int here), a base_sha, and repo/
        # branch context; anything short of that is unverifiable.
        stamp_ok = (
            isinstance(n, int) and not isinstance(n, bool) and n >= 0
            and isinstance(stamped_base, str) and stamped_base
            and isinstance(repo, str) and isinstance(branch, str)
            and isinstance(checked_at, str) and checked_at
        )
        if not stamp_ok:
            unverified.append((name, "no valid distance stamp is committed"))
            continue
        if not (isinstance(current_base, str) and current_base):
            unverified.append((name, "the product's current fork base is missing"))
            continue
        if stamped_base != current_base:
            unverified.append((name, "the distance stamp was measured against a different fork base"))
            continue
        if n > threshold:
            behind.append((name, n, repo, branch, checked_at))
    if not behind and not unverified:
        return ""
    lines = []
    if behind:
        detail = ", ".join(
            f"**{name}** ({n} commits behind `{repo}`@`{branch}` as of {checked_at})"
            for name, n, repo, branch, checked_at in behind
        )
        lines.append(
            f"> ⚠️ **STALE VS UPSTREAM** — the fork base these numbers were measured on is more "
            f"than {threshold} commits behind the current upstream HEAD: {detail}. The measured "
            f"numbers may not reflect recent upstream changes; re-base the fork and re-fire, then "
            f"re-run `scripts/verify-upstream-freshness.py --update-stamp` to refresh the distance."
        )
    if unverified:
        detail = ", ".join(f"**{name}** ({why})" for name, why in unverified)
        lines.append(
            f"> ⚠️ **UPSTREAM DISTANCE UNVERIFIED** — this page cannot certify how far behind "
            f"upstream HEAD these numbers' fork base is: {detail}. Treat them as potentially "
            f"behind upstream; run `scripts/verify-upstream-freshness.py --update-stamp` to "
            f"(re)establish the distance."
        )
    return "\n>\n".join(lines)


def _clean_history(rows):
    """Closed-schema-validate history rows, drop any that fail, sort by generated_at.

    Same discipline as the per-product render: a row renders ONLY HISTORY_FIELDS keys, each
    passing its predicate; a row missing a field or failing a predicate is dropped entirely
    (a malformed history file degrades to fewer trend rows, never to a leak). A row whose only
    "failure" is a missing `outcome` (pre-#547 legacy row) is back-filled first (#548) so it
    survives instead of being silently and permanently dropped.
    """
    clean = []
    if not isinstance(rows, list):
        return clean
    for r in rows:
        if not isinstance(r, dict):
            continue
        r = backfill_legacy_history_row(r)
        ok_all = True
        out = {}
        for key, ok in HISTORY_FIELDS.items():
            if key not in r:
                ok_all = False
                break
            try:
                if not ok(r[key]):
                    ok_all = False
                    break
            except (TypeError, ValueError):
                ok_all = False
                break
            out[key] = r[key]
        if ok_all:
            clean.append(out)
    clean.sort(key=lambda r: r["generated_at"])
    return clean


def _clean_warmpool_separation_history(rows):
    """Closed-schema-validate warmpool-separation history rows, drop any that fail, sort by generated_at.

    Sibling of _clean_history for the SEPARATE append-only warmpool-separation-history.jsonl
    store (schema.WARMPOOL_SEPARATION_HISTORY_FIELDS, #6890 item 3) -- same discipline: a row
    renders ONLY the closed-schema keys, each passing its predicate; a row missing a field or
    failing a predicate is dropped entirely (a malformed history file degrades to fewer
    disclosed measurements, never to a leak). Unlike _clean_history there is no legacy-backfill
    step -- this store is new as of #6890, so every row it will ever contain already carries the
    full schema.
    """
    clean = []
    if not isinstance(rows, list):
        return clean
    for r in rows:
        if not isinstance(r, dict):
            continue
        ok_all = True
        out = {}
        for key, ok in WARMPOOL_SEPARATION_HISTORY_FIELDS.items():
            if key not in r:
                ok_all = False
                break
            try:
                if not ok(r[key]):
                    ok_all = False
                    break
            except (TypeError, ValueError):
                ok_all = False
                break
            out[key] = r[key]
        if ok_all:
            clean.append(out)
    clean.sort(key=lambda r: r["generated_at"])
    return clean


def _latest_measured_count(latest_results):
    """Return (count, digest_or_empty, date) for the latest fire's burst_create COUNT, or None.

    Mirrors render.accrue_history._burst_count_row: the trend's headline COUNT is
    `sla_metrics.sandboxes_ready_under_1s` on ANY burst_create cell that measured one — NOT
    gated on outcome == "PASS" (#546). The scenario's own contract surfaces the COUNT on both
    PASS and FAIL; only a genuine all-cold burst (count==0) emits an empty sla_metrics, which
    is the true CASE-1 benign "nothing to reconcile" case. `digest_or_empty` is the provenance
    controller_digest AS PUBLISHED — the emitter drops the key when empty, so an un-anchorable
    fire yields "" here; `date` is generated_at[:10].
    """
    if not isinstance(latest_results, dict):
        return None
    count = None
    for s in latest_results.get("scenarios", []) or []:
        if not isinstance(s, dict) or s.get("name") != "burst_create":
            continue
        m = s.get("sla_metrics")
        if not isinstance(m, dict) or not m:
            return None
        c = m.get("sandboxes_ready_under_1s")
        if isinstance(c, bool) or not isinstance(c, (int, float)):
            return None
        count = c
        break
    if count is None:
        return None
    prov = latest_results.get("provenance")
    prov = prov if isinstance(prov, dict) else {}
    digest = prov.get("controller_digest") or ""
    date = latest_results.get("generated_at")
    date = date[:10] if isinstance(date, str) else ""
    return count, digest, date


def render_trend(history_rows, latest_results=None):
    """Render the build-over-build THROUGHPUT-COUNT trend table (#3918), or "" if empty.

    One row per distinct controller build (the accrual store is upsert-by-digest), oldest →
    newest. The headline COUNT (sandboxes ready <1s in one 1.0s burst against one warm pool)
    carries a delta-vs-prior-build column — the build-over-build trajectory alex's #1 directive
    asks for, which a single latest.json snapshot cannot show. First build is the baseline
    (delta "—"); every later build shows the signed change in COUNT vs the build before it.

    Every row also carries its **Outcome** (PASS/FAIL, #546): the COUNT is charted whenever it
    was measured, regardless of whether the burst cleared the delivery-ratio SLA, so a FAIL row
    is a real, honest count — but without an explicit outcome cell a FAIL build reads as if it
    had cleared the SLA bar just because it appears in the table. The column makes that legible
    without hiding the count itself.

    `latest_results` (the newest sandbox latest.json) is the trend-vs-latest divergence guard:
    the table is sourced from history.jsonl alone, so a fresh fire that MEASURED the headline
    COUNT but could not anchor to a build (empty controller_digest, dropped by the emitter) or
    whose build is not yet accrued would otherwise leave this table showing its last frozen row
    with no on-page signal that the newest measured COUNT diverged. The accrual's CASE-2 loud-fail
    (render.accrue_history) protects the FIRE pipeline (rc=3); it does NOT protect this committed
    render — the auto-refresh data PR commits a fresh latest.json beside a stale history. So when
    the latest fire measured a COUNT whose build is not the newest row here, disclose it rather
    than render stale-as-fresh (guard-then-fill; AGENTS.md "Transition guards on trust surfaces").
    None ⇒ no guard (byte-identical to the pre-guard render for existing callers/tests).
    """
    rows = _clean_history(history_rows)
    if not rows:
        return ""
    lines = [
        "## Throughput — build-over-build",
        "",
        "The headline COUNT — sandboxes ready in <1s in a single 1.0s burst against one warm",
        "pool — tracked across distinct controller builds (oldest first). **Δ** is the change in",
        "COUNT vs the prior build; the first build is the baseline. Drive this COUNT up.",
        "",
    ]
    header = [
        "Build (controller digest)",
        "Date",
        "Sandboxes ready <1s",
        "Δ",
        "Density /vCPU (this build)",
        "n",
        "Outcome",
    ]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")
    prev = None
    prev_n = None
    any_low_n_delta = False
    any_fail = False
    for r in rows:
        count = r["sandboxes_ready_under_1s"]
        n = r["n"]
        if prev is None:
            delta = "—"
        else:
            d = count - prev
            delta = f"+{d:g}" if d >= 0 else f"{d:g}"
            # Comparability guard: a Δ is a cross-build RANKING of the COUNT, so it is only a
            # trustworthy build-over-build signal when BOTH builds cleared the same
            # N>=TTFE_COMPARABILITY_MIN_N sample floor the matrix uses for cross-sample TTFE
            # ranking. Below it the swing can be sampling noise rather than a real move — mark
            # the Δ (never silently rank), mirroring the matrix's both-rows-clear comparison
            # gate. The COUNT itself stays unmarked: it is an honest raw measurement of its own
            # build; the guard belongs on the explicit comparison artifact, not the datum.
            if n < TTFE_COMPARABILITY_MIN_N or prev_n < TTFE_COMPARABILITY_MIN_N:
                delta += f" {_LOW_N_MARK}"
                any_low_n_delta = True
        prev = count
        prev_n = n
        outcome = r["outcome"]
        if outcome == "FAIL":
            any_fail = True
        cells = [
            f"`{r['controller_digest'][:19]}…`",
            r["generated_at"][:10],
            f"{count:g}",
            delta,
            f"{r['density_per_vcpu']:g}",
            f"{r['n']:g}",
            outcome,
        ]
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")
    if any_low_n_delta:
        lines.append(
            f"_{_LOW_N_MARK} Δ spans a build whose burst sampled fewer than "
            f"N={TTFE_COMPARABILITY_MIN_N} claims — too few to rank build-over-build; the swing "
            f"may be sampling noise, not a real move._"
        )
        lines.append("")
    if any_fail:
        lines.append(
            "_A FAIL Outcome means that build's burst did not clear the delivery-ratio SLA — "
            "the COUNT is still the real, measured number, not fabricated or estimated._"
        )
        lines.append("")
    lines.append(
        "_Density /vCPU (this build) is this single burst's own per-vCPU figure — a different, "
        "typically much smaller measurement than the Max Density table above (peak across "
        "scenarios), not a build-over-build regression._"
    )
    lines.append("")
    # Trend-vs-latest divergence guard (see docstring): when the newest fire measured the
    # headline COUNT but its build is not the newest row above, the trend has silently frozen —
    # say so on the page instead of rendering the last row as if it were current.
    latest = _latest_measured_count(latest_results)
    if latest is not None:
        count, digest, date = latest
        newest_digest = rows[-1]["controller_digest"]
        anchored = isinstance(digest, str) and bool(_SHA256.match(digest))
        if not (anchored and digest == newest_digest):
            reason = (
                "its build is not yet accrued into the trend"
                if anchored
                else "its provenance carries no `controller_digest`, so it cannot be anchored to "
                "a build"
            )
            lines.append(
                f"_⚠️ The most recent fire ({date}) measured a headline COUNT of {count:g} but is "
                f"not reflected above — {reason}. The trend is not advanced past "
                f"{rows[-1]['generated_at'][:10]}; fix the fire's provenance capture and this "
                f"caveat clears on the next accrual._"
            )
            lines.append("")
    return "\n".join(lines)


_TREND_BAR_WIDTH = 20


def render_throughput_trend_chart(history_rows):
    """Render the build-over-build throughput-COUNT trend as a Unicode block-bar chart (WS2
    follow-up, epic #6669), or "" when INERT (no rows).

    Visual companion to render_trend's table: same source (_clean_history), so the chart can
    never show a build the table itself doesn't. One bar per distinct controller build, oldest
    to newest (matching the table's own order), letting a reader see the build-over-build
    trajectory at a glance instead of scanning a Δ column row by row.

    A FAIL-outcome build still charts its real, measured COUNT (#546 discipline, same as the
    table) — the bar is annotated with the outcome only when it is FAIL, so a clean run's label
    stays uncluttered.

    Plain code-block Unicode bars, not mermaid xychart-beta (same GitHub-support rationale as
    render_density_bars/render_ttfe_bars/render_concurrent_burst_chart).
    """
    rows = _clean_history(history_rows)
    if not rows:
        return ""
    max_count = max(r["sandboxes_ready_under_1s"] for r in rows)
    if max_count <= 0:
        return ""
    label_width = max(len(r["generated_at"][:10]) for r in rows)
    lines = ["```", "Throughput — build-over-build (sandboxes ready <1s)", ""]
    for r in rows:
        count = r["sandboxes_ready_under_1s"]
        label = r["generated_at"][:10].ljust(label_width)
        bar = "█" * max(1, round(count / max_count * _TREND_BAR_WIDTH))
        suffix = " (FAIL)" if r["outcome"] == "FAIL" else ""
        lines.append(f"{label} {bar} {_fmt_num(count)}{suffix}")
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


# --- Goal 2.1: Core Benchmark Matrix render -----------------------------------------------
# The customer-facing page is the doc's exact 9-column "Agent Sandbox Core Metrics Table":
# rows are (runtime × activation-mode), columns are throughput@TTFE-threshold / TTFE p50,p95 /
# samples / density / exec-success. The honesty spine is TTFE (the sandbox executed its first
# instruction and returned a result) — NOT pod-Ready. A cell we have not yet measured renders
# `pending`; a sub-1s throughput the p95 misses renders the harness-emitted honest `0`.

_PENDING = "pending"

# Small-sample marker for TTFE cells below TTFE_COMPARABILITY_MIN_N. A dagger, NOT the ⚠️ used
# for the exec-success honesty check — overloading ⚠️ would conflate "this run had failures"
# with "this row's N is too small to rank against another row".
_LOW_N_MARK = "†"

# a4z1 goal-2.1 display-vs-spec audit gap 1: flags a MEASURED per-node throughput rate that
# renders alongside a row TTFE p95 sitting OVER that column's bar (e.g. a Kata warm cell's
# `1 /node` next to a 2.2426s p95 under the 1s bar). Not the same condition as the derived-`0`
# rule a few lines below in thpt_dual_cell() — that rule fires when NO throughput fire ran and
# a `0` is derived FROM the over-bar p95; this marker fires when a throughput fire DID run and
# its real rate is printed as-is. The two are different measured quantities (a threshold-
# crossing RATE over the sample window vs. a latency PERCENTILE), so they can disagree without
# either being wrong — but shown side-by-side with no disclosure they read as inconsistent.
# Deliberately NOT reusing _STARSTAR_TAGS: that system is basis-driven (hb#230) and true_ttfe
# is deliberately excluded from it by doctrine — this is an independent, additive disclosure,
# not a reversal of that doctrine.
_RATE_OVER_P95_MARK = "‡"

# N/A-by-construction cell (distinct from `pending`, which awaits a measurement). Used for
# the resume-from-suspend × Kata+microVM cell: CRIU checkpoint/restore does not transfer to
# the Kata VM model, so that cell can never be measured — na-by-design, not not-yet-measured.
_NA = "N/A"

# hb#132 dual-throughput. Each throughput cell carries TWO numbers: `<node> /node · <cluster>`.
# The per-node half is the engineering rate (comparable across runtimes); the cluster half is a
# MEASURED per-activation-mode cluster rate at X nodes — never a per-node × N extrapolation (that
# fiction breaks above the controller reconcile ceiling). The cluster half pends
# `pending (cluster-fire)` until a schema-validated PER-MODE cluster-throughput fire carries
# thpt_*_per_cluster in that mode's scenario sla_metrics. The standalone whole-cluster Saturation
# ceiling (top-level cluster_saturation, rendered in DETAILS) is a DIFFERENT quantity — completion
# throughput at overload, not the SLO-gated sustained rate these cells are defined to hold — so it
# never fills a matrix cell. A landed cluster figure below the sizing target renders with ⚠️
# (honest under-target signal); the target itself is the test-sizing floor and is NEVER printed
# as a value.
_CLUSTER_FIRE = "cluster-fire"
CLUSTER_THROUGHPUT_TARGET = 300


def _fmt_num(v):
    """Compact numeric (no trailing zeros): 4.0 -> 4, 1.86 -> 1.86."""
    return f"{v:g}"


def _fmt_ratio(r):
    """A retention ratio to 2 dp, no trailing zeros: 0.989474 -> 0.99, 1.06 -> 1.06."""
    return f"{round(r, 2):g}"


def _fmt_pct(r):
    """A [0,1] rate as a percentage, no trailing zeros: 1.0 -> 100%, 0.97 -> 97%, 0.965 -> 96.5%."""
    return f"{round(r * 100, 1):g}%"


def _fmt_bytes(n):
    """Compact IEC bytes: 512 -> 512 B, 4096 -> 4 KiB, 1572864 -> 1.5 MiB."""
    step = 1024.0
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    for unit in units:
        if abs(n) < step or unit == units[-1]:
            return f"{n:g} {unit}" if unit == "B" else f"{round(n, 2):g} {unit}"
        n /= step


def _fmt_secs(ms):
    """Milliseconds -> the doc's seconds format: 600 -> 0.6s, 1560 -> 1.56s."""
    return f"{ms / 1000.0:g}s"


def _fmt_wait(ms):
    """Operating-envelope budgeting figure: a friendly 1-dp APPROXIMATION with a `~` prefix
    (631.7 -> ~0.6s, 2939.65 -> ~2.9s). Deliberately coarser than `_fmt_secs` — the envelope is
    a plan-around-this summary for non-experts, and the exact measured value lives in the detail
    table each row is sourced from. Still render-derived; only the display precision differs."""
    return f"~{round(ms / 1000.0, 1):g}s"


def _fmt_usd(v):
    """A USD cost figure, 2dp, no trailing zeros: 0.42 -> $0.42, 1.5 -> $1.5, 12 -> $12."""
    return f"${round(v, 2):g}"


def _exec_cell(rate, n_total, n_succ=None):
    """Doc's exec-success ("Honesty Check") cell.

    100% renders plain; <100% shows the succeeded/total fraction + a ⚠️ flag (the doc's
    "92.8% (1277/1376) ⚠️"). The numerator is exec_success_n when the harness emits it,
    else derived as round(rate * N) so the fraction always reconciles to the Samples column.
    """
    cell = f"{round(rate * 100, 1):g}%"
    if rate < 1.0:
        if n_succ is None and n_total:
            n_succ = round(rate * n_total)
        if n_succ is not None and n_total:
            cell += f" ({n_succ}/{n_total})"
        cell += " ⚠️"
    return cell


def _clean_matrix_metrics(metrics):
    """Keep only MATRIX_METRIC_FIELDS keys whose values pass their predicate (closed schema).

    hb#174 fail-closed basis gate: a `thpt_slo_basis` that was PRESENT in the input but
    failed its predicate (unknown / outside SLO_BASIS_VALUES) drops the whole cluster
    triple + the n stamp — an SLO rate must never render with an undisclosed measured
    basis (the enum is genuinely closed, per the sign-off). An ABSENT basis keeps the
    triple: legacy / direct saturation-fire triples predate the stamp and are legitimate.
    """
    out = {}
    if not isinstance(metrics, dict):
        return out
    for key, ok in MATRIX_METRIC_FIELDS.items():
        if key in metrics:
            try:
                if ok(metrics[key]):
                    out[key] = metrics[key]
            except (TypeError, ValueError):
                pass
    if "thpt_slo_basis" in metrics and "thpt_slo_basis" not in out:
        for key in (
            "thpt_under_5s_per_cluster",
            "thpt_under_1s_per_cluster",
            "thpt_cluster_node_count",
            "thpt_slo_n_exec_ok",
        ):
            out.pop(key, None)
    # hb#230 Gap B: the SAME fail-closed posture, per bar. A per-bar basis PRESENT in
    # input but dropped (outside SLO_BASIS_VALUES) drops THAT bar's cluster figure — a
    # bar's SLO rate must never render with an undisclosed/invalid basis. Only the one
    # bar's cluster figure is dropped (the shared node-count / n stamp may still caveat a
    # valid sibling bar). The coercer already RAISES on a non-enum per-bar basis at emit,
    # so this is defense-in-depth mirroring the whole-triple gate above.
    for bar_key, cluster_key in (
        ("thpt_slo_basis_5s", "thpt_under_5s_per_cluster"),
        ("thpt_slo_basis_1s", "thpt_under_1s_per_cluster"),
    ):
        if bar_key in metrics and bar_key not in out:
            out.pop(cluster_key, None)
    return out


def _matrix_scenarios(scenarios):
    """Map scenario internal-NAME -> {outcome, n, metrics} for matrix-row lookup.

    Keyed by the harness name (not the display label) because matrix rows are addressed by
    activation-mode scenario id. sla_metrics are closed-schema-cleaned to MATRIX_METRIC_FIELDS;
    unknown metric keys are dropped before they can reach the public page.
    """
    out = {}
    if not isinstance(scenarios, list):
        return out
    for s in scenarios:
        if not isinstance(s, dict):
            continue
        name = s.get("name")
        if not isinstance(name, str):
            continue
        n = s.get("n")
        n = n if isinstance(n, int) and not isinstance(n, bool) and n >= 0 else 0
        outcome = s.get("outcome")
        metrics = _clean_matrix_metrics(s.get("sla_metrics"))
        # Carry the pending_reason through (closed-enum-guarded, same discipline as the
        # non-matrix _clean_scenario at 106-107) so a pending matrix cell can say WHY it is
        # pending. This is the honesty distinction: a gVisor resume cell whose run DID land
        # but is held by an upstream controller bug is `upstream-blocked` — it must NOT read
        # like a not-yet-run `not-yet-measured` pending. A free-text / unknown reason is
        # dropped (renders bare `pending`), never leaked to the public page.
        reason = s.get("pending_reason")
        if reason is not None and reason not in PENDING_REASONS:
            reason = None
        # A `pending` scenario has no publishable measurement: its sla_metrics are
        # provisional gap-probe artifacts, not results. The upstream-blocked resume probe
        # is the canonical case — it records the probe's timeout CEILING (the wall-clock it
        # waits out a never-clearing Suspended condition), not a real resume TTFE. Suppress
        # those metrics so a pending matrix cell renders `pending (<reason>)` across EVERY
        # metric column instead of leaking a number a reader would rank against a real
        # distribution. A PASS scenario keeps its metrics (absent individual keys still
        # fall through to per-cell `pending`), so a resume row graduates cleanly
        # pending -> real the moment its outcome flips to PASS.
        if outcome == "pending":
            metrics = {}
        # hb#230 Fork 5: the resume probe ceiling is carried TOP-LEVEL (not in sla_metrics)
        # precisely so it survives the pending-cell metric suppression above — a pending
        # resume row still publishes its recorded ceiling. Positive finite number only; any
        # other shape drops to None and the row falls back to the normal pending render.
        ceiling = s.get("resume_probe_ceiling_ms")
        if not (
            isinstance(ceiling, (int, float))
            and not isinstance(ceiling, bool)
            and ceiling == ceiling
            and ceiling not in (float("inf"), float("-inf"))
            and ceiling > 0
        ):
            ceiling = None
        out[name] = {
            "outcome": outcome,
            "n": n,
            "metrics": metrics,
            "pending_reason": reason,
            "resume_probe_ceiling_ms": ceiling,
        }
    return out


# hb#550: the two literal FAIL-disclosure idioms live independently in render_matrix
# (row_mode_label's "⚠️ FAIL" tag) and render_what_this_means ("did NOT clear its SLA"
# prose) — no shared substring exists across all three FAIL-disclosure call sites, so
# check_render_downgrade scans for either marker rather than assuming one canonical string.
_FAIL_DISCLOSURE_MARKERS = ("⚠️ FAIL", "did NOT clear its SLA")


def check_render_downgrade(scenarios, rendered_text):
    """Detect a render-time trust downgrade the AGENTS.md fail-closed doctrine forbids (hb#550).

    Two loss-directional legs, scoped to ACTIVATION_MODE_ROWS names only — the only
    scenarios that populate the Core Metrics matrix (and thus the only ones for which
    `_matrix_scenarios`'s MATRIX_METRIC_FIELDS allow-list emptying is unambiguously a
    regression rather than expected behavior; a non-activation scenario like
    `burst_create` legitimately carries metric keys outside MATRIX_METRIC_FIELDS and
    would false-positive if checked the same way):

    1. FAIL disclosure loss — a measured FAIL scenario with a real TTFE figure
       (mirrors render_matrix's own hb#4420 scope-gate: `ttfe_p95_ms` or `ttfe_p50_ms`
       present) whose disclosure marker is nowhere in the rendered text;
    2. silent pending-render — a measured, non-`pending`-outcome scenario whose
       `_matrix_scenarios`-cleaned metrics come back entirely empty, which would render
       every cell for that row as bare `pending`, indistinguishable from never-measured.

    `scenarios` is the raw `results["scenarios"]` list (one product/runtime's worth);
    `rendered_text` is the full rendered page text to scan for FAIL-disclosure prose.

    Returns human-readable finding strings; empty means clean. The caller decides the
    posture (generate.main() fails closed unless BENCH_ALLOW_RENDER_DOWNGRADE is set).
    """
    findings = []
    if not isinstance(scenarios, list):
        return findings
    text = rendered_text if isinstance(rendered_text, str) else ""
    activation_names = {name for name, _label in ACTIVATION_MODE_ROWS}
    mapped = _matrix_scenarios(scenarios)
    for s in scenarios:
        if not isinstance(s, dict):
            continue
        name = s.get("name")
        if not isinstance(name, str) or name not in activation_names:
            continue
        outcome = s.get("outcome")
        raw_metrics = s.get("sla_metrics")
        has_measured = isinstance(raw_metrics, dict) and len(raw_metrics) > 0
        if not has_measured or outcome == "pending":
            continue

        entry = mapped.get(name)
        entry_metrics = entry.get("metrics") if isinstance(entry, dict) else None
        if not entry_metrics:
            findings.append(
                f"{name}: outcome={outcome!r} carries measured sla_metrics but "
                f"_matrix_scenarios cleared them entirely — every cell for this row "
                f"would render as bare `pending`"
            )
            continue

        if outcome == "FAIL" and (
            "ttfe_p95_ms" in entry_metrics or "ttfe_p50_ms" in entry_metrics
        ):
            if not any(marker in text for marker in _FAIL_DISCLOSURE_MARKERS):
                findings.append(
                    f"{name}: measured FAIL with a real TTFE metric but no FAIL "
                    f"disclosure marker found anywhere in the rendered text"
                )
    return findings


def _runtime_density(scen_by_name):
    """Per-runtime Max-Density /vCPU: first of DENSITY_SOURCE_SCENARIOS carrying it, else any
    activation-mode scenario that emitted its own density_per_vcpu. None ⇒ render pending."""
    for name in DENSITY_SOURCE_SCENARIOS:
        sc = scen_by_name.get(name)
        if sc and "density_per_vcpu" in sc["metrics"]:
            return sc["metrics"]["density_per_vcpu"]
    for name, _label in ACTIVATION_MODE_ROWS:
        sc = scen_by_name.get(name)
        if sc and "density_per_vcpu" in sc["metrics"]:
            return sc["metrics"]["density_per_vcpu"]
    return None


def render_density_detail(results, kata_results=None):
    """DETAILS.md deep-dive: per-runtime Max-Density (sandboxes per node-allocatable
    sandbox-schedulable vCPU). Relocated off the headline matrix (hb#134 page-friendliness
    pass — a non-infra reader does not need it in the core table) but PRESERVED here so the
    #133/#135 saturation measurement is not lost. Same per-runtime source logic as the
    matrix: the primary results claim their measured runtime; kata_results (the sandbox-kata
    product) may fill the kata-microvm slot. Unmeasured runtimes render `pending`. Returns ""
    (INERT) only for an unknown product — otherwise it always renders the runtime skeleton,
    rows pending individually, mirroring the matrix's honest-skeleton behaviour."""
    product = results.get("product")
    if product not in PRODUCTS:
        return ""
    prov = _clean_provenance(results.get("provenance"))
    measured_runtime = prov.get("runtime") or "gvisor"
    sources = {measured_runtime: _matrix_scenarios(results.get("scenarios"))}
    if (
        isinstance(kata_results, dict)
        and kata_results.get("product") == "sandbox-kata"
        and "kata-microvm" not in sources
    ):
        kp = _clean_provenance(kata_results.get("provenance"))
        if kp.get("runtime") == "kata-microvm":
            sources["kata-microvm"] = _matrix_scenarios(kata_results.get("scenarios"))
    lines = ["## Max Density (sandboxes per vCPU)", ""]
    lines.append(
        "Max Density is sandboxes per node-allocatable sandbox-schedulable vCPU (the "
        "per-node denominator), not per total-cluster vCPU. This is the absolute per-vCPU "
        "figure — distinct from the linearity check's per-node density-retention series "
        "(a ratio across node counts), which uses a different denominator. An unmeasured "
        "runtime renders `pending`."
    )
    lines.append("")
    lines.append("| Runtime | Max Density (sb/vCPU) |")
    lines.append("|---|---|")
    for rt in MATRIX_RUNTIMES:
        rt_scen = sources.get(rt)
        density = _runtime_density(rt_scen) if rt_scen is not None else None
        cell = _fmt_num(density) if density is not None else link_pending(_PENDING)
        lines.append(f"| {RUNTIME_LABELS[rt]} | {cell} |")
    lines.append("")
    return "\n".join(lines) + "\n"


_DENSITY_BAR_WIDTH = 20


def render_density_bars(results, kata_results=None):
    """Render the per-runtime Max-Density table above as a Unicode block-bar chart, or "" when
    INERT (no runtime has a measured density yet — a bar chart with zero data points is not a
    visual, it is noise).

    Visual companion to render_density_detail (WS2, epic #6669) — same source resolution
    (primary results claim their measured runtime; kata_results may only fill an empty
    kata-microvm slot), so the bars can never show a runtime the table itself doesn't. Density
    (sandboxes per vCPU) is an absolute per-runtime figure, not a part-of-whole share, so a
    length-proportional bar chart is the right shape — not a `pie` (reserved for genuine
    part-of-whole splits, e.g. render_bind_exec_pie). Rendered as a plain code block of Unicode
    block characters rather than mermaid `xychart-beta`: GitHub's docs confirm native support
    for flow/sequence/pie charts but do not confirm xychart-beta, and an unrendered chart block
    on the public page is worse than no chart — a code-block bar needs no diagram-engine version
    at all.

    An unmeasured runtime is OMITTED from the chart (not drawn as a zero-length bar, which
    would misread as "measured at zero") — mirrors the table's `pending` cell semantics without
    implying a false zero measurement.
    """
    product = results.get("product")
    if product not in PRODUCTS:
        return ""
    prov = _clean_provenance(results.get("provenance"))
    measured_runtime = prov.get("runtime") or "gvisor"
    sources = {measured_runtime: _matrix_scenarios(results.get("scenarios"))}
    if (
        isinstance(kata_results, dict)
        and kata_results.get("product") == "sandbox-kata"
        and "kata-microvm" not in sources
    ):
        kp = _clean_provenance(kata_results.get("provenance"))
        if kp.get("runtime") == "kata-microvm":
            sources["kata-microvm"] = _matrix_scenarios(kata_results.get("scenarios"))
    rows = []
    for rt in MATRIX_RUNTIMES:
        rt_scen = sources.get(rt)
        density = _runtime_density(rt_scen) if rt_scen is not None else None
        if isinstance(density, (int, float)) and not isinstance(density, bool) and density > 0:
            rows.append((RUNTIME_LABELS[rt], density))
    if not rows:
        return ""
    label_width = max(len(label) for label, _ in rows)
    max_density = max(density for _, density in rows)
    lines = ["```", "Max Density (sandboxes per vCPU)", ""]
    for label, density in rows:
        bar_len = max(1, round(density / max_density * _DENSITY_BAR_WIDTH))
        bar = "█" * bar_len
        lines.append(f"{label.ljust(label_width)}  {bar} {_fmt_num(density)}")
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


def _runtime_ttfe(scen_by_name):
    """Per-runtime warm-pool TTFE p50/p95: sourced from the SAME single canonical scenario as
    _runtime_density (warmpool_cold_start, via DENSITY_SOURCE_SCENARIOS) — the render's one
    high-N activation-mode row — rather than render_matrix's full per-row/per-mode extraction.
    Returns (p50_ms, p95_ms, n) only when the scenario carries BOTH percentiles; else None."""
    for name in DENSITY_SOURCE_SCENARIOS:
        sc = scen_by_name.get(name)
        if sc and "ttfe_p50_ms" in sc["metrics"] and "ttfe_p95_ms" in sc["metrics"]:
            return sc["metrics"]["ttfe_p50_ms"], sc["metrics"]["ttfe_p95_ms"], sc["n"]
    return None


_TTFE_BAR_WIDTH = 20


def render_ttfe_bars(results, kata_results=None):
    """Render warm-pool TTFE p50/p95 per runtime as a Unicode block-bar chart, or "" when INERT
    (no runtime's canonical scenario carries both percentiles yet).

    Visual companion to the Core Metrics matrix's warm-pool row (WS2, epic #6669). Deliberately
    sourced from the SAME single canonical scenario as render_density_bars/_runtime_density
    (warmpool_cold_start) rather than duplicating render_matrix's per-row/per-mode extraction
    (small-N marker, pending-reason, upstream refs) — that logic is scoped to a specific table
    cell and re-deriving a subset of it for a chart risks silently disagreeing with the table
    it's meant to illustrate. Same source-resolution rule as the other runtime-scoped visuals:
    the primary results claim their measured runtime; kata_results may only fill an empty
    kata-microvm slot. A runtime whose canonical scenario lacks either percentile is OMITTED
    (not drawn as a zero bar, which would misread as a real sub-zero measurement).

    A runtime whose canonical-scenario N is below TTFE_COMPARABILITY_MIN_N gets a `*` marker
    on its label plus a trailing footnote, mirroring the matrix's own low-N TTFE dagger — a
    single-sample p50 is not a distribution and should not be visually ranked against one.

    Plain code-block Unicode bars, not mermaid xychart-beta (same GitHub-support rationale as
    render_density_bars: docs confirm pie/flow/sequence, not xychart-beta).
    """
    product = results.get("product")
    if product not in PRODUCTS:
        return ""
    prov = _clean_provenance(results.get("provenance"))
    measured_runtime = prov.get("runtime") or "gvisor"
    sources = {measured_runtime: _matrix_scenarios(results.get("scenarios"))}
    if (
        isinstance(kata_results, dict)
        and kata_results.get("product") == "sandbox-kata"
        and "kata-microvm" not in sources
    ):
        kp = _clean_provenance(kata_results.get("provenance"))
        if kp.get("runtime") == "kata-microvm":
            sources["kata-microvm"] = _matrix_scenarios(kata_results.get("scenarios"))
    rows = []
    for rt in MATRIX_RUNTIMES:
        rt_scen = sources.get(rt)
        ttfe = _runtime_ttfe(rt_scen) if rt_scen is not None else None
        if ttfe is not None:
            p50, p95, n = ttfe
            rows.append((RUNTIME_LABELS[rt], p50, p95, n))
    if not rows:
        return ""
    any_low_n = any(n < TTFE_COMPARABILITY_MIN_N for _, _, _, n in rows)
    label_width = max(len(label) for label, _, _, _ in rows) + (1 if any_low_n else 0)
    max_val = max(max(p50, p95) for _, p50, p95, _ in rows)
    lines = ["```", "Warm-Pool TTFE (ms) — p50 vs p95", ""]
    for label, p50, p95, n in rows:
        star = "*" if n < TTFE_COMPARABILITY_MIN_N else ""
        row_label = (label + star).ljust(label_width)
        p50_bar = "█" * max(1, round(p50 / max_val * _TTFE_BAR_WIDTH))
        p95_bar = "█" * max(1, round(p95 / max_val * _TTFE_BAR_WIDTH))
        lines.append(f"{row_label} p50  {p50_bar} {_fmt_secs(p50)}")
        lines.append(f"{' ' * label_width} p95  {p95_bar} {_fmt_secs(p95)}")
    if any_low_n:
        lines.append("")
        lines.append(f"* fewer than {TTFE_COMPARABILITY_MIN_N} samples — not a stable distribution")
    lines.append("```")
    lines.append("")
    shape = []
    if prov.get("node_count") is not None:
        shape.append(f"node_count={prov['node_count']}")
    if prov.get("machine_type"):
        shape.append(f"`{prov['machine_type']}`")
    if shape:
        lines.append(
            f"_Cluster shape (gVisor leg): {', '.join(shape)} — the swing-flag threshold "
            "compares consecutive fires on this chart, so a node-count or machine-class change "
            "shows up here first._"
        )
        lines.append("")
    return "\n".join(lines)


def _landed_cluster_x(m):
    """A metrics dict's landed, valid thpt_cluster_node_count as int — None if absent/invalid.

    The single validity rule shared by the caption resolver AND the per-cell cluster-half gate,
    so "has an X" means the same thing in both places (numeric, non-bool, > 0)."""
    x = (m or {}).get("thpt_cluster_node_count")
    if isinstance(x, (int, float)) and not isinstance(x, bool) and x > 0:
        return int(x)
    return None


def _resolve_cluster_x(sources):
    """hb#132: the X in the `@X nodes` cluster-throughput caption — the node count the per-cluster
    figures were MEASURED at, resolved PER RUNTIME (first landed thpt_cluster_node_count within
    each runtime's scenarios). Returns {runtime: X}; empty ⇒ no cluster figure has landed yet
    (caption says cluster halves pend). Per-runtime because two runtimes' cluster fires may land
    at DIFFERENT X — a single first-match X would silently caption one runtime's figures with the
    other runtime's node count (the mixed-X ambiguity).

    hb#230 finding #2 (within-runtime mixed-X): the per-runtime resolution is still FIRST-LANDED —
    if a single runtime's scenarios landed cluster figures at two different node counts (e.g. gVisor
    warm at 10 nodes, cold at 9), the caption reports whichever scenario's X iterated first, not a
    per-scenario X. This is IMMATERIAL today because the only within-runtime divergence is the gVisor
    cold floor-zero (both per-cluster bars are a measured 0.0 — a caption node count cannot mislead a
    reader about a 0). If a within-runtime row ever lands a NON-zero cluster figure at an X different
    from the captioned one, this resolver must become per-scenario; until then first-landed is fine."""
    xs = {}
    for rt, rt_scen in sources.items():
        if not rt_scen:
            continue
        for sc in rt_scen.values():
            x = _landed_cluster_x((sc or {}).get("metrics"))
            if x is not None:
                xs[rt] = x
                break
    return xs


# hb#174: per-basis disclosure text for the cluster-throughput caption. Keyed by the
# thpt_slo_basis value (schema.SLO_BASIS_VALUES — a sync test asserts every non-default
# member has an entry here). The DEFAULT basis (true_ttfe — cluster rate read off the
# true-TTFE Pareto) is deliberately ABSENT: it is what the legend already describes, so it
# renders no extra line. The two literal bases are honest fallbacks with two disclosures
# each: (a) the latency gate is the literal exec-probe warm p95 — an UPPER bound on TTFE
# (every sample carries exec websocket-setup overhead), so a rung compliant at the bar is
# conservatively compliant; (b) the rate is measured over a candidate-specific window,
# named explicitly so the two candidates' non-identical denominators are never conflated.
_SLO_BASIS_NOTES = {
    "literal_ttfe_upper_bound+controller_completed": (
        "derived from the literal exec-probe warm p95 — an UPPER bound on TTFE (includes "
        "exec setup overhead), so compliance at the bar is conservative — gated against the "
        "controller completion rate (completion-count delta over inter-scrape wall time, "
        "which includes inter-step overhead and therefore under-reads the sustained rate: "
        "also conservative)"
    ),
    "literal_ttfe_upper_bound+acq_fulfilled": (
        "derived from the literal exec-probe warm p95 — an UPPER bound on TTFE (includes "
        "exec setup overhead), so compliance at the bar is conservative; this basis fills "
        "the <5s cell — it cannot itself certify the stricter <1s bar, so any <1s figure "
        "shown is credited under the acquire-side `***U` basis below, not this one — throughput "
        "is the acquisition rate: fulfilled (claim->bound)/s, steady-state, pending claims "
        "excluded; trust-gated per rung on agreement with the independent controller "
        "completion rate (divergent rungs are ineligible)"
    ),
    # hb#214 part 1 (DRAFT): the honest-ZERO basis. The polarity inversion matters — the
    # literal warm p95 is an UPPER bound, so for a NEGATIVE claim (a zero) mere
    # over-the-bar is NOT conservative; the predicate therefore requires the FLOOR rung's
    # observed latencies to clear the bar by a pre-declared margin, with unknowns granted
    # a pass (adversarial fill), before a 0.0 may be emitted. Fires only when both
    # positive bases failed to derive — a positive rate anywhere outranks the zero.
    "literal_ttfe_upper_bound+floor_zero_margin": (
        "a measured ZERO, not an absence: at the LOWEST offered rate fired, the majority "
        "of literal exec-probe warm samples exceeded the 5s bar by the pre-declared 1.5x "
        "margin even after granting every unevaluable sample a pass — so no compliant "
        "operating point exists at or above the floor rate; derived only after both "
        "positive literal bases failed, from a trusted steady-state rung (dual "
        "rate-candidate agreement, n>=20)"
    ),
    # hb#230 (alex doctrine flip, 2026-07-08): the Class A *** caveat prose. Consulted
    # only after the corroborated bases derive nothing; it publishes the best measured
    # acquire-side rate SINGLE-SOURCE (controller cross-check dropped), which is exactly
    # why it can read HIGHER than a corroborated cell (Fork 3 two-trust-tiers). The
    # upstream link (#940 -> fix #1087) lives in the consolidated *** footnote block, not
    # inline here (matching the URL-free style of the other notes).
    "acq_fulfilled+acq_p95_uncorroborated": (
        "the UNCORROBORATED acquire-side rate: fulfilled (claim->bound)/s at the highest "
        "rung whose acquisition p95 cleared the bar, with the independent controller-"
        "completion cross-check DROPPED — single-source, so it can read HIGHER than a "
        "cross-corroborated cell; controller corroboration is unavailable pending the "
        "upstream metric fix. The figure is the highest OFFERED rung, NOT a saturation "
        "ceiling — the ladder was not driven to saturation, so the true sustainable rate "
        "is at least this and likely higher"
    ),
    # hb#230 Fork 4 (alex doctrine flip, 2026-07-08): the cold-start honest-ZERO note.
    # A measured ZERO, not an absence — the controller cold-start floor exceeds BOTH
    # bars at every offered rate (rate-independent), so no operating point complies. The
    # floor rung may be controller-untrusted, so the predicate additionally requires a
    # controller-MEASURED (trusted) rung whose cold p50 is also over both bars — the
    # zero is corroborated, never asserted from an untrusted floor alone. The specific
    # cold-floor p50 + the clean acquire-side rate + the upstream link (#751 -> #761)
    # live in the consolidated *** footnote block, URL-free here (matching the others).
    "controller_cold_floor_zero_corroborated": (
        "a measured ZERO, not an absence: the controller cold-start floor exceeds BOTH "
        "bars at every offered rate (rate-independent), so no compliant operating point "
        "exists — the zero is the sandbox cold-start floor, not an acquire-path miss "
        "(the acquire-side latency is clean sub-second at every rung). Corroborated by a "
        "controller-MEASURED (trusted) rung whose cold p50 is also over both bars, so it "
        "is never asserted from the controller-untrusted floor rung alone"
    ),
    # hb#230 Kata-cold <5s ruling (2026-07-08): the unresolved-bounds note. A
    # measurement WAS taken, but the true value is bounded in a bracket that STRADDLES
    # the bar — the controller-cold proxy (lower bound) does not breach and the literal
    # exec-probe (upper bound) does not clear — so no claim is supportable either
    # direction. Rendered "unk.***", NOT "pending" (which would imply no measurement).
    # The specific bracket + the exec-probe-overhead explanation live in the
    # consolidated *** footnote block, URL-free here (matching the others).
    "unresolved_bounds_bar_bracketed": (
        "neither a compliant rate nor an honest zero: a measurement was taken, but the "
        "true TTFE p95 is bounded in a bracket that STRADDLES the bar — the lower-bound "
        "proxy does not breach the bar (so no honest-zero) and the upper-bound literal "
        "exec-probe does not clear it (so no positive rate), leaving the claim unresolved "
        "by construction. Distinct from a pending cell: the measurement exists, the bar is "
        "provably unresolvable at this operating point, not merely unmeasured"
    ),
}


# hb#230 (alex doctrine flip, 2026-07-08): the bases that carry a per-cell *** caveat.
# NOT every non-default basis — the cross-corroborated literal bases (acq~controller
# agreement) are the TRUSTED tier and render clean (Fork 3). The *** marks the
# single-source / bounded / cold bases whose figure is published-with-a-caveat:
#   - Class A: the uncorroborated acquire-side rate (controller cross-check dropped)
#   - Fork 4: the cold-start honest-ZERO (cold floor over both bars, acq clean)
#   - Kata-cold: the unresolved-bounds cell (bar inside the [lower, upper] bracket)
# Each basis maps to its OWN letter-suffixed tag (disambiguation, hb#404) rather than a
# shared bare "***" — a reader can tell which caveat applies from the tag alone, without
# cross-referencing by row/column position. All four (incl. the resume-ceiling "R" tag,
# a separate hardcoded path not driven by this map) point at the ONE consolidated
# footnote block below the matrix.
_STARSTAR_TAGS = {
    "acq_fulfilled+acq_p95_uncorroborated": "***U",
    "controller_cold_floor_zero_corroborated": "***Z",
    "unresolved_bounds_bar_bracketed": "***K",
}


# hb#230 ask (a) — certification-floor prefix. A per-cluster figure produced by one of
# these bases is a LOWER BOUND on the true sustainable rate, not the rate itself, so it
# renders `≥y /cluster` (not a bare `y`). Two floor constructions qualify:
#   - literal_ttfe_upper_bound+* : a TTFE UPPER bound yields a rate LOWER bound by
#     construction ("TTFE ≤ t ⇒ rate ≥ 1/t"). Both the controller-corroborated and the
#     acq-corroborated literal variants are floors.
#   - acq_fulfilled+acq_p95_uncorroborated : the uncorroborated acquire-side rate — the
#     highest rung whose acq p95 cleared the bar, controller cross-check dropped; the
#     consolidated *** footnote already states the true rate is "at least this and
#     likely higher". These are trust-gate-capped (upstream #940 disqualifies the higher
#     rungs), so the ladder did NOT saturate — a `≥` is the honest read.
# EXCLUDED (never get `≥`): the floor-ZERO bases (a `≥0` is meaningless / misleading),
# unresolved_bounds_bar_bracketed (renders `unk.`), and true_ttfe (a graduated real
# number, not a bound). The value>0 guard in thpt_dual_cell is belt-and-suspenders so a
# zero can never render `≥0` even if a floor basis were mis-stamped onto it.
_FLOOR_BASES = frozenset(
    {
        "literal_ttfe_upper_bound+controller_completed",
        "literal_ttfe_upper_bound+acq_fulfilled",
        "acq_fulfilled+acq_p95_uncorroborated",
    }
)


def _bar_basis(m, bar_s):
    """hb#230 Gap B: the basis governing ONE bar of a dual cell. The per-bar stamp
    (thpt_slo_basis_5s / _1s) wins; absent it, the whole-triple thpt_slo_basis governs
    both bars (the pre-Gap-B convention). The emitter fail-closes on carrying both, so
    exactly one convention is ever present. Returns the basis string or None."""
    key = "thpt_slo_basis_5s" if bar_s == 5.0 else "thpt_slo_basis_1s"
    pb = (m or {}).get(key)
    return pb if pb is not None else (m or {}).get("thpt_slo_basis")


def _resolve_cluster_basis(sources):
    """hb#174: per-runtime SLO-basis stamp for the caption disclosure. Returns
    {runtime: [(basis, n_exec_ok), ...]} — one entry per DISTINCT non-default
    thpt_slo_basis (one with an _SLO_BASIS_NOTES entry) across ALL of that runtime's
    landed-X scenarios, in scenario order. hb#214: the prior first-landed-match-wins
    rule (mirroring the X resolver) silently SHADOWED a second scenario that landed on
    a DIFFERENT basis — e.g. a positive literal basis in one activation mode alongside
    a floor-zero in another — so the caption disclosed only one of the two bases a
    reader was looking at. Every distinct basis now gets its own line. n_exec_ok is the
    MINIMUM known thpt_slo_n_exec_ok across that basis's landed scenarios (the weakest
    credited sample count — the conservative one to caption) or None when absent
    everywhere. Resolved from the SAME metrics dict that carries the landed X — a basis
    stamp on a cell with no landed cluster figure discloses nothing (there is no figure
    to caveat), mirroring _resolve_cluster_x's landed-X rule."""
    bases = {}
    for rt, rt_scen in sources.items():
        if not rt_scen:
            continue
        per_basis = {}
        order = []
        for sc in rt_scen.values():
            m = (sc or {}).get("metrics") or {}
            n = m.get("thpt_slo_n_exec_ok")
            if not isinstance(n, int) or isinstance(n, bool):
                n = None
            # hb#230 Gap B: a cell may carry the whole-triple basis OR per-bar bases
            # (never both — the emitter fail-closes on mixing). Disclose EVERY distinct
            # non-default basis a reader will see, so the caption never omits the Class-A
            # *** basis on a mixed-basis cell. The whole-triple basis keeps its landed-X
            # gate (a basis on a cell with no landed cluster figure caveats nothing); a
            # per-bar stamp is the producer's own assertion that THAT bar resolved to the
            # basis (including the no-number honest-0 / unresolved-bounds cases the regen
            # script emits), so its presence alone qualifies it for disclosure.
            for basis, eligible in (
                (m.get("thpt_slo_basis"), _landed_cluster_x(m) is not None),
                (m.get("thpt_slo_basis_5s"), True),
                (m.get("thpt_slo_basis_1s"), True),
            ):
                if not eligible:
                    continue
                if basis not in _SLO_BASIS_NOTES:
                    continue
                if basis not in per_basis:
                    per_basis[basis] = n
                    order.append(basis)
                elif n is not None and (
                    per_basis[basis] is None or n < per_basis[basis]
                ):
                    per_basis[basis] = n
        if order:
            bases[rt] = [(b, per_basis[b]) for b in order]
    return bases


def _core_metrics_glossary_bullets():
    """The 'How to read the cells' glossary bullets — the full cell-decoding key.

    Shared single source: render_matrix(include_legend=True) inlines these (unit-test parity),
    and render_core_metrics_legend() emits them under an H2 in DETAILS (the home page carries
    only a compact pointer). Returns a list of one bullet per element (byte-identical to the
    prior inline appends when extended in order)."""
    return [
        "- **TTFE** — Time-To-First-Instruction: wall-clock from asking for a sandbox until your "
        "agent's first instruction has run and returned a result — not merely pod-Ready.",
        "- **p50 / p95** — median / worst-in-20; plan UX around p95. Read TTFE *down* a column, "
        "not across rows — activation-mode rows differ in sample size by orders of magnitude "
        "(each cell shows its own `(count=N)`), so only rows with similar N are comparable.",
        "- **Warm-pool hit vs. Unique-image cold (RL reality)** — a warm-pool hit is served from "
        "a pre-started idle pool (startup already paid); the unique-image-cold row is a fresh "
        "sandbox on a never-pulled image — image pull + cold start on the critical path, the "
        "worst case a reinforcement-learning training loop actually hits.",
        "- **Throughput `x /node · y /cluster`** — per-node is the engineering rate (comparable "
        "across runtimes); per-cluster is a MEASURED per-activation-mode rate at the node count "
        "named in the bold caption above the table — the per-cluster fire is separate from the "
        "per-node fire, so the build line's `node_count` (the per-node fire's shape) does not "
        "apply to it — never a per-node × N extrapolation.",
        "- **Why the per-node rate can repeat across the `<5s` and `<1s` columns** — the two "
        "throughput columns are SLO-gated: a per-node figure fills a column when the row's TTFE "
        "p95 clears THAT column's bar. When p95 clears BOTH bars (p95 < 1s ⇒ p95 < 5s too), the "
        "same per-node rate legitimately satisfies both, so it renders identically in both "
        "columns — not a copy-paste. The two per-CLUSTER halves can still differ (or carry "
        "different caveats) because each bar's cluster figure is credited under its own basis "
        "— and may even coincide numerically while resting on DIFFERENT bases (e.g. a literal-"
        "TTFE floor at the <5s bar and an acquire-side floor at the <1s bar landing on the same "
        "number), distinguished by the per-cell caveat tag (`***U`/`***Z`/`***K` — see below), "
        "not by the digits.",
        "- **`≥y /cluster` (certification floor)** — a per-cluster figure prefixed `≥` is a LOWER "
        "BOUND on the true sustainable rate, not the rate itself. Two floor constructions carry "
        "it, and the `≥` arises for DIFFERENT reasons: **(a) a literal-TTFE-upper-bound basis** — "
        "a TTFE ceiling `t` yields a rate floor `≥1/t` by construction (the exec-probe warm p95 is "
        "an UPPER bound on TTFE, so the derived rate is a lower bound regardless of the trust gate; "
        "this basis is not trust-gate-capped once the controller cross-check corroborates it). "
        "**(b) the uncorroborated acquire-side basis (`***U`)** — the highest rung whose acquire "
        "p95 cleared the bar, with the controller cross-check dropped; `≥` because upstream #940 "
        "double-records warm-path Ready transitions, disqualifying the higher rungs, so the ladder "
        "never saturated and a higher real rate exists but is presently uncorroborated. These are "
        "NOT the same graduation story. The (b) trust-gate cap is what agent-sandbox#1114 (merged "
        "2026-07-22, the controller double-count fix) clears — but re-measuring against a "
        "#1114-bearing controller only restores controller corroboration, moving the cell onto the "
        "(a) construction, which STILL renders `≥`. A floor graduates to a *bare* measured rate "
        "ONLY under the true-TTFE (webhook-corroborated) basis — a graduated real number rather "
        "than a bound — not merely from a gate fix plus a re-fire. A `≥` figure below the cluster "
        "sizing target still carries ⚠️ (the floor itself is under target); an uncorroborated "
        "floor also carries `***U` (see the caveat block below).",
        "- **honest `0`** — the measurement ran and could not hold the bar: the measured TTFE p95 misses "
        "that cell's SLO, so the SLO-compliant throughput is a real `0` (we print it rather than "
        "round up) — not \"zero activity\". A derived `0` inherits the sample basis of the p95 it "
        f"reads, so a single-sample p95 yields a single-sample `0` carrying {_LOW_N_MARK}.",
        "- **measured `0` (floor-zero)** — the second zero provenance, distinct from the "
        "derived `0` above: here the SLO-rate fire itself RAN and emitted a stamped zero — at "
        "the lowest offered rate fired, the majority of samples missed the bar by a "
        "pre-declared margin even after granting every unevaluable sample a pass, so no "
        "compliant operating point exists at or above the floor. When this basis is in play "
        "the italic basis line above the table names it; a derived `0` instead reads off a "
        "measured TTFE p95 with no throughput fire behind it.",
        "- **A sub-bar TTFE p95 next to a `0` in that column's throughput** (e.g. the "
        "unique-image-cold row's 3.x s p95 under the <5s bar, yet <5s throughput `0`) — not a "
        "contradiction: the TTFE p95 is the acquire-side exec-probe (clean here), but the "
        "throughput gate is the CONTROLLER cold-start floor, a SEPARATE and higher measurement "
        "that exceeds both bars at every rate — so no compliant operating point exists and the "
        "rate is a measured `0` (tagged `***Z`; see the cold-start floor zero note in the caveat "
        "block below).",
        f"- **A per-node rate next to a p95 OVER that column's bar (`x /node{_RATE_OVER_P95_MARK}`)** "
        "— the inverse of the case above, and likewise not a contradiction: the per-node figure "
        "is a RATE (the fraction of the sample window's activations that cleared the bar), while "
        "the row's TTFE p95 is a PERCENTILE (the 95th-worst single sample). The two are different "
        "measured quantities, so a nonzero rate can coexist with an over-bar p95 honestly — e.g. "
        "most samples clear the bar and a slow tail alone pulls the percentile over it. Tagged "
        f"`{_RATE_OVER_P95_MARK}` so a reader knows to read this note instead of assuming a "
        "render error or a stale figure.",
        f"- **{_LOW_N_MARK}** — measured over fewer than N={TTFE_COMPARABILITY_MIN_N} samples: "
        "read it as a single observation, not a distribution; do not rank it against a high-N row.",
        "- **⚠️** — a miss flag: on Execution Success it marks <100% (and prints the "
        "succeeded/total fraction); on a per-cluster throughput figure it marks a rate below the "
        "cluster sizing target.",
        "- **`pending`** — awaits its TTFE-instrumented run (a genuinely not-yet-run cell).",
        "- **`pending (cluster-fire)`** — the per-node figure is measured, but the per-cluster "
        "half awaits a schema-validated per-mode cluster-throughput fire (distinct from the "
        "whole-cluster Saturation ceiling in DETAILS, which measures the aggregate ceiling at "
        "overload, not these SLO-gated per-mode cells).",
        "- **`N/A`** — `N/A` by construction: Resume-from-suspend × Kata + microVM can never be "
        "measured — CRIU checkpoint/restore does not transfer to the Kata VM isolation model — "
        "distinct from `pending`, which awaits a run.",
        "- **Why a `pending` is not just printed as `0`** — a blunter display rule would print "
        "`0` for any cell that cannot show compliance: an upper-bound latency basis cannot prove "
        "a true miss, a failed agreement gate cannot certify a rate in either direction, and a "
        "floor rung whose samples are majority-unevaluable cannot establish the negative claim "
        "(the floor-zero predicate's evaluability cap). Each such cell graduates — to a measured "
        "rate or a floor-zero `0` — the moment its condition clears. (Two now-closed `pending` "
        "flavors that used to illustrate this — `trust-gate` and `no-compliant-rung` — are "
        "documented in [Resolved (archive)](#resolved-archive) below.)",
    ]


def _core_metrics_caveat_lines():
    """The consolidated `***` caveat block (header + intro + the two LIVE class bullets),
    returned INCLUDING its trailing blank line. Emitted only when the matrix actually earned a
    `***` caveat — see the matrix_has_starstar snapshot in render_matrix / the recompute in
    render_core_metrics_legend. The archive of graduated flavors (former `pending (...)` and
    `***` classes) lives separately in _resolved_archive_lines(), which is NOT gated on
    matrix_has_starstar — those flavors are historical regardless of what the current matrix
    shows."""
    return [
        "**Published-with-caveat cells (`***Z` / `***K`)**",
        "",
        "A cell tagged `***<letter>` prints the best figure we measured, not an "
        "honest-empty `pending`: the measurement exists but carries a bound or a "
        "single-source caveat, spelled out below. Each letter names a distinct "
        "measurement basis, so a cell's tag alone tells you which caveat below applies "
        "— no need to cross-reference by row/column position. The number is real — read "
        "it with its caveat. Each class graduates to a clean figure when its upstream "
        "fix lands.",
        "",
        "- **`***Z` — Cold-start floor zero** (unique-image-cold SLO-rate cells) — a "
        "MEASURED zero, not an absence: the controller cold-start floor (~14.7s p50) "
        "exceeds BOTH throughput bars at every offered rate (rate-independent), so no "
        "compliant operating point exists. The zero is the sandbox cold-start floor, not "
        "an acquire-path miss — the acquire-side latency is clean sub-second (~5/s) at "
        "every rung. Corroborated by a controller-MEASURED (trusted) rung whose cold p50 "
        "is also over both bars, so it is never asserted from the controller-untrusted "
        "floor rung alone. "
        "Tracked upstream: " + upstream_prose_refs("no-compliant-rung") + ".",
        "- **`***K` — Unresolved bounds** (`unk.***K`, Kata + microVM unique-image-cold "
        "5s cell) — a measurement was taken, but the true TTFE p95 is bounded in "
        "[~2.5s, ~8.4s] at 0.05–0.07/s: the controller-cold proxy (lower bound) does not "
        "breach the 5s bar and the literal exec-probe (upper bound) does not clear it, so "
        "no claim is supportable either direction. The exec-probe upper bound includes "
        "Kata exec websocket setup overhead; the 5s bar sits INSIDE the bracket — no "
        "supportable claim either way. "
        "Tracked upstream: " + upstream_prose_refs("no-compliant-rung") + ".",
        "",
        "Two more caveat classes (`***U`, `***R`) formerly applied here and have since "
        "graduated — see [Resolved (archive)](#resolved-archive) below; a fresh read of "
        "this section never needs them.",
        "",
    ]


def _resolved_archive_lines():
    """The `## Resolved (archive)` section: former `pending (...)` and `***` classes that no
    longer back any live Core Metrics cell. Always emitted when include_legend=True, regardless
    of whether the current matrix carries a `***` cell — the pending-flavor archive entries
    (upstream-blocked, trust-gate, no-compliant-rung) apply independently of `***` status, and
    the glossary's forward reference to this section is unconditional. Returned INCLUDING its
    trailing blank line."""
    return [
        "## Resolved (archive)",
        "",
        "The classes below no longer back any live Core Metrics cell — each was resolved "
        "by a merged upstream fix and the affected cells graduated to measured numbers. "
        "Kept here (not deleted) so an older discussion or result file that still cites "
        "one of these names resolves to an explanation instead of a dangling reference — "
        "mirrors the equivalent archive section in WORK_IN_PROGRESS.md.",
        "",
        "- **`pending (upstream-blocked)`** — formerly gated the gVisor resume cell: the "
        "run landed, but an upstream controller gap (the resume path's Suspended condition "
        "never cleared on resume) held the SLO-compliant figure. That fix has since merged "
        "upstream and a fresh resume probe landed, so the gVisor resume row now carries "
        "measured numbers. "
        "Tracked upstream: " + upstream_prose_refs("upstream-blocked") + ".",
        "- **`pending (trust-gate)`** — formerly gated warm-pool per-cluster SLO-rate "
        "cells: derivation was refused by the acquire/controller agreement gate (rel-diff "
        "tolerance 0.10) because the upstream controller startup-latency histogram "
        "double-recorded Ready transitions on stale-informer replays, inflating the "
        "controller leg ~1.7–2× on warm-pool-fulfilled paths. Both upstream legs have "
        "since merged and a post-fix fire confirmed the agreement gate now passes. "
        "Tracked upstream: " + upstream_prose_refs("trust-gate") + ".",
        "- **`pending (no-compliant-rung)`** — formerly gated cold-start per-cluster "
        "SLO-rate cells: the trust gate PASSED, but every measured rung's p95 (on the "
        "literal-TTFE upper-bound basis) sat over the cell's SLO bar, and the tighter "
        "true-TTFE basis that could shrink the bound had no production writer upstream. "
        "That writer has since merged and both formerly-gated cold cells graduated "
        "independently. "
        "Tracked upstream: " + upstream_prose_refs("no-compliant-rung") + ".",
        "- **`***U` — Uncorroborated acquire-side rate** (formerly applied to warm-pool-hit "
        "SLO-rate cells) — applied while controller corroboration was unavailable: the "
        "upstream controller startup-latency histogram double-recorded Ready transitions "
        "on stale-informer replays, inflating the controller leg ~1.7–2× on "
        "warm-pool-fulfilled paths, so a published rate could only cite the single-source "
        "acquire-side leg. Both upstream legs have since merged and a post-fix fire "
        "confirmed the histogram-vs-acquire cross-check now PASSES, so the warm-pool "
        "cells are no longer single-source-capped. "
        "Tracked upstream: " + upstream_prose_refs("trust-gate") + ".",
        "- **`***R` — Resume probe ceiling** (formerly the two TTFE cells of the "
        "Resume-from-suspend × gVisor row) — applied while the resume never completed "
        "(the upstream Suspended condition never cleared on resume), so the probe "
        "recorded only the wall-clock ceiling it spent waiting and that ceiling printed "
        "as a floor (`≥N.Ns`). That upstream fix has since merged and a fresh resume "
        "probe landed, so the resume row graduated to a measured completion "
        "distribution. "
        "Tracked upstream: " + upstream_prose_refs("upstream-blocked") + " (see "
        "[WORK_IN_PROGRESS.md#upstream-blocked](WORK_IN_PROGRESS.md#upstream-blocked)).",
        "",
    ]


def _core_metrics_compact_legend_lines():
    """Home-page compact cell key: a short scannable decode line plus a pointer to the full
    glossary in DETAILS. MUST stay `***`-free — render_core_metrics_legend() gates its caveat
    block on `'***' in render_matrix(..., include_legend=False)`, so any `***` here would
    falsely force the caveat block on for clean scenarios."""
    return [
        "**Reading the cells** — TTFE is Time-To-First-Instruction (wall-clock until your "
        "agent's first instruction returns, not merely pod-Ready). Read TTFE p50/p95 *down* a "
        "column, not across rows — activation-mode rows differ in sample size by orders of "
        "magnitude.",
        "",
        "| Symbol | Meaning |",
        "|---|---|",
        "| `(count=N)` | Sample size for that cell — compare down a column, not across rows |",
        "| `†` | Sub-N sample: a single observation, not a distribution |",
        "| `⚠️` | Miss flag: sub-100% Execution Success, or a per-cluster rate below the "
        "sizing target |",
        "| `pending` | No publishable figure yet (currently only the `(cluster-fire)` flavor "
        "is live) |",
        # hb#518: a plain, untagged `0` and a caveat-tagged floor-zero cell share one glyph but
        # rest on different evidential bases — a table row each keeps the distinction scannable.
        # MUST stay asterisk-free (see the docstring above) — no literal tag characters.
        "| plain `0` | DERIVED zero: implied by that row's TTFE p95 exceeding the column's "
        "bar, no throughput fire behind it — can flip to a real rate on a latency improvement "
        "alone |",
        "| caveat-tagged floor-zero | MEASURED zero from an actual throughput fire — needs "
        "the cold-start floor itself to move (see the full key) |",
        "",
        "Full cell-decoding key — TTFE basis, honest vs. measured zeros, the dual per-node · "
        "per-cluster throughput pair, the certification-floor `≥` figures, every `pending` "
        "flavor, and the published-with-caveat tag classes — is in "
        "[DETAILS.md](DETAILS.md#how-to-read-the-core-metrics-cells).",
        "",
    ]


def render_core_metrics_legend(results, kata_results=None):
    """The full Core-Metrics cell-decoding key, for DETAILS.md (relocated off the home page,
    hb home-page slim). Emits the same glossary + `***` caveat block render_matrix carries
    when include_legend=True, under an H2 whose anchor the home-page compact key points at.
    The caveat block gates on whether the matrix earned a `***` — recomputed here faithfully
    from the include_legend=False render (which carries the same pre-snapshot matrix cells +
    basis lines, and no `***` in the compact legend/Max-Density/kata/banner tail)."""
    include_caveats = "***" in render_matrix(
        results, kata_results=kata_results, include_legend=False
    )
    out = ["## How to read the Core Metrics cells", ""]
    out.extend(_core_metrics_glossary_bullets())
    out.append("")
    if include_caveats:
        out.extend(_core_metrics_caveat_lines())
    out.extend(_resolved_archive_lines())
    return "\n".join(out).rstrip()


def render_matrix(results, kata_results=None, include_legend=True):
    """Render the doc's 7-column Core Metrics Table (primary results + optional kata results).

    A single run measures ONE runtime (provenance.runtime, default gvisor); that runtime's rows
    fill from the measured scenarios. The Kata + microVM rows can fill from a SECOND results
    file (`kata_results`, the sandbox-kata product) measured in a separate run on the kata pool
    — the run split exists because `run --product sandbox-kata` writes its own latest.json and
    can never overwrite the gVisor artifact (harness/scenario_map.py). kata_results is used
    ONLY when its product is "sandbox-kata" AND its cleaned provenance.runtime is kata-microvm;
    the primary results win on conflict (a kata-measured primary run ignores kata_results).
    Unmeasured runtime rows render `pending`. Per-metric cells render `pending` until the
    TTFE-instrumented harness emits them, so the page degrades to an honest skeleton rather
    than a blank or a guess.
    """
    product = results.get("product")
    if product not in PRODUCTS:
        raise ValueError(f"unknown product (not in closed schema): {product!r}")

    prov = _clean_provenance(results.get("provenance"))
    measured_runtime = prov.get("runtime") or "gvisor"
    # Per-runtime scenario sources. The primary results claim their measured runtime first;
    # kata_results may ONLY fill the kata-microvm slot if still empty (primary wins).
    sources = {measured_runtime: _matrix_scenarios(results.get("scenarios"))}
    kata_prov = None
    kata_gen = None
    if (
        isinstance(kata_results, dict)
        and kata_results.get("product") == "sandbox-kata"
        and "kata-microvm" not in sources
    ):
        kp = _clean_provenance(kata_results.get("provenance"))
        if kp.get("runtime") == "kata-microvm":
            sources["kata-microvm"] = _matrix_scenarios(kata_results.get("scenarios"))
            kata_prov = kp
            g = kata_results.get("generated_at")
            if isinstance(g, str) and _ISO.match(g):
                kata_gen = g

    header = [
        "Runtime",
        "Activation Mode",
        "Throughput @ <5s TTFE (sb/s — node · cluster)",
        "Throughput @ <1s TTFE (sb/s — node · cluster)",
        "TTFE p50",
        "TTFE p95",
        "Execution Success (Honesty Check)",
    ]
    lines = ["## Agent Sandbox — Core Metrics", ""]
    # hb#132/#134: throughput cells are dual (`per-node · per-cluster`). The static how-to-read
    # (TTFE-down-a-column, the dual-throughput pair, the † and ⚠️ flags, the three pending flavors,
    # N/A-by-construction) lives ONCE in the "How to read the cells" legend below the table; the
    # caption above carries only the DYNAMIC cluster measurement size (X nodes), which a static
    # legend cannot. X is resolved per runtime from the landed thpt_cluster_node_count; absent
    # everywhere, the cluster halves render `pending (cluster-fire)` and the caption just anchors
    # the pair. When two runtimes' cluster legs landed at the SAME X the caption stays
    # single-figure; at DIFFERENT X it names each runtime's X explicitly so one runtime's figures
    # are never captioned with the other's node count (the mixed-X ambiguity).
    cluster_xs = _resolve_cluster_x(sources)
    distinct_xs = set(cluster_xs.values())
    if len(distinct_xs) == 1:
        cluster_x = next(iter(distinct_xs))
        lines.append(
            "**Throughput is dual — `per-node · per-cluster`.** Per-cluster figures here are a "
            f"MEASURED cluster rate at {cluster_x} nodes; see the legend below for how to read "
            "the pair. (This is a different `node_count` than the one printed in each build's "
            "provenance banner below the table — that one describes the per-node fire's shape, "
            "not this per-cluster measurement.)"
        )
    elif len(distinct_xs) > 1:
        per_rt = "; ".join(
            f"{RUNTIME_LABELS[rt]} at {cluster_xs[rt]} nodes"
            for rt in MATRIX_RUNTIMES
            if rt in cluster_xs
        )
        lines.append(
            "**Throughput is dual — `per-node · per-cluster`.** Per-cluster figures are measured "
            f"per runtime at DIFFERENT node counts — {per_rt} — so they are NOT comparable across "
            "runtimes here (different X); see the legend below. (This is a different `node_count` "
            "than the one printed in each build's provenance banner below the table — that one "
            "describes the per-node fire's shape, not this per-cluster measurement.)"
        )
    else:
        lines.append(
            "**Throughput is dual — `per-node · per-cluster`.** Cluster halves render "
            "`pending (cluster-fire)` until a schema-validated per-mode cluster-throughput fire "
            "lands them; see the legend below for how to read the pair."
        )
    # hb#174: when a runtime's per-cluster figures were derived from a NON-DEFAULT SLO basis
    # (the literal upper-bound leg), disclose it right under the @X caption — the reader must
    # see the basis + its measurement window next to the figures, not buried in the legend.
    # true_ttfe (the default) adds no line. One italic line per affected runtime.
    cluster_bases = _resolve_cluster_basis(sources)
    for rt in MATRIX_RUNTIMES:
        # hb#214: one italic line PER DISTINCT BASIS — a runtime whose activation
        # modes landed on different bases (e.g. a positive literal rate in one row
        # and a floor-zero in another) discloses both, in scenario order.
        for basis, n_exec_ok in cluster_bases.get(rt, ()):
            note = _SLO_BASIS_NOTES[basis]
            # hb#174 sign-off (c): a literal p95 over 20 <= n < 100 warm-exec samples
            # derives honestly (the harness floor is 20) but is a COARSE percentile —
            # caption the weakest credited sample count so the reader can weigh it.
            if n_exec_ok is not None and 20 <= n_exec_ok < 100:
                note = f"{note}; coarse p95 (n={n_exec_ok} warm-exec samples)"
            # hb#404: prefix each disclosure line with the same per-cell tag the cell
            # itself would carry — a *** basis names its letter; a non-*** basis (e.g.
            # the literal-upper-bound leg, which fills the cell clean) says so
            # explicitly rather than leaving the reader to infer "no tag" from absence.
            tag = _STARSTAR_TAGS.get(basis)
            if tag:
                prefix = f"**[`{tag}`]** "
            else:
                base_prefix = basis.split("+", 1)[0]
                label = "-".join(
                    "TTFE" if part == "ttfe" else part for part in base_prefix.split("_")
                )
                prefix = f"**[no `***` tag — {label} basis]** "
            lines.append(f"{prefix}*{RUNTIME_LABELS[rt]} per-cluster rates: {note}.*")
    lines.append("")
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")

    # #4420: collect (runtime, activation-mode) cells whose scenario OUTCOME is FAIL so a
    # loud caveat can be emitted below the table. A FAIL cell keeps its real metrics (unlike
    # a `pending` cell, whose metrics _matrix_scenarios suppresses) — a FAILing measurement
    # is honest data, not absent — but rendering those numbers with no FAIL disclosure would
    # let an SLA-failing cell read as a clean pass, the same silent-downgrade the North Star
    # caption's own FAIL caveat closes.
    fail_cells = []

    # #6913: the suspend_resume Suspended-persists FAIL is a CONTRACT regression, not a
    # metric-SLA miss (see the disclosure-gate comment in the loop). Collected separately so
    # it gets its own honest caveat ("contract regression … not an SLA miss") rather than the
    # metric-SLA-miss wording, which would misdescribe a no-measurement FAIL.
    contract_fail_cells = []

    # hb#554: collect (runtime, activation-mode, date) for any row whose per-cluster SLO
    # triple carries a `thpt_slo_measured_at` stamp — the disclosure that lets a reader
    # tell a carried (point-in-time, do-not-auto-decay) cluster figure from today's fresh
    # per-node fire, mirroring render_cluster_saturation's own `_Measured {date} — ...
    # (point-in-time)._` caption for the analogous top-level block.
    stale_triple_cells = []

    for rt in MATRIX_RUNTIMES:
        rt_label = RUNTIME_LABELS[rt]
        rt_scen = sources.get(rt)
        measured = rt_scen is not None
        for scen_name, mode_label in ACTIVATION_MODE_ROWS:
            is_resume = scen_name == "suspend_resume"
            # Resume-from-suspend × Kata+microVM is N/A by construction: CRIU
            # checkpoint/restore does not transfer to the Kata VM isolation model
            # (harness/scenarios/suspend_resume.py), so this cell can NEVER be
            # measured. Render it na-by-design — NOT `pending`, which would imply a
            # future measurement that is structurally impossible. Holds regardless of
            # which runtime this run measured (a kata-measured run still N/As it).
            if is_resume and rt == "kata-microvm":
                na_cell = link_pending(_NA)
                lines.append("| " + " | ".join([rt_label, mode_label] + [na_cell] * 5) + " |")
                continue
            sc = rt_scen.get(scen_name) if measured else None
            sc_pending = bool(sc) and sc.get("outcome") == "pending"
            sc_fail = bool(sc) and sc.get("outcome") == "FAIL"
            m = sc["metrics"] if sc else {}
            # #4420 disclosure-gate for the loud ⚠️ FAIL tag. Two honest FAIL classes:
            #   - metric-SLA-miss FAIL (throughput/TTFE family): earns the tag ONLY with a
            #     REAL TTFE measurement — the caveat asserts a measured SLA miss. The
            #     never-reached-first-execution FAIL (empty TTFE, only exec_success_rate:0.0)
            #     is a DIFFERENT failure the Execution-Success cell already discloses (`0% ⚠️`);
            #     tagging it would claim a measurement that isn't there. Mirrors the North Star
            #     caveat's own `p95 is not None` gate.
            #   - suspend_resume FAIL (#6913 re-key): ALWAYS a CONTRACT regression of the
            #     closed agent-sandbox#1150 fix (Suspended persisted past resume), NEVER a
            #     metric-SLA miss — the scenario's verdict is set SOLELY by the Suspended-clear
            #     check (harness/scenarios/suspend_resume.py `_eval_resume_gap`); ttfe/exec are
            #     orthogonal and never flip it. In the INERT (TTFE-probe-off) default it carries
            #     NO ttfe AND no exec_success_rate, so the ttfe-gated tag alone would leave it
            #     reading as a bare `pending` cell — silently downgrading a closed gate back to
            #     a known-gap (the exact #4420 failure this re-key exists to catch). Tag it off
            #     the RAW outcome so the regression fails LOUD; its own honest caveat (below)
            #     carries the "contract regression, not an SLA miss" framing without claiming a
            #     ttfe measurement that isn't there (fabricating exec_success_rate:0.0 is
            #     rejected for the same reason — no exec probe ran in INERT mode).
            resume_contract_fail = is_resume and sc_fail
            sc_fail = resume_contract_fail or (
                sc_fail and ("ttfe_p95_ms" in m or "ttfe_p50_ms" in m))

            # hb#230 Fork 5 (resume Class-C ceiling): the gVisor resume row DID record a probe
            # ceiling — the wall-clock it waited out against a never-clearing Suspended
            # condition (upstream #873 → #1150). Per alex's doctrine flip (a caveated measured
            # number always beats an empty cell), publish that ceiling as `≥<X>s***` — but
            # ONLY in the two TTFE columns, where a duration is the correct unit. The earlier
            # revision filled the ceiling across ALL FIVE cells, which stamped a *duration*
            # (`≥34.6s`) into the two THROUGHPUT columns (a rate, sb/s) and the EXECUTION-SUCCESS
            # column (a %) — a units mismatch (hb#230 nit). The correct per-column render:
            #   - throughput (<5s / <1s):  `0*** (upstream-blocked)` — zero sandboxes reached a
            #     TTFE bar because the resume never completed; the rate is a true zero, not a floor.
            #   - TTFE p50 / p95:          `≥<X>s***` — the honest measured wall-clock floor.
            #   - execution success:       `0/N completed***` — zero of N probe attempts completed.
            # The `***` on each points at the consolidated Class-C footnote (probe ceiling, resume
            # never completed). Scoped to the gVisor resume row (Kata resume is N/A-by-construction,
            # handled above); a resume row with no recorded ceiling falls through to the normal
            # pending path. It is NOT a resume TTFE (the operation never completes) — the `≥` and
            # the footnote carry that; the number is the honest measured wall-clock floor.
            # Transition-guard: gate the ceiling override on `sc_pending`. The override
            # is an honest-empty→caveated-measured UPGRADE that only holds while the row is
            # pending; if the resume row ever GRADUATES (outcome=="pass") but still carries a
            # vestigial `resume_probe_ceiling_ms`, an ungated override would MASK the five real
            # graduated metrics behind a stale ceiling — a silent trust downgrade. A graduated
            # row must fall through to normal per-cell metric rendering.
            resume_ceiling_ms = sc.get("resume_probe_ceiling_ms") if sc else None
            if is_resume and rt == "gvisor" and resume_ceiling_ms is not None and sc_pending:
                ceiling_tok = f"≥{resume_ceiling_ms / 1000.0:.1f}s***R"
                thpt_tok = "0***R (upstream-blocked)"
                n_attempts = sc["n"] if (sc and sc.get("n")) else 0
                success_tok = f"0/{n_attempts} completed***R"
                resume_cells = [thpt_tok, thpt_tok, ceiling_tok, ceiling_tok, success_tok]
                lines.append(
                    "| " + " | ".join([rt_label, mode_label] + resume_cells) + " |"
                )
                continue

            # A pending cell distinguishes WHY it is pending. The canonical case is the
            # gVisor resume row: its run DID land, but an upstream controller bug (the
            # Suspended condition never clears) blocks graduation — that is `upstream-blocked`,
            # NOT a not-yet-run cell. Render `pending (<reason>)` so a reader cannot mistake a
            # known-upstream-gap for an unmeasured one. A pending scenario with no carried
            # reason falls back to bare `pending` (a genuinely not-yet-run cell). Non-measured
            # runtime rows (sc is None) also fall back to bare `pending` — correct, since they
            # simply were not measured in this run.
            pending_tok = _PENDING
            pending_refs = ""
            if sc_pending:
                reason = sc.get("pending_reason")
                if reason:
                    pending_tok = f"{_PENDING} ({reason})"
                    # hb#181: a reasoned pending cell carries its exact upstream refs
                    # (issue + fix-PR with live status) inline, right after the linked
                    # token — "" for classes with no upstream mapping (e.g. a bare
                    # not-yet-run or a cluster-fire pend), so unmapped cells are unchanged.
                    pending_refs = upstream_cell_refs(reason)

            def cell(key, fmt):
                return fmt(m[key]) if key in m else pending_tok

            # A pending scenario's N is a probe-attempt count, not a published sample size;
            # render it `pending` too so the whole row reads pending until graduation.
            n_val = sc["n"] if (sc and sc["n"] > 0 and not sc_pending) else None
            n_cell = str(n_val) if n_val else pending_tok

            # Low-N TTFE cells carry a small-sample marker so a reader does not rank them
            # against a high-N row (a single-sample p50 is not a distribution). Mark only a
            # rendered measurement (not `pending`) whose N is known and below the floor.
            low_n_ttfe = n_val is not None and n_val < TTFE_COMPARABILITY_MIN_N

            # hb#142: each TTFE cell carries its sample count inline as `value (count=N)` —
            # the receipt that used to live in a separate DETAILS "Sample Sizes" appendix now
            # rides the cell itself, so a reader never has to cross-reference to tell a p50 over
            # hundreds of samples from a p50 over one. The low-N dagger stays AFTER the count:
            # `value (count=N) †`. Only a rendered measurement with a known N gets the suffix; a
            # `pending` cell (n_val is None) renders unchanged.
            def ttfe_cell(key):
                v = cell(key, _fmt_secs)
                if n_val is None or v == pending_tok:
                    return v
                v = f"{v} (count={n_val})"
                return f"{v} {_LOW_N_MARK}" if low_n_ttfe else v

            # hb#132 dual cell: `<node> /node · <cluster>`. The per-node half preserves the prior
            # single-figure behavior (absent ⇒ the whole cell is pending, incl. `pending
            # (<reason>)` for a pending scenario since m is empty). The cluster half pends
            # `pending (cluster-fire)` until the schema-validated fire carries the per-cluster
            # field; a landed cluster figure below the sizing target carries ⚠️. The cluster half
            # additionally requires thpt_cluster_node_count in the SAME metrics dict — a
            # per_cluster figure with no X has no measurement size to disclose, so it pends
            # rather than rendering a real rate under a caption that can't pin its X
            # (defense-in-depth: the emit side already couples the triple all-or-nothing).
            def thpt_dual_cell(node_key, cluster_key, bar_s):
                # hb#230 Gap B: the basis governing THIS bar (per-bar stamp wins). A
                # non-corroborated / bounded / cold basis earns the per-cell *** caveat
                # (pointing at the consolidated footnote); the corroborated literal bases
                # stay clean (Fork 3). Computed once, applied to whichever token renders.
                bar_basis = _bar_basis(m, bar_s)
                star = _STARSTAR_TAGS.get(bar_basis, "")
                if node_key not in m:
                    # hb#230 Kata-cold ruling: the unresolved-bounds cell — a measurement
                    # WAS taken but the bar sits inside the [lower, upper] bracket, so no
                    # claim is supportable either direction. Render `unk.***K` (NOT pending
                    # — pending implies unmeasured). Keyed off the per-bar basis stamp the
                    # regen script emits for exactly this cell.
                    if bar_basis == "unresolved_bounds_bar_bracketed":
                        return f"unk.{star}"
                    # Derivable honest-0 (hb#142.1): the throughput fire has not run, but if the
                    # TTFE p95 IS measured and exceeds this cell's bar, then p95 misses the bar —
                    # the SAME condition under which a real fire emits `0` (see the honesty
                    # footnote below). So the sustained throughput at that bar is a derived `0`,
                    # obtainable from the measured TTFE distribution with no throughput fire. A
                    # per-node rate of exactly 0 forces the per-cluster rate to 0 (the one exact
                    # case, NOT a per-node × N extrapolation), so BOTH halves render the derived 0.
                    # If p95 is absent or within the bar, we cannot derive a 0 and fall back to
                    # `pending`.
                    p95_ms = m.get("ttfe_p95_ms")
                    if (
                        isinstance(p95_ms, (int, float))
                        and not isinstance(p95_ms, bool)
                        and p95_ms / 1000.0 > bar_s
                    ):
                        return f"{_fmt_num(0)} /node · {_fmt_num(0)} /cluster{star}"
                    # 07-06 SLO-rate fire: the whole cell (both halves) ran and came back
                    # honest-empty for a carried, closed-enum reason — e.g. the cold fire's
                    # every-rung-over-bar `no-compliant-rung`, where a derived 0 is ALSO
                    # unavailable because the row's TTFE p95 sits under the bar (a bracketed,
                    # unmeasured interval). Refs ride INSIDE the returned string because the
                    # data_cells comprehension only appends refs to the exact whole-cell token.
                    cell_reason = m.get(node_key.replace("_per_node", "") + "_pend_reason")
                    if cell_reason:
                        return f"{_PENDING} ({cell_reason})" + upstream_cell_refs(cell_reason)
                    return pending_tok
                node_half = f"{_fmt_num(m[node_key])} /node"
                # hb#(gap 1, a4z1 goal-2.1 display-vs-spec audit): this half prints a REAL
                # measured rate with no cross-check against the row's own p95 — unlike the
                # derived-`0` branch above, which explicitly gates on p95 vs. bar before
                # rendering. Flag it when p95 IS measured and sits over this cell's bar, so a
                # reader isn't left inferring on their own that a nonzero rate next to an
                # over-bar p95 is a render bug rather than two different measured bases.
                p95_ms = m.get("ttfe_p95_ms")
                if (
                    isinstance(p95_ms, (int, float))
                    and not isinstance(p95_ms, bool)
                    and p95_ms / 1000.0 > bar_s
                    and m[node_key] > 0
                ):
                    node_half += f" {_RATE_OVER_P95_MARK}"
                if cluster_key in m and _landed_cluster_x(m) is not None:
                    # hb#230 ask (a): a per-cluster figure from a certification-FLOOR basis
                    # is a LOWER BOUND on the true sustainable rate (trust-gate-capped, so
                    # the ladder did not saturate) — render `≥y`, not a bare `y`, so a
                    # reader can't mistake the certification floor for the capability.
                    # value>0 guard: a floor-zero can never render `≥0`.
                    floor = bar_basis in _FLOOR_BASES and m[cluster_key] > 0
                    pfx = "≥" if floor else ""
                    cluster_half = f"{pfx}{_fmt_num(m[cluster_key])} /cluster"
                    if m[cluster_key] < CLUSTER_THROUGHPUT_TARGET:
                        cluster_half += " ⚠️"
                else:
                    # 07-06 SLO-rate fire: a cluster half whose fire RAN but whose derivation
                    # was refused for a carried, closed-enum reason (e.g. `trust-gate` — the
                    # acq/controller agreement gate failed at every measured rung) renders
                    # that reason + its upstream refs instead of the generic cluster-fire
                    # pend, so a reader can't mistake ran-and-refused for not-yet-fired.
                    cluster_reason = m.get(cluster_key + "_pend_reason")
                    if cluster_reason:
                        cluster_half = f"{_PENDING} ({cluster_reason})" + upstream_cell_refs(
                            cluster_reason
                        )
                    else:
                        cluster_half = f"{_PENDING} ({_CLUSTER_FIRE})"
                return f"{node_half} · {cluster_half}{star}"

            thpt5 = thpt_dual_cell("thpt_under_5s_per_node", "thpt_under_5s_per_cluster", 5.0)
            thpt1 = thpt_dual_cell("thpt_under_1s_per_node", "thpt_under_1s_per_cluster", 1.0)
            p50 = ttfe_cell("ttfe_p50_ms")
            p95 = ttfe_cell("ttfe_p95_ms")
            if "exec_success_rate" in m:
                exec_cell = _exec_cell(m["exec_success_rate"], n_val, m.get("exec_success_n"))
            else:
                exec_cell = pending_tok

            # hb#181: append the upstream refs AFTER link_pending's wrapped token (never
            # inside it — nested markdown links don't render). Only the exact whole-cell
            # pending token gets refs; composite cells (e.g. a dual throughput cell whose
            # cluster half embeds `pending (cluster-fire)`) are left alone.
            data_cells = [
                link_pending(c) + (pending_refs if (pending_refs and c == pending_tok) else "")
                for c in (thpt5, thpt1, p50, p95, exec_cell)
            ]
            # #4420: a FAIL cell renders its real metrics but flags the mode label loudly so
            # the row cannot read as a clean pass; the full disclosure rides the caveat below.
            row_mode_label = f"{mode_label} ⚠️ FAIL" if sc_fail else mode_label
            if sc_fail:
                if resume_contract_fail:
                    contract_fail_cells.append((rt_label, mode_label))
                else:
                    fail_cells.append((rt_label, mode_label))
            stale_ma = m.get("thpt_slo_measured_at")
            if isinstance(stale_ma, str) and stale_ma:
                stale_triple_cells.append((rt_label, mode_label, stale_ma[:10]))
            lines.append(
                "| "
                + " | ".join([rt_label, row_mode_label] + data_cells)
                + " |"
            )
    lines.append("")

    # #4420: emit the loud FAIL disclosure directly under the matrix — mirrors
    # render_cluster_saturation's own outcome=FAIL headline (a FAILing measurement is
    # surfaced as SLA-not-met, never softened into a green cell).
    if fail_cells:
        who = "; ".join(f"**{rt}** {mode}" for rt, mode in fail_cells)
        lines.append(
            f"_⚠️ **Scenario FAIL:** {who} — the row above carries a real measurement whose "
            "own scenario outcome is **FAIL** (SLA not met), not a passing warm hit. The "
            "numbers are honest data, disclosed as a miss rather than dropped or greened; a "
            "later refresh whose scenario returns to PASS clears this._"
        )
        lines.append("")

    # #6913 / #4420: the suspend_resume Suspended-persists FAIL gets its own honest headline —
    # it is a CONTRACT regression (the resume completed but the Suspended condition never
    # cleared), NOT a measured-SLA miss, so the metric-SLA caveat above would misdescribe it.
    # Failing it LOUD here is the whole point of the re-key: a silent downgrade of the closed
    # agent-sandbox#1150 gate back to a benign pending cell is the trust-surface failure this
    # gate exists to catch.
    if contract_fail_cells:
        who = "; ".join(f"**{rt}** {mode}" for rt, mode in contract_fail_cells)
        lines.append(
            f"_⚠️ **Scenario FAIL (resume contract regression):** {who} — the resume "
            "lifecycle completed (new Pod, Ready) but the **Suspended** condition PERSISTED "
            "past the clear-window. This regresses the closed agent-sandbox#1150 fix "
            "(resume status-write retry-to-convergence); it is disclosed LOUD rather than "
            "masked as a benign `pending` cell, and is a contract failure, NOT an SLA miss on "
            "a measured number. A later refresh whose Suspended condition clears returns this "
            "row to PASS._"
        )
        lines.append("")

    # hb#554: emit the point-in-time disclosure for any row whose per-cluster SLO triple
    # is CARRIED from a manual sweep fire (harness/run.py's carry_prior_cluster_triples)
    # rather than freshly measured this run — mirrors the #4420 FAIL disclosure above and
    # render_cluster_saturation's own `_Measured {date} — ... (point-in-time)._` caption.
    # Without this, a frozen cluster figure reads as fresh forever across every daily
    # single-node refresh, exactly the silent-trust-downgrade class #4420 forbids.
    if stale_triple_cells:
        who = "; ".join(
            f"**{rt}** {mode} ({date})" for rt, mode, date in stale_triple_cells
        )
        lines.append(
            f"_📅 **Cluster figure point-in-time:** {who} — the `/cluster` half above is "
            "carried from that date's manual step-up sweep, not this run's daily refresh "
            "(the per-cluster SLO triple is sweep-only; see hb#132/hb#554); the `/node` "
            "half and other columns ARE fresh. A new sweep fire replaces the date and the "
            "figure together._"
        )
        lines.append("")

    # hb#230 (alex doctrine flip): snapshot whether ANY matrix cell earned a *** caveat
    # BEFORE the glossary/footnote text below (which itself contains a literal `***` in the
    # reconciled upstream-blocked line and the consolidated block). Gating the footnote on
    # this snapshot — not on `any("***" in lines)` at append time — keeps the block from
    # self-triggering off its own prose when the matrix has no caveated cell.
    matrix_has_starstar = any("***" in ln for ln in lines)

    # hb#134 refinement (GOAL-2.1 gap-1): Max Density is a spec Core-Metrics figure, so it
    # belongs in this section — but it is per-RUNTIME (constant across a runtime's three
    # activation-mode rows), NOT per-mode. A literal matrix column would repeat each value 3x
    # down the mode rows and falsely imply mode-dependence, so it renders as a compact
    # per-runtime sub-table directly under the matrix rather than a 7th column. This surfaces
    # the spec figure in core (the hb#134 relocation had dropped it to DETAILS-only) while
    # keeping the per-runtime shape honest; the full methodology deep-dive stays in DETAILS.
    lines.append("### Max Density (sandboxes per vCPU)")
    lines.append("")
    lines.append(
        "Density is per-**runtime** — constant across a runtime's activation-mode rows above, "
        "so it renders as a compact per-runtime sub-table here rather than a matrix column (a "
        "column would repeat each value down the mode rows and imply a mode-dependence that "
        "does not exist). Full methodology (per-vCPU denominator, saturation source) is in "
        "[DETAILS.md](DETAILS.md)."
    )
    lines.append("")
    lines.append("| Runtime | Max Density (sb/vCPU) |")
    lines.append("|---|---|")
    for rt in MATRIX_RUNTIMES:
        rt_scen = sources.get(rt)
        density = _runtime_density(rt_scen) if rt_scen is not None else None
        cell = _fmt_num(density) if density is not None else link_pending(_PENDING)
        lines.append(f"| {RUNTIME_LABELS[rt]} | {cell} |")
    lines.append("")

    # honesty / provenance legend (no internal refs — public PII fence). A compact
    # "How to read the cells" glossary replaces the prior loose footnote stack: every honesty
    # semantic (TTFE basis, honest-0, dual throughput, low-N †, ⚠️ miss-flag, the three pending
    # flavors, N/A-by-construction) is one scannable line, plus a plain-English gloss for the
    # warm-pool-hit vs unique-image-cold rows. The DYNAMIC Kata-provenance line stays a separate
    # italic footnote below — it carries closed-schema run provenance the glossary cannot.
    # Home page (include_legend=False) collapses to a compact `***`-free key + a pointer to the
    # full glossary in DETAILS (render_core_metrics_legend); the full glossary+caveat block is
    # inlined only when include_legend=True, which preserves byte-identity for the direct
    # render_matrix() unit tests.
    if include_legend:
        lines.append("**How to read the cells**")
        lines.append("")
        lines.extend(_core_metrics_glossary_bullets())
        lines.append("")
    else:
        lines.extend(_core_metrics_compact_legend_lines())

    # hb#230 (alex doctrine flip, 2026-07-08): the ONE consolidated *** caveat block. Every
    # `***`-tagged cell above publishes the best number we measured rather than an honest-empty
    # `pending` — a caveated number always beats a blank cell. Each caveat class is named ONCE
    # here with its measured basis + the upstream fix that graduates it to a clean number, and
    # its upstream link lives ONLY here (the cells carry the bare `***`, no inline ref). Gated
    # on the matrix-only snapshot so the LIVE block never renders when no cell earned a caveat.
    # The archive of graduated flavors is unconditional — see _resolved_archive_lines().
    if include_legend and matrix_has_starstar:
        lines.extend(_core_metrics_caveat_lines())
    if include_legend:
        lines.extend(_resolved_archive_lines())

    # The Kata rows fill from a SEPARATE run (the sandbox-kata product) on the kata node pool —
    # a different cluster substrate + machine shape than the build banner below — so disclose
    # that run's own closed-schema provenance rather than letting the gVisor banner silently
    # cover both. Same closed-schema fields as the banner; no free-text can ride this line.
    if "kata-microvm" not in sources:
        lines.append("_Kata + microVM rows are not-yet-measured (requires-kata-microvm)._")
    elif kata_prov is not None:
        kata_banner = [
            f"{k}={kata_prov[k]}"
            for k in ("cluster_substrate", "machine_type", "node_count")
            if k in kata_prov
        ]
        if kata_gen:
            kata_banner.append(f"generated-at={kata_gen}")
        lines.append(
            "_Kata + microVM rows are measured in a separate run on the kata node pool"
            + (": " + " · ".join(kata_banner) if kata_banner else "")
            + "._"
        )
    lines.append("")

    banner_order = [
        "cluster_substrate",
        "controller_image",
        "controller_digest",
        "crd_version",
        "suite_git_sha",
        # WS3 (epic #6669): upstream ref the numbers were measured against — same pin as the
        # per-product build banner above (omit-when-absent).
        "upstream_ref",
        "run_id",
        "node_count",
    ]
    banner = [f"{k}={prov[k]}" for k in banner_order if k in prov]
    # Fork-build source leg (WS4(c), epic #6669): mirrors the per-product build banner above —
    # appends "source=fork@<sha> (+N fixes over upstream@<base>)" only on a fork-build fire,
    # INERT (byte-unchanged) on every prebuilt-image run.
    fork_str = _fork_provenance_str(prov)
    if fork_str:
        banner.append(f"source={fork_str}")
    if banner:
        lines.append("_build: " + " · ".join(banner) + "_")
    gen = results.get("generated_at")
    if isinstance(gen, str) and _ISO.match(gen):
        lines.append(f"_generated-at: {gen}_")
    lines.append("")
    return "\n".join(lines)


# --- #4162 / hb#202: North-Star scorecard (render-derived; zero emit-key change) -----------
# The North Star is the bar in alex's spec doc: warm-pool-hit TTFE p95 < 1s ("Our North Star
# is < 1 second Time-To-First-Instruction"). This block prints the measured GAP to that target
# instead of leaving it implied. Separately, a 0.5s STRETCH bar (landed via hb#148) is kept as
# a visually-distinct, explicitly-labeled stretch row — it no longer wears the "North Star"
# label, resolving hb#202's SoT drift (the spec doc says <1s; 0.5s appears nowhere in it). Both
# are derived entirely from the already-emitted warm-hit ttfe_p95_ms — no new emit key, so the
# locked emitter⇄renderer schema contract is untouched. The matrix's 5s/1s throughput bars
# remain today's operating envelope.
NORTH_STAR_TTFE_P95_MS = 1000.0
STRETCH_TTFE_P95_MS = 500.0

# hb#5414 (Transition guards on trust surfaces, AGENTS.md #4420): a refresh that silently
# swings the North Star p95 by 2x+, or flips its ✅/❌ verdict against the bar, is exactly
# the "downgrades trust quietly" shape that doctrine forbids — it must reopen loudly (an
# in-page caveat) rather than render next to last run's number with no signal tying them
# together. Flagged in EITHER direction: a suspicious 2x *improvement* is as worth a second
# look as a regression (machine-class change, broken measurement, or a real fix all look
# the same as "the number moved a lot").
NORTH_STAR_DELTA_FACTOR = 2.0


def _p95_verdict(p95, bar_ms, p50, n):
    """Signed-margin verdict for a measured p95 against a bar.

    Renders ✅/❌ with the measured headroom/gap. hb#202 (flap-risk ask): when a MISS
    sits inside the sample's spread, append a `within N=<n> sampling noise` annotation so a
    2ms miss does not read as a hard fail on the next re-fire. The spread proxy is
    (p95−p50)/√n — distribution-free, uses only committed schema fields (no CI field exists),
    and by construction only ANNOTATES a miss, never converts ❌ into ✅.
    """
    if p95 < bar_ms:
        return f"✅ met ({_fmt_secs(bar_ms - p95)} headroom)"
    gap = p95 - bar_ms
    verdict = f"❌ not met ({_fmt_secs(gap)} above the bar)"
    if isinstance(p50, (int, float)) and isinstance(n, int) and n > 0 and p95 > p50:
        half_width = (p95 - p50) / math.sqrt(n)
        if gap < half_width:
            verdict += f" · within N={n} sampling noise"
    return verdict


def _north_star_rows(results, kata_results=None):
    """Per-runtime measured warm-hit p95 rows, sourced exactly like the matrix.

    Returns a list of (runtime_label, p95_or_None, p95_cell_or_None, p50_or_None,
    n_or_None, outcome_or_None). A runtime with no measured warm-hit p95 carries
    p95=None (renders `pending`). The scenario's own `outcome` is carried through
    (unchanged by the p95 read) so the caption can disclose a FAIL loudly — the p95
    is sourced REGARDLESS of outcome, so a FAILing run still emits a real p95 that
    would otherwise render as a clean green number (the #4420 trust-surface gap).
    """
    prov = _clean_provenance(results.get("provenance"))
    measured_runtime = prov.get("runtime") or "gvisor"
    sources = {measured_runtime: _matrix_scenarios(results.get("scenarios"))}
    if (
        isinstance(kata_results, dict)
        and kata_results.get("product") == "sandbox-kata"
        and "kata-microvm" not in sources
    ):
        kp = _clean_provenance(kata_results.get("provenance"))
        if kp.get("runtime") == "kata-microvm":
            sources["kata-microvm"] = _matrix_scenarios(kata_results.get("scenarios"))

    rows = []
    for rt in MATRIX_RUNTIMES:
        rt_scen = sources.get(rt)
        sc = rt_scen.get("warmpool_cold_start") if rt_scen is not None else None
        p95 = sc["metrics"].get("ttfe_p95_ms") if sc else None
        outcome = sc.get("outcome") if sc else None
        if p95 is None:
            rows.append((RUNTIME_LABELS[rt], None, None, None, None, outcome))
            continue
        p50 = sc["metrics"].get("ttfe_p50_ms")
        n_val = sc["n"] if sc["n"] > 0 else None
        p95_cell = _fmt_secs(p95)
        # hb#142: inline sample count, mirroring the matrix TTFE cells. `value (count=N)`,
        # with the low-N dagger AFTER the count when the row is below the comparability floor.
        if n_val is not None:
            p95_cell += f" (count={n_val})"
            if n_val < TTFE_COMPARABILITY_MIN_N:
                p95_cell += f" {_LOW_N_MARK}"
        rows.append((RUNTIME_LABELS[rt], p95, p95_cell, p50, n_val, outcome))
    return rows


def _north_star_delta_flag(
    label, current_p95, prior_p95, current_node_count=None, prior_node_count=None,
    current_node_image=None, prior_node_image=None,
    current_machine_type=None, prior_machine_type=None,
    current_controller_digest=None, prior_controller_digest=None,
    current_suite_git_sha=None, prior_suite_git_sha=None,
):
    """One flagged-runtime line, or None if this runtime has nothing to flag.

    Flags a >=NORTH_STAR_DELTA_FACTOR swing in EITHER direction, or a verdict flip
    against NORTH_STAR_TTFE_P95_MS (✅↔❌) — either condition alone is enough to flag,
    matching the "downgrade OR loses information" trigger in the trust-surface doctrine
    rather than requiring both.

    When the flagged delta ALSO spans a machine_type change (prior != current, both
    non-empty strings — build_provenance stamps prior_machine_type only when it differs,
    the same "only if it differs" gate as prior_node_image), a `· machine_type X→Y` clause
    is appended FIRST among the confound clauses. Machine-class is the primary confound —
    it is the whole reason _machine_class_caveat exists — so a reader must see it named on
    the delta line itself rather than left to guess which of the generic trailing candidates
    applies. Same ride-ON-a-flagged-delta rule as the two clauses below.

    When the flagged delta also spans a node_count change (prior != current, both ints),
    a `· node_count X→Y` clause is appended — the warm-pool capacity is a confound on the
    swing, so a reader can see that part of the delta may be a node-count artifact, not a
    pure substrate regression/fix. The clause rides ON a flagged delta only (it is never a
    flag on its own): a node_count change with no ttfe swing/flip is not a trust downgrade.

    When the flagged delta ALSO spans a node_image change (prior != current, both
    non-empty strings — the GKE kubeletVersion float on the unpinned RAPID channel), a
    `· node_image X→Y` clause is appended for the same reason: a kernel/kubelet build swap
    is a confound on the TTFE swing. Same ride-ON-a-flagged-delta rule (a node-image
    change with no ttfe swing/flip is not a trust downgrade), and all three confound
    clauses compose — machine_type, node_count, and node_image can appear on one flag.

    When the flagged delta ALSO spans a build-lineage change — a different
    controller_digest and/or suite_git_sha between the prior and current published run
    (#6828, the confound axis that cost the full #6762 investigation for the 1.36s→4.00s
    swing) — a `· controller_digest X→Y` and/or `· suite_git_sha X→Y` clause is appended
    last, same ride-ON-a-flagged-delta rule as the three clauses above (a rebuild with no
    ttfe swing/flip is not itself a trust downgrade). controller_digest is shown truncated
    to its first 19 chars (the `sha256:` prefix + 12 hex chars, mirroring render_trend's
    existing digest-display convention) since the full 64-hex-char digest would dominate
    the line; suite_git_sha is short enough to show in full. The two are independent —
    either, both, or neither can fire depending on which build components actually moved.
    """
    if not isinstance(current_p95, (int, float)) or not isinstance(prior_p95, (int, float)):
        return None
    if prior_p95 <= 0 or current_p95 <= 0:
        return None
    ratio = current_p95 / prior_p95
    big_delta = ratio >= NORTH_STAR_DELTA_FACTOR or ratio <= 1.0 / NORTH_STAR_DELTA_FACTOR
    prior_pass = prior_p95 < NORTH_STAR_TTFE_P95_MS
    current_pass = current_p95 < NORTH_STAR_TTFE_P95_MS
    flipped = prior_pass != current_pass
    if not big_delta and not flipped:
        return None
    direction = "regressed" if current_p95 > prior_p95 else "improved"
    flag = (
        f"**{label}** {direction} by {_fmt_secs(abs(current_p95 - prior_p95))} "
        f"({_fmt_secs(prior_p95)} → {_fmt_secs(current_p95)}, {ratio:.1f}x)"
    )
    if flipped:
        flag += " · verdict flip " + ("✅→❌" if prior_pass else "❌→✅")
    if (
        isinstance(current_machine_type, str)
        and isinstance(prior_machine_type, str)
        and current_machine_type.strip()
        and prior_machine_type.strip()
        and current_machine_type != prior_machine_type
    ):
        flag += f" · machine_type {prior_machine_type}→{current_machine_type}"
    if (
        isinstance(current_node_count, int)
        and not isinstance(current_node_count, bool)
        and isinstance(prior_node_count, int)
        and not isinstance(prior_node_count, bool)
        and current_node_count != prior_node_count
    ):
        flag += f" · node_count {prior_node_count}→{current_node_count}"
    if (
        isinstance(current_node_image, str)
        and isinstance(prior_node_image, str)
        and current_node_image.strip()
        and prior_node_image.strip()
        and current_node_image != prior_node_image
    ):
        flag += f" · node_image {prior_node_image}→{current_node_image}"
    if (
        isinstance(current_controller_digest, str)
        and isinstance(prior_controller_digest, str)
        and current_controller_digest.strip()
        and prior_controller_digest.strip()
        and current_controller_digest != prior_controller_digest
    ):
        flag += (
            f" · controller_digest `{prior_controller_digest[:19]}…` → "
            f"`{current_controller_digest[:19]}…`"
        )
    if (
        isinstance(current_suite_git_sha, str)
        and isinstance(prior_suite_git_sha, str)
        and current_suite_git_sha.strip()
        and prior_suite_git_sha.strip()
        and current_suite_git_sha != prior_suite_git_sha
    ):
        flag += f" · suite_git_sha `{prior_suite_git_sha}`→`{current_suite_git_sha}`"
    return flag


# One-off, run_id-scoped disposition addendum (honest-bench#636) for the gVisor
# warmpool_cold_start TTFE p95 swing published by hb#621 (1357.4ms -> 3995.5ms,
# run_id f71eac5dede24c11bf818053d3b5d0d8). hb#621's data commit predates #6828/#633's
# controller_digest/suite_git_sha auto-disclosure clause, so it never stamped
# prior_controller_digest/prior_suite_git_sha — the generic _north_star_delta_flag
# clause guard (both-sides-non-empty-string check) correctly falls through to the
# generic checklist text for this one data point, and the exact prior digest/sha are
# not honestly recoverable from sandbox/results/history.jsonl (it tracks an unrelated
# density-per-vcpu build metric, not per-scenario TTFE — see #636). Rather than
# fabricate the missing prior fields, this is a hand-written, run_id-scoped addendum —
# same "manual footnote for a fact the automated mechanism can't see" shape as hb#352's
# _REGIME_BOUNDARY_NOTE above, but deliberately CONDITIONAL, not permanent: it stops
# applying the instant the next refresh publishes a different run_id, since at that
# point the banner is comparing a different pair of runs entirely. The underlying swing
# IS already investigated to a conclusion — a controlled single-variable re-fire
# cleared node-count as a cause (holding the controller build identical across 1->2
# nodes did not inflate the warm tier; separation improved) and found build-lineage
# (fork@4c71c2cf vs. upstream) to be the confirmed, reproducible residual driver.
_HB621_SWING_RUN_ID = "f71eac5dede24c11bf818053d3b5d0d8"
_HB621_SWING_DISPOSITION = (
    " Disposition (honest-bench#636): node-count cleared as a cause — a "
    "controlled single-variable re-fire held the controller build byte-identical "
    "across 1→2 nodes and saw the warm-tier separation improve, not worsen; "
    "build-lineage (fork@4c71c2cf vs. upstream) is the confirmed, reproducible "
    "residual driver. The exact prior controller_digest/suite_git_sha for this data "
    "point are not recoverable from sandbox/results/history.jsonl (it tracks an "
    "unrelated build metric, not per-scenario TTFE), so this note stands in for the "
    "automated build-lineage clause on this one refresh."
)


def _hb621_swing_disposition_addendum(prov):
    """One-off addendum for the hb#621 swing (see _HB621_SWING_DISPOSITION above).

    Fires only while the CURRENT published run carries hb#621's run_id — the next
    refresh publishes a new run_id and this addendum silently stops applying on its
    own, no manual cleanup required.
    """
    if prov.get("run_id") == _HB621_SWING_RUN_ID:
        return _HB621_SWING_DISPOSITION
    return ""


def _north_star_delta_caveat(results, kata_results=None):
    """Refresh-over-refresh delta/verdict-flip caveat for the North Star cell (hb#5414).

    Reads the `prior_warmpool_ttfe_p95_ms` provenance field (stamped by
    harness/run.py's build_provenance, carried forward from the previously published
    run, same mechanism as _machine_class_caveat's prior_machine_type) and compares it
    to this run's measured p95 per runtime. Also reads the `prior_controller_digest` /
    `prior_suite_git_sha` fields (#6828, same "only if it differs" stamping gate) so a
    build-lineage confound self-disambiguates on-page alongside the existing
    machine_type/node_count/node_image confound clauses. Pure function of (results,
    kata_results); returns "" when nothing to flag so callers can unconditionally
    append it.
    """
    rows = _north_star_rows(results, kata_results)
    label_to_rt = {v: k for k, v in RUNTIME_LABELS.items()}

    prior_by_runtime = {}
    prior_nc_by_runtime = {}
    current_nc_by_runtime = {}
    prior_ni_by_runtime = {}
    current_ni_by_runtime = {}
    prior_mt_by_runtime = {}
    current_mt_by_runtime = {}
    prior_cd_by_runtime = {}
    current_cd_by_runtime = {}
    prior_sha_by_runtime = {}
    current_sha_by_runtime = {}
    prov = _clean_provenance(results.get("provenance"))
    measured_runtime = prov.get("runtime") or "gvisor"
    prior_p95 = prov.get("prior_warmpool_ttfe_p95_ms")
    if isinstance(prior_p95, (int, float)):
        prior_by_runtime[measured_runtime] = prior_p95
    if isinstance(prov.get("node_count"), int):
        current_nc_by_runtime[measured_runtime] = prov["node_count"]
    if isinstance(prov.get("prior_node_count"), int):
        prior_nc_by_runtime[measured_runtime] = prov["prior_node_count"]
    if isinstance(prov.get("node_image"), str):
        current_ni_by_runtime[measured_runtime] = prov["node_image"]
    if isinstance(prov.get("prior_node_image"), str):
        prior_ni_by_runtime[measured_runtime] = prov["prior_node_image"]
    if isinstance(prov.get("machine_type"), str):
        current_mt_by_runtime[measured_runtime] = prov["machine_type"]
    if isinstance(prov.get("prior_machine_type"), str):
        prior_mt_by_runtime[measured_runtime] = prov["prior_machine_type"]
    if isinstance(prov.get("controller_digest"), str):
        current_cd_by_runtime[measured_runtime] = prov["controller_digest"]
    if isinstance(prov.get("prior_controller_digest"), str):
        prior_cd_by_runtime[measured_runtime] = prov["prior_controller_digest"]
    if isinstance(prov.get("suite_git_sha"), str):
        current_sha_by_runtime[measured_runtime] = prov["suite_git_sha"]
    if isinstance(prov.get("prior_suite_git_sha"), str):
        prior_sha_by_runtime[measured_runtime] = prov["prior_suite_git_sha"]
    if isinstance(kata_results, dict):
        kp = _clean_provenance(kata_results.get("provenance"))
        if kp.get("runtime") == "kata-microvm":
            kata_prior = kp.get("prior_warmpool_ttfe_p95_ms")
            if isinstance(kata_prior, (int, float)):
                prior_by_runtime["kata-microvm"] = kata_prior
            if isinstance(kp.get("node_count"), int):
                current_nc_by_runtime["kata-microvm"] = kp["node_count"]
            if isinstance(kp.get("prior_node_count"), int):
                prior_nc_by_runtime["kata-microvm"] = kp["prior_node_count"]
            if isinstance(kp.get("node_image"), str):
                current_ni_by_runtime["kata-microvm"] = kp["node_image"]
            if isinstance(kp.get("prior_node_image"), str):
                prior_ni_by_runtime["kata-microvm"] = kp["prior_node_image"]
            if isinstance(kp.get("machine_type"), str):
                current_mt_by_runtime["kata-microvm"] = kp["machine_type"]
            if isinstance(kp.get("prior_machine_type"), str):
                prior_mt_by_runtime["kata-microvm"] = kp["prior_machine_type"]
            if isinstance(kp.get("controller_digest"), str):
                current_cd_by_runtime["kata-microvm"] = kp["controller_digest"]
            if isinstance(kp.get("prior_controller_digest"), str):
                prior_cd_by_runtime["kata-microvm"] = kp["prior_controller_digest"]
            if isinstance(kp.get("suite_git_sha"), str):
                current_sha_by_runtime["kata-microvm"] = kp["suite_git_sha"]
            if isinstance(kp.get("prior_suite_git_sha"), str):
                prior_sha_by_runtime["kata-microvm"] = kp["prior_suite_git_sha"]

    flags = []
    for label, p95, _cell, _p50, _n, _outcome in rows:
        if p95 is None:
            continue
        rt = label_to_rt.get(label)
        prior = prior_by_runtime.get(rt)
        if prior is None:
            continue
        flag = _north_star_delta_flag(
            label, p95, prior,
            current_node_count=current_nc_by_runtime.get(rt),
            prior_node_count=prior_nc_by_runtime.get(rt),
            current_node_image=current_ni_by_runtime.get(rt),
            prior_node_image=prior_ni_by_runtime.get(rt),
            current_machine_type=current_mt_by_runtime.get(rt),
            prior_machine_type=prior_mt_by_runtime.get(rt),
            current_controller_digest=current_cd_by_runtime.get(rt),
            prior_controller_digest=prior_cd_by_runtime.get(rt),
            current_suite_git_sha=current_sha_by_runtime.get(rt),
            prior_suite_git_sha=prior_sha_by_runtime.get(rt),
        )
        if flag:
            flags.append(flag)

    if not flags:
        return ""
    return (
        "> ⚠️ **Refresh delta:** " + "; ".join(flags) + ". A swing this large, or a bar-crossing "
        "flip, between consecutive published runs is flagged for a second look before trusting it "
        "as a substrate signal — check for a machine-class change, a node-count change, a "
        "node-image change, a build-lineage change (controller/suite rebuild), a broken "
        "measurement, or a real regression/fix." + _hb621_swing_disposition_addendum(prov)
    )


# One-time, hand-written historical footnote (hb#352) — NOT a computed caveat like
# _machine_class_caveat/_north_star_delta_caveat above, because the fact it documents predates
# both provenance fields those read: the page's very first published warmpool_cold_start numbers
# (bind_p95 678ms / ttfe_p95 922ms, dated 2026-07-04) were measured on a long-lived, pre-warmed
# internal GKE cluster — NOT by this repo's own CI. The `hb-refresh-gke-sandbox` Cloud Build
# trigger's first-ever automated fire was 2026-07-20, and by design (see the cloudbuild config's
# "Honest by construction" note) every fire since provisions a brand-new, single-node ephemeral
# cluster with an empty containerd cache — a genuine cold pull. Neither prior_warmpool_ttfe_p95_ms
# nor prior_machine_type existed before that first automated fire, so neither delta-tripwire can
# see across this boundary; a reader diffing today's ~3s cold numbers against an earlier ~700-900ms
# citation of this page is comparing two different measurement regimes, not seeing a regression.
# Static and permanent: this is a fact about the page's history, not a live signal that clears on
# its own — unlike the two caveats above, there is nothing to re-check on a future run.
_REGIME_BOUNDARY_NOTE = (
    "> ℹ️ **Regime note:** every CI-measured refresh since **2026-07-20** measures a brand-new, "
    "single-node ephemeral CI cluster with an empty containerd cache per run — a deliberately "
    "cold pull (see \"Reproduce it\" below). Numbers published **before 2026-07-20** (e.g. the "
    "2026-07-04 baseline) were instead measured on a long-lived, pre-warmed internal cluster, not "
    "by this repo's own CI. If you're comparing today's cold-start figures against an older "
    "citation of this page and see a large jump, that's this regime switch — not a code or "
    "controller regression. (\"CI-measured\" means *machine-measured on a cold ephemeral "
    "cluster*, **not** *scheduled* — see the refresh cadence below.)"
)

# hb#511: refresh cadence is on-demand, not a recurring cron. Declaring a freshness horizon
# keeps "is this stale?" decidable without standing up recurring billed infra.
_REFRESH_CADENCE_NOTE = (
    "> ℹ️ **Refresh cadence (on-demand, not scheduled):** refreshes are **manually invoked / "
    "on-demand** — a hand-run CI fire (`gcloud builds triggers run` / the reproduce script "
    "below), never a recurring cron. To keep \"is this stale?\" a decidable question without "
    "standing up recurring billed infra, a refresh is **due** when either (a) a **regime "
    "boundary** occurs — cluster recreate, node-image float, or controller-build digest change "
    "(all caught by the sandbox accrual detectors) — or (b) a **30-day floor** elapses since the "
    "last fire (the `_generated-at:_` stamp under the Core Metrics table). Between those, the "
    "published numbers are current, not stale."
)


def _north_star_fail_caveat(rows):
    """Loud disclosure when a runtime's warmpool_cold_start scenario OUTCOME is FAIL.

    The North Star p95 is sourced from the warmpool_cold_start scenario REGARDLESS of that
    scenario's own outcome — a FAILing run still emits a real p95 that _north_star_rows reads
    and the caption grades against the bar and the refresh-delta tripwire carries forward as
    the baseline. Per the trust-surface doctrine (#4420: a downgrade must reopen loudly, never
    render silently), a FAIL outcome on the very scenario whose p95 the caption adopts must be
    disclosed — the bar grade (❌/✅) and the scenario's own outcome are INDEPENDENT signals, so
    a p95 that happens to clear the bar would otherwise render a green ✅ while its own run was
    marked FAIL. Mirrors render_cluster_saturation's own outcome=FAIL headline caveat. Returns
    "" when no runtime FAILs so the caller can unconditionally append it.
    """
    failed = [
        label
        for label, p95, _cell, _p50, _n, outcome in rows
        if outcome == "FAIL" and p95 is not None
    ]
    if not failed:
        return ""
    who = ", ".join(f"**{lbl}**" for lbl in failed)
    return (
        "> ⚠️ **Scenario FAIL:** the warm-pool-hit scenario's own outcome is **FAIL** for "
        f"{who} — the p95 above is a real measurement that MISSED its SLA, not a passing "
        "warm hit. It is still graded against the bar and carried forward as the refresh "
        "baseline honestly (an SLA-failing number is disclosed, never softened into a green "
        "cell); a later refresh whose scenario returns to PASS clears this."
    )


def _raw_sla_p95(scenarios, name, key):
    """Read one sla_metrics value straight from the RAW scenario list, by scenario name.

    bind_p95_ms is deliberately NOT in MATRIX_METRIC_FIELDS — it never becomes a public matrix
    cell — so _matrix_scenarios strips it. The inversion tripwire's bind leg needs it, so it is
    read here from the raw emit: disclosure-only, no new published cell, no schema-contract change.
    Returns None on any missing / non-numeric / bool shape (so a caller treats it as absent).
    """
    if not isinstance(scenarios, list):
        return None
    for s in scenarios:
        if isinstance(s, dict) and s.get("name") == name:
            m = s.get("sla_metrics")
            if isinstance(m, dict):
                v = m.get(key)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    return v
            return None
    return None


def _warm_cold_inversion_caveat(results, kata_results=None):
    """Loud disclosure when a runtime's warm-pool-hit path is slower than its own cold-start path.

    Warm-pool activation (warmpool_cold_start) is meant to be the FAST path — a pre-warmed
    pool hands off an already-running sandbox, so its latency must sit at or below the
    unique-image cold-start (native_digest_cold), which pays the full image pull + boot.
    When the published matrix shows warm > cold, the trust surface is rendering a backwards
    number: a reader ranking the warm and cold rows top-to-bottom would conclude warm is the
    slow path. Per the trust-surface doctrine (#4420: a surface must fail loud while degraded,
    never render the bad value silently), that inversion is disclosed as a standing caveat
    rather than left to read as a clean pair of green cells.

    TWO legs are checked, because TTFE ≈ bind_leg + exec_leg and the two localize the fault:
      - **bind** (bind_p95_ms) is the mechanistically-primary tell for warm-pool under-delivery.
        A genuine warm hit binds an already-running pod ~instantly, so a warm bind p95 at/above
        the cold bind p95 means the "warm" population is blended with genuinely-cold claims (the
        #379 signature — the issue is literally named `gVisor bind_p95 ≈ cold bind_p95`). bind is
        read from the RAW emit (it is stripped from the public matrix), so this adds no cell.
      - **TTFE** (ttfe_p95_ms) is total latency — the downstream shadow of the same contamination.
        A TTFE-only inversion whose bind leg is clean points instead at the exec phase, not the pool.
    Whichever leg(s) are inverted are named; the breakdown lets a reader localize the fault
    without the tripwire asserting one.

    CAUSE-AGNOSTIC by design: it asserts no mechanism (a warm fire not gating on pool-Ready
    before probing, a silent image-pull on the warm hit, or a genuine tail regression are all
    candidates — pinning the cause drives the FIX and is a separate lane). It only reports that
    the live numbers are inverted. It is a guard-then-fill tripwire: it fires ONLY on live
    inverted data and AUTO-CLEARS the instant a refresh returns warm below cold on every leg.

    Gated on BOTH rows having N >= TTFE_COMPARABILITY_MIN_N so it never fires on sampling
    noise — a low-N cold row can read faster than a high-N warm row purely from a lucky draw
    (exactly the cross-row inversion TTFE_COMPARABILITY_MIN_N exists to mark), which is NOT a
    real warm>cold condition. pending rows are skipped (no real measurement). Returns "" when
    no runtime/leg is inverted so the caller can unconditionally append it. Mirrors
    _north_star_fail_caveat's shape (pure function; loud "> ⚠️" block; auto-clearing).
    """
    prov = _clean_provenance(results.get("provenance"))
    measured_runtime = prov.get("runtime") or "gvisor"
    sources = {measured_runtime: _matrix_scenarios(results.get("scenarios"))}
    raw_sources = {measured_runtime: results.get("scenarios")}
    if (
        isinstance(kata_results, dict)
        and kata_results.get("product") == "sandbox-kata"
        and "kata-microvm" not in sources
    ):
        kp = _clean_provenance(kata_results.get("provenance"))
        if kp.get("runtime") == "kata-microvm":
            sources["kata-microvm"] = _matrix_scenarios(kata_results.get("scenarios"))
            raw_sources["kata-microvm"] = kata_results.get("scenarios")

    inverted = []
    for rt in MATRIX_RUNTIMES:
        rt_scen = sources.get(rt)
        if rt_scen is None:
            continue
        warm = rt_scen.get("warmpool_cold_start")
        cold = rt_scen.get("native_digest_cold")
        if not warm or not cold:
            continue
        warm_n = warm.get("n")
        cold_n = cold.get("n")
        if not isinstance(warm_n, int) or isinstance(warm_n, bool):
            continue
        if not isinstance(cold_n, int) or isinstance(cold_n, bool):
            continue
        if warm_n < TTFE_COMPARABILITY_MIN_N or cold_n < TTFE_COMPARABILITY_MIN_N:
            continue
        # A pending row carries no real measurement (its matrix metrics are suppressed to {});
        # never read an inversion off it.
        if warm.get("outcome") == "pending" or cold.get("outcome") == "pending":
            continue

        legs = []  # (leg_label, warm_val, cold_val) for each inverted leg
        # bind leg — from the RAW emit (stripped from the matrix); primary contamination tell.
        raw = raw_sources.get(rt)
        warm_bind = _raw_sla_p95(raw, "warmpool_cold_start", "bind_p95_ms")
        cold_bind = _raw_sla_p95(raw, "native_digest_cold", "bind_p95_ms")
        if warm_bind is not None and cold_bind is not None and warm_bind > cold_bind:
            legs.append(("bind", warm_bind, cold_bind))
        # TTFE leg — from the cleaned matrix metrics; downstream total-latency shadow.
        warm_ttfe = warm["metrics"].get("ttfe_p95_ms")
        cold_ttfe = cold["metrics"].get("ttfe_p95_ms")
        warm_ttfe_ok = isinstance(warm_ttfe, (int, float)) and not isinstance(warm_ttfe, bool)
        cold_ttfe_ok = isinstance(cold_ttfe, (int, float)) and not isinstance(cold_ttfe, bool)
        if warm_ttfe_ok and cold_ttfe_ok and warm_ttfe > cold_ttfe:
            legs.append(("TTFE", warm_ttfe, cold_ttfe))

        if legs:
            inverted.append((RUNTIME_LABELS[rt], warm_n, cold_n, legs))
    if not inverted:
        return ""
    who = "; ".join(
        f"**{lbl}** (warm count={wn}, cold count={cn}): "
        + ", ".join(
            f"{leg} warm {_fmt_secs(w)} > cold {_fmt_secs(c)}" for leg, w, c in legs
        )
        for lbl, wn, cn, legs in inverted
    )
    return (
        "> ⚠️ **Warm-slower-than-cold:** the warm-pool-hit path is SLOWER than the unique-image "
        f"cold-start path for {who} — a backwards result (warm is meant to be the fast path). "
        f"Both rows clear the N={TTFE_COMPARABILITY_MIN_N} comparability floor, so this is not a "
        "small-sample inversion. The per-leg breakdown localizes it: the **bind** leg (pod-bind "
        "latency) is where warm-pool under-delivery shows up — a real warm hit binds an "
        "already-running pod ~instantly, so an inverted bind p95 means the warm population is "
        "blended with genuinely-cold claims; the **TTFE** leg is total latency (bind + exec), so "
        "a TTFE-only inversion with a clean bind leg points instead at the exec phase, not the "
        "pool. The cause is not asserted here (candidates: the warm fire not gating on pool-Ready "
        "before probing, a silent image-pull on the warm hit, or a real tail regression); a later "
        "refresh whose warm legs return below cold clears this."
    )


def _warmpool_separation_caveat(results, kata_results=None):
    """Loud disclosure when the warm-pool separation ratio is below its gate.

    warmpool_cold_start emits warmpool_gate_separation_ratio = cold_min / warm_max — the factor
    by which the slowest warm-pool hit beats the fastest unique-image cold claim. It is the
    CANONICAL warm-pool trust metric: a real pre-warmed pool hands off already-running sandboxes,
    so its slowest hit should still clear the fastest cold start by a wide margin. The gate target
    is WARMPOOL_SEPARATION_MIN_RATIO (1.8x). A ratio at ~1x means the warm and cold populations
    overlap — "warm" and "cold" are statistically indistinguishable, so the published warm tier is
    not demonstrably a fast path at all.

    This is the QUANTITATIVE companion to _warm_cold_inversion_caveat: that caveat fires only when
    warm p95 STRICTLY exceeds cold p95 (a backwards central tendency); this one fires on the
    weaker-but-still-broken condition that the two distributions do not cleanly separate, which
    holds even when warm p95 sits just below cold p95. Per #4420 the ratio is disclosed rather
    than left un-rendered while it fails its gate.

    Read from the RAW emit (the three warmpool_gate_* keys are not public matrix cells), so it
    adds no cell and no schema-contract change. Cause-agnostic (asserts no mechanism), gated on
    the warm row N >= TTFE_COMPARABILITY_MIN_N so it never fires on sampling noise, skips pending
    rows, and AUTO-CLEARS the instant a refresh returns the ratio at/above the gate. Returns ""
    when every runtime separates cleanly (or the metric is absent) so the caller can append it
    unconditionally. Mirrors _warm_cold_inversion_caveat's shape.
    """
    prov = _clean_provenance(results.get("provenance"))
    measured_runtime = prov.get("runtime") or "gvisor"
    sources = {measured_runtime: _matrix_scenarios(results.get("scenarios"))}
    raw_sources = {measured_runtime: results.get("scenarios")}
    if (
        isinstance(kata_results, dict)
        and kata_results.get("product") == "sandbox-kata"
        and "kata-microvm" not in sources
    ):
        kp = _clean_provenance(kata_results.get("provenance"))
        if kp.get("runtime") == "kata-microvm":
            sources["kata-microvm"] = _matrix_scenarios(kata_results.get("scenarios"))
            raw_sources["kata-microvm"] = kata_results.get("scenarios")

    failing = []  # (label, warm_n, ratio, warm_max_ms, cold_min_ms)
    for rt in MATRIX_RUNTIMES:
        rt_scen = sources.get(rt)
        if rt_scen is None:
            continue
        warm = rt_scen.get("warmpool_cold_start")
        if not warm:
            continue
        warm_n = warm.get("n")
        if not isinstance(warm_n, int) or isinstance(warm_n, bool):
            continue
        if warm_n < TTFE_COMPARABILITY_MIN_N:
            continue
        if warm.get("outcome") == "pending":
            continue
        raw = raw_sources.get(rt)
        ratio = _raw_sla_p95(raw, "warmpool_cold_start", "warmpool_gate_separation_ratio")
        if ratio is None or ratio >= WARMPOOL_SEPARATION_MIN_RATIO:
            continue
        warm_max = _raw_sla_p95(raw, "warmpool_cold_start", "warmpool_gate_warm_max_ms")
        cold_min = _raw_sla_p95(raw, "warmpool_cold_start", "warmpool_gate_cold_min_ms")
        min_ready = _raw_sla_p95(raw, "warmpool_cold_start", "warmpool_gate_min_ready_during_burst")
        failing.append((RUNTIME_LABELS[rt], warm_n, ratio, warm_max, cold_min, min_ready))
    if not failing:
        return ""

    def _bounds(warm_max, cold_min):
        if warm_max is None or cold_min is None:
            return ""
        # hb#537: warmpool_gate_warm_max_ms/cold_min_ms are BIND latencies
        # (create->bound, harness/scenarios/warmpool_cold_start.py's `latencies`
        # dict feeding _classify_latencies) -- a strictly smaller quantity than
        # the TTFE (bind+exec) figures published elsewhere on the page. Left
        # unlabeled, "slowest warm {bind}" reads as directly comparable to
        # "warm TTFE p95" and a bind-max below the TTFE p95 looks impossible; it
        # is not, once both legs are named for what they measure.
        return (
            f" (slowest warm bind {_fmt_secs(warm_max)} vs "
            f"fastest cold bind {_fmt_secs(cold_min)})"
        )

    who = "; ".join(
        f"**{lbl}** (warm count={wn}): {ratio:.3g}x{_bounds(wmax, cmin)}"
        for lbl, wn, ratio, wmax, cmin, _min_ready in failing
    )
    # hb#588: warmpool_gate_min_ready_during_burst (hb#379) directly evidences the drain
    # mechanism when present -- readyReplicas dropping below the configured pool target
    # during the claim burst means remaining warm-tier binds queue behind an
    # under-supplied pool. This supersedes the pre-hb#450 "blending genuinely-cold claims
    # into the warm tier" guess: hb#450's provenance gate already excludes blends from the
    # warm_n counted here, so contamination is not an available explanation for any row
    # this caveat fires on. Older cells that predate the min_ready emit fall back to a
    # cause description that no longer names the refuted blend hypothesis.
    drained = [
        (lbl, min_ready) for lbl, _wn, _ratio, _wmax, _cmin, min_ready in failing
        if isinstance(min_ready, (int, float))
    ]
    if drained:
        evidence = "; ".join(f"**{lbl}** min readyReplicas={mr:g} during the burst" for lbl, mr in drained)
        cause = (
            "The cause is a supply-constrained pool draining under load, not cold-claim "
            f"contamination (hb#450's provenance gate already excludes blends from the counted "
            f"warm hits): {evidence} — remaining warm-tier binds queue behind the drain rather "
            "than being served pre-warmed."
        )
    else:
        cause = (
            "The cause is not asserted here (the pool may be under-delivering ready replicas "
            "relative to its configured target during the burst; this is not cold-claim "
            "contamination, since hb#450's provenance gate already excludes blends from the "
            "counted warm hits)."
        )
    return (
        "> ⚠️ **Warm/cold separation below gate:** the warm-pool separation ratio "
        "(fastest cold start ÷ slowest warm-pool hit) is below the "
        f"{WARMPOOL_SEPARATION_MIN_RATIO:g}x gate for {who} — at ~1x the warm and cold "
        "populations overlap, so the published warm tier is not demonstrably faster than a "
        f"unique-image cold start. The warm row clears the N={TTFE_COMPARABILITY_MIN_N} floor, so "
        f"this is not a small-sample artifact. {cause} A later refresh whose ratio returns to "
        "the gate clears this."
    )


def _warmpool_separation_variance_caveat(history_rows):
    """Loud disclosure when the SAME controller build measures wildly different separation ratios.

    _warmpool_separation_caveat above answers "is THIS fire's ratio bad?" from the single
    live-results snapshot. This is a different question that a single snapshot cannot even ask:
    "does the SAME build (same controller_digest) measure CONSISTENTLY across independent
    fires?" The #6890 Fire A/B/C suspect-check found the SAME digest
    (sha256:f511a1ab3350...) fired twice produced 0.27x then 1.06x -- a 3.9x spread on
    byte-identical build inputs. That variance is invisible on the public page because
    results/latest.json is a single snapshot overwritten every fire; the append-only
    warmpool-separation-history.jsonl store (schema.WARMPOOL_SEPARATION_HISTORY_FIELDS,
    render.accrue_warmpool_separation) exists precisely to keep every fire's measurement so this
    check can see it.

    Groups clean history rows by controller_digest; for any digest with 2+ measurements, flags it
    when max/min separation_ratio >= WARMPOOL_SEPARATION_VARIANCE_MIN_SPREAD (2.0x). This is
    orthogonal to the gate check above -- a build can pass the gate on every measurement and still
    trip this (instability in the MEASUREMENT is the thing being disclosed, not a bad ratio), and
    a flagged digest need not be the one currently live in latest.json (a prior build's variance
    stays disclosed here for as long as its history rows persist, since a resolved-then-reverted
    regression is still a fact about that build). Returns "" when no digest exceeds the spread (or
    fewer than 2 digests have 2+ measurements at all) so the caller can append it unconditionally.
    """
    rows = _clean_warmpool_separation_history(history_rows)
    by_digest = {}
    for r in rows:
        by_digest.setdefault(r["controller_digest"], []).append(r)

    flagged = []  # (digest, n_measurements, min_ratio, max_ratio, spread)
    for digest, digest_rows in by_digest.items():
        if len(digest_rows) < 2:
            continue
        ratios = [r["separation_ratio"] for r in digest_rows]
        lo, hi = min(ratios), max(ratios)
        if lo <= 0:
            continue
        spread = hi / lo
        if spread < WARMPOOL_SEPARATION_VARIANCE_MIN_SPREAD:
            continue
        flagged.append((digest, len(digest_rows), lo, hi, spread))
    if not flagged:
        return ""

    flagged.sort(key=lambda t: t[4], reverse=True)
    who = "; ".join(
        f"`{digest[:19]}…` ({n} measurements): {lo:.3g}x – {hi:.3g}x ({spread:.3g}x spread)"
        for digest, n, lo, hi, spread in flagged
    )
    return (
        "> ⚠️ **Same-build separation-ratio variance:** the warm-pool separation ratio measured "
        f"on the SAME controller build swings by {WARMPOOL_SEPARATION_VARIANCE_MIN_SPREAD:g}x or "
        f"more across independent fires for {who} — a byte-identical build should measure "
        "consistently, so this large a swing points at instability in the measurement (pool "
        "warm-up timing, node contention, or similar), not the build itself. The cause is not "
        "asserted here. A single-fire snapshot cannot show this: it is visible only because every "
        "fire's ratio is retained in warmpool-separation-history.jsonl rather than the latest "
        "fire overwriting the prior one. This entry persists for as long as the flagged digest's "
        "history rows do, even after a later build supersedes it in latest.json."
    )


def _warmpool_separation_verdict_caveat(results, history_rows, kata_results=None):
    """Loud disclosure when a single-fire separation PASS/FAIL is NOT statistically defensible.

    _warmpool_separation_caveat above renders a single-fire PASS/FAIL against the fixed 1.8x
    gate directly from this snapshot's ratio. _warmpool_separation_variance_caveat discloses that
    the same build measures inconsistently. This caveat is the VERDICT layer that reconciles the
    two: it takes the live single-fire ratio AND the historical noise floor and REFUSES to issue a
    pass/fail when the noise band is wider than the ratio's margin to the gate — i.e. when a single
    fire cannot tell a real regression from an unlucky draw. That refusal is the trust-surface
    output; the arithmetic lives in warmpool_verdict.variance_aware_verdict and the full protocol
    in WARMPOOL_SEPARATION_VERDICT_PROTOCOL.md.

    All statistics are in log-space (multiplicative ratio noise → additive/symmetric). The noise
    floor sigma is the pooled within-digest stdev of ln(separation_ratio) over the accrued history;
    the verdict is INDETERMINATE (refused) when the confidence interval straddles the gate, or when
    no same-build replication exists to estimate the noise floor at all. INDETERMINATE is
    fail-closed by construction: with no defensible verdict the surface withholds one loudly rather
    than silently emitting the raw single-fire pass/fail this layer exists to distrust.

    Mirrors _warmpool_separation_caveat's runtime iteration to obtain the live ratio (same N floor,
    same pending skip, same raw-emit read). Consumes the CLEANED history rows so the verdict module
    sees only validated (controller_digest, positive separation_ratio) pairs. Returns "" when every
    live ratio resolves to a defensible PASS/FAIL (or there is no live ratio to judge) so the caller
    can append it unconditionally.
    """
    prov = _clean_provenance(results.get("provenance"))
    measured_runtime = prov.get("runtime") or "gvisor"
    raw_sources = {measured_runtime: results.get("scenarios")}
    matrix_sources = {measured_runtime: _matrix_scenarios(results.get("scenarios"))}
    if (
        isinstance(kata_results, dict)
        and kata_results.get("product") == "sandbox-kata"
    ):
        kp = _clean_provenance(kata_results.get("provenance"))
        if kp.get("runtime") == "kata-microvm":
            raw_sources["kata-microvm"] = kata_results.get("scenarios")
            matrix_sources["kata-microvm"] = _matrix_scenarios(kata_results.get("scenarios"))

    clean_history = _clean_warmpool_separation_history(history_rows or [])

    refused = []  # (label, ratio, verdict_dict)
    for rt in MATRIX_RUNTIMES:
        rt_scen = matrix_sources.get(rt)
        if rt_scen is None:
            continue
        warm = rt_scen.get("warmpool_cold_start")
        if not warm:
            continue
        warm_n = warm.get("n")
        if not isinstance(warm_n, int) or isinstance(warm_n, bool):
            continue
        if warm_n < TTFE_COMPARABILITY_MIN_N:
            continue
        if warm.get("outcome") == "pending":
            continue
        ratio = _raw_sla_p95(
            raw_sources.get(rt), "warmpool_cold_start", "warmpool_gate_separation_ratio"
        )
        if ratio is None or ratio <= 0:
            continue
        verdict = warmpool_verdict.variance_aware_verdict(
            [ratio], clean_history, threshold=WARMPOOL_SEPARATION_MIN_RATIO
        )
        if verdict["verdict"] == warmpool_verdict.INDETERMINATE:
            refused.append((RUNTIME_LABELS[rt], ratio, verdict))
    if not refused:
        return ""

    def _one(lbl, ratio, v):
        if v["reason"] == "indeterminate-no-noise-floor":
            return (
                f"**{lbl}** — one fire measured {ratio:.3g}x, but the accrued history has no "
                "same-build replication (no controller build with 2+ measurements), so the "
                "measurement noise floor cannot be estimated and no single-fire pass/fail is "
                "defensible."
            )
        return (
            f"**{lbl}** — one fire measured {ratio:.3g}x; at the measured noise floor "
            f"(σ(log)={v['sigma_log']:.2g}, {int(v['confidence'] * 100)}% band "
            f"{v['ci_low']:.3g}x–{v['ci_high']:.3g}x) the interval straddles the "
            f"{WARMPOOL_SEPARATION_MIN_RATIO:g}x gate, so this single fire cannot tell a real "
            f"pass from an unlucky draw — {v['n_required']} consistent fires would resolve this "
            "margin."
        )

    who = " ".join(_one(lbl, ratio, v) for lbl, ratio, v in refused)
    return (
        "> ⚠️ **Single-fire separation verdict withheld:** the raw gate issues a pass/fail from "
        "ONE fire's separation ratio, but reconciling that ratio against the run-to-run noise "
        "floor measured across the accrued same-build history shows the noise band is wider than "
        f"the ratio's margin to the {WARMPOOL_SEPARATION_MIN_RATIO:g}x gate, so no single-fire "
        f"verdict is defensible: {who} The verdict layer refuses to issue one and states the fires "
        "required instead (fail-closed: it withholds the pass/fail rather than emitting the raw "
        "single-fire one it cannot defend). See "
        "[WARMPOOL_SEPARATION_VERDICT_PROTOCOL.md](WARMPOOL_SEPARATION_VERDICT_PROTOCOL.md). A "
        "refresh with enough consistent fires to clear the noise band resolves this."
    )


def render_north_star_caption(results, kata_results=None):
    """One-line measured-verdict captions for the <1s North Star + 0.5s stretch bar.

    hb#227 (GOAL-2.1, keep/drop DROP-2): the former full-table "How close to the North Star?"
    scorecard + "Stretch bar" section fold to two compact caption lines placed directly under
    the Core Metrics matrix. The measured per-runtime verdicts are PRESERVED — same
    `_north_star_rows` source + `_p95_verdict` grading (headroom/gap, within-sampling-noise tag,
    low-N dagger, `pending` for unmeasured) as the retired scorecard — just rendered inline
    instead of as two standalone H3 tables. The bar is the spec doc's <1s warm-pool-hit TTFE
    p95; the 0.5s stretch stays an explicitly-labeled aspiration, not the North Star. Derived
    entirely from the already-emitted warm-hit ttfe_p95_ms — no new emit key (the locked
    emitter⇄renderer schema contract is untouched).
    """
    product = results.get("product")
    if product not in PRODUCTS:
        return ""
    rows = _north_star_rows(results, kata_results)
    ns_bar = _fmt_secs(NORTH_STAR_TTFE_P95_MS)
    stretch_bar = _fmt_secs(STRETCH_TTFE_P95_MS)

    def _entries(bar_ms):
        parts = []
        for label, p95, p95_cell, p50, n_val, outcome in rows:
            if p95 is None:
                parts.append(f"{label} {link_pending(_PENDING)}")
            else:
                entry = f"{label} {p95_cell} {_p95_verdict(p95, bar_ms, p50, n_val)}"
                # #4420: a FAIL outcome on the sourced scenario overrides the bar grade —
                # never let a p95 that clears the bar render a silent green ✅ while its own
                # run was marked FAIL. The full disclosure rides the caveat block below.
                if outcome == "FAIL":
                    entry += " ⚠️ **scenario FAIL**"
                parts.append(entry)
        return "; ".join(parts)

    north_star = (
        f"_**North Star** — warm-pool-hit TTFE p95 < {ns_bar} (the spec doc bar): "
        f"{_entries(NORTH_STAR_TTFE_P95_MS)}. An honest ❌ prints the measured gap to the bar "
        "(tagged `within sampling noise` when the miss sits inside the sample spread — it stays "
        "a ❌, the tag never flips a miss to a pass); `pending` = unmeasured (never a guess); "
        f"{_LOW_N_MARK} marks a p95 over fewer than N={TTFE_COMPARABILITY_MIN_N} samples._"
    )
    stretch = (
        f"_**Stretch bar** — warm-pool-hit TTFE p95 < {stretch_bar} (an aspiration above the "
        "North Star, not the North Star itself; the step-up curve grades sustained creation-rate "
        f"against it — see [DETAILS.md](DETAILS.md)): {_entries(STRETCH_TTFE_P95_MS)}._"
    )
    # WS1 known-anomalies consolidation (epic #6669): the FAIL / inversion / separation /
    # regime / cadence blocks formerly appended here unconditionally now live in the
    # "Known anomalies" table (render_known_anomalies_table) + DETAILS.md
    # (render_known_anomalies_detail) — see those functions for the live-marker/link
    # scheme. `caveat` (machine-class) and `delta_caveat` (refresh-over-refresh regression)
    # are NOT part of that consolidation and stay wired here unchanged.
    caveat = _machine_class_caveat(_clean_provenance(results.get("provenance")))
    delta_caveat = _north_star_delta_caveat(results, kata_results)
    out = north_star + "\n\n" + stretch
    if caveat:
        out += "\n\n" + caveat
    if delta_caveat:
        out += "\n\n" + delta_caveat
    return out


def render_known_anomalies_table(results, kata_results=None, history_rows=None):
    """Compact "is anything currently wrong?" table (epic #6669 WS1) replacing the 6
    standalone banner blockquotes that used to render inline under the North Star caption.

    Each row's Status cell is a live marker + link into DETAILS.md's `## Known anomalies`
    section — the marker degrades loudly (`⚠️ ACTIVE`) the instant its underlying condition
    trips and reverts to `✅ clear` the instant it doesn't (#4420: never a silent downgrade,
    never a stale "still looks fine" cell). A clear row still links, so the reader can see
    exactly what the check covers even when nothing is currently wrong.

    Regime note / Refresh cadence are always-standing context (not conditional checks), so
    they render an unconditional `ℹ️ standing note` marker rather than active/clear.

    The optional Concurrent-burst row is wayfinding-only, never summarized into a verdict
    here: that disclosure can fire 0, 1, or N times per refresh (once per date/regime group
    inside render_cluster_scale), so folding it into one Status cell would either hide
    multi-group detail or fabricate a single answer. It only appears when the harness
    emitted a closed-schema-clean top-level `concurrent_burst` object.

    `history_rows` (raw, pre-validation rows from warmpool-separation-history.jsonl; None or []
    are both treated as "no history available") feeds the Same-build separation variance row —
    see _warmpool_separation_variance_caveat. Unlike the other rows this one is cross-fire
    (spans every fire's history, not just this snapshot's `results`), so an absent/empty history
    renders a clear cell rather than omitting the row — the row always names the check.
    """
    product = results.get("product")
    if product not in PRODUCTS:
        return ""
    rows = _north_star_rows(results, kata_results)
    fail_active = bool(_north_star_fail_caveat(rows))
    inversion_active = bool(_warm_cold_inversion_caveat(results, kata_results))
    separation_active = bool(_warmpool_separation_caveat(results, kata_results))
    mixed_rig_active = bool(_mixed_rig_confound_caveat(results))
    variance_active = bool(_warmpool_separation_variance_caveat(history_rows or []))
    verdict_active = bool(
        _warmpool_separation_verdict_caveat(results, history_rows or [], kata_results)
    )

    def _cell(active, anchor):
        marker = "⚠️ ACTIVE" if active else "✅ clear"
        return f"[{marker}](DETAILS.md#{anchor})"

    lines = [
        "### Known anomalies",
        "",
        "| Anomaly | Status |",
        "|---|---|",
        f"| Scenario FAIL | {_cell(fail_active, 'scenario-fail')} |",
        f"| Warm-slower-than-cold | {_cell(inversion_active, 'warm-slower-than-cold')} |",
        "| Warm-cold separation below gate | "
        f"{_cell(separation_active, 'warm-cold-separation-below-gate')} |",
        "| Same-build separation-ratio variance | "
        f"{_cell(variance_active, 'same-build-separation-ratio-variance')} |",
        "| Single-fire separation verdict defensibility | "
        f"{_cell(verdict_active, 'single-fire-separation-verdict-defensibility')} |",
        "| Mixed rig within this run | "
        f"{_cell(mixed_rig_active, 'mixed-rig-within-this-run')} |",
        "| Regime note | [ℹ️ standing note](DETAILS.md#regime-note) |",
        "| Refresh cadence | [ℹ️ standing note](DETAILS.md#refresh-cadence) |",
    ]
    if _clean_concurrent_burst(results):
        lines.append(
            "| Concurrent-burst measurement regime | [ℹ️ see section]"
            "(DETAILS.md#concurrent-burst-measurement-regime) |"
        )
    lines.append("")
    return "\n".join(lines)


def render_known_anomalies_detail(results, kata_results=None, history_rows=None):
    """DETAILS.md counterpart to render_known_anomalies_table (epic #6669 WS1).

    One `### <heading>` per row, heading text chosen so GitHub's own slug algorithm
    produces exactly the anchor the README table links to (no explicit `<a id>` — this
    codebase's DETAILS.md anchors are always plain hand-synced headings, never anchor
    tags). Live caveat text is byte-identical to what previously rendered inline under the
    North Star caption; a clear condition gets a short fallback sentence instead of the
    (empty) caveat text, naming the check without duplicating prose that has nothing to
    disclose. Regime note / Refresh cadence show their full static text unconditionally —
    they were never conditional in the first place.

    `history_rows` — see render_known_anomalies_table's docstring; same absent/empty handling.
    """
    product = results.get("product")
    if product not in PRODUCTS:
        return ""
    rows = _north_star_rows(results, kata_results)
    fail_caveat = _north_star_fail_caveat(rows)
    inversion_caveat = _warm_cold_inversion_caveat(results, kata_results)
    separation_caveat = _warmpool_separation_caveat(results, kata_results)
    variance_caveat = _warmpool_separation_variance_caveat(history_rows or [])
    verdict_caveat = _warmpool_separation_verdict_caveat(results, history_rows or [], kata_results)
    mixed_rig_caveat = _mixed_rig_confound_caveat(results)

    def _clear(name):
        return f"_Clear as of the latest measured refresh — no {name} currently disclosed._"

    lines = ["## Known anomalies", ""]
    lines.append("### Scenario FAIL")
    lines.append("")
    lines.append(fail_caveat if fail_caveat else _clear("scenario FAIL"))
    lines.append("")
    lines.append("### Warm-slower-than-cold")
    lines.append("")
    lines.append(
        inversion_caveat if inversion_caveat else _clear("warm-slower-than-cold inversion")
    )
    lines.append("")
    lines.append("### Warm-cold separation below gate")
    lines.append("")
    lines.append(
        separation_caveat if separation_caveat else _clear("warm/cold separation shortfall")
    )
    lines.append("")
    lines.append("### Same-build separation-ratio variance")
    lines.append("")
    lines.append(
        variance_caveat if variance_caveat else _clear("same-build separation-ratio variance")
    )
    lines.append("")
    lines.append("### Single-fire separation verdict defensibility")
    lines.append("")
    lines.append(
        verdict_caveat if verdict_caveat
        else _clear("single-fire separation verdict as indefensible")
    )
    lines.append("")
    lines.append("### Mixed rig within this run")
    lines.append("")
    lines.append(mixed_rig_caveat if mixed_rig_caveat else _clear("mixed-rig confound"))
    lines.append("")
    lines.append("### Regime note")
    lines.append("")
    lines.append(_REGIME_BOUNDARY_NOTE)
    lines.append("")
    lines.append("### Refresh cadence")
    lines.append("")
    lines.append(_REFRESH_CADENCE_NOTE)
    lines.append("")
    if _clean_concurrent_burst(results):
        lines.append("### Concurrent-burst measurement regime")
        lines.append("")
        lines.append(
            "Per-fire measurement-regime disclosures for the concurrent-burst sweep render "
            "inline in [README.md](README.md#does-it-hold-at-cluster-scale) next to each "
            "burst — the disclosure can repeat once per date/regime group, so it is not "
            "summarized here."
        )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_warmpool_separation_trend_chart(history_rows):
    """Render the warm/cold separation-ratio history as a Unicode block-bar chart (WS2 follow-up
    to #6890, epic #6669), or "" when INERT (no rows).

    Visual companion to the prose caveats in render_known_anomalies_detail
    (_warmpool_separation_variance_caveat / _warmpool_separation_verdict_caveat): those disclose
    the SAME-build cross-fire variance and the near-threshold-verdict defensibility in text; this
    chart lets a reader see the ratio's fire-over-fire trajectory against the gate at a glance,
    including WHICH builds moved it (a short controller_digest prefix per bar), rather than
    scanning the JSONL by hand. Same source (_clean_warmpool_separation_history) as those
    caveats, so the chart can never show a fire the prose disclosures don't already know about.

    One bar per accrued fire (this store is append-only keyed by run_id, so re-fires of the same
    build show as separate bars — that repetition IS the signal the #6890 store exists to
    surface), oldest to newest. A bar below WARMPOOL_SEPARATION_MIN_RATIO is annotated
    " (below gate)" using the exact comparison _warmpool_separation_caveat uses
    (ratio < WARMPOOL_SEPARATION_MIN_RATIO) so the chart's flagging never drifts from the prose's.

    Plain code-block Unicode bars, not mermaid xychart-beta (same GitHub-support rationale as
    render_throughput_trend_chart / render_density_bars / render_ttfe_bars).
    """
    rows = _clean_warmpool_separation_history(history_rows)
    if not rows:
        return ""
    max_ratio = max(r["separation_ratio"] for r in rows)
    if max_ratio <= 0:
        return ""
    label_width = max(len(r["generated_at"][:10]) for r in rows)
    lines = ["```", "Warm/cold separation ratio — fire-over-fire (gate: {:.1f}x)".format(
        WARMPOOL_SEPARATION_MIN_RATIO
    ), ""]
    for r in rows:
        ratio = r["separation_ratio"]
        date_label = r["generated_at"][:10].ljust(label_width)
        digest = r["controller_digest"]
        digest_label = digest[7:15] if digest.startswith("sha256:") else digest[:8]
        bar = "█" * max(1, round(ratio / max_ratio * _TREND_BAR_WIDTH))
        suffix = " (below gate)" if ratio < WARMPOOL_SEPARATION_MIN_RATIO else ""
        lines.append(f"{date_label} {digest_label} {bar} {ratio:.2f}x{suffix}")
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


# --- hb#134: operating-envelope headline table -------------------------------------------
# The single "given MY load, what wait do I budget?" table — the reader's only real question.
# It does NOT re-measure anything: it reconciles the warm/wait numbers already measured across
# four INDEPENDENT blocks (the warmpool_cold_start scenario, at_scale_contention, the warm
# concurrent_burst leg, and warm_pool_acquisition) into ONE reader-facing envelope. Every number
# is READ live from the schema via the SAME closed-schema cleaners the source blocks use — no
# hardcoded illustrative figures — and each row INHERITS its source block's pending semantics: an
# INERT/absent source renders THAT row `pending` rather than dropping it (honest skeleton, same
# discipline as the matrix). A `scope` column keeps a full-TTFE row from ever being silently
# compared against the acquisition-only sub-phase. The warm-vs-cold speedup leg is deliberately
# EXCLUDED — it is a ratio-context number, not a wait a reader budgets.
_ENVELOPE_FULL_TTFE = "full start → first result"
_ENVELOPE_ACQ_ONLY = "pool hand-off only (before exec)"


def _envelope_warm_burst_leg(results):
    """The representative "many at once" warm concurrent-burst leg for the envelope.

    Prefer the n=300 warm leg (the agreed representative simultaneous-burst point); if absent,
    fall back to the LARGEST-n warm leg so the row degrades to whatever simultaneous burst was
    actually measured, labelled with its true N. None ⇒ no warm burst leg ⇒ that row pends.
    """
    cb = _clean_concurrent_burst(results)
    if not cb:
        return None
    warm = [
        leg for leg in cb["legs"]
        if leg.get("mode") == "warm" and "ttfe_p50_ms" in leg
        and isinstance(leg.get("n"), int) and not isinstance(leg.get("n"), bool)
    ]
    if not warm:
        return None
    for leg in warm:
        if leg["n"] == 300:
            return leg
    return max(warm, key=lambda leg: leg["n"])


def render_operating_envelope(results, heading=None):
    """Render the hb#134 operating-envelope headline table (always renders; rows pend individually).

    Answers the one question a model-builder / agentic-dev actually has: *given my load pattern,
    what wait do I budget?* Four load patterns, each with its measured p50 wait and a `scope`
    column so the acquisition-only row is never mis-ranked against the full-TTFE rows. Numbers are
    render-derived from the live schema; a source block that is absent/INERT pends its row.
    """
    scen = _matrix_scenarios(results.get("scenarios"))
    asc = _clean_at_scale_contention(results)
    burst = _envelope_warm_burst_leg(results)
    wpa = _clean_warm_pool_acquisition(results)

    rows = []
    fail_notes = []

    # Row 1 — steady trickle, warm pool keeps up (full TTFE, from the matrix warm scenario).
    # #548: gate on the metric being PRESENT, not on outcome=="PASS" — _matrix_scenarios
    # suppresses metrics only for a `pending` outcome, so a FAIL cell still carries a real
    # measurement here. Keep the real number and tag the row loudly (mirrors the core matrix's
    # own #4420 FAIL idiom) instead of discarding it into a bare `pending`.
    sc = scen.get("warmpool_cold_start")
    label1 = "Steady trickle — warm pool keeps up with demand"
    if sc and "ttfe_p50_ms" in sc["metrics"]:
        if sc.get("outcome") == "FAIL":
            fail_notes.append(label1)
            label1 = f"{label1} ⚠️ FAIL"
        rows.append((label1, _fmt_wait(sc["metrics"]["ttfe_p50_ms"]), _ENVELOPE_FULL_TTFE))
    else:
        # hb#134 (nit): a row-1 pend inherits the matrix scenario's pending_reason so a
        # known upstream/cluster gap reads `pending (<reason>)` here exactly as it does in the
        # matrix, not a bare `pending` that looks not-yet-run. The reason decorates only a
        # genuinely pending scenario (mirrors the matrix pending_tok logic at ~653); a
        # missing-ttfe PASS or an absent scenario falls back to bare `pending`.
        pending_tok = _PENDING
        if sc is not None and sc.get("outcome") == "pending":
            reason = sc.get("pending_reason")
            if reason:
                pending_tok = f"{_PENDING} ({reason})"
        rows.append((label1, pending_tok, _ENVELOPE_FULL_TTFE))

    # Row 2 — bursty, pool oversubscribed (full TTFE, from the contention retraction point).
    if asc:
        pool, claims = asc["pool_size"], asc["claim_count"]
        ratio = f"{_fmt_ratio(claims / pool)}:1" if pool else "?:1"
        rows.append((
            f"Bursty — pool oversubscribed {ratio} ({_fmt_num(claims)} claims / {_fmt_num(pool)} ready)",
            _fmt_wait(asc["ttfe_p50_ms"]), _ENVELOPE_FULL_TTFE,
        ))
    else:
        rows.append((
            "Bursty — pool oversubscribed (more claims than ready pool)",
            _PENDING, _ENVELOPE_FULL_TTFE,
        ))

    # Row 3 — many simultaneous @1:1 (full TTFE, from the warm concurrent-burst leg).
    if burst:
        rows.append((
            f"{_fmt_num(burst['n'])} sandboxes requested at once (1:1 pool)",
            _fmt_wait(burst["ttfe_p50_ms"]), _ENVELOPE_FULL_TTFE,
        ))
    else:
        rows.append((
            "Hundreds of sandboxes requested at once (1:1 pool)",
            _PENDING, _ENVELOPE_FULL_TTFE,
        ))

    # Row 4 — sustained high-rate churn (acquisition-ONLY sub-phase — NOT comparable above).
    if wpa:
        rate = wpa.get("offered_rate_per_s")
        label4 = (
            f"Sustained {_fmt_num(rate)}/sec churn" if rate is not None
            else "Sustained high-rate churn"
        )
        rows.append((label4, _fmt_wait(wpa["acq_p50_ms"]), _ENVELOPE_ACQ_ONLY))
    else:
        rows.append(("Sustained high-rate churn", _PENDING, _ENVELOPE_ACQ_ONLY))

    heading = heading or "## Operating Envelope — what wait should I budget?"
    lines = [heading, ""]
    lines.append(
        "Find the row closest to **your** load; the p50 is the wait to plan around. The **Scope** "
        "column is load-bearing: the first three rows are the **full** start→first-result wait "
        "(TTFE), directly comparable to one another; the last row is only the **pool hand-off** "
        "sub-phase (it stops the moment you hold a ready sandbox, before your code runs), so do "
        "**not** rank its number against the full-TTFE rows above it. Every number is measured, "
        "not modelled — an unmeasured row reads `pending`, never a guess."
    )
    lines.append("")
    header = ["Your load pattern", "Wait to budget (p50)", "Scope"]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")
    for label, wait, scope in rows:
        lines.append(f"| {label} | {link_pending(wait)} | {scope} |")
    lines.append("")
    # #548: mirrors the core matrix's own #4420 FAIL disclosure — a FAIL row above carries a
    # real measurement, disclosed loudly here rather than softened or dropped.
    if fail_notes:
        who = "; ".join(f"**{note}**" for note in fail_notes)
        lines.append(
            f"_⚠️ **Scenario FAIL:** {who} — the row above carries a real measurement whose "
            "own scenario outcome is **FAIL** (SLA not met), not a passing warm hit. The wait "
            "is honest data, disclosed as a miss rather than dropped or hidden as `pending`; a "
            "later refresh whose scenario returns to PASS clears this._"
        )
        lines.append("")
    return "\n".join(lines)


# The ephemeral-CI regime boundary (see _REGIME_BOUNDARY_NOTE above) as a machine-comparable
# ISO-8601 string. Both `generated_at` fields this compares against are canonical
# "%Y-%m-%dT%H:%M:%SZ" UTC stamps (same format on every product's harness output), so a plain
# string comparison sorts chronologically identically to a real datetime comparison — no need to
# parse into datetime objects for a single boundary check.
_EPHEMERAL_CI_CUTOVER = "2026-07-20T00:00:00Z"


def _runtime_choice_clause(results, kata_results):
    """The gVisor-vs-Kata isolation-tradeoff clause in render_what_this_means (hb#352 follow-up).

    Only claims warm-pool latency is "comparable between them" when both runtimes' numbers were
    actually measured in the same cluster regime (see _REGIME_BOUNDARY_NOTE) — pre- or
    post-2026-07-20 ephemeral-CI cutover. A cross-regime pairing (one side pre-cutover on the
    long-lived pre-warmed cluster, the other post-cutover on a cold ephemeral cluster) makes any
    latency gap between the two runtimes at least partly a regime artifact, not a runtime
    difference, so the unqualified comparability claim would be misleading without disclosure.
    """
    kata_measured = False
    kata_gen = None
    if isinstance(kata_results, dict) and kata_results.get("product") == "sandbox-kata":
        kp = _clean_provenance(kata_results.get("provenance"))
        if kp.get("runtime") == "kata-microvm":
            k_scen = _matrix_scenarios(kata_results.get("scenarios"))
            sc = k_scen.get("warmpool_cold_start")
            if sc and "ttfe_p95_ms" in sc.get("metrics", {}):
                kata_measured = True
                g = kata_results.get("generated_at")
                if isinstance(g, str) and _ISO.match(g):
                    kata_gen = g

    if not kata_measured:
        return (
            "- **gVisor is measured here; Kata + microVM adds hardware-grade VM isolation for "
            "workloads that need it.** Its own warm-pool latency numbers land once that runtime "
            "is measured — see the matrix above for current status."
        )

    gvisor_gen = results.get("generated_at")
    gvisor_gen = gvisor_gen if isinstance(gvisor_gen, str) and _ISO.match(gvisor_gen) else None
    cross_regime = (
        gvisor_gen is not None
        and kata_gen is not None
        and (gvisor_gen >= _EPHEMERAL_CI_CUTOVER) != (kata_gen >= _EPHEMERAL_CI_CUTOVER)
    )
    if cross_regime:
        return (
            "- **Both runtimes are measured, but not from the same cluster regime right now.** "
            "gVisor's numbers above are from a fresh ephemeral CI cluster (cold containerd "
            "cache); Kata + microVM's are from an earlier run on a long-lived, pre-warmed "
            "cluster (see the matrix's per-run disclosure above) — so today's warm-pool-latency "
            "gap between them is at least partly that regime difference, not necessarily the "
            "runtimes themselves. gVisor delivers the higher per-node throughput; Kata + microVM "
            "puts each sandbox in its own VM for hardware-grade isolation. If unsure, start with "
            "gVisor and move only the workloads that need a VM boundary to Kata once its own "
            "ephemeral-regime numbers land."
        )
    return (
        "- **Both runtimes are measured — choose by isolation need.** In the measurements above, "
        "warm-pool latency is comparable between them; gVisor delivers the higher per-node "
        "throughput, while Kata + microVM puts each sandbox in its own VM for hardware-grade "
        "isolation. If unsure, start with gVisor and move only the workloads that need a VM "
        "boundary to Kata."
    )


def render_what_this_means(results, kata_results=None):
    """hb#134 plain-English synthesis for the non-infra reader (model-builders / agentic devs).

    Reads the SAME closed-schema cleaners the tables above use, so every number here is the
    render-derived measured value — never a hand-typed figure that could drift from the table it
    summarizes. The section ALWAYS renders; each measured clause degrades to a qualitative
    statement (never a guessed number) when its source block is absent/INERT, mirroring
    render_operating_envelope. The product-shape and pool-sizing statements are static by nature
    (a rule of thumb / a capability posture, not a measurement) and labelled as such so a reader
    never mistakes them for one of the machine-rendered figures.
    """
    scen = _matrix_scenarios(results.get("scenarios"))
    wc = _clean_warm_vs_cold(results)
    burst = _envelope_warm_burst_leg(results)

    lines = ["## What this means for you", ""]
    lines.append(
        "The tables above are the raw measurements. If you build *on* sandboxes but do not run "
        "the cluster yourself, here is what they mean in practice:"
    )
    lines.append("")

    # Clause 1 — the everyday wait when the warm pool keeps up (matrix warm scenario p50/p95).
    # #548: gate on the metric being PRESENT, not on outcome=="PASS" (mirrors Row 1's fix in
    # render_operating_envelope above) — a FAIL cell still carries a real p50/p95 measurement.
    sc = scen.get("warmpool_cold_start")
    if sc and "ttfe_p50_ms" in sc["metrics"]:
        p50 = _fmt_wait(sc["metrics"]["ttfe_p50_ms"])
        tail = (
            f" ({_fmt_wait(sc['metrics']['ttfe_p95_ms'])} at the p95)"
            if "ttfe_p95_ms" in sc["metrics"] else ""
        )
        if sc.get("outcome") == "FAIL":
            # Honest disclosure, not the affirmative "fast enough" clause and not the
            # "measurement lands" pending clause below — the number is real, but this run's
            # own scenario outcome is FAIL (SLA not met), so it must not read as a clean pass.
            lines.append(
                f"- **⚠️ Measured, but the warm pool did NOT clear its SLA this run: a new "
                f"sandbox took {p50}{tail}.** That figure is real — not fabricated or "
                "estimated — but this scenario's own outcome is FAIL, so treat it as a "
                "measured miss to budget against rather than a clean steady-state number; a "
                "later refresh whose scenario returns to PASS clears this caveat."
            )
        else:
            lines.append(
                f"- **Keep a warm pool sized to demand and a new sandbox is ready in {p50}"
                f"{tail}.** That is fast enough to put a fresh sandbox directly in a user-facing "
                "request path — no need to hide it behind a spinner or pre-allocate one per session."
            )
    else:
        lines.append(
            "- **Keep a warm pool sized to demand and a new sandbox is ready quickly** — a claim "
            "against a ready pool skips the fresh-node startup path. The exact wait to budget is "
            "in the operating envelope below once that measurement lands."
        )

    # Clause 2 — warm pools pay off (warm_vs_cold speedup + runtime).
    if wc:
        speedup = _fmt_num(round(wc["cold_ms"] / wc["warm_p50_ms"], 1))
        rt = RUNTIME_LABELS[wc["runtime_class"]]
        lines.append(
            f"- **A warm-pool hit is about {speedup}× faster than starting cold ({rt}).** If "
            "start-up latency matters to you, the warm pool is the single biggest lever — size it "
            "for your steady demand and most claims never pay the cold path. (This ratio is the "
            "dedicated warm-vs-cold leg — a separate point-in-time measurement from the Core "
            "Metrics matrix rows above, so do not reproduce it by dividing the matrix cells.)"
        )
    else:
        lines.append(
            "- **A warm-pool hit is much faster than starting cold.** If start-up latency matters "
            "to you, the warm pool is the single biggest lever — size it for your steady demand "
            "and most claims never pay the cold path."
        )

    # Clause 3 — bursts work but are the overflow regime (warm concurrent-burst leg).
    if burst:
        n = _fmt_num(burst["n"])
        bw = _fmt_wait(burst["ttfe_p50_ms"])
        lines.append(
            f"- **Big simultaneous bursts still work — {n} sandboxes asked for at once settled in "
            f"{bw}.** But that is the pool-overflow regime: the wait climbs toward the "
            "cold-start number as claims outrun ready slots, so plan the pool around your steady "
            "rate, not your worst spike."
        )
    else:
        lines.append(
            "- **Big simultaneous bursts still work, but they are the pool-overflow regime** — the "
            "wait climbs toward the cold-start number as claims outrun ready slots, so plan the "
            "pool around your steady rate, not your worst spike."
        )

    # Static planning heuristic + product-shape posture (NOT measurements — labelled as such).
    lines.append(
        "- **Rule of thumb for pool size:** start near your typical concurrent demand (≈0.75× of "
        "it) and tune from there. This is a planning heuristic, not one of the measured numbers "
        "above."
    )
    lines.append(_runtime_choice_clause(results, kata_results))
    lines.append(
        "- **gVisor suspend/resume is measured — Kata resume is not.** The gVisor resume cells now "
        "carry real numbers (the upstream resume-graduation fix merged and a fresh probe landed). "
        "Kata resume stays `N/A` by construction (checkpoint-restore does not transfer to the VM "
        "model) — treat Kata resume as unavailable."
    )
    lines.append(
        "- **A cell marked `pending` is unmeasured, not bad.** It means that measurement has not "
        "run yet (or is blocked upstream) — never that the platform failed it."
    )
    lines.append("")
    return "\n".join(lines)


def _clean_burst_corroboration(scenarios):
    """Find burst_create and closed-schema-clean its corroboration metrics (#3954).

    Returns {ready, exec, n, exec_success_rate, exec_success_n} ONLY when BOTH the pod-Ready
    count (sandboxes_ready_under_1s) and the executed-TTFE count (sandboxes_exec_under_1s) are
    present — that BOTH-required gate is what keeps the block INERT until the #3954 exec fields
    land (today's ready-only data renders nothing). Returns None otherwise. Any sla_metrics key
    not in BURST_CORROBORATION_FIELDS, or failing its predicate, is dropped (closed schema).
    """
    if not isinstance(scenarios, list):
        return None
    for s in scenarios:
        if not isinstance(s, dict) or s.get("name") != "burst_create":
            continue
        metrics = s.get("sla_metrics")
        if not isinstance(metrics, dict):
            return None
        clean = {}
        for key, ok in BURST_CORROBORATION_FIELDS.items():
            if key in metrics:
                try:
                    if ok(metrics[key]):
                        clean[key] = metrics[key]
                except (TypeError, ValueError):
                    pass
        if "sandboxes_ready_under_1s" not in clean or "sandboxes_exec_under_1s" not in clean:
            return None
        n = s.get("n")
        n = n if isinstance(n, int) and not isinstance(n, bool) and n >= 0 else 0
        return {
            "ready": clean["sandboxes_ready_under_1s"],
            "exec": clean["sandboxes_exec_under_1s"],
            "n": n,
            "exec_success_rate": clean.get("exec_success_rate"),
            "exec_success_n": clean.get("exec_success_n"),
        }
    return None


def render_burst_corroboration(results):
    """Render the burst-create TTFE corroboration block (#3954), or "" when INERT.

    The headline burst count is POD-READY (the weaker claim — a pod can report Ready before it
    can run your code). This block surfaces the stronger TTFE claim (the sandbox executed its
    first instruction and returned a result <1s) alongside it, and the GAP between them —
    sandboxes that reported Ready but had not yet run code, i.e. the over-claim a pod-Ready-only
    headline would hide. Rendered ONLY when both counts are present (see
    _clean_burst_corroboration), so the public page is byte-unchanged until a #3954 fire lands.
    """
    corr = _clean_burst_corroboration(results.get("scenarios"))
    if not corr:
        return ""
    gap = corr["ready"] - corr["exec"]
    lines = ["## Burst Create — TTFE Corroboration", ""]
    lines.append(
        "The headline burst count is **pod-Ready** — but a pod can report Ready before it can "
        "run your code. TTFE is the stronger claim: the sandbox *executed its first instruction "
        "and returned a result*. This block corroborates the two; the **gap** is sandboxes that "
        "reported Ready but had not yet run code."
    )
    lines.append("")
    header = ["Signal", "Count"]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")
    lines.append(f"| Pod-Ready <1s (weaker claim) | {_fmt_num(corr['ready'])} |")
    lines.append(
        f"| Executed first-instruction <1s (TTFE, stronger claim) | {_fmt_num(corr['exec'])} |"
    )
    lines.append(f"| Ready-but-not-yet-run (gap) | {_fmt_num(gap)} |")
    if corr["exec_success_rate"] is not None:
        n_total = corr["n"] or None
        lines.append(
            "| Execution success (Honesty Check) | "
            + _exec_cell(corr["exec_success_rate"], n_total, corr["exec_success_n"])
            + " |"
        )
    lines.append("")
    lines.append(
        "_Pod-Ready ≥ executed-TTFE by construction; the gap is the over-claim a pod-Ready "
        "headline would hide._"
    )
    lines.append("")
    return "\n".join(lines)


def _clean_warm_bind_decomposition(scenarios):
    """Find warmpool_cold_start and closed-schema-clean its TTFE decomposition (inch #1).

    Returns {bind_p50, bind_p95, exec_p50, exec_p95, ttfe_p50, ttfe_p95} ONLY when ALL of the six
    percentile keys are present — that all-six-required gate is what keeps the block INERT until a
    decomposition-instrumented fire lands (today's pre-decomposition data has the ttfe pair but
    not the bind/exec pairs, so this renders nothing). Returns None otherwise. Every value is a
    GENUINELY-MEASURED percentile: exec_p50_ms/exec_p95_ms come from the producer's per-claim
    (ttfe_ms - bind_ms) distribution, NOT a render-side p50(ttfe)-p50(bind) subtraction. Any
    sla_metrics key not in WARM_BIND_FIELDS, or failing its predicate, is dropped (closed schema).
    """
    if not isinstance(scenarios, list):
        return None
    for s in scenarios:
        if not isinstance(s, dict) or s.get("name") != "warmpool_cold_start":
            continue
        metrics = s.get("sla_metrics")
        if not isinstance(metrics, dict):
            return None
        clean = {}
        for key, ok in WARM_BIND_FIELDS.items():
            if key in metrics:
                try:
                    if ok(metrics[key]):
                        clean[key] = metrics[key]
                except (TypeError, ValueError):
                    pass
        needed = (
            "bind_p50_ms", "bind_p95_ms",
            "exec_p50_ms", "exec_p95_ms",
            "ttfe_p50_ms", "ttfe_p95_ms",
        )
        if any(k not in clean for k in needed):
            return None
        return {
            "bind_p50": clean["bind_p50_ms"],
            "bind_p95": clean["bind_p95_ms"],
            "exec_p50": clean["exec_p50_ms"],
            "exec_p95": clean["exec_p95_ms"],
            "ttfe_p50": clean["ttfe_p50_ms"],
            "ttfe_p95": clean["ttfe_p95_ms"],
        }
    return None


def render_warm_bind_decomposition(results):
    """Render the warm-hit TTFE bind-vs-exec decomposition (inch #1), or "" when INERT.

    The warm-pool-hit TTFE (create->first-instruction-result) splits into BIND (create->bound,
    i.e. provisioning) + EXEC (websocket setup + the first-instruction round-trip). When the
    warm-hit p50/p95 sits above the <1s North Star, this block shows WHERE the time lives: a bind
    p50 near the TTFE p50 means provisioning dominates (a real controller/clone target); a small
    bind p50 with a large exec p50 means the exec channel (websocket setup) dominates (a
    harness/product artifact, not a controller regression).

    HONESTY: bind, exec, and TTFE are each an INDEPENDENTLY-MEASURED percentile of its own
    per-claim distribution — exec comes from the producer's per-claim (ttfe_ms - bind_ms) samples,
    NOT a render-side p50(ttfe)-p50(bind) subtraction (percentiles do not subtract linearly). The
    three rows therefore need NOT sum. Rendered ONLY when all of bind, exec, AND TTFE percentiles
    are present (see _clean_warm_bind_decomposition), so the public page is byte-unchanged until a
    fire emits the keys. Diagnostic-only — adds a block, changes no existing cell.
    """
    dec = _clean_warm_bind_decomposition(results.get("scenarios"))
    if not dec:
        return ""
    lines = ["## Warm-Hit TTFE — Bind vs Exec Decomposition", ""]
    lines.append(
        "Warm-hit TTFE (create → first-instruction result) splits into **bind** (create → bound, "
        "i.e. provisioning the pool member) and **exec** (websocket setup + the first-instruction "
        "round-trip). This block shows *where* a warm-hit above the <1s target lives — a large "
        "bind points at provisioning (a controller/clone target); a large exec points at the "
        "exec channel (a harness/product artifact, not a controller regression)."
    )
    lines.append("")
    header = ["Stage", "p50", "p95"]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")
    lines.append(
        f"| Bind (create → bound, provisioning) | {_fmt_secs(dec['bind_p50'])} | "
        f"{_fmt_secs(dec['bind_p95'])} |"
    )
    lines.append(
        f"| Exec (websocket + first-instruction) | {_fmt_secs(dec['exec_p50'])} | "
        f"{_fmt_secs(dec['exec_p95'])} |"
    )
    lines.append(
        f"| **TTFE (total)** | **{_fmt_secs(dec['ttfe_p50'])}** | **{_fmt_secs(dec['ttfe_p95'])}** |"
    )
    lines.append("")
    lines.append(
        "_Each row is an independently-measured percentile of its own per-claim distribution "
        "(exec is measured per-claim as TTFE − bind, then percentiled — not p50(TTFE) − p50(bind)). "
        "Percentiles do not sum, so bind and exec need not add exactly to the total TTFE._"
    )
    # Drained-regime caveat (#103/#111), data-keyed on provenance.regime so it cannot rot:
    # once an under-load fire clears the bar and emits regime="under-load" (or omits it),
    # this caveat stops rendering by construction. Kept off any measured cell — it qualifies
    # the claim, it does not alter a number.
    regime = None
    prov = results.get("provenance")
    if isinstance(prov, dict):
        regime = prov.get("regime")
    if regime == "drained":
        caveat = (
            "> ⚠️ **Regime caveat:** this warm tier was measured on a **drained, "
            "low-contention cluster** (single fire, small claim count). A green warm tier "
            "here is honest for THIS fire but is **not yet a sustained North-Star claim** — "
            "it wants corroboration under representative load before sub-1s warm is treated "
            "as durable."
        )
        # #4137: name the scaling term ON the caveat, only when the drained fire emits a valid
        # warm_scaling_term. The dict .get() validates against the closed WARM_SCALING_TERMS
        # vocabulary; an absent or out-of-enum value renders the caveat unchanged.
        scaling_term = prov.get("warm_scaling_term") if isinstance(prov, dict) else None
        caveat += _WARM_SCALING_TERM_CLAUSE.get(scaling_term, "")
        lines.append("")
        lines.append(caveat)
    lines.append("")
    return "\n".join(lines)


def render_bind_exec_pie(results):
    """Render the warm-hit bind-vs-exec split as a mermaid pie chart, or "" when INERT.

    Visual companion to render_warm_bind_decomposition (WS2, epic #6669) — same data, same
    INERT gate (_clean_warm_bind_decomposition), so this chart can never diverge from or
    outlive the table it sits beside. Uses bind_p50/exec_p50 (not p95) as the pie slices: a
    pie is a part-of-whole share, and p50 is each stage's typical share of the typical
    warm-hit TTFE. GitHub's built-in markdown renderer supports mermaid `pie` charts natively
    — no external image build, no new dependency (mirrors render_measurement_path_diagram's
    reliance on GitHub-native mermaid, not a newer/unconfirmed diagram type such as
    xychart-beta).
    """
    dec = _clean_warm_bind_decomposition(results.get("scenarios"))
    if not dec:
        return ""
    lines = [
        "```mermaid",
        "pie showData",
        "    title Warm-Hit TTFE p50 split — Bind vs Exec (ms)",
        f'    "Bind (provisioning)" : {dec["bind_p50"]}',
        f'    "Exec (websocket + first-instruction)" : {dec["exec_p50"]}',
        "```",
        "",
    ]
    return "\n".join(lines)


def _clean_session_turnover(scenarios):
    """Find session_turnover and closed-schema-clean its refill-latency metrics, or None.

    session_turnover measures the full claim → use → release → reclaim loop: after each claim is
    released the controller must REPLENISH the warm pool, and the scenario reports how long that
    refill takes under sustained cycling. refill_latency_ms (the median) is the REQUIRED spine —
    its presence is the INERT gate, so today's pre-fire data (no session_turnover cell, or a cell
    whose pool never refilled and emitted {}) renders nothing. refill_p90_ms (the tail) is
    OPTIONAL. n (completed-cycle count, for the sample-size footnote) is read from the scenario's
    TOP-LEVEL "n" field, NOT from sla_metrics — run.py lifts the reserved "n" key out of
    sla_metrics into a top-level scenario field before coercion (mirrors _clean_burst_corroboration).
    Any sla_metrics key not in SESSION_TURNOVER_FIELDS, or failing its predicate, is dropped
    (closed schema).
    """
    if not isinstance(scenarios, list):
        return None
    for s in scenarios:
        if not isinstance(s, dict) or s.get("name") != "session_turnover":
            continue
        metrics = s.get("sla_metrics")
        if not isinstance(metrics, dict):
            return None
        clean = {}
        for key, ok in SESSION_TURNOVER_FIELDS.items():
            if key in metrics:
                try:
                    if ok(metrics[key]):
                        clean[key] = metrics[key]
                except (TypeError, ValueError):
                    pass
        if "refill_latency_ms" not in clean:
            return None
        n = s.get("n")
        n = n if isinstance(n, int) and not isinstance(n, bool) and n >= 0 else None
        return {
            "refill_p50": clean["refill_latency_ms"],
            "refill_p90": clean.get("refill_p90_ms"),
            "n": n,
        }
    return None


def render_session_turnover(results):
    """Render the warm-pool session-turnover refill-latency block, or "" when INERT.

    The headline page measures the CLAIM side of the agentic lifecycle (warm-pool acquisition +
    warm-vs-cold). This block measures the RECLAIM side: after a claim is released, how fast does
    the controller replenish the warm pool so the next claim is still a warm hit? Under sustained
    claim/release churn (the most-asked agentic pattern — a fleet cycling sandboxes continuously)
    a slow refill silently demotes later claims from warm to cold. Rendered ONLY when a fire emits
    a measured median refill (see _clean_session_turnover), so the public page is byte-unchanged
    until a session_turnover fire lands. Diagnostic-only — adds a block, changes no existing cell.
    """
    turn = _clean_session_turnover(results.get("scenarios"))
    if not turn:
        return ""
    lines = ["## Warm-Pool Turnover — Sustained-Churn Refill Latency", ""]
    lines.append(
        "The matrix measures the **claim** side (a warm hit is sub-second). This block measures "
        "the **reclaim** side: after a claim is released, how long the controller takes to "
        "**replenish** the warm pool under sustained claim/release churn. A slow refill silently "
        "demotes later claims from warm to cold — the failure mode a fleet cycling sandboxes "
        "continuously actually hits."
    )
    lines.append("")
    n = turn["n"]
    n_note = f" (over {_fmt_num(n)} cycles)" if n is not None else ""
    header = ["Refill latency", "Value"]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")
    lines.append(f"| Median (p50){n_note} | {_fmt_secs(turn['refill_p50'])} |")
    if turn["refill_p90"] is not None:
        lines.append(f"| Tail (p90) | {_fmt_secs(turn['refill_p90'])} |")
    lines.append("")
    # The tail row is suppressed when no p90 was emitted (a single-cycle fire); keep the caption's
    # percentile claim in lockstep with the rows actually rendered so prose never promises a tail
    # the table omits.
    pctile_clause = (
        "the median and tail are percentiles"
        if turn["refill_p90"] is not None
        else "the median is the 50th percentile"
    )
    lines.append(
        "_Refill latency is measured per-cycle as the wall-clock from a claim release to the warm "
        f"pool returning to full readiness; {pctile_clause} of the completed-cycle distribution._"
    )
    lines.append("")
    return "\n".join(lines)


def _clean_suspend_latency(scenarios):
    """Find suspend_resume and closed-schema-clean its administrative-suspend latency, or None.

    The suspend_resume cell's correctness verdict (the Suspended-clear gap) and its resume-
    activation TTFE distribution are rendered elsewhere; THIS reads only the administrative-suspend
    latency pair the cell now also emits. suspend_latency_ms (the median) is the REQUIRED spine —
    its presence is the INERT gate, so today's pre-fire data (no suspend_resume cell, or a cell
    that ran before this axis emitted, whose sla_metrics carries no suspend_latency_ms) renders
    nothing. suspend_p90_ms (the tail) is OPTIONAL (present only when the fire ran n>=2 cycles).
    Any sla_metrics key not in SUSPEND_LATENCY_FIELDS, or failing its predicate, is dropped
    (closed schema) — so the resume TTFE pair, pending_reason, and n never leak into this block.
    """
    if not isinstance(scenarios, list):
        return None
    for s in scenarios:
        if not isinstance(s, dict) or s.get("name") != "suspend_resume":
            continue
        metrics = s.get("sla_metrics")
        if not isinstance(metrics, dict):
            return None
        clean = {}
        for key, ok in SUSPEND_LATENCY_FIELDS.items():
            if key in metrics:
                try:
                    if ok(metrics[key]):
                        clean[key] = metrics[key]
                except (TypeError, ValueError):
                    pass
        if "suspend_latency_ms" not in clean:
            return None
        return {
            "suspend_p50": clean["suspend_latency_ms"],
            "suspend_p90": clean.get("suspend_p90_ms"),
        }
    return None


def render_suspend_latency(results):
    """Render the administrative-suspend latency block, or "" when INERT.

    Suspend is the cost-lever an operator pulls to reclaim a Sandbox's compute while preserving its
    identity (the CR survives; only the backing Pod is released). This block reports how fast that
    ADMINISTRATIVE suspend completes — the operatingMode=Suspended patch → terminal-Suspended
    wall-clock. It is DELIBERATELY not framed as an idle/auto-suspend: upstream agent-sandbox has no
    idle-timeout or activity-reclaim path, so a reader must not infer an automatic scale-to-zero
    from this number. Rendered ONLY when a suspend_resume fire emits a measured median suspend
    latency (see _clean_suspend_latency), so the public page is byte-unchanged until that fire lands.
    Diagnostic-only — adds a block, changes no existing cell.
    """
    susp = _clean_suspend_latency(results.get("scenarios"))
    if not susp:
        return ""
    lines = ["## Administrative Suspend Latency", ""]
    lines.append(
        "Suspend is the cost-lever for reclaiming a sandbox's compute while keeping its identity: "
        "an `operatingMode=Suspended` patch releases the backing Pod but preserves the CR, so a "
        "later `operatingMode=Running` patch resumes it. This block reports how fast that "
        "**administrative** suspend completes — from the patch to the terminal Suspended state "
        "(Pod released + the Suspended condition observed)."
    )
    lines.append("")
    lines.append(
        "_Capability note: this is an **administrative** (operator- or user-driven) suspend. "
        "Upstream agent-sandbox exposes only the closed `operatingMode` enum (`Running`; "
        "`Suspended`) — there is **no idle-timeout, activity-reclaim, or auto-suspend** path, so "
        "this latency must not be read as an automatic scale-to-zero._"
    )
    lines.append("")
    header = ["Suspend latency", "Value"]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")
    lines.append(f"| Median (p50) | {_fmt_secs(susp['suspend_p50'])} |")
    if susp["suspend_p90"] is not None:
        lines.append(f"| Tail (p90) | {_fmt_secs(susp['suspend_p90'])} |")
    lines.append("")
    # The tail row is suppressed when no p90 was emitted (a single-cycle fire); keep the caption's
    # percentile claim in lockstep with the rows actually rendered so prose never promises a tail
    # the table omits.
    pctile_clause = (
        "the median and tail are percentiles"
        if susp["suspend_p90"] is not None
        else "the median is the 50th percentile"
    )
    lines.append(
        "_Suspend latency is measured per-cycle as the wall-clock from the "
        f"`operatingMode=Suspended` patch return to the terminal Suspended state; {pctile_clause} "
        "of the measured suspend distribution._"
    )
    lines.append("")
    return "\n".join(lines)


def _footprint_from_provenance(prov):
    """Return (cpu_m, mem_mib) from a cleaned provenance dict, or None when either is absent.

    Both fields are required — a half-stated footprint is not a reproducible qualifier, so a
    provenance carrying only one renders pending (same all-or-nothing gate the decomposition
    blocks use). `_clean_provenance` has already dropped any value failing its schema predicate,
    so a present key here is a valid non-negative int."""
    if not isinstance(prov, dict):
        return None
    cpu = prov.get("sandbox_cpu_request_m")
    mem = prov.get("sandbox_mem_request_mib")
    if cpu is None or mem is None:
        return None
    return cpu, mem


def render_vcpu_footprint(results, kata_results=None):
    """DETAILS.md deep-dive: the per-sandbox DECLARED cpu/mem request each runtime's density
    was measured under. This is NOT an independent measurement — it is the reproducibility
    qualifier for Max Density: gVisor's tiny footprint and Kata's guest-sane microVM floor are
    an order of magnitude apart, so a sandboxes-per-vCPU figure is only reproducible with the
    footprint it was measured under stated alongside. Same per-runtime source logic as
    render_density_detail: the primary results claim their measured runtime; kata_results (the
    sandbox-kata product) may fill the kata-microvm slot. Returns "" (INERT) until at least one
    runtime carries a footprint in its provenance, so the public page is byte-unchanged until a
    fire emits it. Diagnostic-only — adds a block, changes no existing cell."""
    product = results.get("product")
    if product not in PRODUCTS:
        return ""
    prov = _clean_provenance(results.get("provenance"))
    measured_runtime = prov.get("runtime") or "gvisor"
    footprints = {}
    fp = _footprint_from_provenance(prov)
    if fp is not None:
        footprints[measured_runtime] = fp
    if (
        isinstance(kata_results, dict)
        and kata_results.get("product") == "sandbox-kata"
        and "kata-microvm" not in footprints
    ):
        kp = _clean_provenance(kata_results.get("provenance"))
        if kp.get("runtime") == "kata-microvm":
            kfp = _footprint_from_provenance(kp)
            if kfp is not None:
                footprints["kata-microvm"] = kfp
    if not footprints:
        return ""
    lines = ["## Per-Sandbox Footprint (declared request)", ""]
    lines.append(
        "The declared per-sandbox cpu/memory **request** each runtime's Max Density was measured "
        "under — the reproducibility qualifier for the density figures, not an independent "
        "measurement. gVisor's tiny footprint and Kata's guest-sane microVM floor differ by an "
        "order of magnitude, so a sandboxes-per-vCPU figure is only comparable across runtimes "
        "with the footprint it was measured under stated alongside it. A runtime with no measured "
        "run renders `pending`."
    )
    lines.append("")
    lines.append("| Runtime | CPU request | Memory request |")
    lines.append("|---|---|---|")
    for rt in MATRIX_RUNTIMES:
        rt_fp = footprints.get(rt)
        if rt_fp is None:
            cpu_cell = mem_cell = link_pending(_PENDING)
        else:
            cpu_cell = f"{rt_fp[0]}m"
            mem_cell = f"{rt_fp[1]}Mi"
        lines.append(f"| {RUNTIME_LABELS[rt]} | {cpu_cell} | {mem_cell} |")
    lines.append("")
    return "\n".join(lines) + "\n"


def _clean_cold_bind_decomposition(scenarios):
    """Find native_digest_cold and closed-schema-clean its TTFE decomposition (inch #2).

    The cold twin of _clean_warm_bind_decomposition: same six percentile keys, same
    all-six-required INERT gate (today's pre-decomposition cold data has the ttfe pair
    but not the bind/exec pairs, so this renders nothing until a decomposition-
    instrumented cold fire lands). Scoped to the native_digest_cold scenario. Reuses
    WARM_BIND_FIELDS (identical field set + predicates). Every value is a
    GENUINELY-MEASURED percentile: for the n=1 cold cell, bind is the measured
    create->Ready time and exec is the measured residual (ttfe_ms - bind_ms) against
    the SAME shared t0, NOT a render-side subtraction of percentiles. Any sla_metrics
    key not in WARM_BIND_FIELDS, or failing its predicate, is dropped (closed schema).
    """
    if not isinstance(scenarios, list):
        return None
    for s in scenarios:
        if not isinstance(s, dict) or s.get("name") != "native_digest_cold":
            continue
        metrics = s.get("sla_metrics")
        if not isinstance(metrics, dict):
            return None
        clean = {}
        for key, ok in WARM_BIND_FIELDS.items():
            if key in metrics:
                try:
                    if ok(metrics[key]):
                        clean[key] = metrics[key]
                except (TypeError, ValueError):
                    pass
        needed = (
            "bind_p50_ms", "bind_p95_ms",
            "exec_p50_ms", "exec_p95_ms",
            "ttfe_p50_ms", "ttfe_p95_ms",
        )
        if any(k not in clean for k in needed):
            return None
        return {
            "bind_p50": clean["bind_p50_ms"],
            "bind_p95": clean["bind_p95_ms"],
            "exec_p50": clean["exec_p50_ms"],
            "exec_p95": clean["exec_p95_ms"],
            "ttfe_p50": clean["ttfe_p50_ms"],
            "ttfe_p95": clean["ttfe_p95_ms"],
        }
    return None


def render_cold_bind_decomposition(results):
    """Render the cold-start TTFE provision-vs-exec decomposition (inch #2), or "" when INERT.

    The cold twin of render_warm_bind_decomposition. Cold TTFE (create->first-instruction-
    result) splits into PROVISION (create->Ready: controller reconcile + pod schedule + image
    pull + container start) + EXEC (websocket setup + the first-instruction round-trip on the
    already-Ready sandbox). Unlike the warm case — where a large bind is a surprise worth
    flagging — for cold the provision is EXPECTED to dominate (a cold pull is genuinely slow),
    so this block's diagnostic value is inverted: a *large exec* is the surprise, pointing at
    an exec-channel artifact (websocket setup) rather than the cold provision itself.

    HONESTY: provision, exec, and TTFE are each an INDEPENDENTLY-MEASURED value — for the n=1
    cold cell, provision is the measured create->Ready time, exec is the measured residual
    (ttfe_ms - bind_ms) against the SAME shared t0, and TTFE is the measured total; they are
    NOT a render-side subtraction of percentiles. Rendered ONLY when all of provision, exec,
    AND TTFE keys are present (see _clean_cold_bind_decomposition), so the public page is
    byte-unchanged until a decomposition-instrumented cold fire lands. Diagnostic-only — adds
    a block, changes no existing cell.
    """
    dec = _clean_cold_bind_decomposition(results.get("scenarios"))
    if not dec:
        return ""
    lines = ["## Cold-Start TTFE — Provision vs Exec Decomposition", ""]
    lines.append(
        "Cold-start TTFE (create → first-instruction result) splits into **provision** "
        "(create → Ready: controller reconcile + pod schedule + image pull + container start) "
        "and **exec** (websocket setup + the first-instruction round-trip on the already-Ready "
        "sandbox). For a cold start the provision is *expected* to dominate — a cold image pull "
        "is genuinely slow — so the signal to watch here is a large **exec**, which would point "
        "at the exec channel (a harness/product artifact), not the cold provision itself."
    )
    lines.append("")
    header = ["Stage", "p50", "p95"]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")
    lines.append(
        f"| Provision (create → Ready) | {_fmt_secs(dec['bind_p50'])} | "
        f"{_fmt_secs(dec['bind_p95'])} |"
    )
    lines.append(
        f"| Exec (websocket + first-instruction) | {_fmt_secs(dec['exec_p50'])} | "
        f"{_fmt_secs(dec['exec_p95'])} |"
    )
    lines.append(
        f"| **TTFE (total)** | **{_fmt_secs(dec['ttfe_p50'])}** | **{_fmt_secs(dec['ttfe_p95'])}** |"
    )
    lines.append("")
    lines.append(
        "_Each row is an independently-measured value against the same shared t0 (exec is the "
        "measured residual TTFE − provision, not a subtraction of percentiles). For the "
        "single-sample cold cell the p50 and p95 are the one measured sample._"
    )
    # Drained-regime caveat (#103/#111), data-keyed on provenance.regime so it cannot rot —
    # same posture as the warm block: once an under-load fire emits regime != "drained" this
    # caveat stops rendering by construction. Kept off any measured cell.
    regime = None
    prov = results.get("provenance")
    if isinstance(prov, dict):
        regime = prov.get("regime")
    if regime == "drained":
        lines.append("")
        lines.append(
            "> ⚠️ **Regime caveat:** this cold decomposition was measured on a **drained, "
            "low-contention cluster** (single cold provision, n=1). The split is honest for "
            "THIS fire but wants corroboration under representative load before the "
            "provision/exec ratio is treated as durable."
        )
    lines.append("")
    return "\n".join(lines)


def _flat_verdict(retention):
    """✅/⚠️ flat verdict for a retention ratio; pending when absent.

    ASYMMETRIC framing (v2 lock, PR #28): retention >= ~0.9 reads flat/linear-or-better
    — a superlinear result (>1.0) is a BEAT under the floor-not-ceiling framing, NOT a
    regression, so it must read ✅, not ⚠️. Only retention < ~0.9 reads ⚠️ No: the per-node
    number sagged as the cluster grew, i.e. the controller is the ceiling and the page says so.
    """
    if retention is None:
        return _PENDING
    return "✅ Yes" if retention >= 0.9 else "⚠️ No"


def _per_step_retention_line(points, key, noun):
    """Per-step retention (the delta) for `key` plus a convergence read.

    The Scale-Proof table's column is the ENDPOINT ratio (val@maxN / val@minN).
    That averages a single mid-sweep sag into the whole span, so a 1→2 hold
    followed by a 2→4 collapse reads the same as a uniform gentle decay. This
    subline exposes each ADJACENT step (1→2, 2→4) so the shape of the decay is
    visible — derived from the same scale_points the table already uses.

    THREE-WAY convergence read (fixes the slow-uniform-decay wording gap,
    PR #54 fast-follow): a per-step ⚠️ is not the only way the endpoint sags. Each
    step can sit within tolerance (every step ✅, ≥0.9) yet COMPOUND to an endpoint
    below 0.9 — the table reads ⚠️ No while the steps all read ✅, an apparent
    contradiction. So:
      • any step < 0.9            → "sags mid-sweep" (a visible single-step sag)
      • all steps ≥ 0.9 but the endpoint (val@maxN/val@minN) < 0.9
                                  → "within tolerance each step but compounds to an
                                     endpoint sag" (reconciles the ⚠️ table cell)
      • all steps ≥ 0.9 and endpoint ≥ 0.9
                                  → "holds flat step-to-step"

    Returns "" for a sweep with fewer than two steps (a single step IS the endpoint
    ratio, so per-step would just restate the table) or when no step is measurable
    (a zero base everywhere). Same asymmetric ≥0.9 threshold as _flat_verdict: a
    superlinear step is a beat (✅), only a sag below 0.9 reads ⚠️.
    """
    if len(points) < 3:
        return ""
    steps = []
    all_flat = True
    measurable = False
    for prev, cur in zip(points, points[1:]):
        base = prev.get(key)
        label = f"{prev['node_count']}→{cur['node_count']}"
        if not base:
            steps.append(f"{label} {_PENDING}")
            continue
        cur_val = cur.get(key)
        if cur_val is None:
            steps.append(f"{label} {_PENDING}")
            continue
        ratio = cur_val / base
        measurable = True
        mark = "✅" if ratio >= 0.9 else "⚠️"
        if ratio < 0.9:
            all_flat = False
        steps.append(f"{label} {mark} {_fmt_ratio(ratio)}")
    if not measurable:
        return ""
    if not all_flat:
        read = "sags mid-sweep"
    else:
        base0, valN = points[0].get(key), points[-1].get(key)
        endpoint = valN / base0 if base0 else None
        if endpoint is not None and endpoint < 0.9:
            read = "within tolerance each step but compounds to an endpoint sag"
        else:
            read = "holds flat step-to-step"
    return f"_Per-step {noun} retention: " + " · ".join(steps) + f" — {read}._"


def _clean_scale_proof(results):
    """Closed-schema-validate the scale_proof object. Requires scale_points; retentions are
    optional (density_retention derives from the points if absent). None ⇒ no table."""
    sp = results.get("scale_proof")
    if not isinstance(sp, dict):
        return None
    pts = sp.get("scale_points")
    try:
        if not SCALE_PROOF_FIELDS["scale_points"](pts):
            return None
    except (TypeError, ValueError):
        return None
    points = sorted(pts, key=lambda p: p["node_count"])

    def _ratio(key):
        v = sp.get(key)
        try:
            return v if SCALE_PROOF_FIELDS[key](v) else None
        except (TypeError, ValueError):
            return None

    dens_ret = _ratio("density_retention")
    if dens_ret is None and points and points[0]["density"]:
        dens_ret = points[-1]["density"] / points[0]["density"]
    ma = sp.get("measured_at")
    try:
        measured_at = ma if SCALE_PROOF_FIELDS["measured_at"](ma) else None
    except (TypeError, ValueError):
        measured_at = None
    mt = sp.get("machine_type")
    try:
        machine_type = mt if SCALE_PROOF_FIELDS["machine_type"](mt) else None
    except (TypeError, ValueError):
        machine_type = None
    return {
        "points": points,
        "density_retention": dens_ret,
        "thpt_retention": _ratio("thpt_retention"),
        "measured_at": measured_at,
        "machine_type": machine_type,
    }


def render_scale_proof(results, heading="## Scale Proof (Linearity Check)"):
    """Render the doc's Scale Proof (Linearity Check) table, or "" when no scale_proof present.

    Proof that per-node throughput + density hold flat as the cluster grows — the linearity the
    doc's second table asserts. Retention >= ~0.9 reads ✅ (flat or a superlinear beat); only a
    sag below ~0.9 reads ⚠️ (controller-is-ceiling). See _flat_verdict for the asymmetric framing.

    hb#134: `heading` is overridable so the combined "Does it hold at cluster scale?" section
    (render_cluster_scale) can demote this to a `###` sub-block; default keeps the standalone `##`.
    """
    sp = _clean_scale_proof(results)
    if not sp:
        return ""
    nodes = " → ".join(str(p["node_count"]) for p in sp["points"])
    dens_seq = " → ".join(_fmt_num(p["density"]) for p in sp["points"])
    # Surface the retention RATIO behind each verdict so the ✅/⚠️ is falsifiable against the
    # documented 0.9 flat-threshold (a reader sees 0.15 < 0.9 ⇒ ⚠️, 0.98 ≥ 0.9 ⇒ ✅) rather than
    # a bald token whose earning number is hidden. Before this, the throughput leg rendered a
    # verdict-only "⚠️ No" with NO figure — the worse-performing leg disclosed LESS than the
    # passing density leg (which already shows its per-point sequence), an unfalsifiable claim.
    # Density keeps its per-point sequence (the decay shape); throughput has no reliable per-point
    # series in the emit (often absent — see the stale live scale data), so the ratio is the
    # number it can honestly show. Both read the already-present retention keys — no schema change.
    dens_verdict = _flat_verdict(sp["density_retention"])
    if sp["density_retention"] is not None:
        dens_verdict += f" ({_fmt_ratio(sp['density_retention'])}× · {dens_seq})"
    thpt_verdict = _flat_verdict(sp["thpt_retention"])
    if sp["thpt_retention"] is not None:
        thpt_verdict += f" ({_fmt_ratio(sp['thpt_retention'])}×)"

    header = ["Nodes Tested", "Density Holds Flat?", "Throughput Holds Flat?"]
    lines = [heading, ""]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")
    lines.append(
        "| " + " | ".join([nodes, link_pending(dens_verdict), link_pending(thpt_verdict)]) + " |"
    )
    lines.append("")
    # hb#142.3: the density figures in the sequence above are the per-node density RETAINED at
    # each node count (a linearity series — does per-node density hold flat as nodes grow?), a
    # DIFFERENT denominator than the absolute Max Density per vCPU (reported in DETAILS). Label
    # it so a reader does not read this retention series against the Max-Density figure.
    lines.append(
        "_The density values in this row are the per-node density retained at each node count "
        "(a linearity series — does per-node density stay flat as the cluster grows?), not the "
        "absolute Max Density per vCPU (reported separately in DETAILS)._"
    )
    lines.append("")
    for key, noun in (("density", "density"), ("throughput", "throughput")):
        step_line = _per_step_retention_line(sp["points"], key, noun)
        if step_line:
            lines.append(step_line)
            lines.append("")
    # Dated subline (#3952): the Scale Proof is a point-in-time multi-node sweep,
    # carried forward across the daily single-node refresh — so it carries its own
    # measured date, honestly distinct from the page's daily-refreshed timestamp.
    if sp.get("measured_at"):
        lines.append(
            f"_Measured {sp['measured_at'][:10]} — node-count linearity sweep "
            "(point-in-time; refreshed on the next multi-node sweep)._"
        )
        lines.append("")
    # hb#(gap-2, a4z1 goal-2.1 display-vs-spec audit): Scale Proof has no daily in-process
    # producer (same shape as concurrent_burst/at_scale_contention/cluster_saturation, which
    # already carry this banner) — a rig change since the last multi-node sweep must be
    # disclosed the same way those sibling sections already disclose it.
    stale_caveat = _stale_carry_forward_caveat(
        sp, _clean_provenance(results.get("provenance")), label="scale-proof")
    if stale_caveat:
        lines.append(stale_caveat)
        lines.append("")
    return "\n".join(lines)


# Public semantic labels for the warm-vs-cold legs. TTFE is the page's headline metric
# (executed first-instruction, returned a result); TTFI is the weaker "accepted" claim.
# Out-of-enum semantics never reach here — the closed-schema predicate drops the block.
_SEMANTIC_LABELS = {
    "ttfe": "TTFE (executed first-instruction)",
    "ttfi": "TTFI (first-instruction accepted)",
}


def _clean_warm_vs_cold(results):
    """Closed-schema-validate the TOP-LEVEL warm_vs_cold object (#3954 sibling).

    Returns the cleaned dict ONLY when every REQUIRED field (warm_p50_ms, cold_ms, speedup,
    semantic, runtime_class) is present and passes its predicate AND warm_p50_ms/cold_ms are
    strictly positive (a zero leg makes the ratio undefined); None otherwise (⇒ INERT). n_warm
    is optional. runtime_class is validated against the PUBLIC RUNTIME_LABELS enum, so a
    free-text or out-of-enum runtime fails closed and drops the whole block.
    """
    wc = results.get("warm_vs_cold")
    if not isinstance(wc, dict):
        return None
    clean = {}
    for key, ok in WARM_VS_COLD_FIELDS.items():
        if key in wc:
            try:
                if ok(wc[key]):
                    clean[key] = wc[key]
            except (TypeError, ValueError):
                pass
    for req in ("warm_p50_ms", "cold_ms", "speedup", "semantic", "runtime_class"):
        if req not in clean:
            return None
    if clean["warm_p50_ms"] <= 0 or clean["cold_ms"] <= 0:
        return None
    # cold_start_mode (#4024) is OPTIONAL but NOT silently-droppable: a present-but-invalid
    # mode (e.g. a typo "cold-provison") must fail the block CLOSED rather than fall through
    # to the true-cold default phrasing, which would silently over-claim unique-image. The
    # validation loop above only adds it to `clean` when valid, so "in wc but not in clean"
    # == present-but-invalid ⇒ INERT. Absent stays valid (⇒ true-cold default).
    if "cold_start_mode" in wc and "cold_start_mode" not in clean:
        return None
    return clean


# Public cold-leg phrasing keyed by cold_start_mode (#4024). The warm-vs-cold cold leg can
# be a true unique-image cold pull (cold-pull, the locked Framing-A native_digest_cold leg)
# or a warm-pool-overflow fresh-node provision off the SHARED base image (cold-provision) —
# NOT the same cost, so an overflow provision must never claim "unique-image". Each entry
# supplies the three public surfaces the cold semantic touches: the table leg label, the
# headline cold-descriptor, and the mechanism sentence. Absent ⇒ _COLD_LEG_DEFAULT, which is
# byte-identical to the pre-#4024 hardcoded true-cold phrasing, so the existing locked block
# + its tests are unchanged (graceful degradation, mirrors _measured_cell).
_COLD_LEG = {
    "cold-pull": {
        "leg": "True-cold (unique-image)",
        "descriptor": "a true-cold start",
        "mechanism": ("The warm pool keeps a ready slot so a claim skips the fresh-node "
                      "image-pull path a cold start pays in full."),
    },
    "cold-provision": {
        "leg": "Cold-provision (node overflow)",
        "descriptor": "a cold-provision start (warm-pool overflow)",
        "mechanism": ("The warm pool keeps a ready slot so a claim skips the fresh-node "
                      "provisioning path an overflow claim pays when the pool is exhausted "
                      "— provisioning off the SHARED base image (one node-cacheable image, "
                      "NOT a unique image per claim)."),
    },
}
_COLD_LEG_DEFAULT = _COLD_LEG["cold-pull"]


def render_warm_vs_cold(results):
    """Render the warm-vs-cold speedup block (#3954 sibling), or "" when INERT.

    Composes the warm leg (warm-pool TTFx p50) and the true-cold leg (unique-image cold) into
    ONE honest headline a reader can quote: warm provisioning is N times faster than cold. INERT
    (returns "") until the harness emits a complete, closed-schema-clean warm_vs_cold object —
    the classifier itself fails closed if the two legs ever diverge in semantic or runtime class.

    Since hb#134 this block lives only in the DETAILS deep-dive appendix (the standalone
    headline was folded off the README page); it renders the full leg-by-leg table.
    """
    wc = _clean_warm_vs_cold(results)
    if not wc:
        return ""
    rt_label = RUNTIME_LABELS[wc["runtime_class"]]
    sem_label = _SEMANTIC_LABELS[wc["semantic"]]
    # Recompute the displayed ratio from the two displayed legs rather than printing the
    # emitter's `speedup` verbatim, so the headline/table/footnote can never contradict the
    # legs shown beside them (the footnote literally claims "computed from the displayed
    # values"). The legs are strictly-positive-gated in _clean_warm_vs_cold, so the ratio is
    # always defined and positive — this also closes an emitter speedup<=0.
    speedup = _fmt_num(wc["cold_ms"] / wc["warm_p50_ms"])
    cold = _COLD_LEG.get(wc.get("cold_start_mode"), _COLD_LEG_DEFAULT)
    # Small-sample comparability guard (#414 sibling): the speedup is a cross-sample RANKING of
    # the warm leg against the cold leg, so it inherits the same N>=TTFE_COMPARABILITY_MIN_N floor
    # the matrix p95 cells and the build-over-build trend Δ already use. When the warm leg's p50
    # rests on fewer than the floor's worth of claims the ratio may be sampling noise, so we (a)
    # dagger the ratio wherever it is quoted and (b) swap the headline's portability CLAIM for an
    # explicit provisional caveat — a "reproduce it on your own cluster" promise is exactly what an
    # under-sampled ratio cannot keep. n_warm absent ⇒ sample size unknown ⇒ no marker (we mark
    # KNOWN-low n, never an unknown as if it were low; mirrors the matrix's n-present gate).
    low_n = "n_warm" in wc and wc["n_warm"] < TTFE_COMPARABILITY_MIN_N
    mark = f" {_LOW_N_MARK}" if low_n else ""
    portability = (
        f"but this ratio rests on only n={wc['n_warm']} warm claims — fewer than "
        f"N={TTFE_COMPARABILITY_MIN_N}, too few to rank reliably, so treat it as provisional."
        if low_n else
        "the ratio is the portable headline you can reproduce on your own cluster."
    )
    lines = ["## Warm-vs-Cold Speedup", ""]
    lines.append(
        f"A warm-pool provision is **{speedup}× faster**{mark} than {cold['descriptor']} "
        f"({rt_label}). {cold['mechanism']} Both legs are measured the same way "
        f"({sem_label}); {portability}")
    lines.append("")
    header = ["Leg", _SEMANTIC_LABELS[wc["semantic"]].split(" ")[0] + " (p50)"]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")
    # #103: carry the warm sample size INLINE in the leg label so this row cannot be visually
    # conflated with the Core Metrics matrix "Warm-pool hit (Base image)" row — that is the
    # dedicated warmpool_cold_start scenario (its own N in the matrix's Samples column); THIS
    # is the point-in-time warm-vs-cold pair's warm leg at its own n. Two different scenarios,
    # two different operating points — the inline n (data-keyed off wc, so it cannot rot)
    # plus the cross-block caveat below make that unambiguous to a reader scanning both tables.
    warm_n = f", n={wc['n_warm']}" if "n_warm" in wc else ""
    lines.append(f"| Warm-pool hit ({rt_label}{warm_n}) | {_fmt_secs(wc['warm_p50_ms'])} |")
    lines.append(f"| {cold['leg']} | {_fmt_secs(wc['cold_ms'])} |")
    lines.append(f"| Speedup (warm is N× faster) | {speedup}×{mark} |")
    lines.append("")
    n_note = f" over n={wc['n_warm']} warm claims" if "n_warm" in wc else ""
    lines.append(
        f"_Speedup = cold ÷ warm, computed from the displayed values{n_note}; both legs are "
        "medians (p50) — the warm leg over its warm-pool claims and the cold leg over the "
        "true-cold distribution — so half of warm claims and half of cold starts run slower "
        "than the values shown._")
    lines.append("")
    if low_n:
        lines.append(
            f"_{_LOW_N_MARK} The warm leg's p50 is drawn from only n={wc['n_warm']} claims — "
            f"fewer than the N={TTFE_COMPARABILITY_MIN_N} sample floor the page uses across its "
            "cross-sample rankings (matrix p95 cells, build-over-build trend Δ); the speedup may "
            "be sampling noise, not a stable ratio you can reproduce._")
        lines.append("")
    # Machine-class-change caveat (PR#313 review): data-keyed off the SAME
    # run-level provenance the build banner stamps (see _machine_class_caveat) — renders
    # only when this run's rig differs from the prior published run's, so a speedup delta
    # (e.g. 9.98x -> 5.50x) reads as machine-class-confounded rather than a substrate signal.
    machine_caveat = _machine_class_caveat(_clean_provenance(results.get("provenance")))
    if machine_caveat:
        lines.append(machine_caveat)
        lines.append("")
    # Cross-block coherence caveat (#103): this warm-vs-cold pair is its own
    # point-in-time run at its own operating point — NOT the same measurement as the
    # Core Metrics matrix "Warm-pool hit" row. A reader comparing the two warm p50s
    # across blocks must not read a divergence as a contradiction. Static prose (no numbers,
    # no sample-size comparison) so it can never rot against either block's independent
    # refresh cadence or either block's n.
    lines.append(
        "_This warm-vs-cold pair is a standalone point-in-time run; its warm-pool leg is a "
        "separate measurement from the Core Metrics matrix \"Warm-pool hit\" row (an "
        "independent run at its own operating point, refreshed on its own cadence). Read "
        "each block on its own terms — the two warm p50s are not directly comparable._")
    lines.append("")
    # Dated subline (mirrors scale_proof #3952): the warm-vs-cold pair is a point-in-time
    # measurement carried forward across the daily refresh, so it carries its own measured
    # date, honestly distinct from the page's daily-refreshed timestamp.
    if wc.get("measured_at"):
        lines.append(
            f"_Measured {wc['measured_at'][:10]} — warm-vs-cold speedup "
            "(point-in-time; refreshed on the next TTFE fire)._"
        )
        lines.append("")
    return "\n".join(lines)


# Optional Kata fields that fail the block CLOSED when present-but-invalid (mirrors warm_vs_cold's
# cold_start_mode handling): a typo'd hypervisor / a registry-path image / a free-text resume must
# never publish, so "present in raw but not in clean" ⇒ INERT for these. (Required fields are
# enforced by the separate required-loop below.) Kept in sync with the emitter coercer's fail-closed
# optionals; a drift is caught by the cross-contract test.
_KATA_FAIL_CLOSED_OPTIONALS = ("warm_image", "hypervisor", "resume_status", "kata_version")


def _clean_kata_activation(results):
    """Closed-schema-validate the TOP-LEVEL kata_activation object (#3942).

    Returns the cleaned dict ONLY when every REQUIRED field (runtime_class, microvm_activation_ms,
    warm_ready_ms, cold_ready, guest_kernel, host_kernel) is present and passes its predicate; None
    otherwise (⇒ INERT). The optional enum/shape fields fail the block CLOSED when present-but-invalid
    (a typo'd hypervisor / registry-path image / free-text resume never publishes), mirroring
    warm_vs_cold's cold_start_mode posture.
    """
    ka = results.get("kata_activation")
    if not isinstance(ka, dict):
        return None
    clean = {}
    for key, ok in KATA_ACTIVATION_FIELDS.items():
        if key in ka:
            try:
                if ok(ka[key]):
                    clean[key] = ka[key]
            except (TypeError, ValueError):
                pass
    for req in ("runtime_class", "microvm_activation_ms", "warm_ready_ms",
                "cold_ready", "guest_kernel", "host_kernel"):
        if req not in clean:
            return None
    # Present-but-invalid optional enum/shape ⇒ fail closed (over-claim guard).
    for opt in _KATA_FAIL_CLOSED_OPTIONALS:
        if opt in ka and opt not in clean:
            return None
    return clean


def render_kata_activation(results):
    """Render the Kata+microVM activation block (#3942), or "" when INERT.

    Publishes Kata pod-Ready / microVM-activation latency. This is DELIBERATELY NOT the TTFE the
    Core Metrics matrix keys on (executed first-instruction + returned a result): the matrix TTFE
    cells for Kata stay honestly `pending`, and the caption restates the distinction so a reader
    cannot read these Ready numbers as TTFE or compare them against the gVisor TTFE columns. The
    resume cell reads N/A — upstream-blocked (CRIU resume not wired upstream, #3097), a genuine
    upstream gap rather than an unrun or failed test. INERT (returns "") until the harness emits a
    complete, closed-schema-clean kata_activation object.
    """
    ka = _clean_kata_activation(results)
    if not ka:
        return ""
    rt_label = RUNTIME_LABELS[ka["runtime_class"]]
    lines = ["## Kata + microVM Activation (pod-Ready — NOT TTFE)", ""]
    caption = (
        f"These are **{rt_label} pod-Ready / microVM-activation** latencies — the time to bring "
        "the guest microVM up and the pod Ready. They are **not TTFE** (the Core Metrics matrix's "
        "executed-first-instruction-and-returned-a-result metric), so they are **not comparable "
        "to the matrix TTFE columns**. For the Kata TTFE itself, read the matrix TTFE cells: they "
        "report it where a TTFE probe has run under Kata, and `pending` where one has not."
    )
    meta = []
    if ka.get("hypervisor"):
        meta.append(f"hypervisor **{ka['hypervisor']}**")
    if ka.get("kata_version"):
        meta.append(f"Kata **{ka['kata_version']}**")
    meta.append(f"guest kernel `{ka['guest_kernel']}`")
    meta.append(f"host kernel `{ka['host_kernel']}`")
    n_note = f", n={ka['n']}" if "n" in ka else ""
    lines.append(caption + f" Measured on {', '.join(meta)}{n_note}.")
    lines.append("")
    header = ["Phase", "Pod-Ready latency"]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")
    warm_img = f" ({ka['warm_image']})" if ka.get("warm_image") else ""
    lines.append(f"| microVM activation | {_fmt_secs(ka['microvm_activation_ms'])} |")
    lines.append(f"| Warm-pool hit{warm_img} | {_fmt_secs(ka['warm_ready_ms'])} |")
    for e in ka["cold_ready"]:
        pull = f" (image pull {_fmt_secs(e['image_pull_ms'])})" if "image_pull_ms" in e else ""
        lines.append(f"| Cold start — {e['image']}{pull} | {_fmt_secs(e['ready_ms'])} |")
    # Resume row under Kata + microVM: N/A by construction, matching the Core Metrics
    # matrix (hb#172 unification). The benchmark's resume metric is CRIU-based, and CRIU
    # checkpoint/restore does not transfer to the Kata VM isolation model — so THIS metric
    # can never be measured under Kata (a hypothetical VM-snapshot resume would be a
    # different cell, not this one graduating). `upstream-blocked` scopes to gVisor only,
    # where the run lands but the Suspended condition never clears. Same anchor as the
    # matrix so both public surfaces tell one story (hb#166: WIP-linked, no bare issue ref).
    resume_cell = wip_link(
        NA_BY_CONSTRUCTION,
        "N/A — CRIU checkpoint/restore does not transfer to the Kata VM model",
    )
    lines.append(f"| Snapshot resume | {resume_cell} |")
    lines.append("")
    if ka.get("measured_at"):
        lines.append(
            f"_Measured {ka['measured_at'][:10]} — Kata pod-Ready / microVM-activation "
            "(point-in-time; not TTFE)._"
        )
        lines.append("")
    return "\n".join(lines)


# --- #4021: concurrent-burst sweep render -------------------------------------------------
# The Core Metrics matrix and the step-up table both report a per-SECOND creation RATE (sandboxes
# launched per second, ramped). This block reports the complementary axis that was asked for: a
# single ALL-AT-ONCE burst of N concurrent claims (300/500), warm-pool vs cold-provision. Same TTFE
# honesty spine as the matrix (executed-first-instruction-and-returned-a-result), so the numbers are
# directly comparable to the matrix TTFE columns. INERT (returns "") until the harness emits a
# complete, closed-schema-clean concurrent_burst object.


def _clean_concurrent_burst(results):
    """Closed-schema-validate the TOP-LEVEL concurrent_burst object (#4021). None ⇒ INERT.

    Returns the cleaned dict ONLY when the REQUIRED `legs` list is present and every leg passes
    its predicate (n, mode, ttfe_p50_ms, ttfe_p95_ms required per leg; throughput + exec fractions
    optional). Optional provenance scalars (node_count, machine_type, measured_at) render only when
    valid; a present-but-invalid one is dropped on read, never fabricated.
    """
    cb = results.get("concurrent_burst")
    if not isinstance(cb, dict):
        return None
    clean = {}
    for key, ok in CONCURRENT_BURST_FIELDS.items():
        if key in cb:
            try:
                if ok(cb[key]):
                    clean[key] = cb[key]
            except (TypeError, ValueError):
                pass
    if "legs" not in clean:
        return None
    return clean


_CONCURRENT_BURST_MODE_LABELS = {
    "warm": "Warm pool",
    "cold": "Cold provision",
}


def _cb_thpt_cell(leg, key):
    """Throughput-per-node cell: the value as-is (compact), or em-dash when the leg omits it —
    honest "not measured", never a fabricated 0."""
    if key in leg:
        return _fmt_num(leg[key])
    return "—"


_CONCURRENT_BURST_EXPLICIT_REGIME_NOTES = {
    "prewarmed": (
        "> ℹ️ **Measurement regime:** this burst ran on a long-lived, **pre-warmed cluster** "
        "(warm containerd cache), independently of when it fired. Not directly comparable to an "
        "**ephemeral CI cluster** row above/below — a TTFE gap between differently-regimed rows "
        "is at least partly a regime artifact, not a workload difference."
    ),
    "ephemeral_ci": (
        "> ℹ️ **Measurement regime:** this burst ran on a cold, single-fire **ephemeral CI "
        "cluster** (empty containerd cache; node-autoscaler + image-pull in the critical path), "
        "independently of when it fired. Not directly comparable to a **pre-warmed cluster** row "
        "above/below — a TTFE gap between differently-regimed rows is at least partly a regime "
        "artifact, not a workload difference."
    ),
}


def _concurrent_burst_regime_note(cb):
    """Measurement-regime disclosure for the concurrent-burst block (#4021 pre-stage).

    A concurrent-burst fire records its cluster regime implicitly via `measured_at`: fires on or
    after the 2026-07-20 ephemeral-CI cutover (_EPHEMERAL_CI_CUTOVER) run on a cold, single-fire
    ephemeral CI cluster (empty containerd cache, node-autoscaler + image-pull in the critical
    path); earlier fires ran on a long-lived, pre-warmed cluster (warm cache). Cross-regime the
    two are NOT directly comparable — a TTFE gap is at least partly a regime artifact, not a
    workload difference. This note makes that explicit so a reader never diffs a post-cutover
    300→500-concurrent row against a pre-2026-07-20 warm-persistent baseline as apples-to-apples.

    Both polarities are surfaced (post-cutover cold-ephemeral AND pre-cutover warm-persistent) so
    the disclosure is symmetric regardless of which regime the fire landed in.

    Fail-safe: an absent or non-date-shaped `measured_at` returns "" (no fabricated regime claim),
    mirroring the _ISO-guarded pattern in _runtime_choice_clause. `measured_at` may be a full ISO
    stamp or a bare YYYY-MM-DD (per CONCURRENT_BURST_FIELDS), so we validate only the date prefix
    without importing `re`.

    OPTIONAL `cluster_regime` override (#5474): the date-cutover inference above is a PROXY — it
    only holds for fires that actually ran through honest-bench's own CI harness. A leg fired
    out-of-band against a known cluster (e.g. a manual low-N true-per-sandbox fire against a
    genuinely long-lived, pre-warmed internal cluster, dated after the cutover) states its TRUE
    regime explicitly via this key, which takes precedence over the date inference entirely —
    the proxy never overrides a known fact.
    """
    explicit = cb.get("cluster_regime")
    if explicit in _CONCURRENT_BURST_EXPLICIT_REGIME_NOTES:
        return _CONCURRENT_BURST_EXPLICIT_REGIME_NOTES[explicit]
    ma = cb.get("measured_at")
    if not isinstance(ma, str):
        return ""
    day = ma[:10]
    if len(day) != 10 or day[4] != "-" or day[7] != "-":
        return ""
    if not (day[:4].isdigit() and day[5:7].isdigit() and day[8:10].isdigit()):
        return ""
    if day >= _EPHEMERAL_CI_CUTOVER[:10]:
        return (
            "> ℹ️ **Measurement regime:** this burst ran on a cold, single-fire **ephemeral CI "
            "cluster** (empty containerd cache; node-autoscaler + image-pull in the critical "
            "path). It is **not directly comparable to pre-2026-07-20 warm-persistent baselines** "
            "— a TTFE gap against an earlier long-lived-cluster run is at least partly a regime "
            "artifact, not a workload difference."
        )
    return (
        "> ℹ️ **Measurement regime:** this burst ran on a long-lived, **pre-warmed cluster** "
        "(warm containerd cache). Fires on or after 2026-07-20 run on cold ephemeral CI clusters "
        "and are **not directly comparable** to this baseline."
    )


def render_concurrent_burst(results, heading="## Concurrent Burst — TTFE at N simultaneous claims"):
    """Render the concurrent-burst sweep block (#4021), or "" when INERT.

    Publishes a single all-at-once burst of N concurrent claims (the complement to the per-second
    rate the matrix/step-up report), warm-pool vs cold-provision, on the SAME TTFE spine as the
    Core Metrics matrix — so the TTFE columns ARE comparable to the matrix. INERT until the harness
    emits a closed-schema-clean concurrent_burst object.

    hb#134: `heading` is overridable so the combined "Does it hold at cluster scale?" section
    (render_cluster_scale) can demote this to a `###` sub-block; default keeps the standalone `##`.
    """
    cb = _clean_concurrent_burst(results)
    if not cb:
        return ""
    lines = [heading, ""]
    caption = (
        "Each row is a **single all-at-once burst of N concurrent claims** (not a ramped "
        "per-second rate). TTFE is the same metric the Core Metrics matrix reports "
        "(executed-first-instruction-and-returned-a-result), so these columns **are comparable "
        "to the matrix TTFE columns**. *Warm pool* fires against a pre-provisioned pool of N "
        "ready sandboxes; *cold provision* starts from an empty pool (node-autoscaler + image-pull "
        "in the critical path)."
    )
    meta = []
    if cb.get("node_count") is not None:
        meta.append(f"node_count={cb['node_count']}")
    if cb.get("machine_type"):
        meta.append(f"`{cb['machine_type']}`")
    if meta:
        caption += f" Measured on {', '.join(meta)}."
    lines.append(caption)
    lines.append("")
    header = [
        "Concurrency (N)", "Activation Mode", "TTFE p50", "TTFE p95",
        "Throughput @ <5s/node", "Throughput @ <1s/node", "Execution Success",
    ]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")
    for leg in cb["legs"]:
        mode_label = _CONCURRENT_BURST_MODE_LABELS.get(leg["mode"], leg["mode"])
        exec_cell = (
            _exec_cell(leg["exec_success_rate"], leg["n"])
            if "exec_success_rate" in leg else "—"
        )
        row = [
            _fmt_num(leg["n"]),
            mode_label,
            _fmt_secs(leg["ttfe_p50_ms"]),
            _fmt_secs(leg["ttfe_p95_ms"]),
            _cb_thpt_cell(leg, "thpt_under_5s_per_node"),
            _cb_thpt_cell(leg, "thpt_under_1s_per_node"),
            exec_cell,
        ]
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    lines.extend(_concurrent_burst_provenance_lines(cb))
    stale_caveat = _stale_carry_forward_caveat(
        cb, _clean_provenance(results.get("provenance")), label="concurrent-burst")
    if stale_caveat:
        lines.append(stale_caveat)
        lines.append("")
    return "\n".join(lines)


def _concurrent_burst_provenance_lines(cb):
    """Per-date-group measured-at + regime disclosure (#5474).

    Legs normally share one fire date (the block-level `measured_at`), in which case this
    renders identically to the original single-line + single-note shape. When a leg carries its
    OWN `measured_at` override (a later low-N leg landed alongside an earlier N=300/500 burst),
    legs are grouped by their EFFECTIVE date (leg override, else the block scalar) and each group
    gets its own dated subline + regime note, scoped to the legs it covers — so a reader is never
    told a single date/regime applies to rows fired on different days (the mixed-basis trust-
    surface problem this repo's own idiom forbids; see PR hb#359).
    """
    block_date = cb.get("measured_at")
    block_regime = cb.get("cluster_regime")
    groups = []  # [((date_or_None, regime_or_None), [leg, ...]), ...], first-seen order
    for leg in cb["legs"]:
        date = leg.get("measured_at") or block_date
        regime = leg.get("cluster_regime") or block_regime
        key = (date, regime)
        for g_key, g_legs in groups:
            if g_key == key:
                g_legs.append(leg)
                break
        else:
            groups.append((key, [leg]))

    lines = []
    single_group = len(groups) <= 1
    for (date, regime), legs in groups:
        if not date:
            continue
        if single_group:
            lines.append(f"_Measured {date[:10]} — concurrent-burst TTFE (point-in-time)._")
        else:
            covers = ", ".join(
                f"N={leg['n']} {_CONCURRENT_BURST_MODE_LABELS.get(leg['mode'], leg['mode'])}"
                for leg in legs
            )
            lines.append(
                f"_Measured {date[:10]} — concurrent-burst TTFE (point-in-time): {covers}._"
            )
        lines.append("")
        regime_note = _concurrent_burst_regime_note({"measured_at": date, "cluster_regime": regime})
        if regime_note:
            lines.append(regime_note)
            lines.append("")
    return lines


def render_concurrent_burst_chart(results):
    """Render the concurrent-burst N-vs-TTFE sweep as a Unicode block-bar chart (WS2, epic
    #6669: "TTFE-vs-concurrency curve"), or "" when INERT (no legs).

    Visual companion to render_concurrent_burst's table: same source (_clean_concurrent_burst),
    so the chart can never show a leg the table itself doesn't. One (N, mode) leg becomes one
    p50/p95 bar pair, letting a reader see the curve's shape — how TTFE grows with concurrency,
    and the warm-vs-cold gap at each N — at a glance, the same way render_ttfe_bars turns the
    Core Metrics matrix's per-runtime row into a bar pair.

    Legs are sorted by (n, mode) — concurrency ascending, warm before cold within each N — not
    left in fire order (the table's own order, which interleaves N=300/500/30 as legs landed).
    A "curve" is only legible monotonic-in-N; the table beneath already carries the per-leg
    dated/regime captions, so re-ordering the chart alone does not lose any disclosure the
    table provides.

    Plain code-block Unicode bars, not mermaid xychart-beta (same GitHub-support rationale as
    render_density_bars/render_ttfe_bars: docs confirm pie/flow/sequence, not xychart-beta).
    """
    cb = _clean_concurrent_burst(results)
    if not cb or not cb["legs"]:
        return ""
    legs = sorted(
        cb["legs"],
        key=lambda leg: (leg["n"], {"warm": 0, "cold": 1}.get(leg["mode"], 2)),
    )
    rows = [
        (f"N={_fmt_num(leg['n'])} {_CONCURRENT_BURST_MODE_LABELS.get(leg['mode'], leg['mode'])}",
         leg["ttfe_p50_ms"], leg["ttfe_p95_ms"])
        for leg in legs
    ]
    label_width = max(len(label) for label, _, _ in rows)
    max_val = max(max(p50, p95) for _, p50, p95 in rows)
    if max_val <= 0:
        return ""
    lines = ["```", "Concurrent Burst — TTFE p50 vs p95 by concurrency (N)", ""]
    for label, p50, p95 in rows:
        row_label = label.ljust(label_width)
        p50_bar = "█" * max(1, round(p50 / max_val * _TTFE_BAR_WIDTH))
        p95_bar = "█" * max(1, round(p95 / max_val * _TTFE_BAR_WIDTH))
        lines.append(f"{row_label} p50  {p50_bar} {_fmt_secs(p50)}")
        lines.append(f"{' ' * label_width} p95  {p95_bar} {_fmt_secs(p95)}")
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


def render_cluster_scale(results):
    """hb#134: the combined "Does it hold at cluster scale?" headline section.

    Merges the two cluster-scale questions a non-infra reader actually has — does per-node
    throughput/density stay flat as nodes grow (linearity, render_scale_proof) and what does a
    single all-at-once burst of N claims cost (concurrency, render_concurrent_burst) — under one
    user-facing question, with the two tables demoted to `###` sub-blocks. Each sub-block stays
    independently closed-schema INERT (an absent one simply doesn't render); the wrapper heading +
    intro appear ONLY when at least one sub-block is present, so the section degrades to nothing
    rather than an empty header. Same page-split discipline as render_warm_vs_cold/at_scale.
    """
    scale = render_scale_proof(
        results, heading="### Linearity — throughput and density hold flat as nodes grow")
    burst = render_concurrent_burst(
        results, heading="### Concurrent burst — TTFE at N simultaneous claims")
    burst_chart = render_concurrent_burst_chart(results)
    saturation = render_cluster_saturation(
        results, heading="### Saturation — the whole-cluster warm-hand-out ceiling")
    contention = render_at_scale_contention(
        results, page_heading="### Where it breaks — an over-subscribed pool")
    if (not scale.strip() and not burst.strip() and not burst_chart.strip()
            and not saturation.strip() and not contention.strip()):
        return ""
    lines = ["## Does it hold at cluster scale?", ""]
    lines.append(
        "Four questions a bigger cluster raises: does throughput stay flat as you add nodes "
        "(**linearity**), what does a single all-at-once burst of N claims cost (**concurrency**), "
        "where does the whole-cluster warm hand-out rate saturate (**ceiling**), and what happens "
        "when the pool is over-subscribed (**contention**)? All below, on the same TTFE spine as "
        "the headline matrix.")
    lines.append("")
    if scale.strip():
        lines.append(scale.rstrip())
        lines.append("")
    if burst.strip():
        lines.append(burst.rstrip())
        lines.append("")
    if burst_chart.strip():
        lines.append(burst_chart.rstrip())
        lines.append("")
    if saturation.strip():
        lines.append(saturation.rstrip())
        lines.append("")
    if contention.strip():
        lines.append(contention.rstrip())
    return "\n".join(lines).rstrip()


# --- #4083: warm-pool acquisition-latency render ------------------------------------------
def _clean_warm_pool_acquisition(results):
    """Closed-schema-validate the TOP-LEVEL warm_pool_acquisition object (#4083). None ⇒ INERT.

    Returns the cleaned dict ONLY when the REQUIRED spine (runtime_class, acq_p50_ms, acq_p95_ms,
    n) is present and each field passes its predicate. Optional decomposition/provenance fields
    (acq_p99_ms, offered_rate_per_s, warmpool_size, controller_startup_p95_ms, machine_type,
    node_count, measured_at) render only when valid; a present-but-invalid one is dropped on read,
    never fabricated. runtime_class validates against the PUBLIC RUNTIME_LABELS enum, so an
    out-of-enum runtime fails closed and drops the whole block.
    """
    wpa = results.get("warm_pool_acquisition")
    if not isinstance(wpa, dict):
        return None
    clean = {}
    for key, ok in WARM_POOL_ACQUISITION_FIELDS.items():
        if key in wpa:
            try:
                if ok(wpa[key]):
                    clean[key] = wpa[key]
            except (TypeError, ValueError):
                pass
    if not all(k in clean for k in ("runtime_class", "acq_p50_ms", "acq_p95_ms", "n")):
        return None
    return clean


def render_warm_pool_acquisition(results):
    """Render the warm-pool acquisition-latency block (#4083), or "" when INERT.

    Reports the DECOMPOSED claim→bound sub-phase of TTFE — SandboxClaim requested → bound (a ready
    warm sandbox handed back), the number a warm-pool operator sizes against. It EXCLUDES the
    exec-attach + first-instruction round-trip the concurrent_burst/matrix TTFE legs include, so
    the caption states plainly it is NOT comparable to those TTFE columns. The optional
    controller_startup_p95 renders as an explicit LOWER-BOUND proxy (mirrors the step-up #3975
    discipline). INERT until the harness emits a closed-schema-clean warm_pool_acquisition object.
    """
    wpa = _clean_warm_pool_acquisition(results)
    if not wpa:
        return ""
    label = RUNTIME_LABELS[wpa["runtime_class"]]
    lines = ["## Warm-Pool Acquisition — how fast the pool hands you a sandbox", ""]
    caption = (
        f"Acquisition latency on **{label}**: the time from a `SandboxClaim` being **requested** "
        "to it being **bound** — a warm, ready sandbox handed back to the caller. This is a "
        "**decomposed sub-phase of TTFE**, not the whole thing: it stops at the moment you hold a "
        "ready sandbox and **excludes** the exec-attach + first-instruction round-trip the "
        "Concurrent Burst and Core Metrics tables measure — so these numbers are **not comparable** "
        "to those TTFE columns. It is the earlier, isolated question a warm-pool operator sizes "
        "against: *once my pool is warm, how quickly do I get a sandbox?*"
    )
    ctx = []
    if wpa.get("offered_rate_per_s") is not None:
        ctx.append(f"a sustained **{_fmt_num(wpa['offered_rate_per_s'])} claims/sec** offered load")
    if wpa.get("warmpool_size") is not None:
        ctx.append(f"a warm pool of **{_fmt_num(wpa['warmpool_size'])}**")
    if ctx:
        caption += " Measured under " + " against ".join(ctx) + "."
    shape = []
    if wpa.get("node_count") is not None:
        shape.append(f"node_count={wpa['node_count']}")
    if wpa.get("machine_type"):
        shape.append(f"`{wpa['machine_type']}`")
    if shape:
        caption += f" Cluster shape: {', '.join(shape)}."
    lines.append(caption)
    lines.append("")
    header = ["Sample (n)", "Acquisition p50", "Acquisition p95", "Acquisition p99"]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")
    row = [
        _fmt_num(wpa["n"]),
        _fmt_secs(wpa["acq_p50_ms"]),
        _fmt_secs(wpa["acq_p95_ms"]),
        _fmt_secs(wpa["acq_p99_ms"]) if "acq_p99_ms" in wpa else "—",
    ]
    lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    if wpa.get("controller_startup_p95_ms") is not None:
        lines.append(
            f"_Controller-startup lower bound (p95 **{_fmt_secs(wpa['controller_startup_p95_ms'])}**): "
            "controller-first-observed → Ready, which EXCLUDES the claim-admission → first-reconcile "
            "queueing lag — it UNDER-reports the true acquisition path, so treat it as a floor on the "
            "controller's own contribution, not a second acquisition measurement._")
        lines.append("")
    if wpa.get("measured_at"):
        lines.append(f"_Measured {wpa['measured_at'][:10]} — warm-pool acquisition latency (point-in-time)._")
        lines.append("")
    return "\n".join(lines)


# --- at-scale-under-contention RETRACTION render ------------------------------------------
def _clean_at_scale_contention(results):
    """Closed-schema-validate the TOP-LEVEL at_scale_contention object. None ⇒ INERT.

    Returns the cleaned dict ONLY when the REQUIRED spine (runtime_class, pool_size, claim_count,
    ttfe_p50_ms, ttfe_p95_ms) is present and each field passes its predicate. Optional bind/exec
    decomposition + provenance fields render only when valid; a present-but-invalid one is dropped
    on read, never fabricated. runtime_class validates against the PUBLIC RUNTIME_LABELS enum, so
    an out-of-enum runtime fails closed and drops the whole block.
    """
    asc = results.get("at_scale_contention")
    if not isinstance(asc, dict):
        return None
    clean = {}
    for key, ok in AT_SCALE_CONTENTION_FIELDS.items():
        if key in asc:
            try:
                if ok(asc[key]):
                    clean[key] = asc[key]
            except (TypeError, ValueError):
                pass
    if not all(k in clean for k in ("runtime_class", "pool_size", "claim_count", "ttfe_p50_ms", "ttfe_p95_ms")):
        return None
    return clean


def render_at_scale_contention(results, detail=False, page_heading=None):
    """Render the at-scale-under-contention RETRACTION block, or "" when INERT.

    The deliberate counter-point to the flattering 1:1 warm bursts: a single measured operating
    point where the pool is OVER-SUBSCRIBED (claim_count > pool_size) and warm activation is NO
    LONGER sub-second. Publishing this ceiling keeps the fast matrix/burst numbers from reading as
    an unconditional guarantee. TTFE is node-count-independent, so it IS comparable to the matrix /
    Concurrent Burst TTFE columns; the per-node throughput axis is DELIBERATELY absent (this point
    ran at node_count=1, non-comparable to the node_count=20 bursts). INERT until the harness emits
    a closed-schema-clean at_scale_contention object.

    hb#134 page-split: the DEFAULT (page) path renders the honest-limits retraction posture — the
    prose + the headline TTFE p50/p95 the reader needs to budget for the worst case — under the
    friendlier "Where it breaks today" heading, with a pointer to the full bind/exec decomposition
    table in DETAILS.md. `detail=True` renders that full table (deep-dive appendix). The retraction
    NEVER leaves the headline page — only the decomposition working moves.
    """
    asc = _clean_at_scale_contention(results)
    if not asc:
        return ""
    prov = _clean_provenance(results.get("provenance"))
    stale_caveat = _stale_carry_forward_caveat(asc, prov, label="at-scale-contention")
    label = RUNTIME_LABELS[asc["runtime_class"]]
    pool, claims = asc["pool_size"], asc["claim_count"]
    ratio = f"{_fmt_ratio(claims / pool)}:1" if pool else "—"
    heading = ("## At Scale Under Contention — where sub-second warm activation breaks"
               if detail else (page_heading or "## Where it breaks today (honest limits)"))
    lines = [heading, ""]
    # hb#134 (nit): the Concurrent Burst table lives on the headline README, so "above" is
    # correct on the page path but dangles in the DETAILS detail-path (nothing is above it there).
    burst_locator = "on the headline page" if detail else "above"
    if detail:
        # Deep-dive appendix keeps the full mechanism explanation (over-subscription serialization).
        caption = (
            f"The Concurrent Burst legs {burst_locator} are **1:1** — N ready sandboxes hit with N claims. This "
            "is the deliberate **retraction**: the operating point where the pool is "
            "**over-subscribed** (more concurrent claims than ready pool members), and warm activation "
            f"**stops being sub-second**. Measured on **{label}**: a pool of **{_fmt_num(pool)}** ready "
            f"sandboxes hit with **{_fmt_num(claims)}** simultaneous claims (**{ratio} contention**). "
            "Every claim still binds, but the over-subscription serializes the bind path — so the "
            "\"warm hit is <1s\" claim from the Core Metrics matrix does **not** hold here."
        )
    else:
        # hb#488: the headline page keeps the numbers + the honest "does not hold here" retraction;
        # the over-subscription-serializes-the-bind-path mechanism moves to DETAILS (detail path).
        caption = (
            f"The deliberate **retraction** — an **over-subscribed** pool (**{_fmt_num(pool)}** ready "
            f"sandboxes hit with **{_fmt_num(claims)}** simultaneous claims, **{ratio} contention**) on "
            f"**{label}**. Warm activation stops being sub-second: the \"warm hit is <1s\" claim from "
            "the Core Metrics matrix does **not** hold here."
        )
    shape = []
    if asc.get("node_count") is not None:
        shape.append(f"node_count={asc['node_count']}")
    if asc.get("machine_type"):
        shape.append(f"`{asc['machine_type']}`")
    if shape:
        caption += f" Cluster shape: {', '.join(shape)}."
    lines.append(caption)
    lines.append("")
    if not detail:
        # Page path: surface the worst-case TTFE inline (so the retraction is self-contained
        # without the table) + point to the full bind/exec decomposition in the appendix.
        lines.append(
            f"Under this contention, TTFE degrades to **{_fmt_secs(asc['ttfe_p50_ms'])} p50** / "
            f"**{_fmt_secs(asc['ttfe_p95_ms'])} p95** — budget for that, not the sub-second warm "
            "hit, when your claim rate can outrun your pool. Full bind/exec decomposition is in "
            "the deep-dive appendix, [DETAILS.md](DETAILS.md).")
        lines.append("")
        if asc.get("measured_at"):
            lines.append(f"_Measured {asc['measured_at'][:10]} — warm-pool at-scale contention ceiling (point-in-time)._")
            lines.append("")
        if stale_caveat:
            lines.append(stale_caveat)
            lines.append("")
        return "\n".join(lines)
    header = ["Pool", "Claims", "Contention", "TTFE p50", "TTFE p95"]
    have_bind = "bind_p50_ms" in asc and "bind_p95_ms" in asc
    if have_bind:
        header += ["Bind p50", "Bind p95"]
    have_exec = "exec_success_rate" in asc
    if have_exec:
        header.append("Execution Success")
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")
    row = [
        _fmt_num(pool),
        _fmt_num(claims),
        ratio,
        _fmt_secs(asc["ttfe_p50_ms"]),
        _fmt_secs(asc["ttfe_p95_ms"]),
    ]
    if have_bind:
        row += [_fmt_secs(asc["bind_p50_ms"]), _fmt_secs(asc["bind_p95_ms"])]
    if have_exec:
        row.append(_exec_cell(asc["exec_success_rate"], claims))
    lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    lines.append(
        "_Not directly comparable to the 1:1 Concurrent Burst legs: this point ran at "
        "node_count=1 with an over-subscribed pool — a distinct operating point. Latency is "
        "node-count-independent (so the TTFE columns DO compare to the matrix/burst TTFE), but the "
        "per-node throughput axis is omitted here as non-comparable to the node_count=20 bursts._"
    )
    lines.append("")
    if asc.get("measured_at"):
        lines.append(f"_Measured {asc['measured_at'][:10]} — warm-pool at-scale contention ceiling (point-in-time)._")
        lines.append("")
    if stale_caveat:
        lines.append(stale_caveat)
        lines.append("")
    return "\n".join(lines)


# --- cluster-scale SATURATION render ------------------------------------------------------
def _clean_cluster_saturation(results):
    """Closed-schema-validate the TOP-LEVEL cluster_saturation object. None ⇒ INERT.

    Returns the cleaned dict ONLY when the REQUIRED spine is present and each field passes its
    predicate: runtime_class, pool_size, claim_count, node_count, ttfe_p50_ms, ttfe_p95_ms, AND the
    measured per-cluster throughput triple (thpt_under_5s_per_cluster, thpt_under_1s_per_cluster,
    thpt_cluster_node_count) — the coupled-triple rule the matrix uses, so a per-cluster figure
    never renders without the node count it was measured at. Optional per-node halves, bind/exec
    decomposition, outcome, and provenance render only when valid; a present-but-invalid one is
    dropped on read, never fabricated. runtime_class validates against the PUBLIC RUNTIME_LABELS
    enum, so an out-of-enum runtime fails closed and drops the whole block.
    """
    cs = results.get("cluster_saturation")
    if not isinstance(cs, dict):
        return None
    clean = {}
    for key, ok in CLUSTER_SATURATION_FIELDS.items():
        if key in cs:
            try:
                if ok(cs[key]):
                    clean[key] = cs[key]
            except (TypeError, ValueError):
                pass
    required = (
        "runtime_class", "pool_size", "claim_count", "node_count",
        "ttfe_p50_ms", "ttfe_p95_ms",
        "thpt_under_5s_per_cluster", "thpt_under_1s_per_cluster", "thpt_cluster_node_count",
    )
    if not all(k in clean for k in required):
        return None
    return clean


def render_cluster_saturation(results, heading="### Saturation — the whole-cluster warm-hand-out ceiling", detail=False):
    """Render the cluster-scale SATURATION block, or "" when INERT.

    The third cluster-scale question, distinct from Linearity and Concurrent Burst above it and from
    the at-scale-contention retraction: a 1:1 ALL-WARM fire (pool == claim, NOT over-subscribed)
    driven to CLUSTER saturation — a large claim burst spread across many nodes where the bind path
    saturates even though every claim has a ready warm pool member. This is the honest ceiling for
    "how fast can the whole cluster hand out warm sandboxes at once": the per-cluster throughput
    (MEASURED at node_count, never a per-node × N extrapolation) collapses far below the per-node
    engineering rate, and the sub-second warm hit the Core Metrics matrix reports does NOT hold at
    this scale. outcome=FAIL is headlined as the honest SLA-not-met ceiling, not softened.

    hb#134 page-split: the DEFAULT (page) path renders under a demoted `###` sub-heading (it is the
    third sub-block of the "Does it hold at cluster scale?" section, render_cluster_scale) — the
    prose + the headline per-cluster throughput + TTFE the reader needs, with a pointer to the full
    per-node/per-cluster + bind/exec decomposition table in DETAILS.md. `detail=True` renders that
    full table under a standalone `##` heading (deep-dive appendix). The ceiling posture NEVER
    leaves the headline page — only the decomposition working moves.
    """
    cs = _clean_cluster_saturation(results)
    if not cs:
        return ""
    label = RUNTIME_LABELS[cs["runtime_class"]]
    pool, claims, nodes = cs["pool_size"], cs["claim_count"], cs["node_count"]
    x = _landed_cluster_x(cs)
    if detail:
        heading = "## Cluster Saturation — the whole-cluster warm-hand-out ceiling"
    lines = [heading, ""]
    if detail:
        # Deep-dive appendix keeps the full mechanism explanation (why the bind path saturates).
        caption = (
            "The Concurrent Burst legs above are small 1:1 warm bursts. This is the **saturation** "
            f"ceiling: a **1:1 all-warm** fire — a pool of **{_fmt_num(pool)}** ready sandboxes hit "
            f"with **{_fmt_num(claims)}** simultaneous claims (**not** over-subscribed), spread across "
            f"**{_fmt_num(nodes)}** nodes on **{label}**. Every claim has a ready warm pool member, yet "
            "at this scale the bind path itself saturates — so the whole-cluster warm hand-out rate "
            "collapses far below the per-node engineering rate, and the \"warm hit is <1s\" claim from "
            "the Core Metrics matrix does **not** hold here."
        )
    else:
        # hb#488: the headline page keeps the numbers + the honest "does not hold here" retraction;
        # the bind-path-saturation mechanism explanation moves to DETAILS (detail path above).
        caption = (
            f"**Saturation** ceiling — a **1:1 all-warm** fire (**{_fmt_num(pool)}** ready sandboxes, "
            f"**{_fmt_num(claims)}** simultaneous claims, **not** over-subscribed) across "
            f"**{_fmt_num(nodes)}** nodes on **{label}**. At this scale the \"warm hit is <1s\" claim "
            "from the Core Metrics matrix does **not** hold here."
        )
    if cs.get("machine_type"):
        caption += f" Cluster shape: `{cs['machine_type']}`."
    lines.append(caption)
    lines.append("")
    if not detail:
        # Page path: surface the collapsed per-cluster throughput + worst-case TTFE inline (so the
        # ceiling is self-contained without the table) + point to the full decomposition in DETAILS.
        lines.append(
            f"At **{_fmt_num(x)} nodes** the cluster sustains only "
            f"**{_fmt_num(cs['thpt_under_5s_per_cluster'])} claims/sec under 5s** "
            f"(**{_fmt_num(cs['thpt_under_1s_per_cluster'])}/sec under 1s**) across the whole "
            f"cluster, and TTFE degrades to **{_fmt_secs(cs['ttfe_p50_ms'])} p50** / "
            f"**{_fmt_secs(cs['ttfe_p95_ms'])} p95**. This is the honest per-cluster hand-out "
            "ceiling — budget for it when your claim rate can outrun the bind path, not for the "
            "sub-second per-node warm hit. Full per-node/per-cluster and bind/exec decomposition is "
            "in the deep-dive appendix, [DETAILS.md](DETAILS.md).")
        lines.append("")
        if cs.get("outcome") == "FAIL":
            lines.append(
                "_SLA ceiling: **not met** at this operating point — this row is the honest "
                "saturation limit, not a warm-hit guarantee. Every claim still bound and executed; "
                "the FAIL is the throughput collapse against the sizing floor, not a correctness "
                "failure._")
            lines.append("")
        if cs.get("measured_at"):
            lines.append(f"_Measured {cs['measured_at'][:10]} — whole-cluster saturation ceiling (point-in-time)._")
            lines.append("")
        return "\n".join(lines)
    # Detail path: the full per-node + per-cluster throughput triple + bind/exec decomposition.
    header = ["Pool", "Claims", "Nodes", "TTFE p50", "TTFE p95",
              "Throughput @ <5s", "Throughput @ <1s"]
    have_bind = "bind_p50_ms" in cs and "bind_p95_ms" in cs
    if have_bind:
        header += ["Bind p50", "Bind p95"]
    have_exec = "exec_success_rate" in cs
    if have_exec:
        header.append("Execution Success")
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")

    def _triple(node_key, cluster_key):
        cluster_half = f"{_fmt_num(cs[cluster_key])} /cluster"
        if node_key in cs:
            return f"{_fmt_num(cs[node_key])} /node · {cluster_half}"
        return cluster_half

    row = [
        _fmt_num(pool),
        _fmt_num(claims),
        _fmt_num(nodes),
        _fmt_secs(cs["ttfe_p50_ms"]),
        _fmt_secs(cs["ttfe_p95_ms"]),
        _triple("thpt_under_5s_per_node", "thpt_under_5s_per_cluster"),
        _triple("thpt_under_1s_per_node", "thpt_under_1s_per_cluster"),
    ]
    if have_bind:
        row += [_fmt_secs(cs["bind_p50_ms"]), _fmt_secs(cs["bind_p95_ms"])]
    if have_exec:
        row.append(_exec_cell(cs["exec_success_rate"], claims))
    lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    lines.append(
        f"_Per-cluster throughput MEASURED at **{_fmt_num(x)} nodes** — never a per-node × N "
        "extrapolation (that fiction breaks above the controller reconcile ceiling). This is a "
        "**1:1 all-warm** operating point (pool == claim, not over-subscribed), distinct from the "
        "over-subscribed contention ceiling: the collapse here is the bind path saturating at "
        "cluster scale, not pool exhaustion. Latency is node-count-independent (so the TTFE columns "
        "DO compare to the matrix/burst TTFE)._")
    lines.append("")
    if cs.get("outcome") == "FAIL":
        lines.append(
            "_SLA ceiling: **not met** at this operating point — the honest saturation limit. "
            "Execution success confirms every claim still bound and executed; the FAIL is the "
            "throughput collapse against the sizing floor, not a correctness failure._")
        lines.append("")
    if cs.get("measured_at"):
        lines.append(f"_Measured {cs['measured_at'][:10]} — whole-cluster saturation ceiling (point-in-time)._")
        lines.append("")
    return "\n".join(lines)


# --- #4086: Provisioning rate sweep (reconcile-bound warm-pool convergence) ---------------
# A THIRD distinct axis, deliberately NOT folded into stepup's TTFE Pareto or the
# at_scale_contention claim:pool ratio: this measures warm-pool PROVISIONING convergence
# (Ready% within pool_warm_timeout) as a function of the OFFERED reconcile RATE. Folding it
# into either of those would falsely imply a same-regime measurement — an honesty violation.


def _clean_provisioning_rate_sweep(results):
    """Closed-schema-validate the TOP-LEVEL provisioning_rate_sweep object. None ⇒ INERT.

    Requires a non-empty rate_points list (each point validated by _rate_points_ok). Optional
    runtime_class / ceiling_low_per_s / ceiling_high_per_s / measured_at render only when valid;
    a present-but-invalid optional is dropped on read, never fabricated. runtime_class validates
    against the PUBLIC RUNTIME_LABELS enum, so an out-of-enum runtime fails closed.
    """
    prs = results.get("provisioning_rate_sweep")
    if not isinstance(prs, dict):
        return None
    clean = {}
    for key, ok in PROVISIONING_RATE_SWEEP_FIELDS.items():
        if key in prs:
            try:
                if ok(prs[key]):
                    clean[key] = prs[key]
            except (TypeError, ValueError):
                pass
    if "rate_points" not in clean:
        return None
    clean["rate_points"] = sorted(clean["rate_points"], key=lambda p: p["offered_rate_per_s"])
    return clean


def _rate_verdict_cell(point):
    """One provisioning-rate row's outcome cell: ✅ when the pool converged (Ready% hit target
    within the warm timeout), ❌ when it timed out under-provisioned. The measured Ready% is
    always shown so a partial fill reads honestly, never rounded up to a pass/fail bit."""
    pct = point["ready_pct"]
    converged = point.get("converged")
    if converged is None:
        converged = pct >= 100.0
    mark = "✅" if converged else "❌"
    cell = f"{mark} {_fmt_num(round(pct, 1))}%"
    el, to = point.get("elapsed_s"), point.get("timeout_s")
    if converged and el is not None:
        cell += f" (converged ~{_fmt_num(round(el))}s)"
    elif not converged and to is not None:
        cell += f" (timeout {_fmt_num(round(to))}s)"
    return cell


def render_provisioning_rate_sweep(results):
    """Render the provisioning rate-sweep block (#4086), or "" when INERT.

    Warm-pool provisioning convergence vs OFFERED reconcile rate: at each offered rate the harness
    drives a warm-pool target and measures whether the pool reaches Ready within pool_warm_timeout.
    Monotonic degradation past a rate ceiling reads reconcile-bound (the controller reconcile path
    is the ceiling), NOT node/quota-bound. INERT until the harness emits a closed-schema-clean
    provisioning_rate_sweep object.
    """
    prs = _clean_provisioning_rate_sweep(results)
    if not prs:
        return ""
    label = RUNTIME_LABELS.get(prs.get("runtime_class"))
    lines = ["## Provisioning Rate Sweep — where warm-pool fill goes reconcile-bound", ""]
    caption = (
        "The warm-pool numbers elsewhere assume the pool is **already Ready**. This block measures "
        "the step before that: how fast the pool can be **provisioned** as a function of the "
        "**offered reconcile rate** (sandboxes requested per second). At each rate the pool is "
        "driven to a target size and we measure whether it reaches Ready **within the warm "
        "timeout**."
    )
    if label:
        caption += f" Measured on **{label}**."
    lines.append(caption)
    lines.append("")
    header = ["Offered reconcile rate", "Warm-pool target", "Ready within timeout"]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")
    for p in prs["rate_points"]:
        wps = p.get("warmpool_size")
        lines.append(
            "| "
            + " | ".join(
                [
                    f"{_fmt_num(p['offered_rate_per_s'])} sb/s",
                    _fmt_num(wps) if wps is not None else "—",
                    _rate_verdict_cell(p),
                ]
            )
            + " |"
        )
    lines.append("")
    lo, hi = prs.get("ceiling_low_per_s"), prs.get("ceiling_high_per_s")
    if lo is not None and hi is not None:
        lines.append(
            f"**Provisioning converges at ~{_fmt_num(lo)} sb/s; over-subscribed beyond "
            f"~({_fmt_num(lo)}, {_fmt_num(hi)}) sb/s** — monotonic degradation past the ceiling is "
            "**reconcile-bound** (the controller reconcile path is the ceiling), not node- or "
            "quota-bound."
        )
        lines.append("")
    lines.append(
        "_A distinct axis from the Concurrent Burst (claim:pool ratio) and Step-up (creation-rate "
        "TTFE) blocks: this measures provisioning **offered-rate** convergence, a separate regime — "
        "not directly comparable to those latency/throughput points._"
    )
    lines.append("")
    if prs.get("measured_at"):
        lines.append(
            f"_Measured {prs['measured_at'][:10]} — warm-pool provisioning rate sweep "
            "(point-in-time; refreshed on the next rate sweep)._"
        )
        lines.append("")
    return "\n".join(lines)


# --- Step-up backfill saturation render -----------------------------------------------------
# The saturation headline that was asked for ("max sandboxes/sec under 5s AND under 1s,
# warm+cold") is computed by the internal classifier (#4030) and emitted as the pre-validated
# saturation_point block (2×2 warm/cold × tight(1s)/loose(5s) bars). Render reads it straight —
# the operator headline is the emitter's number, not a render-time re-derivation. The schema
# characteristic band-rates + verdict (0.5s stretch bar / collapse 2000ms) and the per-step
# Pareto table render additively below as the methodology/study story.


def _clean_stepup(results):
    """Closed-schema-validate the TOP-LEVEL stepup object. None ⇒ INERT.

    Every present field renders ONLY if it is declared in STEPUP_PARETO_FIELDS and passes its
    predicate; anything else is dropped on read. The block is INERT unless the union of
    {pareto_points, controller_startup} is non-empty (the emitter's no-all-empty invariant) —
    a stepup object carrying only sweep params but no measured table never renders.
    """
    su = results.get("stepup")
    if not isinstance(su, dict):
        return None
    clean = {}
    for key, ok in STEPUP_PARETO_FIELDS.items():
        if key in su:
            try:
                if ok(su[key]):
                    clean[key] = su[key]
            except (TypeError, ValueError):
                pass
    if not any(k in clean for k in ("saturation_point", "pareto_points", "controller_startup", "literal_ttfe", "acquisition")):
        return None
    return clean


def _sp_cell(leg, key):
    """One saturation-point table cell: the positive-int rate as "N/s", or em-dash when the bar
    was unmet (value None or absent) — honest "nothing met this bar", never a fabricated 0."""
    rv = leg.get(key)
    if isinstance(rv, int) and not isinstance(rv, bool):
        return f"{_fmt_num(rv)}/s"
    return "—"


_STEPUP_VERDICT_LABELS = {
    "flat-through-sweep": "✅ flat through the whole sweep (no measured step breached the 0.5s stretch bar)",
    "degrading": "⚠️ degrading (at least one step breached the 0.5s stretch bar; none collapsed)",
    "saturated": "🛑 saturated (at least one step crossed the collapse band)",
    "no-measured-steps": "pending (no step produced a measured TTFE — infra/scrape gap, honest)",
}


def render_stepup(results):
    """Render the step-up saturation block, or "" when INERT.

    Headline = the operator Saturation Point table (#4030): max sustained creation rate with
    TTFE p95 under the 1s (tight) and 5s (loose) bars, split by leg (warm-pool hit vs cold-
    provision overflow), read straight off the emitter's pre-validated saturation_point block.
    An unmet bar renders em-dash, never a fabricated 0. The schema verdict + characteristic
    band-rates (0.5s stretch bar / collapse 2000ms) and the per-step Pareto table render
    additively below as the methodology/study story. The controller_startup proxy renders as an
    explicit LOWER BOUND (it excludes claim→first-reconcile queueing, so it under-reports true
    TTFE). INERT until a closed-schema-clean stepup object with a non-empty table is emitted.
    """
    su = _clean_stepup(results)
    if not su:
        return ""
    lines = []

    sp = su.get("saturation_point")
    if sp:
        tight = _fmt_secs(sp["tight_ms"])
        loose = _fmt_secs(sp["loose_ms"])
        lines.append("## Saturation Point — max sustained creation rate")
        lines.append("")
        lines.append(
            "Max sustained creation rate (offered sandboxes/sec) that held TTFE p95 under each "
            "operator bar, split by leg — warm-pool hit vs cold-provision (node overflow). An "
            "em-dash means no swept rate met that bar; we never round a miss up to a 0.")
        lines.append("")
        header = ["Leg", f"Max rate @ TTFE p95 < {tight}", f"@ p95 < {loose}"]
        lines.append("| " + " | ".join(header) + " |")
        lines.append("|" + "|".join(["---"] * len(header)) + "|")
        for leg_key, leg_label in (("warm", "Warm-pool hit"), ("cold", "Cold-provision (node overflow)")):
            leg = sp.get(leg_key)
            if not isinstance(leg, dict):
                continue
            lines.append(f"| {leg_label} | {_sp_cell(leg, 'max_rate_under_tight')} | {_sp_cell(leg, 'max_rate_under_loose')} |")
        lines.append("")
    else:
        # No saturation_point — e.g. the #3975 proxy-only sweep (no true-TTFE steps, so the
        # classifier emits no headline). The study/proxy detail below still renders.
        lines.append("## Saturation — step-up throughput study")
        lines.append("")

    pts = su.get("pareto_points")
    # Schema verdict + characteristic band-rates (stricter 500ms/2000ms framing) — additive.
    band_bits = []
    if "max_flat_rate" in su:
        band_bits.append(f"highest rate under the 0.5s stretch bar: **{_fmt_num(su['max_flat_rate'])}/s**")
    if "north_star_breach_rate" in su:
        band_bits.append(f"first rate to breach 500ms: {_fmt_num(su['north_star_breach_rate'])}/s")
    if "saturation_rate" in su:
        band_bits.append(f"first rate to cross 2000ms: {_fmt_num(su['saturation_rate'])}/s")
    # Falsifiability guard (hb#416/#417 lineage): each MEASURED verdict label asserts a
    # band-crossing claim ("no measured step breached the 0.5s stretch bar" / "at least one step
    # crossed the collapse band"). verdict, the three band-rates, AND pareto_points are all
    # INDEPENDENTLY optional in the schema, so a producer emitting a measured verdict with neither
    # a characteristic band-rate NOR a Pareto table would otherwise publish that assertion on a
    # trust surface with nothing behind it — a render-side bare verdict. In that state no true-TTFE
    # step was actually substantiated, so degrade to the honest no-measured-steps "pending" label
    # rather than trust the classifier's band-coupling invariant at the render boundary.
    if "verdict" in su:
        v = su["verdict"]
        if v in ("flat-through-sweep", "degrading", "saturated") and not band_bits and not pts:
            v = "no-measured-steps"
        lines.append(f"Curve verdict (0.5s stretch-bar p95<500ms / collapse 2000ms bands): {_STEPUP_VERDICT_LABELS[v]}.")
        lines.append("")
    if band_bits:
        lines.append("Characteristic rates — " + "; ".join(band_bits) + ".")
        lines.append("")

    # True-TTFE Pareto table.
    if pts:
        # Cost ($/1k ready) is the step-up item-4 axis — stamped per point by the adapter's
        # enrich_pareto_cost only when the cluster shape resolves a rate (unknown machine_type /
        # non-positive ready leaves it ABSENT, honest "pending"). Render the column ONLY when at
        # least one point actually carries a cost, so an axis that was never enabled is omitted
        # entirely rather than shown as a full column of dashes falsely implying a failed measure;
        # a point missing cost while siblings have it gets a partial-coverage "—".
        any_cost = any("cost_usd_per_1k_ready" in p for p in pts)
        header = ["Offered rate (/s)", "TTFE p50", "TTFE p95", "TTFE p99", "Ready /s"]
        if any_cost:
            header.append("Cost ($/1k ready)")
        lines.append("| " + " | ".join(header) + " |")
        lines.append("|" + "|".join(["---"] * len(header)) + "|")
        for p in sorted(pts, key=lambda q: q["offered_rate_per_s"]):
            cells = [
                _fmt_num(p["offered_rate_per_s"]),
                _fmt_secs(p["ttfe_p50_ms"]) if "ttfe_p50_ms" in p else "—",
                _fmt_secs(p["ttfe_p95_ms"]),
                _fmt_secs(p["ttfe_p99_ms"]) if "ttfe_p99_ms" in p else "—",
                _fmt_num(p["ready_per_s"]) if "ready_per_s" in p else "—",
            ]
            if any_cost:
                cells.append(_fmt_usd(p["cost_usd_per_1k_ready"]) if "cost_usd_per_1k_ready" in p else "—")
            lines.append("| " + " | ".join(cells) + " |")
        lines.append("")
        # Cost-BASIS disclosure (step-up item-4): a published Cost cell is only as trustworthy as
        # the node-hour rate it was computed from, so name WHICH price source produced that rate
        # rather than publishing a bare number the reader must trust blind. Keyed off the
        # sweep-level cost_basis flag (schema-gated to operator_rate | list_price), same posture as
        # the controller_startup lower_bound / SLO-basis caveats — a list_price cost is a coarse
        # UPPER bound (real billing is materially lower under committed-use / spot / sustained-use),
        # an operator_rate cost is the operator's real committed rate. Rendered only alongside a
        # real cost column; no cost stamped => no basis line (honest pending, same absent spine).
        if any_cost:
            basis = su.get("cost_basis")
            if basis == "list_price":
                lines.append(
                    f"Cost basis: **coarse public GCP on-demand list price** (as of {LIST_PRICE_AS_OF}) "
                    "— an UPPER bound; real billing is materially lower under committed-use / spot / "
                    "sustained-use discounts, and a list price is a point-in-time snapshot that drifts "
                    "as GCP prices change. Pass an explicit node-hour rate for the operator's real, "
                    "current cost.")
                lines.append("")
            elif basis == "operator_rate":
                lines.append(
                    "Cost basis: operator-supplied node-hour rate (the operator's real committed rate).")
                lines.append("")

    # Claim-ACQUISITION axis — the DISTINCT compliant axis, rendered SEPARATELY from
    # TTFE so the page never averages the two into one verdict: a bracket can be acq-compliant
    # AND TTFE-non-compliant at once, and that split is the finding.
    acq = su.get("acquisition")
    if acq:
        ns = acq.get("north_star_p95_ms")
        bar = f" (north-star p95 < {_fmt_secs(ns)})" if isinstance(ns, (int, float)) and not isinstance(ns, bool) else ""
        if acq.get("compliant") and acq.get("no_knee_in_range"):
            lines.append(
                f"**Claim acquisition stays compliant across the swept bracket{bar} — no knee.** "
                "Acquisition (submit → claim bound) is measured directly, not via a TTFE proxy; "
                "its compliance is a SEPARATE axis from the end-to-end readiness bounds below.")
        else:
            lines.append(
                f"Claim-acquisition latency (submit → claim bound){bar} — measured directly, a "
                "SEPARATE axis from the end-to-end readiness bounds below.")
        lines.append("")
        header = ["Offered rate (/s)", "Acq p50", "Acq p95", "Acq p99", "p95 < bar", "Fulfilled /s"]
        lines.append("| " + " | ".join(header) + " |")
        lines.append("|" + "|".join(["---"] * len(header)) + "|")
        for p in sorted(acq["pareto_points"], key=lambda q: q["offered_rate_per_s"]):
            under = p.get("p95_under_500ms")
            under_cell = "✅" if under is True else ("🛑" if under is False else "—")
            cells = [
                _fmt_num(p["offered_rate_per_s"]),
                _fmt_secs(p["acq_p50_ms"]) if "acq_p50_ms" in p else "—",
                _fmt_secs(p["acq_p95_ms"]),
                _fmt_secs(p["acq_p99_ms"]) if "acq_p99_ms" in p else "—",
                under_cell,
                _fmt_num(p["acq_fulfilled_per_s"]) if "acq_fulfilled_per_s" in p else "—",
            ]
            lines.append("| " + " | ".join(cells) + " |")
        lines.append("")

    # End-to-end readiness (TTFE) window — pinned between two DISTINCT named bounds,
    # never rendered as a single range: the literal-TTFE UPPER bound (includes exec-setup) and
    # the controller-startup LOWER bound (excludes it). The gap between them is exec-readiness
    # queueing (pod-startup / exec-readiness), which readers want to see.
    #
    # The collapse framing is VERDICT-gated, not presence-gated (fast-follow): the prose
    # keys on verdict == "saturated" (either bound), so a future sweep at lower rungs that folds
    # verdict: "compliant" blocks into latest.json renders neutral "stays within band" prose
    # instead of a silent-false "has already collapsed" publish on a trust surface. The
    # count-word ("two distinct" vs "a single") also tracks how many bounds actually survive the
    # schema predicate, so a dropped bound can't leave a stale "two" behind.
    lt = su.get("literal_ttfe")
    cs = su.get("controller_startup")
    _ttfe_bounds = [b for b in (lt, cs) if b]
    _ttfe_saturated = any(b.get("verdict") == "saturated" for b in _ttfe_bounds)
    if _ttfe_bounds:
        _window = ("pinned between two distinct measured bounds" if len(_ttfe_bounds) == 2
                   else "reported by a single measured bound")
        if _ttfe_saturated:
            lines.append(
                "**End-to-end readiness (TTFE) has already collapsed across this bracket** — the "
                "binding constraint is warm controller-startup queueing, not claim acquisition. The "
                f"collapse is {_window}:")
        else:
            lines.append(
                "**End-to-end readiness (TTFE) stays within the measured band across this bracket** "
                "— no collapse knee is measured here. The end-to-end readiness window is "
                f"{_window}:")
        lines.append("")

    # Literal-TTFE UPPER bound — explicit caveat keyed off upper_bound (load-bearing: the schema
    # requires upper_bound=true, so this caveat can never be dropped while the block renders).
    if lt:
        lines.append(
            "_Literal-TTFE upper bound: exec-probe round-trip (claim → Ready → first exec "
            "instruction), which INCLUDES exec websocket-setup overhead — it OVER-reports true "
            "TTFE, so treat it as a ceiling, not a TTFE measurement._")
        lines.append("")
        if "verdict" in lt:
            lines.append(f"Upper-bound curve verdict: {_STEPUP_VERDICT_LABELS[lt['verdict']]}.")
            lines.append("")
        header = ["Offered rate (/s)", "Literal-TTFE p50", "Literal-TTFE p95", "Literal-TTFE p99", "Over-5s", "Fulfilled /s"]
        lines.append("| " + " | ".join(header) + " |")
        lines.append("|" + "|".join(["---"] * len(header)) + "|")
        for p in sorted(lt["pareto_points"], key=lambda q: q["offered_rate_per_s"]):
            over_cell = "—"
            if "n_over_bar_5s" in p and p.get("n_sampled"):
                over_cell = f"{_fmt_num(p['n_over_bar_5s'])}/{_fmt_num(p['n_sampled'])}"
            cells = [
                _fmt_num(p["offered_rate_per_s"]),
                _fmt_secs(p["literal_p50_ms"]) if "literal_p50_ms" in p else "—",
                _fmt_secs(p["literal_p95_ms"]),
                _fmt_secs(p["literal_p99_ms"]) if "literal_p99_ms" in p else "—",
                over_cell,
                _fmt_num(p["acq_fulfilled_per_s"]) if "acq_fulfilled_per_s" in p else "—",
            ]
            lines.append("| " + " | ".join(cells) + " |")
        lines.append("")

    # Controller-startup LOWER-BOUND proxy (#3975) — separate table, explicit caveat keyed
    # off lower_bound (load-bearing: the schema requires lower_bound=true, so this caveat can
    # never be dropped while the proxy renders). `cs` is resolved with `lt` above the TTFE window.
    if cs:
        lines.append(
            "_Controller-startup lower bound: controller-first-observed → Ready, which "
            "EXCLUDES the claim-admission → first-reconcile queueing lag — it UNDER-reports true "
            "TTFE, so treat it as a floor, not a TTFE measurement._")
        lines.append("")
        if "verdict" in cs:
            lines.append(f"Proxy curve verdict: {_STEPUP_VERDICT_LABELS[cs['verdict']]}.")
            lines.append("")
        header = ["Offered rate (/s)", "Ctrl-startup p50", "Ctrl-startup p95", "Ctrl-startup p99", "Ready /s"]
        lines.append("| " + " | ".join(header) + " |")
        lines.append("|" + "|".join(["---"] * len(header)) + "|")
        for p in sorted(cs["pareto_points"], key=lambda q: q["offered_rate_per_s"]):
            cells = [
                _fmt_num(p["offered_rate_per_s"]),
                _fmt_secs(p["controller_startup_p50_ms"]) if "controller_startup_p50_ms" in p else "—",
                _fmt_secs(p["controller_startup_p95_ms"]),
                _fmt_secs(p["controller_startup_p99_ms"]) if "controller_startup_p99_ms" in p else "—",
                _fmt_num(p["controller_ready_per_s"]) if "controller_ready_per_s" in p else "—",
            ]
            lines.append("| " + " | ".join(cells) + " |")
        lines.append("")

    # Knee-below-ceiling statement — tie the two axes together. The published ceiling
    # is the TTFE-compliant rate (referenced BY POINTER, never a stale literal), and the TTFE
    # 1s/5s crossing sits BELOW the lowest swept rung: these rungs are acquisition-compliant but
    # TTFE-non-compliant, so they never replace the published Warm-Pool Acquisition ceiling.
    if lt and cs:
        _gap = ("_The gap between the upper (literal-TTFE) and lower (controller-startup) bounds is "
                "exec-readiness queueing (pod-startup / exec-readiness).")
        if _ttfe_saturated:
            # lt >= cs by construction, so a saturated verdict on either implies the literal
            # (upper) ceiling is over the collapse band — the 1s/5s crossing sits below the rung.
            _gap += (" The measured bounds are in collapse at the bottom of this bracket, so the "
                     "TTFE 1s/5s crossing sits BELOW the lowest swept rung — the published "
                     "Warm-Pool Acquisition ceiling above remains the TTFE-compliant rate, not "
                     "these acquisition-compliant rungs._")
        else:
            _gap += (" Both bounds stay within the measured TTFE band across this bracket, so no "
                     "collapse knee is measured here — the published Warm-Pool Acquisition ceiling "
                     "above remains the reference TTFE-compliant rate._")
        lines.append(_gap)
        lines.append("")

    # Sweep-parameter subline (Little's-law inputs + date) — public-safe scalars.
    params = []
    if "node_count" in su:
        params.append(f"{_fmt_num(su['node_count'])} nodes")
    if "machine_type" in su:
        params.append(su["machine_type"])
    # warmpool_size: warm(>0)/cold(0) provenance discriminator (hb#4364). 0 is a LEGITIMATE
    # stamped value (explicit cold provenance, e.g. the kata_cold sweep) — render it as the honest
    # "cold (no warm pool)" label, never the misleading "warm pool 0"; absent stays omitted (never
    # fabricated). Carried this far by _coerce_stepup + STEPUP_PARETO_FIELDS; this is the display
    # leg that finally surfaces the discriminator on the page beside the cluster shape.
    if "warmpool_size" in su:
        wps = su["warmpool_size"]
        params.append("cold (no warm pool)" if wps == 0 else f"warm pool {_fmt_num(wps)}")
    if "sld_s" in su:
        params.append(f"SLD {_fmt_num(su['sld_s'])}s")
    if "wpr" in su:
        params.append(f"WPR {_fmt_num(su['wpr'])}")
    tail = ""
    if su.get("measured_at"):
        tail = f" — measured {su['measured_at'][:10]} (point-in-time; refreshed on the next sweep)"
    if params or tail:
        lines.append(f"_Sweep: {', '.join(params)}{tail}._")
        lines.append("")
    return "\n".join(lines)


def render_cost_methodology(results):
    """Render the DETAILS deep-dive that documents HOW the step-up Cost ($/1k ready) column is
    computed, or "" when INERT.

    The headline step-up Pareto table carries a `Cost ($/1k ready)` column (render_stepup), but a
    published number is only trustworthy if the reader can see the working: the closed formula, the
    two cluster-level terms it divides, the honesty spine (absent — never a fabricated 0 — when no
    node-hour rate resolves), and what the two rate bases mean. That methodology has no home on the
    scannable headline page, so it lives here. INERT unless the run actually carries a costed
    Pareto point — a run with a step-up table but no cost column renders nothing (no methodology for
    a number that isn't on the page), same absent-spine posture as the cost column itself.
    """
    su = _clean_stepup(results)
    if su is None:
        return ""
    pts = su.get("pareto_points") or []
    if not any("cost_usd_per_1k_ready" in p for p in pts):
        return ""
    lines = ["## Cost per 1k Ready — how the $/1k-ready column is computed", ""]
    lines.append(
        "The step-up Pareto table above carries a **Cost ($/1k ready)** column. It is not a billing "
        "export — it is a single closed formula over two cluster-level terms, so the reader can "
        "reproduce every cell by hand:"
    )
    lines.append("")
    lines.append("```")
    lines.append("cost_usd_per_1k_ready = (node_count × $/node-hour) ÷ (ready_per_s × 3600) × 1000")
    lines.append("```")
    lines.append("")
    lines.append(
        "The numerator `node_count × $/node-hour` is the **cluster cost rate** (dollars per hour to "
        "run the whole pool). The denominator `ready_per_s × 3600` is the **cluster throughput** "
        "(sandboxes brought to Ready per hour) at that offered rate. Their ratio is dollars per "
        "ready-sandbox; the `× 1000` restates it per **1,000** ready so the headline cells stay "
        "readable. Both terms are already on the page — `ready_per_s` is the same column's Ready/s "
        "value — so the cost cell is internally consistent with the throughput it sits beside, not "
        "a number from a separate accounting path."
    )
    lines.append("")
    lines.append(
        "**Honesty spine.** The rate that feeds `$/node-hour` resolves in one of two ways, and if "
        "neither yields a positive rate the cost cell is **absent** (an em-dash / dropped field), "
        "never a fabricated `0` or a guessed figure:"
    )
    lines.append("")
    lines.append(
        "- **operator_rate** — an explicit operator-supplied node-hour rate was passed. This is the "
        "operator's **real committed rate** and always wins over the list-price table (a bad "
        "explicit rate fails closed to absent rather than silently falling through to list price)."
    )
    lines.append(
        "- **list_price** — no explicit rate, so the machine type (e.g. `e2-standard-16`, "
        "`n2-standard-16`) is looked up in a coarse **public GCP on-demand list-price** table. This "
        "is an **UPPER bound**: real billing is materially lower under committed-use, spot, and "
        "sustained-use discounts. It is also a **point-in-time public snapshot** — GCP list prices "
        "change over time, so a list_price cost can drift stale as well as over-state. Both are "
        "reasons to pass an explicit rate for the operator's true, current cost."
    )
    lines.append("")
    lines.append(
        "The active basis is disclosed as a footnote directly under the Pareto table (`Cost basis: "
        "…`). An unknown machine type with no explicit rate resolves to no rate — so the cost cell, "
        "and its basis footnote, are simply absent for that run rather than reporting a cost the "
        "harness cannot stand behind."
    )
    lines.append("")
    return "\n".join(lines)


# --- #4021: Reproducibility Recipe (static, product-agnostic) ------------------------------
# The preamble promises "reproducible from the recipe at the bottom"; this is that section.
# It is STATIC architecture-shape prose — no measured numbers, so it carries zero PII risk and
# needs no results arg. The RUNNABLE version (exact commands, pinned installs, CI workflows)
# lives in recipe/REPRODUCE.md and is cross-linked, not duplicated. The one honesty rule baked
# in: the honest-today latency is referenced BY POINTER to the live Warm-Pool Acquisition /
# Concurrent Burst cells rather than restated as a literal, so it can never go stale or
# contradict the machine-rendered tables above it, and no contested sub-1s@300/s headline can
# be slipped in ahead of the measured cells.
_RECIPE = """\
## Reproduce it

Every number above comes from a *vanilla* GKE cluster you can provision yourself — no private
tuning. The full recipe — runnable steps (commands, pinned installs, dispatch-only CI), the exact
cluster shape it needs, and the one sizing rule worth copying (**size the warm pool to your active
concurrency**) — lives in [`recipe/REPRODUCE.md`](recipe/REPRODUCE.md).
When a drained-regime fire is on the page, the Warm-Pool decomposition in [DETAILS.md](DETAILS.md)
names the scaling term directly.

**Honesty:** a row marked `pending` is not-yet-measured — never a provisional number dressed as a
result. The **sub-1s @ 300/s warm headline is not yet published**; today's honest figures are the
measured cells above (Core Metrics + **Concurrent Burst**) plus the **Warm-Pool Acquisition**
decomposition in [DETAILS.md](DETAILS.md). TRUE-TTFE (webhook-stamped first-instruction) is now
the live measurement basis for the warm-pool figures above ([asbx#761](https://github.com/kubernetes-sigs/agent-sandbox/pull/761)
merged) — cells publish on `thpt_slo_basis: "true_ttfe"`, not a proxy.
"""


def render_storage_config(record):
    """hb#132 / #4164: the "Which storage class should you pick?" customer-guidance section — a
    3-row closed-enum table (ephemeral / pd / snapshot) of per-class Samples(n) / Payload p50 /
    Pass rate, machine-rendered from a schema-validated `sandbox/records/storage-config-*.json`
    controlled-fire record. It is customer guidance, not a scale property, so it renders as its
    own top-level `##` (placed after "Does it hold at cluster scale?", before "Reproduce it").

    Returns "" (INERT) when: `record` is not a dict; it lacks a valid ISO-8601 `measured_at`
    (an undated measured block must never reach the public page); or no class carries a fully
    valid per-class object. Otherwise it renders the full 3-row skeleton, `pending` for a class
    the record omits (honest-skeleton, mirroring render_density_detail). Closed-schema read: an
    out-of-enum class key or an out-of-range field is dropped (fail-closed) — a half-measured
    class never renders a half-row. Data-keyed degradation (WARM_REGIMES idiom): the section is
    absent until real values land and cannot rot into a stale hardcoded table. The point-in-time
    caption is sourced from the record's fire date; the per-row n is the trust gate.
    """
    if not isinstance(record, dict):
        return ""
    measured_at = record.get("measured_at")
    if not (isinstance(measured_at, str) and _ISO.match(measured_at)):
        return ""
    raw = record.get("storage_classes")
    if not isinstance(raw, dict):
        return ""
    clean = {}
    for cls, obj in raw.items():
        if cls not in STORAGE_CLASSES or not isinstance(obj, dict):
            continue
        if not all(k in obj and pred(obj[k]) for k, pred in STORAGE_CLASS_FIELDS.items()):
            continue
        # #4164 condition-3: a PRESENT `basis` must be in the closed enum, else the class fails
        # closed (dropped whole, same posture as a bad required field). ABSENT basis is fine —
        # it defaults to du-blocks below, so a pre-basis record renders byte-identical.
        if "basis" in obj and not storage_class_basis_ok(obj["basis"]):
            continue
        clean[cls] = obj
    if not clean:
        return ""
    lines = ["## Which storage class should you pick?", ""]
    lines.append(
        "Per-class results from a controlled storage-config fire (fixed workload). An "
        "unmeasured class renders `pending`; the per-row sample count is the trust gate."
    )
    lines.append("")
    # #4164 condition-1: does the measured set span more than one measurement basis? When it
    # does, the `Payload p50` column is not a like-for-like byte comparison, so the diverging
    # (non-default) rows carry a `†` anchor and a footnote below names BOTH differences.
    bases = {obj.get("basis", STORAGE_BASIS_DEFAULT) for obj in clean.values()}
    basis_divergent = len(bases) > 1
    lines.append("| Storage class | Samples (n) | Payload p50 | Pass rate |")
    lines.append("|---|---|---|---|")
    for cls, label in STORAGE_CLASS_LABELS.items():
        obj = clean.get(cls)
        if obj is None:
            n = payload = pr = link_pending(_PENDING)
        else:
            n = _fmt_num(obj["n"])
            payload = _fmt_bytes(obj["bytes_p50"])
            if basis_divergent and obj.get("basis", STORAGE_BASIS_DEFAULT) != STORAGE_BASIS_DEFAULT:
                payload += " †"
            pr = _fmt_pct(obj["pass_rate"])
        lines.append(f"| {label} | {n} | {payload} | {pr} |")
    lines.append("")
    # #4164 W disclosure: when the record carries a valid top-level payload_bytes, name the
    # identical fixed written state so bytes_p50 is interpretable cross-class (eph/pd read
    # ~W; snapshot's delta vs W is the checkpoint overhead — the genuine cross-class signal).
    # Data-driven from the record, never a hardcoded render constant that could drift from
    # the producer's W. Absent/invalid ⇒ caption unchanged (byte-identical to pre-#4164-W).
    w = record.get("payload_bytes")
    w_clause = (
        f"; each class carried an identical controlled write, W = {_fmt_bytes(w)}"
        if storage_payload_bytes_ok(w)
        else ""
    )
    lines.append(f"_Measured {measured_at[:10]} — storage-config axis (point-in-time){w_clause}._")
    lines.append("")
    # #4164 condition-1: when the measured classes span MORE THAN ONE measurement basis (i.e. at
    # least one class counts artifact-bytes while others count du-blocks), the cross-class
    # `Payload p50` column is not like-for-like — the reader must be told BOTH why the numbers
    # differ in kind. Absent divergence (all measured classes share one basis) the footnote is
    # noise and is omitted. Public-safe: generic mechanism prose, no internal names.
    if basis_divergent:
        lines.append(
            "† Payload p50 is not measured the same way across classes, so the column is not a "
            "like-for-like byte comparison. Ephemeral and persistent-disk write a fixed pattern "
            "to a mount and count the **allocated writable-fs blocks** (`du`). The snapshot class "
            "instead counts the **checkpoint-artifact object bytes**: a snapshot captures process "
            "memory, not the writable-fs layer, so its identical W lives in an incompressible "
            "in-memory buffer (a zero-filled buffer would be dropped by the checkpointer's "
            "zero-page optimization and never appear in the artifact), and the artifact bytes "
            "include checkpoint overhead beyond W. Same controlled W per class; different bytes "
            "counted."
        )
        lines.append("")
    return "\n".join(lines) + "\n"


_MEASUREMENT_PATH_DIAGRAM = """\
## How is TTFE measured?

```mermaid
flowchart LR
    A["Claim<br/>(request a sandbox)"] --> B["Bind<br/>(pool assigns + provisions)"]
    B --> C["Exec-probe<br/>(websocket + first-instruction round-trip)"]
    C --> D["Webhook TTFE stamp<br/>(executor reports the true first-instruction timestamp)"]
    D --> E["Closed-schema render<br/>(results/latest.json &rarr; this page)"]
```

TTFE (Time To First Execution) is the **webhook-stamped** timestamp in step D — not pod-Ready,
which only proves the sandbox exists, not that it ran your code (see **Burst Create — TTFE
Corroboration** in [DETAILS.md](DETAILS.md) for the two claims side by side). Step E is the same
closed-schema render every table on this page goes through: a result only reaches `results/latest.json`
by clearing schema validation, so nothing between the probe and the page can silently drop or
reshape a number.
"""


def render_measurement_path_diagram():
    """Render the static "How is TTFE measured?" mermaid flowchart (epic #6669 WS2).

    Product-agnostic architecture-shape diagram (same posture as render_recipe): no measured
    numbers, so it can never go stale or contradict the machine-rendered tables above it. Always
    rendered — first machine-rendered *visual* on the public page (WS2 DoD: >=4 rendered visuals,
    regenerated by render/generate.py, not hand-drawn). Plain ```mermaid flowchart LR``` — GitHub's
    built-in mermaid renderer supports flowcharts natively, no external image build step, no new
    dependency (unlike a matplotlib/PNG route, which harness/requirements.txt deliberately omits).
    """
    return _MEASUREMENT_PATH_DIAGRAM.rstrip()


def render_recipe():
    """Render the static "Reproduce it" H2 block (#4021; hb#134 page-pass trim + rename).

    Product-agnostic architecture-shape prose, always rendered (the preamble forward-refs it).
    No measured numbers — the honest-today latency is referenced by pointer to the live
    Warm-Pool Acquisition / Concurrent Burst cells above, so this block can never go stale or
    contradict the machine-rendered tables. The runnable recipe (commands, pinned installs, CI)
    is cross-linked to recipe/REPRODUCE.md, not duplicated.

    hb#488 numbers-first slice-2 (alex 2026-07-26): the spelled-out warm-pool sizing math (the
    0.75 replica rule) and the cluster-shape enumeration (machine type, RuntimeClass, pod CIDR,
    pre-pull DaemonSet) relocate off the headline page into recipe/REPRODUCE.md — which already
    carries both in full — leaving only the vanilla-GKE promise, the recipe pointer, and the
    compact measured-vs-pending honesty note. Detail moves to the sub-page; numbers-first stays.
    """
    return _RECIPE.rstrip()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("results")
    args = ap.parse_args(argv)
    with open(args.results) as fh:
        results = json.load(fh)
    sys.stdout.write(render_product(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
