"""Max-density saturation probe: how many sandboxes fit on ONE node (#3868).

Measures the Max Density matrix cell's INPUTS — it never publishes a number
itself. The render-designated density source is warmpool_cold_start, which emits
``density_per_vcpu`` only when the fire path supplies BOTH env inputs
(``BENCH_DENSITY_MAX_CONCURRENT`` + ``BENCH_DENSITY_ALLOCATABLE_VCPU_PER_NODE``,
"supplied by the fire path from the real saturation measurement"). This probe IS
that real saturation measurement: it packs warm sandboxes onto a single pinned
node until Ready plateaus against an unschedulable backlog, then prints the two
env exports for the canonical schema-validated re-fire. Publication rides the
canonical fire; the probe's own JSON report is an operator artifact.

## Measurement shape

1. Pick ONE runtime-capable target node (env-overridable) and read its
   ``status.allocatable.cpu`` — the LOCKED per-node allocatable denominator
   (see ``metrics.density_per_vcpu``: per-node allocatable sandbox-schedulable
   vCPU, NOT total-cluster capacity).
2. Create a SandboxTemplate pinned to that node (``kubernetes.io/hostname``
   nodeSelector — set BEFORE ``apply_runtime_class`` so the caller pin wins by
   the helper's merge contract) AND to the runtime under test.
3. Create ONE SandboxWarmPool at a replica CEILING deliberately above any
   plausible single-node fit (default 120 > the GKE kubelet max-pods 110), so
   the node itself — not our ask — is the binding constraint.
4. Poll the probe's pods: max_concurrent = Ready pods verified ON the target
   node UNDER the pinned runtime. Saturation = that count stable across a hold
   window WHILE >=1 probe pod sits Pending-Unschedulable (demand provably
   exceeded supply). All-replicas-Ready means the ceiling was too low — an
   honest NON-measurement (raise the ceiling and re-run), never a density.
5. Classify the binding constraint from the Unschedulable scheduling messages
   (Too many pods / Insufficient cpu / Insufficient memory) and record it in
   the report — a max-pods-bound density is a different claim than a CPU-bound
   one, and the page must be able to say which it measured.

## Honesty posture

- No density is ever reported for a non-saturated verdict (ceiling / timeout).
- Ready pods on the WRONG node or WRONG runtime are never counted; any present
  fail the run (the count would not be the labeled runtime's density).
- The per-pod resource requests in force are recorded in the report — density
  is a function of the sandbox footprint, so the number is reproducible only
  with its footprint stated.

## Crash posture

Infrastructure failures raise. The saturated/ceiling/timeout verdicts are
returned in the report; ``main`` exits 0 only for ``saturated``.
"""

from __future__ import annotations

try:  # package context (python3 -m harness.scenarios.density_probe)
    from . import runtime_class as rc
    from ._apiversion import ext_api_version, template_gvr, warmpool_gvr
    from ._kube import load_cluster_config
    from .. import metrics
except ImportError:  # standalone (dependency-free test from the scenarios/ dir)
    import runtime_class as rc
    from _apiversion import ext_api_version, template_gvr, warmpool_gvr
    from _kube import load_cluster_config
    import sys as _sys
    import pathlib as _pathlib

    _sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))
    import metrics

import json
import logging
import os
import time
import uuid

log = logging.getLogger("sandbox-density-probe")

_NAMESPACE = os.environ.get("BENCH_NAMESPACE", "default")
_PROBE_LABEL_KEY = "honest-bench/scenario"
_PROBE_LABEL_VALUE = "density-probe"

_TPL_GVR = template_gvr()
_SWP_GVR = warmpool_gvr()

# Runtime-capability node markers, keyed by FAMILY (resolve via rc.runtime_family).
# gVisor GKE Sandbox nodes carry the sandbox.gke.io/runtime=gvisor label; the Kata
# nested-virt pool is selected by the same nested-virtualization=enabled label the
# scheduling profile pins (runtime_class._RUNTIME_SCHEDULING).
_NODE_CAPABILITY_LABELS: dict[str, tuple[str, str]] = {
    rc.GVISOR: ("sandbox.gke.io/runtime", "gvisor"),
    rc.KATA: ("nested-virtualization", "enabled"),
}

# Binding-constraint classification tokens, checked in this order. The scheduler's
# aggregate Unschedulable message mixes the pinned node's true reason with every
# OTHER node's "didn't match ... node selector/affinity" (our hostname pin excludes
# them by design), so selector-mismatch classifies only when NO positive resource
# reason is present anywhere in the messages.
_CONSTRAINT_TOKENS: tuple[tuple[str, str], ...] = (
    ("Too many pods", "max-pods"),
    ("Insufficient cpu", "insufficient-cpu"),
    ("Insufficient memory", "insufficient-memory"),
)
_SELECTOR_MISMATCH_FRAGMENTS = ("node affinity", "node selector", "didn't match")


# ---------------------------------------------------------------------------
# Pure core
# ---------------------------------------------------------------------------

def parse_cpu_quantity(q: str) -> float:
    """Kubernetes CPU quantity -> vCPU float ("15890m" -> 15.89, "16" -> 16.0).

    Handles the n/u/m SI suffixes the API serializes for CPU. Raises ValueError
    on an empty or unparseable quantity — the denominator must never be guessed.
    """
    s = (q or "").strip()
    if not s:
        raise ValueError("empty CPU quantity")
    scale = 1.0
    if s.endswith("n"):
        scale, s = 1e-9, s[:-1]
    elif s.endswith("u"):
        scale, s = 1e-6, s[:-1]
    elif s.endswith("m"):
        scale, s = 1e-3, s[:-1]
    return float(s) * scale


def select_target_node(
    nodes: list[dict], runtime_class: str, override_name: str = "",
) -> dict:
    """Pick the single node the probe packs, from raw Node JSON dicts.

    Returns {"name", "allocatable_vcpu", "allocatable_pods"}. With
    ``override_name`` set, that node is used (raising if absent — an explicit
    pin that silently fell back would measure the wrong node). Otherwise the
    first schedulable node carrying the runtime family's capability label wins.
    Raises when no candidate exists: a probe with no capable node has nothing
    honest to measure.
    """
    fam = rc.runtime_family(runtime_class)
    cap = _NODE_CAPABILITY_LABELS.get(fam)
    if cap is None:
        raise ValueError(
            f"runtime_class {runtime_class!r} has no node-capability marker — "
            f"the probe only measures runtime-labeled matrix rows (gvisor/kata)"
        )

    chosen = None
    for node in nodes:
        meta = node.get("metadata") or {}
        name = meta.get("name") or ""
        if override_name:
            if name == override_name:
                chosen = node
                break
            continue
        if (node.get("spec") or {}).get("unschedulable"):
            continue
        labels = meta.get("labels") or {}
        if labels.get(cap[0]) == cap[1]:
            chosen = node
            break
    if chosen is None:
        target = override_name or f"a schedulable node with {cap[0]}={cap[1]}"
        raise RuntimeError(f"no target node found: wanted {target}")

    alloc = ((chosen.get("status") or {}).get("allocatable")) or {}
    return {
        "name": (chosen.get("metadata") or {}).get("name"),
        "allocatable_vcpu": parse_cpu_quantity(alloc.get("cpu", "")),
        "allocatable_pods": int(alloc.get("pods", 0)),
    }


def build_probe_template_manifest(
    template_name: str,
    *,
    node_name: str,
    runtime_class: str,
    image: str,
    namespace: str = _NAMESPACE,
) -> dict:
    """SandboxTemplate pinned to ONE node + the runtime under test.

    The hostname nodeSelector is set BEFORE apply_runtime_class — the helper's
    merge contract keeps caller-set keys (setdefault), so the pin survives and
    the runtime's own selector/toleration are merged alongside. The podTemplate
    carries the probe label so the controller-created pods are identifiable.
    """
    pod_spec = {
        "nodeSelector": {"kubernetes.io/hostname": node_name},
        "containers": [
            {
                "name": "sandbox",
                "image": image,
                "imagePullPolicy": "IfNotPresent",
                "command": ["sh", "-c", "sleep 3600"],
                "resources": rc.container_resources_from_env(runtime_class),
            },
        ],
        "restartPolicy": "Never",
    }
    rc.apply_runtime_class(pod_spec, runtime_class)
    return {
        "apiVersion": ext_api_version(),
        "kind": "SandboxTemplate",
        "metadata": {
            "name": template_name,
            "namespace": namespace,
            "labels": {_PROBE_LABEL_KEY: _PROBE_LABEL_VALUE},
        },
        "spec": {
            "podTemplate": {
                "metadata": {
                    "labels": {_PROBE_LABEL_KEY: _PROBE_LABEL_VALUE},
                },
                "spec": pod_spec,
            },
        },
    }


def build_probe_warmpool_manifest(
    pool_name: str, template_name: str, replicas: int,
    *, namespace: str = _NAMESPACE,
) -> dict:
    """SandboxWarmPool at the oversubscription ceiling."""
    return {
        "apiVersion": ext_api_version(),
        "kind": "SandboxWarmPool",
        "metadata": {
            "name": pool_name,
            "namespace": namespace,
            "labels": {_PROBE_LABEL_KEY: _PROBE_LABEL_VALUE},
        },
        "spec": {
            "replicas": replicas,
            "sandboxTemplateRef": {"name": template_name},
        },
    }


def is_probe_pod(pod: dict, *, node_name: str) -> bool:
    """Identify the probe's pods among raw Pod JSON dicts.

    Primary: the probe label propagated from the podTemplate. Fallback (in case
    a controller version does not propagate template labels): the probe's
    unique hostname pin in spec.nodeSelector. Either marks the pod ours.
    """
    labels = ((pod.get("metadata") or {}).get("labels")) or {}
    if labels.get(_PROBE_LABEL_KEY) == _PROBE_LABEL_VALUE:
        return True
    sel = ((pod.get("spec") or {}).get("nodeSelector")) or {}
    return sel.get("kubernetes.io/hostname") == node_name


def _pod_condition(pod: dict, cond_type: str) -> dict:
    for c in ((pod.get("status") or {}).get("conditions")) or []:
        if c.get("type") == cond_type:
            return c
    return {}


def count_target_pods(
    pods: list[dict], *, node_name: str, runtime_class: str,
) -> dict:
    """Classify the probe's pods for the saturation decision (pure).

    Returns {"ready_on_target", "pending_unschedulable_msgs", "wrong_node",
    "wrong_runtime", "other"}. ready_on_target counts ONLY pods that are
    Running+Ready AND on the target node AND under the pinned runtime — the
    honest max_concurrent numerator. A Ready pod elsewhere or under another
    runtime is a violation counter, never silently counted OR dropped.
    """
    ready_on_target = 0
    wrong_node = 0
    wrong_runtime = 0
    other = 0
    pending_msgs: list[str] = []
    for pod in pods:
        if not is_probe_pod(pod, node_name=node_name):
            continue
        spec = pod.get("spec") or {}
        status = pod.get("status") or {}
        phase = status.get("phase")
        is_ready = (
            phase == "Running"
            and _pod_condition(pod, "Ready").get("status") == "True"
        )
        if is_ready:
            if spec.get("nodeName") != node_name:
                wrong_node += 1
            elif spec.get("runtimeClassName") != runtime_class:
                wrong_runtime += 1
            else:
                ready_on_target += 1
            continue
        if phase == "Pending":
            sched = _pod_condition(pod, "PodScheduled")
            if sched.get("status") == "False" and sched.get("reason") == "Unschedulable":
                pending_msgs.append(sched.get("message") or "")
                continue
        other += 1
    return {
        "ready_on_target": ready_on_target,
        "pending_unschedulable_msgs": pending_msgs,
        "wrong_node": wrong_node,
        "wrong_runtime": wrong_runtime,
        "other": other,
    }


def classify_binding_constraints(messages: list[str]) -> list[str]:
    """Distinct binding-constraint classes from Unschedulable messages (pure).

    Positive resource reasons (max-pods / insufficient-cpu / insufficient-memory)
    win; selector-mismatch classifies only when no positive reason appears at all
    (the aggregate message always names other nodes' selector mismatches — that is
    the hostname pin working, not a constraint). Empty input or no recognized
    token -> ["unknown"].
    """
    found: list[str] = []
    blob = "\n".join(messages)
    for token, cls in _CONSTRAINT_TOKENS:
        if token in blob and cls not in found:
            found.append(cls)
    if found:
        return found
    if any(f in blob for f in _SELECTOR_MISMATCH_FRAGMENTS):
        return ["node-selector-mismatch"]
    return ["unknown"]


def saturation_verdict(
    ready_history: list[int],
    *,
    hold_checks: int,
    pending_unschedulable: int,
    replicas: int,
) -> str:
    """Saturation decision over the polled ready-count history (pure).

    - "all-ready-ceiling": every replica went Ready — the ceiling, not the node,
      bound the packing; an honest non-measurement.
    - "saturated": the last ``hold_checks`` observations are identical, non-zero,
      AND >=1 pod is provably Unschedulable — supply plateaued under excess
      demand.
    - "in-progress": anything else (still climbing, or backlog not yet visible).
    """
    ready_now = ready_history[-1] if ready_history else 0
    if ready_now >= replicas:
        return "all-ready-ceiling"
    if (
        len(ready_history) >= hold_checks
        and ready_now > 0
        and pending_unschedulable > 0
        and len(set(ready_history[-hold_checks:])) == 1
    ):
        return "saturated"
    return "in-progress"


def assemble_density_report(
    *,
    verdict: str,
    max_concurrent: int,
    allocatable_vcpu: float,
    allocatable_pods: int,
    node_name: str,
    runtime_class: str,
    constraints: list[str],
    pod_resources: dict,
    counts: dict,
    poll_history: list[int],
) -> dict:
    """The probe's operator-facing report (pure).

    density_per_vcpu (via the LOCKED metrics core) and the two canonical-fire
    env exports are present ONLY on a saturated verdict — a ceiling/timeout run
    has no honest max and must never leak a publishable number.
    """
    report = {
        "probe": "density-probe",
        "verdict": verdict,
        "node": node_name,
        "runtime_class": runtime_class,
        "allocatable_vcpu": allocatable_vcpu,
        "allocatable_pods": allocatable_pods,
        "pod_resources": pod_resources,
        "counts": counts,
        "poll_history_tail": poll_history[-20:],
    }
    if verdict == "saturated":
        report["max_concurrent"] = max_concurrent
        report["binding_constraints"] = constraints
        report["density_per_vcpu"] = metrics.density_per_vcpu(
            max_concurrent, allocatable_vcpu,
        )
        report["canonical_fire_env"] = {
            "BENCH_DENSITY_MAX_CONCURRENT": str(max_concurrent),
            "BENCH_DENSITY_ALLOCATABLE_VCPU_PER_NODE": str(allocatable_vcpu),
        }
    return report


# ---------------------------------------------------------------------------
# Thin I/O runner
# ---------------------------------------------------------------------------

def _raw_json(resp) -> dict:
    return json.loads(resp.data)


def _list_probe_pods_raw(core_v1, namespace: str) -> list[dict]:
    resp = core_v1.list_namespaced_pod(namespace, _preload_content=False)
    return (_raw_json(resp).get("items")) or []


def run_probe() -> dict:
    """Execute the saturation probe against the live cluster; return the report."""
    from kubernetes import client as k8s_client

    runtime_class = os.environ.get("DENSITY_PROBE_RUNTIME_CLASS", "")
    substrate = os.environ.get("BENCH_CLUSTER_SUBSTRATE", "kind")
    override_node = os.environ.get("DENSITY_PROBE_TARGET_NODE", "")
    ceiling = int(os.environ.get("DENSITY_PROBE_REPLICA_CEILING", "120"))
    hold_s = int(os.environ.get("DENSITY_PROBE_HOLD_S", "90"))
    poll_s = int(os.environ.get("DENSITY_PROBE_POLL_S", "10"))
    timeout_s = int(os.environ.get("DENSITY_PROBE_TIMEOUT_S", "1200"))
    image = os.environ.get("DENSITY_PROBE_IMAGE", "busybox:1.36")
    if not runtime_class:
        raise SystemExit(
            "DENSITY_PROBE_RUNTIME_CLASS is required (gvisor|kata...) — the "
            "density cell is a per-runtime matrix row"
        )
    rc.assert_substrate_runtime_consistency(substrate, runtime_class)
    hold_checks = max(2, hold_s // max(poll_s, 1))

    load_cluster_config()
    core_v1 = k8s_client.CoreV1Api()
    custom = k8s_client.CustomObjectsApi()

    nodes = (_raw_json(core_v1.list_node(_preload_content=False)).get("items")) or []
    target = select_target_node(nodes, runtime_class, override_node)
    pod_resources = rc.container_resources_from_env(runtime_class)
    log.info(
        "target node %s (allocatable %.2f vCPU, %d pods); ceiling=%d hold=%ds",
        target["name"], target["allocatable_vcpu"], target["allocatable_pods"],
        ceiling, hold_s,
    )

    suffix = uuid.uuid4().hex[:8]
    template_name = f"density-tmpl-{suffix}"
    pool_name = f"density-pool-{suffix}"
    custom.create_namespaced_custom_object(
        group=_TPL_GVR[0], version=_TPL_GVR[1], namespace=_NAMESPACE,
        plural=_TPL_GVR[2],
        body=build_probe_template_manifest(
            template_name, node_name=target["name"],
            runtime_class=runtime_class, image=image,
        ),
    )
    custom.create_namespaced_custom_object(
        group=_SWP_GVR[0], version=_SWP_GVR[1], namespace=_NAMESPACE,
        plural=_SWP_GVR[2],
        body=build_probe_warmpool_manifest(pool_name, template_name, ceiling),
    )

    history: list[int] = []
    counts: dict = {}
    verdict = "timeout"
    deadline = time.monotonic() + timeout_s
    try:
        while time.monotonic() < deadline:
            time.sleep(poll_s)
            pods = _list_probe_pods_raw(core_v1, _NAMESPACE)
            counts = count_target_pods(
                pods, node_name=target["name"], runtime_class=runtime_class,
            )
            history.append(counts["ready_on_target"])
            log.info(
                "ready_on_target=%d pending_unschedulable=%d wrong_node=%d "
                "wrong_runtime=%d other=%d",
                counts["ready_on_target"],
                len(counts["pending_unschedulable_msgs"]),
                counts["wrong_node"], counts["wrong_runtime"], counts["other"],
            )
            v = saturation_verdict(
                history, hold_checks=hold_checks,
                pending_unschedulable=len(counts["pending_unschedulable_msgs"]),
                replicas=ceiling,
            )
            if v != "in-progress":
                verdict = v
                break
    finally:
        _cleanup(custom, pool_name=pool_name, template_name=template_name)

    if counts.get("wrong_runtime") or counts.get("wrong_node"):
        raise RuntimeError(
            f"runtime/node pin violated: wrong_runtime={counts.get('wrong_runtime')} "
            f"wrong_node={counts.get('wrong_node')} — count would not be the "
            f"labeled runtime's single-node density"
        )

    return assemble_density_report(
        verdict=verdict,
        max_concurrent=history[-1] if history else 0,
        allocatable_vcpu=target["allocatable_vcpu"],
        allocatable_pods=target["allocatable_pods"],
        node_name=target["name"],
        runtime_class=runtime_class,
        constraints=classify_binding_constraints(
            counts.get("pending_unschedulable_msgs") or []
        ),
        pod_resources=pod_resources,
        counts={k: (len(v) if k == "pending_unschedulable_msgs" else v)
                for k, v in counts.items()},
        poll_history=history,
    )


def _cleanup(custom, *, pool_name: str, template_name: str) -> None:
    """Best-effort delete: pool, then template."""
    from kubernetes.client.exceptions import ApiException
    for (label, gvr, name) in (
        ("warmpool", _SWP_GVR, pool_name),
        ("template", _TPL_GVR, template_name),
    ):
        group, version, plural = gvr
        try:
            custom.delete_namespaced_custom_object(
                group=group, version=version, namespace=_NAMESPACE,
                plural=plural, name=name,
            )
        except ApiException as e:
            if e.status != 404:
                log.warning("cleanup: delete %s %s failed: %s", label, name, e)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    report = run_probe()
    print(json.dumps(report, indent=2, sort_keys=True))
    if report["verdict"] == "saturated":
        env = report["canonical_fire_env"]
        log.info(
            "canonical re-fire inputs: BENCH_DENSITY_MAX_CONCURRENT=%s "
            "BENCH_DENSITY_ALLOCATABLE_VCPU_PER_NODE=%s",
            env["BENCH_DENSITY_MAX_CONCURRENT"],
            env["BENCH_DENSITY_ALLOCATABLE_VCPU_PER_NODE"],
        )
        return 0
    log.error("probe did not saturate (verdict=%s) — no density measured", report["verdict"])
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
