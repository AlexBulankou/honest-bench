"""Cluster-free tests for the agent_identity_podcert badge's pure parse helpers.

Dependency-free: `python3 test_agent_identity_podcert.py` (exit 0 = pass). These
assert the three presence legs (served API surface, Running+Ready controller, both
ate-native signers anchored) classify correctly off fixtures, so the load-bearing
PASS/FAIL logic is verified without a cluster or the kubernetes client installed.
"""

import agent_identity_podcert as cell

A_SIGNER, B_SIGNER = cell.ATE_SIGNERS
PREFIX = cell.CONTROLLER_NAME_PREFIX

_PEM = "-----BEGIN CERTIFICATE-----\nMIIB...\n-----END CERTIFICATE-----\n"


# ---- parse_api_surface ----

def test_api_surface_both_served_ok():
    r = cell.parse_api_surface(
        {"resources": [
            {"name": "clustertrustbundles"},
            {"name": "podcertificaterequests"},
            {"name": "certificatesigningrequests"},  # unrelated sibling kind
        ]}
    )
    assert r["ok"] is True
    assert r["clustertrustbundles_served"] is True
    assert r["podcertificaterequests_served"] is True


def test_api_surface_one_missing_fails():
    r = cell.parse_api_surface({"resources": [{"name": "clustertrustbundles"}]})
    assert r["ok"] is False
    assert r["clustertrustbundles_served"] is True
    assert r["podcertificaterequests_served"] is False


def test_api_surface_not_served_404_models_empty():
    # _fetch_api_surface returns {"resources": []} on a 404 — must read not-served.
    r = cell.parse_api_surface({"resources": []})
    assert r["ok"] is False
    assert r["served"] == []


def test_api_surface_none_and_garbage_safe():
    assert cell.parse_api_surface(None)["ok"] is False
    assert cell.parse_api_surface({"resources": ["not-a-dict", {}]})["ok"] is False


# ---- parse_controller ----

def test_controller_running_ready_ok():
    r = cell.parse_controller([
        {"namespace": "kube-system", "name": f"{PREFIX}-7d9f-abcde", "phase": "Running", "ready": True},
        {"namespace": "default", "name": "unrelated-pod", "phase": "Running", "ready": True},
    ])
    assert r["ok"] is True
    assert r["running"] == 1
    assert len(r["pods"]) == 1  # only the prefix-matched pod is reported


def test_controller_present_but_not_ready_fails():
    r = cell.parse_controller([
        {"namespace": "kube-system", "name": f"{PREFIX}-x", "phase": "Running", "ready": False},
    ])
    assert r["ok"] is False
    assert r["running"] == 0
    assert len(r["pods"]) == 1  # matched but not counted as running-ready


def test_controller_pending_phase_fails():
    r = cell.parse_controller([
        {"namespace": "kube-system", "name": f"{PREFIX}-x", "phase": "Pending", "ready": False},
    ])
    assert r["ok"] is False


def test_controller_absent_fails():
    r = cell.parse_controller([
        {"namespace": "default", "name": "some-other-pod", "phase": "Running", "ready": True},
    ])
    assert r["ok"] is False
    assert r["pods"] == []


def test_controller_empty_and_none_safe():
    assert cell.parse_controller([])["ok"] is False
    assert cell.parse_controller(None)["ok"] is False


# ---- parse_signer_bundles ----

def _ctb(name, signer, anchor):
    return {"metadata": {"name": name}, "spec": {"signerName": signer, "trustBundle": anchor}}


def test_signers_both_anchored_ok():
    r = cell.parse_signer_bundles({"items": [
        _ctb("a-bundle", A_SIGNER, _PEM),
        _ctb("b-bundle", B_SIGNER, _PEM),
    ]})
    assert r["ok"] is True
    assert r["signers"][A_SIGNER]["ok"] is True
    assert r["signers"][B_SIGNER]["ok"] is True


def test_signers_one_empty_anchor_fails():
    r = cell.parse_signer_bundles({"items": [
        _ctb("a-bundle", A_SIGNER, _PEM),
        _ctb("b-bundle", B_SIGNER, ""),  # admitted but empty anchor
    ]})
    assert r["ok"] is False
    assert r["signers"][A_SIGNER]["ok"] is True
    assert r["signers"][B_SIGNER]["ok"] is False


def test_signers_one_missing_fails():
    r = cell.parse_signer_bundles({"items": [_ctb("a-bundle", A_SIGNER, _PEM)]})
    assert r["ok"] is False
    assert r["signers"][B_SIGNER]["ok"] is False
    assert r["signers"][B_SIGNER]["bundles"] == []


def test_signers_unrelated_signer_ignored():
    r = cell.parse_signer_bundles({"items": [
        _ctb("a-bundle", A_SIGNER, _PEM),
        _ctb("b-bundle", B_SIGNER, _PEM),
        _ctb("noise", "kubernetes.io/kube-apiserver-client", _PEM),
    ]})
    assert r["ok"] is True


def test_signers_anchorless_non_pem_string_fails():
    r = cell.parse_signer_bundles({"items": [
        _ctb("a-bundle", A_SIGNER, "not a pem block"),
        _ctb("b-bundle", B_SIGNER, _PEM),
    ]})
    assert r["ok"] is False
    assert r["signers"][A_SIGNER]["ok"] is False


def test_signers_empty_and_none_safe():
    assert cell.parse_signer_bundles({"items": []})["ok"] is False
    assert cell.parse_signer_bundles(None)["ok"] is False


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok: {fn.__name__}")
    print(f"test_agent_identity_podcert: all {len(fns)} assertions passed")


if __name__ == "__main__":
    _run_all()
