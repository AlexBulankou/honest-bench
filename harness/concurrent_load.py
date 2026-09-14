"""Fire-time concurrent-load provenance sampler (hb#880).

Captures a snapshot of the standing umbrella pipeline's per-scenario child Jobs
active on the SHARED scenarios cluster during a cold/warm TTFE sweep's fire
window, so cross-fire p95 deltas become attributable to background load rather
than being read as sweep-intrinsic noise. The motivating case surfaced on
hb#880: a true_ttfe p95 swing that plausibly tracks a readiness dip coinciding
with the standing umbrella children running on the same cluster the kata cold
benchmark fires on — collision-ack clears agent-owned jobs, but standing-pipeline
load varies by wall-clock, so fire-to-fire deltas inherit unlabeled background
variance this stamp makes attributable.

WHY FIRE-TIME (not retrospective): the umbrella children carry a 24h TTL GC
(k8s/sandbox/umbrella-cronjob.yaml ttlSecondsAfterFinished=86400), so a past
fire window's concurrent-load timestamps are GC'd within a day. The only place
to capture it is at fire time, co-located with the same boundary /metrics
scrapes the sweep already takes.

PUBLIC-REPO SAFETY (this file ships in the public honest-bench repo): the
namespace to scan and the label key that carries the scenario slug are read from
env / passed in by the caller with NO internal-string default. Unset => the
feature is INERT (returns None), degrade-closed. The real internal values are
supplied only by the private fire path (scripts/fire-hb-refresh-gke-kata.sh
substitutions), never defaulted here — the same pattern the fire path already
uses for the cluster name (empty-defaulted _CLUSTER in the public cloudbuild,
real value passed by the private wrapper).

HONESTY / DEGRADE-CLOSED (mirrors ``_sample_node_count`` in the sweeps):
  - unconfigured (no namespace) => None (feature off, not a fake 0);
  - any client/list exception => None (best-effort; a sampler hiccup must never
    fail a fire);
  - a genuine empty result (namespace scanned, zero active jobs) => a real
    zero-count snapshot, NOT None — "measured zero" is a first-class value
    distinct from "not measured", the same distinction ``ttfe_stamp`` draws for
    an unmeasured rung/count.
"""
import os

NAMESPACE_ENV = "CONCURRENT_LOAD_NAMESPACE"
SCENARIO_LABEL_ENV = "CONCURRENT_LOAD_SCENARIO_LABEL"


def config_from_env():
    """(namespace, scenario_label) from env, each None when unset/blank.

    No internal-string default lives here (public-repo safety, see module
    docstring). ``namespace is None`` => the sampler is inert.
    """
    ns = (os.environ.get(NAMESPACE_ENV) or "").strip() or None
    label = (os.environ.get(SCENARIO_LABEL_ENV) or "").strip() or None
    return ns, label


def sample_concurrent_load(batch_v1, namespace, scenario_label=None):
    """One point-in-time snapshot of active Jobs in ``namespace``.

    Returns ``{"active_count": int, "scenario_slugs": [str, ...]}`` on a
    successful scan (including a genuine zero), or ``None`` when the feature is
    unconfigured or the list call fails — degrade-closed so a sampler hiccup
    never fails the fire. ``scenario_slugs`` is populated only when
    ``scenario_label`` is given: the value of that label on each active Job (the
    public scenario kebab-slug from the umbrella source), deduped + sorted; Jobs
    missing the label contribute to ``active_count`` but not to the slug list.
    """
    if not namespace or batch_v1 is None:
        return None
    try:
        jobs = (batch_v1.list_namespaced_job(namespace=namespace) or None)
        items = (jobs.items if jobs is not None else None) or []
    except Exception:  # noqa: BLE001 — best-effort; never fail the fire
        return None
    active = []
    for j in items:
        status = getattr(j, "status", None)
        if status is not None and (getattr(status, "active", None) or 0) > 0:
            active.append(j)
    slugs = []
    if scenario_label:
        for j in active:
            meta = getattr(j, "metadata", None)
            labels = (getattr(meta, "labels", None) if meta is not None else None) or {}
            val = labels.get(scenario_label)
            if val:
                slugs.append(val)
    return {"active_count": len(active), "scenario_slugs": sorted(set(slugs))}


def aggregate_concurrent_load(samples):
    """Fold per-boundary snapshots into one provenance stamp.

    Ignores ``None`` samples (a boundary whose sample failed); returns ``None``
    iff EVERY sample is ``None`` (feature off, or never measured this fire) —
    mirroring ``ttfe_stamp``'s "None iff unmeasurable in every rung". Otherwise:

        {"peak_active_scenario_jobs": <max active_count across real samples>,
         "scenario_slugs": <sorted union of all slugs seen>,
         "n_samples": <count of real (non-None) samples>}

    Peak (not mean) is the honest attributor here: the worst-case concurrent
    background load the fire overlapped is what a p95 tail is most plausibly
    charged to.
    """
    real = [s for s in (samples or []) if isinstance(s, dict)]
    if not real:
        return None
    peak = max(int(s.get("active_count", 0) or 0) for s in real)
    slugs = set()
    for s in real:
        for slug in (s.get("scenario_slugs") or []):
            slugs.add(slug)
    return {
        "peak_active_scenario_jobs": peak,
        "scenario_slugs": sorted(slugs),
        "n_samples": len(real),
    }
