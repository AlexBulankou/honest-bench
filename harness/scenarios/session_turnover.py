"""Session-turnover benchmark: how fast a warm pool refills a consumed slot.

The burst-create cell answers "how many sandboxes go Ready in <1s from a *full*
warm pool" — a cold-start throughput question. This cell answers the companion
*sustained-churn* question alex's agentic framing implies: once a session claims
a warm slot and a replacement must be provisioned, **how fast does the pool
return to full readiness?** That replenishment latency is what governs whether
back-to-back agent sessions keep hitting warm slots or start cold-binding — i.e.
whether the warm-pool promise holds under continuous turnover, not just on the
first burst.

    Setup    : SandboxWarmPool replicas: K (controller has filled all K slots).
    Action   : For each of N turnover cycles, fire ONE SandboxClaim. Binding it
               consumes a warm slot, so the pool's readyReplicas drops K -> K-1
               and the controller provisions a replacement to refill K-1 -> K.
               Measure the wall-clock from the observed drop to the observed
               return-to-K. Then delete the claim (release the session) and let
               the pool settle before the next cycle.
    Headline : refill_latency_ms = the MEDIAN replenishment latency across the N
               cycles (milliseconds). Median, not mean — robust to a single
               cold-tail provision. n = the number of cycles.
    Why      : Sustained agentic throughput is bounded by refill latency: if
               sessions turn over faster than the pool replenishes, the warm
               tier drains and later claims cold-bind. The median is falsifiable
               — a stranger reruns the harness and gets the same shape.

## Test shape

1. Provision a SandboxTemplate (minimal busybox — controller-level provisioning
   latency is what the scenario measures, not the user's image-build time).
2. Create SandboxWarmPool replicas=K; wait until status.readyReplicas=K.
3. For cycle i in 0..N-1:
   a. Re-assert the pool is at readyReplicas=K (a clean baseline).
   b. Create claim_i against the pool.
   c. Tight-poll the pool's readyReplicas: record `t_drop` on the first
      observation that it fell below K (the claim consumed a slot), then record
      `t_refill` on the first observation that it returned to >=K (the
      controller replenished the consumed slot).
   d. refill_ms_i = (t_refill - t_drop) * 1000. A cycle that never dropped, or
      never refilled within the per-cycle window, records None (a non-completer).
   e. Delete claim_i (release the session); best-effort wait for the pool to
      settle back to K before the next cycle (not part of the measurement).
4. refill_latency_ms = median(completed refill_ms). PASS = enough cycles
   completed AND the median cleared the ceiling:
   completed >= ceil(N * MIN_COMPLETED_RATIO) AND median < REFILL_CEILING_MS.
5. Cleanup: any surviving claim, the pool, the template.

## Why the measurement is pool-count-driven (not claim-Ready-driven)

The consume event and the replenishment event are both visible in ONE place —
the warm pool's status.readyReplicas. A claim binding pulls a slot out of the
ready tier (K -> K-1); the controller's replacement provision puts one back
(K-1 -> K). Reading the single pool counter captures both edges without a
per-claim Ready watch, so the metric needs no extra RBAC beyond the pool/claim
CRUD the setup already uses. The only blind spot is a replenishment faster than
one poll interval (_POLL_S, 100ms) — implausible for provisioning a fresh pod,
so a missed-drop records an honest None rather than a fabricated fast number.

## Crash posture

Infrastructure failures (controller unhealthy, CRDs missing, RBAC denied) raise
— the harness loop records a crash as a FAIL cell, never a fabricated PASS.
Scenario-outcome FAILs (pool refilled too slowly, or too few cycles completed)
return ("FAIL", "<excerpt>", sla_metrics) with the real measured median
surfaced whenever at least one cycle completed.
"""

from __future__ import annotations

try:  # package context (production: run.py loads harness.scenarios.session_turnover)
    from ._apiversion import claim_gvr, ext_api_version, template_gvr, warmpool_gvr
    from ._kube import load_cluster_config
except ImportError:  # standalone (dependency-free test from the scenarios/ dir)
    from _apiversion import claim_gvr, ext_api_version, template_gvr, warmpool_gvr
    from _kube import load_cluster_config

import logging
import math
import os
import statistics
import time
import uuid

log = logging.getLogger("sandbox-scenario.session-turnover")


_NAMESPACE = os.environ.get("BENCH_NAMESPACE", "default")
_SANDBOX_IMAGE = os.environ.get("SESSION_TURNOVER_SANDBOX_IMAGE", "busybox:1.36")

# Optional RuntimeClass for the pool's pods. Default "" -> omit the field, so the
# pods run under the node's default runtime (runc) — correct for vanilla kind
# (the auto-refresh Action runner). Set SESSION_TURNOVER_RUNTIME_CLASS=gvisor to
# measure refill latency under the gVisor runtime on a gke-sandbox cluster, so
# the published refill_latency_ms is a REAL gVisor-isolated number (the build
# banner's cluster_substrate makes the substrate explicit). Same probe, two
# substrates — no separate scenario. Mirrors burst_create's knob exactly.
_RUNTIME_CLASS = os.environ.get("SESSION_TURNOVER_RUNTIME_CLASS", "")

# Warm-pool size. Turnover consumes one slot per cycle, so a small pool suffices
# — K small keeps the fire cost low (K busybox pods at 10m each) while a K>=2
# default keeps the drop a *partial* drain (K -> K-1), representative of a real
# maintained warm pool rather than a fully-drained K=1 edge. Env-tunable up for a
# larger pool on a real GKE cluster.
_POOL_REPLICAS = int(os.environ.get("SESSION_TURNOVER_POOL_REPLICAS", "4"))

# Number of turnover cycles measured for the median. 5 gives a stable median
# without a long fire; env-tunable up for a tighter distribution.
_CYCLES = int(os.environ.get("SESSION_TURNOVER_CYCLES", "5"))

# PASS bar: the median replenishment latency (ms) must clear this ceiling.
# Default 10000ms (10s) is a generous upper bound for "the pool actually refills
# in a reasonable time" — a replenishment is a single-pod cold provision, so it
# sits in the cold-start envelope, not the sub-1s warm-bind one. Tune down after
# the first real fire surfaces the actual distribution.
_REFILL_CEILING_MS = float(
    os.environ.get("SESSION_TURNOVER_REFILL_CEILING_MS", "10000")
)

# PASS gate: fraction of the N cycles that must yield a refill measurement. 0.8
# tolerates one flaky cycle in five (a missed drop / a cycle that never refilled
# within the window) without failing an otherwise-healthy pool.
_MIN_COMPLETED_RATIO = float(
    os.environ.get("SESSION_TURNOVER_MIN_COMPLETED_RATIO", "0.8")
)

# Public benchmark metric keys. lowercase-alnum+underscore so they pass the
# emitter's _METRIC_KEY_RE with no harness-schema change; render/schema.py's
# METRIC_LABELS registers each display label (the render-side lane).
# refill_latency_ms = MEDIAN replenishment latency (the headline, robust to a
# single cold-tail provision); refill_p90_ms = the p90 tail, surfaced as a
# tail column so the floor-not-ceiling framing shows the slow end too.
_KEY_REFILL = "refill_latency_ms"
_KEY_REFILL_P90 = "refill_p90_ms"

# Timeouts. Pool warmup: 240s for the initial K-slot fill. Per-cycle refill:
# 180s — a replenishment provisions a fresh pod, which can take 30-90s on a cold
# node; we want to measure it, not time it out early. Inter-cycle settle: bounded
# best-effort wait for the pool to return to K after a release. All env-tunable;
# gVisor (runsc) adds per-pod sandbox-init overhead, so raise the warmup/refill
# windows on a gke-sandbox cluster rather than crash-FAIL a slow-but-healthy pool.
_WARMUP_TIMEOUT_S = int(os.environ.get("SESSION_TURNOVER_WARMUP_TIMEOUT_S", "240"))
_REFILL_TIMEOUT_S = int(os.environ.get("SESSION_TURNOVER_REFILL_TIMEOUT_S", "180"))
_SETTLE_TIMEOUT_S = int(os.environ.get("SESSION_TURNOVER_SETTLE_TIMEOUT_S", "120"))
_POLL_S = 0.1  # pool readyReplicas poll — fine enough for a seconds-scale refill

# CR coordinates.
_TPL_GVR = template_gvr()
_CLM_GVR = claim_gvr()
_SWP_GVR = warmpool_gvr()

_SCENARIO_LABEL = {"honest-bench/scenario": "session-turnover"}


def _build_template_manifest(template_name: str) -> dict:
    """Minimal busybox SandboxTemplate.

    When _RUNTIME_CLASS is set (e.g. "gvisor" on a gke-sandbox cluster), the
    pod's `runtimeClassName` is pinned so the whole warm pool — and therefore the
    measured refill — runs under that runtime, and the GKE-Sandbox node taint is
    tolerated so the pods can land on the gVisor node pool. Omitted by default so
    a vanilla-kind run (no gVisor RuntimeClass) is not stranded Pending.
    """
    pod_spec = {
        "containers": [
            {
                "name": "sandbox",
                "image": _SANDBOX_IMAGE,
                "imagePullPolicy": "IfNotPresent",
                "command": ["sh", "-c", "sleep 600"],
                "resources": {
                    "requests": {"cpu": "10m", "memory": "16Mi"},
                    "limits": {"cpu": "100m", "memory": "64Mi"},
                },
            },
        ],
        "restartPolicy": "Never",
    }
    if _RUNTIME_CLASS:
        pod_spec["runtimeClassName"] = _RUNTIME_CLASS
        # GKE Sandbox taints its gVisor node pool sandbox.gke.io/runtime=<rc>:NoSchedule
        # so non-sandboxed Pods can't land there. A Pod pinned to the gVisor runtime
        # must tolerate that taint or it stays Pending and the warm pool never fills.
        # Gated on _RUNTIME_CLASS (a vanilla-kind run has no such taint) and keyed
        # operator=Exists so any value (gvisor / gvisor-experimental) is covered.
        pod_spec["tolerations"] = [
            {
                "key": "sandbox.gke.io/runtime",
                "operator": "Exists",
                "effect": "NoSchedule",
            },
        ]
    return {
        "apiVersion": ext_api_version(),
        "kind": "SandboxTemplate",
        "metadata": {
            "name": template_name,
            "namespace": _NAMESPACE,
            "labels": dict(_SCENARIO_LABEL),
        },
        "spec": {"podTemplate": {"spec": pod_spec}},
    }


def _build_warmpool_manifest(
    pool_name: str, template_name: str, replicas: int,
) -> dict:
    """SandboxWarmPool with `replicas` slots, referencing the template."""
    return {
        "apiVersion": ext_api_version(),
        "kind": "SandboxWarmPool",
        "metadata": {
            "name": pool_name,
            "namespace": _NAMESPACE,
            "labels": dict(_SCENARIO_LABEL),
        },
        "spec": {
            "replicas": replicas,
            "sandboxTemplateRef": {"name": template_name},
        },
    }


def _build_claim_manifest(claim_name: str, pool_name: str) -> dict:
    """Minimum-viable SandboxClaim (binds via warmPoolRef)."""
    return {
        "apiVersion": ext_api_version(),
        "kind": "SandboxClaim",
        "metadata": {
            "name": claim_name,
            "namespace": _NAMESPACE,
            "labels": dict(_SCENARIO_LABEL),
        },
        "spec": {"warmPoolRef": {"name": pool_name}},
    }


def _percentile(sorted_vals: list[float], q: float) -> float | None:
    """Nearest-rank percentile of an already-sorted list (q in [0, 1]).

    Pure helper for the breakdown's tail figure. Returns None on an empty list.
    Nearest-rank (index = ceil(q * m) - 1, clamped) avoids interpolation, so a
    small-N turnover sample reports an actual observed latency, not a synthesised
    between-samples value.
    """
    m = len(sorted_vals)
    if m == 0:
        return None
    idx = math.ceil(q * m) - 1
    idx = max(0, min(idx, m - 1))
    return sorted_vals[idx]


def _classify_turnover(
    refills_ms: dict[str, float | None],
    *,
    cycle_count: int,
    refill_ceiling_ms: float,
    min_completed_ratio: float,
) -> tuple[bool, dict, dict]:
    """Pure turnover classifier. No cluster, no clock — unit-testable.

    `refills_ms` maps each turnover cycle to its measured replenishment latency
    in milliseconds, or None if that cycle never observed a drop+refill within
    the window. Returns (passed, breakdown, sla_metrics):
      - refill_latency_ms = median of the completed cycles' latencies
      - passed iff completed >= ceil(cycle_count * min_completed_ratio)
        AND that median < refill_ceiling_ms
    sla_metrics is emitted whenever at least one cycle completed (a real
    measurement, surfaced even on FAIL — mirrors burst_create surfacing the real
    count on a partial-delivery FAIL); zero completers emit {} so a pool that
    never refilled publishes no fabricated number.
    """
    completed = [v for v in refills_ms.values() if v is not None]
    timeouts = [k for k, v in refills_ms.items() if v is None]
    completed_sorted = sorted(completed)

    median_ms = statistics.median(completed_sorted) if completed_sorted else None
    p90_ms = _percentile(completed_sorted, 0.9)
    threshold = math.ceil(cycle_count * min_completed_ratio)
    enough = len(completed) >= threshold
    passed = enough and median_ms is not None and median_ms < refill_ceiling_ms

    breakdown = {
        "cycle_count": cycle_count,
        "completed_count": len(completed),
        "completed_threshold": threshold,
        "refill_ceiling_ms": refill_ceiling_ms,
        "median_ms": median_ms,
        "p90_ms": p90_ms,
        "min_ms": completed_sorted[0] if completed_sorted else None,
        "max_ms": completed_sorted[-1] if completed_sorted else None,
        "timeouts": timeouts,
        "all_refills_ms": completed_sorted,
    }

    if completed:
        sla_metrics = {
            _KEY_REFILL: float(median_ms),
            _KEY_REFILL_P90: float(p90_ms),
            "n": cycle_count,
        }
    else:
        sla_metrics = {}

    return passed, breakdown, sla_metrics


def _read_pool_ready(custom, *, pool_name: str) -> int | None:
    """Get the warm pool's status.readyReplicas, or None on a transient error.

    Returns 0 when the field is absent (pool exists but not yet filled). Returns
    None only on an API error, so the caller can retry rather than mistake a
    transient blip for a zero-ready pool.
    """
    group, version, plural = _SWP_GVR
    try:
        obj = custom.get_namespaced_custom_object(
            group=group, version=version, namespace=_NAMESPACE,
            plural=plural, name=pool_name,
        )
    except Exception as e:  # noqa: BLE001 — best-effort, caller retries
        log.warning("poll: get pool %s failed: %s — retrying", pool_name, e)
        return None
    status = (obj or {}).get("status") or {}
    return int(status.get("readyReplicas") or 0)


def _wait_pool_at_least(
    custom, *, pool_name: str, target: int, timeout_s: int,
) -> bool:
    """Poll until readyReplicas >= target, or timeout. True if reached."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        ready = _read_pool_ready(custom, pool_name=pool_name)
        if ready is not None and ready >= target:
            return True
        time.sleep(1.0)
    return False


def _measure_one_refill(
    custom, *, pool_name: str, target_k: int, timeout_s: float,
) -> float | None:
    """One turnover cycle's replenishment latency in ms, or None.

    Assumes the pool is at readyReplicas==target_k and a claim has just been
    fired. Tight-polls the pool: records t_drop on the first observation that
    readyReplicas fell below target_k (the claim consumed a slot), then t_refill
    on the first observation that it returned to >=target_k (the controller
    replenished). Returns (t_refill - t_drop) * 1000, or None if the drop or the
    refill was not observed within `timeout_s`.
    """
    deadline = time.monotonic() + timeout_s
    t_drop: float | None = None
    while time.monotonic() < deadline:
        ready = _read_pool_ready(custom, pool_name=pool_name)
        now = time.monotonic()
        if ready is None:
            time.sleep(_POLL_S)
            continue
        if t_drop is None:
            if ready < target_k:
                t_drop = now
        else:
            if ready >= target_k:
                return (now - t_drop) * 1000.0
        time.sleep(_POLL_S)
    return None


def _delete_claim(custom, *, claim_name: str) -> None:
    """Best-effort delete of a single claim (404 tolerated)."""
    from kubernetes.client.exceptions import ApiException
    try:
        custom.delete_namespaced_custom_object(
            group=_CLM_GVR[0], version=_CLM_GVR[1], namespace=_NAMESPACE,
            plural=_CLM_GVR[2], name=claim_name,
        )
    except ApiException as e:
        if e.status != 404:
            log.warning("delete claim %s failed: %s", claim_name, e)


def _cleanup(
    custom, *, claim_names: list[str], pool_name: str, template_name: str,
) -> None:
    """Best-effort delete: all claims, then pool, then template."""
    from kubernetes.client.exceptions import ApiException
    for name in claim_names:
        _delete_claim(custom, claim_name=name)
    for (label, gvr, name) in (
        ("warmpool", _SWP_GVR, pool_name),
        ("template", _TPL_GVR, template_name),
    ):
        group, version, plural = gvr
        try:
            custom.delete_namespaced_custom_object(
                group=group, version=version, namespace=_NAMESPACE,
                plural=plural, name=name,
            )
        except ApiException as e:
            if e.status != 404:
                log.warning("cleanup: delete %s %s failed: %s", label, name, e)


def run(scenario_name: str) -> tuple[str, str, dict]:
    """Provision pool, run N turnover cycles, classify on the median refill.

    Returns a 3-tuple (outcome, excerpt, sla_metrics). `sla_metrics` carries
    refill_latency_ms (the median replenishment latency) + the reserved "n" (=
    cycles) when at least one cycle completed; a pool that never refilled emits
    {} (no fabricated number). The metric is surfaced on PASS and FAIL so a pool
    that refilled-but-too-slowly still publishes the real median it achieved.
    """
    from kubernetes import client as k8s_client

    # Portable kubeconfig load (see _kube.load_cluster_config): an explicit
    # KUBECONFIG wins, else in-cluster when running as a pod, else the default
    # kubeconfig.
    load_cluster_config()

    custom = k8s_client.CustomObjectsApi()

    suffix = uuid.uuid4().hex[:8]
    template_name = f"tmpl-{suffix}"
    pool_name = f"pool-{suffix}"
    claim_names = [f"turn{i:02d}-{suffix}" for i in range(_CYCLES)]

    log.info(
        "creating Template %s + WarmPool %s (replicas=%d); will run %d turnover cycles",
        template_name, pool_name, _POOL_REPLICAS, _CYCLES,
    )
    custom.create_namespaced_custom_object(
        group=_TPL_GVR[0], version=_TPL_GVR[1], namespace=_NAMESPACE,
        plural=_TPL_GVR[2], body=_build_template_manifest(template_name),
    )
    custom.create_namespaced_custom_object(
        group=_SWP_GVR[0], version=_SWP_GVR[1], namespace=_NAMESPACE,
        plural=_SWP_GVR[2],
        body=_build_warmpool_manifest(pool_name, template_name, _POOL_REPLICAS),
    )

    try:
        log.info(
            "waiting for WarmPool %s to reach readyReplicas=%d (window=%ds)",
            pool_name, _POOL_REPLICAS, _WARMUP_TIMEOUT_S,
        )
        if not _wait_pool_at_least(
            custom, pool_name=pool_name,
            target=_POOL_REPLICAS, timeout_s=_WARMUP_TIMEOUT_S,
        ):
            raise RuntimeError(
                f"SandboxWarmPool {pool_name} did not reach "
                f"readyReplicas>={_POOL_REPLICAS} within {_WARMUP_TIMEOUT_S}s"
            )

        refills_ms: dict[str, float | None] = {}
        for i, name in enumerate(claim_names):
            # Re-assert a clean K-ready baseline before each cycle (the first is
            # already warm from the warmup above; later cycles wait for the prior
            # release to settle). A baseline that never settles is recorded as a
            # non-completing cycle rather than crashing the whole scenario.
            if i > 0 and not _wait_pool_at_least(
                custom, pool_name=pool_name,
                target=_POOL_REPLICAS, timeout_s=_SETTLE_TIMEOUT_S,
            ):
                log.warning(
                    "cycle %d: pool did not settle back to readyReplicas=%d "
                    "within %ds — recording cycle as non-completing",
                    i, _POOL_REPLICAS, _SETTLE_TIMEOUT_S,
                )
                refills_ms[name] = None
                continue

            custom.create_namespaced_custom_object(
                group=_CLM_GVR[0], version=_CLM_GVR[1], namespace=_NAMESPACE,
                plural=_CLM_GVR[2],
                body=_build_claim_manifest(name, pool_name),
            )
            refill = _measure_one_refill(
                custom, pool_name=pool_name,
                target_k=_POOL_REPLICAS, timeout_s=_REFILL_TIMEOUT_S,
            )
            refills_ms[name] = refill
            if refill is None:
                log.warning(
                    "cycle %d (%s): no drop+refill observed within %ds",
                    i, name, _REFILL_TIMEOUT_S,
                )
            else:
                log.info("cycle %d (%s): refill_ms=%.1f", i, name, refill)
            # Release the session so the next cycle starts from a clean baseline.
            _delete_claim(custom, claim_name=name)

        passed, breakdown, sla_metrics = _classify_turnover(
            refills_ms,
            cycle_count=_CYCLES,
            refill_ceiling_ms=_REFILL_CEILING_MS,
            min_completed_ratio=_MIN_COMPLETED_RATIO,
        )

        all_str = ", ".join(f"{x:.1f}" for x in breakdown["all_refills_ms"])
        med = breakdown["median_ms"]
        med_str = f"{med:.1f}" if med is not None else "n/a"
        common = (
            f"{breakdown['completed_count']}/{_CYCLES} turnover cycles refilled "
            f"(median={med_str}ms, p90={breakdown['p90_ms']}, "
            f"ceiling={_REFILL_CEILING_MS:g}ms); "
            f"completed threshold={breakdown['completed_threshold']}. "
            f"All refills (ms, sorted): [{all_str}]. "
            f"Non-completing cycles: {breakdown['timeouts']!r}."
        )
        if passed:
            return ("PASS",
                    f"Warm pool sustained turnover: {common}", sla_metrics)
        return (
            "FAIL",
            f"Warm pool under-delivered turnover refill: {common} "
            f"Pool either refilled slower than the ceiling or too few cycles "
            f"completed — controller-side warm-pool replenishment candidate.",
            sla_metrics,
        )
    finally:
        _cleanup(
            custom, claim_names=claim_names,
            pool_name=pool_name, template_name=template_name,
        )
