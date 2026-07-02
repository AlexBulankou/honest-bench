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

try:  # package context (production: run.py loads harness.scenarios.warmpool_cold_start)
    from . import runtime_class as rc
    from ._apiversion import (
        claim_gvr, ext_api_version, sandbox_gvr, template_gvr, warmpool_gvr,
    )
    from ._kube import load_cluster_config
    from .. import metrics, ttfe_probe
except ImportError:  # standalone (dependency-free test from the scenarios/ dir)
    import runtime_class as rc
    from _apiversion import (
        claim_gvr, ext_api_version, sandbox_gvr, template_gvr, warmpool_gvr,
    )
    from _kube import load_cluster_config
    import sys as _sys
    import pathlib as _pathlib

    _sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))
    import metrics, ttfe_probe

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
# the render schema's metric vocabulary. Used only on the LEGACY (TTFE-off) emit
# path; the TTFE path supersedes it with the create->first-instruction histogram.
_SLA_METRIC_KEY = "activation_ms"


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _opt_int(name: str) -> int | None:
    v = os.environ.get(name)
    return int(v) if v not in (None, "") else None


def _opt_float(name: str) -> float | None:
    v = os.environ.get(name)
    return float(v) if v not in (None, "") else None


# TTFE Layer-2 exec probe. DEFAULT-OFF.
#
# gated: default-off until the runner ServiceAccount carries pods/exec RBAC and
# the fire path flips it ON in the SAME change that grants the verb. The probe
# (ttfe_probe.probe_first_instruction) collapses an RBAC-denied exec and a
# genuine exec-failure to the same (None, False) — it cannot tell them apart —
# so an ungated default-on would publish a false 0% exec-success + empty TTFE
# histograms before the grant lands. Flip-issue: #3944.
_TTFE_EXEC = _env_flag("BENCH_TTFE_EXEC")

# Node count for the per-node throughput denominator (matches run.py provenance).
_NODE_COUNT = max(1, _opt_int("BENCH_NODE_COUNT") or 1)

# Density basis (the LOCKED 1.88/vCPU reconcile). Supplied by the fire path from
# the real saturation measurement: max concurrent sandboxes / per-node
# ALLOCATABLE sandbox-schedulable vCPU. When either is unset, no density_per_vcpu
# key is emitted and the Max-Density cell renders pending (never a fabricated
# value). warmpool_cold_start is the render-designated DENSITY_SOURCE_SCENARIO.
_DENSITY_MAX_CONCURRENT = _opt_int("BENCH_DENSITY_MAX_CONCURRENT")
_DENSITY_ALLOC_VCPU = _opt_float("BENCH_DENSITY_ALLOCATABLE_VCPU_PER_NODE")

# Per-cluster throughput node count (hb#132 dual matrix cells). OPT-IN, unset by
# default: pass ONLY on a genuine cluster-saturation fire (offered load held
# at/above the cluster's saturation point at this node count). When set, the
# metrics core emits the coupled triple thpt_under_{5s,1s}_per_cluster +
# thpt_cluster_node_count — measured from THIS fire's samples, never per-node x N
# extrapolation. On a non-saturating fire the per-cluster halves would report
# offered load, not capacity, so leaving it unset keeps the cluster cells
# honestly `pending (cluster-fire)`.
_CLUSTER_NODE_COUNT = _opt_int("BENCH_CLUSTER_NODE_COUNT")

# Timeouts. Pool warmup: 180s for 5 replicas (pull + schedule + start).
# Per-claim bind: 180s — cold-path claims can take 30-90s on a fresh node.
#
# Both are env-tunable so a large-N concurrent fire (300/500 claims) can raise
# the ceilings: warming 300 gVisor pods, or cold-provisioning 300 concurrent
# claims on a finite node pool, legitimately exceeds the 5-replica default. The
# defaults preserve the canonical small-N shape exactly.
_WARMUP_TIMEOUT_S = int(
    os.environ.get("WARMPOOL_COLD_START_WARMUP_TIMEOUT_S", "180")
)
_BIND_TIMEOUT_S = int(
    os.environ.get("WARMPOOL_COLD_START_BIND_TIMEOUT_S", "180")
)
_POLL_S = 0.05  # per-claim thread poll — must be << the warm threshold

# CR coordinates.
_TPL_GVR = template_gvr()
_CLM_GVR = claim_gvr()
_SWP_GVR = warmpool_gvr()
_SBX_GVR = sandbox_gvr()

_SCENARIO_LABEL = {"honest-bench/scenario": "warmpool-cold-start"}

# Runtime-class pin + bound-pod verification (DEFAULT-OFF; mirrors burst_create).
#
# Unset (the default) -> the template is built byte-identical to its pre-#3942 shape
# and the verification path is gated off, so a vanilla-kind run is unchanged. Set
# WARMPOOL_COLD_START_RUNTIME_CLASS=gvisor on a gke-sandbox cluster (or =kata on the
# nested-virt pool, #3989) and the warm-pool row becomes a REAL runtime-isolated
# number: the pool's Pods are pinned to that runtime (runtimeClassName + the runtime's
# toleration/nodeSelector) AND each counted sandbox's backing Pod is verified to have
# actually run under it before the row is published. The shared runtime_class helper
# (gVisor + Kata profiles) owns the pin + verify so this scenario, native_digest_cold,
# and suspend_resume all pin-and-verify identically. (burst_create pins-and-verifies
# with the same INTENT but via its own inline impl, NOT this helper: its
# WarmPool->Claim object model needs a claim-based verify over bound_claim_names,
# not the helper's direct sandbox_names. Editing runtime_class.py does NOT change
# burst_create — keep the two in step by hand.) See runtime_class.py.
_RUNTIME_CLASS = os.environ.get("WARMPOOL_COLD_START_RUNTIME_CLASS", "")

# The cluster's substrate banner (run.py provenance). A gke-sandbox banner asserts
# gVisor isolation, so the consistency guard refuses an unset/non-gVisor runtime on it
# before any cluster call — preventing a runc count from publishing under a gVisor
# label. kind/gke make no isolation claim and impose no constraint.
_CLUSTER_SUBSTRATE = os.environ.get("BENCH_CLUSTER_SUBSTRATE", "kind")


def _build_template_manifest(template_name: str) -> dict:
    """Minimal busybox SandboxTemplate.

    When WARMPOOL_COLD_START_RUNTIME_CLASS is set, the pod_spec is pinned to that
    runtime (runtimeClassName + the runtime's toleration/nodeSelector) via the shared
    runtime_class helper. Default-off: with the knob unset apply_runtime_class is a
    byte-identical no-op, so the template is exactly its pre-#3942 shape.
    """
    pod_spec = {
        "containers": [
            {
                "name": "sandbox",
                "image": _SANDBOX_IMAGE,
                "imagePullPolicy": "IfNotPresent",
                "command": ["sh", "-c", "sleep 600"],
                "resources": rc.container_resources_from_env(_RUNTIME_CLASS),
            },
        ],
        "restartPolicy": "Never",
    }
    rc.apply_runtime_class(pod_spec, _RUNTIME_CLASS)
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
                "spec": pod_spec,
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
                     bound_at: dict[str, float],
                     sandbox_names: dict[str, str],
                     ttfe_enabled: bool,
                     create_monotonic: float | None,
                     ttfe_results: dict[str, tuple]) -> None:
    """Tight-poll a single claim until Ready+bound or deadline; record time,
    then — when TTFE is enabled — run the first-instruction probe IN THIS THREAD.

    Runs in its own thread with its own CustomObjectsApi. The Api object is
    per-thread but its urllib3 pool is the default ApiClient's shared pool, so
    `run()` pins connection_pool_maxsize >= claim count before spawning us —
    otherwise threads would serialize waiting for a free connection and
    re-coarsen the sub-second granularity. On first observation of Ready+bound it
    writes `bound_at[claim_name] = time.monotonic()` and the backing pod name
    `sandbox_names[claim_name] = status.sandbox.name` (dict item-assignment is
    atomic under CPython) — the pod name is the exec target the TTFE probe needs.

    ## Why the TTFE probe runs HERE (per-claim, at this claim's own bind)

    Running the probe inside each claim's own watcher thread, the instant that
    claim binds, is load-bearing for an honest warm TTFE. A serial probe pass
    that runs only AFTER all claims have bound inflates every claim's
    create->first-exec span two ways: (a) probe-after-full-join — a fast warm
    claim is not probed until the SLOWEST cold claim binds, so its t1 inherits
    the slowest-bind as a shared floor; and (b) serial accumulation — each probe
    waits for every earlier-probed claim's exec round-trip. Both push the whole
    histogram toward the slowest-cold-bind floor, so p50/p95 bunch high (e.g.
    6.8s/7.6s) while the bind gate still passes sub-ceiling and thpt_under_5s
    reads 0. Probing per-claim at its own bind makes t1 = its-own-bind + one exec
    round-trip — the real activation latency the warm pool actually delivers.
    The probe (`ttfe_probe.probe_first_instruction`) never raises on an
    exec/cluster/RBAC error — it collapses all of them to (None, False), recorded
    here as this claim's result — so one bad claim degrades only its own cell.
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
        status = (obj or {}).get("status") or {}
        if _is_claim_ready_and_bound(status):
            pod_name = (status.get("sandbox") or {}).get("name")
            if pod_name:
                sandbox_names[claim_name] = pod_name
            # t1-anchoring bind timestamp recorded BEFORE the probe runs, so the
            # warm-tier bind-latency classification is unchanged — only the TTFE
            # histogram moves to the honest per-claim measurement.
            bound_at[claim_name] = time.monotonic()
            if ttfe_enabled and pod_name and create_monotonic is not None:
                core_v1 = k8s_client.CoreV1Api()
                ttfe_results[claim_name] = ttfe_probe.probe_first_instruction(
                    core_v1,
                    pod_name=pod_name,
                    namespace=_NAMESPACE,
                    create_monotonic=create_monotonic,
                )
            return
        time.sleep(_POLL_S)


def _measure_claim_latencies(
    claim_names: list[str], *, timeout_s: int,
    ttfe_enabled: bool = False,
    create_times: dict[str, float] | None = None,
) -> tuple[dict[str, float], set[str], dict[str, str], dict[str, tuple]]:
    """Measure each claim's Ready+bound latency with one thread per claim.

    Returns (bound_at, pending, sandbox_names, ttfe_results) where bound_at maps
    each resolved claim to its monotonic bind-observation time, pending is the set
    that never bound within `timeout_s`, sandbox_names maps each bound claim to its
    backing pod name (the exec target for the TTFE probe), and ttfe_results maps
    each probed claim to its (ttfe_ms_or_None, exec_ok) tuple.

    When `ttfe_enabled`, each watcher thread runs the first-instruction TTFE probe
    inline the instant its own claim binds (see `_watch_one_claim`), so the probe
    is CONCURRENT per claim — t1 = the claim's own bind + one exec round-trip, not
    contaminated by the slowest cold-claim bind or by serial probe accumulation.
    `create_times` supplies each claim's t0; a claim absent from it (or a missing
    pod name) is left unprobed and surfaces downstream as a failed exec.

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

    create_times = create_times or {}
    bound_at: dict[str, float] = {}
    sandbox_names: dict[str, str] = {}
    ttfe_results: dict[str, tuple] = {}
    deadline = time.monotonic() + timeout_s
    threads = [
        threading.Thread(
            target=_watch_one_claim,
            kwargs={"claim_name": name, "deadline": deadline,
                    "bound_at": bound_at, "sandbox_names": sandbox_names,
                    "ttfe_enabled": ttfe_enabled,
                    "create_monotonic": create_times.get(name),
                    "ttfe_results": ttfe_results},
            daemon=True,
        )
        for name in claim_names
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=timeout_s + 5)
    pending = {name for name in claim_names if name not in bound_at}
    return bound_at, pending, sandbox_names, ttfe_results


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
    # Sort (latency, name) pairs ONCE so the warm tier's max AND its member NAMES
    # derive from a single ordering — the single-source-of-truth the emit path
    # reuses (a re-derived second sort could silently drift and reintroduce the
    # blended-p50 mislabel). Ties break by name: deterministic, and irrelevant to
    # warm_max (a tie at the boundary is the same latency either way).
    completed_pairs = sorted(
        (v, k) for k, v in latencies.items() if v is not None
    )
    completed = [v for v, _ in completed_pairs]
    timeouts = sorted(k for k, v in latencies.items() if v is None)

    remainder = completed[pool_replicas:]
    cold_path_min = remainder[0] if remainder else None
    cold_path_max = remainder[-1] if remainder else None

    breakdown = {
        "warm_max_s": None,
        "warm_names": [],
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
    # The gate's warm tier = the pool_replicas fastest-binding claims. Publish the
    # member NAMES so the emit path scopes the TTFE histogram to EXACTLY this set
    # (never a re-derived sort). Same ordering as warm_max, so warm_max ==
    # latencies[warm_names[-1]] by construction.
    breakdown["warm_names"] = [k for _, k in completed_pairs[:pool_replicas]]

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


def _activation_window_s(
    create_times: dict[str, float], bound_at: dict[str, float],
) -> float | None:
    """Wall-clock span of the activation BURST: first create -> last bind.

    The throughput denominator (sandboxes/sec/node) is "how long did the whole
    burst take to land", NOT a probe span. The honest window is the activation
    burst: from the EARLIEST create (among claims that actually bound) to the
    LATEST bind. That matches the doc's "~50s window" semantics and is independent
    of how the probes are scheduled — it stays bind-anchored even though the TTFE
    probes now run concurrently per claim (see _watch_one_claim).

    Returns None when no claim bound (no burst happened); the caller then has no
    throughput to report. Floors at 0.001s so a degenerate single-claim
    instant-bind never divides by zero.
    """
    if not bound_at:
        return None
    earliest_create = min(create_times[name] for name in bound_at)
    latest_bind = max(bound_at.values())
    return max(latest_bind - earliest_create, 0.001)


def _assemble_ttfe_metrics(
    ttfe_ms_samples: list[float],
    exec_oks: list[bool],
    *,
    window_s: float,
    node_count: int,
    max_concurrent_sandboxes: int | None,
    allocatable_sandbox_vcpu_per_node: float | None,
    bind_ms_samples: list[float] | None = None,
    exec_ms_samples: list[float] | None = None,
    cluster_node_count: int | None = None,
) -> dict:
    """Assemble the warmpool TTFE sla_metrics dict (delegates to the pure core).

    Adds the reserved `n` key = the attempt total (len(exec_oks)) so the harness
    lifts it to the top-level schema field. n == len(exec_oks) by the locked
    contract: one exec_ok per claim FIRED (a never-bound claim contributes
    exec_ok=False), so exec_success_rate's denominator is the attempt total and
    the render derives exec_success_n = round(rate * n).

    bind_ms_samples / exec_ms_samples (the TTFE decomposition, inch #1): the
    per-claim bind latencies (create->bound, ms) and per-claim exec latencies
    (create->first-instruction minus create->bound, ms) for the SAME emit set as
    the TTFE samples, so metrics.ttfe_sla_metrics emits bind_p50_ms/bind_p95_ms +
    exec_p50_ms/exec_p95_ms alongside the TTFE percentiles. exec is measured
    per-claim (NOT p50(ttfe)-p50(bind)). Diagnostic-only — see
    metrics.ttfe_sla_metrics.
    """
    m = metrics.ttfe_sla_metrics(
        ttfe_ms_samples,
        exec_oks,
        window_s=window_s,
        node_count=node_count,
        max_concurrent_sandboxes=max_concurrent_sandboxes,
        allocatable_sandbox_vcpu_per_node=allocatable_sandbox_vcpu_per_node,
        bind_ms_samples=bind_ms_samples,
        exec_ms_samples=exec_ms_samples,
        cluster_node_count=cluster_node_count,
    )
    m["n"] = len(exec_oks)
    return m


def _assemble_probe_results(
    claim_names: list[str],
    ttfe_results: dict[str, tuple],
) -> tuple[list[float], list[bool]]:
    """Collect the per-claim concurrent-probe results into histogram inputs.

    Pure assembly — no I/O. The probes already ran CONCURRENTLY inside each
    claim's watcher thread (see `_watch_one_claim`), depositing each claim's
    (ttfe_ms_or_None, exec_ok) into `ttfe_results` at that claim's own bind moment.
    This walks the fired-claim list in order and flattens those into the two
    parallel lists the metrics core consumes.

    One exec_oks entry per claim FIRED (the locked contract: attempt total ==
    len(exec_oks) == n == len(claim_names)). A claim absent from `ttfe_results`
    never bound (or bound with no pod name / TTFE disabled) — record exec_ok=False
    with no sample (attempted-never-executed) so it drags exec_success_rate
    honestly. A present claim contributes its exec_ok, plus its TTFE sample only
    when the probe returned a latency (a failed exec contributes False to
    exec_success_rate but NO sample to the histogram — a sandbox that never ran an
    instruction has no honest first-instruction latency).
    """
    ttfe_ms_samples: list[float] = []
    exec_oks: list[bool] = []
    for name in claim_names:
        result = ttfe_results.get(name)
        if result is None:
            exec_oks.append(False)
            continue
        ttfe_ms_sample, exec_ok = result
        exec_oks.append(exec_ok)
        if ttfe_ms_sample is not None:
            ttfe_ms_samples.append(ttfe_ms_sample)
    return ttfe_ms_samples, exec_oks


def _under_delivery_outcome(
    breakdown: dict,
    *,
    pool_replicas: int,
    claim_count: int,
    all_lat_str: str,
) -> tuple[str, str, dict] | None:
    """Honest FAIL row for warm-pool under-delivery on the TTFE-on path (#4093).

    When the pool under-delivers (`len(completed) < pool_replicas`),
    `_classify_latencies` returns early with `warm_max_s=None` and
    `warm_names=[]`. The TTFE-on emit block's single-source assert
    (`len(emit_names) == pool_replicas`) would then raise AssertionError on the
    empty warm set — surfacing as an OPAQUE crash-caught 'fail' cell rather than
    an explicit FAIL row naming the shortfall. This pure helper returns that
    honest FAIL triple (empty sla_metrics — under-delivery has no isolated
    warm-tier measurement, so the report skip-not-breaches on the absent key)
    so `run()` can return it BEFORE reaching the assert. With the early return
    in place the assert is reachable only when `warm_max_s is not None` (warm
    set is full-length by construction), so it purely guards genuine warm-set
    drift — its true invariant.

    Returns None (no under-delivery outcome to emit) for the cold-baseline mode
    (`pool_replicas <= 0`, no warm tier to under-deliver) or when a full warm
    cluster delivered (`warm_max_s is not None`) — leaving both the cold-baseline
    path and the normal PASS/FAIL path untouched.
    """
    if pool_replicas <= 0 or breakdown["warm_max_s"] is not None:
        return None
    completed_n = breakdown["completed_count"]
    return (
        "FAIL",
        f"WarmPool under-delivered warm slots: only {completed_n}/{pool_replicas} "
        f"claims bound into the warm tier (claims fired={claim_count}). No full "
        f"warm cluster to measure TTFE against — controller-side warm-pool "
        f"candidate. All latencies (s, sorted): [{all_lat_str}]. "
        f"Timeouts: {breakdown['timeouts']!r}.",
        {},
    )


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

    # Sub-gap 1 (pure, fail-fast): a gke-sandbox-labeled result MUST pin the gVisor
    # RuntimeClass, else the published warm-pool row is a runc number under a gVisor
    # banner. Checked before the cluster is touched so the mistake crashes immediately.
    rc.assert_substrate_runtime_consistency(_CLUSTER_SUBSTRATE, _RUNTIME_CLASS)

    # Portable kubeconfig load (see _kube.load_cluster_config): an explicit
    # KUBECONFIG wins, else in-cluster when running as a pod, else the default
    # kubeconfig. The explicit-KUBECONFIG precedence is what lets a pod on one
    # cluster fire the suite against another.
    load_cluster_config()

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
    core_v1 = k8s_client.CoreV1Api()

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

        bound_at, timed_out, sandbox_names, ttfe_results = (
            _measure_claim_latencies(
                claim_names, timeout_s=_BIND_TIMEOUT_S,
                ttfe_enabled=_TTFE_EXEC, create_times=create_times,
            )
        )

        latencies: dict[str, float | None] = {}
        for name in claim_names:
            if name in bound_at:
                latencies[name] = bound_at[name] - create_times[name]
            else:
                latencies[name] = None
                log.warning("claim %s timed out before Ready+bound", name)

        # Sub-gap 2 (live read-back, any isolation substrate): the counted sandboxes are
        # the bound claims; on a substrate that makes a verifiable runtime claim
        # (gke-sandbox -> gvisor, gke-kata -> kata) verify each one's backing Pod
        # actually scheduled under the pinned runtime before publishing the
        # runtime-labeled row. Crash-FAILs on a silent runc fallback. kind/gke skip this
        # (required_runtime_for_substrate -> None, no runtime claim to verify, so the
        # path stays read-free there). Runs post-measurement so it never perturbs the
        # measured bind latency. The gate condition shares the single substrate->runtime
        # source of truth with the sub-gap-1 consistency guard above, which already
        # proved _RUNTIME_CLASS == the required runtime for any ruled substrate.
        if rc.required_runtime_for_substrate(_CLUSTER_SUBSTRATE) is not None:
            bound_sandbox_names = [
                sandbox_names[n] for n in claim_names if n in sandbox_names
            ]
            verified = rc.verify_bound_pod_runtimes(
                custom, core_v1,
                namespace=_NAMESPACE,
                sandbox_names=bound_sandbox_names,
                sandbox_gvr=_SBX_GVR,
                expected_runtime_class=_RUNTIME_CLASS,
            )
            log.info(
                "runtime read-back: %d/%d bound sandboxes verified under "
                "RuntimeClass %r", verified, len(bound_sandbox_names), _RUNTIME_CLASS,
            )

        passed, breakdown = _classify_latencies(
            latencies, pool_replicas=_POOL_REPLICAS,
            abs_ceiling_s=_ABS_FAST_CEILING_S,
            separation_ratio=_SEPARATION_RATIO,
        )

        all_lat_str = ", ".join(f"{x:.3f}" for x in breakdown["all_latencies_s"])
        warm_max = breakdown["warm_max_s"]
        warm_max_str = f"{warm_max:.3f}s" if warm_max is not None else "<n/a>"
        # Under-delivery honest-FAIL short-circuit (#4093). When the warm pool
        # under-delivered (warm_max is None), return an explicit FAIL row naming
        # the shortfall BEFORE the TTFE-on emit block below — whose single-source
        # assert would otherwise raise on the empty warm set and surface as an
        # opaque crash-caught 'fail' cell. No-op for the cold-baseline mode
        # (_POOL_REPLICAS <= 0) and for a delivered warm cluster (warm_max set),
        # so both those paths fall through unchanged.
        under = _under_delivery_outcome(
            breakdown,
            pool_replicas=_POOL_REPLICAS,
            claim_count=_CLAIM_COUNT,
            all_lat_str=all_lat_str,
        )
        if under is not None:
            return under
        # Emit-key assembly. Two paths, gated by BENCH_TTFE_EXEC:
        #
        #   TTFE-on  (the doc-headline path) — probe each bound claim's first
        #     instruction and emit the create->first-instruction histogram:
        #     ttfe_p50_ms/ttfe_p95_ms, thpt_under_5s/1s_per_node,
        #     exec_success_rate, density_per_vcpu (when both env inputs supplied),
        #     n = attempt total. This SUPERSEDES activation_ms (the doc reports
        #     TTFE, not bind latency), so the legacy key is dropped on this path.
        #   TTFE-off (legacy) — emit the isolated warm-tier bind latency as
        #     activation_ms (ms), only when a full warm cluster delivered a real
        #     warm_max; under-delivery (None) emits no key. The reserved "n" here
        #     is the warm-tier size (_POOL_REPLICAS) backing warm_max.
        #
        # The harness lifts "n" to the top-level schema field so it renders as
        # "(n=N)"; it is never coerced into a metric.
        if _TTFE_EXEC and bound_at:
            # Probes already ran CONCURRENTLY per claim, each at its own bind
            # moment inside its watcher thread (the honest-TTFE fix — see
            # _watch_one_claim). Here we only flatten the collected results.
            #
            # WARM-TIER SCOPE (the row-labels-what-it-measures fix): the scenario
            # deliberately OVERFLOWS (claim_count > pool_replicas) so the gate can
            # prove a distinct fast tier — but the emitted p50/p95 must describe
            # the WARM-POOL HIT, not the warm+cold blend. Scope the whole row
            # (TTFE, n, throughput window, density) to the gate's warm set —
            # option b, one uniform N, keeps matched-N (#1038). POOL_REPLICAS==0
            # is the cold-baseline mode (no warm tier): report over all claims.
            if _POOL_REPLICAS > 0:
                emit_names = breakdown["warm_names"]
                # SINGLE-SOURCE ASSERT: the emitted-warm set is EXACTLY the
                # gate-warm set — same size, every member within warm_max.
                assert len(emit_names) == _POOL_REPLICAS and all(
                    latencies[n] is not None
                    and latencies[n] <= breakdown["warm_max_s"]
                    for n in emit_names
                ), "emit warm set drifted from gate warm set"
                emit_bound_at = {
                    n: bound_at[n] for n in emit_names if n in bound_at
                }
            else:
                emit_names = claim_names
                emit_bound_at = bound_at
            ttfe_ms_samples, exec_oks = _assemble_probe_results(
                emit_names, ttfe_results,
            )
            window_s = _activation_window_s(create_times, emit_bound_at)
            # TTFE decomposition (inch #1): the per-claim BIND latency
            # (create->bound, i.e. provisioning) for the SAME emit set, in ms.
            # latencies[name] is create->bound in SECONDS; scope to emit_names
            # (the gate-warm set when POOL_REPLICAS>0, else all claims) so the
            # bind percentiles describe the exact same population as the TTFE
            # percentiles. A never-bound claim (latencies[name] is None) has no
            # bind sample — dropped, same as its absent TTFE sample.
            bind_ms_samples = [
                latencies[name] * 1000.0
                for name in emit_names
                if latencies.get(name) is not None
            ]
            # EXEC decomposition (inch #1): the per-claim exec latency
            # (websocket setup + first-instruction round-trip) as a GENUINELY
            # MEASURED sample, paired per-claim so the exec percentile is real —
            # NOT p50(ttfe) - p50(bind) (percentiles don't subtract linearly).
            # For each claim we have both create->bound (latencies[name], s) and
            # create->first-instruction (ttfe_results[name][0], ms), sharing the
            # same create() t0; their difference is that claim's exec time. Only
            # claims with BOTH a bind and a non-None TTFE sample contribute (a
            # claim that never bound or never executed has no honest exec split).
            exec_ms_samples = []
            for name in emit_names:
                bind_s = latencies.get(name)
                probe = ttfe_results.get(name)
                if bind_s is None or probe is None:
                    continue
                ttfe_ms_for_claim = probe[0]
                if ttfe_ms_for_claim is None:
                    continue
                exec_ms_samples.append(ttfe_ms_for_claim - bind_s * 1000.0)
            sla_metrics = _assemble_ttfe_metrics(
                ttfe_ms_samples,
                exec_oks,
                window_s=window_s,
                node_count=_NODE_COUNT,
                max_concurrent_sandboxes=_DENSITY_MAX_CONCURRENT,
                allocatable_sandbox_vcpu_per_node=_DENSITY_ALLOC_VCPU,
                bind_ms_samples=bind_ms_samples,
                exec_ms_samples=exec_ms_samples,
                cluster_node_count=_CLUSTER_NODE_COUNT,
            )
        else:
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
        # Cold-baseline mode (POOL_REPLICAS=0): no warm pool exists, so the
        # warm-tier separation gate does not apply — every claim cold-provisions
        # (overflow-claim cold-start is the same path the 5-warm/5-cold default
        # exercises for its cold tier). The run's purpose here is to RECORD the
        # all-cold TTFE distribution, not to assert a warm fast tier, so report a
        # neutral outcome carrying the same sla_metrics (already assembled over
        # every bound claim) rather than a misleading warm-under-delivery FAIL.
        if _POOL_REPLICAS == 0:
            bound_n = len(bound_at)
            return (
                "PASS",
                f"Cold baseline (no warm pool): {bound_n}/{_CLAIM_COUNT} claims "
                f"cold-provisioned and bound. No warm-tier gate applies — TTFE "
                f"distribution recorded over all bound claims. "
                f"All latencies (s, sorted): [{all_lat_str}]. "
                f"Timeouts: {breakdown['timeouts']!r}.",
                sla_metrics,
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
