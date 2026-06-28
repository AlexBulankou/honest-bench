"""Burst-create throughput benchmark: how many sandboxes go Ready in under 1s.

This is the fleet headline cell. alex's framing (#1 metric): NOT a single
cold-start number — the throughput question "how many sandboxes can you get
ready, each within 1 second, in a burst" against a warm pool.

    Setup    : SandboxWarmPool replicas: K (controller has had the warmup window
               to fill all K slots).
    Action   : Fire K SandboxClaims as fast as a serial loop allows. Measure
               each claim's wall-clock time-to-first-instruction (TTFI),
               approximated by Ready+bound (see note below).
    Headline : sandboxes_ready_under_1s = the COUNT of claims whose TTFI cleared
               the sub-1s bar. density_per_vcpu = that count divided by the
               cluster's total capacity vCPU, so the number is comparable across
               runner hardware (a 2-vCPU hosted runner vs a real GKE node).
    Why      : Sub-second burst provisioning is the latency-critical AI-agent
               enabler warm pools promise. The count (not a rate) is exactly
               alex's "X sandboxes in <1s" headline, and it is falsifiable: a
               stranger reruns the harness and gets the same shape.

## Test shape

1. Provision a SandboxTemplate (minimal busybox — controller-level provisioning
   latency is what the scenario measures, not the user's image-build time).
2. Create SandboxWarmPool replicas=K; wait until status.readyReplicas=K.
3. Fire K SandboxClaim creates. Record `t0_i` for each immediately after
   create() returns (per-claim baseline = user-perceived create-call-return).
4. Poll each claim until Ready+bound; record `t1_i` on first observation.
5. TTFI_i = t1_i - t0_i (seconds), measured from each claim's OWN create time —
   so serial firing never penalizes a later claim's sub-1s budget.
6. sandboxes_ready_under_1s = |{ i : TTFI_i < TTFI_CEILING_S }| (default 1.0s).
   density_per_vcpu = sandboxes_ready_under_1s / total_cluster_vcpu.
7. PASS = the warm pool delivered most of its burst under the bar:
   sandboxes_ready_under_1s >= ceil(K * MIN_QUALIFIED_RATIO) (default 0.8).
8. Cleanup: K claims, pool, template.

## Why "Ready+bound" approximates "first-byte-stdout" (TTFI)

Literal first-byte-stdout requires an in-pod exec per claim (K parallel
pod/exec channels) — ~Kx the RBAC surface, and the websocket-setup latency
masks the pool delta. The claim's Ready=True condition fires only after the pod
is Ready, so Ready+bound is a tight upper bound on first-byte time (at most a
few hundred ms looser). A future iteration wanting literal stdout-time can add a
pods/exec grant and a per-claim exec round-trip after Ready+bound.

## Why a COUNT, not a per-second rate

alex's headline is "X sandboxes in <1s". A count is more falsifiable than a rate
because TTFI is measured per-claim from its own create time, so the metric never
depends on a divide-by-window choice (which would conflate the client's serial
create-loop speed with the controller's bind rate). density_per_vcpu normalizes
the count for hardware so a kind-on-a-2-vCPU-runner number is not mis-read
against a GKE-node number — the build banner's cluster_substrate makes the
substrate explicit, and the density makes the magnitude comparable.

## Crash posture

Infrastructure failures (controller unhealthy, CRDs missing, RBAC denied) raise
— the harness loop records a crash as a FAIL cell, never a fabricated PASS.
Scenario-outcome FAILs (pool under-delivered the sub-1s burst) return
("FAIL", "<excerpt>", sla_metrics) with the real measured count surfaced.

## Runtime-class guard (gke-sandbox headline honesty)

`cluster_substrate` (the render banner) and the pool's `runtimeClassName` are
INDEPENDENT env vars, so a `gke-sandbox` substrate with an unset/non-gVisor
BURST_CREATE_RUNTIME_CLASS would publish runc pods under a gVisor banner — a
green count that lies. Two guards close that, both crash-FAIL (never a fabricated
PASS), gated to the gke-sandbox substrate so kind/gke are unaffected:

  1. Pure (fail-fast, no cluster): a gke-sandbox-labeled run MUST pin
     `runtimeClassName=gvisor` (`_assert_substrate_runtime_consistency`).
  2. Live read-back (post-measurement): each counted (bound) sandbox's backing
     Pod is resolved by owner-uid and asserted to carry the expected
     `spec.runtimeClassName` (`_verify_bound_pods_runtime`) — the same
     "Ready does not prove gVisor" assertion gvisor_canary makes, applied to the
     throughput headline so a silent runc fallback can't publish as gVisor.
"""

from __future__ import annotations

try:  # package context (production: run.py loads harness.scenarios.burst_create)
    from ._apiversion import (
        claim_gvr, ext_api_version, sandbox_gvr, template_gvr, warmpool_gvr,
    )
except ImportError:  # standalone (dependency-free test from the scenarios/ dir)
    from _apiversion import (
        claim_gvr, ext_api_version, sandbox_gvr, template_gvr, warmpool_gvr,
    )

import logging
import math
import os
import time
import uuid

log = logging.getLogger("sandbox-scenario.burst-create")


_NAMESPACE = os.environ.get("BENCH_NAMESPACE", "default")
_SANDBOX_IMAGE = os.environ.get("BURST_CREATE_SANDBOX_IMAGE", "busybox:1.36")

# Optional RuntimeClass for the pool's pods. Default "" -> omit the field, so the
# pods run under the node's default runtime (runc) — correct for vanilla kind
# (the auto-refresh Action runner), where no gVisor RuntimeClass exists. Set
# BURST_CREATE_RUNTIME_CLASS=gvisor to pin the burst to the gVisor runtime on a
# gke-sandbox cluster, so the published sandboxes_ready_under_1s is a REAL
# gVisor-isolated throughput number (build banner's cluster_substrate makes the
# substrate explicit). Same probe, two substrates — no separate scenario.
_RUNTIME_CLASS = os.environ.get("BURST_CREATE_RUNTIME_CLASS", "")

# The substrate the run.py banner will label this result with (same env run.py
# reads). burst_create is substrate-agnostic (perf matrix), so on a `gke-sandbox`
# substrate its published sandboxes_ready_under_1s reads as a gVisor number — the
# two env vars are otherwise INDEPENDENT, so a gke-sandbox substrate with an unset
# BURST_CREATE_RUNTIME_CLASS would label runc pods as gVisor (the false-headline
# gap the guard below closes). Read here only to enforce that consistency.
_CLUSTER_SUBSTRATE = os.environ.get("BENCH_CLUSTER_SUBSTRATE", "kind")

# The canonical gVisor RuntimeClass name (GKE Sandbox / upstream gVisor convention,
# matching gvisor_canary's default). A `gke-sandbox`-labeled burst MUST pin this so
# the headline count is a REAL gVisor-isolated number, not a runc number wearing a
# gVisor label.
_GVISOR_RUNTIME_CLASS = "gvisor"

# Burst size. K warm slots, K claims fired — the whole pool is the warm tier
# (unlike warmpool_cold_start, which deliberately fires more claims than slots to
# expose a cold tier). Default 10 fits a 2-vCPU hosted runner's kind node
# (busybox at 10m/16Mi -> 100m/160Mi for 10 pods); env-tunable up for a real
# GKE cluster where a larger burst is the honest headline.
_POOL_REPLICAS = int(os.environ.get("BURST_CREATE_POOL_REPLICAS", "10"))
_CLAIM_COUNT = int(os.environ.get("BURST_CREATE_CLAIM_COUNT", str(_POOL_REPLICAS)))

# The sub-1s TTFI bar (seconds). alex's "<1s" headline; env-tunable for recal.
_TTFI_CEILING_S = float(os.environ.get("BURST_CREATE_TTFI_CEILING_S", "1.0"))

# PASS gate: fraction of the burst that must clear the sub-1s bar. 0.8 = the warm
# pool delivered most of its slots under 1s; a pool that under-delivers (fewer
# warm slots than K, so the tail binds cold > 1s) drops below the ratio -> FAIL.
_MIN_QUALIFIED_RATIO = float(
    os.environ.get("BURST_CREATE_MIN_QUALIFIED_RATIO", "0.8")
)

# Public benchmark metric keys. Both lowercase-alnum+underscore so they pass the
# emitter's _METRIC_KEY_RE with no harness-schema change; render/schema.py's
# METRIC_LABELS registers their display labels (a4s1's render-side lane).
_KEY_COUNT = "sandboxes_ready_under_1s"
_KEY_DENSITY = "density_per_vcpu"

# Timeouts. Pool warmup: 240s for up to ~10-20 replicas (pull + schedule +
# start). Per-claim bind: 180s — a cold-tail claim can take 30-90s on a fresh
# node, and we want to measure it (as a >1s non-qualifier), not time it out early.
# Both env-tunable: gVisor (runsc) adds per-pod sandbox-init overhead, so a large
# burst on a gke-sandbox cluster may need a longer warmup window than runc/kind —
# raise BURST_CREATE_WARMUP_TIMEOUT_S rather than crash-FAIL a slow-but-healthy
# pool fill. Neither timeout affects the measured TTFI (per-claim, from create).
_WARMUP_TIMEOUT_S = int(os.environ.get("BURST_CREATE_WARMUP_TIMEOUT_S", "240"))
_BIND_TIMEOUT_S = int(os.environ.get("BURST_CREATE_BIND_TIMEOUT_S", "180"))
_POLL_S = 0.05  # per-claim thread poll — must be << the sub-1s threshold

# CR coordinates.
_TPL_GVR = template_gvr()
_CLM_GVR = claim_gvr()
_SWP_GVR = warmpool_gvr()
_SBX_GVR = sandbox_gvr()

_SCENARIO_LABEL = {"honest-bench/scenario": "burst-create"}


def _assert_substrate_runtime_consistency(
    substrate: str, runtime_class: str,
) -> None:
    """Sub-gap 1: refuse a gVisor-labeled headline when the runtime isn't gVisor.

    Pure logic (no cluster calls) — `cluster_substrate` and the pool's
    `runtimeClassName` are independent env vars with no cross-check, so a
    `gke-sandbox` substrate with an unset/non-gVisor BURST_CREATE_RUNTIME_CLASS
    would publish runc pods under a gVisor banner. Crash-FAIL (consistent with the
    cell's crash posture) before the cluster is touched, so the mistake is caught
    fail-fast rather than after a full burst. Substrates other than `gke-sandbox`
    impose no constraint (kind/gke do not claim gVisor isolation).
    """
    if substrate == "gke-sandbox" and runtime_class != _GVISOR_RUNTIME_CLASS:
        raise RuntimeError(
            f"burst_create refuses to publish a gke-sandbox-labeled result while "
            f"BURST_CREATE_RUNTIME_CLASS={runtime_class!r} (expected "
            f"{_GVISOR_RUNTIME_CLASS!r}): the cluster_substrate banner says gVisor "
            f"but the warm pool would run under the node default runtime, so the "
            f"published sandboxes_ready_under_1s would be a false gVisor headline. "
            f"Set BURST_CREATE_RUNTIME_CLASS=gvisor on a gke-sandbox cluster."
        )


def _build_template_manifest(template_name: str) -> dict:
    """Minimal busybox SandboxTemplate.

    When _RUNTIME_CLASS is set (e.g. "gvisor" on a gke-sandbox cluster), the
    pod's `runtimeClassName` is pinned so the whole warm pool — and therefore the
    measured burst — runs under that runtime. Omitted by default so a vanilla-kind
    run (no gVisor RuntimeClass) is not stranded Pending on an unschedulable pod.
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
        # must tolerate that taint or it stays Pending — the warm pool would never
        # fill and the burst would crash-FAIL. We add the toleration ourselves rather
        # than rely on GKE's admission webhook auto-injecting it: a belt-and-suspenders
        # no-op if GKE also injects it, necessary if it doesn't. Gated on _RUNTIME_CLASS
        # so a vanilla-kind run (no such taint) is unaffected, and operator=Exists keys
        # only on the taint key so any value (gvisor / gvisor-experimental) is covered.
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


def _parse_cpu_quantity(q) -> float:
    """Parse a K8s cpu quantity (e.g. "8", "8000m") to whole vCPU (float cores).

    Capacity cpu is usually an integer-core string ("8") but millicores ("8000m")
    are valid; both map to cores. Returns 0.0 on anything unparseable so a
    surprise unit can never inflate the density denominator silently.
    """
    if isinstance(q, (int, float)) and not isinstance(q, bool):
        return float(q)
    if not isinstance(q, str) or not q:
        return 0.0
    try:
        if q.endswith("m"):
            return float(q[:-1]) / 1000.0
        return float(q)
    except ValueError:
        return 0.0


def _sum_node_vcpu(core_v1) -> float:
    """Total cluster capacity vCPU = sum of every node's status.capacity.cpu.

    Cluster-level (not the harness process's os.cpu_count()) so density_per_vcpu
    reflects the substrate the sandboxes actually ran on. Raises on an API
    failure (an infra problem, surfaced as a crash-FAIL) rather than guessing.
    """
    nodes = core_v1.list_node()
    total = 0.0
    for node in nodes.items:
        cap = (node.status.capacity or {}) if node.status else {}
        total += _parse_cpu_quantity(cap.get("cpu"))
    return total


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

    Runs in its own thread with its own CustomObjectsApi sharing the default
    ApiClient's urllib3 pool — `run()` pins connection_pool_maxsize >= claim
    count before spawning us so threads do not serialize on a free connection
    (which would re-coarsen the sub-second granularity). On first observation of
    Ready+bound it writes `bound_at[claim_name] = time.monotonic()` (dict
    item-assignment is atomic under CPython) and returns.
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
) -> dict[str, float]:
    """Measure each claim's Ready+bound observation time, one thread per claim.

    Returns bound_at mapping each resolved claim to its monotonic bind time;
    claims that never bound within `timeout_s` are absent. Thread-per-claim (not
    single-threaded round-robin) is required because the metric gates on a sub-1s
    threshold: a round-robin sweep of K claims with a poll-sleep has worst-case
    observation granularity ~= K x per-GET + poll ~= ~1s, the same order as the
    threshold, so a genuinely-warm 0.3s bind gets measured >1.0s and miscounted.
    Independent per-claim threads give ~_POLL_S (50ms) granularity. K I/O-bound
    GET loops are trivial apiserver load (the GIL releases during the round-trip).
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
    return bound_at


def _classify_burst(
    ttfis: dict[str, float | None],
    *,
    claim_count: int,
    ttfi_ceiling_s: float,
    total_vcpu: float,
    min_qualified_ratio: float,
) -> tuple[bool, dict, dict]:
    """Pure burst-throughput classifier. No cluster, no clock — unit-testable.

    `ttfis` maps each fired claim to its measured TTFI seconds, or None if it
    never bound. Returns (passed, breakdown, sla_metrics):
      - sandboxes_ready_under_1s = count of claims with TTFI < ttfi_ceiling_s
      - density_per_vcpu = that count / total_vcpu (0.0 if vcpu unknown)
      - passed iff count >= ceil(claim_count * min_qualified_ratio)
    sla_metrics is emitted whenever count > 0 (a real measurement, surfaced even
    on FAIL like warmpool_cold_start surfaces warm_max); count == 0 emits {} so a
    zero-delivery burst publishes no fabricated number.
    """
    measured = [v for v in ttfis.values() if v is not None]
    count_under = sum(1 for v in measured if v < ttfi_ceiling_s)
    timeouts = [k for k, v in ttfis.items() if v is None]

    density = (count_under / total_vcpu) if total_vcpu > 0 else 0.0
    threshold = math.ceil(claim_count * min_qualified_ratio)
    passed = count_under >= threshold

    breakdown = {
        "count_under": count_under,
        "claim_count": claim_count,
        "pass_threshold": threshold,
        "total_vcpu": total_vcpu,
        "density_per_vcpu": density,
        "ttfi_ceiling_s": ttfi_ceiling_s,
        "completed_count": len(measured),
        "timeouts": timeouts,
        "all_ttfi_s": sorted(measured),
    }

    if count_under > 0:
        sla_metrics = {
            _KEY_COUNT: float(count_under),
            _KEY_DENSITY: density,
            "n": claim_count,
        }
    else:
        sla_metrics = {}

    return passed, breakdown, sla_metrics


def _bound_sandbox_name(custom, *, claim_name: str) -> str | None:
    """Re-GET a bound claim; return its status.sandbox.name (the bound Sandbox)."""
    group, version, plural = _CLM_GVR
    obj = custom.get_namespaced_custom_object(
        group=group, version=version, namespace=_NAMESPACE,
        plural=plural, name=claim_name,
    )
    return ((obj or {}).get("status") or {}).get("sandbox", {}).get("name")


def _verify_bound_pods_runtime(
    custom, core, *, bound_claim_names: list[str], expected_runtime_class: str,
) -> int:
    """Sub-gap 2: assert every counted sandbox's backing Pod ran under runsc.

    The published count is labeled gVisor on a gke-sandbox substrate; a Ready
    sandbox alone does NOT prove gVisor (a controller that silently stripped
    runtimeClassName would still reach Ready under runc — the "green cell that
    lies" gvisor_canary guards against). This closes that hole for the throughput
    headline: it resolves each bound claim -> its bound Sandbox -> the backing Pod
    (owner-uid match, gvisor_canary's convention-independent path — pod-name shape
    and label propagation are not assumed) and crash-FAILs if ANY backing Pod's
    `spec.runtimeClassName` != expected (or is unlocatable). A partially-runc burst
    therefore never publishes as a gVisor headline. Returns the count verified.

    Live read (one claim-GET + one Sandbox-GET per bound claim + one namespace
    Pod list); runs post-measurement so it never perturbs the measured TTFI, and
    run() gates it to the gke-sandbox substrate only (kind/gke stay read-free).
    """
    sbx_group, sbx_version, sbx_plural = _SBX_GVR
    uid_to_sandbox: dict[str, str] = {}
    for claim_name in bound_claim_names:
        sbx_name = _bound_sandbox_name(custom, claim_name=claim_name)
        if not sbx_name:
            raise RuntimeError(
                f"bound claim {claim_name} has no status.sandbox.name on re-read — "
                f"its backing Pod's runtime class cannot be verified, so the "
                f"gVisor-labeled count cannot honestly publish."
            )
        sbx = custom.get_namespaced_custom_object(
            group=sbx_group, version=sbx_version, namespace=_NAMESPACE,
            plural=sbx_plural, name=sbx_name,
        )
        uid = ((sbx or {}).get("metadata") or {}).get("uid")
        if uid:
            uid_to_sandbox[uid] = sbx_name

    pods = core.list_namespaced_pod(namespace=_NAMESPACE)
    uid_to_pod: dict[str, object] = {}
    for pod in pods.items:
        for owner in (pod.metadata.owner_references or []):
            if owner.uid in uid_to_sandbox:
                uid_to_pod[owner.uid] = pod

    violations: list[str] = []
    verified = 0
    for uid, sbx_name in uid_to_sandbox.items():
        pod = uid_to_pod.get(uid)
        if pod is None:
            violations.append(f"{sbx_name}: backing Pod not found by owner uid")
            continue
        rtc = pod.spec.runtime_class_name
        if rtc != expected_runtime_class:
            violations.append(
                f"{sbx_name}: Pod {pod.metadata.name} runtimeClassName={rtc!r}"
            )
        else:
            verified += 1
    if violations:
        raise RuntimeError(
            f"burst_create refuses to publish a gVisor-labeled count: "
            f"{len(violations)}/{len(uid_to_sandbox)} bound sandboxes did not "
            f"schedule under RuntimeClass {expected_runtime_class!r} "
            f"[{'; '.join(violations)}]. A Ready sandbox running under runc is the "
            f"silent isolation drop the runtime read-back exists to catch."
        )
    return verified


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
    """Provision pool, fire K claims, count sub-1s binds, classify PASS/FAIL.

    Returns a 3-tuple (outcome, excerpt, sla_metrics). `sla_metrics` carries
    sandboxes_ready_under_1s + density_per_vcpu + the reserved "n" (= claims
    fired) when at least one claim cleared the sub-1s bar; an all-cold burst
    emits {} (no fabricated number). The metric is surfaced on PASS and FAIL so a
    pool that delivers some-but-not-enough sub-1s slots still publishes the real
    count it achieved.
    """
    from kubernetes import client as k8s_client
    from kubernetes import config as k8s_config

    # Sub-gap 1 (pure, fail-fast): a gke-sandbox-labeled result MUST pin the gVisor
    # RuntimeClass, else the published count is a runc number under a gVisor banner.
    # Checked before the cluster is touched so the mistake crashes immediately.
    _assert_substrate_runtime_consistency(_CLUSTER_SUBSTRATE, _RUNTIME_CLASS)

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
    core_v1 = k8s_client.CoreV1Api()

    # Cluster vCPU for the density denominator — read live, before the burst.
    total_vcpu = _sum_node_vcpu(core_v1)
    log.info("cluster total capacity vCPU = %g", total_vcpu)

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
        # after each create() returns — the per-claim baseline is user-perceived
        # create-call-return, so serial firing never eats a later claim's budget.
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

        bound_at = _measure_claim_latencies(
            claim_names, timeout_s=_BIND_TIMEOUT_S,
        )

        ttfis: dict[str, float | None] = {}
        for name in claim_names:
            if name in bound_at:
                ttfis[name] = bound_at[name] - create_times[name]
            else:
                ttfis[name] = None
                log.warning("claim %s timed out before Ready+bound", name)

        # Sub-gap 2 (live read-back, gke-sandbox only): the counted sandboxes are
        # the bound claims; on a gke-sandbox substrate verify each one's backing
        # Pod actually scheduled under runsc before publishing the gVisor-labeled
        # count. Crash-FAILs on a silent runc fallback. kind/gke skip this (no
        # gVisor claim to verify, so the path stays read-free there).
        if _CLUSTER_SUBSTRATE == "gke-sandbox":
            bound_claim_names = [n for n in claim_names if n in bound_at]
            verified = _verify_bound_pods_runtime(
                custom, core_v1,
                bound_claim_names=bound_claim_names,
                expected_runtime_class=_RUNTIME_CLASS,
            )
            log.info(
                "runtime read-back: %d/%d bound sandboxes verified under "
                "RuntimeClass %r", verified, len(bound_claim_names), _RUNTIME_CLASS,
            )

        passed, breakdown, sla_metrics = _classify_burst(
            ttfis,
            claim_count=_CLAIM_COUNT,
            ttfi_ceiling_s=_TTFI_CEILING_S,
            total_vcpu=total_vcpu,
            min_qualified_ratio=_MIN_QUALIFIED_RATIO,
        )

        all_ttfi_str = ", ".join(f"{x:.3f}" for x in breakdown["all_ttfi_s"])
        density = breakdown["density_per_vcpu"]
        common = (
            f"{breakdown['count_under']}/{_CLAIM_COUNT} sandboxes Ready in "
            f"< {_TTFI_CEILING_S}s "
            f"(density={density:.3f}/vCPU over {breakdown['total_vcpu']:g} vCPU); "
            f"pass threshold={breakdown['pass_threshold']}. "
            f"All TTFIs (s, sorted): [{all_ttfi_str}]. "
            f"Timeouts: {breakdown['timeouts']!r}."
        )
        if passed:
            return ("PASS", f"Burst-create delivered the sub-1s tier: {common}",
                    sla_metrics)
        return (
            "FAIL",
            f"Burst-create under-delivered the sub-1s tier: {common} "
            f"Pool served fewer than {breakdown['pass_threshold']} warm slots "
            f"under the bar — controller-side warm-pool candidate.",
            sla_metrics,
        )
    finally:
        _cleanup(
            custom, claim_names=claim_names,
            pool_name=pool_name, template_name=template_name,
        )
