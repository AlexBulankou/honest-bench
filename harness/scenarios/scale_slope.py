"""Scale Proof (Linearity Check): does per-node sandbox density/throughput hold flat?

This producer answers the doc's "Scale Proof (Linearity Check)" question — when you
add nodes, do you keep the SAME per-node sandbox density and the SAME per-node
sub-1s throughput, or does coordination overhead erode them? A linear substrate
retains both: 1.88 sandboxes/vCPU on 1 node should still be ~1.88/vCPU on 4 nodes,
and 4 sandboxes/s/node on 1 node should still be ~4/s/node on 4 nodes.

    Setup    : a node-count sweep {1, 2, 4} (env-tunable). For each K-node point,
               size the warm pool to fill the cluster and fire a claim burst —
               exactly the burst_create measurement, repeated per scale point.
    Per point: density   = density_per_vcpu(max_concurrent, allocatable_vcpu/node)
               throughput= throughput_per_node(ttfi_samples, <1s, window, K)
    Headline : density_retention = retention(density@base, density@max_scale)
               thpt_retention    = retention(thpt@base,    thpt@max_scale)
               1.0 = perfectly flat (the doc's "Holds Flat? Yes").
    Why      : a single cold-start number says nothing about whether the substrate
               SCALES. The retention ratios are the portable linearity proof: a
               stranger reruns the sweep on their own multi-node cluster and gets
               the same shape.

## Emit contract — TOP-LEVEL scale_proof object (NOT per-scenario sla_metrics)

The render side (render/schema.py SCALE_PROOF_FIELDS + render/render.py
render_scale_proof) reads a TOP-LEVEL `scale_proof` object:

    {"scale_points": [{"node_count": K, "density": D}, ...],
     "density_retention": float, "thpt_retention": float}

A list value cannot ride sla_metrics (the emitter's _coerce_sla_metrics drops every
list), so this producer returns the scale_proof object directly and the harness
passes it to results_schema.build_results(scale_proof=...), which emits it at the
top level via _coerce_scale_proof.

## emit-only-when-complete

A partial sweep emits {} — never a half-series. The slope needs >= 2 distinct
node-count points (a single point has no slope), and the base point must have
delivered a real density (> 0), else the retention ratio is undefined. This mirrors
burst_create's count==0 → {} and session_turnover's zero-refill → {}: no fabricated
linearity number. thpt_retention is omitted (not zero) when the base point's
throughput was 0, so the throughput column renders pending rather than a divide
that lies.

## Pure vs live

`_classify_scale_slope` is pure (no cluster, no clock) and delegates every number
to the LOCKED metrics.py functions (density_per_vcpu / throughput_per_node /
retention) — it is the load-bearing honest-by-construction logic and is unit-tested
offline. `run_sweep` is the live multi-node producer (lazy kubernetes import) that
gathers the per-point raw measurements and feeds them to the classifier.
"""

from __future__ import annotations

try:  # package context (production: harness.scenarios.scale_slope)
    from ._apiversion import (
        claim_gvr, ext_api_version, template_gvr, warmpool_gvr,
    )
    from .. import metrics
except ImportError:  # standalone (dependency-free test from the scenarios/ dir)
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from scenarios._apiversion import (  # type: ignore
        claim_gvr, ext_api_version, template_gvr, warmpool_gvr,
    )
    import metrics  # type: ignore

import logging
import math
import os
import time
import uuid

log = logging.getLogger("sandbox-scenario.scale-slope")


_NAMESPACE = os.environ.get("BENCH_NAMESPACE", "default")
_SANDBOX_IMAGE = os.environ.get("SCALE_SLOPE_SANDBOX_IMAGE", "busybox:1.36")
_RUNTIME_CLASS = os.environ.get("SCALE_SLOPE_RUNTIME_CLASS", "")

# gVisor-capable node selector ("key=value"; a bare "key" matches on presence). When
# the sweep pins runtimeClassName=gvisor (_RUNTIME_CLASS set) the sandboxes ONLY
# schedule on nodes that advertise the gVisor runtime — a system-pool node cannot
# host one. Both the skip-guard and the per-tier autoscale-wait count ONLY these
# nodes, so a cluster with 1 gVisor node + 2 system nodes is treated as 1 capable
# node (not 3), preventing K×slots gVisor pods from piling onto too few nodes and
# fabricating the density denominator (per-node-vCPU × K). [#3949]
_GVISOR_NODE_LABEL = os.environ.get(
    "SCALE_SLOPE_GVISOR_NODE_LABEL", "sandbox.gke.io/runtime=gvisor"
)

# Autoscale ceiling for gVisor-capable nodes — the MAX the cluster can scale to
# (the skip-guard upper bound). 0 = no autoscale assumed; the skip-guard then falls
# back to the count of gVisor-capable nodes Ready right now. Set this to the gVisor
# node-pool's max size so a tier ABOVE the current Ready count is ATTEMPTED (the
# autoscaler brings nodes up once the tier's warm pool goes pending) instead of
# skipped on the pre-scale count — the exact false-skip that counting total nodes
# caused (k=4 skipped because total=3<4 before any pod was pending). [#3949]
_MAX_GVISOR_NODES = int(os.environ.get("SCALE_SLOPE_MAX_GVISOR_NODES", "0") or "0")

# Per-tier autoscale-wait: how long to wait for the cluster to reach K gVisor-capable
# Ready nodes after the tier's warm pool goes pending, before DROPPING the tier. A
# tier that cannot reach K capable nodes would pile K×slots gVisor pods onto too few
# nodes and fabricate the per-node density denominator (per-node-vCPU × K), so a tier
# whose nodes never arrive is dropped (no point) rather than measured dishonestly. [#3949]
_NODE_SCALE_TIMEOUT_S = int(os.environ.get("SCALE_SLOPE_NODE_SCALE_TIMEOUT_S", "300"))

# The node-count sweep. {1, 2, 4} by default — base=1, max-scale=4. Comma-separated
# env override (e.g. "1,3,6") for a differently-sized cluster. The sweep is honest
# only for node-counts the cluster can actually provide; run_sweep measures whatever
# it can fill and the classifier needs >= 2 achieved points to emit a slope.
_NODE_COUNTS = tuple(
    int(x) for x in os.environ.get("SCALE_SLOPE_NODE_COUNTS", "1,2,4").split(",") if x.strip()
)

# Per-node allocatable sandbox vCPU — the LOCKED density denominator (per-node
# allocatable, NOT cluster-capacity sum; see metrics.density_per_vcpu). Read from
# the cluster live in run_sweep; this env is the override/fallback for the value.
_ALLOCATABLE_VCPU_PER_NODE = float(
    os.environ.get("SCALE_SLOPE_ALLOCATABLE_VCPU_PER_NODE", "0") or "0"
)

# Throughput window + sub-1s bar (mirrors burst_create / metrics THRESHOLD_1S_MS).
_THROUGHPUT_WINDOW_S = float(os.environ.get("SCALE_SLOPE_WINDOW_S", "1.0"))
_TTFI_CEILING_MS = float(os.environ.get("SCALE_SLOPE_TTFI_CEILING_MS", "1000.0"))

# Warm slots per node — the per-point burst size scales with the node-count so each
# point measures the SAME per-node load (the retention question is per-node).
_SLOTS_PER_NODE = int(os.environ.get("SCALE_SLOPE_SLOTS_PER_NODE", "10"))

_WARMUP_TIMEOUT_S = int(os.environ.get("SCALE_SLOPE_WARMUP_TIMEOUT_S", "240"))
_BIND_TIMEOUT_S = int(os.environ.get("SCALE_SLOPE_BIND_TIMEOUT_S", "180"))
_POLL_S = 0.05

_TPL_GVR = template_gvr()
_CLM_GVR = claim_gvr()
_SWP_GVR = warmpool_gvr()

_SCENARIO_LABEL = {"honest-bench/scenario": "scale-slope"}


def _build_template_manifest(template_name: str) -> dict:
    """Minimal busybox SandboxTemplate (mirrors burst_create's gVisor gating)."""
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


def _build_warmpool_manifest(pool_name: str, template_name: str, replicas: int) -> dict:
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


def _classify_scale_slope(points, *, threshold_ms: float, window_s: float) -> dict:
    """Pure linearity classifier. No cluster, no clock — unit-testable.

    `points` is a list of per-node-count raw measurements, each a dict:
        {"node_count": int,                    # the scale point (>= 1)
         "max_concurrent": int,                # sandboxes Ready (density numerator)
         "allocatable_vcpu_per_node": float,   # density denominator (per-node)
         "ttfi_samples_ms": [float|None, ...]} # for throughput_per_node

    Returns the TOP-LEVEL scale_proof object the render side consumes:
        {"scale_points": [{"node_count": K, "density": D}, ...],
         "density_retention": float,
         "thpt_retention": float}              # omitted when base throughput == 0

    emit-only-when-complete: returns {} unless there are >= 2 distinct node-count
    points AND the base point delivered a real density (> 0). Every number is
    computed by the LOCKED metrics.py functions so the page's basis is single-source.
    """
    if not isinstance(points, list):
        return {}

    # Keep only well-formed points, dedup by node_count (first wins), sort ascending.
    seen: dict[int, dict] = {}
    for p in points:
        if not isinstance(p, dict):
            continue
        nc = p.get("node_count")
        if isinstance(nc, bool) or not isinstance(nc, int) or nc < 1:
            continue
        if nc in seen:
            continue
        seen[nc] = p
    ordered = [seen[k] for k in sorted(seen)]
    if len(ordered) < 2:
        return {}  # no slope from a single point

    densities: list[float] = []
    thpts: list[float] = []
    for p in ordered:
        # density at scale point K = total-ready / TOTAL-allocatable-vCPU (per-node ×
        # K). Both numerator and denominator scale with K, so a linear substrate
        # reads the SAME per-vCPU density at every point (the doc's flat 1.88). The
        # LOCKED density_per_vcpu is generic division; the producer supplies the
        # total-vCPU basis (matches the cross-contract: density_per_vcpu(188,100) →
        # density_per_vcpu(752,400), all 1.88).
        total_vcpu = p["allocatable_vcpu_per_node"] * p["node_count"]
        if total_vcpu <= 0:
            return {}  # unknown allocatable vCPU — density basis undefined, emit nothing
        densities.append(metrics.density_per_vcpu(p["max_concurrent"], total_vcpu))
        thpts.append(
            metrics.throughput_per_node(
                p.get("ttfi_samples_ms", []), threshold_ms, window_s, p["node_count"]
            )
        )

    if densities[0] <= 0:
        return {}  # degenerate base — density retention undefined

    scale_points = [
        {"node_count": p["node_count"], "density": densities[i]}
        for i, p in enumerate(ordered)
    ]
    out = {
        "scale_points": scale_points,
        "density_retention": metrics.retention(densities[0], densities[-1]),
    }
    # thpt_retention only when the base throughput is real (> 0). retention() raises
    # on a zero base, and the render side has NO per-point fallback for it, so an
    # omitted thpt_retention renders the throughput column pending (honest), never a
    # fabricated ratio.
    if thpts[0] > 0:
        out["thpt_retention"] = metrics.retention(thpts[0], thpts[-1])
    return out


def _is_claim_ready_and_bound(status: dict) -> bool:
    if not status:
        return False
    conds = status.get("conditions") or []
    has_ready = any(
        c.get("type") == "Ready" and c.get("status") == "True" for c in conds
    )
    has_bound = bool((status.get("sandbox") or {}).get("name"))
    return has_ready and has_bound


def _allocatable_vcpu_per_node(core_v1) -> float:
    """Per-node allocatable vCPU = the LOCKED density denominator.

    The minimum allocatable cpu across schedulable nodes (the honest per-node
    sandbox-schedulable basis; a heterogeneous pool is bounded by its smallest
    node). Falls back to the env override when no node reports allocatable cpu.
    """
    nodes = core_v1.list_node()
    per_node = []
    for node in nodes.items:
        alloc = (node.status.allocatable or {}) if node.status else {}
        cpu = alloc.get("cpu")
        if cpu is None:
            continue
        if isinstance(cpu, str) and cpu.endswith("m"):
            per_node.append(float(cpu[:-1]) / 1000.0)
        else:
            try:
                per_node.append(float(cpu))
            except (TypeError, ValueError):
                continue
    if per_node:
        return min(per_node)
    return _ALLOCATABLE_VCPU_PER_NODE


def _parse_label_selector(selector: str):
    """Split a "key=value" (or bare "key") node-label selector into (key, value).

    A bare "key" yields value=None, which _node_matches reads as a presence test
    (the label key exists, any value). An empty/whitespace selector yields
    (None, None) — _count_capable_nodes treats a selector-less call as "match none"
    only when a runtime class is pinned, so this is never the no-runtime path.
    """
    sel = (selector or "").strip()
    if not sel:
        return None, None
    if "=" in sel:
        key, _, value = sel.partition("=")
        return key.strip(), value.strip()
    return sel, None


def _node_matches(labels: dict, key, value) -> bool:
    """True when a node's label dict satisfies the (key, value) selector.

    value=None → presence test (key in labels); value set → exact-match.
    """
    if not key:
        return False
    if value is None:
        return key in (labels or {})
    return (labels or {}).get(key) == value


def _count_capable_nodes(node_label_dicts, *, runtime_class: str, gvisor_label: str) -> int:
    """Pure: how many nodes can actually HOST this sweep's sandboxes.

    When the sweep pins a runtimeClassName (runtime_class truthy — e.g. gvisor), a
    sandbox schedules ONLY onto nodes advertising that runtime, so the capable count
    is the number of nodes matching gvisor_label. When no runtime class is pinned,
    every node can host the sandbox, so the capable count is simply len(nodes). This
    is the honest-by-construction denominator basis: counting total nodes (3) when
    only 1 is gVisor-capable lets a tier pile K×slots gVisor pods onto 1 node and
    fabricate per-node density. [#3949]
    """
    nodes = list(node_label_dicts or [])
    if not runtime_class:
        return len(nodes)
    key, value = _parse_label_selector(gvisor_label)
    return sum(1 for labels in nodes if _node_matches(labels, key, value))


def _node_is_ready(node) -> bool:
    conds = (node.status.conditions or []) if node.status else []
    return any(
        getattr(c, "type", None) == "Ready" and getattr(c, "status", None) == "True"
        for c in conds
    )


def _node_label_dicts(core_v1, *, ready_only: bool = True) -> list:
    """Live: each node's label dict (Ready nodes only by default).

    Autoscale-wait counts only Ready nodes — a node that exists but is NotReady
    cannot yet host a pod, so counting it would let the tier proceed before the
    capacity is genuinely there.
    """
    out = []
    for node in core_v1.list_node().items:
        if ready_only and not _node_is_ready(node):
            continue
        out.append((node.metadata.labels or {}) if node.metadata else {})
    return out


def _wait_for_capable_nodes(
    core_v1, *, target: int, timeout_s: int, runtime_class: str, gvisor_label: str
) -> int:
    """Poll until >= target gVisor-capable Ready nodes exist, or timeout. Returns
    the final capable count (caller drops the tier when it is < target)."""
    deadline = time.monotonic() + timeout_s
    capable = _count_capable_nodes(
        _node_label_dicts(core_v1, ready_only=True),
        runtime_class=runtime_class, gvisor_label=gvisor_label,
    )
    while capable < target and time.monotonic() < deadline:
        time.sleep(2.0)
        capable = _count_capable_nodes(
            _node_label_dicts(core_v1, ready_only=True),
            runtime_class=runtime_class, gvisor_label=gvisor_label,
        )
    return capable


def run_sweep(scenario_name: str = "scale_slope") -> dict:
    """Live multi-node sweep producer. Returns the scale_proof object (or {}).

    For each node-count in _NODE_COUNTS the cluster can fill, provision a warm pool
    of K * _SLOTS_PER_NODE slots, fire that many claims, and record each claim's
    Ready+bound TTFI. The per-point raw measurements feed _classify_scale_slope,
    which delegates the density/throughput/retention math to the LOCKED metrics
    functions. Lazy kubernetes import keeps the offline classifier tests free of the
    client dependency.

    NOTE: this is the heavy, mutating live producer. It is invoked by the
    coordinated multi-node sweep fire (a4s1's lane), NOT by the default single-node
    auto-refresh run — on a single-node cluster only the K=1 point is achievable, so
    the classifier returns {} (no slope) by construction.
    """
    from kubernetes import client as k8s_client

    try:  # package context
        from ._kube import load_cluster_config
    except ImportError:  # standalone
        from _kube import load_cluster_config  # type: ignore

    load_cluster_config()
    core_v1 = k8s_client.CoreV1Api()
    custom = k8s_client.CustomObjectsApi()

    capable_now = _count_capable_nodes(
        _node_label_dicts(core_v1, ready_only=True),
        runtime_class=_RUNTIME_CLASS, gvisor_label=_GVISOR_NODE_LABEL,
    )
    # Skip-guard ceiling: the MAX gVisor-capable nodes the cluster can reach. With an
    # autoscaling gVisor pool, set _MAX_GVISOR_NODES to the pool max so a tier above
    # the currently-Ready count is attempted (the per-tier autoscale-wait brings the
    # nodes up) rather than false-skipped on the pre-scale count. 0 → assume no
    # autoscale and cap at what is Ready now. [#3949]
    max_capable = _MAX_GVISOR_NODES or capable_now
    alloc_vcpu = _allocatable_vcpu_per_node(core_v1)
    log.info(
        "scale-slope sweep: %d gVisor-capable node(s) Ready now (ceiling=%d, "
        "runtime_class=%r); allocatable vCPU/node=%g; sweep node-counts=%r",
        capable_now, max_capable, _RUNTIME_CLASS, alloc_vcpu, _NODE_COUNTS,
    )

    points: list[dict] = []
    for k in sorted(set(_NODE_COUNTS)):
        if k > max_capable:
            log.info(
                "skipping node-count=%d (cluster can reach at most %d gVisor-capable "
                "nodes)", k, max_capable,
            )
            continue
        claim_count = k * _SLOTS_PER_NODE
        ttfis = _measure_point(
            custom, core_v1, node_count=k, claim_count=claim_count,
        )
        if ttfis is None:
            log.info(
                "dropping node-count=%d (autoscale-wait did not reach %d capable "
                "nodes)", k, k,
            )
            continue
        max_concurrent = sum(1 for v in ttfis if v is not None)
        points.append(
            {
                "node_count": k,
                "max_concurrent": max_concurrent,
                "allocatable_vcpu_per_node": alloc_vcpu,
                "ttfi_samples_ms": ttfis,
            }
        )

    return _classify_scale_slope(
        points, threshold_ms=_TTFI_CEILING_MS, window_s=_THROUGHPUT_WINDOW_S,
    )


def _measure_point(custom, core_v1, *, node_count: int, claim_count: int):
    """Provision a K-node-sized burst, fire claims, return TTFI samples (ms|None).

    Returns None to DROP the tier when the cluster cannot reach `node_count`
    gVisor-capable Ready nodes within the autoscale-wait — measuring it anyway would
    pile claim_count gVisor pods onto too few nodes and fabricate per-node density.
    """
    suffix = uuid.uuid4().hex[:8]
    template_name = f"sstmpl-{suffix}"
    pool_name = f"sspool-{suffix}"
    claim_names = [f"ssc{i:03d}-{suffix}" for i in range(claim_count)]

    custom.create_namespaced_custom_object(
        group=_TPL_GVR[0], version=_TPL_GVR[1], namespace=_NAMESPACE,
        plural=_TPL_GVR[2], body=_build_template_manifest(template_name),
    )
    custom.create_namespaced_custom_object(
        group=_SWP_GVR[0], version=_SWP_GVR[1], namespace=_NAMESPACE,
        plural=_SWP_GVR[2],
        body=_build_warmpool_manifest(pool_name, template_name, claim_count),
    )
    try:
        # Per-tier autoscale-wait: the warm pool above just made claim_count gVisor
        # pods pending, which triggers the autoscaler. Wait for K gVisor-capable
        # nodes before measuring; drop the tier if they never arrive. [#3949]
        capable = _wait_for_capable_nodes(
            core_v1, target=node_count, timeout_s=_NODE_SCALE_TIMEOUT_S,
            runtime_class=_RUNTIME_CLASS, gvisor_label=_GVISOR_NODE_LABEL,
        )
        if capable < node_count:
            log.info(
                "node-count=%d: only %d gVisor-capable node(s) Ready after %ds "
                "autoscale-wait — dropping tier", node_count, capable,
                _NODE_SCALE_TIMEOUT_S,
            )
            return None
        _wait_for_pool_warm(
            custom, pool_name=pool_name, target_ready=claim_count,
            timeout_s=_WARMUP_TIMEOUT_S,
        )
        create_times: dict[str, float] = {}
        for name in claim_names:
            custom.create_namespaced_custom_object(
                group=_CLM_GVR[0], version=_CLM_GVR[1], namespace=_NAMESPACE,
                plural=_CLM_GVR[2], body=_build_claim_manifest(name, pool_name),
            )
            create_times[name] = time.monotonic()

        bound_at = _measure_claim_latencies(claim_names, timeout_s=_BIND_TIMEOUT_S)
        ttfis: list = []
        for name in claim_names:
            if name in bound_at:
                ttfis.append((bound_at[name] - create_times[name]) * 1000.0)
            else:
                ttfis.append(None)
        return ttfis
    finally:
        _cleanup(
            custom, claim_names=claim_names, pool_name=pool_name,
            template_name=template_name,
        )


def _wait_for_pool_warm(custom, *, pool_name: str, target_ready: int, timeout_s: int) -> dict:
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
        if int(status.get("readyReplicas") or 0) >= target_ready:
            return obj
        time.sleep(1.0)
    raise RuntimeError(
        f"SandboxWarmPool {pool_name} did not reach readyReplicas>={target_ready} "
        f"within {timeout_s}s (last status={last_status!r})"
    )


def _watch_one_claim(*, claim_name: str, deadline: float, bound_at: dict) -> None:
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


def _measure_claim_latencies(claim_names: list, *, timeout_s: int) -> dict:
    import threading

    bound_at: dict = {}
    deadline = time.monotonic() + timeout_s
    threads = [
        threading.Thread(
            target=_watch_one_claim,
            kwargs={"claim_name": name, "deadline": deadline, "bound_at": bound_at},
            daemon=True,
        )
        for name in claim_names
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=timeout_s + 5)
    return bound_at


def _cleanup(custom, *, claim_names: list, pool_name: str, template_name: str) -> None:
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


# math imported for potential ceil-based sizing parity with burst_create; keep the
# import referenced so linters do not flag it while run_sweep evolves.
_ = math
