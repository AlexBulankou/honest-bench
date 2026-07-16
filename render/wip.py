"""hb#166: the WORK_IN_PROGRESS.md single source of truth.

Every pending / absent / N-A DATA cell on the public pages links to an anchor on
`WORK_IN_PROGRESS.md`. The anchor set is DRIVEN OFF the closed pending-reason enum
in schema.py (`PENDING_REASONS`) plus one synthetic `na-by-construction` anchor for
the N/A-by-construction cells — so a rendered link can never dangle and the page can
never omit a reason the harness may emit. Adding an enum member without a catalog
entry (or vice-versa) fails loud at import (`_assert_catalog_covers_enum`).

Public-safety (this page ships verbatim to the PUBLIC repo): no internal names,
params, cluster ids, node-pool ids, or codenames. `AlexBulankou/a` is a PRIVATE
repo, so an a#NNNN reference renders as explicit "internal tracking a#NNNN" prose
(NOT a bare `#NNNN`, which GitHub would auto-link to a non-existent PUBLIC issue).
Public honest-bench issues render as normal `hb#NNNN` links; public upstream
projects (agent-sandbox, substrate) are named in plain English, with real
upstream GitHub issue/PR links (public OSS refs — hb#181) sourced from the
`upstream_links` mapping, never hand-typed.
"""

from schema import PENDING_REASONS
from upstream_links import upstream_prose_refs

WORK_IN_PROGRESS_FILE = "WORK_IN_PROGRESS.md"

# Synthetic anchor for the N/A-by-construction cells. These are NOT a harness
# pending_reason (they can NEVER be measured), so they are deliberately absent from
# PENDING_REASONS and carried here instead.
NA_BY_CONSTRUCTION = "na-by-construction"

# A bare `pending` token (a cell with no carried reason) is a genuinely not-yet-run
# cell — it maps to the `not-yet-measured` anchor.
BARE_PENDING_REASON = "not-yet-measured"

_HB = "https://github.com/AlexBulankou/honest-bench/issues"

# One entry per anchor. The anchor slug IS the dict key (== the enum value, or the
# synthetic `na-by-construction`). Each carries the hb#166 honest-tone contract:
#   what      — the metric in plain English
#   why       — the honest absence class (not-yet-measured / gated / N/A by construction)
#   in_flight — is anyone working it, and how
#   eta       — a date, or "gated on <named gate>" (never "soon")
#   trace     — tracking links (public hb# / plain-English upstream / internal a# prose)
WIP_CATALOG = {
    "cluster-fire": {
        "title": "Per-cluster throughput awaits a saturation fire (`cluster-fire`)",
        "what": (
            "The per-**cluster** sustained creation throughput — how many sandboxes/sec the "
            "whole cluster holds under that row's SLO bar. It is the second half of each dual "
            "throughput cell (`per-node · per-cluster`); the per-node half has already landed."
        ),
        "why": (
            "**not-yet-measured.** The per-node engineering rate is measured, but the validated "
            "per-cluster figure needs its own schema-validated cluster-saturation fire. We refuse "
            "to print a per-node × N extrapolation — that fiction breaks above the controller "
            "reconcile ceiling — so the cluster half stays `pending (cluster-fire)` until a real "
            "per-mode cluster fire lands the `thpt_*_per_cluster` fields."
        ),
        "in_flight": (
            "Yes — the per-activation-mode cluster-throughput fire that emits the per-cluster "
            "fields is the deliverable that graduates these halves."
        ),
        "eta": f"Gated on the per-mode cluster-throughput fire ([hb#132]({_HB}/132)).",
        "trace": f"[hb#132]({_HB}/132) (dual per-node + per-cluster throughput).",
    },
    "trust-gate": {
        "title": "SLO-rate fire ran; derivation refused by the trust gate (`trust-gate`)",
        "what": (
            "A warm-pool per-**cluster** SLO-rate cell whose measurement fire DID run, but "
            "whose per-mode derivation was refused: the controller-side rate leg disagreed "
            "with the acquisition-side leg beyond the pre-declared tolerance (rel-diff "
            "> 0.10) at every measured rung, on both runtimes."
        ),
        "why": (
            "**Gated (upstream, trust).** The two independent rate legs must agree before a "
            "number publishes; on the warm-pool path they do not — the controller startup-latency "
            "histogram double-records Ready transitions on stale-informer replays, inflating the "
            "controller leg ~1.7–2×. Cold-path control legs PASS the same gate on both runtimes, "
            "pinning the defect to the warm-pool path. The cell is honest-empty rather than "
            "publish a number whose cross-check fails."
        ),
        "in_flight": (
            "Yes — tracked upstream in the agent-sandbox controller: "
            + upstream_prose_refs("trust-gate")
            + ". Internal tracking a#4364 (gate exposure) / a#4277 (no tuning to avoid "
            "honest-empty)."
        ),
        "eta": (
            "Gated on the upstream histogram record-once fix. The cell graduates the moment a "
            "post-fix fire passes the agreement gate — no honest-bench-side date."
        ),
        "trace": (
            "Upstream agent-sandbox controller (histogram double-record): "
            + upstream_prose_refs("trust-gate")
            + ". Internal tracking a#4364."
        ),
    },
    "no-compliant-rung": {
        "title": "SLO-rate fire ran; no rung met the bar (`no-compliant-rung`)",
        "what": (
            "A cold-start per-**cluster** SLO-rate cell whose measurement fire DID run with the "
            "trust gate PASSING, but where every measured rung's p95 sits over the cell's SLO "
            "bar on the only available (literal upper-bound) basis."
        ),
        "why": (
            "**not-yet-graduated (basis-gated).** An SLO-gated rate cannot be published as 0 "
            "from a finite ladder — a lower untested rate could still comply — so \"no compliant "
            "rung ⇒ pend, never 0\". The literal TTFE basis is an UPPER bound (it includes probe "
            "scheduling overhead); the tighter true-TTFE basis has no production writer upstream, "
            "so the cell may yet fill once that lands."
        ),
        "in_flight": (
            "Yes — the true-TTFE annotation writer is tracked upstream: "
            + upstream_prose_refs("no-compliant-rung")
            + ". Internal tracking a#3975 (basis fallback)."
        ),
        "eta": (
            "Gated on the upstream true-TTFE writer, or a future fire whose literal-basis p95 "
            "clears the bar at some measured rate."
        ),
        "trace": (
            "Upstream agent-sandbox (end-to-end TTFE measurability): "
            + upstream_prose_refs("no-compliant-rung")
            + ". Internal tracking a#3975 / a#4364."
        ),
    },
    "upstream-blocked": {
        "title": "Resume-from-suspend is blocked upstream (`upstream-blocked`)",
        "what": (
            "TTFE and throughput for the **resume-from-suspend** activation mode — restore a "
            "previously-suspended sandbox and run the first instruction."
        ),
        "why": (
            "**Gated (upstream).** The run itself lands, but an upstream controller gap holds "
            "graduation: on gVisor the suspended condition never clears. This is a known upstream "
            "gap, NOT an unrun or failed cell. (The Kata + microVM resume cell is a separate story "
            "— `na-by-construction`, because this CRIU-based metric does not transfer to the Kata "
            "VM isolation model.)"
        ),
        "in_flight": (
            "Yes — tracked upstream in the agent-sandbox controller: "
            + upstream_prose_refs("upstream-blocked")
            + ". No honest-bench-side measurement can graduate it until the upstream fix lands."
        ),
        "eta": (
            "Gated on the upstream agent-sandbox resume-graduation fix. There is no "
            "honest-bench-side date — the cell graduates to a real number the moment upstream "
            "lands, not when a run is scheduled."
        ),
        "trace": (
            "Upstream agent-sandbox controller (resume graduation): "
            + upstream_prose_refs("upstream-blocked")
            + "."
        ),
    },
    "not-yet-measured": {
        "title": "Awaiting its measurement fire (`pending`)",
        "what": (
            "A cell whose metric simply has not been measured yet — a genuinely not-yet-run cell "
            "(for example, a throughput figure before its TTFE fire has run)."
        ),
        "why": (
            "**not-yet-measured.** No blocker — the measurement fire has not run for this cell. "
            "A bare `pending` (with no reason in parentheses) is always this class."
        ),
        "in_flight": (
            "Yes — filled by a manually-invoked TTFE / throughput refresh run. There is no "
            "automatic recurring cadence: the run is triggered by hand, and its results-publish "
            "step needs a credential that is still being provisioned."
        ),
        "eta": (
            "Gated on provisioning the refresh run's results-publish credential (internal "
            "tracking a#4183). No scheduled date — once that lands, the run is invoked by hand "
            "and the page regenerates from its results with no hand-entry."
        ),
        "trace": f"Refresh mechanism tracked in [hb#166]({_HB}/166); publish-credential gate: internal tracking a#4183.",
    },
    "requires-gvisor-runtime": {
        "title": "Needs a gVisor run (`requires-gvisor-runtime`)",
        "what": "A gVisor-family cell whose measurement requires the live node to run the gVisor runtime.",
        "why": (
            "**not-yet-measured (runtime-gated).** A single run measures one runtime; this run "
            "measured a different one, so the gVisor cell pends until a gVisor run fills it."
        ),
        "in_flight": (
            "Yes — filled by a manually-invoked gVisor refresh run (no automatic recurring "
            "cadence)."
        ),
        "eta": (
            "Gated on provisioning the refresh run's results-publish credential (internal "
            "tracking a#4183); fires on manual invocation once that lands. No scheduled date."
        ),
        "trace": f"Refresh mechanism tracked in [hb#166]({_HB}/166); publish-credential gate: internal tracking a#4183.",
    },
    "requires-kata-runtime": {
        "title": "Needs a Kata run (`requires-kata-runtime`)",
        "what": "A Kata-family cell whose measurement requires the live node to run the Kata runtime.",
        "why": (
            "**not-yet-measured (runtime-gated).** Symmetric with the gVisor case: this run "
            "measured a different runtime, so the Kata cell pends until a Kata run fills it."
        ),
        "in_flight": (
            "Yes — filled by a manually-invoked Kata refresh run on the Kata node pool (no "
            "automatic recurring cadence)."
        ),
        "eta": (
            "Gated on provisioning the refresh run's results-publish credential (internal "
            "tracking a#4183); fires on manual invocation once that lands. No scheduled date."
        ),
        "trace": f"Refresh mechanism tracked in [hb#166]({_HB}/166); publish-credential gate: internal tracking a#4183.",
    },
    "requires-gke": {
        "title": "Needs a GKE cluster (`requires-gke`)",
        "what": "A cell whose measurement requires a GKE cluster (the substrate these numbers are measured on).",
        "why": "**not-yet-measured (environment-gated).** The measurement pends until it runs on a GKE cluster.",
        "in_flight": (
            "Yes — filled by a manually-invoked refresh run on a GKE cluster (no automatic "
            "recurring cadence)."
        ),
        "eta": (
            "Gated on provisioning the refresh run's results-publish credential (internal "
            "tracking a#4183); fires on manual invocation once that lands. No scheduled date."
        ),
        "trace": f"Refresh mechanism tracked in [hb#166]({_HB}/166); publish-credential gate: internal tracking a#4183.",
    },
    "requires-kata-microvm": {
        "title": "Kata + microVM rows not yet measured (`requires-kata-microvm`)",
        "what": "The Kata + microVM runtime rows, where a Kata+microVM measurement has not yet run.",
        "why": (
            "**not-yet-measured.** The Kata + microVM matrix rows are uniformly awaiting their "
            "measurement; the public page carries no internal issue ref for them by the PII fence."
        ),
        "in_flight": (
            "Yes — tracked internally; filled by a manually-invoked Kata + microVM refresh run "
            "(no automatic recurring cadence)."
        ),
        "eta": (
            "Gated on provisioning the refresh run's results-publish credential (internal "
            "tracking a#4183); fires on manual invocation once that lands. No scheduled date."
        ),
        "trace": "Internal tracking (no public issue by the PII fence); publish-credential gate: internal tracking a#4183.",
    },
    "pool-topology-constrained": {
        "title": "Needs a pool sized for N concurrent warms (`pool-topology-constrained`)",
        "what": (
            "A cell whose run DID land, but whose number is a node-pool topology artifact — N "
            "concurrent microVM boots contend for a single pool node's vCPUs, stalling the "
            "marginal replica — rather than a runtime property."
        ),
        "why": (
            "**Gated (spend).** A representative figure needs a node pool sized for N concurrent "
            "warms, which is a deliberate spend action, not a re-run of the existing pool."
        ),
        "in_flight": "Not scheduled — spend-gated pending a deliberate pool-sizing decision.",
        "eta": "Gated on provisioning a larger pool (a deliberate spend decision).",
        "trace": "Internal tracking (spend decision).",
    },
    NA_BY_CONSTRUCTION: {
        "title": "N/A by construction — structurally impossible (`N/A`)",
        "what": (
            "The resume-from-suspend × Kata + microVM cell (and any cell rendered `N/A`)."
        ),
        "why": (
            "**N/A by construction.** CRIU checkpoint/restore does not transfer to the Kata VM "
            "isolation model, so this cell can NEVER be measured. This is distinct from `pending`, "
            "which awaits a run that is at least possible."
        ),
        "in_flight": "None — there is nothing to measure.",
        "eta": (
            "None. This is not a pending measurement and carries no ETA — it will never graduate "
            "to a number (an honest `N/A` beats an implied future measurement)."
        ),
        "trace": "None — structural, not tracked.",
    },
}

# Deterministic render order: the enum classes in a fixed sequence, then the
# synthetic N/A anchor last. A fixed tuple (not set iteration) keeps the generated
# page byte-stable across runs.
WIP_ORDER = (
    "not-yet-measured",
    "cluster-fire",
    "trust-gate",
    "no-compliant-rung",
    "upstream-blocked",
    "requires-gvisor-runtime",
    "requires-kata-runtime",
    "requires-gke",
    "requires-kata-microvm",
    "pool-topology-constrained",
    NA_BY_CONSTRUCTION,
)


def _assert_catalog_covers_enum():
    """Fail loud at import if the catalog and the pending-reason enum drift apart.

    Every PENDING_REASONS member must have a catalog entry (so a rendered
    `pending (<reason>)` can always link), and every catalog key must be either an
    enum member or the synthetic N/A anchor (no orphan sections). WIP_ORDER must
    list exactly the catalog keys once each (so the page is complete + deterministic).
    """
    catalog_keys = set(WIP_CATALOG)
    expected = set(PENDING_REASONS) | {NA_BY_CONSTRUCTION}
    missing = expected - catalog_keys
    if missing:
        raise AssertionError(f"WIP_CATALOG missing entries for: {sorted(missing)}")
    orphan = catalog_keys - expected
    if orphan:
        raise AssertionError(f"WIP_CATALOG has orphan entries (not in enum): {sorted(orphan)}")
    if list(WIP_ORDER) != sorted(WIP_ORDER, key=list(WIP_ORDER).index) or set(WIP_ORDER) != catalog_keys:
        raise AssertionError("WIP_ORDER must list each WIP_CATALOG key exactly once")


_assert_catalog_covers_enum()


def _link(anchor, text):
    return f"[{text}]({WORK_IN_PROGRESS_FILE}#{anchor})"


def wip_link(reason, text=None):
    """Explicit WIP-anchor link for a hand-authored cell (e.g. the kata snapshot-resume note).

    Raises if the reason is not catalogued, so a typo fails loud at generate time
    rather than shipping a dangling link.
    """
    if reason not in WIP_CATALOG:
        raise KeyError(f"no WIP anchor for reason {reason!r}")
    return _link(reason, reason if text is None else text)


# pending (<reason>)  |  bare pending  |  N/A  — matched in that precedence so a
# `pending (reason)` is never split into a bare-pending match.
_LINK_PATTERN = __import__("re").compile(
    r"pending \((?P<reason>[a-z0-9-]+)\)"  # pending (reason)
    r"|(?P<bare>pending)"                   # bare pending
    r"|(?P<na>N/A)"                         # N/A by construction
)


def link_pending(cell):
    """Wrap any pending / N-A token in a rendered DATA cell as a WIP-anchor link.

    Enum-driven: a `pending (<reason>)` links ONLY when <reason> is a catalogued
    anchor, so a non-enum free-text pending is left untouched and can never dangle.
    A bare `pending` links to the not-yet-measured anchor; `N/A` to the
    na-by-construction anchor. Handles embedded tokens (e.g. the cluster half of a
    dual `<node> /node · pending (cluster-fire)` cell).

    Apply ONLY to data cells (matrix rows + data tables) — NEVER to legend/prose,
    where the tokens are glossary entries, not measurements.
    """

    def repl(m):
        whole = m.group(0)
        if m.group("reason") is not None:
            reason = m.group("reason")
            return _link(reason, whole) if reason in WIP_CATALOG else whole
        if m.group("bare") is not None:
            return _link(BARE_PENDING_REASON, whole)
        if m.group("na") is not None:
            return _link(NA_BY_CONSTRUCTION, whole)
        return whole

    return _LINK_PATTERN.sub(repl, cell)


def build_work_in_progress():
    """Render the full WORK_IN_PROGRESS.md page (one section per catalogued anchor).

    Every section carries an explicit `<a id="slug"></a>` HTML anchor so the link
    target is decoupled from the heading prose. Deterministic (WIP_ORDER), so the
    output is byte-stable and guardable by a freshness test.
    """
    lines = [
        "# Work in progress — pending & absent cells",
        "",
        "Every `pending`, `N/A`, or otherwise-absent cell on this benchmark's pages links "
        "here, to the entry for **why** it is absent and **when** it graduates. This is the "
        "honesty contract: an absent cell is never a silent gap — it names its reason class, "
        "its status, and either a date or the named gate it waits on.",
        "",
        "Each entry declares: **What** (the metric), **Why absent** (not-yet-measured vs "
        "gated vs N/A-by-construction), **In flight** (who/what is working it), **ETA** (a "
        "date or a named gate — never “soon”), and **Trace** (tracking links). An ETA "
        "is a commitment or a named gate; a slipped ETA is updated here, not deleted.",
        "",
        "_Anchors and the entry set are generated from the closed pending-reason enum — this "
        "page is machine-rendered, not hand-maintained._",
        "",
    ]
    for anchor in WIP_ORDER:
        e = WIP_CATALOG[anchor]
        lines.append(f'<a id="{anchor}"></a>')
        lines.append("")
        lines.append(f"## {e['title']}")
        lines.append("")
        lines.append(f"- **What:** {e['what']}")
        lines.append(f"- **Why absent:** {e['why']}")
        lines.append(f"- **In flight:** {e['in_flight']}")
        lines.append(f"- **ETA:** {e['eta']}")
        lines.append(f"- **Trace:** {e['trace']}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
