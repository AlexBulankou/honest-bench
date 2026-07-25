"""Resource-limit enforcement: does the runtime CONFINE a sandbox to its declared footprint?

The Max-Density headline (gVisor 5.98 sb/vCPU, Kata+microVM 1.26 sb/vCPU) is
derived as count-of-Ready ÷ total cluster vCPU, measured at a DECLARED
per-sandbox cpu/mem request footprint (#3868 records that footprint as the
reproducibility qualifier). The honesty gap this axis closes: **declared ≠
enforced.** A sandbox reaches Ready when the scheduler fits its *requests* and
the container starts — that says nothing about whether the runtime HOLDS the
sandbox to that footprint under load. If a sandbox can silently exceed its
declared cpu/mem, the density figure is an optimistic *packing* claim, not a
*capacity-under-load* guarantee. This cell verifies the runtime actually enforces
the footprint the density number is measured at — the same claimed-vs-enforced
honesty pattern honest-bench already applied when it flipped the NetworkPolicy
cells from control-plane (admission-accepted) to dataplane-enforced/FAIL (#3950).

## The per-runtime angle (why the axis earns its keep)

Enforcement mechanism differs by runtime, so this axis makes the 5.98-vs-1.26
gap's *robustness* explicit rather than implied:

  - **gVisor** — sandboxes are `runsc` userspace-kernel processes under host
    cgroup limits. A memory overshoot should trigger a cgroup OOM-kill of the
    runsc process (container OOMKilled, exit 137). gVisor packs ~5x denser, so
    the honest question is exactly: does a runaway sandbox get contained
    (protecting its ~5 neighbors) or does it degrade the node?
  - **Kata + microVM** — each sandbox is a real VM with a hypervisor-hard
    memory/vCPU allocation. Enforcement is a hardware-ish boundary, so the 1.26
    figure is inherently capacity-guaranteed; this cell verifies the guest
    contains the overshoot rather than the host node.

## The probe — a bounded controlled overshoot (why the signal is unambiguous)

The container's OWN command IS the probe: it attempts to allocate a FIXED amount
`A` (RLIMIT_OVERSHOOT_ALLOC_MIB, default 256Mi) of real, page-touched anonymous
memory, while the podTemplate declares `limits.memory = L`
(RLIMIT_OVERSHOOT_LIMIT_MIB, default 64Mi). `A` is chosen so that `L < A <<
node_memory`:

  - **Enforced** — the memory cgroup caps the process at `L`; the allocation of
    `A > L` trips the cgroup OOM-killer BEFORE it completes → the container
    terminates `reason=OOMKilled` / `exitCode=137`. **Contained → PASS.**
  - **NOT enforced** — the limit is ignored/stripped; `A` (a mere 256Mi) fits
    trivially on the node, so the allocation SUCCEEDS and the process exits
    cleanly → `exitCode=0`. The sandbox exceeded its declared footprint and
    lived. **Breach → FAIL.**

Choosing `A << node_memory` is what makes exit 137 UNAMBIGUOUS: in the
unenforced case the allocation exits 0 long before it could ever approach node
capacity, so a node-level OOM (which would also read 137) never occurs — an OOM
here can only be the cgroup limit doing its job. This is the design insight that
lets a single K8s-API-observable signal (terminated reason / exit code) decide
the verdict for BOTH runtimes without an in-sandbox cgroup read (the gVisor
sentry's cgroup view differs from the host and is unreliable to read in-pod).

CPU CFS-throttle containment is the documented next iteration (same deferral
shape as gvisor_canary's in-kernel-signature probe): reading `nr_throttled` from
inside a gVisor sandbox is not host-faithful, so the MVP badge is the
memory-overshoot axis, which is host-cgroup-observable end-to-end.

## Split: pure core vs. thin I/O (mirrors netpol_probe / ttfe_probe)

Everything that decides the verdict is pure and fully offline-testable:

  - ``overshoot_probe_enabled()``  — the default-off arm-gate flag.
  - ``memory_overshoot_command()`` — the canonical allocate-A-and-exit argv.
  - ``classify_containment()``     — map (terminated reason, exit code) to
                                     contained / breach / inconclusive.
  - ``classify_enforcement()``     — map containment to (outcome, badge_scope,
                                     badge_construction).

Only ``run()``'s deploy/wait/cleanup body touches the cluster, and it
lazy-imports ``kubernetes`` AND the sibling ``._apiversion`` / ``._kube`` helpers
INSIDE the call, so importing this module (offline tests, stdlib-only renderer)
is a pure-stdlib operation that never needs the client. Keeping the relative
imports lazy (rather than at module load, as gvisor_canary does) is what lets the
pure core be exercised by a standalone ``python3 test_…py`` run with no package
context.

## Default-off arm gate (BENCH_RLIMIT_OVERSHOOT_PROBE)

Gated behind ``BENCH_RLIMIT_OVERSHOOT_PROBE`` (default-off), the same posture as
netpol_probe's ``BENCH_NETPOL_DATAPLANE_PROBE``. Unset ⇒ ``run()`` is a pure
stdlib no-op that reports ``pending (not-yet-measured)`` and touches NO cluster —
the satisfied-substrate stub state. This is the correct substrate-scoped default,
not a dormant feature: a kind/CI-unit run has no gVisor/Kata runtime whose
enforcement there would be meaningful, and the substrate gate
(``requires_substrate``) already short-circuits this module out of the kind
artifact entirely (run._run_one pends requires-gvisor-runtime /
requires-kata-runtime WITHOUT importing this module). The coordinated fill-fire
arms the gate explicitly for one run on the real gVisor/Kata substrate (#5634).

## Honest failure posture

An armed probe that cannot classify its outcome is INCONCLUSIVE — it degrades to
``pending (overshoot-inconclusive)``, NEVER a fabricated enforcement claim and
never a false breach. The only states that move the badge are a clean cgroup
OOM-kill (``enforced``) or a clean over-limit allocation that survived (FAIL). A
provisioning/infra failure (backing Pod never located, no terminal container
state within the window) RAISES — the harness loop classifies a raised exception
as a crash-fail cell, distinct from a real breach result.

## Why a badge, not a latency

Like gvisor_canary and the netpol #3950 split, this is a binary
correctness/honesty assertion (confined vs not), not a perf number — so a PASS
renders an ``enforced`` badge, not a millisecond figure. The badge_construction
value ``footprint-overshoot`` discloses WHICH mechanism produced the enforced
claim (a controlled memory overshoot), the resource-limit sibling of the netpol
``standard-np`` disclosure. The badge renders only on PASS, and emitting a naked
``badge_scope="enforced"`` without its construction would trip the #4051
over-claim guard — so the two are emitted as one atomic pair. A pending or FAIL
outcome emits no badge at all.
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Optional

log = logging.getLogger("sandbox-scenario.resource-limit-enforcement")

# Bounded-enum pending reasons (⊆ render's PENDING_REASONS allow-list).
#   not-yet-measured     — the arm gate is off (the satisfied-substrate stub): the
#                          cell is registered and the substrate gate is met, but the
#                          overshoot probe was not armed this run. Distinct from the
#                          kind-side requires-*-runtime short-circuit run._run_one
#                          emits before this module is ever imported.
#   overshoot-inconclusive — the probe WAS armed and ran, but the container reached a
#                          terminal state that classifies as neither a clean cgroup
#                          OOM-kill nor a clean over-limit survival (e.g. a non-OOM
#                          crash / unexpected exit). Honest-empty, never a fabricated
#                          badge or a false breach.
_PENDING_NOT_MEASURED = "not-yet-measured"
_PENDING_INCONCLUSIVE = "overshoot-inconclusive"

# Default-off arm gate (mirrors netpol_probe.BENCH_NETPOL_DATAPLANE_PROBE).
_PROBE_ENV = "BENCH_RLIMIT_OVERSHOOT_PROBE"

_NAMESPACE = os.environ.get("BENCH_NAMESPACE", "default")

# A python image so the overshoot command can force real anonymous RSS (bytearray
# + per-page touch) — the thing a memory cgroup limit governs. busybox has no
# portable anonymous-allocation primitive; netpol_probe similarly assumes `nc`.
_SANDBOX_IMAGE = os.environ.get("RLIMIT_OVERSHOOT_IMAGE", "python:3.12-alpine")

# The RuntimeClass the sandbox requests. Default "gvisor"; env-tunable so the same
# probe fires against a kata RuntimeClass on the Kata leg of the coordinated fire.
_RUNTIME_CLASS = os.environ.get("RLIMIT_OVERSHOOT_RUNTIME_CLASS", "gvisor")

# Declared footprint L and the overshoot allocation A. Invariant L < A << node_mem
# (see module docstring) is what makes exit 137 an unambiguous cgroup-OOM signal.
_LIMIT_MIB = int(os.environ.get("RLIMIT_OVERSHOOT_LIMIT_MIB", "64"))
_OVERSHOOT_MIB = int(os.environ.get("RLIMIT_OVERSHOOT_ALLOC_MIB", "256"))

# Terminal-state budget. The container OOMs (enforced) or allocates+exits
# (breach) within seconds; 240s bounds a hung provision so the cell crash-fails
# rather than hanging (same shape as gvisor_canary._READY_TIMEOUT_S).
_TERMINAL_TIMEOUT_S = int(os.environ.get("RLIMIT_OVERSHOOT_TERMINAL_TIMEOUT_S", "240"))
# Backing-Pod discovery budget; the Pod exists shortly after the Sandbox create.
_POD_DISCOVERY_TIMEOUT_S = 30
_POLL_S = 0.25

_SCENARIO_LABEL = {"honest-bench/scenario": "resource-limit-enforcement"}

# Printed by the overshoot command ONLY if the allocation completed (the breach
# path). Corroboration for local fire diagnosis; the verdict keys off the
# terminated reason / exit code, not this sentinel (no log-read needed).
_ALLOCATED_SENTINEL = "RLIMIT_OVERSHOOT_ALLOCATED"


# --------------------------------------------------------------------------- #
# Pure core (offline-testable; no cluster, no third-party import)
# --------------------------------------------------------------------------- #


def overshoot_probe_enabled() -> bool:
    """True iff BENCH_RLIMIT_OVERSHOOT_PROBE arms the controlled-overshoot probe.

    Default-off (mirrors netpol_probe.dataplane_probe_enabled): a kind/CI-unit run
    has no gVisor/Kata runtime whose enforcement is meaningful to probe, and the
    substrate gate already short-circuits this module out of the kind artifact. The
    coordinated fill-fire (#5634) arms it explicitly for one run on the real
    substrate. Off-by-default is the substrate-scoped default, not a dormant gate.
    """
    return os.environ.get(_PROBE_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def memory_overshoot_command(overshoot_mib: int) -> list[str]:
    """argv that allocates ``overshoot_mib`` of page-touched anonymous memory, then exits 0.

    ``bytearray(n)`` reserves the buffer; the per-page write loop forces the kernel
    to back every page with a real frame (CPython's calloc-backed bytearray can
    otherwise map the shared zero page COW, so RSS would not actually grow). On an
    ENFORCED runtime the cgroup OOM-killer trips DURING the allocation (the process
    never reaches the print/exit) → OOMKilled / 137. On an UNENFORCED runtime the
    allocation completes, prints the sentinel, and exits 0 → the breach signal. Run
    as the container's own command (no exec surface needed, unlike netpol_probe).
    """
    n = int(overshoot_mib)
    py = (
        f"n={n}*1024*1024\n"
        "buf=bytearray(n)\n"
        "for i in range(0,n,4096): buf[i]=1\n"
        f"print('{_ALLOCATED_SENTINEL}')"
    )
    return ["python3", "-c", py]


def classify_containment(terminated_reason: object, exit_code: object) -> Optional[bool]:
    """Map a terminated container's (reason, exit_code) to contained / breach / inconclusive.

    Returns:
      True  (contained) — ``reason == "OOMKilled"`` OR ``exit_code == 137``: the
                          allocation of A>L tripped the memory cgroup limit. Because
                          A << node_memory (module docstring), a 137 here can only be
                          the cgroup OOM-killer, never a node-level OOM.
      False (breach)    — ``exit_code == 0``: the over-limit allocation SUCCEEDED and
                          the process exited cleanly — the sandbox exceeded its
                          declared footprint and lived.
      None  (inconclusive) — any other terminal state (non-OOM crash, unexpected
                          non-zero exit): neither a clean containment nor a clean
                          breach, so the cell degrades to pending rather than guess.

    ``bool`` is excluded from the int checks (it is an int subclass) so a stray
    ``True``/``False`` exit_code cannot alias 1/0.
    """
    if terminated_reason == "OOMKilled":
        return True
    if isinstance(exit_code, bool):
        return None
    if isinstance(exit_code, int):
        if exit_code == 137:
            return True
        if exit_code == 0:
            return False
    return None


def classify_enforcement(
    contained: Optional[bool],
) -> tuple[str, Optional[str], Optional[str]]:
    """Map containment to (outcome, badge_scope, badge_construction).

      contained is True  → ("PASS", "enforced", "footprint-overshoot") — the runtime
                           held the sandbox to its declared footprint; the badge
                           upgrades to enforced with its construction disclosure (the
                           #4051 over-claim guard is satisfied by the atomic pair).
      contained is False → ("FAIL", None, None) — a real breach; no badge (a FAIL
                           renders no enforcement claim).
      contained is None  → ("pending", None, None) — inconclusive; degrade to a
                           pending cell, never a fabricated badge or false breach.
    """
    if contained is True:
        return ("PASS", "enforced", "footprint-overshoot")
    if contained is False:
        return ("FAIL", None, None)
    return ("pending", None, None)


# --------------------------------------------------------------------------- #
# Thin I/O (touches the cluster; lazy-imports kubernetes inside run())
# --------------------------------------------------------------------------- #


def _build_sandbox_manifest(sandbox_name: str, api_version: str) -> dict:
    """Sandbox CR whose container's own command is the controlled memory overshoot.

    `runtimeClassName` requests the runtime under test; `limits.memory = L` is the
    declared footprint whose enforcement this cell verifies; the container command
    attempts to allocate A>L (see memory_overshoot_command). `restartPolicy: Never`
    so the first terminal state IS the verdict (no restart masking the OOM).
    ``api_version`` is passed in (resolved lazily by run() from ._apiversion) so
    this module stays stdlib-only at import.
    """
    limit_mem = f"{_LIMIT_MIB}Mi"
    return {
        "apiVersion": api_version,
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
                            "command": memory_overshoot_command(_OVERSHOOT_MIB),
                            "resources": {
                                # requests == limits for memory so the cgroup limit
                                # is exactly L (the declared footprint under test).
                                "requests": {"cpu": "50m", "memory": limit_mem},
                                "limits": {"cpu": "500m", "memory": limit_mem},
                            },
                        },
                    ],
                    "restartPolicy": "Never",
                },
            },
        },
    }


def _find_backing_pod(core, *, sandbox_uid: str):
    """Return the Pod owned by the Sandbox (ownerReferences uid match), or None.

    uid-match is convention-independent (no pod-name-shape or label-propagation
    assumption). The scenario label narrows the scan with a full-namespace
    fallback. Mirrors gvisor_canary._find_backing_pod (each cell keeps its own copy
    — the sibling cells deliberately do not share a helper module).
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


def _read_terminated(pod) -> Optional[tuple[object, object]]:
    """Return (reason, exit_code) of the first terminated container, or None.

    Reads containerStatuses[].state.terminated first, then .lastState.terminated
    (restartPolicy Never means state.terminated is the final state, but lastState
    is checked as a defensive fallback). None while no container has terminated.
    """
    statuses = (pod.status.container_statuses or []) if pod.status else []
    for cs in statuses:
        for holder in (cs.state, cs.last_state):
            term = getattr(holder, "terminated", None) if holder else None
            if term is not None:
                return term.reason, term.exit_code
    return None


def _wait_for_terminal(core, *, sandbox_uid: str) -> tuple[object, object]:
    """Poll the backing Pod until a container terminates; raise on timeout.

    Does NOT wait for Ready — the overshoot container may OOM before Ready. Locates
    the backing Pod by owner uid, then polls its containerStatuses for a terminated
    state. Raises (crash-fail) if the Pod is never located or never terminates
    within the window — an infra failure, distinct from a real breach result.

    Timeout bound is ~240-270s, not a literal 240s: the outer deadline is only
    re-checked between _find_backing_pod calls, and a single such call can burn
    its own _POD_DISCOVERY_TIMEOUT_S (~30s) before returning None. This only
    affects the pod-never-located crash-fail path, never a PASS/FAIL verdict.
    """
    deadline = time.monotonic() + _TERMINAL_TIMEOUT_S
    last_pod = None
    while time.monotonic() < deadline:
        pod = _find_backing_pod(core, sandbox_uid=sandbox_uid)
        if pod is not None:
            last_pod = pod
            term = _read_terminated(pod)
            if term is not None:
                return term
        time.sleep(_POLL_S)
    if last_pod is None:
        raise RuntimeError(
            f"backing Pod for sandbox uid {sandbox_uid!r} not located within "
            f"{_TERMINAL_TIMEOUT_S}s — controller may be unhealthy or the "
            f"RuntimeClass {_RUNTIME_CLASS!r} absent on the node"
        )
    raise RuntimeError(
        f"backing Pod {last_pod.metadata.name} did not reach a terminated "
        f"container state within {_TERMINAL_TIMEOUT_S}s (phase="
        f"{last_pod.status.phase if last_pod.status else '<none>'!r}) — the "
        f"overshoot command may be hung or the runtime never scheduled it"
    )


def _cleanup(custom, *, sandbox_name: str, gvr: tuple) -> None:
    """Best-effort delete of the Sandbox (controller cascades the Pod)."""
    from kubernetes.client.exceptions import ApiException
    group, version, plural = gvr
    try:
        custom.delete_namespaced_custom_object(
            group=group, version=version, namespace=_NAMESPACE,
            plural=plural, name=sandbox_name,
        )
    except ApiException as e:
        if e.status != 404:
            log.warning("cleanup: delete sandbox %s failed: %s", sandbox_name, e)


def run(scenario_name: str) -> tuple[str, str, dict]:
    """Controlled-overshoot enforcement probe (arm-gated) → (outcome, excerpt, sla_metrics).

    Arm gate OFF (default): a pure stdlib no-op — pending(not-yet-measured), no
    cluster touched. Arm gate ON: deploy a Sandbox whose container attempts to
    allocate A>L, wait for its terminal state, and classify containment. PASS
    (enforced/footprint-overshoot) on a cgroup OOM-kill; FAIL on a clean over-limit
    survival; pending(overshoot-inconclusive) on an unclassifiable terminal state;
    raise (crash-fail) on a provisioning/infra failure.
    """
    if not overshoot_probe_enabled():
        log.info(
            "resource_limit_enforcement INERT (arm gate %s off) — pending %s; "
            "the controlled-overshoot probe arms only on the coordinated fill-fire "
            "(#5634); no cluster touched",
            _PROBE_ENV, _PENDING_NOT_MEASURED,
        )
        excerpt = (
            "not-yet-measured: resource-limit-enforcement axis registered; "
            "controlled-overshoot confinement probe present but not armed "
            f"({_PROBE_ENV} off) — pending the coordinated fill-fire"
        )
        return "pending", excerpt, {"pending_reason": _PENDING_NOT_MEASURED}

    from kubernetes import client as k8s_client

    from ._apiversion import sandbox_api_version, sandbox_gvr
    from ._kube import load_cluster_config

    gvr = sandbox_gvr()
    api_version = sandbox_api_version()

    load_cluster_config()
    custom = k8s_client.CustomObjectsApi()
    core = k8s_client.CoreV1Api()

    suffix = uuid.uuid4().hex[:8]
    sandbox_name = f"rlimit-{suffix}"

    log.info(
        "creating Sandbox %s (image=%s, runtimeClassName=%s, limit=%dMi, "
        "overshoot=%dMi) for resource-limit-enforcement probe",
        sandbox_name, _SANDBOX_IMAGE, _RUNTIME_CLASS, _LIMIT_MIB, _OVERSHOOT_MIB,
    )
    created = custom.create_namespaced_custom_object(
        group=gvr[0], version=gvr[1], namespace=_NAMESPACE,
        plural=gvr[2], body=_build_sandbox_manifest(sandbox_name, api_version),
    )
    sandbox_uid = ((created or {}).get("metadata") or {}).get("uid")
    if not sandbox_uid:
        obj = custom.get_namespaced_custom_object(
            group=gvr[0], version=gvr[1], namespace=_NAMESPACE,
            plural=gvr[2], name=sandbox_name,
        )
        sandbox_uid = ((obj or {}).get("metadata") or {}).get("uid")

    try:
        if not sandbox_uid:
            raise RuntimeError(
                f"Sandbox {sandbox_name} create returned no metadata.uid — cannot "
                f"locate the backing Pod to read enforcement"
            )
        reason, exit_code = _wait_for_terminal(core, sandbox_uid=sandbox_uid)
        contained = classify_containment(reason, exit_code)
        outcome, scope, construction = classify_enforcement(contained)

        if outcome == "PASS":
            return (
                "PASS",
                f"Sandbox {sandbox_name} was CONFINED to its declared "
                f"{_LIMIT_MIB}Mi limit: allocating {_OVERSHOOT_MIB}Mi tripped the "
                f"memory cgroup OOM-killer (reason={reason!r}, exit={exit_code!r}) "
                f"before the over-limit allocation completed. The runtime enforces "
                f"the footprint the density figure is measured at. (Badge: enforced "
                f"via footprint-overshoot; CPU CFS-throttle is the documented next "
                f"iteration.)",
                {"badge_scope": scope, "badge_construction": construction},
            )
        if outcome == "FAIL":
            # excerpt is classification-only (run.py drops it before write); log the
            # semantic verdict (no resource-name PII) so a badge regression is
            # attributable after the ephemeral fire cluster tears down.
            log.warning(
                "resource-limit BREACH: reason=%r exit=%r (declared %dMi, "
                "allocated %dMi and survived)",
                reason, exit_code, _LIMIT_MIB, _OVERSHOOT_MIB,
            )
            return (
                "FAIL",
                f"Sandbox {sandbox_name} BREACHED its declared {_LIMIT_MIB}Mi "
                f"limit: the {_OVERSHOOT_MIB}Mi over-limit allocation SUCCEEDED and "
                f"the container exited cleanly (reason={reason!r}, exit={exit_code!r}) "
                f"— the runtime did NOT hold the sandbox to its declared footprint, "
                f"so the density figure is a packing claim, not a capacity guarantee.",
                {},
            )
        return (
            "pending",
            f"Sandbox {sandbox_name} reached an unclassifiable terminal state "
            f"(reason={reason!r}, exit={exit_code!r}) — neither a clean cgroup "
            f"OOM-kill nor a clean over-limit survival; degrading to "
            f"{_PENDING_INCONCLUSIVE} rather than fabricating a verdict.",
            {"pending_reason": _PENDING_INCONCLUSIVE},
        )
    finally:
        _cleanup(custom, sandbox_name=sandbox_name, gvr=gvr)
