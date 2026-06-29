"""Runtime-class pin + bound-pod runtime verification — shared, honest-by-construction.

The Core-Metrics matrix publishes per-runtime rows (gVisor / Kata+microVM) for each
activation mode (warm-pool hit / unique-image cold / resume-from-suspend). A row is a
HONEST runtime headline only if two things hold:

  1. the scenario's Pods were *pinned* to that runtime (``runtimeClassName`` set) AND
     could actually schedule onto the runtime's (usually tainted) node pool, and
  2. the Pods that backed the counted sandboxes *actually ran* under that runtime.

A Ready sandbox alone proves NEITHER: a controller that silently dropped
``runtimeClassName`` would still reach Ready under the node default runtime (runc),
so a runc burst could publish under a gVisor/Kata banner — the "green cell that lies".
``burst_create`` already closes this for the headline count; this module factors the
same guard into a shared, runtime-agnostic primitive so the matrix scenarios
(``warmpool_cold_start`` for the warm-pool row, and the cold/Kata rows as their owners
adopt it) all pin-and-verify the SAME way instead of each re-deriving the toleration
shape and the owner-uid pod walk.

## Split: pure core vs. thin I/O (mirrors netpol_probe / ttfe_probe)

Everything that decides shape or verdict is pure and fully offline-testable:

  - ``resolve_scheduling()``    — runtime_class -> the toleration(s) + nodeSelector a
                                  Pod needs to land on that runtime's node pool.
  - ``apply_runtime_class()``   — set ``runtimeClassName`` + merge that scheduling onto
                                  a pod_spec dict. No-op for the empty class (node
                                  default runtime — correct for vanilla kind).
  - ``assert_substrate_runtime_consistency()`` — refuse a substrate banner whose
                                  claimed isolation does not match the pinned runtime.
  - ``classify_runtime_violations()`` / ``assert_no_runtime_violations()`` — given
                                  observed (sandbox -> backing runtimeClassName) pairs,
                                  decide which backing Pods betray the expected runtime.

Only ``verify_bound_pod_runtimes()`` touches the cluster (claim/sandbox GETs + a
namespace Pod list). It lazy-imports nothing client-specific at module load, so the
offline tests and the stdlib-only renderer can import this module with no kubernetes
client present.

## Default-off / honest-by-construction

A scenario reads its own ``*_RUNTIME_CLASS`` knob (default ``""``). Unset -> every
function here is a no-op (``apply_runtime_class`` leaves the pod_spec byte-identical;
the consistency + verification calls are gated off by the caller), so the producer
lands INERT and a vanilla-kind run is never stranded Pending on an unschedulable Pod.
Set the knob on the matching cluster (``gvisor`` on a gke-sandbox cluster, ``kata`` on
the nested-virt pool) and the row becomes a REAL runtime-isolated number.

## Known runtime profiles (publishable GKE scheduling conventions)

  - ``gvisor`` — GKE Sandbox taints its gVisor pool ``sandbox.gke.io/runtime=<rc>``
    (``NoSchedule``); a pinned Pod tolerates it with ``operator=Exists`` (covers
    ``gvisor`` / ``gvisor-experimental`` alike). No nodeSelector needed.
  - ``kata``   — the nested-virt pool is tainted ``sandbox.gke.io/kata=true``
    (``NoSchedule``) and labeled ``nested-virtualization=enabled``; a pinned Pod
    tolerates the taint AND selects the label so it only lands on a nested-virt node.

These are generic Kubernetes scheduling primitives (taint keys, a node label) — no
cluster/project/customer identifiers — so they live honestly in the public producer.
An unknown non-empty runtime_class sets ``runtimeClassName`` only (no toleration /
nodeSelector); if it needs special scheduling the caller supplies it — a Pending pool
fails loud on the first fire rather than silently mis-publishing.
"""

from __future__ import annotations

import logging
from typing import Optional, Sequence

log = logging.getLogger("sandbox-scenario.runtime-class")

# Canonical runtime-class names.
GVISOR = "gvisor"
KATA = "kata"

# Per-runtime scheduling requirements. Keyed on the runtimeClassName string. Each
# entry is the (tolerations, node_selector) a Pod must carry to land on that runtime's
# (tainted) node pool. operator=Exists keys on the taint KEY only so any taint value
# is tolerated (e.g. gvisor / gvisor-experimental share the sandbox.gke.io/runtime key).
_RUNTIME_SCHEDULING: dict[str, dict] = {
    GVISOR: {
        "tolerations": [
            {
                "key": "sandbox.gke.io/runtime",
                "operator": "Exists",
                "effect": "NoSchedule",
            },
        ],
        "node_selector": {},
    },
    KATA: {
        "tolerations": [
            {
                "key": "sandbox.gke.io/kata",
                "operator": "Exists",
                "effect": "NoSchedule",
            },
        ],
        "node_selector": {"nested-virtualization": "enabled"},
    },
}

# substrate banner value -> the runtime_class that banner's isolation claim REQUIRES.
# A gke-sandbox banner asserts gVisor isolation, so the pinned runtime MUST be gvisor;
# a gke-kata banner asserts Kata+microVM isolation, so the pin MUST be kata; otherwise
# the published row would be a runc number wearing an isolation label. Only verifiable
# rules are seeded — other substrates (kind/gke) make no isolation claim, so they impose
# no constraint and a missing key is a no-op (never a fabricated rule).
_SUBSTRATE_REQUIRED_RUNTIME: dict[str, str] = {
    "gke-sandbox": GVISOR,
    "gke-kata": KATA,
}


def required_runtime_for_substrate(substrate: str) -> Optional[str]:
    """The runtime_class a substrate banner's isolation claim REQUIRES, or None.

    Single source of truth for both the pure consistency guard
    (``assert_substrate_runtime_consistency``) and the live runtime read-back gate in
    the producers: a substrate with a seeded rule (gke-sandbox -> gvisor, gke-kata ->
    kata) makes a verifiable runtime claim, so the bound-Pod runtime MUST be checked
    before its row publishes; a substrate with no rule (kind/gke) returns None and the
    producer skips the read-back (no isolation claim to verify, path stays read-free).
    """
    return _SUBSTRATE_REQUIRED_RUNTIME.get(substrate)


def resolve_scheduling(runtime_class: str) -> tuple[list[dict], dict]:
    """Return ``(tolerations, node_selector)`` a Pod needs for ``runtime_class``.

    Empty class -> ``([], {})`` (node default runtime, no scheduling additions). A
    known class returns COPIES of its profile (so a caller mutating the result never
    corrupts the shared registry). An unknown non-empty class returns ``([], {})`` —
    ``runtimeClassName`` will still be set by ``apply_runtime_class`` but no scheduling
    is invented; the caller owns any extra scheduling that class needs.
    """
    if not runtime_class:
        return ([], {})
    profile = _RUNTIME_SCHEDULING.get(runtime_class)
    if profile is None:
        log.warning(
            "runtime_class %r has no known scheduling profile — pinning "
            "runtimeClassName only (no toleration/nodeSelector added)",
            runtime_class,
        )
        return ([], {})
    tolerations = [dict(t) for t in profile["tolerations"]]
    node_selector = dict(profile["node_selector"])
    return (tolerations, node_selector)


def apply_runtime_class(pod_spec: dict, runtime_class: str) -> dict:
    """Pin ``runtime_class`` onto ``pod_spec`` (a Pod ``spec`` dict); return it.

    No-op when ``runtime_class`` is empty: the pod_spec is returned byte-identical, so
    an unset knob leaves the producer's manifest exactly as it was (vanilla-kind safe).
    When non-empty: sets ``runtimeClassName`` and MERGES the runtime's tolerations +
    nodeSelector — appending to any the caller already set rather than clobbering them
    (a toleration already present by key is not duplicated; existing nodeSelector keys
    win on conflict so a caller's explicit pin is never overwritten). Mutates and
    returns ``pod_spec`` in place (the manifest builders pass their dict straight in).
    """
    if not runtime_class:
        return pod_spec

    pod_spec["runtimeClassName"] = runtime_class
    tolerations, node_selector = resolve_scheduling(runtime_class)

    if tolerations:
        existing = pod_spec.get("tolerations") or []
        existing_keys = {t.get("key") for t in existing}
        merged = list(existing)
        for tol in tolerations:
            if tol.get("key") not in existing_keys:
                merged.append(tol)
        pod_spec["tolerations"] = merged

    if node_selector:
        existing_sel = dict(pod_spec.get("nodeSelector") or {})
        for k, v in node_selector.items():
            existing_sel.setdefault(k, v)  # caller's explicit pin wins
        pod_spec["nodeSelector"] = existing_sel

    return pod_spec


def assert_substrate_runtime_consistency(
    substrate: str, runtime_class: str,
) -> None:
    """Refuse a substrate banner whose isolation claim != the pinned runtime.

    Pure logic, no cluster calls. ``cluster_substrate`` (the run.py banner) and the
    pool's ``runtimeClassName`` are independent env vars with no cross-check, so a
    gke-sandbox substrate with an unset/non-gVisor runtime_class would publish runc
    Pods under a gVisor banner. Crash-FAIL (consistent with the scenarios' crash
    posture) before the cluster is touched, so the mismatch is caught fail-fast. A
    substrate with no seeded rule (kind/gke) imposes no constraint.
    """
    required = required_runtime_for_substrate(substrate)
    if required is not None and runtime_class != required:
        raise RuntimeError(
            f"runtime_class refuses a {substrate!r}-labeled result while "
            f"runtime_class={runtime_class!r} (expected {required!r}): the "
            f"cluster_substrate banner claims {required!r} isolation but the Pods "
            f"would run under runtime_class={runtime_class!r}, so the published row "
            f"would be a false {required!r} headline. Pin runtime_class={required!r} "
            f"on a {substrate!r} cluster."
        )


def classify_runtime_violations(
    observed: Sequence[tuple[str, Optional[str]]], expected_runtime_class: str,
) -> list[str]:
    """Map observed (sandbox_name, backing_runtime_class) pairs to violation strings.

    ``observed`` is one ``(sandbox_name, backing_runtime_class_or_None)`` per counted
    sandbox: ``None`` means the backing Pod's runtime could not be resolved (Pod not
    found / no runtime field), a string is its actual ``runtimeClassName``. Returns the
    list of violations (empty == every backing Pod ran the expected runtime). A
    None-or-mismatch is a violation — a counted sandbox we cannot PROVE ran the
    expected runtime must not pass for an honest runtime headline.
    """
    violations: list[str] = []
    for name, observed_rc in observed:
        if observed_rc is None:
            violations.append(f"{name}: backing Pod runtime could not be resolved")
        elif observed_rc != expected_runtime_class:
            violations.append(f"{name}: runtimeClassName={observed_rc!r}")
    return violations


def assert_no_runtime_violations(
    observed: Sequence[tuple[str, Optional[str]]],
    expected_runtime_class: str,
) -> int:
    """Crash-FAIL if any backing Pod betrays ``expected_runtime_class``; else return count.

    Pure (given the already-collected ``observed`` pairs) and offline-testable. The
    raise message mirrors burst_create's: a Ready sandbox running under the node
    default runtime is the silent isolation drop this read-back exists to catch.
    """
    violations = classify_runtime_violations(observed, expected_runtime_class)
    if violations:
        raise RuntimeError(
            f"runtime_class refuses to publish a {expected_runtime_class!r}-labeled "
            f"count: {len(violations)}/{len(observed)} bound sandboxes did not run "
            f"under RuntimeClass {expected_runtime_class!r} "
            f"[{'; '.join(violations)}]. A Ready sandbox running under a different "
            f"runtime is the silent isolation drop the runtime read-back catches."
        )
    return len(observed)


def verify_bound_pod_runtimes(
    custom,
    core,
    *,
    namespace: str,
    sandbox_names: Sequence[str],
    sandbox_gvr: tuple[str, str, str],
    expected_runtime_class: str,
) -> int:
    """Resolve each bound Sandbox -> its backing Pod -> runtimeClassName; assert.

    The ONE cluster-touching surface. For each Sandbox name it GETs the Sandbox to
    learn its uid, lists namespace Pods, and matches the backing Pod by owner-uid
    (convention-independent — no pod-name-shape or label-propagation assumption), then
    delegates the verdict to the pure ``assert_no_runtime_violations``. Runs
    post-measurement so it never perturbs the measured latency; the caller gates it to
    the substrate/runtime where a runtime headline is actually claimed. Returns the
    count verified; raises on ANY violation.
    """
    sbx_group, sbx_version, sbx_plural = sandbox_gvr

    uid_to_sandbox: dict[str, str] = {}
    for sbx_name in sandbox_names:
        sbx = custom.get_namespaced_custom_object(
            group=sbx_group, version=sbx_version, namespace=namespace,
            plural=sbx_plural, name=sbx_name,
        )
        uid = ((sbx or {}).get("metadata") or {}).get("uid")
        if uid:
            uid_to_sandbox[uid] = sbx_name

    pods = core.list_namespaced_pod(namespace=namespace)
    uid_to_pod: dict[str, object] = {}
    for pod in pods.items:
        for owner in (pod.metadata.owner_references or []):
            if owner.uid in uid_to_sandbox:
                uid_to_pod[owner.uid] = pod

    observed: list[tuple[str, Optional[str]]] = []
    for uid, sbx_name in uid_to_sandbox.items():
        pod = uid_to_pod.get(uid)
        observed.append(
            (sbx_name, pod.spec.runtime_class_name if pod is not None else None)
        )
    # Sandboxes whose uid was unreadable never made it into uid_to_sandbox; surface
    # them as unresolved so the count cannot silently shrink past the violation gate.
    resolved = set(uid_to_sandbox.values())
    for sbx_name in sandbox_names:
        if sbx_name not in resolved:
            observed.append((sbx_name, None))

    return assert_no_runtime_violations(observed, expected_runtime_class)
