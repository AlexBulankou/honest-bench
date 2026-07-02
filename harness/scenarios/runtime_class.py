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

## Family normalization (concrete hypervisor variant -> profile family)

A concrete ``runtimeClassName`` is usually a per-hypervisor variant, not the bare
family name: kata-deploy installs ``kata-clh`` (Cloud Hypervisor) and ``kata-qemu``
(QEMU), and GKE Sandbox can expose ``gvisor`` / ``gvisor-experimental``. Every kata
hypervisor lands on the SAME nested-virt pool with the SAME taint+label, and every
gVisor variant tolerates the SAME GKE-Sandbox taint — so SCHEDULING and the
substrate-isolation claim key on the FAMILY (``runtime_family()`` maps ``kata-clh`` /
``kata-qemu`` -> ``kata`` and ``gvisor-experimental`` -> ``gvisor``). The bound-Pod
runtime read-back stays EXACT: a Pod pinned ``kata-clh`` that silently fell back to
``kata-qemu`` is still a violation (same family, wrong hypervisor — the honest headline
names the hypervisor it measured). An unknown non-empty runtime_class is its own family
(no profile): it sets ``runtimeClassName`` only (no toleration / nodeSelector); if it
needs special scheduling the caller supplies it — a Pending pool fails loud on the
first fire rather than silently mis-publishing.
"""

from __future__ import annotations

import logging
from typing import Optional, Sequence

log = logging.getLogger("sandbox-scenario.runtime-class")

# Canonical runtime-class FAMILY names. Concrete runtimeClassNames are per-hypervisor
# variants (kata-clh / kata-qemu; gvisor / gvisor-experimental) that normalize to one of
# these families via runtime_family() for scheduling + the substrate-isolation claim.
GVISOR = "gvisor"
KATA = "kata"

# Recognised families, longest-first is irrelevant (no family is a prefix of another),
# but order is fixed for determinism. A concrete class belongs to family F iff it equals
# F or starts with "F-" (the variant suffix). Used by runtime_family().
_RUNTIME_FAMILIES: tuple[str, ...] = (KATA, GVISOR)


def runtime_family(runtime_class: str) -> str:
    """Normalize a concrete runtimeClassName to its scheduling/profile FAMILY.

    ``kata-clh`` / ``kata-qemu`` -> ``kata``; ``gvisor-experimental`` -> ``gvisor``;
    the bare family name maps to itself. Empty -> empty. An unrecognised non-empty class
    is returned unchanged (it is its own family — no profile, no isolation rule). This is
    the single seam that lets a per-hypervisor pin (what kata-deploy actually installs)
    resolve the family scheduling profile + satisfy the family isolation claim, while the
    bound-Pod read-back stays EXACT (it compares the concrete class, not the family).
    """
    if not runtime_class:
        return ""
    for fam in _RUNTIME_FAMILIES:
        if runtime_class == fam or runtime_class.startswith(fam + "-"):
            return fam
    return runtime_class


# Per-runtime-FAMILY scheduling requirements. Keyed on the family name (resolve via
# runtime_family() so a kata-clh / kata-qemu / gvisor-experimental pin finds its profile).
# Each entry is the (tolerations, node_selector) a Pod must carry to land on that family's
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
    known class (resolved by FAMILY, so kata-clh / kata-qemu / gvisor-experimental all
    find their profile) returns COPIES of its profile (so a caller mutating the result
    never corrupts the shared registry). An unknown non-empty class returns ``([], {})``
    — ``runtimeClassName`` will still be set by ``apply_runtime_class`` but no scheduling
    is invented; the caller owns any extra scheduling that class needs.
    """
    if not runtime_class:
        return ([], {})
    profile = _RUNTIME_SCHEDULING.get(runtime_family(runtime_class))
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


# Per-runtime-FAMILY default container resources (#3942/#830). gVisor and the node-default
# runtime run the workload as an ordinary (heavily over-committable) container, so the
# canonical tiny footprint is correct and keeps a vanilla-kind / gVisor manifest
# byte-identical to its pre-#3942 shape. Kata sizes the GUEST microVM from the Pod's
# cpu+memory, so a tiny request SIGKILLs (137) the in-guest container the instant it starts
# despite the sandbox reaching Ready (confirmed kata-clh + kata-qemu, #3942) — the kata
# family therefore needs a guest-sane floor. Keyed by FAMILY (resolve via runtime_family
# so kata-clh / kata-qemu both find the kata floor); an absent family falls back to tiny.
_TINY_RESOURCES: dict = {
    "requests": {"cpu": "10m", "memory": "16Mi"},
    "limits": {"cpu": "100m", "memory": "64Mi"},
}
_KATA_RESOURCES: dict = {
    "requests": {"cpu": "500m", "memory": "512Mi"},
    "limits": {"cpu": "1", "memory": "1Gi"},
}
_RUNTIME_RESOURCES: dict[str, dict] = {KATA: _KATA_RESOURCES}


def container_resources(
    runtime_class: str,
    *,
    cpu_request: Optional[str] = None,
    mem_request: Optional[str] = None,
    cpu_limit: Optional[str] = None,
    mem_limit: Optional[str] = None,
) -> dict:
    """Return the container ``resources`` dict for ``runtime_class`` (pure).

    Runtime-family-aware defaults: the kata family (kata-clh / kata-qemu, resolved via
    ``runtime_family``) gets a guest-sane floor because kata sizes the microVM from the
    Pod's cpu+memory — a tiny request SIGKILLs (137) the in-guest container despite Ready
    (#3942). Every other family (gVisor, and the node-default runtime for an unset/unknown
    class) gets the canonical tiny footprint, so a vanilla-kind / gVisor manifest is
    byte-identical to its pre-#3942 shape. Each of the four fields is independently
    overridable (a fire that needs a different box passes the value); an override of
    ``None`` keeps the family default. Returns a fresh nested dict so a caller mutating the
    result never corrupts the registry.
    """
    base = _RUNTIME_RESOURCES.get(runtime_family(runtime_class), _TINY_RESOURCES)
    resources = {
        "requests": dict(base["requests"]),
        "limits": dict(base["limits"]),
    }
    if cpu_request is not None:
        resources["requests"]["cpu"] = cpu_request
    if mem_request is not None:
        resources["requests"]["memory"] = mem_request
    if cpu_limit is not None:
        resources["limits"]["cpu"] = cpu_limit
    if mem_limit is not None:
        resources["limits"]["memory"] = mem_limit
    return resources


def container_resources_from_env(runtime_class: str) -> dict:
    """Thin env wrapper over ``container_resources`` — the shared matrix-scenario entry.

    Reads the four shared, un-prefixed ``BENCH_POD_*`` knobs (same names across every
    matrix scenario, because the resource floor is a property of the RUNTIME, not the
    scenario) and delegates to the pure core. An unset knob -> ``None`` -> the
    runtime-family default, so a fire that just sets its ``*_RUNTIME_CLASS=kata-clh``
    automatically gets the kata floor with no extra env, while a fire that needs a bigger
    box tunes any field. The only env-reading function in the module (mirrors the
    pure-core / thin-I/O split above); ``os`` is imported locally to keep module load
    free of it.
    """
    import os

    def _opt(name: str) -> Optional[str]:
        v = os.environ.get(name, "").strip()
        return v or None

    return container_resources(
        runtime_class,
        cpu_request=_opt("BENCH_POD_CPU_REQUEST"),
        mem_request=_opt("BENCH_POD_MEM_REQUEST"),
        cpu_limit=_opt("BENCH_POD_CPU_LIMIT"),
        mem_limit=_opt("BENCH_POD_MEM_LIMIT"),
    )


def assert_substrate_runtime_consistency(
    substrate: str, runtime_class: str,
) -> None:
    """Refuse a substrate banner whose isolation claim != the pinned runtime.

    Pure logic, no cluster calls. ``cluster_substrate`` (the run.py banner) and the
    pool's ``runtimeClassName`` are independent env vars with no cross-check, so a
    gke-sandbox substrate with an unset/non-gVisor runtime_class would publish runc
    Pods under a gVisor banner. Crash-FAIL (consistent with the scenarios' crash
    posture) before the cluster is touched, so the mismatch is caught fail-fast. A
    substrate with no seeded rule (kind/gke) imposes no constraint. The claim is checked
    by FAMILY (``runtime_family()``), so a gke-kata banner is satisfied by ANY kata
    hypervisor pin (``kata-clh`` / ``kata-qemu``) and a gke-sandbox banner by any gVisor
    variant — the isolation claim is the family, not the hypervisor; which hypervisor
    actually ran is enforced exactly by the bound-Pod read-back, not here.
    """
    required = required_runtime_for_substrate(substrate)
    if required is not None and runtime_family(runtime_class) != required:
        raise RuntimeError(
            f"runtime_class refuses a {substrate!r}-labeled result while "
            f"runtime_class={runtime_class!r} (expected the {required!r} family): the "
            f"cluster_substrate banner claims {required!r} isolation but the Pods "
            f"would run under runtime_class={runtime_class!r}, so the published row "
            f"would be a false {required!r} headline. Pin a {required!r}-family "
            f"runtime_class on a {substrate!r} cluster."
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
    settle_retries: int = 3,
    settle_sleep_s: float = 2.0,
) -> int:
    """Resolve each bound Sandbox -> its backing Pod -> runtimeClassName; assert.

    The ONE cluster-touching surface. For each Sandbox name it GETs the Sandbox to
    learn its uid, lists namespace Pods, and matches the backing Pod by owner-uid
    (convention-independent — no pod-name-shape or label-propagation assumption), then
    delegates the verdict to the pure ``assert_no_runtime_violations``. Runs
    post-measurement so it never perturbs the measured latency; the caller gates it to
    the substrate/runtime where a runtime headline is actually claimed. Returns the
    count verified; raises on ANY violation.

    Bounded settle+retry on the UNRESOLVED (None) subset only. A Ready+bound sandbox
    whose backing Pod cannot yet be matched by owner-uid is almost always owner-ref
    propagation lag — a warm-pool pod re-parents from the SandboxWarmPool to the
    Sandbox during the claim->active conversion, and a read-back fired immediately
    after bind can catch a fraction of the pods mid-flip (observed ~10/300 at warm-500
    burst). That is NOT the silent isolation drop this gate exists to catch: a runc
    fallback surfaces a WRONG ``runtimeClassName`` (not None), so it is never retried
    and still fails immediately at the assert below. Re-resolving just the None subset
    a few times lets the propagation race settle before a None is treated as terminal,
    while the conservative "unresolved counts as a violation" guarantee is preserved
    for pods that stay unresolvable past the retries.
    """
    sbx_group, sbx_version, sbx_plural = sandbox_gvr

    def _resolve(names: Sequence[str]) -> dict[str, Optional[str]]:
        """One read pass for ``names`` -> {name: runtimeClassName or None}."""
        uid_to_sandbox: dict[str, str] = {}
        for sbx_name in names:
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

        out: dict[str, Optional[str]] = {}
        for uid, sbx_name in uid_to_sandbox.items():
            pod = uid_to_pod.get(uid)
            out[sbx_name] = pod.spec.runtime_class_name if pod is not None else None
        # Sandboxes whose uid was unreadable never made it into uid_to_sandbox; surface
        # them as unresolved so the count cannot silently shrink past the violation gate.
        for sbx_name in names:
            out.setdefault(sbx_name, None)
        return out

    observed_map = _resolve(sandbox_names)
    for _ in range(max(0, settle_retries)):
        unresolved = [n for n, runtime in observed_map.items() if runtime is None]
        if not unresolved:
            break
        import time as _time

        _time.sleep(settle_sleep_s)
        observed_map.update(_resolve(unresolved))

    observed = [(name, observed_map[name]) for name in sandbox_names]
    return assert_no_runtime_violations(observed, expected_runtime_class)
