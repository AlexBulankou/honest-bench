"""Warm-pool cold-start benchmark: sub-second provisioning from a warm pool.

    Setup    : SandboxWarmPool replicas: 5 (controller has had >=60s post-
               create to warm).
    Action   : Issue 10 SandboxClaim requests in rapid succession against
               the template. Measure wall-clock from claim-create to
               Ready+bound for each.
    Expected : The 5 warm-pool-served claims form a distinct fast tier; the
               next 5 (cold) are reported with their own latency (no SLO claim
               on the cold path, but the number is recorded).
    Why      : Sub-second provisioning is the latency-critical AI-agent enabler
               warm pools promise. This scenario quantifies it on a vanilla
               cluster and creates a longitudinal record across controller
               version updates.

## Test shape

1. Provision a SandboxTemplate (minimal busybox — controller-level provisioning
   latency is what the scenario measures, not the user's image-build time).
2. Create SandboxWarmPool replicas=5; wait until status.readyReplicas=5.
3. Fire 10 SandboxClaim creates as fast as a serial loop allows. Record `t0_i`
   for each immediately after create() returns.
4. Poll each claim until Ready+bound; record `t1_i` on first observation.
5. Latency_i = t1_i - t0_i (seconds). Report all 10 latencies.
6. PASS = the 5 fastest claims (the warm tier) form a distinct fast cluster, by
   EITHER measure: warm_max < ABS_FAST_CEILING_S (default 2.5s) OR the gap to the
   next-fastest claim clears SEPARATION_RATIO (default 1.8x). This is a
   separation gate, not an absolute threshold — robust to warm-tier latency
   drift under cluster load while still catching genuine warm-pool
   under-delivery (fewer than 5 warm slots -> the 5th-fastest is itself cold ->
   ratio ~= 1.0 -> FAIL).
7. Cleanup: 10 claims, pool, template.

## Why "at least 5 fast" and not "first 5 fast"

The "first 5 (warm-pool-served)" framing assumes the controller serves claims in
arrival order. In practice, apiserver admission + controller reconciliation are
not strictly FIFO across 10 concurrent claims — pool slots are assigned by the
reconciler's next sweep, which may pick from informer-cache order. The PASS gate
is order-independent: if the pool served 5 slots fast (whichever 5), the
sub-second-provisioning promise held; the remaining 5 are the cold-path baseline.

## Why "Ready+bound" approximates "first-byte-stdout"

Literal first-byte-stdout requires an in-pod exec per claim (10 parallel
pod/exec channels) — ~10x the RBAC surface, and the websocket-setup latency
masks the pool-vs-cold delta. The claim's Ready=True condition fires only after
the pod is Ready, so Ready+bound is a tight upper bound on first-byte time (at
most a few hundred ms looser). A future iteration wanting literal stdout-time can
add a pods/exec grant and a per-claim exec round-trip after Ready+bound.

## Crash posture

Infrastructure failures (controller unhealthy, CRDs missing, RBAC denied) raise.
Scenario-outcome FAILs return ("FAIL", "<excerpt>", sla_metrics). The harness
loop catches a raised exception as a crash-fail cell.
"""

from __future__ import annotations

from ._apiversion import claim_gvr, ext_api_version, template_gvr, warmpool_gvr

import logging
import os
import time
import uuid

log = logging.getLogger("sandbox-scenario.warmpool-cold-start")


_NAMESPACE = os.environ.get("BENCH_NAMESPACE", "default")
_SANDBOX_IMAGE = os.environ.get(
    "WARMPOOL_COLD_START_SANDBOX_IMAGE", "busybox:1.36"
)

# Pool size + claim count (5 warm + 5 cold). Both env-tunable for longitudinal
# cycles that want different shapes; defaults match the canonical spec.
_POOL_REPLICAS = int(os.environ.get("WARMPOOL_COLD_START_POOL_REPLICAS", "5"))
_CLAIM_COUNT = int(os.environ.get("WARMPOOL_COLD_START_CLAIM_COUNT", "10"))

# Warm-tier gate (separation-based). The scenario verifies the warm pool yields
# a DISTINCT FAST provisioning tier — not an absolute latency. PASS iff the
# _POOL_REPLICAS fastest claims form a warm cluster that is EITHER absolutely
# fast (warm_max < _ABS_FAST_CEILING_S) OR clearly separated from the
# next-fastest claim (next/warm_max >= _SEPARATION_RATIO).
#
# Rationale: warm-tier end-to-end latency (scheduler + kubelet + container start)
# drifts up under cluster load, so a zero-margin absolute gate false-FAILs when
# genuinely-warm binds drift past the line while staying far below the cold tier.
# The separation gate is robust to absolute drift in either tier yet still
# catches genuine under-delivery (if the pool serves fewer than _POOL_REPLICAS
# warm slots, the _POOL_REPLICAS-th fastest claim falls into the cold cluster ->
# ratio ~= 1.0 -> FAIL). The absolute clause keeps the gate honest if a future
# instant-on/snapshot path makes the cold tier fast too (separation collapses but
# provisioning is genuinely fast). Both bounds env-tunable for recalibration.
_ABS_FAST_CEILING_S = float(
    os.environ.get("WARMPOOL_COLD_START_ABS_CEILING_S", "2.5")
)
_SEPARATION_RATIO = float(
    os.environ.get("WARMPOOL_COLD_START_SEPARATION_RATIO", "1.8")
)

# Public benchmark metric key (milliseconds). The warm-tier bind latency this
# scenario measures internally as `warm_max_s` (seconds) is emitted via the
# run() 3-tuple as the activation latency, converted to milliseconds to match
# the render schema's metric vocabulary.
_SLA_METRIC_KEY = "activation_ms"

# Timeouts. Pool warmup: 180s for 5 replicas (pull + schedule + start).
# Per-claim bind: 180s — cold-path claims can take 30-90s on a fresh node.
_WARMUP_TIMEOUT_S = 180
_BIND_TIMEOUT_S = 180
_POLL_S = 0.05  # per-claim thread poll — must be << the warm threshold

# CR coordinates.
_TPL_GVR = template_gvr()
_CLM_GVR = claim_gvr()
_SWP_GVR = warmpool_gvr()

_SCENARIO_LABEL = {"honest-bench/scenario": "warmpool-cold-start"}


def _build_template_manifest(template_name: str) -> dict:
    """Minimal busybox SandboxTemplate."""
    return {
        "apiVersion": ext_api_version(),
        "kind": "SandboxTemplate",
        "metadata": {
            "name": template_name,
            "namespace": _NAMESPACE,
            "labels": dict(_SCENARIO_LABEL),
        },
        "spec": {
            "podTemplate": {
                "spec": {
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
                },
            },
        },
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


def _is_claim_ready_and_bound(status: dict) -> bool:
    """Ready=True AND sandbox.name set."""
    if not status:
        return False
    conds = status.get("conditions") or []
    has_ready = any(
        c.get("type") == "Ready" and c.get("status") == "True"
        for c in conds
    )
    has_bound = bool((status.get("sandbox") or {}).get("name"))
    return has_ready and has_bound


def _wait_for_pool_warm(
    custom, *, pool_name: str, target_ready: int, timeout_s: int,
) -> dict:
    """Poll WarmPool until status.readyReplicas >= target_ready, or raise."""
    group, version, plural = _SWP_GVR
    deadline = time.monotonic() + timeout_s
    last_status: object = "<no-status>"
    while time.monotonic() < deadline:
        obj = custom.get_namespaced_custom_object(
            group=group, version=version, namespace=_NAMESPACE,
            plural=plural, name=pool_name,
        )
        status = (obj or {}).get("status") or {}
        last_status = status
        ready = int(status.get("readyReplicas") or 0)
        if ready >= target_ready:
            return obj
        time.sleep(1.0)
    raise RuntimeError(
        f"SandboxWarmPool {pool_name} did not reach readyReplicas>={target_ready} "
        f"within {timeout_s}s (last status={last_status!r})"
    )


def _watch_one_claim(*, claim_name: str, deadline: float,
                     bound_at: dict[str, float]) -> None:
    """Tight-poll a single claim until Ready+bound or deadline; record time.

    Runs in its own thread with its own CustomObjectsApi. The Api object is
    per-thread but its urllib3 pool is the default ApiClient's shared pool, so
    `run()` pins connection_pool_maxsize >= claim count before spawning us —
    otherwise threads would serialize waiting for a free connection and
    re-coarsen the sub-second granularity. On first observation of Ready+bound it
    writes `bound_at[claim_name] = time.monotonic()` (dict item-assignment is
    atomic under CPython) and returns.
    """
    from kubernetes import client as k8s_client

    group, version, plural = _CLM_GVR
    custom = k8s_client.CustomObjectsApi()
    while time.monotonic() < deadline:
        try:
            obj = custom.get_namespaced_custom_object(
                group=group, version=version, namespace=_NAMESPACE,
                plural=plural, name=claim_name,
            )
        except Exception as e:  # noqa: BLE001 — best-effort, retry
            log.warning("poll: get claim %s failed: %s — retrying", claim_name, e)
            time.sleep(_POLL_S)
            continue
        if _is_claim_ready_and_bound((obj or {}).get("status") or {}):
            bound_at[claim_name] = time.monotonic()
            return
        time.sleep(_POLL_S)


def _measure_claim_latencies(
    claim_names: list[str], *, timeout_s: int,
) -> tuple[dict[str, float], set[str]]:
    """Measure each claim's Ready+bound latency with one thread per claim.

    Returns (bound_at, pending) where bound_at maps each resolved claim to its
    monotonic bind-observation time and pending is the set that never bound
    within `timeout_s`.

    Thread-per-claim (not single-threaded round-robin) is required because the
    scenario gates on a sub-second threshold: a round-robin sweep of N claims
    with a poll-sleep has worst-case observation granularity ~= N x per-GET +
    poll-interval ~= ~1s, the same order as the threshold — so a genuinely-warm
    0.3s bind gets measured >1.0s and miscounted as cold. Independent per-claim
    threads decouple the claims and give ~_POLL_S (50ms) granularity. 10 I/O-bound
    GET loops are trivial apiserver load and do not bottleneck each other (the GIL
    is released during the network round-trip).

    Latency captured = wall-clock seconds from `create_time` (recorded by the
    caller immediately after create() returns) to first observation of
    Ready+bound.
    """
    import threading

    bound_at: dict[str, float] = {}
    deadline = time.monotonic() + timeout_s
    threads = [
        threading.Thread(
            target=_watch_one_claim,
            kwargs={"claim_name": name, "deadline": deadline,
                    "bound_at": bound_at},
            daemon=True,
        )
        for name in claim_names
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=timeout_s + 5)
    pending = {name for name in claim_names if name not in bound_at}
    return bound_at, pending


def _classify_latencies(
    latencies: dict[str, float | None],
    *,
    pool_replicas: int,
    abs_ceiling_s: float,
    separation_ratio: float,
) -> tuple[bool, dict]:
    """Separation-based warm-tier gate.

    The warm cluster = the `pool_replicas` fastest completed claims. PASS iff
    that cluster is fast by EITHER measure:
      - absolute   : warm_max < abs_ceiling_s, OR
      - separation : next_fastest / warm_max >= separation_ratio
    Robust to absolute warm-tier drift while still catching genuine
    under-delivery: if the pool serves fewer than `pool_replicas` warm slots, the
    `pool_replicas`-th fastest claim is itself a cold-path bind, so warm_max ~=
    next_fastest -> ratio ~= 1.0 -> FAIL.

    Returns (passed, breakdown) carrying the summary stats for the excerpt and
    longitudinal record.
    """
    completed = sorted(v for v in latencies.values() if v is not None)
    timeouts = sorted(k for k, v in latencies.items() if v is None)

    remainder = completed[pool_replicas:]
    cold_path_min = remainder[0] if remainder else None
    cold_path_max = remainder[-1] if remainder else None

    breakdown = {
        "warm_max_s": None,
        "next_fastest_s": cold_path_min,
        "separation_observed": None,
        "cold_path_min_s": cold_path_min,
        "cold_path_max_s": cold_path_max,
        "absolute_ok": False,
        "separation_ok": False,
        "timeouts": timeouts,
        "completed_count": len(completed),
        "all_latencies_s": completed,
    }

    # Need a full warm cluster to even evaluate the gate; fewer completed claims
    # than the pool size means the pool under-delivered (or claims timed out).
    if len(completed) < pool_replicas:
        return False, breakdown

    warm_max = completed[pool_replicas - 1]
    breakdown["warm_max_s"] = warm_max

    absolute_ok = warm_max < abs_ceiling_s
    if remainder and warm_max > 0:
        separation_observed = remainder[0] / warm_max
        separation_ok = separation_observed >= separation_ratio
    else:
        # No cold tier to separate from (claim_count == pool_replicas):
        # only the absolute clause applies.
        separation_observed = None
        separation_ok = False

    breakdown["separation_observed"] = separation_observed
    breakdown["absolute_ok"] = absolute_ok
    breakdown["separation_ok"] = separation_ok

    return (absolute_ok or separation_ok), breakdown


def _cleanup(
    custom, *, claim_names: list[str], pool_name: str, template_name: str,
) -> None:
    """Best-effort delete: all claims, then pool, then template."""
    from kubernetes.client.exceptions import ApiException
    for name in claim_names:
        try:
            custom.delete_namespaced_custom_object(
                group=_CLM_GVR[0], version=_CLM_GVR[1], namespace=_NAMESPACE,
                plural=_CLM_GVR[2], name=name,
            )
        except ApiException as e:
            if e.status != 404:
                log.warning("cleanup: delete claim %s failed: %s", name, e)
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
    """Provision pool, fire N claims, measure latencies, classify PASS/FAIL.

    Returns a 3-tuple (outcome, excerpt, sla_metrics). `sla_metrics` carries the
    isolated warm-tier bind latency as `activation_ms` (milliseconds) when a full
    warm cluster delivered, else {} (under-delivery FAIL has no isolated
    measurement — the report skip-not-breaches on the absent key). The value is
    warm_max converted from seconds to milliseconds, emitted regardless of
    PASS/FAIL so a warm_max that clears the scenario's own separation gate but
    breaches a stricter activation target still surfaces.
    """
    from kubernetes import client as k8s_client
    from kubernetes import config as k8s_config

    # Portable kubeconfig load: in-cluster when running as a pod, otherwise
    # whatever the runner's KUBECONFIG / default kubeconfig points at.
    try:
        k8s_config.load_incluster_config()
    except k8s_config.ConfigException:
        k8s_config.load_kube_config()

    # Per-claim watcher threads each build their own CustomObjectsApi(), sharing
    # the default ApiClient's urllib3 pool (default maxsize cpu_count()*5, which
    # can drop below _CLAIM_COUNT in a CPU-limited pod — threads would then
    # serialize on a free connection, re-coarsening granularity). Pin it >= claim
    # count (+4 headroom for the main thread's own GETs).
    _cfg = k8s_client.Configuration.get_default_copy()
    _cfg.connection_pool_maxsize = max(
        _cfg.connection_pool_maxsize or 0, _CLAIM_COUNT + 4
    )
    k8s_client.Configuration.set_default(_cfg)

    custom = k8s_client.CustomObjectsApi()

    suffix = uuid.uuid4().hex[:8]
    template_name = f"tmpl-{suffix}"
    pool_name = f"pool-{suffix}"
    claim_names = [f"claim{i:02d}-{suffix}" for i in range(_CLAIM_COUNT)]

    log.info(
        "creating Template %s + WarmPool %s (replicas=%d); will fire %d claims",
        template_name, pool_name, _POOL_REPLICAS, _CLAIM_COUNT,
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
        _wait_for_pool_warm(
            custom, pool_name=pool_name,
            target_ready=_POOL_REPLICAS, timeout_s=_WARMUP_TIMEOUT_S,
        )
        log.info(
            "pool fully warm (readyReplicas=%d); firing %d claims",
            _POOL_REPLICAS, _CLAIM_COUNT,
        )

        # Fire all claims as fast as a serial loop allows. Record t0 IMMEDIATELY
        # after each create() returns — the baseline is user-perceived
        # create-call-return, not loop-start.
        create_times: dict[str, float] = {}
        for name in claim_names:
            custom.create_namespaced_custom_object(
                group=_CLM_GVR[0], version=_CLM_GVR[1], namespace=_NAMESPACE,
                plural=_CLM_GVR[2],
                body=_build_claim_manifest(name, pool_name),
            )
            create_times[name] = time.monotonic()
        log.info(
            "fired %d claims in %.3fs; now polling for Ready+bound",
            _CLAIM_COUNT,
            create_times[claim_names[-1]] - create_times[claim_names[0]],
        )

        bound_at, timed_out = _measure_claim_latencies(
            claim_names, timeout_s=_BIND_TIMEOUT_S,
        )

        latencies: dict[str, float | None] = {}
        for name in claim_names:
            if name in bound_at:
                latencies[name] = bound_at[name] - create_times[name]
            else:
                latencies[name] = None
                log.warning("claim %s timed out before Ready+bound", name)

        passed, breakdown = _classify_latencies(
            latencies, pool_replicas=_POOL_REPLICAS,
            abs_ceiling_s=_ABS_FAST_CEILING_S,
            separation_ratio=_SEPARATION_RATIO,
        )

        all_lat_str = ", ".join(f"{x:.3f}" for x in breakdown["all_latencies_s"])
        warm_max = breakdown["warm_max_s"]
        warm_max_str = f"{warm_max:.3f}s" if warm_max is not None else "<n/a>"
        # Isolated activation latency in milliseconds: only when a full warm
        # cluster delivered a real warm_max; under-delivery (None) emits no key.
        # The reserved "n" key reports the sample count backing the measurement
        # (the warm-tier size, _POOL_REPLICAS — warm_max is the max over exactly
        # those fastest claims). The harness lifts "n" to the top-level schema
        # field so it renders as "(n=N)"; it is never coerced into a metric.
        sla_metrics = (
            {_SLA_METRIC_KEY: warm_max * 1000.0, "n": _POOL_REPLICAS}
            if warm_max is not None
            else {}
        )
        sep = breakdown["separation_observed"]
        sep_str = f"{sep:.2f}x" if sep is not None else "<no-cold-tier>"
        clause = (
            "absolute" if breakdown["absolute_ok"]
            else "separation" if breakdown["separation_ok"]
            else "none"
        )
        if passed:
            return (
                "PASS",
                f"WarmPool provides a distinct fast tier ({clause} clause): "
                f"warm cluster (fastest {_POOL_REPLICAS}/{_CLAIM_COUNT}) "
                f"max={warm_max_str}, ceiling={_ABS_FAST_CEILING_S}s; "
                f"separation={sep_str} (>= {_SEPARATION_RATIO}x), "
                f"cold-path min={breakdown['cold_path_min_s']!r} "
                f"max={breakdown['cold_path_max_s']!r}. "
                f"Pool: replicas={_POOL_REPLICAS}, claims fired={_CLAIM_COUNT}. "
                f"All latencies (s, sorted): [{all_lat_str}]. "
                f"Timeouts: {breakdown['timeouts']!r}.",
                sla_metrics,
            )
        return (
            "FAIL",
            f"WarmPool fast tier not distinct: warm cluster (fastest "
            f"{_POOL_REPLICAS}/{_CLAIM_COUNT}) max={warm_max_str} is neither "
            f"< {_ABS_FAST_CEILING_S}s nor separated >= {_SEPARATION_RATIO}x "
            f"from the next claim (separation={sep_str}). "
            f"Pool: replicas={_POOL_REPLICAS}, claims fired={_CLAIM_COUNT}, "
            f"completed={breakdown['completed_count']}. "
            f"All latencies (s, sorted): [{all_lat_str}]. "
            f"Timeouts: {breakdown['timeouts']!r}. "
            f"Pool under-delivered warm slots — controller-side warm-pool "
            f"candidate.",
            sla_metrics,
        )
    finally:
        _cleanup(
            custom, claim_names=claim_names,
            pool_name=pool_name, template_name=template_name,
        )
