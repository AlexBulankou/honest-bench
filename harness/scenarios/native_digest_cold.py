"""Unique-image cold-start benchmark: full cold provision of a fresh Sandbox.

    Setup    : Nothing pre-warmed. No SandboxWarmPool, no SandboxClaim — those
               paths pre-provision a sandbox so a claim binds warm, which is
               exactly the latency this cell isolates AGAINST.
    Action   : Create N (default ONE; NATIVE_DIGEST_COLD_SAMPLES, hb#196) bare
               Sandbox CRs SERIALLY on a configurable image with
               imagePullPolicy=Always (so the kubelet always re-validates the
               image rather than trusting a stale cache). Measure wall-clock
               from each create() return to that Sandbox's first Ready=True
               condition; delete before the next sample.
    Expected : The Sandbox provisions cold — controller reconcile + Pod schedule
               + image pull + container start — and reports Ready. The measured
               cold_start_ms is recorded (no in-scenario SLO gate; the render
               page compares it against targets).
    Why      : Cold start is the worst-case provisioning latency an AI-agent
               platform pays when neither a warm pool nor a snapshot serves the
               request. Quantifying it on a vanilla cluster — separately from the
               warm-pool-hit path (warmpool_cold_start) and the resume path
               (suspend_resume) — gives the honest upper bound and a longitudinal
               record across controller version updates.

## Test shape

1. Create a bare Sandbox CR (minimal single-container podTemplate) directly —
   the base `agents.x-k8s.io` Sandbox kind, NOT a Template/WarmPool/Claim chain.
   At v1beta1 a SandboxClaim binds only through a SandboxWarmPool, and the pool
   pre-warms a sandbox; the direct Sandbox CR is the only provisioning path with
   no pre-warming, so it is the faithful cold path.
2. Record `t0` immediately after create() returns (user-perceived create-call
   return, not loop start).
3. Poll the Sandbox until a `type=Ready, status=True` condition is present;
   record `t1` on first observation.
4. cold_start_ms = (t1 - t0) * 1000.
5. PASS with the measured cold_start_ms. Provisioning failure (never Ready
   within the window) RAISES — see crash posture.
6. Cleanup: delete the Sandbox (controller cascades the underlying Pod).

## On "unique / never-cached image"

A truly cold *layer pull* can only be guaranteed with an image tag/digest the
node has never pulled. We cannot push to a registry from a portable harness, so
the image is env-tunable (`NATIVE_DIGEST_COLD_SANDBOX_IMAGE`) and defaults to a
small public image with imagePullPolicy=Always. With the default on a node that
has the layers cached, the measurement is the cold PROVISION path (controller
reconcile + schedule + manifest re-validation + container start) — the honest
upper bound on a warm-cached node. To measure a genuine cold *pull*, point the
env at a unique / never-cached tag (or a digest the target node has never
pulled); then the same code path also captures full layer-download time. Either
way the number is an honest no-warm-pool cold start; the env override only
widens it to include layer download. Which mode a given run measured is recorded
in provenance as `cold_start_mode` (cold-provision vs cold-pull, #3885) — set by
the runner from `BENCH_NATIVE_DIGEST_COLD_MODE` (conservative default
cold-provision) — so the render page can label which the published cold_start_ms
represents.

## Sample count (NATIVE_DIGEST_COLD_SAMPLES, hb#196) — mode-dependent honesty

The single-sample rationale binds ONLY the cold-PULL mode: a cold pull is cold
exactly once per (node, image) — the first create downloads and caches the
layers, so a second create of the SAME image on the SAME node is warm and a
median of repeated same-image creates would measure caching, not cold pull.
In cold-pull mode the scenario therefore REFUSES N>1 (falls back to 1 with a
log line); the longitudinal record across runs (fresh nodes / fresh tags) is
where that distribution lives.

In cold-PROVISION mode (the conservative default, and what both published cold
cells measured) the layers are already cached BY CONSTRUCTION — the cell
measures controller reconcile + Pod schedule + manifest re-validation +
container start. There is no cache confound: every bare-Sandbox create is an
independent cold provision, so repeated samples are honest and
NATIVE_DIGEST_COLD_SAMPLES=N (default 1 = byte-identical prior behavior) runs
N SERIAL create -> first Ready=True -> delete cycles and emits the
samples-derived p50/p95 + n. Serial, never concurrent — concurrent cold creates
would contend on scheduling, which is a different quantity (burst_create's).
This is the graduation path for the render's TTFE_COMPARABILITY_MIN_N (n>=30
drops the low-N dagger).

## Crash posture

Infrastructure failures (controller unhealthy, CRDs missing, RBAC denied,
Sandbox never Ready within the window) raise — the harness loop classifies a
raised exception as a crash-fail cell. There is no in-scenario SLO threshold, so
this scenario does not return a ("FAIL", ...) outcome of its own: it either
measures a cold start (PASS) or the provision genuinely failed (raise).
"""

from __future__ import annotations

try:  # package context (production: run.py loads harness.scenarios.native_digest_cold)
    from . import runtime_class as rc
    from ._apiversion import sandbox_api_version, sandbox_gvr
    from ._kube import load_cluster_config
    from .. import metrics, ttfe_probe
except ImportError:  # standalone (dependency-free test from the scenarios/ dir)
    import runtime_class as rc
    from _apiversion import sandbox_api_version, sandbox_gvr
    from _kube import load_cluster_config
    import sys as _sys
    import pathlib as _pathlib

    _sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))
    import metrics, ttfe_probe

import logging
import os
import time
import uuid

log = logging.getLogger("sandbox-scenario.native-digest-cold")


_NAMESPACE = os.environ.get("BENCH_NAMESPACE", "default")
_SANDBOX_IMAGE = os.environ.get(
    "NATIVE_DIGEST_COLD_SANDBOX_IMAGE", "busybox:1.36"
)

# Runtime-class pin for the #3942 Core Metrics matrix Kata row. DEFAULT-OFF: with
# the knob unset, apply_runtime_class is a byte-identical no-op so the Sandbox
# manifest is exactly its pre-#3942 shape and this scenario stays the plain
# cold-start cell. Set to "gvisor"/"kata" to pin the cold provision to that
# runtime (runtimeClassName + the runtime's toleration/nodeSelector) so the
# published cold row is honestly attributed to the runtime it ran under. The
# shared runtime_class helper owns the pin + post-measurement verify so this
# scenario, warmpool_cold_start, and suspend_resume pin-and-verify identically.
# (burst_create pins-and-verifies with the same INTENT but via its own inline
# impl, NOT this helper: its WarmPool->Claim object model needs a claim-based
# verify over bound_claim_names, not the helper's direct sandbox_names. Editing
# runtime_class.py does NOT change burst_create — keep the two in step by hand.)
_RUNTIME_CLASS = os.environ.get("NATIVE_DIGEST_COLD_RUNTIME_CLASS", "")

# The cluster's substrate banner (run.py provenance). A gke-sandbox banner asserts
# gVisor isolation, so the consistency guard refuses an unset/non-gVisor runtime on
# it before any cluster call — preventing a runc cold number from publishing under a
# gVisor label. kind/gke make no isolation claim and impose no constraint.
_CLUSTER_SUBSTRATE = os.environ.get("BENCH_CLUSTER_SUBSTRATE", "kind")

# Sample-count knob (hb#196). Default 1 = byte-identical single-sample behavior.
# N>1 runs N SERIAL create->Ready->delete cycles and emits samples-derived
# p50/p95 + n — honest ONLY in cold-provision mode (see module docstring); the
# run()-time guard refuses N>1 in cold-pull mode. Parsed fail-fast: a
# non-integer or <1 value raises at import (crash posture — a mis-set knob must
# never silently publish a differently-shaped cell).
_SAMPLES = int(os.environ.get("NATIVE_DIGEST_COLD_SAMPLES", "1"))
if _SAMPLES < 1:
    raise ValueError(
        f"NATIVE_DIGEST_COLD_SAMPLES must be >= 1, got {_SAMPLES}"
    )

# Which cold mode this run measures (provenance is stamped by the runner from the
# same env, #3885; conservative default cold-provision). Read here ONLY to gate
# the N>1 refusal — in cold-pull mode repeated same-image creates measure
# caching, not cold pull, so the honest sample count is 1.
_COLD_MODE = os.environ.get("BENCH_NATIVE_DIGEST_COLD_MODE", "cold-provision")

# Public benchmark metric key (milliseconds). The cold create→Ready wall-clock
# this scenario measures internally in seconds is emitted via the run() 3-tuple
# as cold_start_ms, converted to milliseconds to match the render schema's
# metric vocabulary. Used only on the LEGACY (TTFE-off) emit path; the TTFE path
# supersedes it with the create->first-instruction histogram.
_SLA_METRIC_KEY = "cold_start_ms"


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


# TTFE Layer-2 exec probe. DEFAULT-OFF.
#
# gated: default-off until the runner ServiceAccount carries pods/exec RBAC and
# the fire path flips it ON in the SAME change that grants the verb. The probe
# (ttfe_probe.probe_first_instruction) collapses an RBAC-denied exec and a
# genuine exec-failure to the same (None, False) — it cannot tell them apart — so
# an ungated default-on would publish a false 0% exec-success before the grant
# lands. Flip-issue: #3944.
_TTFE_EXEC = _env_flag("BENCH_TTFE_EXEC")


# Cold-provision budget. A cold image pull + schedule + container start can take
# 30-120s on a fresh node depending on image size; 240s is a generous ceiling
# that still bounds a hung provision so the cell fails rather than hanging.
_READY_TIMEOUT_S = 240
# Poll interval while timing. Cold start is a coarse measurement (tens of
# seconds), so a 0.25s poll keeps measurement error well under 1% without
# hammering the apiserver.
_POLL_S = 0.25

_SBX_GVR = sandbox_gvr()

_SCENARIO_LABEL = {"honest-bench/scenario": "native-digest-cold"}


def _build_sandbox_manifest(sandbox_name: str) -> dict:
    """Minimal bare Sandbox CR — single container, sleeps, restartPolicy Never.

    imagePullPolicy=Always so the kubelet always re-validates the image (closest
    to cold on a reused tag; a full layer pull when the env points at a unique /
    never-cached tag). Mirrors the upstream hello-world minimum; the controller
    backfills the underlying Pod naming + UID + status.

    When NATIVE_DIGEST_COLD_RUNTIME_CLASS is set, the inner pod_spec is pinned to
    that runtime (runtimeClassName + the runtime's toleration/nodeSelector) via the
    shared runtime_class helper. Default-off: with the knob unset apply_runtime_class
    is a byte-identical no-op, so the manifest is exactly its pre-#3942 shape.
    """
    pod_spec = {
        "containers": [
            {
                "name": "sandbox",
                "image": _SANDBOX_IMAGE,
                "imagePullPolicy": "Always",
                "command": ["sh", "-c", "sleep 600"],
                "resources": rc.container_resources_from_env(_RUNTIME_CLASS),
            },
        ],
        "restartPolicy": "Never",
    }
    rc.apply_runtime_class(pod_spec, _RUNTIME_CLASS)
    return {
        "apiVersion": sandbox_api_version(),
        "kind": "Sandbox",
        "metadata": {
            "name": sandbox_name,
            "namespace": _NAMESPACE,
            "labels": dict(_SCENARIO_LABEL),
        },
        "spec": {
            "podTemplate": {
                "spec": pod_spec,
            },
        },
    }


def _is_ready(status: dict) -> bool:
    """True iff status carries a Ready=True condition.

    A freshly-created (never-suspended) Sandbox's Ready=True fires only once the
    underlying Pod is Ready (the controller's computeReadyCondition with a live
    Pod sets status=True / message "Pod is Ready"), so Ready=True is a faithful
    "cold start complete" signal here — no suspended-vs-running ambiguity, since
    this scenario never suspends the sandbox.
    """
    conds = (status or {}).get("conditions") or []
    return any(
        c.get("type") == "Ready" and c.get("status") == "True"
        for c in conds
    )


def _wait_for_sandbox_ready(custom, *, sandbox_name: str) -> float:
    """Poll the Sandbox until Ready=True; return the monotonic observation time.

    Raises on timeout with the last-seen status so the crash-fail excerpt
    surfaces controller state for diagnosis.
    """
    group, version, plural = _SBX_GVR
    deadline = time.monotonic() + _READY_TIMEOUT_S
    last_status: object = "<no-status>"
    while time.monotonic() < deadline:
        obj = custom.get_namespaced_custom_object(
            group=group, version=version, namespace=_NAMESPACE,
            plural=plural, name=sandbox_name,
        )
        status = (obj or {}).get("status") or {}
        last_status = status
        if _is_ready(status):
            return time.monotonic()
        time.sleep(_POLL_S)
    raise RuntimeError(
        f"Sandbox {sandbox_name} did not reach Ready=True within "
        f"{_READY_TIMEOUT_S}s (last status={last_status!r}) — controller may be "
        f"unhealthy, image pull failing, or CRD schema drifted"
    )


def _cleanup(custom, *, sandbox_name: str) -> None:
    """Best-effort delete of the Sandbox (controller cascades the Pod)."""
    from kubernetes.client.exceptions import ApiException
    group, version, plural = _SBX_GVR
    try:
        custom.delete_namespaced_custom_object(
            group=group, version=version, namespace=_NAMESPACE,
            plural=plural, name=sandbox_name,
        )
    except ApiException as e:
        if e.status != 404:
            log.warning("cleanup: delete sandbox %s failed: %s", sandbox_name, e)


def _effective_samples(requested: int, mode: str) -> int:
    """Sample count actually run, after the cold-pull honesty guard.

    Pure (testable dependency-free): in cold-pull mode N>1 is refused — a cold
    pull is cold exactly once per (node, image), so repeated same-image creates
    on the same node would measure caching, not cold pull. cold-provision has no
    cache confound (layers cached by construction; every create is an
    independent cold provision), so the requested N passes through.
    """
    if mode == "cold-pull" and requested > 1:
        log.warning(
            "NATIVE_DIGEST_COLD_SAMPLES=%d refused in cold-pull mode — a cold "
            "pull is cold exactly once per node+image, repeats would measure "
            "caching; falling back to a single sample",
            requested,
        )
        return 1
    return requested


def _one_cold_sample(
    custom, k8s_client
) -> tuple[str, float, "float | None", "bool | None"]:
    """One full cold cycle: create -> first Ready=True -> (probe) -> delete.

    Returns (sandbox_name, cold_start_s, ttfe_ms, exec_ok); ttfe_ms/exec_ok are
    (None, None) when BENCH_TTFE_EXEC is off. The sandbox is ALWAYS deleted
    before return (per-sample finally), so with N>1 at most one benchmark
    sandbox exists at a time — samples are serial and independent, and a
    mid-loop crash leaks at most the in-flight sandbox (same posture as the
    prior single-sample shape).
    """
    suffix = uuid.uuid4().hex[:8]
    sandbox_name = f"cold-{suffix}"

    log.info(
        "creating bare Sandbox %s (image=%s, pull=Always) for cold-start timing",
        sandbox_name, _SANDBOX_IMAGE,
    )
    custom.create_namespaced_custom_object(
        group=_SBX_GVR[0], version=_SBX_GVR[1], namespace=_NAMESPACE,
        plural=_SBX_GVR[2], body=_build_sandbox_manifest(sandbox_name),
    )
    # Record t0 immediately after create() returns — user-perceived start.
    t0 = time.monotonic()

    try:
        t1 = _wait_for_sandbox_ready(custom, sandbox_name=sandbox_name)
        cold_start_s = t1 - t0
        log.info("Sandbox %s Ready in %.2fs (cold)", sandbox_name, cold_start_s)

        # Runtime read-back (post-measurement, mirrors warmpool_cold_start): verify the
        # cold Sandbox's backing Pod actually scheduled under the pinned runtime before
        # publishing the runtime-labeled cold row. Crash-FAILs on a silent runc fallback.
        # kind/gke skip this (required_runtime_for_substrate -> None — no runtime claim to
        # verify, so the path stays read-free there, preserving the default INERT shape).
        # Runs AFTER the measurement so it never perturbs the measured cold latency. The
        # gate shares the single substrate->runtime source of truth with the consistency
        # guard in run(), which already proved _RUNTIME_CLASS == the required runtime.
        # Per-sample: each sandbox is verified while it exists (pre-delete).
        if rc.required_runtime_for_substrate(_CLUSTER_SUBSTRATE) is not None:
            core_v1 = k8s_client.CoreV1Api()
            verified = rc.verify_bound_pod_runtimes(
                custom, core_v1,
                namespace=_NAMESPACE,
                sandbox_names=[sandbox_name],
                sandbox_gvr=_SBX_GVR,
                expected_runtime_class=_RUNTIME_CLASS,
            )
            log.info(
                "runtime read-back: %d/1 cold sandbox verified under RuntimeClass %r",
                verified, _RUNTIME_CLASS,
            )

        ttfe_ms: "float | None" = None
        exec_ok: "bool | None" = None
        if _TTFE_EXEC:
            core_v1 = k8s_client.CoreV1Api()
            # The bare Sandbox's backing pod is named for the CR (pod name ==
            # sandbox_name); t0 = create return, so ttfe is create->first-
            # instruction-result against the same clock as cold_start_s.
            ttfe_ms, exec_ok = ttfe_probe.probe_first_instruction(
                core_v1,
                pod_name=sandbox_name,
                namespace=_NAMESPACE,
                create_monotonic=t0,
            )
        return (sandbox_name, cold_start_s, ttfe_ms, exec_ok)
    finally:
        _cleanup(custom, sandbox_name=sandbox_name)


def run(scenario_name: str) -> tuple[str, str, dict]:
    """Create N bare Sandboxes cold (serial), time create→Ready, return metrics.

    N = NATIVE_DIGEST_COLD_SAMPLES (default 1 — byte-identical single-sample
    behavior). Each sample is a full create -> first Ready=True -> delete cycle,
    run SERIALLY so samples never contend on scheduling (concurrent cold creates
    measure burst contention — burst_create's quantity, not this cell's). In
    cold-pull mode N>1 is refused (falls back to 1 with a log line): a cold pull
    is cold exactly once per node+image, so repeats would measure caching.

    Returns a 3-tuple (outcome, excerpt, sla_metrics). On success sla_metrics
    carries the samples-derived point: TTFE-on -> ttfe/bind/exec p50+p95 +
    exec_success_rate + n; TTFE-off (legacy) -> cold_start_ms (+ p95 when n>1)
    + n. A provisioning failure raises (crash-fail cell); this scenario has no
    in-scenario SLO gate, so it never returns a ("FAIL", ...) outcome of its own.
    """
    from kubernetes import client as k8s_client

    # Pure, fail-fast (mirrors warmpool_cold_start): a gke-sandbox-labeled result
    # MUST pin the gVisor RuntimeClass, else the published cold row is a runc number
    # under a gVisor banner. Checked before the cluster is touched so the mistake
    # crashes immediately. kind/gke impose no constraint.
    rc.assert_substrate_runtime_consistency(_CLUSTER_SUBSTRATE, _RUNTIME_CLASS)

    n_samples = _effective_samples(_SAMPLES, _COLD_MODE)

    # Portable kubeconfig load (see _kube.load_cluster_config): an explicit
    # KUBECONFIG wins, else in-cluster when running as a pod, else the default
    # kubeconfig.
    load_cluster_config()

    custom = k8s_client.CustomObjectsApi()

    cold_start_ss: list[float] = []
    ttfe_ms_samples: list["float | None"] = []
    exec_oks: list[bool] = []
    last_sandbox_name = ""
    for i in range(n_samples):
        if n_samples > 1:
            log.info("cold sample %d/%d", i + 1, n_samples)
        sandbox_name, cold_start_s, ttfe_ms, exec_ok = _one_cold_sample(
            custom, k8s_client
        )
        last_sandbox_name = sandbox_name
        cold_start_ss.append(cold_start_s)
        if _TTFE_EXEC:
            ttfe_ms_samples.append(ttfe_ms)
            exec_oks.append(bool(exec_ok))

    # Emit-key assembly. Two paths, gated by BENCH_TTFE_EXEC:
    #
    #   TTFE-on  — the create->first-instruction-result distribution
    #     (ttfe_p50_ms/ttfe_p95_ms over the present samples, exec_success_rate
    #     over all attempts, n = attempts), plus the inch-#2 bind/exec
    #     decomposition (per-sample INDEPENDENTLY MEASURED: bind = create->Ready
    #     = the full cold provision; exec = ttfe - bind residual against the
    #     SAME per-sample t0 — Ready precedes the first instruction, so
    #     exec >= 0 by construction; provision is EXPECTED to dominate cold).
    #     This SUPERSEDES cold_start_ms (the doc reports TTFE, not
    #     create->Ready), so the legacy key is dropped on this path.
    #   TTFE-off (legacy) — cold_start_ms (create->Ready, ms) + n. n=1 stays
    #     byte-identical to the prior single-sample emit; n>1 reports the p50 as
    #     cold_start_ms plus a cold_start_p95_ms tail.
    if _TTFE_EXEC:
        bind_ms_samples = [s * 1000.0 for s in cold_start_ss]
        exec_ms_samples = [
            (t - b) if t is not None else None
            for t, b in zip(ttfe_ms_samples, bind_ms_samples)
        ]
        sla_metrics = metrics.multi_sample_ttfe_point(
            ttfe_ms_samples, exec_oks,
            bind_ms_samples=bind_ms_samples,
            exec_ms_samples=exec_ms_samples,
        )
        if n_samples == 1:
            bind_ms = bind_ms_samples[0]
            excerpt = (
                f"Cold provision of bare Sandbox {last_sandbox_name} "
                f"(image={_SANDBOX_IMAGE}, pull=Always, no warm pool): "
                f"create->Ready {cold_start_ss[0]:.2f}s (bind_ms={bind_ms:.0f}); "
                f"TTFE probe exec_ok={exec_oks[0]}, ttfe_ms={ttfe_ms_samples[0]!r}, "
                f"exec_ms={exec_ms_samples[0]!r} (n=1; create->first-instruction-result)."
            )
        else:
            n_exec_ok = sum(1 for ok in exec_oks if ok)
            excerpt = (
                f"Cold provision x{n_samples} serial bare Sandboxes "
                f"(image={_SANDBOX_IMAGE}, pull=Always, no warm pool, "
                f"mode={_COLD_MODE}): exec_ok {n_exec_ok}/{n_samples}; "
                f"ttfe_p50_ms={sla_metrics.get('ttfe_p50_ms')!r}, "
                f"ttfe_p95_ms={sla_metrics.get('ttfe_p95_ms')!r}, "
                f"bind_p50_ms={sla_metrics.get('bind_p50_ms')!r} "
                f"(n={n_samples}; create->first-instruction-result per sample)."
            )
    else:
        if n_samples == 1:
            cold_start_s = cold_start_ss[0]
            sla_metrics = {_SLA_METRIC_KEY: cold_start_s * 1000.0, "n": 1}
            excerpt = (
                f"Cold provision of bare Sandbox {last_sandbox_name} "
                f"(image={_SANDBOX_IMAGE}, pull=Always, no warm pool) reached "
                f"Ready=True in {cold_start_s:.2f}s. "
                f"cold_start_ms={cold_start_s * 1000.0:.0f} "
                f"(n=1; single cold provision — a cold pull is cold once per "
                f"node+image)."
            )
        else:
            cold_ms = [s * 1000.0 for s in cold_start_ss]
            p50 = round(metrics.percentile(cold_ms, 50), 1)
            p95 = round(metrics.percentile(cold_ms, 95), 1)
            sla_metrics = {
                _SLA_METRIC_KEY: p50,
                "cold_start_p95_ms": p95,
                "n": n_samples,
            }
            excerpt = (
                f"Cold provision x{n_samples} serial bare Sandboxes "
                f"(image={_SANDBOX_IMAGE}, pull=Always, no warm pool, "
                f"mode={_COLD_MODE}): create->Ready p50={p50:.0f}ms "
                f"p95={p95:.0f}ms (n={n_samples})."
            )
    return ("PASS", excerpt, sla_metrics)
