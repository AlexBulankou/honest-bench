"""Cross-tenant network isolation: verify a same-tenant-only NetworkPolicy is admitted, binds tenant-A, and excludes tenant-B.

    Setup    : Two bare Sandbox CRs (busybox podTemplate) in one namespace, a
               "tenant-A" and a "tenant-B" sandbox, each carrying a shared scenario
               label plus a distinct tenant label the controller propagates to the
               backing Pod.
    Action   : Create both Sandboxes, wait for Ready, locate each backing Pod, then
               apply an ingress NetworkPolicy that selects tenant-A and allows
               ingress ONLY from tenant-A (`policyTypes: [Ingress]`, a single
               `from` rule keyed on the tenant-A label). Verify the policy was
               admitted with that same-tenant-only shape AND that its `podSelector`
               binds the live tenant-A Pod while NOT binding the live tenant-B Pod.
    Expected : The NetworkPolicy is persisted selecting tenant-A only and admitting
               ingress from tenant-A only — the control-plane declaration of
               "tenant-B cannot reach tenant-A" is in place and correctly targeted.
    Why      : Multi-tenant isolation is the property buyers interrogate when many
               agents share a cluster. The silent failure mode is a policy that is
               admitted but whose selector matches the WRONG set — e.g. it also
               binds tenant-B (so the "isolation" is namespace-wide noise), or it
               binds nothing (label propagation dropped). This cell fails LOUD on
               either mis-binding, so a green badge means the boundary is declared
               AND points at the right tenant.

## Test shape

1. Create two busybox Sandbox CRs — tenant-A and tenant-B — capturing each
   `metadata.uid`.
2. Wait for `type=Ready, status=True` on both (raise on timeout -> crash-fail).
3. Discover each backing Pod by `ownerReferences[].uid == <sandbox uid>`
   (convention-independent of pod-name shape / label propagation).
4. Apply a NetworkPolicy: `podSelector` = scenario + tenant-A labels;
   `policyTypes: [Ingress]`; one `ingress.from` rule whose `podSelector` is the
   scenario + tenant-A labels (same-tenant-only ingress).
5. Read the policy back (admission): it persisted, `Ingress` is in `policyTypes`,
   it carries exactly the one same-tenant ingress `from` rule.
6. Binding (the cross-tenant honesty check, BOTH halves required):
   a. the admitted `podSelector` matches the LIVE tenant-A Pod's labels, AND
   b. the admitted `podSelector` does NOT match the LIVE tenant-B Pod's labels.
   Half (a) catches dropped label propagation; half (b) catches an over-broad
   selector that would bind both tenants (no real isolation).
7. PASS iff admitted (same-tenant-only ingress shape) AND binds tenant-A AND
   excludes tenant-B.
8. FAIL iff the policy is not admitted / not same-tenant-only, the selector does
   not bind tenant-A, the selector also binds tenant-B, or either backing Pod
   could not be located.
9. Cleanup: delete the NetworkPolicy and both Sandboxes (controller cascades the
   Pods).

## Honesty caveat (load-bearing)

This MVP verifies the CONTROL-PLANE half only: the policy is admitted, selects
tenant-A, allows ingress from tenant-A only, and does not also select tenant-B. It
does NOT prove ENFORCEMENT — that a packet from tenant-B to tenant-A is actually
dropped on the wire. A policy can be admitted-and-correctly-bound yet not enforced
if the CNI does not honor NetworkPolicy (kindnet does not; this is exactly why the
cell is `requires-gke`). So a PASS here means "cross-tenant policy admitted and
correctly targeted at tenant-A only", NOT "tenant-B traffic is blocked". The
data-plane proof — an in-Pod exec connectivity probe that opens a socket from
tenant-B to tenant-A and asserts it is refused/times out, with a same-tenant
control to show the allow-rule still passes — is the documented next iteration,
kept out of the MVP so this cell is not blocked on the heavier pods/exec surface
(the same exec-surface tradeoff gvisor_canary defers for its in-sandbox
kernel-signature probe).

## Why a badge, not a latency

This is an isolation BADGE (is the cross-tenant boundary declared and targeted at
the right tenant?), not a perf measurement, so `run()` returns no `sla_metrics`
({}). There is no honest number to publish here and a fabricated one would violate
honest-by-construction.

## Substrate gate

This cell declares `requires_substrate="gke-sandbox"` with
`pending_reason="requires-gke"` in scenario_map. On vanilla kind (kindnet does not
enforce NetworkPolicy) run.py's gate emits a `pending (requires-gke)` cell WITHOUT
importing this module, so the published kind artifact renders pending and this body
runs only where a policy-enforcing CNI is genuinely available.
"""

from __future__ import annotations

from ._apiversion import sandbox_api_version, sandbox_gvr
from ._kube import load_cluster_config

import logging
import os
import time
import uuid

log = logging.getLogger("sandbox-scenario.cross-tenant-network-isolation")


_NAMESPACE = os.environ.get("BENCH_NAMESPACE", "default")
_SANDBOX_IMAGE = os.environ.get(
    "CROSS_TENANT_NETWORK_ISOLATION_SANDBOX_IMAGE", "busybox:1.36"
)

# Ready budget bounds a hung provision so the cell fails rather than hanging.
_READY_TIMEOUT_S = 240
# Backing-Pod discovery budget after Ready — resolves on the first sweep on a
# healthy cluster; the short window only absorbs informer-cache lag.
_POD_DISCOVERY_TIMEOUT_S = 30
_POLL_S = 0.25

_SBX_GVR = sandbox_gvr()

# Standard NetworkPolicy API (core networking group, stable v1).
_NETPOL_API_VERSION = "networking.k8s.io/v1"

# Shared scenario label on both tenants; a distinct tenant label per sandbox.
_SCENARIO_KEY = "honest-bench/scenario"
_SCENARIO_VALUE = "cross-tenant-network-isolation"
_TENANT_KEY = "honest-bench/tenant"
_TENANT_A = "a"
_TENANT_B = "b"


def _tenant_labels(tenant: str) -> dict:
    """Labels for a tenant sandbox: shared scenario label + this tenant's label."""
    return {_SCENARIO_KEY: _SCENARIO_VALUE, _TENANT_KEY: tenant}


def _build_sandbox_manifest(sandbox_name: str, tenant: str) -> dict:
    """Minimal busybox Sandbox CR for one tenant, carrying scenario + tenant labels.

    The labels are set on the podTemplate.metadata so the controller propagates
    them to the backing Pod — the NetworkPolicy podSelector keys on them, and the
    binding check verifies the propagation actually happened.
    """
    labels = _tenant_labels(tenant)
    return {
        "apiVersion": sandbox_api_version(),
        "kind": "Sandbox",
        "metadata": {
            "name": sandbox_name,
            "namespace": _NAMESPACE,
            "labels": dict(labels),
        },
        "spec": {
            "podTemplate": {
                "metadata": {"labels": dict(labels)},
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


def _build_isolation_policy_manifest(policy_name: str) -> dict:
    """Same-tenant-only ingress NetworkPolicy: select tenant-A, allow from tenant-A.

    `podSelector` = scenario + tenant-A labels (binds tenant-A's Pod only, NOT
    tenant-B's). `policyTypes: [Ingress]` with a single `from` rule whose
    `podSelector` is again the tenant-A labels => ingress to tenant-A is allowed
    ONLY from tenant-A, so tenant-B cannot reach tenant-A. The selector is scoped
    to the scenario label so the cell never touches unrelated workloads on a shared
    cluster.
    """
    tenant_a_selector = {"matchLabels": _tenant_labels(_TENANT_A)}
    return {
        "apiVersion": _NETPOL_API_VERSION,
        "kind": "NetworkPolicy",
        "metadata": {
            "name": policy_name,
            "namespace": _NAMESPACE,
            "labels": {_SCENARIO_KEY: _SCENARIO_VALUE},
        },
        "spec": {
            "podSelector": dict(tenant_a_selector),
            "policyTypes": ["Ingress"],
            "ingress": [{"from": [{"podSelector": dict(tenant_a_selector)}]}],
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
        f"unhealthy or the Pod failed to schedule"
    )


def _find_backing_pod(core, *, sandbox_uid: str, tenant: str):
    """Return the Pod owned by the Sandbox (ownerReferences uid match), or None.

    uid-match is convention-independent — it does not assume a pod-name shape or
    that the controller propagates podTemplate labels. The tenant label narrows the
    list, with a full-namespace fallback if label propagation did not occur.
    """
    labels = _tenant_labels(tenant)
    label_selector = ",".join(f"{k}={v}" for k, v in labels.items())
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


def _selector_matches(match_labels: dict, pod_labels: dict) -> bool:
    """True iff the live Pod labels satisfy the policy's matchLabels.

    Empty matchLabels selects all Pods (return True) — but the isolation policy
    uses a specific, non-empty selector, so a True here is a real binding against
    the tenant labels the controller propagated to the Pod.
    """
    if not match_labels:
        return True
    pod_labels = pod_labels or {}
    return all(pod_labels.get(k) == v for k, v in match_labels.items())


def _ingress_from_is_tenant_a_only(ingress_rules) -> bool:
    """True iff ingress admits exactly the single same-tenant (tenant-A) from-rule.

    Verifies the admitted policy allows ingress ONLY from tenant-A: exactly one
    ingress rule, exactly one `from` peer, that peer is a podSelector matching the
    tenant-A labels (and nothing broader). An empty/missing from-list would be
    allow-all (not isolation); an extra peer would widen the allowed set.
    """
    rules = ingress_rules or []
    if len(rules) != 1:
        return False
    peers = getattr(rules[0], "_from", None)
    # The python client maps the `from` field to the `_from` attribute (reserved
    # word); fall back to dict access for safety across client shapes.
    if peers is None and isinstance(rules[0], dict):
        peers = rules[0].get("from")
    peers = peers or []
    if len(peers) != 1:
        return False
    peer = peers[0]
    peer_selector = getattr(peer, "pod_selector", None)
    if peer_selector is None and isinstance(peer, dict):
        peer_selector = peer.get("podSelector") or peer.get("pod_selector")
    match_labels = getattr(peer_selector, "match_labels", None)
    if match_labels is None and isinstance(peer_selector, dict):
        match_labels = peer_selector.get("matchLabels") or peer_selector.get("match_labels")
    return (match_labels or {}) == _tenant_labels(_TENANT_A)


def _cleanup(custom, networking, *, sandbox_names: list, policy_name: str) -> None:
    """Best-effort delete of the NetworkPolicy and both Sandboxes (Pods cascade)."""
    from kubernetes.client.exceptions import ApiException
    try:
        networking.delete_namespaced_network_policy(
            name=policy_name, namespace=_NAMESPACE,
        )
    except ApiException as e:
        if e.status != 404:
            log.warning("cleanup: delete netpol %s failed: %s", policy_name, e)
    group, version, plural = _SBX_GVR
    for sandbox_name in sandbox_names:
        try:
            custom.delete_namespaced_custom_object(
                group=group, version=version, namespace=_NAMESPACE,
                plural=plural, name=sandbox_name,
            )
        except ApiException as e:
            if e.status != 404:
                log.warning("cleanup: delete sandbox %s failed: %s", sandbox_name, e)


def _create_sandbox(custom, *, sandbox_name: str, tenant: str) -> str:
    """Create one tenant Sandbox; return its metadata.uid (re-GET if create omits it)."""
    created = custom.create_namespaced_custom_object(
        group=_SBX_GVR[0], version=_SBX_GVR[1], namespace=_NAMESPACE,
        plural=_SBX_GVR[2], body=_build_sandbox_manifest(sandbox_name, tenant),
    )
    uid = ((created or {}).get("metadata") or {}).get("uid")
    if not uid:
        obj = custom.get_namespaced_custom_object(
            group=_SBX_GVR[0], version=_SBX_GVR[1], namespace=_NAMESPACE,
            plural=_SBX_GVR[2], name=sandbox_name,
        )
        uid = ((obj or {}).get("metadata") or {}).get("uid")
    return uid


def run(scenario_name: str) -> tuple[str, str, dict]:
    """Admit a same-tenant-only ingress policy; verify it binds tenant-A, excludes tenant-B.

    Returns a 3-tuple (outcome, excerpt, sla_metrics). Isolation badge, so
    sla_metrics is always {}. PASS iff the policy is admitted with a
    same-tenant-only ingress shape AND its selector binds the live tenant-A Pod AND
    does NOT bind the live tenant-B Pod; FAIL on a non-admitted/mis-shaped policy,
    a selector that does not bind tenant-A, a selector that also binds tenant-B, or
    an unlocatable backing Pod; raise on a provisioning/infra failure (crash-fail).
    """
    from kubernetes import client as k8s_client

    # Portable kubeconfig load (see _kube.load_cluster_config): an explicit
    # KUBECONFIG wins, else in-cluster when running as a pod, else the default
    # kubeconfig.
    load_cluster_config()

    custom = k8s_client.CustomObjectsApi()
    core = k8s_client.CoreV1Api()
    networking = k8s_client.NetworkingV1Api()

    suffix = uuid.uuid4().hex[:8]
    sandbox_a = f"xtenant-a-{suffix}"
    sandbox_b = f"xtenant-b-{suffix}"
    policy_name = f"xtenant-{suffix}"

    log.info(
        "creating tenant-A %s and tenant-B %s (image=%s) for cross-tenant badge",
        sandbox_a, sandbox_b, _SANDBOX_IMAGE,
    )
    uid_a = _create_sandbox(custom, sandbox_name=sandbox_a, tenant=_TENANT_A)
    uid_b = _create_sandbox(custom, sandbox_name=sandbox_b, tenant=_TENANT_B)

    try:
        _wait_for_sandbox_ready(custom, sandbox_name=sandbox_a)
        _wait_for_sandbox_ready(custom, sandbox_name=sandbox_b)
        log.info("both tenants Ready; locating backing Pods by owner uid")

        pod_a = _find_backing_pod(core, sandbox_uid=uid_a, tenant=_TENANT_A) if uid_a else None
        pod_b = _find_backing_pod(core, sandbox_uid=uid_b, tenant=_TENANT_B) if uid_b else None
        if pod_a is None or pod_b is None:
            missing = []
            if pod_a is None:
                missing.append(f"tenant-A (uid {uid_a!r})")
            if pod_b is None:
                missing.append(f"tenant-B (uid {uid_b!r})")
            return (
                "FAIL",
                f"Both tenant Sandboxes reached Ready but a backing Pod could not "
                f"be located by owner uid for {', '.join(missing)} within "
                f"{_POD_DISCOVERY_TIMEOUT_S}s — the cross-tenant binding is "
                f"unverifiable, so the badge cannot honestly pass.",
                {},
            )

        networking.create_namespaced_network_policy(
            namespace=_NAMESPACE, body=_build_isolation_policy_manifest(policy_name),
        )

        admitted = networking.read_namespaced_network_policy(
            name=policy_name, namespace=_NAMESPACE,
        )
        policy_types = list(admitted.spec.policy_types or [])
        ingress_rules = admitted.spec.ingress or []
        is_same_tenant_only = (
            ("Ingress" in policy_types)
            and _ingress_from_is_tenant_a_only(ingress_rules)
        )

        selector_labels = (
            admitted.spec.pod_selector.match_labels
            if admitted.spec.pod_selector else None
        ) or {}
        labels_a = pod_a.metadata.labels or {}
        labels_b = pod_b.metadata.labels or {}
        binds_a = _selector_matches(selector_labels, labels_a)
        excludes_b = not _selector_matches(selector_labels, labels_b)

        if is_same_tenant_only and binds_a and excludes_b:
            return (
                "PASS",
                f"NetworkPolicy {policy_name} admitted (policyTypes={policy_types}, "
                f"ingress from tenant-A only) and its podSelector {selector_labels} "
                f"binds the tenant-A backing Pod {pod_a.metadata.name} while "
                f"excluding the tenant-B backing Pod {pod_b.metadata.name} — the "
                f"cross-tenant boundary is declared and correctly targeted. Badge: "
                f"control-plane admission+binding verified; the in-Pod tenant-B->"
                f"tenant-A connectivity probe is the documented next iteration — a "
                f"PASS here does NOT assert tenant-B traffic is dropped on the wire.",
                {},
            )
        return (
            "FAIL",
            f"NetworkPolicy {policy_name} did not pass the admission+binding gate: "
            f"same_tenant_only_ingress={is_same_tenant_only} "
            f"(policyTypes={policy_types}, ingress_rule_count={len(ingress_rules)}), "
            f"binds_tenant_a={binds_a}, excludes_tenant_b={excludes_b} "
            f"(selector={selector_labels}, tenant_a_labels={labels_a}, "
            f"tenant_b_labels={labels_b}). Either the policy was not admitted with a "
            f"same-tenant-only ingress shape, its selector does not bind tenant-A, "
            f"or it over-broadly also binds tenant-B (no real isolation) — the "
            f"cross-tenant boundary is not correctly declared+targeted.",
            {},
        )
    finally:
        _cleanup(
            custom, networking,
            sandbox_names=[sandbox_a, sandbox_b],
            policy_name=policy_name,
        )
