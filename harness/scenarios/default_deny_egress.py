"""Default-deny egress: verify a default-deny-egress NetworkPolicy is admitted and binds the sandbox Pod.

    Setup    : A single bare Sandbox CR (busybox podTemplate), carrying a scenario
               label that the controller propagates to the backing Pod.
    Action   : Create the Sandbox, wait for Ready, locate the backing Pod, then
               apply a default-deny-egress NetworkPolicy scoped to that Pod
               (`policyTypes: [Egress]`, no egress rules) and verify it was
               admitted by the API server AND that its `podSelector` actually
               binds the live backing Pod (the Pod's labels satisfy the admitted
               selector).
    Expected : The NetworkPolicy is persisted with a deny-all-egress shape and its
               selector selects the sandbox Pod — the control-plane half of egress
               lockdown is in place and correctly targeted.
    Why      : A default-deny-egress posture is the network control buyers should
               interrogate first ("sandboxed" is a spectrum, not a checkbox). The
               first failure mode is silent: a policy that is admitted but whose
               selector matches NO pod (e.g. the controller did not propagate the
               podTemplate labels to the backing Pod) enforces nothing while the
               table looks green. This cell fails LOUD on that mis-binding.

## Test shape

1. Create a bare busybox Sandbox CR; capture `metadata.uid`.
2. Wait for `type=Ready, status=True` (raise on timeout -> crash-fail cell).
3. Discover the backing Pod by `ownerReferences[].uid == <sandbox uid>`
   (convention-independent of pod-name shape / label propagation).
4. Apply a NetworkPolicy: `podSelector` = the scenario label, `policyTypes:
   [Egress]`, NO `egress` rules (an empty egress rule-set with Egress in
   policyTypes is the canonical default-deny-egress shape).
5. Read the policy back (admission): it persisted, `Egress` is in `policyTypes`,
   and it carries no egress allow-rules (deny-all).
6. Binding: evaluate the ADMITTED policy's `podSelector.matchLabels` against the
   LIVE backing Pod's labels — PASS the binding half only if the real Pod (as the
   API server holds it) satisfies the selector. This catches a controller that
   dropped/renamed the podTemplate labels, which would leave the policy selecting
   nothing.
7. PASS iff admitted (deny-all-egress shape) AND bound to the backing Pod.
8. FAIL iff the policy is not admitted / not deny-all-egress, the selector does
   not bind the live Pod, or the backing Pod could not be located.
9. Cleanup: delete the NetworkPolicy and the Sandbox (controller cascades the Pod).

## Honesty caveat (load-bearing)

This MVP verifies the CONTROL-PLANE half only: the policy is admitted and its
selector binds the Pod. It does NOT prove ENFORCEMENT — that egress traffic is
actually dropped on the wire. A policy can be admitted-and-bound yet not enforced
if the CNI does not honor NetworkPolicy (kindnet does not; this is exactly why the
cell is `requires-gke`). So a PASS here means "default-deny-egress policy admitted
and correctly targeted", NOT "egress is blocked". The data-plane proof — an
in-Pod exec connectivity probe that attempts an outbound connection and asserts it
is refused/times out — is the documented next iteration, kept out of the MVP so
this cell is not blocked on the heavier pods/exec surface (the same exec-surface
tradeoff gvisor_canary defers for its in-sandbox kernel-signature probe).

## Why a badge, not a latency

This is an isolation BADGE (is the egress boundary declared and targeted?), not a
perf measurement, so `run()` returns no `sla_metrics` ({}). There is no honest
number to publish here and a fabricated one would violate honest-by-construction.

## Substrate gate

This cell declares `requires_substrate="gke-sandbox"` with
`pending_reason="requires-gke"` in scenario_map. On vanilla kind (kindnet does not
enforce NetworkPolicy) run.py's gate emits a `pending (requires-gke)` cell WITHOUT
importing this module, so the published kind artifact renders pending and this
body runs only where a policy-enforcing CNI is genuinely available.
"""

from __future__ import annotations

from . import netpol_probe
from ._apiversion import sandbox_api_version, sandbox_gvr
from ._kube import load_cluster_config

import logging
import os
import time
import uuid

log = logging.getLogger("sandbox-scenario.default-deny-egress")


_NAMESPACE = os.environ.get("BENCH_NAMESPACE", "default")
_SANDBOX_IMAGE = os.environ.get("DEFAULT_DENY_EGRESS_SANDBOX_IMAGE", "busybox:1.36")

# Ready budget bounds a hung provision so the cell fails rather than hanging.
_READY_TIMEOUT_S = 240
# Backing-Pod discovery budget after Ready — resolves on the first sweep on a
# healthy cluster; the short window only absorbs informer-cache lag.
_POD_DISCOVERY_TIMEOUT_S = 30
_POLL_S = 0.25

# Armed data-plane probe (#3907) egress target. A public vendor endpoint
# (Cloudflare 1.1.1.1:443) the backing Pod attempts to reach; default-deny-egress
# should block it post-policy. Env-overridable so a fire can point at a reachable
# target appropriate to the cluster's egress path. No internal host is ever
# defaulted here (public-repo hygiene).
_EGRESS_PROBE_HOST = os.environ.get("BENCH_NETPOL_EGRESS_PROBE_HOST", "1.1.1.1")
_EGRESS_PROBE_PORT = int(os.environ.get("BENCH_NETPOL_EGRESS_PROBE_PORT", "443"))

_SBX_GVR = sandbox_gvr()

# Standard NetworkPolicy API (core networking group, stable v1).
_NETPOL_API_VERSION = "networking.k8s.io/v1"

_SCENARIO_LABEL = {"honest-bench/scenario": "default-deny-egress"}


def _build_sandbox_manifest(sandbox_name: str) -> dict:
    """Minimal busybox Sandbox CR carrying the scenario label.

    The scenario label is set on the podTemplate.metadata so the controller
    propagates it to the backing Pod — the NetworkPolicy podSelector keys on it,
    and the binding check verifies the propagation actually happened.
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


def _build_egress_policy_manifest(policy_name: str) -> dict:
    """Default-deny-egress NetworkPolicy scoped to the scenario Pod.

    `policyTypes: [Egress]` with NO `egress` key is the canonical deny-all-egress
    shape. The selector is the scenario label (scoped to OUR sandbox Pod, not the
    whole namespace) so the cell never accidentally denies egress for unrelated
    workloads on a shared cluster.
    """
    return {
        "apiVersion": _NETPOL_API_VERSION,
        "kind": "NetworkPolicy",
        "metadata": {
            "name": policy_name,
            "namespace": _NAMESPACE,
            "labels": dict(_SCENARIO_LABEL),
        },
        "spec": {
            "podSelector": {"matchLabels": dict(_SCENARIO_LABEL)},
            "policyTypes": ["Egress"],
            # No `egress` rules => deny all egress for the selected Pod.
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


def _find_backing_pod(core, *, sandbox_uid: str):
    """Return the Pod owned by the Sandbox (ownerReferences uid match), or None.

    uid-match is convention-independent — it does not assume a pod-name shape or
    that the controller propagates podTemplate labels. The scenario label narrows
    the list, with a full-namespace fallback if label propagation did not occur.
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


def _selector_matches(match_labels: dict, pod_labels: dict) -> bool:
    """True iff the live Pod labels satisfy the policy's matchLabels.

    Empty matchLabels selects all Pods (return True) — but the deny-egress policy
    uses a specific, non-empty selector, so a True here is a real binding against
    the scenario label the controller propagated to the Pod.
    """
    if not match_labels:
        return True
    pod_labels = pod_labels or {}
    return all(pod_labels.get(k) == v for k, v in match_labels.items())


def _cleanup(custom, networking, *, sandbox_name: str, policy_name: str) -> None:
    """Best-effort delete of the NetworkPolicy and Sandbox (Pod cascades)."""
    from kubernetes.client.exceptions import ApiException
    try:
        networking.delete_namespaced_network_policy(
            name=policy_name, namespace=_NAMESPACE,
        )
    except ApiException as e:
        if e.status != 404:
            log.warning("cleanup: delete netpol %s failed: %s", policy_name, e)
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
    """Admit a default-deny-egress policy and verify it binds the sandbox Pod.

    Returns a 3-tuple (outcome, excerpt, sla_metrics). Isolation badge, so
    sla_metrics is always {}. PASS iff the policy is admitted with a
    deny-all-egress shape AND its selector binds the live backing Pod; FAIL on a
    non-admitted/mis-shaped policy, a selector that does not bind, or an
    unlocatable backing Pod; raise on a provisioning/infra failure (crash-fail).
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
    sandbox_name = f"deny-egress-{suffix}"
    policy_name = f"deny-egress-{suffix}"

    log.info(
        "creating Sandbox %s (image=%s) for default-deny-egress badge",
        sandbox_name, _SANDBOX_IMAGE,
    )
    created = custom.create_namespaced_custom_object(
        group=_SBX_GVR[0], version=_SBX_GVR[1], namespace=_NAMESPACE,
        plural=_SBX_GVR[2], body=_build_sandbox_manifest(sandbox_name),
    )
    sandbox_uid = ((created or {}).get("metadata") or {}).get("uid")
    if not sandbox_uid:
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
                f"{_POD_DISCOVERY_TIMEOUT_S}s — the egress-policy binding is "
                f"unverifiable, so the badge cannot honestly pass.",
                {},
            )

        # Data-plane probe (#3907) — default-off. Baseline egress connect BEFORE the
        # policy: establishes that outbound egress works at all, so a later
        # post-policy block is attributable to the policy and not to a no-egress test
        # environment. Only a True baseline arms the deny half below; a None/False
        # baseline keeps the probe inconclusive (the cell keeps its control-plane
        # badge — never a fabricated breach). Never raises (exec_connection returns
        # None on any cluster/exec error).
        egress_armed = netpol_probe.dataplane_probe_enabled()
        baseline_egress = None
        if egress_armed:
            baseline_egress = netpol_probe.exec_connection(
                core, namespace=_NAMESPACE, pod_name=pod.metadata.name,
                host=_EGRESS_PROBE_HOST, port=_EGRESS_PROBE_PORT,
            )

        networking.create_namespaced_network_policy(
            namespace=_NAMESPACE, body=_build_egress_policy_manifest(policy_name),
        )

        admitted = networking.read_namespaced_network_policy(
            name=policy_name, namespace=_NAMESPACE,
        )
        policy_types = list(admitted.spec.policy_types or [])
        egress_rules = admitted.spec.egress or []
        is_deny_all_egress = ("Egress" in policy_types) and (len(egress_rules) == 0)

        selector_labels = (
            admitted.spec.pod_selector.match_labels
            if admitted.spec.pod_selector else None
        ) or {}
        pod_labels = pod.metadata.labels or {}
        is_bound = _selector_matches(selector_labels, pod_labels)

        if is_deny_all_egress and is_bound:
            # Data-plane probe (#3907) — default-off. When armed AND the baseline
            # egress connect succeeded pre-policy, re-attempt the same outbound
            # connect: with default-deny-egress in place it should now be blocked.
            # control_allowed carries the baseline precondition (True only if egress
            # worked pre-policy), so a non-True baseline degrades to inconclusive ->
            # control-plane (never a false breach/over-block). enforced upgrades the
            # badge; a breach (egress still flowed) FAILs.
            if egress_armed:
                if baseline_egress is True:
                    post = netpol_probe.exec_connection(
                        core, namespace=_NAMESPACE, pod_name=pod.metadata.name,
                        host=_EGRESS_PROBE_HOST, port=_EGRESS_PROBE_PORT,
                    )
                    deny_blocked = (not post) if post is not None else None
                    control_allowed = True
                else:
                    deny_blocked = None
                    control_allowed = None
                verdict, scope = netpol_probe.classify_dataplane(
                    deny_blocked, control_allowed
                )
                if verdict == "enforced":
                    return (
                        "PASS",
                        f"NetworkPolicy {policy_name} ENFORCED: the in-Pod probe "
                        f"confirmed outbound egress from Pod {pod.metadata.name} to "
                        f"{_EGRESS_PROBE_HOST}:{_EGRESS_PROBE_PORT} worked pre-policy "
                        f"and is blocked on the wire with default-deny-egress applied "
                        f"— egress lockdown verified, not just admitted.",
                        {"badge_scope": "enforced", "badge_construction": "standard-np"},
                    )
                if verdict in ("breach", "over-block"):
                    # See the matching comment in cross_tenant_network_isolation.py:
                    # this excerpt is `del`-ed by run.py before results.json is ever
                    # written (public-safety rule), so without this log line a badge
                    # regression is unattributable after the ephemeral CI cluster
                    # tears down (hb#314). Safe to log: semantic verdict only, no
                    # resource names, lands in Cloud Build's private build log.
                    log.warning(
                        "dataplane FAIL: verdict=%s deny_blocked=%s control_allowed=%s",
                        verdict, deny_blocked, control_allowed,
                    )
                    return (
                        "FAIL",
                        f"NetworkPolicy {policy_name} admitted+bound (control-plane "
                        f"OK) but the in-Pod data-plane probe read {verdict}: "
                        f"egress from Pod {pod.metadata.name} to "
                        f"{_EGRESS_PROBE_HOST}:{_EGRESS_PROBE_PORT} still flowed "
                        f"despite default-deny-egress. The policy is declared but "
                        f"does NOT enforce egress lockdown on the wire — an "
                        f"admitted-but-inert policy is not isolation.",
                        {},
                    )
                # inconclusive: probe could not establish a clean baseline+block —
                # degrade to the control-plane badge (the admission proof) below.
                log.info(
                    "dataplane egress probe inconclusive (baseline=%s, "
                    "deny_blocked=%s) — keeping control-plane badge",
                    baseline_egress, deny_blocked,
                )
            return (
                "PASS",
                f"NetworkPolicy {policy_name} admitted (policyTypes={policy_types}, "
                f"no egress rules => default-deny-egress) and its podSelector "
                f"{selector_labels} binds the sandbox backing Pod "
                f"{pod.metadata.name} (live Pod labels satisfy the admitted "
                f"selector). Badge: control-plane admission+binding verified; the "
                f"in-Pod egress-blocked connectivity probe is the documented next "
                f"iteration — a PASS here does NOT assert egress is dropped on the "
                f"wire.",
                {},
            )
        return (
            "FAIL",
            f"NetworkPolicy {policy_name} did not pass the admission+binding gate: "
            f"deny_all_egress={is_deny_all_egress} "
            f"(policyTypes={policy_types}, egress_rule_count={len(egress_rules)}), "
            f"bound={is_bound} (selector={selector_labels}, "
            f"pod_labels={pod_labels}). Either the policy was not admitted with a "
            f"deny-all-egress shape, or its selector does not bind the live "
            f"backing Pod (the controller may not have propagated the podTemplate "
            f"labels) — the egress boundary is not correctly declared+targeted.",
            {},
        )
    finally:
        _cleanup(
            custom, networking,
            sandbox_name=sandbox_name,
            policy_name=policy_name,
        )
