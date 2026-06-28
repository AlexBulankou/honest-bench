"""gVisor isolation canary: verify a Sandbox actually runs under the gVisor runtime.

    Setup    : Nothing pre-warmed. A single bare Sandbox CR whose podTemplate
               requests the gVisor RuntimeClass (`runtimeClassName: gvisor`).
    Action   : Create the Sandbox, wait for Ready, then read the backing Pod the
               controller created and assert it was admitted + scheduled under the
               gVisor RuntimeClass (`pod.spec.runtimeClassName == "gvisor"`,
               phase Running).
    Expected : The gVisor RuntimeClass handler accepted the workload and the
               kubelet started the container under `runsc`. A cluster whose nodes
               lack `runsc` (or whose controller silently strips the RuntimeClass)
               never produces a gVisor-bound Ready Pod — which is exactly the
               silent-isolation-drop this canary exists to catch.
    Why      : "Sandboxed" is a spectrum, not a checkbox. The value of a canary is
               detecting when the isolation REQUEST is silently not honored — the
               RuntimeClass dropped, the pod fell back to `runc`, the node had no
               gVisor handler. This cell is the longitudinal isolation badge that
               fails LOUD on that regression instead of a green table that lies.

## Test shape

1. Create a bare Sandbox CR (minimal busybox podTemplate) with
   `spec.podTemplate.spec.runtimeClassName = "gvisor"` (env-tunable). Capture the
   Sandbox `metadata.uid` from the create() response.
2. Wait for the Sandbox's `type=Ready, status=True` condition (raise on timeout —
   a never-Ready gVisor sandbox is an infra/isolation failure, surfaced as a
   crash-fail cell with the last-seen status).
3. Discover the backing Pod by `ownerReferences[].uid == <sandbox uid>` (the
   controller owns the Pod it creates — uid match is convention-independent of
   pod-name shape or label propagation). Poll briefly: the Pod exists by the time
   the Sandbox is Ready, so this resolves immediately on a healthy cluster.
4. PASS iff the backing Pod is found AND `pod.spec.runtimeClassName == "gvisor"`
   AND the Pod phase is Running — the isolation request was honored end-to-end at
   the spec the kubelet acted on.
5. FAIL iff the Sandbox reached Ready but the backing Pod is NOT gVisor-bound
   (silent isolation drop — the canary's whole purpose), or the backing Pod could
   not be located (isolation unverifiable — never a false PASS).
6. Cleanup: delete the Sandbox (controller cascades the Pod).

## Why a badge, not a latency

This cell is an isolation BADGE (is the boundary real?), not a perf measurement,
so `run()` returns no `sla_metrics` — there is no honest number to publish here,
and a fabricated one would violate honest-by-construction. The render page shows
the PASS/FAIL/pending outcome, not a millisecond figure.

## Why read the Pod spec and not just "Ready"

A Ready Sandbox alone does not prove gVisor: a controller that silently stripped
`runtimeClassName` would still reach Ready running under `runc` — a green cell
that lies. Reading the backing Pod's `spec.runtimeClassName` (the value the
kubelet actually acted on) closes that hole without an in-sandbox probe. The
STRONGER canary — exec'ing into the sandbox and asserting the in-kernel gVisor
signature (`dmesg` reports "Starting gVisor...", `/proc/version` carries the
gVisor sentry build) — is the documented next iteration: it requires a pods/exec
grant and a per-sandbox exec round-trip, the same exec-surface tradeoff
warmpool_cold_start defers for literal first-byte-stdout timing. The Pod-spec
read-back is the honest MVP isolation assertion; the in-sandbox kernel signature
strengthens it from "scheduled under runsc" to "running on the gVisor kernel".

## Crash posture

Infrastructure failures (controller unhealthy, CRDs missing, RBAC denied, Sandbox
never Ready within the window) raise — the harness loop classifies a raised
exception as a crash-fail cell. The silent-isolation-drop case is a scenario
outcome, returned as ("FAIL", excerpt, {}); it is a real benchmark result (the
isolation badge is red), not a harness crash.

## Substrate gate

This cell declares `requires_substrate="gke-sandbox"` in scenario_map. On a
substrate without gVisor (vanilla kind — no `runsc`), run.py's gate emits a
`pending (requires-gvisor-runtime)` cell WITHOUT importing this module, so the
published kind artifact renders pending and this body runs only where gVisor is
genuinely available. The defensive runtimeClassName read-back means that even if
this body were somehow driven on a non-gVisor substrate, it FAILs honestly rather
than fabricating a PASS.
"""

from __future__ import annotations

from ._apiversion import sandbox_api_version, sandbox_gvr

import logging
import os
import time
import uuid

log = logging.getLogger("sandbox-scenario.gvisor-canary")


_NAMESPACE = os.environ.get("BENCH_NAMESPACE", "default")
_SANDBOX_IMAGE = os.environ.get("GVISOR_CANARY_SANDBOX_IMAGE", "busybox:1.36")

# The RuntimeClass name the sandbox requests. Default "gvisor" matches the
# GKE-Sandbox / upstream gVisor RuntimeClass convention; env-tunable for clusters
# that name their runsc RuntimeClass differently.
_RUNTIME_CLASS = os.environ.get("GVISOR_CANARY_RUNTIME_CLASS", "gvisor")

# Ready budget. A gVisor sandbox cold-starts slower than runc (sentry boot +
# image pull + container start); 240s bounds a hung provision so the cell fails
# rather than hanging.
_READY_TIMEOUT_S = 240
# Backing-Pod discovery budget after Ready. The Pod exists well before the
# Sandbox is Ready, so this resolves on the first sweep on a healthy cluster; the
# short window only absorbs informer-cache lag.
_POD_DISCOVERY_TIMEOUT_S = 30
_POLL_S = 0.25

_SBX_GVR = sandbox_gvr()

_SCENARIO_LABEL = {"honest-bench/scenario": "gvisor-canary"}


def _build_sandbox_manifest(sandbox_name: str) -> dict:
    """Minimal busybox Sandbox CR pinned to the gVisor RuntimeClass.

    `runtimeClassName` is a standard PodSpec field; setting it on the podTemplate
    is the isolation REQUEST whose end-to-end honoring this canary verifies. The
    scenario label is set on both the Sandbox and the podTemplate so the backing
    Pod is additionally label-discoverable; uid-match is the primary discovery
    path (see run()).
    """
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
                "metadata": {"labels": dict(_SCENARIO_LABEL)},
                "spec": {
                    "runtimeClassName": _RUNTIME_CLASS,
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


def _is_ready(status: dict) -> bool:
    """True iff status carries a Ready=True condition."""
    conds = (status or {}).get("conditions") or []
    return any(
        c.get("type") == "Ready" and c.get("status") == "True"
        for c in conds
    )


def _wait_for_sandbox_ready(custom, *, sandbox_name: str) -> None:
    """Poll the Sandbox until Ready=True; raise on timeout with last status."""
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
            return
        time.sleep(_POLL_S)
    raise RuntimeError(
        f"Sandbox {sandbox_name} did not reach Ready=True within "
        f"{_READY_TIMEOUT_S}s (last status={last_status!r}) — controller may be "
        f"unhealthy, the gVisor RuntimeClass {_RUNTIME_CLASS!r} may be absent on "
        f"the node, or no node provides runsc"
    )


def _find_backing_pod(core, *, sandbox_uid: str):
    """Return the Pod owned by the Sandbox (ownerReferences uid match), or None.

    uid-match is convention-independent — it does not assume a pod-name shape or
    that the controller propagates podTemplate labels. Polls briefly because the
    Pod is already present by the time the Sandbox is Ready; the window only
    absorbs informer-cache lag. The scenario label narrows the list to keep the
    scan cheap on a busy namespace, with a full-namespace fallback if label
    propagation did not occur.
    """
    label_selector = ",".join(f"{k}={v}" for k, v in _SCENARIO_LABEL.items())
    deadline = time.monotonic() + _POD_DISCOVERY_TIMEOUT_S
    while time.monotonic() < deadline:
        for selector in (label_selector, None):
            kwargs = {"namespace": _NAMESPACE}
            if selector:
                kwargs["label_selector"] = selector
            pods = core.list_namespaced_pod(**kwargs)
            for pod in pods.items:
                owners = pod.metadata.owner_references or []
                if any(o.uid == sandbox_uid for o in owners):
                    return pod
        time.sleep(_POLL_S)
    return None


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


def run(scenario_name: str) -> tuple[str, str, dict]:
    """Provision a gVisor-pinned Sandbox, verify it runs under runsc.

    Returns a 3-tuple (outcome, excerpt, sla_metrics). This is an isolation badge,
    so sla_metrics is always {} — there is no honest number to publish. PASS iff
    the backing Pod is gVisor-bound (spec.runtimeClassName == the requested class,
    phase Running); FAIL on a silent isolation drop or an unlocatable backing Pod;
    raise on a provisioning/infra failure (crash-fail cell).
    """
    from kubernetes import client as k8s_client
    from kubernetes import config as k8s_config

    # Portable kubeconfig load: in-cluster when running as a pod, otherwise
    # whatever the runner's KUBECONFIG / default kubeconfig points at.
    try:
        k8s_config.load_incluster_config()
    except k8s_config.ConfigException:
        k8s_config.load_kube_config()

    custom = k8s_client.CustomObjectsApi()
    core = k8s_client.CoreV1Api()

    suffix = uuid.uuid4().hex[:8]
    sandbox_name = f"gvisor-{suffix}"

    log.info(
        "creating Sandbox %s (image=%s, runtimeClassName=%s) for gVisor canary",
        sandbox_name, _SANDBOX_IMAGE, _RUNTIME_CLASS,
    )
    created = custom.create_namespaced_custom_object(
        group=_SBX_GVR[0], version=_SBX_GVR[1], namespace=_NAMESPACE,
        plural=_SBX_GVR[2], body=_build_sandbox_manifest(sandbox_name),
    )
    sandbox_uid = ((created or {}).get("metadata") or {}).get("uid")
    if not sandbox_uid:
        # No uid means the create did not return the persisted object — re-GET
        # rather than guess, so backing-Pod discovery has a real owner uid.
        obj = custom.get_namespaced_custom_object(
            group=_SBX_GVR[0], version=_SBX_GVR[1], namespace=_NAMESPACE,
            plural=_SBX_GVR[2], name=sandbox_name,
        )
        sandbox_uid = ((obj or {}).get("metadata") or {}).get("uid")

    try:
        _wait_for_sandbox_ready(custom, sandbox_name=sandbox_name)
        log.info("Sandbox %s Ready; locating backing Pod by owner uid", sandbox_name)

        pod = _find_backing_pod(core, sandbox_uid=sandbox_uid) if sandbox_uid else None
        if pod is None:
            return (
                "FAIL",
                f"Sandbox {sandbox_name} reached Ready but its backing Pod could "
                f"not be located by owner uid {sandbox_uid!r} within "
                f"{_POD_DISCOVERY_TIMEOUT_S}s — gVisor binding is unverifiable, so "
                f"the isolation badge cannot honestly pass.",
                {},
            )

        pod_rtc = pod.spec.runtime_class_name
        pod_phase = pod.status.phase if pod.status else None
        is_gvisor = pod_rtc == _RUNTIME_CLASS
        is_running = pod_phase == "Running"

        if is_gvisor and is_running:
            return (
                "PASS",
                f"Sandbox {sandbox_name} backing Pod {pod.metadata.name} is bound "
                f"to RuntimeClass {pod_rtc!r} (phase={pod_phase}) — the gVisor "
                f"isolation request was admitted, scheduled, and started under "
                f"runsc end-to-end. (Badge: isolation verified at the Pod spec the "
                f"kubelet acted on; in-sandbox kernel-signature probe is the "
                f"documented next iteration.)",
                {},
            )
        return (
            "FAIL",
            f"Sandbox {sandbox_name} reached Ready but backing Pod "
            f"{pod.metadata.name} is NOT gVisor-bound: "
            f"runtimeClassName={pod_rtc!r} (expected {_RUNTIME_CLASS!r}), "
            f"phase={pod_phase!r}. Silent isolation drop — the workload ran "
            f"outside the requested gVisor boundary; this is the regression the "
            f"canary exists to catch.",
            {},
        )
    finally:
        _cleanup(custom, sandbox_name=sandbox_name)
