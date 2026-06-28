"""Shared sandbox-CRD apiVersion / GVR resolution.

Single source of truth for the version string every scenario module uses to
address the four agent-sandbox CRDs. This resolves to the served version
``v1beta1`` (the upstream agent-sandbox controller, built from ``main``, serves
v1beta1).

The version is a literal constant here, NOT discovery-derived: pinning the
intended version lets a self-check assert concrete literal expected GVR tuples
(e.g. ``("extensions.agents.x-k8s.io", "v1beta1", "sandboxtemplates")``) rather
than tautologically re-deriving them from these helpers.

Out of scope: the ``podsnapshot.gke.io/v1`` GVR used by the snapshot scenarios is
a GKE-native API on its own version line, untouched by the sandbox CRD versioning.
"""

from __future__ import annotations

# The served agent-sandbox CRD version (upstream main serves v1beta1).
SANDBOX_API_VERSION = "v1beta1"

# CRD API groups — stable across version graduations.
SANDBOX_GROUP = "agents.x-k8s.io"
SANDBOX_EXT_GROUP = "extensions.agents.x-k8s.io"


def sandbox_api_version() -> str:
    """``apiVersion`` string for core-group resources (Sandbox)."""
    return f"{SANDBOX_GROUP}/{SANDBOX_API_VERSION}"


def ext_api_version() -> str:
    """``apiVersion`` string for extensions-group resources (Template/Claim/WarmPool)."""
    return f"{SANDBOX_EXT_GROUP}/{SANDBOX_API_VERSION}"


def sandbox_gvr() -> tuple[str, str, str]:
    """(group, version, plural) for the Sandbox CRD."""
    return (SANDBOX_GROUP, SANDBOX_API_VERSION, "sandboxes")


def template_gvr() -> tuple[str, str, str]:
    """(group, version, plural) for the SandboxTemplate CRD."""
    return (SANDBOX_EXT_GROUP, SANDBOX_API_VERSION, "sandboxtemplates")


def claim_gvr() -> tuple[str, str, str]:
    """(group, version, plural) for the SandboxClaim CRD."""
    return (SANDBOX_EXT_GROUP, SANDBOX_API_VERSION, "sandboxclaims")


def warmpool_gvr() -> tuple[str, str, str]:
    """(group, version, plural) for the SandboxWarmPool CRD."""
    return (SANDBOX_EXT_GROUP, SANDBOX_API_VERSION, "sandboxwarmpools")
