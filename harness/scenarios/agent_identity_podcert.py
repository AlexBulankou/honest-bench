"""Agent-identity (Pod-Certificate): verify the SPIFFE/Pod-Certificate control-plane chain is present and ready.

    Setup    : A cluster on which the agent-identity stack is expected — the
               beta Pod-Certificate API surface, the upstream
               podcertificate-controller, and the two ate-native signer
               ClusterTrustBundles that anchor agent identity.
    Action   : Read-only, three control-plane assertions: (1) the beta API
               surface serves BOTH `clustertrustbundles` and
               `podcertificaterequests` at `certificates.k8s.io/v1beta1`; (2) at
               least one `podcertificate-controller` Pod is Running+Ready; (3)
               each of the two ate-native signers has a ClusterTrustBundle whose
               `spec.trustBundle` carries a non-empty PEM trust anchor.
    Expected : All three hold — the API kinds are served, the controller that
               fulfils PodCertificateRequests is live, and both signers publish a
               trust anchor. The control-plane chain an agent's Pod-bound
               identity depends on is in place.
    Why      : Agent identity is the property buyers interrogate when many agents
               share a cluster and must authenticate to each other and to
               services. The silent failure mode is a half-present chain: the
               beta kinds registered but the controller crash-looping (requests
               never fulfilled), or the controller up but a signer's trust anchor
               empty (issued certs chain to nothing). This cell fails LOUD on any
               missing leg, so a green badge means the whole presence chain is
               there — not just one rung of it.

## Test shape

1. Discover the served API surface at `certificates.k8s.io/v1beta1` (a raw GET of
   the versioned group endpoint). PASS-leg iff BOTH `clustertrustbundles` and
   `podcertificaterequests` appear in the served resource list. A 404 on the
   versioned endpoint means the group/version is not served at all -> not-served
   (leg fails), an honest FAIL, not a crash.
2. List Pods cluster-wide; match on the `podcertificate-controller` name prefix.
   PASS-leg iff >=1 matched Pod is `phase==Running` AND `Ready=True`.
3. List `clustertrustbundles` (cluster-scoped custom objects); group by
   `spec.signerName`. PASS-leg iff EACH of the two ate-native signers
   (`podidentity.podcert.ate.dev/identity`, `servicedns.podcert.ate.dev/identity`)
   has at least one bundle whose `spec.trustBundle` contains a PEM
   `BEGIN CERTIFICATE` block (non-empty anchor).
4. PASS iff all three legs hold; FAIL (honest, with a leg-by-leg excerpt) iff any
   leg is missing; raise on an auth/connectivity error reaching the cluster
   (crash-fail — the chain is unverifiable, not absent).

## Honesty caveat (load-bearing)

This MVP verifies the CONTROL-PLANE PRESENCE chain only: the beta kinds are
served, the controller is Running+Ready, and both signers publish a trust anchor.
It does NOT exercise a live identity ISSUANCE — it does not create a
PodCertificateRequest, wait for the controller to fulfil it, and cryptographically
verify the returned leaf chains to the signer's anchor. So a PASS here means "the
agent-identity control-plane chain is present and ready", NOT "a Pod just got a
valid, anchor-chained certificate". The end-to-end proof — a kubelet-driven
PodCertificateRequest fulfilment with leaf-to-anchor chain verification in a
throwaway namespace — is the documented next iteration, kept out of the MVP so
this badge is not blocked on the heavier mutating-issuance surface (the same
read-only-vs-mutating tradeoff the other isolation badges make).

## Why a badge, not a latency

This is an identity-presence BADGE (is the Pod-Certificate chain present and
ready?), not a perf measurement, so `run()` returns no `sla_metrics` ({}). There
is no honest number to publish here and a fabricated one would violate
honest-by-construction.

## Substrate gate

This is a `substrate`-product cell: the agent-identity chain lives on the
substrate operator's cluster. On a substrate that does not run the Pod-Certificate
stack the legs honestly FAIL (kinds not served / controller absent / no signer
anchor) rather than crashing — the badge reports the chain absent, which is the
true state. SPIFFE / Pod-Certificate / ClusterTrustBundle / PodCertificateRequest
are public GKE security surfaces.
"""

from __future__ import annotations

import logging

log = logging.getLogger("substrate-scenario.agent-identity-podcert")

# Beta Pod-Certificate API surface (public GKE security surface).
API_GROUP = "certificates.k8s.io"
API_VERSION_BETA = "v1beta1"
# The two kinds the agent-identity chain depends on.
_BETA_KINDS = ("clustertrustbundles", "podcertificaterequests")

# Upstream controller that fulfils PodCertificateRequests; matched by name prefix
# (the Deployment appends a pod-template hash + replica suffix).
CONTROLLER_NAME_PREFIX = "podcertificate-controller"

# ate-native signer names that anchor agent identity (public infra names).
ATE_SIGNERS = (
    "podidentity.podcert.ate.dev/identity",
    "servicedns.podcert.ate.dev/identity",
)

# ClusterTrustBundle is a cluster-scoped custom resource.
_CTB_PLURAL = "clustertrustbundles"

# PEM marker proving a trust anchor is actually present (not an empty string).
_PEM_ANCHOR_MARKER = "BEGIN CERTIFICATE"


def parse_api_surface(apiresourcelist: dict) -> dict:
    """Given the APIResourceList dict for certificates.k8s.io/v1beta1, check both kinds served.

    `apiresourcelist` is the raw `{"resources": [{"name": ...}, ...]}` returned by a
    GET of the versioned group endpoint (an empty resources list models a 404 /
    group-version-not-served). Pure: decoded-data-in, result-dict-out, so the
    served-surface gate is unit-testable without a cluster.
    """
    names = {
        r.get("name")
        for r in (apiresourcelist or {}).get("resources", [])
        if isinstance(r, dict)
    }
    ctb_ok = _BETA_KINDS[0] in names
    pcr_ok = _BETA_KINDS[1] in names
    return {
        "clustertrustbundles_served": ctb_ok,
        "podcertificaterequests_served": pcr_ok,
        "served": sorted(n for n in names if n),
        "ok": ctb_ok and pcr_ok,
    }


def parse_controller(pods: list) -> dict:
    """Given normalized pod dicts, check >=1 podcertificate-controller is Running+Ready.

    Each pod is a plain dict {"namespace","name","phase","ready"} — run() normalizes
    the V1Pod objects down to this shape so this function stays pure and
    cluster-free for unit testing.
    """
    matches = [
        p for p in (pods or [])
        if str(p.get("name", "")).startswith(CONTROLLER_NAME_PREFIX)
    ]
    running = [
        m for m in matches
        if m.get("phase") == "Running" and m.get("ready") is True
    ]
    return {"ok": len(running) >= 1, "running": len(running), "pods": matches}


def parse_signer_bundles(ctb_list: dict) -> dict:
    """Given the ClusterTrustBundle list dict, check each ate-native signer has a non-empty anchor.

    `ctb_list` is the raw `{"items": [...]}` returned by a cluster-scoped custom
    object list (camelCase preserved). Pure: groups bundles by `spec.signerName`,
    flags each as anchor-present iff `spec.trustBundle` carries a PEM block, then
    requires BOTH ate-native signers to have at least one anchored bundle.
    """
    by_signer: dict = {}
    for ctb in (ctb_list or {}).get("items", []):
        if not isinstance(ctb, dict):
            continue
        spec = ctb.get("spec", {}) or {}
        signer = spec.get("signerName", "") or ""
        anchor = spec.get("trustBundle", "") or ""
        name = ((ctb.get("metadata", {}) or {}).get("name", "?")) or "?"
        by_signer.setdefault(signer, []).append(
            {"name": name, "anchor_present": _PEM_ANCHOR_MARKER in anchor}
        )
    per_signer: dict = {}
    all_ok = True
    for signer in ATE_SIGNERS:
        bundles = by_signer.get(signer, [])
        ok = any(b["anchor_present"] for b in bundles)
        all_ok = all_ok and ok
        per_signer[signer] = {"ok": ok, "bundles": bundles}
    return {"ok": all_ok, "signers": per_signer}


def _fetch_api_surface(api_client) -> dict:
    """Raw GET of /apis/<group>/<beta>; return the APIResourceList dict ({} on 404)."""
    from kubernetes.client.exceptions import ApiException

    try:
        resp = api_client.call_api(
            f"/apis/{API_GROUP}/{API_VERSION_BETA}",
            "GET",
            auth_settings=["BearerToken"],
            response_type="object",
            _return_http_data_only=True,
        )
        return resp or {}
    except ApiException as e:
        if e.status == 404:
            # Group/version not served at all — model as an empty resource list so
            # the presence leg fails honestly rather than crashing.
            return {"resources": []}
        raise


def _normalize_pods(pod_list) -> list:
    """V1PodList -> list of {"namespace","name","phase","ready"} dicts for parse_controller."""
    out = []
    for p in pod_list.items:
        conds = {
            c.type: c.status
            for c in (p.status.conditions or [])
        } if p.status else {}
        out.append({
            "namespace": p.metadata.namespace,
            "name": p.metadata.name,
            "phase": (p.status.phase if p.status else None),
            "ready": conds.get("Ready") == "True",
        })
    return out


def run(scenario_name: str) -> tuple[str, str, dict]:
    """Verify the Pod-Certificate control-plane presence chain (served API + controller + signer anchors).

    Returns a 3-tuple (outcome, excerpt, sla_metrics). Identity-presence badge, so
    sla_metrics is always {}. PASS iff all three legs hold (beta kinds served,
    controller Running+Ready, both ate-native signers anchored); FAIL (with a
    leg-by-leg excerpt) iff any leg is missing; raise on an auth/connectivity
    failure reaching the cluster (crash-fail — chain unverifiable, not absent).
    """
    from kubernetes import client as k8s_client
    from kubernetes import config as k8s_config

    try:
        k8s_config.load_incluster_config()
    except k8s_config.ConfigException:
        k8s_config.load_kube_config()

    api_client = k8s_client.ApiClient()
    core = k8s_client.CoreV1Api()
    custom = k8s_client.CustomObjectsApi()

    log.info("reading Pod-Certificate control-plane presence chain (read-only)")

    api_surface = parse_api_surface(_fetch_api_surface(api_client))

    pod_list = core.list_pod_for_all_namespaces()
    controller = parse_controller(_normalize_pods(pod_list))

    ctb_list = custom.list_cluster_custom_object(
        group=API_GROUP, version=API_VERSION_BETA, plural=_CTB_PLURAL,
    )
    signers = parse_signer_bundles(ctb_list)

    all_ok = api_surface["ok"] and controller["ok"] and signers["ok"]

    signer_summary = ", ".join(
        f"{s.rsplit('/', 1)[0]}={'anchored' if signers['signers'][s]['ok'] else 'MISSING'}"
        for s in ATE_SIGNERS
    )
    if all_ok:
        return (
            "PASS",
            f"Pod-Certificate control-plane chain present: "
            f"certificates.k8s.io/{API_VERSION_BETA} serves both "
            f"clustertrustbundles+podcertificaterequests; "
            f"{controller['running']} podcertificate-controller Pod(s) Running+Ready; "
            f"both ate-native signers anchored ({signer_summary}). Badge: "
            f"control-plane PRESENCE verified; a live PodCertificateRequest "
            f"issuance + leaf-to-anchor chain verification is the documented next "
            f"iteration — a PASS here does NOT assert a cert was just issued.",
            {},
        )
    return (
        "FAIL",
        f"Pod-Certificate control-plane chain incomplete: "
        f"api_served={api_surface['ok']} "
        f"(clustertrustbundles={api_surface['clustertrustbundles_served']}, "
        f"podcertificaterequests={api_surface['podcertificaterequests_served']}), "
        f"controller_running_ready={controller['ok']} "
        f"(matched={len(controller['pods'])}, running_ready={controller['running']}), "
        f"signers_anchored={signers['ok']} ({signer_summary}). At least one leg of "
        f"the agent-identity presence chain is missing, so the badge cannot "
        f"honestly pass.",
        {},
    )
