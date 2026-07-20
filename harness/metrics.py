"""Pure metrics-derivation core for the honest-benchmark harness (goal 2.1).

No I/O, no cluster, no clock -- every function is a deterministic transform of
in-memory samples into the locked emit-key values. Offline-testable with bare
python3 (the auto-refresh GH-runner needs no extra deps), mirroring the
`results_schema` and `session_turnover` pure/offline discipline.

The locked emit keys this module produces (the harness/render schema contract):

  ttfe_p50_ms, ttfe_p95_ms           -- TTFE distribution (Time-To-First-Execution)
  thpt_under_5s_per_node             -- sustained throughput @ <5s TTFE (sb/sec/node)
  thpt_under_1s_per_node             -- sustained throughput @ <1s TTFE (sb/sec/node)
  thpt_under_5s_per_cluster          -- opt-in MEASURED cluster rate (sb/sec), hb#132
  thpt_under_1s_per_cluster          -- opt-in MEASURED cluster rate (sb/sec), hb#132
  thpt_cluster_node_count            -- node count X the cluster rate was measured at
  exec_success_rate                  -- fraction of first-instructions that succeeded
  density_per_vcpu                   -- max concurrent sandboxes / per-node allocatable sandbox vCPU
  density_retention, thpt_retention  -- scale-proof linearity (value@max-nodes / value@1-node)

TTFE = Time-To-First-Execution: wall-clock from the sandbox-create request to the
moment the sandbox returned the RESULT of its first instruction (NOT pod-Ready).
This is the spec doc's headline semantics; Ready != TTFE.

Throughput definition (honest, reproducible, doc-validated):

  thpt@threshold (per node) = count(ttfe_ms <= threshold_ms) / window_s / node_count

A sandbox that never reached first-execution, or whose TTFE exceeded the
threshold, does NOT count toward the numerator -- so a burst where no sandbox
beats 1s yields throughput 0 (the doc's honest "print 0", not a blank). The
definition is doc-validated: a 200-sandbox warm-pool burst all under 5s in a ~50s
window on 1 node yields 200/50/1 = 4 sb/sec/node, matching the spec table.

Density denominator (the LOCKED reconcile, goal 2.1): per-node ALLOCATABLE vCPU of
the sandbox-schedulable nodepool -- NOT the cluster-wide capacity sum across all
nodes (the burst_create 0.45 reading, which folds in system-pool + control-plane
overhead). The page states the denominator next to the cell.
"""

from __future__ import annotations

from numbers import Real
from typing import Iterable, Optional, Sequence

# Doc TTFE thresholds (the two throughput bars the spec matrix reports).
THRESHOLD_5S_MS = 5000.0
THRESHOLD_1S_MS = 1000.0


def _finite_nonneg(samples: Iterable[Optional[Real]]) -> list[float]:
    """Keep only finite, non-negative, present (non-None) numeric samples."""
    out: list[float] = []
    for s in samples:
        if s is None:
            continue
        if not isinstance(s, Real):
            raise TypeError(f"non-numeric sample: {s!r}")
        f = float(s)
        if f != f or f in (float("inf"), float("-inf")):
            raise ValueError(f"non-finite sample: {s!r}")
        if f < 0:
            raise ValueError(f"negative sample: {s!r}")
        out.append(f)
    return out


def percentile(samples: Sequence[Real], p: float) -> float:
    """Linear-interpolation percentile (numpy-default method), p in [0, 100].

    Raises on an empty sample set -- a percentile of nothing is undefined, and the
    caller (assembler below) handles the no-samples case explicitly rather than
    fabricating a value.
    """
    vals = _finite_nonneg(samples)
    if not vals:
        raise ValueError("percentile of empty sample set")
    if not 0.0 <= p <= 100.0:
        raise ValueError(f"percentile p out of range: {p}")
    vals.sort()
    if len(vals) == 1:
        return vals[0]
    rank = (p / 100.0) * (len(vals) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(vals) - 1)
    frac = rank - lo
    return vals[lo] + (vals[hi] - vals[lo]) * frac


def throughput_per_node(
    ttfe_ms_samples: Iterable[Optional[Real]],
    threshold_ms: float,
    window_s: float,
    node_count: int,
) -> float:
    """Sandboxes/sec/node that reached first-execution within threshold_ms.

    None samples (never reached first-execution) never qualify. Returns 0.0 when
    no sample beats the threshold -- the honest "print 0", not a blank cell.
    """
    if window_s <= 0:
        raise ValueError(f"window_s must be > 0: {window_s}")
    if node_count < 1:
        raise ValueError(f"node_count must be >= 1: {node_count}")
    qualifying = 0
    for t in ttfe_ms_samples:
        if t is None:
            continue
        if not isinstance(t, Real):
            raise TypeError(f"non-numeric ttfe sample: {t!r}")
        if float(t) <= threshold_ms:
            qualifying += 1
    return round(qualifying / window_s / node_count, 3)


def throughput_per_cluster(
    ttfe_ms_samples: Iterable[Optional[Real]],
    threshold_ms: float,
    window_s: float,
) -> float:
    """Sandboxes/sec across the WHOLE CLUSTER that beat threshold_ms.

    Same qualifying rule as throughput_per_node but WITHOUT the node divisor:
    qualifying / window_s. This is the hb#132 per-cluster half of the dual
    matrix cell — a MEASURED rate from this fire's own samples. Per the #149
    per-mode contract, that figure equals the cell's defined quantity (the
    SLO-gated sustained rate) only when the fire's offered load sits AT that
    bar's SLO boundary — see the caller contract on ttfe_sla_metrics'
    cluster_node_count kwarg for why a single fire cannot honestly serve both
    bars and why the sweep derivation (harness/slo_rate.py) is the preferred
    producer. It is NEVER per-node x N extrapolation: the render forbids that,
    and this function cannot produce it because it never sees a node count.

    None samples (never reached first-execution) never qualify. Returns 0.0
    when no sample beats the threshold -- the honest "print 0".
    """
    if window_s <= 0:
        raise ValueError(f"window_s must be > 0: {window_s}")
    qualifying = 0
    for t in ttfe_ms_samples:
        if t is None:
            continue
        if not isinstance(t, Real):
            raise TypeError(f"non-numeric ttfe sample: {t!r}")
        if float(t) <= threshold_ms:
            qualifying += 1
    return round(qualifying / window_s, 3)


def exec_success_rate(exec_oks: Sequence[bool]) -> float:
    """Fraction of ATTEMPTED sandboxes whose first instruction succeeded.

    Denominator is every attempted sandbox, so a sandbox that never executed
    (timeout -> exec_ok False) drags the rate down honestly. The doc's
    "Execution Success Rate (Honesty Check)" column.
    """
    if not exec_oks:
        raise ValueError("exec_success_rate of empty attempt set")
    return round(sum(1 for x in exec_oks if x) / len(exec_oks), 4)


def density_per_vcpu(
    max_concurrent_sandboxes: int,
    allocatable_sandbox_vcpu_per_node: float,
) -> float:
    """Max concurrent sandboxes divided by per-node allocatable sandbox vCPU.

    The denominator is the LOCKED definition (goal 2.1): the node's
    sandbox-schedulable ALLOCATABLE vCPU, NOT total-cluster CAPACITY vCPU. This is
    the doc's ~1.88/vCPU basis; dividing the count by the cluster-wide capacity sum
    (incl system-pool + control-plane overhead) is the 0.45 burst_create reading.
    The page states the denominator next to the cell.
    """
    if allocatable_sandbox_vcpu_per_node <= 0:
        raise ValueError(
            f"allocatable_sandbox_vcpu_per_node must be > 0: {allocatable_sandbox_vcpu_per_node}"
        )
    if max_concurrent_sandboxes < 0:
        raise ValueError(f"max_concurrent_sandboxes must be >= 0: {max_concurrent_sandboxes}")
    return round(max_concurrent_sandboxes / allocatable_sandbox_vcpu_per_node, 2)


def parse_cpu_millicores(q: str) -> int:
    """Kubernetes CPU quantity -> integer millicores ("10m" -> 10, "1" -> 1000).

    The per-sandbox DECLARED cpu request is a whole-millicore config value, not a
    denominator measurement, so it rounds to int millicores (density's vCPU float
    is parse_cpu_quantity's job). Handles the n/u/m SI suffixes the API serializes;
    raises ValueError on empty/unparseable — a declared footprint is never guessed.
    """
    s = (q or "").strip()
    if not s:
        raise ValueError("empty CPU quantity")
    scale = 1.0
    if s.endswith("n"):
        scale, s = 1e-9, s[:-1]
    elif s.endswith("u"):
        scale, s = 1e-6, s[:-1]
    elif s.endswith("m"):
        scale, s = 1e-3, s[:-1]
    return round(float(s) * scale * 1000)


_MEM_BINARY = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "Pi": 2**50}
_MEM_DECIMAL = {"k": 10**3, "M": 10**6, "G": 10**9, "T": 10**12, "P": 10**15}


def parse_mem_mib(q: str) -> int:
    """Kubernetes memory quantity -> integer MiB ("16Mi" -> 16, "1Gi" -> 1024).

    Handles the binary (Ki/Mi/Gi/Ti/Pi) + decimal (k/M/G/T/P) suffixes plus a
    bare byte count, normalizing every form to whole MiB (rounded). The per-sandbox
    declared mem request is a config value; raises ValueError on empty/unparseable.
    """
    s = (q or "").strip()
    if not s:
        raise ValueError("empty memory quantity")
    bytes_val: float
    for suf, mult in _MEM_BINARY.items():
        if s.endswith(suf):
            bytes_val = float(s[: -len(suf)]) * mult
            break
    else:
        for suf, mult in _MEM_DECIMAL.items():
            if s.endswith(suf):
                bytes_val = float(s[: -len(suf)]) * mult
                break
        else:
            bytes_val = float(s)  # bare bytes
    return round(bytes_val / (2**20))


def retention(value_at_base: float, value_at_max_scale: float) -> float:
    """Scale-proof linearity ratio: per-node value retained at max scale vs 1-node base.

    1.0 = perfectly flat (ideal linear scaling -- the doc's "Holds Flat? Yes").
    Used for both density_retention and thpt_retention in the Scale Proof table.
    """
    if value_at_base <= 0:
        raise ValueError(f"value_at_base must be > 0: {value_at_base}")
    if value_at_max_scale < 0:
        raise ValueError(f"value_at_max_scale must be >= 0: {value_at_max_scale}")
    return round(value_at_max_scale / value_at_base, 3)


def ttfe_sla_metrics(
    ttfe_ms_samples: Sequence[Optional[Real]],
    exec_oks: Sequence[bool],
    window_s: float,
    node_count: int,
    *,
    max_concurrent_sandboxes: Optional[int] = None,
    allocatable_sandbox_vcpu_per_node: Optional[float] = None,
    bind_ms_samples: Optional[Sequence[Optional[Real]]] = None,
    exec_ms_samples: Optional[Sequence[Optional[Real]]] = None,
    cluster_node_count: Optional[int] = None,
) -> dict[str, float]:
    """Assemble the numeric sla_metrics dict a TTFE scenario emits.

    Returns ONLY finite numbers keyed by the locked emit keys, so it passes the
    results_schema `_coerce_sla_metrics` allow-list by construction. When no
    sandbox reached first-execution, the percentile keys are OMITTED (the cell
    renders pending) but throughput keys are emitted as 0.0 and exec_success_rate
    is still reported -- honest about both the missing distribution and the zero
    throughput.

    density_per_vcpu is emitted only when both density inputs are supplied (the
    suspend-from-resume scenario has density N/A in the doc, so it omits them).

    bind_p50_ms/bind_p95_ms + exec_p50_ms/exec_p95_ms (the TTFE DECOMPOSITION,
    inch #1): when bind_ms_samples / exec_ms_samples are supplied, emit the
    bind- and exec-latency percentiles alongside the TTFE percentiles. TTFE =
    bind (create->bound, i.e. provisioning) + exec (websocket setup +
    first-instruction round-trip). Publishing the split lets the page show WHERE
    a >1s warm-hit lives: a bind_p50 near ttfe_p50 means provisioning dominates
    (a real controller/clone target); a small bind_p50 with a large exec_p50
    means the exec channel (websocket setup) dominates (a harness/product
    artifact, not a controller regression).

    HONESTY — each is an INDEPENDENTLY-MEASURED percentile of its own per-claim
    distribution, NOT an arithmetic split: exec is measured per-claim as
    (ttfe_ms - bind_ms) for the SAME claim (both share the create() t0), then
    percentiled. Percentiles do NOT sum, so bind_p50 + exec_p50 need not equal
    ttfe_p50 — the caller pairs bind and exec per-claim so the exec percentile is
    genuine, never p50(ttfe) - p50(bind). Diagnostic-only — adds keys, changes no
    existing number. Each pair is emitted only when >=1 sample of that kind is
    present (a run with no bound claim has no bind/exec distribution to report).

    cluster_node_count (the hb#132 PER-CLUSTER emit leg, opt-in): when supplied,
    emit the COUPLED TRIPLE thpt_under_5s_per_cluster + thpt_under_1s_per_cluster
    + thpt_cluster_node_count. CALLER CONTRACT (amended per #149 — the matrix
    cluster cell is an SLO-GATED RATE, the sustained creation rate at which p95
    TTFE stays within the bar; NOT saturation/overload throughput): the
    per-cluster halves are measured from THIS fire's own samples
    (qualifying/window_s), so the figure equals the cell's defined quantity only
    when the fire's offered load is pinned AT a bar's SLO boundary. Because this
    triple couples BOTH bars, a single boundary fire cannot honestly serve both
    (the 5s- and 1s-boundary rates generally differ — the off-boundary bar would
    print an under-read). The PREFERRED producer of the matrix triple is
    therefore the step-up sweep derivation (harness/slo_rate.py:
    slo_sla_metrics_from_stepup), which fills each bar independently from its
    own compliant rung and pends the rest. This direct emit leg remains for the
    schema contract and for a fire that genuinely measures one bar's boundary
    (accepting the other bar's under-read is NOT acceptable — route through the
    sweep instead). The triple is all-or-nothing by construction here (never
    per_cluster without node_count — the render pins X from
    thpt_cluster_node_count), and the default None leaves every existing fire's
    emit byte-identical. NEVER derived as per-node x N.
    """
    present = [float(t) for t in ttfe_ms_samples if t is not None]
    metrics: dict[str, float] = {
        "thpt_under_5s_per_node": throughput_per_node(
            ttfe_ms_samples, THRESHOLD_5S_MS, window_s, node_count
        ),
        "thpt_under_1s_per_node": throughput_per_node(
            ttfe_ms_samples, THRESHOLD_1S_MS, window_s, node_count
        ),
    }
    if exec_oks:
        metrics["exec_success_rate"] = exec_success_rate(exec_oks)
    if present:
        metrics["ttfe_p50_ms"] = round(percentile(present, 50), 1)
        metrics["ttfe_p95_ms"] = round(percentile(present, 95), 1)
    if bind_ms_samples is not None:
        bind_present = [float(b) for b in bind_ms_samples if b is not None]
        if bind_present:
            metrics["bind_p50_ms"] = round(percentile(bind_present, 50), 1)
            metrics["bind_p95_ms"] = round(percentile(bind_present, 95), 1)
    if exec_ms_samples is not None:
        exec_present = [float(e) for e in exec_ms_samples if e is not None]
        if exec_present:
            metrics["exec_p50_ms"] = round(percentile(exec_present, 50), 1)
            metrics["exec_p95_ms"] = round(percentile(exec_present, 95), 1)
    if max_concurrent_sandboxes is not None and allocatable_sandbox_vcpu_per_node is not None:
        metrics["density_per_vcpu"] = density_per_vcpu(
            max_concurrent_sandboxes, allocatable_sandbox_vcpu_per_node
        )
    if cluster_node_count is not None:
        if cluster_node_count < 1:
            raise ValueError(f"cluster_node_count must be >= 1: {cluster_node_count}")
        metrics["thpt_under_5s_per_cluster"] = throughput_per_cluster(
            ttfe_ms_samples, THRESHOLD_5S_MS, window_s
        )
        metrics["thpt_under_1s_per_cluster"] = throughput_per_cluster(
            ttfe_ms_samples, THRESHOLD_1S_MS, window_s
        )
        metrics["thpt_cluster_node_count"] = cluster_node_count
    return metrics


def single_sample_ttfe_point(
    ttfe_ms: Optional[Real],
    exec_ok: bool,
    *,
    bind_ms: Optional[Real] = None,
    exec_ms: Optional[Real] = None,
) -> dict[str, float]:
    """Single-sample TTFE metrics for a one-shot activation scenario (cold / resume).

    The cold-start and resume-from-suspend cells measure ONE activation per fire,
    not a sustained burst, so they emit the percentile pair + exec-success + n=1
    and deliberately OMIT throughput (a per-node rate over n=1 is meaningless) and
    density (measured once via the warm-pool DENSITY_SOURCE_SCENARIO, never per
    one-shot sample). This is the shared assembler for those scenarios -- distinct
    from ``ttfe_sla_metrics``, which always emits the throughput pair.

    The percentile keys appear iff the exec succeeded (a failed exec has no honest
    first-instruction latency, so ttfe_ms is None): a failed one-shot exec emits
    ``exec_success_rate=0.0`` with no TTFE percentiles -- honest about both the
    failure and the absent latency. For n=1 the p50 and p95 are both the single
    sample.

    Optional bind/exec decomposition (inch #2, cold): when ``bind_ms`` (the
    create -> Ready provision time) and/or ``exec_ms`` (the residual ttfe_ms -
    bind_ms, i.e. the websocket + first-instruction round-trip on the
    already-Ready sandbox) are supplied, their p50/p95 pair is emitted alongside
    the ttfe pair -- one measured sample each, so p50 == p95 == the sample. These
    are INDEPENDENTLY MEASURED (bind from the create->Ready timer, exec as the
    residual against the SAME shared t0), never derived from the ttfe percentile.
    Both default None so the un-decomposed callers stay byte-identical.
    """
    metrics: dict[str, float] = {
        "exec_success_rate": exec_success_rate([exec_ok]),
        "n": 1,
    }
    if ttfe_ms is not None:
        p = percentile([ttfe_ms], 50)
        metrics["ttfe_p50_ms"] = round(p, 1)
        metrics["ttfe_p95_ms"] = round(p, 1)
    if bind_ms is not None:
        pb = percentile([bind_ms], 50)
        metrics["bind_p50_ms"] = round(pb, 1)
        metrics["bind_p95_ms"] = round(pb, 1)
    if exec_ms is not None:
        pe = percentile([exec_ms], 50)
        metrics["exec_p50_ms"] = round(pe, 1)
        metrics["exec_p95_ms"] = round(pe, 1)
    return metrics


def multi_sample_ttfe_point(
    ttfe_ms_samples: Sequence[Optional[Real]],
    exec_oks: Sequence[bool],
    *,
    bind_ms_samples: Optional[Sequence[Optional[Real]]] = None,
    exec_ms_samples: Optional[Sequence[Optional[Real]]] = None,
) -> dict[str, float]:
    """N-sample TTFE metrics for a REPEATED one-shot activation scenario.

    Generalizes ``single_sample_ttfe_point`` from one activation to N: the
    resume-from-suspend cell can loop N suspend->resume cycles (the cycle-count
    knob), and the cold-provision cell can loop N create->Ready->delete cycles
    (NATIVE_DIGEST_COLD_SAMPLES, hb#196), so a single noisy activation sample
    becomes a real p50/p95 distribution. Like the single-sample form it emits the
    percentile pair + exec-success + the sample count n, and OMITS throughput (a
    per-node rate over a handful of activations is meaningless) and density
    (measured once via the warm-pool DENSITY_SOURCE_SCENARIO, never per
    activation).

    n is the number of ATTEMPTED activations (len(exec_oks)); exec_success_rate is
    over all attempts so a failed cycle drags it down honestly. The percentile keys
    appear iff >=1 cycle produced a latency — a failed exec contributes None, is
    excluded from the distribution, but still counts against exec_success_rate.

    Optional bind/exec decomposition (inch #2, cold — the N-sample form of
    ``single_sample_ttfe_point``'s bind_ms/exec_ms kwargs): when
    ``bind_ms_samples`` (per-cycle create->Ready provision times) and/or
    ``exec_ms_samples`` (per-cycle residuals ttfe - bind against the SAME shared
    per-cycle t0) are supplied, their p50/p95 pair is emitted over the present
    (non-None) samples alongside the ttfe pair. INDEPENDENTLY MEASURED per cycle,
    never derived from the ttfe percentiles. Both default None so the
    un-decomposed callers (resume) stay byte-identical.

    For N=1 this returns output byte-identical to ``single_sample_ttfe_point``
    (with or without the decomposition kwargs), so the default (cycle_count=1 /
    samples=1) emit is unchanged.
    """
    if not exec_oks:
        raise ValueError("multi_sample_ttfe_point of empty attempt set")
    metrics: dict[str, float] = {
        "exec_success_rate": exec_success_rate(exec_oks),
        "n": len(exec_oks),
    }
    present = [float(t) for t in ttfe_ms_samples if t is not None]
    if present:
        metrics["ttfe_p50_ms"] = round(percentile(present, 50), 1)
        metrics["ttfe_p95_ms"] = round(percentile(present, 95), 1)
    if bind_ms_samples is not None:
        pb = [float(b) for b in bind_ms_samples if b is not None]
        if pb:
            metrics["bind_p50_ms"] = round(percentile(pb, 50), 1)
            metrics["bind_p95_ms"] = round(percentile(pb, 95), 1)
    if exec_ms_samples is not None:
        pe = [float(e) for e in exec_ms_samples if e is not None]
        if pe:
            metrics["exec_p50_ms"] = round(percentile(pe, 50), 1)
            metrics["exec_p95_ms"] = round(percentile(pe, 95), 1)
    return metrics


def suspend_latency_point(
    suspend_ms_samples: Sequence[Optional[Real]],
) -> dict[str, float]:
    """Administrative-suspend latency point for the suspend_resume cell.

    Measures the wall-clock cost of an ADMINISTRATIVE suspend: the
    operatingMode=Suspended patch return -> terminal Suspended state (backing Pod
    released + the Suspended condition observed). This is NOT an idle/auto-suspend
    latency -- upstream agent-sandbox has no idle-timeout or activity-reclaim path;
    operatingMode is the closed Running;Suspended enum, toggled only by a deliberate
    operator/user patch. The metric quantifies the response time of that
    administrative cost-lever, not any automatic reclamation.

    The suspend leg runs on EVERY suspend_resume cycle (unlike the TTFE-gated resume
    probe), so N cycles yield N samples. Emits:
      - suspend_latency_ms = median (p50) over the present samples -- the headline,
        robust to a single slow release; the REQUIRED spine (its presence is the
        render-side INERT gate for the whole block).
      - suspend_p90_ms = the p90 tail, emitted ONLY when n>=2 (a p90 over a single
        sample is just that sample, so it carries no tail information).

    Returns {} when no sample is present (a suspend leg that never reached terminal
    contributes None and is excluded) -- no fabricated number, mirroring the
    session_turnover / multi_sample_ttfe_point empty-emit discipline.
    """
    present = [float(t) for t in suspend_ms_samples if t is not None]
    if not present:
        return {}
    point: dict[str, float] = {
        "suspend_latency_ms": round(percentile(present, 50), 1),
    }
    if len(present) >= 2:
        point["suspend_p90_ms"] = round(percentile(present, 90), 1)
    return point
