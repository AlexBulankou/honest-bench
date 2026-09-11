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
import statistics
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

# hb#835 lever-2: burst-aware pre-scale headroom. A claim burst sized close to
# (or above) the nominal pool can transiently drain readyReplicas toward 0
# mid-burst, waiting for steady-state autoscale to catch up rather than
# serving from an already-warm pool (hb#835's confirmed mechanism). Prescale
# the live WarmPool to `claim_count + headroom` (never below the nominal
# `_POOL_REPLICAS`) immediately before the burst so supply comfortably
# exceeds demand throughout, instead of tuning a bigger static floor for
# today's burst shape. 0 disables the lever entirely (opt-out, not the
# default — lever-2 is launched-by-default per the fleet's no-dormant-
# features doctrine).
_PRESCALE_HEADROOM = int(os.environ.get("WARMPOOL_COLD_START_PRESCALE_HEADROOM", "5"))

# hb#843: some nodepools carry a hard structural capacity ceiling below
# claim_count + headroom (e.g. kata's 2-node pool tops out around 42 pods,
# while claim_count=40 + headroom=5 asks for 45) -- lever-2 would then patch
# spec.replicas to an unreachable target every fire, guaranteeing a timeout
# and a degraded/disclosed prescale on every single run instead of only the
# rare genuine-contention case the fallback exists for. 0 disables clamping
# entirely (opt-out default, matches gVisor's much larger ceiling where this
# never binds); set to the nodepool's real per-scenario pod ceiling (e.g. 42
# for kata) to cap the prescale target at that value instead of computing an
# always-unreachable one.
_PRESCALE_CEILING = int(os.environ.get("WARMPOOL_COLD_START_PRESCALE_CEILING", "0"))


def _fill_gate_target(pool_replicas: int, claim_count: int) -> int:
    """Pre-fire fill-gate readyReplicas target.

    hb#804 capped the POST-fire classification target (`warm_target` below)
    at claims actually fired, not the raw pool size, so a diagnostic fire
    sized AT OR ABOVE the claim burst (pool_replicas > claim_count) is
    measured against its own genuine-hit population instead of
    unconditionally reading as under-delivery. The PRE-fire fill gate
    (`_wait_for_pool_warm`'s `target_ready`) had the identical uncapped
    blind spot: it waited for readyReplicas to reach the raw pool size even
    when that pool outsizes the burst. Mirror hb#804's fix here. Cold-
    baseline mode (pool_replicas <= 0) is untouched -- its target stays
    exactly pool_replicas so the negative-index neutral-cold semantics are
    preserved.
    """
    if pool_replicas > 0:
        return min(pool_replicas, claim_count)
    return pool_replicas

# Warm-tier gate (separation-based). The scenario verifies the warm pool yields
# a DISTINCT FAST provisioning tier — not an absolute latency. PASS iff the
# _POOL_REPLICAS fastest genuine-warm claims form a warm cluster that is EITHER
# absolutely fast (warm_max < _ABS_FAST_CEILING_S) OR clearly separated from the
# cold tier by median (cold_p50 / warm_p50 >= _SEPARATION_RATIO).
#
# Rationale: warm-tier end-to-end latency (scheduler + kubelet + container start)
# drifts up under cluster load, so a zero-margin absolute gate false-FAILs when
# genuinely-warm binds drift past the line while staying far below the cold tier.
# The separation gate is robust to absolute drift in either tier.
#
# The separation clause is PERCENTILE-MATCHED (cold_p50 / warm_p50), NOT
# min-vs-max (#6743). The prior cold_min/warm_max form let a single fast-cold
# bind crater the ratio: in a spread submission burst a late-created cold claim
# starts its create->bind clock late, so its span is tiny -> cold_min collapses
# -> ratio craters 20x even on a run whose warm adoption was actually HEALTHY
# (the fork run that scored 2.19 bound its warm claims ~3x SLOWER per-claim than
# the upstream run that scored 0.11 — the old metric inverted relative to warm
# health). Comparing the MEDIAN cold bind to the MEDIAN warm bind is robust to
# that outlier AND to submission-cadence queue pressure, which inflates both
# tiers ~equally so their ratio stays stable. Genuine under-delivery is caught by
# the provenance gate (fewer than _POOL_REPLICAS genuine pre-warmed hits -> FAIL,
# hb#450), decoupled from the separation clause. The absolute clause keeps the
# gate honest if a future instant-on/snapshot path makes the cold tier fast too
# (separation collapses but provisioning is genuinely fast). Both bounds
# env-tunable for recalibration.
#
# Evaluated-and-dropped (#6743): rebasing per-claim latency onto the asbx#761
# `webhook-first-observed-at` server stamp (to remove the serial-submission
# confound at the source). Dropped for two reasons — (1) it is not cleanly
# implementable: bound_at is a client monotonic clock while the webhook stamp is
# server wall-clock, and the two cannot be subtracted; the Ready-condition
# wall-clock stamp is only second-granular, too coarse for sub-second warm binds.
# (2) It buys ~nothing for the SEPARATION ratio anyway: create_times are recorded
# immediately AFTER each create() returns, so a claim's own POST tail is already
# excluded from its create->bind span; the residual confound is reconcile-queue
# pressure, which p50-vs-p50 already absorbs.
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

# Diagnostic-only node-count sampler (hb#319). The scenario's 1-3 node
# autoscale ceiling (ephemeral CI cluster) may not fit a 30-replica/40-claim
# override — a mid-burst scale-up (VM boot + gVisor init + kubelet join) could
# land directly in the observed bind-latency range and smear warm/cold
# together. This can't be confirmed post-hoc (the ephemeral cluster is torn
# down every fire with no retained autoscaler event history), so sample node
# count on a background thread through the pool-warm + claim-burst windows and
# log the series — best-effort, never affects PASS/FAIL or published metrics.
_NODE_SAMPLE_INTERVAL_S = _opt_float("WARMPOOL_COLD_START_NODE_SAMPLE_INTERVAL_S") or 3.0

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

# hb#411: the finally-block cleanup can run inside the sub-92s `a4-hb-refresh@`
# IAM-strike window (hold-hb-refresh-iam.sh documents this as an accepted
# residual gap), where every delete 403s. A single best-effort pass then leaks
# the pool + all member sandboxes on the SHARED, persistent cluster, backing
# real billed nodes. Retry the whole remaining object set together (one shared
# backoff rides out the single IAM window regardless of object count — retrying
# 40 claims independently would take ~1h) long enough to outlast that window.
# Gaps between MAX_ATTEMPTS passes: 4,8,16,32,32,32 = 124s, comfortably past the
# documented sub-92s strike. Env-tunable for recalibration. Only paid on the
# failure path — a clean cleanup deletes on the first pass and never sleeps.
_CLEANUP_MAX_ATTEMPTS = int(
    os.environ.get("WARMPOOL_COLD_START_CLEANUP_MAX_ATTEMPTS", "7")
)
_CLEANUP_BACKOFF_BASE_S = float(
    os.environ.get("WARMPOOL_COLD_START_CLEANUP_BACKOFF_BASE_S", "4.0")
)
_CLEANUP_BACKOFF_CAP_S = float(
    os.environ.get("WARMPOOL_COLD_START_CLEANUP_BACKOFF_CAP_S", "32.0")
)

# hb#379: a bare `readyReplicas >= target_ready` gate certifies "warm" on a
# SINGLE instantaneous poll — indistinguishable from a momentary peak that is
# already draining. Starting the claim burst against a pool mid-drain pushes
# some nominally-warm claims toward cold-like bind latency, closing the
# warm/cold separation margin the scenario's PASS/FAIL gate depends on (the
# hb#379 finding: separation_ratio landed at 1.0246 on canonical n=30, just
# under the 1.8x gate). Require this many CONSECUTIVE 1s polls at/above target
# before declaring the pool warm. Default 3 (~2s minimum hold) is deliberately
# small — it rejects a one-tick flicker without meaningfully eating into the
# 180s warmup budget. Env-tunable for recalibration. Only applied when
# _POOL_REPLICAS > 0 — the POOL_REPLICAS=0 cold-baseline producer must keep
# short-circuiting on the very first poll (test_pool_replicas_zero_is_a_valid_cold_burst).
_WARMUP_STABILITY_POLLS = int(
    os.environ.get("WARMPOOL_COLD_START_WARMUP_STABILITY_POLLS", "3")
)

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


def _sample_node_count(core_v1) -> int:
    """Best-effort node count; -1 on any failure (never raises — diagnostic only)."""
    try:
        return len((core_v1.list_node() or {}).items or [])
    except Exception as e:  # noqa: BLE001 — diagnostic sampler must never break the run
        log.warning("node-count sample failed: %s", e)
        return -1


def _run_node_count_sampler(core_v1, stop_event, samples: list[tuple[float, int]],
                             interval_s: float) -> None:
    """Loop sampling (monotonic-ts, node_count) into `samples` until stop_event fires.

    Runs in its own daemon thread, started before the pool-warm wait and
    stopped in `run()`'s finally so it always covers pool-warm + claim-burst,
    including the exception path. See hb#319 (module-level comment).
    """
    t_start = time.monotonic()
    while not stop_event.is_set():
        samples.append((time.monotonic() - t_start, _sample_node_count(core_v1)))
        stop_event.wait(interval_s)


def _sample_pool_ready(custom, pool_name: str) -> int:
    """Best-effort SandboxWarmPool.status.readyReplicas; -1 on any failure.

    Diagnostic sampler — never raises (mirrors _sample_node_count).
    """
    group, version, plural = _SWP_GVR
    try:
        obj = custom.get_namespaced_custom_object(
            group=group, version=version, namespace=_NAMESPACE,
            plural=plural, name=pool_name,
        )
        status = (obj or {}).get("status") or {}
        return int(status.get("readyReplicas") or 0)
    except Exception as e:  # noqa: BLE001 — diagnostic sampler must never break the run
        log.warning("pool-ready sample failed: %s", e)
        return -1


def _run_pool_ready_sampler(custom, pool_name: str, stop_event,
                             samples: list[tuple[float, int]],
                             interval_s: float) -> None:
    """Loop sampling (monotonic-ts, readyReplicas) into `samples` until stop_event fires.

    hb#379: `_wait_for_pool_warm` proves the pool crossed target_ready ONCE, at the
    instant the gate polls succeed — it says nothing about whether the pool SUSTAINS
    that ready count once the claim burst starts consuming (and the controller starts
    replenishing) pool members. A published warm-tier bind_p95 statistically
    indistinguishable from the fully-cold bind_p95 (hb#379 finding) is consistent with
    either a hard capacity wall (readyReplicas never really holds at target on this
    node shape) or a fast post-gate drain (readyReplicas holds at the gate instant then
    collapses the moment claims fire). Sampling readyReplicas on its own background
    thread through the pool-warm + claim-burst windows — same shape as the hb#319
    node-count sampler, started/stopped alongside it — makes that distinction directly
    visible in the fire log instead of inferred post-hoc from smeared bind latencies.
    Diagnostic-only: never affects PASS/FAIL or published metrics.
    """
    t_start = time.monotonic()
    while not stop_event.is_set():
        samples.append((time.monotonic() - t_start, _sample_pool_ready(custom, pool_name)))
        stop_event.wait(interval_s)


def _wait_for_pool_warm(
    custom, *, pool_name: str, target_ready: int, timeout_s: int,
    stability_polls: int = 1,
) -> dict:
    """Poll WarmPool until status.readyReplicas >= target_ready, or raise.

    hb#379: a single instantaneous poll at/above target_ready is indistinguishable
    from a momentary peak that is already draining. When `stability_polls > 1`,
    require that many CONSECUTIVE 1s polls at/above target_ready before declaring
    the pool warm — any poll that drops below target_ready resets the streak.
    """
    group, version, plural = _SWP_GVR
    deadline = time.monotonic() + timeout_s
    last_status: object = "<no-status>"
    consecutive = 0
    while time.monotonic() < deadline:
        obj = custom.get_namespaced_custom_object(
            group=group, version=version, namespace=_NAMESPACE,
            plural=plural, name=pool_name,
        )
        status = (obj or {}).get("status") or {}
        last_status = status
        ready = int(status.get("readyReplicas") or 0)
        if ready >= target_ready:
            consecutive += 1
            if consecutive >= stability_polls:
                return obj
        else:
            consecutive = 0
        time.sleep(1.0)
    raise RuntimeError(
        f"SandboxWarmPool {pool_name} did not sustain readyReplicas>={target_ready} "
        f"for {stability_polls} consecutive poll(s) within {timeout_s}s "
        f"(last status={last_status!r})"
    )


def _prescale_pool_target(
    pool_replicas: int, claim_count: int, headroom: int, ceiling: int = 0,
) -> int:
    """hb#835 lever-2: readyReplicas target to prescale the WarmPool to.

    Never below the nominal `pool_replicas` (this is a burst-headroom lever,
    not a resize-down) -- the caller additionally gates on
    `pool_replicas > 0` (cold-baseline mode never prescales) and on the
    result exceeding `pool_replicas` (a no-op prescale is skipped, not
    patched-to-itself). Pure so it's testable off fixtures like
    `_fill_gate_target` above; deliberately separate from `_gate_target`,
    which stays capped at `min(pool, claims)` per hb#804 and must not move
    when this lever fires.

    hb#843: `ceiling` (0 = disabled) caps the target at a structural
    per-nodepool capacity limit -- e.g. kata's ~42-pod cap versus a naive
    claim_count=40 + headroom=5 = 45 target that can never land. The ceiling
    is clamped to never go below `pool_replicas` itself, so a misconfigured
    ceiling smaller than the nominal pool can't request a resize-down.
    """
    target = max(pool_replicas, claim_count + headroom)
    if ceiling > 0:
        target = min(target, max(ceiling, pool_replicas))
    return target


def _patch_warmpool_replicas(custom, *, pool_name: str, replicas: int) -> None:
    """Merge-patch spec.replicas on an already-created WarmPool.

    hb#835 lever-2: one scalar field, so the client's default merge-patch is
    correct (no array-merge needed) — same shape as suspend_resume.py's
    `_patch_lifecycle`.
    """
    group, version, plural = _SWP_GVR
    custom.patch_namespaced_custom_object(
        group=group, version=version, namespace=_NAMESPACE,
        plural=plural, name=pool_name,
        body={"spec": {"replicas": replicas}},
    )


def _prescale_pool_with_fallback(
    custom, *, pool_name: str, pool_replicas: int, claim_count: int,
    headroom: int, timeout_s: int, stability_polls: int, ceiling: int = 0,
) -> bool:
    """hb#835 lever-2, made fail-safe: prescale ahead of the burst, degrading
    to the already-warm nominal pool instead of crashing the whole scenario
    when the prescale target is unreachable (e.g. a structural per-nodepool
    capacity ceiling smaller than claim_count + headroom — see hb#843).

    No-op (returns False) in cold-baseline mode or when the nominal pool
    already covers the burst + headroom, same gate as the original inline
    block. Otherwise patches spec.replicas up and waits for it to land; on a
    `_wait_for_pool_warm` timeout the WarmPool is left at whatever
    readyReplicas it actually reached (never patched back down — a partial
    prescale is still strictly better burst headroom than the nominal size)
    and this returns True so the caller can disclose the degrade rather than
    silently proceeding as if the full prescale had landed (#4420: a
    downgrade must never be a silent no-op).

    `ceiling` (0 = disabled, hb#843) caps the computed target at a known
    structural per-nodepool capacity limit before the patch is even
    attempted, so a nodepool with a hard ceiling below claim_count +
    headroom prescales to its actual reachable maximum instead of
    guaranteeing a timeout/degrade on every single fire.

    Returns True iff the prescale was attempted AND did not reach its target
    within timeout_s (degraded); False when no prescale was needed, or the
    prescale fully succeeded.
    """
    prescale_target = _prescale_pool_target(
        pool_replicas, claim_count, headroom, ceiling,
    )
    if pool_replicas <= 0 or prescale_target <= pool_replicas:
        return False
    log.info(
        "hb#835 lever-2: prescaling WarmPool %s %d -> %d "
        "(claim_count=%d + headroom=%d) ahead of burst",
        pool_name, pool_replicas, prescale_target, claim_count, headroom,
    )
    _patch_warmpool_replicas(custom, pool_name=pool_name, replicas=prescale_target)
    try:
        _wait_for_pool_warm(
            custom, pool_name=pool_name,
            target_ready=prescale_target, timeout_s=timeout_s,
            stability_polls=stability_polls,
        )
    except RuntimeError as exc:
        log.warning(
            "hb#835 lever-2: prescale to %d did not land within %ds (%s); "
            "firing the burst against whatever readyReplicas the pool "
            "actually reached instead of crashing the scenario — degrade "
            "will be disclosed via sla_metrics['lever2_prescale_degraded']",
            prescale_target, timeout_s, exc,
        )
        return True
    log.info("hb#835 lever-2: pool prescaled to readyReplicas=%d", prescale_target)
    return False


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


def _snapshot_prewarmed_sandboxes(custom) -> set[str] | None:
    """Snapshot the set of Sandbox names that EXIST before the claim burst.

    These are the pool's genuinely pre-warmed sandboxes (hb#450). A claim that
    binds to a sandbox in this set adopted a pre-warmed pod = genuine warm hit; a
    claim that binds to a sandbox ABSENT from this set was served by a replacement
    the controller created DURING the burst (a depletion-cold blend) and must be
    excluded from the warm tier so warm p95/TTFE measures what the pool actually
    pre-warmed. A name-set snapshot is used rather than a creationTimestamp
    comparison on purpose — Kubernetes `metadata.creationTimestamp` has 1-second
    resolution, so a replacement created in the same wall-second as burst-start
    could alias as pre-warmed; sandbox NAMES are unique and never reused, so the
    snapshot is an exact discriminator with no boundary ambiguity.

    Called immediately before the firing loop, so it captures exactly the warmed
    set the burst will consume (`_wait_for_pool_warm` has already blocked until
    readyReplicas == target, so the pool is at full pre-warm here).

    Returns the name set, or None on any read failure — the caller then falls
    back to rank-only classification (loud-logged). The warm/cold inversion
    tripwire (`_warm_cold_inversion_caveat`) still guards the published trust
    surface in the fallback case, so a contaminated run never publishes silently.
    """
    group, version, plural = _SBX_GVR
    try:
        resp = custom.list_namespaced_custom_object(
            group=group, version=version, namespace=_NAMESPACE, plural=plural,
        )
    except Exception as e:  # noqa: BLE001 — provenance gating is best-effort
        log.warning(
            "hb#450 provenance: pre-burst Sandbox list failed (%s); warm-tier "
            "classification falls back to rank-only", e,
        )
        return None
    names = {
        (item.get("metadata") or {}).get("name")
        for item in (resp or {}).get("items", [])
    }
    names.discard(None)
    return names


def _classify_latencies(
    latencies: dict[str, float | None],
    *,
    pool_replicas: int,
    abs_ceiling_s: float,
    separation_ratio: float,
    warm_eligible: set[str] | None = None,
) -> tuple[bool, dict]:
    """Separation-based warm-tier gate.

    The warm cluster = the `pool_replicas` fastest completed claims. PASS iff
    that cluster is fast by EITHER measure:
      - absolute   : warm_max < abs_ceiling_s, OR
      - separation : cold_p50 / warm_p50 >= separation_ratio
    Robust to absolute warm-tier drift.

    The separation clause is PERCENTILE-MATCHED — the MEDIAN cold-tier bind over
    the MEDIAN warm-tier bind, not cold_min/warm_max (#6743). min-vs-max let a
    single fast-cold bind (created late in a spread submission burst, so its
    create->bind span is tiny) crater cold_min and collapse the ratio ~20x on a
    run where warm adoption was actually healthy; p50-vs-p50 is robust to that
    outlier AND to submission-cadence queue pressure (which inflates both tiers
    ~equally, leaving their ratio stable). Genuine under-delivery is caught by the
    provenance gate below (fewer than `pool_replicas` genuine pre-warmed hits ->
    FAIL), NOT by the separation clause — the two concerns are decoupled.

    ## Provenance gating (hb#450)

    When `warm_eligible` is supplied, the warm tier may ONLY be drawn from claims
    in that set — the claims that adopted a genuinely PRE-WARMED Sandbox (one that
    existed before the claim burst started). The origin bug: the upstream
    warm-pool reconciler gates refill on a COUNT of extant sandboxes, not on
    readyReplicas, so under a claim burst it stalls replenishment and
    readyReplicas collapses mid-burst (hb#379). A depletion-cold blend — a claim
    served by a replacement Sandbox CREATED DURING the burst — then binds on the
    cold path but can rank fast enough to land in the top `pool_replicas`,
    contaminating warm p95/TTFE with cold latencies (the 1.42s-vs-10.81s
    phantom-regression coin-flip). Excluding those blends from the warm candidate
    pool makes warm p95/TTFE describe what the pool ACTUALLY pre-warmed, not a
    warm+cold blend. Fewer than `pool_replicas` genuine pre-warmed hits ->
    under-delivery FAIL (honest: the pool did not deliver a full warm tier). The
    excluded blends still form the cold tier the separation gate measures against.

    `warm_eligible is None` (the default) preserves the rank-only behavior for the
    cold-baseline path (`pool_replicas <= 0`) and the unit tests, and is also the
    graceful fallback when the pre-burst Sandbox snapshot could not be read.

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

    # Warm-tier candidate pool. Default (warm_eligible None): rank-only — every
    # completed claim is a candidate. Provenance-gated (hb#450): only claims that
    # adopted a pre-warmed Sandbox are candidates, so a depletion-cold blend can
    # never enter the warm tier even if it bound fast.
    if warm_eligible is None:
        warm_candidate_pairs = completed_pairs
        warm_eligible_count = None
    else:
        warm_candidate_pairs = [
            (v, k) for v, k in completed_pairs if k in warm_eligible
        ]
        warm_eligible_count = len(warm_candidate_pairs)

    # A diagnostic fire may deliberately size the pool AT OR ABOVE the total
    # claims fired (pool_replicas > len(latencies)) to prove pool health
    # independent of an oversubscribed claim burst — the opposite of the
    # committed design's oversubscription (pool_replicas < claim_count). Cap
    # the warm-tier TARGET at the claims actually fired so that regime is
    # measurable from its own genuine-hit population instead of unconditionally
    # reading as under-delivery merely because the configured pool outsizes the
    # burst. Cold-baseline mode (pool_replicas <= 0) is untouched — its target
    # stays exactly pool_replicas so the negative-index neutral-cold semantics
    # below are preserved.
    warm_target = min(pool_replicas, len(latencies)) if pool_replicas > 0 else pool_replicas

    breakdown = {
        "warm_max_s": None,
        "warm_names": [],
        "next_fastest_s": None,
        "separation_observed": None,
        "warm_p50_s": None,
        "cold_p50_s": None,
        "cold_path_min_s": None,
        "cold_path_max_s": None,
        "absolute_ok": False,
        "separation_ok": False,
        "timeouts": timeouts,
        "completed_count": len(completed),
        "warm_eligible_count": warm_eligible_count,
        "all_latencies_s": completed,
        "warm_target": warm_target,
    }

    # Need a full warm cluster to even evaluate the gate. Rank-only: fewer
    # completed claims than the (possibly capped) target (under-delivery or
    # timeouts), or zero claims fired at all despite pool_replicas > 0.
    # Provenance-gated: fewer GENUINE pre-warmed hits than the target — a
    # depletion collapse can leave <warm_target pre-warmed adoptions even when
    # all claims eventually bind (cold).
    if pool_replicas > 0 and warm_target == 0:
        return False, breakdown
    if len(warm_candidate_pairs) < warm_target:
        return False, breakdown

    warm_pairs = warm_candidate_pairs[:warm_target]
    # Index the boundary element directly (NOT warm_pairs[-1]) so the
    # cold-baseline mode (pool_replicas == 0) preserves its historical semantics:
    # warm_candidate_pairs[-1] is the slowest completed claim, giving warm_max
    # under the absolute ceiling -> the neutral cold PASS record. warm_pairs would
    # be the empty slice [:0] there and crash on [-1]. For pool_replicas > 0 this
    # is exactly warm_pairs[-1][0] (the warm-tier boundary, capped at warm_target).
    warm_max = warm_candidate_pairs[warm_target - 1][0]
    # The gate's warm tier = the pool_replicas fastest-binding candidate claims.
    # Publish the member NAMES so the emit path scopes the TTFE histogram to
    # EXACTLY this set (never a re-derived sort). Same ordering as warm_max, so
    # warm_max == latencies[warm_names[-1]] by construction.
    warm_names = [k for _, k in warm_pairs]
    warm_set = set(warm_names)
    breakdown["warm_max_s"] = warm_max
    breakdown["warm_names"] = warm_names

    # Cold tier (separation denominator) = every completed claim NOT in the warm
    # set, fastest first. Rank-only this is completed[pool_replicas:] exactly;
    # under provenance gating it also folds in the excluded cold blends.
    remainder = [v for v, k in completed_pairs if k not in warm_set]
    cold_path_min = remainder[0] if remainder else None
    cold_path_max = remainder[-1] if remainder else None
    breakdown["next_fastest_s"] = cold_path_min
    breakdown["cold_path_min_s"] = cold_path_min
    breakdown["cold_path_max_s"] = cold_path_max

    # Percentile-matched separation (#6743): MEDIAN cold bind vs MEDIAN warm bind.
    # warm_pairs is empty in cold-baseline mode (pool_replicas == 0), so warm_p50
    # is None there and the separation clause is skipped (only the absolute clause
    # applies) — same neutral-cold behavior the prior min/max form had.
    warm_latencies = [v for v, _ in warm_pairs]
    warm_p50 = statistics.median(warm_latencies) if warm_latencies else None
    cold_p50 = statistics.median(remainder) if remainder else None
    breakdown["warm_p50_s"] = warm_p50
    breakdown["cold_p50_s"] = cold_p50

    absolute_ok = warm_max < abs_ceiling_s
    if remainder and warm_p50 is not None and warm_p50 > 0:
        separation_observed = cold_p50 / warm_p50
        separation_ok = separation_observed >= separation_ratio
    else:
        # No cold tier to separate from (claim_count == pool_replicas), or
        # cold-baseline mode (no warm tier): only the absolute clause applies.
        separation_observed = None
        separation_ok = False

    breakdown["separation_observed"] = separation_observed
    breakdown["absolute_ok"] = absolute_ok
    breakdown["separation_ok"] = separation_ok

    return (absolute_ok or separation_ok), breakdown


def _add_gate_diagnostic_metrics(
    sla_metrics: dict,
    breakdown: dict,
    *,
    warm_max: float | None,
    pool_replicas: int,
) -> dict:
    """Mutate + return `sla_metrics` with numeric hb#379 gate-diagnostic keys.

    The excerpt string that names WHY the gate passed/failed (warm_max /
    cold_path_min / warm_p50 / cold_p50 / separation) is deliberately NEVER
    persisted (run.py: `del excerpt`, the raw-failure_excerpt public-safety
    rule), so a committed FAIL row previously carried no way to tell "absolute
    ceiling missed" from "separation ratio missed" or by how much. These pure
    numbers close that gap without touching the excerpt-scrubbing rule — the
    p50-vs-p50 pair (warm_p50_ms / cold_p50_ms) are the separation ratio's
    inputs, and warm_max_ms / cold_min_ms are retained for the absolute clause
    and longitudinal continuity. Numeric-only keys
    need no results_schema change — `_coerce_sla_metrics` auto-passes any
    finite-number key by shape, not by name allow-list.

    No-ops (returns `sla_metrics` unchanged) when there is no warm tier to
    describe: pool_replicas == 0 (cold-baseline mode, no gate applies),
    sla_metrics isn't a dict, or warm_max is None (under-delivery — the gate
    already emits its own honest-FAIL triple with empty sla_metrics).

    The cold tier can be legitimately EMPTY (every completed claim landed in the
    warm set — e.g. a pool-readiness dip pushed every claim into the during-dip/
    at-supply bucket, leaving no true-cold sample) or the warm p50 can be
    degenerate (<= 0, an unmeasurable separation denominator). Both are real,
    documented conditions, not defects. But the guard-then-fill idiom
    (AGENTS.md's "Transition guards on trust surfaces", #4420) forbids silently
    DROPPING a key a committed prior row carried — harness/run.py's
    check_cell_downgrade compares raw key membership, so an omitted key reads
    as an undifferentiated "downgrade" even when the omission is legitimate.
    So cold_min_ms / cold_p50_ms / separation_ratio are ALWAYS emitted (never
    omitted): explicit `None` plus a closed-set `warmpool_gate_cold_absent_reason`
    when unpopulated, so the guard can tell "re-measured to null, legitimately"
    from "silently regressed".
    """
    if pool_replicas <= 0 or not isinstance(sla_metrics, dict) or warm_max is None:
        return sla_metrics
    sla_metrics["warmpool_gate_warm_max_ms"] = warm_max * 1000.0
    # p50-vs-p50 separation diagnostics (#6743): the two medians the separation
    # ratio is now computed from. Emitted alongside the retained min/max keys so a
    # committed FAIL row shows both the new ratio's inputs and the absolute clause's
    # warm_max. warm_p50_ms is guaranteed present whenever warm_max is (warm_pairs
    # is non-empty by construction once warm_max is computed) — no absent-reason
    # needed for this key.
    if breakdown.get("warm_p50_s") is not None:
        sla_metrics["warmpool_gate_warm_p50_ms"] = breakdown["warm_p50_s"] * 1000.0

    cold_min_s = breakdown["cold_path_min_s"]
    cold_p50_s = breakdown["cold_p50_s"]
    separation = breakdown["separation_observed"]
    if cold_min_s is None:
        # remainder is empty: no true-cold-tier claim exists to measure at all.
        sla_metrics["warmpool_gate_cold_min_ms"] = None
        sla_metrics["warmpool_gate_cold_p50_ms"] = None
        sla_metrics["warmpool_gate_separation_ratio"] = None
        sla_metrics["warmpool_gate_cold_absent_reason"] = "no_true_cold_bucket_claims"
    else:
        sla_metrics["warmpool_gate_cold_min_ms"] = cold_min_s * 1000.0
        sla_metrics["warmpool_gate_cold_p50_ms"] = cold_p50_s * 1000.0
        if separation is not None:
            sla_metrics["warmpool_gate_separation_ratio"] = separation
        else:
            # cold tier is real but warm_p50_s <= 0 — a degenerate (unmeasurable)
            # separation denominator. cold_min/cold_p50 stay real; only the ratio nulls.
            sla_metrics["warmpool_gate_separation_ratio"] = None
            sla_metrics["warmpool_gate_cold_absent_reason"] = "degenerate_warm_p50"
    return sla_metrics


def _min_ready_during_burst(
    samples: list[tuple[float, int]],
    sampler_t0: float,
    burst_start_abs: float,
) -> int | None:
    """Return the minimum readyReplicas held from burst start onward, or None.

    hb#379: `_wait_for_pool_warm` proves the pool crossed target_ready ONCE, at
    the gate-poll instant — it says nothing about whether readyReplicas is
    SUSTAINED once the claim burst starts consuming (and the controller starts
    replenishing) pool members. `samples` is the raw (rel_t, readyReplicas)
    series from `_run_pool_ready_sampler`, `rel_t` relative to `sampler_t0`.
    Filtering to `sampler_t0 + rel_t >= burst_start_abs` drops the pre-burst
    warm-up window on purpose — including it would dilute or mask the churn
    signal this metric exists to capture. A failed poll reads -1 (see
    `_sample_pool_ready`) and is excluded, same as the diagnostic log line.

    Returns None when there are no valid in-burst samples (e.g. the sampler
    never got a successful read once the burst started).
    """
    ready_during_burst = [
        ready
        for rel_t, ready in samples
        if ready >= 0 and (sampler_t0 + rel_t) >= burst_start_abs
    ]
    return min(ready_during_burst) if ready_during_burst else None


def _ready_dip_duration_s(
    samples: list[tuple[float, int]],
    sampler_t0: float,
    burst_start_abs: float,
    target_ready: int,
) -> float | None:
    """Cumulative wall-clock seconds readyReplicas held BELOW `target_ready` during the burst.

    hb#835 lever-3: `_min_ready_during_burst` above answers "how deep did the dip
    go" but not "how long did it last" — a 1-sample instantaneous blip to
    readyReplicas=0 and a dip that HOLDS at 0 for 30s both read identically as
    `min_ready=0`, yet only the latter is consistent with a sustained capacity
    wall (vs. a single missed poll or a momentary reconcile lag). This walks the
    same in-burst-and-valid sample series `_min_ready_during_burst` uses (same
    filtering: `ready >= 0`, `sampler_t0 + rel_t >= burst_start_abs`) and sums
    the wall-clock gap between each CONSECUTIVE pair of in-burst samples whose
    LEADING sample's readyReplicas was below target — a left-Riemann-style step
    approximation (the reading holds from when it was observed until the next
    poll). This deliberately EXCLUDES the trailing interval after the last
    sample (unbounded — the sampler may still be running when this is computed,
    per `_min_ready_during_burst`'s own snapshot-via-list(...) comment), so the
    true dip duration is >= the returned value, never overstated.

    Returns None when there are fewer than 2 valid in-burst samples (can't form
    an interval), same "insufficient evidence" contract as `_min_ready_during_burst`.
    """
    in_burst = sorted(
        (sampler_t0 + rel_t, ready)
        for rel_t, ready in samples
        if ready >= 0 and (sampler_t0 + rel_t) >= burst_start_abs
    )
    if len(in_burst) < 2:
        return None
    duration = 0.0
    for (t0, ready0), (t1, _ready1) in zip(in_burst, in_burst[1:]):
        if ready0 < target_ready:
            duration += t1 - t0
    return duration


def _ready_at_or_before(
    samples: list[tuple[float, int]], sampler_t0: float, at_abs: float,
) -> int | None:
    """Most recent valid readyReplicas reading at-or-before `at_abs`, or None.

    Helper for `_ttfe_by_dip_state`: to bucket a claim's TTFE by "was the pool
    dipped at the moment this claim entered the queue", we need the sampler's
    last-known readyReplicas value AS OF that claim's own create time — not the
    burst-wide min, and not a reading from AFTER the claim was already queued.
    Excludes failed polls (ready == -1), same convention as the two functions
    above. Returns None when no valid sample exists at-or-before `at_abs` (e.g.
    the claim created before the sampler's first successful poll) — the caller
    then leaves that claim unbucketed rather than guessing.
    """
    candidates = [
        (sampler_t0 + rel_t, ready)
        for rel_t, ready in samples
        if ready >= 0 and (sampler_t0 + rel_t) <= at_abs
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda pair: pair[0])[1]


def _ttfe_by_dip_state(
    ttfe_by_name: dict[str, float],
    create_times: dict[str, float],
    samples: list[tuple[float, int]],
    sampler_t0: float,
    target_ready: int,
) -> dict:
    """Split per-claim TTFE(ms) into during-dip vs. at-full-supply buckets.

    hb#835 lever-3's second half: `_ready_dip_duration_s` above establishes
    THAT a dip happened and for how long, but says nothing about whether claims
    caught in the dip actually paid a latency cost — the dip and the TTFE
    degradation could be coincidental (both caused by a third factor) rather
    than causal. This buckets each claim's own measured TTFE by the pool's
    readyReplicas state AT THAT CLAIM'S OWN CREATE TIME (not bind time — bind
    time is the outcome we're trying to explain, so bucketing by it would be
    circular): a claim created while readyReplicas < target_ready enters
    `during_dip`, ready >= target_ready enters `at_full_supply`. A claim
    missing a create time, a TTFE sample, or a readyReplicas reading at its
    create time is silently excluded from both buckets (insufficient evidence
    for that one claim, not a reason to fail the whole computation).

    Returns {"during_dip": summary_or_None, "at_full_supply": summary_or_None}
    where each summary is {"n": int, "median_ms": float} or None when its
    bucket is empty. Two disjoint buckets rather than a correlation
    coefficient, on purpose: a median-vs-median comparison is the honest,
    directly-readable shape for a single-fire disclosure — a real correlation
    coefficient wants many fires' worth of (dip-state, ttfe) pairs to be
    statistically meaningful, which is future work, not this PR's scope.
    """
    during_dip: list[float] = []
    at_full_supply: list[float] = []
    for name, ttfe_ms in ttfe_by_name.items():
        create_t = create_times.get(name)
        if create_t is None or ttfe_ms is None:
            continue
        ready = _ready_at_or_before(samples, sampler_t0, create_t)
        if ready is None:
            continue
        (during_dip if ready < target_ready else at_full_supply).append(ttfe_ms)

    def _summarize(vals: list[float]) -> dict | None:
        if not vals:
            return None
        return {"n": len(vals), "median_ms": statistics.median(vals)}

    return {
        "during_dip": _summarize(during_dip),
        "at_full_supply": _summarize(at_full_supply),
    }


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
    eligible_n = breakdown.get("warm_eligible_count")
    # Name the shortfall against warm_target (the pool size capped at claims
    # actually fired, per _classify_latencies), not the raw pool_replicas
    # config — a diagnostic fire may size the pool ABOVE claim_count, in
    # which case "eligible_n/pool_replicas" would understate the achieved
    # fraction (e.g. 40/40 genuine hits reads as "40/45", falsely implying a
    # shortfall relative to the actually-fireable target).
    warm_target = breakdown["warm_target"]
    if eligible_n is not None and eligible_n < warm_target:
        # Provenance-gated shortfall (hb#450): claims bound, but fewer than
        # warm_target adopted a GENUINELY pre-warmed Sandbox — the rest were
        # depletion-cold blends served by replacements created during the burst.
        # Name the genuine-hit count so the FAIL row isn't misread as a bind
        # failure (completed_n may be >= warm_target here).
        excerpt = (
            f"WarmPool under-delivered warm slots: only {eligible_n}/{warm_target} "
            f"bound claims adopted a pre-warmed sandbox (genuine warm hits); "
            f"{completed_n} claims bound in total (claims fired={claim_count}, "
            f"pool replicas={pool_replicas}), the rest cold blends from replacements "
            f"created mid-burst. No full warm cluster to measure TTFE against — "
            f"controller-side warm-pool candidate. "
            f"All latencies (s, sorted): [{all_lat_str}]. "
            f"Timeouts: {breakdown['timeouts']!r}."
        )
    else:
        excerpt = (
            f"WarmPool under-delivered warm slots: only {completed_n}/{warm_target} "
            f"claims bound into the warm tier (claims fired={claim_count}, pool "
            f"replicas={pool_replicas}). No full warm cluster to measure TTFE "
            f"against — controller-side warm-pool candidate. "
            f"All latencies (s, sorted): [{all_lat_str}]. "
            f"Timeouts: {breakdown['timeouts']!r}."
        )
    return ("FAIL", excerpt, {})


def _cleanup(
    custom, *, claim_names: list[str], pool_name: str, template_name: str,
    max_attempts: int | None = None, backoff_base_s: float | None = None,
    backoff_cap_s: float | None = None, sleep=time.sleep,
) -> list[str]:
    """Delete all claims, then pool, then template — retrying the batch to ride
    out a transient IAM strike (hb#411).

    Retries the WHOLE remaining object set together across up to `max_attempts`
    passes with exponential backoff (capped), so the shared IAM-strike window is
    waited out ONCE regardless of object count. A 404 counts as deleted (already
    gone). On the happy path every delete succeeds on the first pass and no sleep
    is paid.

    Returns the list of `label/name` descriptors that STILL failed to delete
    after all attempts (empty == fully cleaned). A non-empty return is a real
    leak of billed capacity on the shared, persistent cluster, so it is also
    logged LOUD (ERROR, per the #4420 fail-loud-on-degrade idiom) rather than
    swallowed as a WARNING — a silently-leaked pool is exactly the quiet degrade
    that idiom forbids. `sleep`/`max_attempts`/backoff are injectable for tests.
    """
    from kubernetes.client.exceptions import ApiException
    if max_attempts is None:
        max_attempts = _CLEANUP_MAX_ATTEMPTS
    if backoff_base_s is None:
        backoff_base_s = _CLEANUP_BACKOFF_BASE_S
    if backoff_cap_s is None:
        backoff_cap_s = _CLEANUP_BACKOFF_CAP_S

    pending = [("claim", _CLM_GVR, n) for n in claim_names]
    pending += [
        ("warmpool", _SWP_GVR, pool_name),
        ("template", _TPL_GVR, template_name),
    ]
    for attempt in range(1, max_attempts + 1):
        still: list = []
        for (label, gvr, name) in pending:
            group, version, plural = gvr
            try:
                custom.delete_namespaced_custom_object(
                    group=group, version=version, namespace=_NAMESPACE,
                    plural=plural, name=name,
                )
            except ApiException as e:
                if e.status == 404:
                    continue  # already gone — counts as deleted
                still.append((label, gvr, name))
                log.warning(
                    "cleanup: delete %s %s failed (attempt %d/%d): %s",
                    label, name, attempt, max_attempts, e,
                )
        pending = still
        if not pending:
            break
        if attempt < max_attempts:
            sleep(min(backoff_base_s * 2 ** (attempt - 1), backoff_cap_s))

    leaked = [f"{label}/{name}" for (label, _gvr, name) in pending]
    if leaked:
        log.error(
            "cleanup: LEAKED %d object(s) after %d attempts — these back billed "
            "capacity on the SHARED cluster and need manual/reaper deletion: %s",
            len(leaked), max_attempts, ", ".join(leaked),
        )
    return leaked


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

    # hb#319 diagnostic: sample node count on a background thread through the
    # pool-warm + claim-burst windows, so a mid-burst autoscale event (VM boot
    # + gVisor init + kubelet join) is directly visible in the fire log instead
    # of only inferred post-hoc from smeared bind latencies. Started before the
    # try so it's live for the whole measured window; stopped in `finally` so
    # it always stops, including on the exception path.
    import threading

    _node_stop = threading.Event()
    _node_samples: list[tuple[float, int]] = []
    _node_thread = threading.Thread(
        target=_run_node_count_sampler,
        args=(core_v1, _node_stop, _node_samples, _NODE_SAMPLE_INTERVAL_S),
        daemon=True,
    )
    _node_thread.start()

    # hb#379 diagnostic: sample WarmPool.status.readyReplicas on a background
    # thread through the same pool-warm + claim-burst window as the hb#319
    # node-count sampler above. `_wait_for_pool_warm` only proves the pool
    # crossed target_ready ONCE, at the instant the gate polls succeed — it
    # says nothing about whether the pool SUSTAINS that ready count once the
    # claim burst starts consuming (and the controller starts replenishing)
    # pool members. Continuous sampling makes a hard capacity wall vs. a fast
    # post-gate drain directly visible in the fire log. Diagnostic-only:
    # never affects PASS/FAIL or published metrics.
    _pool_ready_stop = threading.Event()
    _pool_ready_samples: list[tuple[float, int]] = []
    # Captured here (not read back from the thread) so `run()` can convert each
    # sample's thread-relative `rel_t` to an absolute monotonic timestamp
    # comparable to `create_times` below — sub-ms skew from the thread's own
    # `t_start` a few lines later is negligible for a burst-window filter.
    _pool_ready_sampler_t0 = time.monotonic()
    _pool_ready_thread = threading.Thread(
        target=_run_pool_ready_sampler,
        args=(custom, pool_name, _pool_ready_stop, _pool_ready_samples, _NODE_SAMPLE_INTERVAL_S),
        daemon=True,
    )
    _pool_ready_thread.start()

    _gate_target = _fill_gate_target(_POOL_REPLICAS, _CLAIM_COUNT)
    try:
        log.info(
            "waiting for WarmPool %s to reach readyReplicas=%d (window=%ds)",
            pool_name, _gate_target, _WARMUP_TIMEOUT_S,
        )
        _wait_for_pool_warm(
            custom, pool_name=pool_name,
            target_ready=_gate_target, timeout_s=_WARMUP_TIMEOUT_S,
            stability_polls=_WARMUP_STABILITY_POLLS if _POOL_REPLICAS > 0 else 1,
        )
        log.info(
            "pool fully warm (readyReplicas=%d); firing %d claims",
            _gate_target, _CLAIM_COUNT,
        )

        # hb#835 lever-2: prescale the pool ahead of the known burst size so
        # readyReplicas has comfortable headroom over demand throughout the
        # burst, instead of relying on steady-state autoscale to react fast
        # enough mid-burst (the confirmed mechanism behind the readyReplicas
        # floor-drain this issue's lever-3 instrumentation measured). No-op
        # in cold-baseline mode or when the nominal pool already covers the
        # burst + headroom. Deliberately independent of `_gate_target`
        # (which stays capped at min(pool, claims) per hb#804) and of
        # `_POOL_REPLICAS` itself (never reassigned) — the classification
        # and lever-3 dip-detection math below keys off both of those
        # UNCHANGED, so this prescale cannot silently move the gate.
        #
        # hb#843: prescale_target can exceed a structural per-nodepool
        # capacity ceiling (e.g. kata's 2-node cap), in which case the wait
        # below degrades rather than raising — see
        # `_prescale_pool_with_fallback`'s docstring. The degrade is
        # disclosed via `_lever2_degraded` below, never silent.
        _lever2_degraded = _prescale_pool_with_fallback(
            custom, pool_name=pool_name,
            pool_replicas=_POOL_REPLICAS, claim_count=_CLAIM_COUNT,
            headroom=_PRESCALE_HEADROOM, timeout_s=_WARMUP_TIMEOUT_S,
            stability_polls=_WARMUP_STABILITY_POLLS, ceiling=_PRESCALE_CEILING,
        )

        # hb#450 provenance snapshot: capture the pre-warmed Sandbox name set
        # IMMEDIATELY before the burst. A claim adopting one of these is a genuine
        # warm hit; a claim adopting a sandbox absent from this set (a depletion
        # replacement created during the burst) is a cold blend, excluded from the
        # warm tier at classification. Only meaningful with a warm pool; the
        # cold-baseline mode (_POOL_REPLICAS <= 0) has no warm tier to gate.
        prewarmed_sandboxes = (
            _snapshot_prewarmed_sandboxes(custom) if _POOL_REPLICAS > 0 else None
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

        # hb#450: translate the pre-warmed Sandbox snapshot into the per-claim
        # warm-eligible set (claims that adopted a pre-warmed sandbox). None (no
        # snapshot / cold-baseline mode) leaves classification rank-only.
        warm_eligible: set[str] | None = None
        if prewarmed_sandboxes is not None:
            warm_eligible = {
                claim for claim, sbx in sandbox_names.items()
                if sbx in prewarmed_sandboxes
            }
            log.info(
                "hb#450 provenance: %d/%d bound claims adopted a pre-warmed "
                "sandbox (genuine warm hits); %d cold blends excluded from the "
                "warm tier",
                len(warm_eligible), len(sandbox_names),
                len(sandbox_names) - len(warm_eligible),
            )

        passed, breakdown = _classify_latencies(
            latencies, pool_replicas=_POOL_REPLICAS,
            abs_ceiling_s=_ABS_FAST_CEILING_S,
            separation_ratio=_SEPARATION_RATIO,
            warm_eligible=warm_eligible,
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
            # hb#723: stamp the same env-knob self-report on the under-delivery
            # FAIL as on every other exit — an under-delivery FAIL is still a
            # measured (non-pending) row and can itself become the committed
            # baseline a later fire is compared against, so it needs the same
            # actionable provenance.
            under[2]["measured_with"] = {
                "WARMPOOL_COLD_START_POOL_REPLICAS": _POOL_REPLICAS,
                "WARMPOOL_COLD_START_CLAIM_COUNT": _CLAIM_COUNT,
                "WARMPOOL_COLD_START_PRESCALE_HEADROOM": _PRESCALE_HEADROOM,
                "WARMPOOL_COLD_START_PRESCALE_CEILING": _PRESCALE_CEILING,
            }
            if _lever2_degraded:
                under[2]["lever2_prescale_degraded"] = True
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
                # Compare against warm_target (not raw _POOL_REPLICAS): a
                # diagnostic fire may size the pool ABOVE the claim count, in
                # which case the gate-warm set is capped at the claims
                # actually fired (see _classify_latencies), so the emitted
                # set legitimately falls short of the raw pool size.
                assert len(emit_names) == breakdown["warm_target"] and all(
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
                {_SLA_METRIC_KEY: warm_max * 1000.0, "n": breakdown["warm_target"]}
                if warm_max is not None
                else {}
            )
        sla_metrics = _add_gate_diagnostic_metrics(
            sla_metrics, breakdown, warm_max=warm_max, pool_replicas=_POOL_REPLICAS,
        )
        # hb#379: promote the readyReplicas-churn sampler above from
        # diagnostic-log-only to a published metric. No-op in cold-baseline
        # mode (no warm tier to sustain, same precedent as
        # `_add_gate_diagnostic_metrics`); the under-delivery path already
        # returned above (`under is not None`), so `sla_metrics` here is
        # always a dict. Snapshot `_pool_ready_samples` via `list(...)` —
        # the background thread keeps sampling until `finally` stops it, but
        # the burst is already complete by this point, so a few extra
        # post-burst samples in the snapshot don't affect the min().
        if _POOL_REPLICAS > 0:
            min_ready = _min_ready_during_burst(
                list(_pool_ready_samples),
                _pool_ready_sampler_t0,
                min(create_times.values()),
            )
            if min_ready is not None:
                sla_metrics["warmpool_gate_min_ready_during_burst"] = min_ready
            # hb#835 lever-3: dip DURATION (this session's addition to the
            # existing dip DEPTH metric above) plus a TTFE-by-dip-state
            # correlation. Evidence-gathering only, per hb#835's own scope
            # ("investigation + a scoped fix proposal — not asking for an
            # immediate threshold change") — these are disclosure-only
            # sla_metrics keys, not a gate/threshold change.
            dip_duration_s = _ready_dip_duration_s(
                list(_pool_ready_samples),
                _pool_ready_sampler_t0,
                min(create_times.values()),
                _gate_target,
            )
            if dip_duration_s is not None:
                sla_metrics["warmpool_gate_ready_dip_duration_s"] = dip_duration_s
            ttfe_by_name = {
                name: result[0]
                for name, result in ttfe_results.items()
                if result[0] is not None
            }
            dip_state = _ttfe_by_dip_state(
                ttfe_by_name,
                create_times,
                list(_pool_ready_samples),
                _pool_ready_sampler_t0,
                _gate_target,
            )
            # hb#835 lever-3 / #4420 (guard-then-fill): either bucket can be
            # legitimately empty for a given fire (no claim was created while the pool
            # was below target, or none once it reached full supply) — that's a real,
            # documented condition, not a defect. Per the same idiom applied to the
            # cold-tier trio above, ALWAYS emit both keys (never omit): explicit `None`
            # plus a closed-set absent-reason when unpopulated, so
            # check_cell_downgrade's key-membership check can tell "re-measured to
            # null, legitimately" from "silently regressed".
            if dip_state["during_dip"] is not None:
                sla_metrics["warmpool_gate_ttfe_during_dip_median_ms"] = dip_state["during_dip"]["median_ms"]
                sla_metrics["warmpool_gate_ttfe_during_dip_n"] = dip_state["during_dip"]["n"]
            else:
                sla_metrics["warmpool_gate_ttfe_during_dip_median_ms"] = None
                sla_metrics["warmpool_gate_ttfe_during_dip_n"] = None
                sla_metrics["warmpool_gate_ttfe_during_dip_absent_reason"] = "no_dip_observed"
            if dip_state["at_full_supply"] is not None:
                sla_metrics["warmpool_gate_ttfe_at_supply_median_ms"] = dip_state["at_full_supply"]["median_ms"]
                sla_metrics["warmpool_gate_ttfe_at_supply_n"] = dip_state["at_full_supply"]["n"]
            else:
                sla_metrics["warmpool_gate_ttfe_at_supply_median_ms"] = None
                sla_metrics["warmpool_gate_ttfe_at_supply_n"] = None
                sla_metrics["warmpool_gate_ttfe_at_supply_absent_reason"] = "no_full_supply_claims"
        # hb#723: self-report the env knobs that gate which sla_metrics keys
        # this fire emits (pool size flips cold-baseline vs warm-tier mode
        # entirely, changing the key set) so check_cell_downgrade's remediation
        # can name concrete envs instead of pointing at nothing. Shared across
        # all three return branches below (cold-baseline / warm PASS / warm FAIL).
        sla_metrics["measured_with"] = {
            "WARMPOOL_COLD_START_POOL_REPLICAS": _POOL_REPLICAS,
            "WARMPOOL_COLD_START_CLAIM_COUNT": _CLAIM_COUNT,
            "WARMPOOL_COLD_START_PRESCALE_HEADROOM": _PRESCALE_HEADROOM,
            "WARMPOOL_COLD_START_PRESCALE_CEILING": _PRESCALE_CEILING,
        }
        if _lever2_degraded:
            sla_metrics["lever2_prescale_degraded"] = True
        sep = breakdown["separation_observed"]
        sep_str = f"{sep:.2f}x" if sep is not None else "<no-cold-tier>"
        clause = (
            "absolute" if breakdown["absolute_ok"]
            else "separation" if breakdown["separation_ok"]
            else "none"
        )
        # #6743 diagnostic: surface the separation-gate inputs (warm/cold p50,
        # ratio, warm_max) to the build-log stdout on EVERY warm-tier fire. These
        # numbers otherwise reach only sla_metrics (the published results JSON) —
        # a fire that refuses to publish (check_n_regression on a reduced-shape
        # diagnostic sweep) computes them but writes nothing, and the excerpt that
        # names them is del'd in run.py, so before this line a non-publishing fire
        # left no way to read the p50s out of the log. Warm-tier only (skipped in
        # cold-baseline mode, which has no warm p50 or ratio). Diagnostic-only:
        # never affects PASS/FAIL or published metrics.
        if _POOL_REPLICAS > 0:
            def _ms(v: float | None) -> str:
                return f"{v * 1000.0:.1f}" if v is not None else "<n/a>"
            log.info(
                "#6743 gate diagnostics: warm_p50=%sms cold_p50=%sms "
                "separation=%s (>= %sx) warm_max=%sms cold_min=%sms clause=%s",
                _ms(breakdown.get("warm_p50_s")), _ms(breakdown.get("cold_p50_s")),
                sep_str, _SEPARATION_RATIO,
                _ms(breakdown.get("warm_max_s")), _ms(breakdown.get("cold_path_min_s")),
                clause,
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
        # Report the achieved warm-tier size (warm_target, capped at claims
        # actually fired) rather than the raw pool_replicas config value — a
        # diagnostic fire may size the pool ABOVE claim_count, in which case
        # "fastest 45/40" would misreport an impossible fraction.
        warm_target = breakdown["warm_target"]
        if passed:
            return (
                "PASS",
                f"WarmPool provides a distinct fast tier ({clause} clause): "
                f"warm cluster (fastest {warm_target}/{_CLAIM_COUNT}) "
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
            f"{warm_target}/{_CLAIM_COUNT}) max={warm_max_str} is neither "
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
        _node_stop.set()
        _node_thread.join(timeout=_NODE_SAMPLE_INTERVAL_S + 5.0)
        if _node_samples:
            counts = [c for _, c in _node_samples if c >= 0]
            log.info(
                "hb#319 node-count diagnostic: %d samples over %.1fs, "
                "min=%s max=%s series=%s",
                len(_node_samples), _node_samples[-1][0],
                min(counts) if counts else "<all-failed>",
                max(counts) if counts else "<all-failed>",
                [(round(t, 1), c) for t, c in _node_samples],
            )
        _pool_ready_stop.set()
        _pool_ready_thread.join(timeout=_NODE_SAMPLE_INTERVAL_S + 5.0)
        if _pool_ready_samples:
            ready_counts = [c for _, c in _pool_ready_samples if c >= 0]
            log.info(
                "hb#379 pool-ready diagnostic: target=%d, %d samples over "
                "%.1fs, min=%s max=%s series=%s",
                _POOL_REPLICAS,
                len(_pool_ready_samples), _pool_ready_samples[-1][0],
                min(ready_counts) if ready_counts else "<all-failed>",
                max(ready_counts) if ready_counts else "<all-failed>",
                [(round(t, 1), c) for t, c in _pool_ready_samples],
            )
        # hb#835 lever-3: log the dip-duration/TTFE-correlation keys (if any
        # landed in sla_metrics) for local diagnosis. Reads back from
        # sla_metrics rather than recomputing, so a `finally` that runs after
        # an early exception (before sla_metrics or create_times/_gate_target
        # exist) never raises NameError here.
        if "sla_metrics" in locals() and isinstance(sla_metrics, dict):
            dip_keys = {
                k: v for k, v in sla_metrics.items()
                if k.startswith("warmpool_gate_ready_dip_duration")
                or k.startswith("warmpool_gate_ttfe_during_dip")
                or k.startswith("warmpool_gate_ttfe_at_supply")
            }
            if dip_keys:
                log.info("hb#835 lever-3 dip-duration/TTFE diagnostic: %s", dip_keys)
        _cleanup(
            custom, claim_names=claim_names,
            pool_name=pool_name, template_name=template_name,
        )
