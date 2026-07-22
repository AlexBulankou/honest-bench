"""Controller-histogram TTFE derivation -- warm/cold p50/95/99 straight from Prometheus.

This is the step-up item-2 path: take Time-To-First-Execution from the controller's
own Prometheus histogram ``agent_sandbox_claim_startup_latency_ms`` (claim-create ->
ready, labeled by ``launch_type`` warm/cold) instead of the exec-channel round-trip
probe in ``ttfe_probe.py``. The exec probe pays ~0.5-0.7s/claim of websocket setup that
is NOT part of activation -- a measurement artifact (the #44 class). Sourcing TTFE from
the controller's instrumented claim-create->ready span removes that artifact entirely.

## Why classic histogram_quantile, byte-for-byte

The step-up load test (CL2) reports its OWN TTFE percentiles via Prometheus's
``histogram_quantile`` over the same series. This module reimplements the *identical*
classic-histogram algorithm so that, fed the SAME scrape, the offline parser and CL2
produce the SAME numbers. That convergence is the point: if the two agree on a live fire
scrape, the exec-round-trip TTFE path is provably redundant and can be retired. A
faithful reimplementation (linear interpolation within the matched bucket; the +Inf
bucket returns the highest finite upper bound; the first finite bucket's lower bound is
0; defensive monotonic clamp) is what makes it a trustworthy cross-check oracle rather
than an independent approximation that would merely be "close".

## Pure + offline-testable, by construction

Every function here is a deterministic transform of an in-memory Prometheus exposition
string into numbers -- no scrape, no cluster, no clock -- mirroring the ``metrics`` and
``ttfe_probe`` pure-core discipline so the offline test runs under bare python3. The
scrape I/O + generate-wiring is a separate, post-fire phase (issue PHASE B); this module
is PHASE A and touches nothing live.

## Honest posture (mirrors metrics.ttfe_sla_metrics + the scale_proof contract)

  - A ``launch_type`` whose histogram has ZERO observations is OMITTED -- never a fake
    0ms (a warm pool that took no claims has no honest latency to report, exactly the
    doc's "print 0 only when you measured 0"). count==0 omits; it does not fabricate.
  - A missing metric, an unparseable exposition, or a histogram with no ``+Inf`` bucket
    yields an EMPTY result -- the caller reads empty as measured=False and excludes it
    from the verdict (the INFRA-vs-test split), rather than emitting a collapse 0.
  - Percentiles for a launch_type are emitted all-or-nothing: a histogram that can
    produce a valid p50 can produce p95/p99 too, so a partial triple never ships.

The emitted keys are the locked TTFE keys (``ttfe_p50_ms``, ``ttfe_p95_ms``,
``ttfe_p99_ms``) -- the metric is already in ms, so the quantiles are ms directly.
"""

from __future__ import annotations

import math
from typing import Optional

# The controller's headline TTFE histogram: claim-create -> ready, labeled launch_type.
# Distinct from agent_sandbox_claim_controller_startup_latency_ms (the PROXY_METRIC below),
# which EXCLUDES the claim-create->controller-observe queueing lag -- exactly the latency
# that grows under step-up backfill -- so it under-reports headline TTFE under load. We
# match the headline name EXACTLY (below) so the controller variant is never folded in.
HEADLINE_METRIC = "agent_sandbox_claim_startup_latency_ms"

# The controller-observed PROXY histogram: controller-first-observed -> ready (the controller
# stamps agents.x-k8s.io/controller-first-observed-at itself, so this populates on the
# DEPLOYED controller NOW, whereas HEADLINE_METRIC is .Observe()d only on claims carrying the
# upstream webhook annotation -- not yet deployed -- so it is dead-by-construction until then).
# Because it excludes the claim-create->controller-observe queueing lag, a percentile derived
# from it is a LOWER BOUND on the true end-to-end TTFE, never the headline number. It is
# surfaced as a DISTINCT caveated field, never aliased onto the headline (the exact-name match
# in parse_metric_histograms keeps the two from ever bleeding together).
PROXY_METRIC = "agent_sandbox_claim_controller_startup_latency_ms"

# The locked percentile triple (q in [0,1]) -> emit key.
_PCTILES = (("ttfe_p50_ms", 0.50), ("ttfe_p95_ms", 0.95), ("ttfe_p99_ms", 0.99))

# The proxy's distinct percentile triple -- separate emit keys so the lower-bound proxy is
# never confused with the headline TTFE downstream (the render/schema controller_startup
# field, #3954's lane, keys on exactly these names).
_PROXY_PCTILES = (
    ("controller_startup_p50_ms", 0.50),
    ("controller_startup_p95_ms", 0.95),
    ("controller_startup_p99_ms", 0.99),
)


class Histogram:
    """One parsed histogram series: its identifying labels + cumulative buckets + sum/count.

    ``buckets`` is a list of ``(le, cumulative_count)`` pairs (NOT yet sorted/clamped --
    ``histogram_quantile`` does that). ``labels`` are the series labels EXCLUDING ``le``
    (so warm and cold are two distinct Histograms). ``sum``/``count`` come from the
    matching ``_sum``/``_count`` series, or None if absent in the scrape.
    """

    __slots__ = ("labels", "buckets", "sum", "count")

    def __init__(self, labels: dict[str, str]):
        self.labels = labels
        self.buckets: list[tuple[float, float]] = []
        self.sum: Optional[float] = None
        self.count: Optional[float] = None

    @property
    def launch_type(self) -> Optional[str]:
        return self.labels.get("launch_type")


def _parse_labels(s: str) -> dict[str, str]:
    """Parse a Prometheus ``{a="x",b="y"}`` label body (the part inside the braces).

    Handles the standard quoted-value escapes (``\\\\`` ``\\"`` ``\\n``) and tolerates
    whitespace around ``=`` and after commas. Robust enough for the controller's simple
    ``launch_type`` / ``le`` labels without pulling in a Prometheus client.
    """
    labels: dict[str, str] = {}
    i, n = 0, len(s)
    while i < n:
        while i < n and s[i] in ", \t":
            i += 1
        if i >= n:
            break
        j = i
        while j < n and (s[j].isalnum() or s[j] == "_"):
            j += 1
        name = s[i:j]
        i = j
        while i < n and s[i] in " \t":
            i += 1
        if i >= n or s[i] != "=":
            break
        i += 1
        while i < n and s[i] in " \t":
            i += 1
        if i >= n or s[i] != '"':
            break
        i += 1
        chars: list[str] = []
        while i < n and s[i] != '"':
            if s[i] == "\\" and i + 1 < n:
                nxt = s[i + 1]
                chars.append({"n": "\n", "\\": "\\", '"': '"'}.get(nxt, nxt))
                i += 2
            else:
                chars.append(s[i])
                i += 1
        i += 1  # skip closing quote
        if name:
            labels[name] = "".join(chars)
    return labels


def _split_line(line: str) -> Optional[tuple[str, dict[str, str], float]]:
    """Split one exposition sample line into (metric_name, labels, value).

    Returns None for comments, blanks, or anything that does not parse as a sample.
    A trailing scrape timestamp (the optional 3rd field) is ignored. The value ``NaN``
    /``+Inf``/``-Inf`` parse via float(); an unparseable value drops the line.
    """
    line = line.strip()
    if not line or line[0] == "#":
        return None
    brace = line.find("{")
    if brace != -1:
        close = line.rfind("}")
        if close < brace:
            return None
        name = line[:brace].strip()
        labels = _parse_labels(line[brace + 1:close])
        rest = line[close + 1:].split()
    else:
        parts = line.split()
        if len(parts) < 2:
            return None
        name = parts[0]
        labels = {}
        rest = parts[1:]
    if not rest:
        return None
    try:
        value = float(rest[0])
    except ValueError:
        return None
    return name, labels, value


def _series_key(labels: dict[str, str], exclude: tuple[str, ...] = ()) -> tuple:
    """Stable identity for a series = sorted (k,v) over labels minus ``exclude``."""
    return tuple(sorted((k, v) for k, v in labels.items() if k not in exclude))


def parse_metric_histograms(text: str, metric_name: str = HEADLINE_METRIC) -> list[Histogram]:
    """Parse all histogram series for ``metric_name`` from a Prometheus exposition string.

    Groups ``_bucket`` samples by their non-``le`` label identity (so warm/cold become
    separate Histograms) and attaches the matching ``_sum``/``_count``. The metric name
    is matched EXACTLY (``metric_name + "_bucket"`` etc.), so a sibling metric that shares
    a prefix -- e.g. the ``_controller_`` variant -- is never folded in.
    """
    bucket_name = metric_name + "_bucket"
    sum_name = metric_name + "_sum"
    count_name = metric_name + "_count"

    hists: dict[tuple, Histogram] = {}
    sums: dict[tuple, float] = {}
    counts: dict[tuple, float] = {}

    for raw in text.splitlines():
        parsed = _split_line(raw)
        if parsed is None:
            continue
        name, labels, value = parsed
        if name == bucket_name:
            le_raw = labels.get("le")
            if le_raw is None:
                continue
            try:
                le = float(le_raw)  # "+Inf" -> inf, "100" -> 100.0
            except ValueError:
                continue
            key = _series_key(labels, exclude=("le",))
            h = hists.get(key)
            if h is None:
                non_le = {k: v for k, v in labels.items() if k != "le"}
                h = Histogram(non_le)
                hists[key] = h
            h.buckets.append((le, value))
        elif name == sum_name:
            sums[_series_key(labels)] = value
        elif name == count_name:
            counts[_series_key(labels)] = value

    out: list[Histogram] = []
    for key, h in hists.items():
        h.sum = sums.get(key)
        h.count = counts.get(key)
        out.append(h)
    return out


def aggregate_by_launch_type(hists: list[Histogram]) -> dict[str, Histogram]:
    """Collapse the ``sandbox_template`` dimension: sum buckets by ``le`` per ``launch_type``.

    The controller emits one histogram series per (launch_type, sandbox_template) pair, but
    the PUBLIC page surfaces latency ONLY by launch_type (warm/cold) -- the sandbox_template
    label VALUES are internal scenario names that must never reach the page. So before any
    percentile is taken, this collapses the template dimension: for each launch_type, sum the
    cumulative bucket count at each ``le`` across every template series (cumulative histograms
    are additive bucket-wise -- the union over templates is the bucket-wise sum), and likewise
    sum ``_count``/``_sum``. This mirrors CL2's ``sum(...) by (le)`` aggregation, so the
    offline parser and CL2 converge on the SAME per-launch_type percentile.

    Honesty + safety properties:
      - A histogram with no ``launch_type`` label is skipped (we never invent a bucket).
      - The returned Histograms carry ONLY the ``launch_type`` label -- the template dimension
        is gone by construction, so a downstream caller cannot leak a template name.
      - ``_count``/``_sum`` stay None for a launch_type only if NO contributing series carried
        them (an absent count is honest measured=False, not a fabricated 0).
      - Single-template input is identity: the sum over one series is that series, so this is
        backward-compatible with the headline metric's single-``launch_type`` scrapes.

    Because ``Histogram`` uses ``__slots__`` (no ad-hoc attributes), accumulation runs in a
    local ``le -> count`` dict and is materialized into the result Histogram's bucket list.
    """
    bucket_acc: dict[str, dict[float, float]] = {}
    count_acc: dict[str, Optional[float]] = {}
    sum_acc: dict[str, Optional[float]] = {}
    for h in hists:
        lt = h.launch_type
        if lt is None:
            continue
        acc = bucket_acc.setdefault(lt, {})
        for le, c in h.buckets:
            acc[le] = acc.get(le, 0.0) + c
        if h.count is not None:
            count_acc[lt] = (count_acc.get(lt) or 0.0) + h.count
        if h.sum is not None:
            sum_acc[lt] = (sum_acc.get(lt) or 0.0) + h.sum

    out: dict[str, Histogram] = {}
    for lt, acc in bucket_acc.items():
        agg = Histogram({"launch_type": lt})
        agg.buckets = sorted(acc.items(), key=lambda b: b[0])
        agg.count = count_acc.get(lt)
        agg.sum = sum_acc.get(lt)
        out[lt] = agg
    return out


def histogram_quantile(q: float, buckets: list[tuple[float, float]]) -> Optional[float]:
    """Classic Prometheus ``histogram_quantile`` over cumulative ``(le, count)`` buckets.

    Returns None when the quantile is undefined: no buckets, no ``+Inf`` bucket (the
    distribution is open-ended and unbounded above), or zero total observations. Otherwise
    reimplements Prometheus's ``bucketQuantile`` faithfully so the result matches CL2's
    own ``histogram_quantile`` on the same scrape:

      - buckets sorted ascending by ``le`` and monotonic-clamped (a scrape race can leave
        a cumulative count momentarily below its predecessor; clamp up, as Prometheus does);
      - rank = q * total, where total is the ``+Inf`` cumulative count;
      - find the first bucket whose cumulative count >= rank;
      - if that is the ``+Inf`` bucket, return the highest FINITE upper bound;
      - the first finite bucket's lower bound is 0 (latency >= 0); else the previous le;
      - linear-interpolate within the matched bucket.
    """
    if not 0.0 <= q <= 1.0:
        raise ValueError(f"quantile q out of [0,1]: {q}")
    if not buckets:
        return None
    bs = sorted(buckets, key=lambda b: b[0])
    clamped: list[tuple[float, float]] = []
    prev_c = 0.0
    for le, c in bs:
        c = c if c >= prev_c else prev_c
        clamped.append((le, c))
        prev_c = c
    bs = clamped

    if not math.isinf(bs[-1][0]) or bs[-1][0] < 0:
        return None  # last bucket must be +Inf
    total = bs[-1][1]
    if total <= 0:
        return None

    rank = q * total
    b = 0
    while b < len(bs) and bs[b][1] < rank:
        b += 1
    if b >= len(bs):
        b = len(bs) - 1

    if b == len(bs) - 1:
        # rank lands in the +Inf bucket -> highest finite upper bound (or None if none)
        return bs[-2][0] if len(bs) >= 2 else None
    if b == 0 and bs[0][0] <= 0:
        return bs[0][0]

    bucket_end = bs[b][0]
    if b > 0:
        bucket_start = bs[b - 1][0]
        count_in = bs[b][1] - bs[b - 1][1]
        rank_in = rank - bs[b - 1][1]
    else:
        bucket_start = 0.0
        count_in = bs[b][1]
        rank_in = rank
    if count_in <= 0:
        return bucket_end
    return bucket_start + (bucket_end - bucket_start) * (rank_in / count_in)


def _total_observations(h: Histogram) -> Optional[float]:
    """Total observations for a histogram: the +Inf cumulative count, else _count.

    The +Inf bucket count and the _count series agree by construction; prefer the +Inf
    bucket (it is what histogram_quantile keys on) and fall back to _count if the scrape
    omitted the +Inf line.
    """
    for le, c in h.buckets:
        if math.isinf(le):
            return c
    return h.count


def _percentiles(h: Histogram, pctiles: tuple[tuple[str, float], ...]) -> dict[str, float]:
    """The percentile triple for one histogram under ``pctiles``, or {} if unmeasurable.

    Returns {} (never a fabricated 0) when the histogram has zero observations or is
    malformed (no +Inf bucket). All-or-nothing: a partial triple never ships. The ``pctiles``
    tuple selects both the quantiles and their emit-key names, so the same engine drives the
    headline (``_PCTILES``) and the controller-startup proxy (``_PROXY_PCTILES``).
    """
    total = _total_observations(h)
    if not total or total <= 0:
        return {}
    out: dict[str, float] = {}
    for key, q in pctiles:
        v = histogram_quantile(q, h.buckets)
        if v is None:
            return {}  # malformed -> emit nothing rather than a partial triple
        out[key] = round(v, 1)
    return out


def ttfe_percentiles(h: Histogram) -> dict[str, float]:
    """{ttfe_p50_ms, ttfe_p95_ms, ttfe_p99_ms} for one histogram, or {} if unmeasurable."""
    return _percentiles(h, _PCTILES)


def _assemble_by_launch_type(
    text: str, metric_name: str, pctiles: tuple[tuple[str, float], ...]
) -> dict[str, dict[str, float]]:
    """Aggregate over sandbox_template, then emit the percentile triple per launch_type.

    The shared spine behind ``ttfe_by_launch_type`` (headline) and
    ``controller_startup_by_launch_type`` (proxy): parse every (launch_type, sandbox_template)
    series, collapse the template dimension via ``aggregate_by_launch_type`` (so the public
    page never sees a template name and the percentile is over the union of all templates),
    then take the ``pctiles`` triple of each aggregated histogram.
    """
    result: dict[str, dict[str, float]] = {}
    for lt, h in aggregate_by_launch_type(parse_metric_histograms(text, metric_name)).items():
        pct = _percentiles(h, pctiles)
        if pct:
            result[lt] = pct
    return result


def ttfe_by_launch_type(text: str, metric_name: str = HEADLINE_METRIC) -> dict[str, dict[str, float]]:
    """Assemble {launch_type: {ttfe_p50_ms, ttfe_p95_ms, ttfe_p99_ms}} from a scrape.

    The top-level result is the honest measured set: a launch_type appears IFF its AGGREGATED
    histogram (summed over all sandbox_template series) had >0 observations AND produced a
    full percentile triple. An empty dict means nothing was measured (the caller reads that as
    measured=False and excludes the point from the verdict -- the INFRA-vs-test split --
    rather than recording a collapse).

    Aggregation over sandbox_template (see ``aggregate_by_launch_type``) is load-bearing for
    two reasons: the controller emits one series per template, so the public page must sum them
    to get the real per-launch_type percentile; and the template label VALUES are internal
    scenario names that must never reach the page. A histogram with no ``launch_type`` label is
    skipped (the metric always carries it).
    """
    return _assemble_by_launch_type(text, metric_name, _PCTILES)


def controller_startup_by_launch_type(text: str) -> dict[str, dict[str, float]]:
    """Proxy lower-bound TTFE: {launch_type: {controller_startup_p50_ms, _p95_ms, _p99_ms}}.

    Same engine as ``ttfe_by_launch_type`` but over the PROXY_METRIC (controller-observed ->
    ready) with the proxy emit keys. Every number here is a LOWER BOUND on the true
    claim-create -> ready TTFE -- it excludes the claim-create -> controller-observe queueing
    lag -- so the caller MUST surface it as a distinct caveated field, never as the headline.
    A ``saturated`` fire makes even this lower bound a HARD FLOOR (a large tail spills past the
    top finite bucket once the warm pool drains), not a typical latency. Honesty spine is
    identical: a launch_type appears IFF its aggregated proxy histogram measured >0 and
    produced a full triple; otherwise it is omitted (measured=False), never a fabricated 0.
    """
    return _assemble_by_launch_type(text, PROXY_METRIC, _PROXY_PCTILES)


# --------------------------------------------------------------- per-step delta convergence
#
# ``ttfe_by_launch_type`` above reads ONE cumulative scrape, so its quantile is the
# CUMULATIVE-UNION over every claim since the controller started observing. CL2's step-up
# pareto percentiles, by contrast, come from ``histogram_quantile(q, sum(rate(..._bucket[
# $promRange])) by (le))`` -- rate-windowed PER STEP. (The ``rate()`` wrapper is a non-issue:
# histogram_quantile depends only on cumulative-count RATIOS, and rate() scales every bucket
# by the same 1/window factor, so the quantile is invariant to it.) What is NOT a non-issue
# is the POPULATION WINDOW: for a multi-step sweep on one controller, a single end-scrape's
# union quantile matches NO single per-step pareto point. The faithful offline cross-check of
# a per-step pareto point therefore needs the per-step INCREMENT: subtract the step's START
# cumulative scrape from its END cumulative scrape, per (launch_type, le), and run the SAME
# classic ``histogram_quantile`` on the increment. That is what these two functions do.
#
# (The single-step / fresh-restart case -- Phase-1's 300/s single step on a controller
# restarted to an empty histogram -- needs no diffing: an empty start scrape makes the
# increment equal the end scrape, and ``histogram_delta`` / the assembler below fall through
# to exactly ``ttfe_by_launch_type(end)``. So one code path covers both shapes honestly.)

# Counter-reset tolerance: bucket counts are integers, but absorb float-parse noise before
# declaring a genuine decrease (which signals a controller restart between the two scrapes).
_RESET_EPS = 1e-9


def histogram_delta(start: Histogram, end: Histogram) -> Optional[Histogram]:
    """Per-step increment ``end - start`` for one launch_type's cumulative histogram.

    Returns a new Histogram whose cumulative buckets are ``end_cum(le) - start_cum(le)``
    (aligned by ``le``; an ``le`` absent from ``start`` is treated as 0, so a launch_type
    that first appeared during this step carries its whole end histogram as the increment).
    The result is itself a valid cumulative histogram -- each per-bucket increment is a
    count of claims that completed in (start, end], hence non-negative, so the cumulative
    increment is monotonic in ``le`` -- and is fed straight to ``ttfe_percentiles``.

    Returns ``None`` (an honest measured=False, never a fabricated delta) when a counter
    RESET is detected: any ``le`` where ``end < start`` beyond ``_RESET_EPS`` means the
    controller restarted between the two scrapes, so the difference is meaningless. Tiny
    within-epsilon negatives (scrape-race noise) are clamped to 0 rather than rejected.
    """
    start_map = dict(start.buckets)
    delta = Histogram(dict(end.labels))
    for le, end_c in end.buckets:
        start_c = start_map.get(le, 0.0)
        inc = end_c - start_c
        if inc < -_RESET_EPS:
            return None  # counter reset between scrapes -> measured=False, honest
        delta.buckets.append((le, inc if inc > 0.0 else 0.0))

    if end.count is not None:
        start_count = start.count if start.count is not None else 0.0
        dc = end.count - start_count
        delta.count = dc if dc >= -_RESET_EPS else None
    if end.sum is not None:
        start_sum = start.sum if start.sum is not None else 0.0
        delta.sum = end.sum - start_sum
    return delta


def _assemble_by_launch_type_delta(
    start_text: str, end_text: str, metric_name: str, pctiles: tuple[tuple[str, float], ...]
) -> dict[str, dict[str, float]]:
    """Aggregate BOTH scrapes over sandbox_template, then take the per-step increment.

    The shared spine behind ``ttfe_by_launch_type_delta`` (headline) and
    ``controller_startup_by_launch_type_delta`` (proxy). Both consecutive cumulative scrapes
    are first collapsed over the template dimension (``aggregate_by_launch_type``), so the
    increment is taken on the per-launch_type union -- the template-collapse must precede the
    subtraction (summing then diffing equals diffing then summing for cumulative counts, but
    collapsing first is what keeps a template name from ever surfacing). Then ``histogram_delta``
    subtracts ``end - start`` per launch_type and ``pctiles`` are taken of the increment.
    """
    start_by_lt = aggregate_by_launch_type(parse_metric_histograms(start_text, metric_name))
    result: dict[str, dict[str, float]] = {}
    for lt, end_hist in aggregate_by_launch_type(
        parse_metric_histograms(end_text, metric_name)
    ).items():
        start_hist = start_by_lt.get(lt)
        if start_hist is None:
            delta = end_hist  # new launch_type since start: increment == end (start all-0)
        else:
            delta = histogram_delta(start_hist, end_hist)
            if delta is None:
                continue  # counter reset -> measured=False for this launch_type
        pct = _percentiles(delta, pctiles)
        if pct:
            result[lt] = pct
    return result


def ttfe_by_launch_type_delta(
    start_text: str, end_text: str, metric_name: str = HEADLINE_METRIC
) -> dict[str, dict[str, float]]:
    """Per-step ``{launch_type: {ttfe_p50_ms, ttfe_p95_ms, ttfe_p99_ms}}`` from the INCREMENT.

    Aggregate both consecutive cumulative scrapes over sandbox_template, subtract
    ``end - start`` per launch_type via ``histogram_delta``, and emit the percentile triple of
    each increment. This is the per-step analogue of ``ttfe_by_launch_type`` and converges
    against the matching CL2 pareto point (rate-windowed per step). The same honesty spine
    holds end-to-end:

      - a launch_type appears IFF its increment had >0 observations AND produced a full
        triple -- a step that took no new claims of a launch_type omits it (never a fake 0);
      - a counter reset for a launch_type (``histogram_delta`` -> None) drops that
        launch_type (measured=False), rather than reporting a meaningless cross-reset delta;
      - a launch_type present in ``end`` but absent from ``start`` carries its full end
        histogram as the increment (its observations all fell in this window);
      - an empty/absent ``start`` scrape (fresh-restarted controller) makes every increment
        equal its end histogram, so the result equals ``ttfe_by_launch_type(end_text)``.
    """
    return _assemble_by_launch_type_delta(start_text, end_text, metric_name, _PCTILES)


def controller_startup_by_launch_type_delta(
    start_text: str, end_text: str
) -> dict[str, dict[str, float]]:
    """Per-step proxy lower-bound: {launch_type: {controller_startup_p50_ms, _p95_ms, _p99_ms}}.

    The per-step analogue of ``controller_startup_by_launch_type`` -- same PROXY_METRIC, same
    proxy emit keys, same lower-bound + saturation caveats (every number is a floor on the
    true TTFE). Converges against the matching CL2 per-step pareto point for the controller
    metric. Honesty spine identical to the headline delta path.
    """
    return _assemble_by_launch_type_delta(start_text, end_text, PROXY_METRIC, _PROXY_PCTILES)


# --------------------------------------------------------------------------------------------
# webhook-stamped claim count (hb#5396 box-3 PHASE-B producer stamp)
# --------------------------------------------------------------------------------------------
#
# The true-TTFE basis (slo_rate.SLO_BASIS_TRUE_TTFE) is trusted only when the fired sweep
# record carries ``true_ttfe_webhook_stamped_claims`` >= 1 (slo_rate._true_ttfe_webhook_
# corroborated). That stamp is the number of claims whose t0 came from the asbx#761 webhook
# annotation ``agents.x-k8s.io/webhook-first-observed-at`` -- and because HEADLINE_METRIC is
# ``.Observe()``d ONCE per annotation-bearing claim, the webhook-stamped population IS the
# HEADLINE_METRIC observation count. So the producer reads the stamp straight off the same
# scrape it derives the pareto ttfe_p95_ms from -- one parser, one scrape, convergent by
# construction (the same reason the percentile path reimplements histogram_quantile byte-for-
# byte). These functions are the count half of that: PURE, offline, no scrape/cluster/clock,
# dead-by-construction until the webhook is live (returns None when the metric is absent),
# auto-populating once it is.


def _sum_total_observations(hists: list[Histogram]) -> Optional[float]:
    """Sum ``_total_observations`` across a list of histogram series, or None if none measured.

    Returns None (measured=False -- the INFRA-vs-test split) when the list is empty or every
    series is malformed (no +Inf and no _count), rather than a fabricated 0. A genuine 0
    total (a series present but with zero observations) contributes 0 and is honest -- distinct
    from "the metric was not there at all".
    """
    total = 0.0
    seen = False
    for h in hists:
        t = _total_observations(h)
        if t is None:
            continue
        total += t
        seen = True
    return total if seen else None


def webhook_stamped_claim_count(
    text: str, metric_name: str = HEADLINE_METRIC
) -> Optional[int]:
    """Count of webhook-stamped claims in a scrape: the summed HEADLINE_METRIC observations.

    The ``true_ttfe_webhook_stamped_claims`` stamp the box-3 producer writes into the sweep
    record. HEADLINE_METRIC is ``.Observe()``d once per claim bearing the asbx#761 webhook
    annotation, so the total observation count across all (launch_type, sandbox_template)
    series IS the webhook-stamped population.

    Honesty spine (mirrors ``ttfe_by_launch_type``):
      - the metric absent from the scrape (webhook not yet live / not deployed on this
        cluster) -> None (measured=False); the read-back guard reads absent-or-<1 as "discard
        true-TTFE, fall to the literal bases" -- exactly the dead-by-construction pre-Friday
        state;
      - a genuine 0 (metric present, zero observations) -> 0, an HONEST measured zero (the
        guard still rejects <1, so a 0-count fire correctly does not license the true-TTFE
        basis) -- never conflated with the absent case;
      - the ``_controller_`` sibling (PROXY_METRIC) is excluded by ``parse_metric_histograms``'
        exact-name match, so a controller-startup series never inflates the headline count.
    """
    hists = parse_metric_histograms(text, metric_name)
    total = _sum_total_observations(hists)
    if total is None:
        return None
    return int(round(total))


def webhook_stamped_claim_count_delta(
    start_text: str, end_text: str, metric_name: str = HEADLINE_METRIC
) -> Optional[int]:
    """Webhook-stamped claim count for the FIRED WINDOW: the HEADLINE_METRIC count INCREMENT.

    The per-step / windowed analogue of ``webhook_stamped_claim_count`` -- the stamp for a
    sweep whose t0/tN bracket a fire, so a claim count accumulated by an earlier warmup never
    inflates the fired-window population. Collapses the template dimension first (matching the
    percentile delta path), subtracts ``end - start`` per launch_type via ``histogram_delta``,
    and sums the per-launch_type increments.

    Honesty spine (identical to ``ttfe_by_launch_type_delta``):
      - a counter reset for ANY launch_type (``histogram_delta`` -> None, e.g. a controller
        restart mid-window) makes the windowed count unmeasurable -> None (measured=False),
        rather than reporting a meaningless cross-reset total;
      - a launch_type present in ``end`` but absent from ``start`` carries its full end count
        (all its observations fell in this window);
      - an empty/absent ``start`` scrape makes every increment equal its end histogram, so the
        result equals ``webhook_stamped_claim_count(end_text)`` (cumulative == windowed);
      - the metric absent from ``end`` -> None (measured=False), same as the cumulative path.
    """
    start_by_lt = aggregate_by_launch_type(parse_metric_histograms(start_text, metric_name))
    end_by_lt = aggregate_by_launch_type(parse_metric_histograms(end_text, metric_name))
    if not end_by_lt:
        return None  # metric absent from end -> measured=False
    total = 0.0
    for lt, end_hist in end_by_lt.items():
        start_hist = start_by_lt.get(lt)
        if start_hist is None:
            delta = end_hist  # new launch_type since start: increment == end (start all-0)
        else:
            delta = histogram_delta(start_hist, end_hist)
            if delta is None:
                return None  # counter reset -> whole windowed count is measured=False
        t = _total_observations(delta)
        if t is not None:
            total += t
    return int(round(total))
