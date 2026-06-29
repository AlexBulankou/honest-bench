"""Suspend/resume lifecycle benchmark: pause a Sandbox to zero compute, resume it.

    Setup    : Empty namespace; agent-sandbox controller installed. A bare
               Sandbox CR (no Template/Claim/WarmPool) — operatingMode is a
               Sandbox.spec field, the simplest dependency surface.
    Action   : (1) apply a minimal Sandbox; wait until it reports a running Pod;
               capture the underlying Pod's UID. (2) patch
               spec.operatingMode=Suspended; poll until terminal-suspended.
               (3) patch spec.operatingMode=Running; poll the resume.
    Expected : Suspend leg — the controller deletes the underlying Pod, the
               Sandbox CR still exists, and the controller emits a dedicated
               Suspended=True/reason=PodTerminated condition while Ready flips to
               False/reason=SandboxSuspended. Resume leg — a NEW Pod is created
               (UID != pre-suspend UID, proving a real recreate, not an in-place
               un-pause) and Ready returns True with message "Pod is Ready".
    Why      : operatingMode Suspended|Running is the sandbox's cost-control
               lever — drop an idle agent sandbox's compute to zero while
               preserving the CR identity (and any attached PVC), scale back on
               demand. Suspend/resume state machines are a classic regression
               site (Pod deleted but status not updated; a resume that un-pauses
               in place instead of recreating). The UID-changed gate on resume
               is load-bearing: a controller that left the old Pod, or did an
               in-place restart, would false-green the "compute actually released
               + re-provisioned" claim.

## Controller contract (v1beta1)

  - suspend (spec.operatingMode=Suspended): the controller deletes the owned
    Pod, then emits a dedicated Suspended=True/reason=PodTerminated condition AND
    marks Ready=False/reason=SandboxSuspended. The Suspended condition
    (status+reason) is the discriminator — a real condition, not a Ready-message
    substring.
  - resume (spec.operatingMode=Running): the Pod is recreated (new UID) and
    Ready returns True/message "Pod is Ready" — the compute lifecycle works.

## Resume is a gap probe (known upstream controller gap)

The current build-from-main controller has a known resume defect: after a resume
the compute lifecycle completes (new Pod + Ready=True) BUT the controller never
clears the stale Suspended=True condition — its resume status write hits a
resourceVersion conflict and is not retried to convergence, so a resumed,
fully-operational Sandbox perpetually advertises Suspended=True. The resume leg
is therefore a TWO-PART probe that flips the moment upstream fixes the bug:

  - resume lifecycle MUST complete (new Pod uid != pre-suspend uid AND
    Ready=True/"Pod is Ready"). A lifecycle break is a REAL regression and
    returns a plain FAIL.
  - given lifecycle-OK, the Suspended condition is then checked:
      * Suspended PERSISTS past the clear-window → outcome=pending,
        pending_reason=upstream-blocked — the documented tracked gap, rendered as
        a known-gap rather than a green PASS or a red regression.
      * Suspended CLEARS on resume → PASS — the signal that upstream closed the
        gap. A pending member newly PASSing is the cue to re-key this scenario to
        a strict gate.

## Why NOT pods:delete in this scenario

Unlike a cold-start scenario that deletes the Pod itself to test recreate, here
the *controller* deletes the Pod on suspend. The scenario's whole point is to
observe the controller's suspend-driven deletion — if the runner deleted the Pod
it would be testing its own delete, not the suspend contract. So this scenario
only needs pods:get (read UID + observe NotFound), never pods:delete.

## Crash posture

Infrastructure-level failures (CRD missing, controller unhealthy, RBAC gap,
quota) raise — the harness loop classifies a raised exception as a crash-fail
cell. Scenario-outcome FAILs return ("FAIL", <excerpt>, {}); the tracked resume
gap returns ("pending", <excerpt>, {"pending_reason": "upstream-blocked"});
success returns ("PASS", <excerpt>, {}).
"""

from __future__ import annotations

try:  # package context (production: run.py loads harness.scenarios.suspend_resume)
    from . import runtime_class as rc
    from ._apiversion import sandbox_api_version, sandbox_gvr
    from ._kube import load_cluster_config
    from .. import metrics, ttfe_probe
except ImportError:  # standalone (dependency-free test from the scenarios/ dir)
    import runtime_class as rc
    from _apiversion import sandbox_api_version, sandbox_gvr
    from _kube import load_cluster_config
    import sys as _sys
    import pathlib as _pathlib

    _sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))
    import metrics, ttfe_probe

import logging
import os
import time
import uuid

log = logging.getLogger("sandbox-scenario.suspend-resume")


_NAMESPACE = os.environ.get("BENCH_NAMESPACE", "default")
_SANDBOX_IMAGE = os.environ.get(
    "SUSPEND_RESUME_SANDBOX_IMAGE", "busybox:1.36"
)

# Runtime-class pin for the #3942 Core Metrics matrix Kata row. DEFAULT-OFF: with
# the knob unset, apply_runtime_class is a byte-identical no-op so the Sandbox
# manifest is exactly its pre-#3942 shape and this scenario stays the plain
# resume-from-suspend cell. Set to "gvisor"/"kata" to pin the resumed Sandbox to
# that runtime (runtimeClassName + the runtime's toleration/nodeSelector) so the
# published resume row is honestly attributed to the runtime it ran under. The
# shared runtime_class helper owns the pin + post-measurement verify so this
# scenario, native_digest_cold, and warmpool_cold_start pin-and-verify identically.
# NOTE (#3942): resume x Kata is N/A-by-construction — CRIU checkpoint/restore does
# not transfer to the Kata VM model — so this pin is exercised only for gvisor; the
# render encodes the Kata resume cell as na-by-design rather than measuring it.
_RUNTIME_CLASS = os.environ.get("SUSPEND_RESUME_RUNTIME_CLASS", "")

# The cluster's substrate banner (run.py provenance). A gke-sandbox banner asserts
# gVisor isolation, so the consistency guard refuses an unset/non-gVisor runtime on
# it before any cluster call — preventing a runc resume number from publishing under
# a gVisor label. kind/gke make no isolation claim and impose no constraint.
_CLUSTER_SUBSTRATE = os.environ.get("BENCH_CLUSTER_SUBSTRATE", "kind")


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    """Parse a non-negative int env var, clamped to >= minimum; default on junk.

    A malformed value falls back to the default rather than raising, so a fat-
    fingered fire-time override degrades to the safe default instead of crashing
    the cell. Sub-minimum values clamp up (cycle_count must be >= 1).
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        val = int(raw)
    except ValueError:
        return default
    return val if val >= minimum else minimum


# TTFE Layer-2 exec probe. DEFAULT-OFF.
#
# gated: default-off until the runner ServiceAccount carries pods/exec RBAC and
# the fire path flips it ON in the SAME change that grants the verb. The probe
# (ttfe_probe.probe_first_instruction) collapses an RBAC-denied exec and a
# genuine exec-failure to the same (None, False) — it cannot tell them apart — so
# an ungated default-on would publish a false 0% exec-success before the grant
# lands. Flip-issue: #3944.
_TTFE_EXEC = _env_flag("BENCH_TTFE_EXEC")

# Resume-cycle-count knob. DEFAULT 1 = a single suspend->resume cycle (the legacy
# behavior; emit byte-unchanged). When > 1 the cell loops N suspend->resume cycles
# on the SAME Sandbox and aggregates the N resume-activation TTFE samples into a
# real p50/p95 distribution (metrics.multi_sample_ttfe_point) instead of the noisy
# n=1 single sample. Fire-time controllable so a4s1 can re-fire resume at higher N
# without a code change. Only the TTFE distribution scales with N; the Suspended-
# clear gap verdict is a deterministic controller property (pending if ANY cycle's
# Suspended condition never clears, else PASS). N has effect only when
# BENCH_TTFE_EXEC is also on — with the probe gated off there is no TTFE sample to
# accumulate, so extra cycles would add wall-clock for no metric; guarded below.
_RESUME_CYCLE_COUNT = _env_int("SUSPEND_RESUME_CYCLE_COUNT", 1)

# Timeouts. 120 (ready) + 90 (suspend) + 90 (resume) + slack keeps a hung
# transition bounded so the cell fails rather than hanging the suite.
_SANDBOX_READY_TIMEOUT_S = 120
_SUSPEND_TIMEOUT_S = 90
_RESUME_TIMEOUT_S = 90
# Gap-probe: after the resume lifecycle completes (new Pod + Ready), how long to
# poll for the Suspended condition to CLEAR before concluding it persists. Sized
# so a FIXED controller (clears Suspended shortly after recreate) reads as the
# gap-closed PASS, not a false pending; the live repro showed Suspended stable
# well past this window, so 30s comfortably distinguishes "never clears" (gap)
# from "clears shortly after" (fixed). Only runs after the lifecycle check
# breaks early, so it does not inflate the steady-state wall-clock.
_RESUME_SUSPENDED_CLEAR_WINDOW_S = 30
_POLL_S = 2

_SANDBOX_GVR = sandbox_gvr()

# Condition surface (v1beta1). Suspend is driven by spec.operatingMode
# (Running|Suspended); the controller emits a dedicated Suspended condition and
# marks a suspended sandbox Ready=False — the discriminator is a real condition
# (status+reason), not a Ready-message substring.
_COND_READY = "Ready"
_COND_SUSPENDED = "Suspended"
_REASON_POD_TERMINATED = "PodTerminated"
_REASON_SANDBOX_SUSPENDED = "SandboxSuspended"
_OPMODE_SUSPENDED = "Suspended"
_OPMODE_RUNNING = "Running"
_MSG_RESUMED = "Pod is Ready"

# Harness-stamped podTemplate label: lets _read_pod fall back to a label selector
# the controller re-applies on recreate, without knowing the controller's
# internal hash label.
_POD_LABEL_KEY = "honest-bench/pod"
_SCENARIO_LABEL_KEY = "honest-bench/scenario"
_SCENARIO = "suspend-resume"


def _build_sandbox_manifest(sandbox_name: str) -> dict:
    """Minimal bare Sandbox CR — single container, sleeps, restartPolicy Never.

    No Template/Claim/WarmPool — operatingMode is a Sandbox.spec field, so the
    bare-CR path is the minimal surface.

    When SUSPEND_RESUME_RUNTIME_CLASS is set, the inner pod_spec is pinned to that
    runtime (runtimeClassName + the runtime's toleration/nodeSelector) via the shared
    runtime_class helper. Default-off: with the knob unset apply_runtime_class is a
    byte-identical no-op, so the manifest is exactly its pre-#3942 shape. The pin
    touches only podTemplate.spec; podTemplate.metadata.labels (the harness-stamped
    pod label _read_pod falls back to on recreate) is preserved unchanged.
    """
    pod_spec = {
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
    }
    rc.apply_runtime_class(pod_spec, _RUNTIME_CLASS)
    return {
        "apiVersion": sandbox_api_version(),
        "kind": "Sandbox",
        "metadata": {
            "name": sandbox_name,
            "namespace": _NAMESPACE,
            "labels": {_SCENARIO_LABEL_KEY: _SCENARIO},
        },
        "spec": {
            "podTemplate": {
                "metadata": {
                    "labels": {_POD_LABEL_KEY: sandbox_name},
                },
                "spec": pod_spec,
            },
        },
    }


def _condition(conditions, cond_type: str):
    """Return the condition dict of the given type, or None. Tolerates None."""
    for c in conditions or []:
        if c.get("type") == cond_type:
            return c
    return None


def _eval_suspend_state(pod_exists: bool, conditions) -> tuple[bool, str]:
    """Is the Sandbox in the TERMINAL suspended state?

    Pure function (no K8s) so the branches are independently exercisable.
    Terminal suspend requires ALL of:

      1. the underlying Pod is gone (pod_exists == False)
      2. Suspended condition: status=True, reason=PodTerminated
      3. Ready condition: status=False, reason=SandboxSuspended — a suspended
         sandbox is marked NOT Ready.

    The Suspended condition (status+reason) is the discriminator, not a Ready
    message substring. Returns (is_terminal, detail) — detail names the first
    unmet gate so a FAIL excerpt says exactly what didn't transition.
    """
    if pod_exists:
        return False, "underlying Pod still present (suspend in progress / not started)"

    susp = _condition(conditions, _COND_SUSPENDED)
    if susp is None:
        return False, (
            "Suspended condition absent while suspended (the controller must "
            "emit a Suspended condition once the pod is terminated)"
        )
    if susp.get("status") != "True":
        return False, (
            f"Suspended.status={susp.get('status')!r} (want True), "
            f"reason={susp.get('reason')!r}, message={susp.get('message')!r}"
        )
    if susp.get("reason") != _REASON_POD_TERMINATED:
        return False, (
            f"Suspended.reason={susp.get('reason')!r} (want "
            f"{_REASON_POD_TERMINATED!r}) — pod-termination not yet reflected"
        )

    ready = _condition(conditions, _COND_READY)
    if ready is None:
        return False, "Ready condition absent while suspended"
    if ready.get("status") != "False" or ready.get("reason") != _REASON_SANDBOX_SUSPENDED:
        return False, (
            f"Ready cross-check failed: want status=False/reason="
            f"{_REASON_SANDBOX_SUSPENDED!r}, got status={ready.get('status')!r}/"
            f"reason={ready.get('reason')!r} (a suspended sandbox is marked NOT "
            f"Ready)"
        )

    return True, (
        f"terminal suspend: Pod gone, Suspended=True/"
        f"{_REASON_POD_TERMINATED}, Ready=False/{_REASON_SANDBOX_SUSPENDED}"
    )


def _eval_resume_lifecycle(pod_uid, old_uid, conditions) -> tuple[bool, str]:
    """Did the resume LIFECYCLE complete? — the half of the resume contract that
    MUST hold regardless of the Suspended-clear gap.

    Pure function. Lifecycle-complete requires ALL of:

      1. a new underlying Pod with a DIFFERENT UID than pre-suspend (real
         recreate, not an in-place un-pause)
      2. Ready condition: status=True, message contains "Pod is Ready"

    Deliberately does NOT inspect the Suspended condition — that is the gap
    signature, evaluated separately by `_eval_resume_gap` so a lifecycle break (a
    real regression) is distinguishable from the documented Suspended-stale gap.
    Returns (lifecycle_ok, detail).
    """
    if pod_uid is None:
        return False, "no underlying Pod yet (resume recreate not started)"
    if pod_uid == old_uid:
        return False, (
            f"underlying Pod UID unchanged ({pod_uid}) — controller un-paused "
            f"in place instead of recreating (compute not actually re-provisioned)"
        )

    ready = _condition(conditions, _COND_READY)
    if ready is None:
        return False, "Ready condition absent after resume"
    if ready.get("status") != "True":
        return False, (
            f"Ready.status={ready.get('status')!r} (want True) after resume, "
            f"reason={ready.get('reason')!r}, message={ready.get('message')!r}"
        )
    msg = ready.get("message") or ""
    if _MSG_RESUMED not in msg:
        return False, (
            f"new Pod uid={pod_uid} but Ready.message={msg!r} does not contain "
            f"{_MSG_RESUMED!r} (Pod recreated but not yet Ready)"
        )

    return True, (
        f"resume lifecycle complete: new Pod uid={pod_uid} (was "
        f"{old_uid}), Ready=True message contains {_MSG_RESUMED!r}"
    )


def _suspended_persists(conditions) -> bool:
    """True iff a Suspended condition with status==True is present — the gap
    signature (the controller never cleared Suspended after resume).

    Pure function. A cleared gap is: condition absent OR status != "True".
    """
    susp = _condition(conditions, _COND_SUSPENDED)
    return susp is not None and susp.get("status") == "True"


def _eval_resume_gap(
    *, suspended_cleared: bool, susp_reason, pod_uid, old_uid, clear_window_s: int
) -> tuple[str, str, dict]:
    """Gap-probe verdict for the resume leg. Pure function.

    Called ONLY after the resume lifecycle has completed (new Pod + Ready) — a
    lifecycle break is a REAL regression handled by the caller's timeout, not
    routed here. Two-part gap signature — the gap is the lifecycle-OK-but-
    Suspended-persists state:

      - Suspended CLEARED on resume → ("PASS", <excerpt>, {}). The gap is CLOSED
        (upstream added retry-to-convergence on the resume status write). A
        pending member newly PASSing is the cue to re-key this scenario to a
        strict PASS gate.
      - Suspended PERSISTS past the clear-window → ("pending", <excerpt>,
        {"pending_reason": "upstream-blocked"}). The documented tracked gap; the
        render page surfaces it as a known-gap, never a green PASS or a red
        regression.
    """
    if suspended_cleared:
        return (
            "PASS",
            f"{_SCENARIO}: resume lifecycle completed (new Pod uid={pod_uid} != "
            f"pre-suspend {old_uid}, Ready=True) AND the Suspended condition "
            f"CLEARED on resume — the controller resume gap is CLOSED (it now "
            f"clears Suspended / retries the resume status write to convergence). "
            f"Re-key this scenario to a strict PASS gate.",
            {},
        )
    return (
        "pending",
        f"resume lifecycle completed (new Pod uid={pod_uid} != pre-suspend "
        f"{old_uid}, Ready=True/message {_MSG_RESUMED!r}) BUT the Suspended "
        f"condition PERSISTS status=True/reason={susp_reason!r} past resume "
        f"(still set after a {clear_window_s}s clear-window) — the controller "
        f"recreates the Pod + marks Ready but never clears the stale Suspended "
        f"condition (resume status write hits a resourceVersion conflict without "
        f"retry-to-convergence). A resumed, fully-operational Sandbox perpetually "
        f"advertises Suspended=True. Tracked upstream-blocked gap (fixable in the "
        f"controller via retry-on-conflict / an explicit Suspended clear).",
        {"pending_reason": "upstream-blocked"},
    )


def _wait_for_sandbox_ready(custom, *, sandbox_name: str) -> dict:
    """Poll Sandbox until status.podIPs has an entry, or raise. Returns status."""
    group, version, plural = _SANDBOX_GVR
    deadline = time.monotonic() + _SANDBOX_READY_TIMEOUT_S
    last_status: object = "<no-status>"
    while time.monotonic() < deadline:
        obj = custom.get_namespaced_custom_object(
            group=group, version=version, namespace=_NAMESPACE,
            plural=plural, name=sandbox_name,
        )
        status = (obj or {}).get("status") or {}
        last_status = status
        if status.get("podIPs"):
            return status
        time.sleep(_POLL_S)
    raise RuntimeError(
        f"Sandbox {sandbox_name} did not populate status.podIPs within "
        f"{_SANDBOX_READY_TIMEOUT_S}s (last status={last_status!r}) — "
        f"controller may be unhealthy, image pull failing, or CRD schema drifted"
    )


def _get_sandbox_conditions(custom, *, sandbox_name: str) -> list:
    """Read the Sandbox status.conditions array (or [])."""
    group, version, plural = _SANDBOX_GVR
    obj = custom.get_namespaced_custom_object(
        group=group, version=version, namespace=_NAMESPACE,
        plural=plural, name=sandbox_name,
    )
    return ((obj or {}).get("status") or {}).get("conditions") or []


def _read_pod(core_v1, *, sandbox_name: str):
    """Return (exists, uid). Direct read-by-name then label fallback.

    The controller names the underlying Pod the same as the Sandbox CR. On 404 we
    fall back to the harness-stamped label selector before concluding the Pod is
    gone, so a mid-recreate rename can't masquerade as deletion.
    """
    from kubernetes.client.exceptions import ApiException
    try:
        pod = core_v1.read_namespaced_pod(name=sandbox_name, namespace=_NAMESPACE)
        return True, pod.metadata.uid
    except ApiException as e:
        if e.status != 404:
            raise
    pods = core_v1.list_namespaced_pod(
        namespace=_NAMESPACE,
        label_selector=f"{_POD_LABEL_KEY}={sandbox_name}",
    )
    if pods.items:
        return True, pods.items[0].metadata.uid
    return False, None


def _lifecycle_patch_body(*, suspend: bool) -> dict:
    """Merge-patch body for the suspend (True) / resume (False) transition.

    Pure function (no K8s). Toggles spec.operatingMode Suspended|Running — one
    scalar field, so the default merge-patch is correct (no array-merge needed).
    """
    return {"spec": {"operatingMode": _OPMODE_SUSPENDED if suspend else _OPMODE_RUNNING}}


def _patch_lifecycle(custom, *, sandbox_name: str, suspend: bool) -> None:
    """Apply the suspend/resume transition via spec.operatingMode."""
    group, version, plural = _SANDBOX_GVR
    custom.patch_namespaced_custom_object(
        group=group, version=version, namespace=_NAMESPACE,
        plural=plural, name=sandbox_name,
        body=_lifecycle_patch_body(suspend=suspend),
    )


def _cleanup(custom, *, sandbox_name: str) -> None:
    """Best-effort cleanup. Suppress NotFound."""
    from kubernetes.client.exceptions import ApiException
    group, version, plural = _SANDBOX_GVR
    try:
        custom.delete_namespaced_custom_object(
            group=group, version=version, namespace=_NAMESPACE,
            plural=plural, name=sandbox_name,
        )
    except ApiException as e:
        if e.status != 404:
            log.warning("cleanup: delete sandbox %s failed: %s", sandbox_name, e)


def _run_suspend_resume_cycle(custom, core_v1, *, sandbox_name, pre_uid):
    """One suspend->resume cycle on an already-Ready Sandbox.

    Returns (kind, data):
      ("fail", (outcome, excerpt, {}))  -- a real suspend/resume LIFECYCLE
          regression; the caller returns it verbatim.
      ("ok", (verdict, gap_excerpt, gap_sla, ttfe_ms, exec_ok, new_uid)) -- the
          Suspended-clear gap verdict for THIS cycle plus the RAW resume-activation
          TTFE sample (ttfe_ms/exec_ok are (None, False) when BENCH_TTFE_EXEC is
          off). The caller accumulates samples across cycles and merges the TTFE
          point itself, so this helper returns the gap excerpt WITHOUT a TTFE
          appendix.

    Does NOT create or delete the Sandbox -- the caller owns its lifecycle.
    pre_uid is the backing Pod uid BEFORE this cycle's suspend; the resume leg
    asserts a NEW uid (Pod recreate), so the caller passes the prior cycle's
    new_uid as the next cycle's pre_uid.
    """
    group, version, plural = _SANDBOX_GVR

    # --- SUSPEND LEG ---
    log.info("suspending (operatingMode=Suspended)")
    _patch_lifecycle(custom, sandbox_name=sandbox_name, suspend=True)

    suspend_detail = "<no poll yet>"
    deadline = time.monotonic() + _SUSPEND_TIMEOUT_S
    while time.monotonic() < deadline:
        exists, _ = _read_pod(core_v1, sandbox_name=sandbox_name)
        conds = _get_sandbox_conditions(custom, sandbox_name=sandbox_name)
        terminal, suspend_detail = _eval_suspend_state(exists, conds)
        if terminal:
            break
        time.sleep(_POLL_S)
    else:
        return ("fail", (
            "FAIL",
            f"suspend leg: Sandbox {sandbox_name!r} did not reach terminal "
            f"suspend state within {_SUSPEND_TIMEOUT_S}s — last gate: "
            f"{suspend_detail}",
            {},
        ))
    log.info("suspend leg OK: %s", suspend_detail)

    # Sandbox CR itself must still exist (suspend preserves identity).
    from kubernetes.client.exceptions import ApiException
    try:
        custom.get_namespaced_custom_object(
            group=group, version=version, namespace=_NAMESPACE,
            plural=plural, name=sandbox_name,
        )
    except ApiException as e:
        if e.status == 404:
            return ("fail", (
                "FAIL",
                f"suspend leg: Sandbox CR {sandbox_name!r} was DELETED on "
                f"suspend — suspend must preserve the CR identity (only the "
                f"Pod should be released)",
                {},
            ))
        raise

    # --- RESUME LEG (gap probe) ---
    log.info("resuming (operatingMode=Running)")
    _patch_lifecycle(custom, sandbox_name=sandbox_name, suspend=False)
    # t0 for the resume-activation TTFE: resume-request return -> first
    # instruction result on the resumed Pod (the doc's resume TTFE column).
    resume_t0 = time.monotonic()

    # Part 1: the resume LIFECYCLE must complete (new Pod uid + Ready=True/
    # "Pod is Ready"). Timing out here is a REAL regression — the operatingMode
    # lifecycle itself broke — so it returns a plain FAIL.
    lifecycle_ok = False
    resume_detail = "<no poll yet>"
    new_uid = None
    deadline = time.monotonic() + _RESUME_TIMEOUT_S
    while time.monotonic() < deadline:
        _, new_uid = _read_pod(core_v1, sandbox_name=sandbox_name)
        conds = _get_sandbox_conditions(custom, sandbox_name=sandbox_name)
        lifecycle_ok, resume_detail = _eval_resume_lifecycle(
            new_uid, pre_uid, conds)
        if lifecycle_ok:
            break
        time.sleep(_POLL_S)
    if not lifecycle_ok:
        return ("fail", (
            "FAIL",
            f"resume leg: Sandbox {sandbox_name!r} suspend/resume LIFECYCLE "
            f"did not complete within {_RESUME_TIMEOUT_S}s — last gate: "
            f"{resume_detail}. This is a REAL regression in the operatingMode "
            f"lifecycle (Pod recreate + Ready), NOT the tracked "
            f"Suspended-clear gap.",
            {},
        ))
    log.info("resume lifecycle OK: %s", resume_detail)

    # Part 2: the Suspended-clear gap check. Poll a clear-window so a FIXED
    # controller reads as the gap-closed PASS; persists past the window →
    # tracked pending(upstream-blocked).
    cleared = False
    susp_deadline = time.monotonic() + _RESUME_SUSPENDED_CLEAR_WINDOW_S
    while time.monotonic() < susp_deadline:
        conds = _get_sandbox_conditions(custom, sandbox_name=sandbox_name)
        if not _suspended_persists(conds):
            cleared = True
            break
        time.sleep(_POLL_S)
    conds = _get_sandbox_conditions(custom, sandbox_name=sandbox_name)
    susp = _condition(conds, _COND_SUSPENDED)
    susp_reason = susp.get("reason") if susp else None
    verdict, gap_excerpt, gap_sla = _eval_resume_gap(
        suspended_cleared=cleared, susp_reason=susp_reason,
        pod_uid=new_uid, old_uid=pre_uid,
        clear_window_s=_RESUME_SUSPENDED_CLEAR_WINDOW_S,
    )

    # TTFE Layer-2 (gated): probe the resumed Pod's first instruction. Return the
    # RAW (ttfe_ms, exec_ok) sample; the caller accumulates across cycles and
    # merges the aggregated TTFE point. Reached only after lifecycle_ok, so the
    # resumed Pod exists; its backing Pod is named for the CR (pod == sandbox).
    ttfe_ms, exec_ok = None, False
    if _TTFE_EXEC:
        ttfe_ms, exec_ok = ttfe_probe.probe_first_instruction(
            core_v1,
            pod_name=sandbox_name,
            namespace=_NAMESPACE,
            create_monotonic=resume_t0,
        )

    return ("ok", (verdict, gap_excerpt, gap_sla, ttfe_ms, exec_ok, new_uid))


def run(scenario_name: str) -> tuple[str, str, dict]:
    """Provision a bare Sandbox, suspend it, resume it; return the verdict.

    Returns a 3-tuple (outcome, excerpt, sla_metrics). Success → ("PASS",
    excerpt, {}); the tracked resume gap (Suspended never cleared) → ("pending",
    excerpt, {"pending_reason": "upstream-blocked"}); any other scenario-outcome
    failure → ("FAIL", excerpt, {}). Infrastructure failures raise (crash-fail
    cell). No latency metric for the MVP — this is a correctness cell.
    """
    from kubernetes import client as k8s_client

    # Pure, fail-fast (mirrors native_digest_cold): a gke-sandbox-labeled result
    # MUST pin the gVisor RuntimeClass, else the published resume row is a runc number
    # under a gVisor banner. Checked before the cluster is touched so the mistake
    # crashes immediately. kind/gke impose no constraint.
    rc.assert_substrate_runtime_consistency(_CLUSTER_SUBSTRATE, _RUNTIME_CLASS)

    # Portable kubeconfig load (see _kube.load_cluster_config): an explicit
    # KUBECONFIG wins, else in-cluster when running as a pod, else the default
    # kubeconfig.
    load_cluster_config()

    core_v1 = k8s_client.CoreV1Api()
    custom = k8s_client.CustomObjectsApi()

    suffix = uuid.uuid4().hex[:8]
    sandbox_name = f"suspend-{suffix}"
    group, version, plural = _SANDBOX_GVR

    log.info("creating Sandbox %s in %s", sandbox_name, _NAMESPACE)
    custom.create_namespaced_custom_object(
        group=group, version=version, namespace=_NAMESPACE,
        plural=plural, body=_build_sandbox_manifest(sandbox_name),
    )

    try:
        _wait_for_sandbox_ready(custom, sandbox_name=sandbox_name)
        exists, pre_uid = _read_pod(core_v1, sandbox_name=sandbox_name)
        if not exists or not pre_uid:
            return (
                "FAIL",
                f"setup: Sandbox {sandbox_name!r} reported Ready (podIPs set) but "
                f"no underlying Pod found by name or label — controller naming "
                f"convention drifted",
                {},
            )
        log.info("Sandbox ready; pre-suspend Pod uid=%s", pre_uid)

        # Loop N suspend->resume cycles on the SAME Sandbox (default 1). Each
        # cycle exercises the suspend/resume LIFECYCLE + the Suspended-clear gap
        # probe and yields ONE resume-activation TTFE sample. Only the TTFE
        # distribution scales with N — the gap verdict is a deterministic
        # controller property — so when the probe is gated off we run a single
        # cycle regardless of the knob (extra cycles would add wall-clock for no
        # metric). FAILs return early (a real lifecycle regression crashes the
        # cell); every completed cycle is PASS or pending(upstream-blocked).
        cycle_count = _RESUME_CYCLE_COUNT if _TTFE_EXEC else 1

        gap_verdicts: list[tuple[str, str, dict]] = []
        ttfe_samples: list = []
        exec_oks: list[bool] = []
        for i in range(cycle_count):
            if cycle_count > 1:
                log.info("suspend/resume cycle %d/%d", i + 1, cycle_count)
            kind, data = _run_suspend_resume_cycle(
                custom, core_v1, sandbox_name=sandbox_name, pre_uid=pre_uid)
            if kind == "fail":
                return data
            verdict, gap_excerpt, gap_sla, ttfe_ms, exec_ok, new_uid = data
            gap_verdicts.append((verdict, gap_excerpt, gap_sla))
            if _TTFE_EXEC:
                ttfe_samples.append(ttfe_ms)
                exec_oks.append(exec_ok)
            pre_uid = new_uid  # next cycle suspends the just-resumed Pod

        # Runtime read-back (post-loop, mirrors native_digest_cold): verify the just-
        # resumed Sandbox's backing Pod actually scheduled under the pinned runtime
        # before publishing the runtime-labeled resume row. Crash-FAILs on a silent
        # runc fallback. kind/gke skip this (required_runtime_for_substrate -> None —
        # no runtime claim to verify, so the path stays read-free there, preserving
        # the default INERT shape). Runs AFTER all cycles so it never perturbs a
        # measured resume-activation latency, and the resumed Pod is Ready in BOTH the
        # PASS and the pending (Suspended-never-cleared) verdicts — the gap is about
        # the Suspended condition not clearing, not the Pod being absent. The gate
        # shares the single substrate->runtime source of truth with the consistency
        # guard above, which already proved _RUNTIME_CLASS == the required runtime.
        if rc.required_runtime_for_substrate(_CLUSTER_SUBSTRATE) is not None:
            verified = rc.verify_bound_pod_runtimes(
                custom, core_v1,
                namespace=_NAMESPACE,
                sandbox_names=[sandbox_name],
                sandbox_gvr=_SANDBOX_GVR,
                expected_runtime_class=_RUNTIME_CLASS,
            )
            log.info(
                "runtime read-back: %d/1 resumed sandbox verified under RuntimeClass %r",
                verified, _RUNTIME_CLASS,
            )

        # Representative gap verdict: pending if ANY cycle's Suspended condition
        # never cleared (the tracked upstream gap), else the first (PASS) cycle.
        verdict, excerpt, sla_metrics = next(
            (v for v in gap_verdicts if v[0] == "pending"), gap_verdicts[0])

        # Merge the resume-activation TTFE distribution into the gap verdict's
        # sla_metrics. The verdict itself is UNCHANGED — the Suspended-clear gap is
        # orthogonal to whether the resumed pod can execute. For n=1 this is
        # byte-identical to the legacy single-sample emit
        # (multi_sample_ttfe_point == single_sample_ttfe_point at N=1); for n>1 it
        # is a real p50/p95 over the N resume samples. The merge preserves any
        # pending_reason key (run.py pops it from sla_metrics). NO throughput (a
        # per-node rate over a handful of activations is meaningless) and NO
        # density (N/A on the resume row per the doc) — same shape as the cold cell.
        if _TTFE_EXEC:
            sla_metrics = {
                **sla_metrics,
                **metrics.multi_sample_ttfe_point(ttfe_samples, exec_oks),
            }
            n = len(exec_oks)
            if n == 1:
                excerpt = (
                    f"{excerpt} TTFE probe: exec_ok={exec_oks[0]}, "
                    f"ttfe_ms={ttfe_samples[0]!r} "
                    f"(n=1; resume-request->first-instruction-result)."
                )
            else:
                ok_count = sum(1 for x in exec_oks if x)
                excerpt = (
                    f"{excerpt} TTFE probe: {ok_count}/{n} cycles exec-ok, "
                    f"samples_ms={ttfe_samples!r} "
                    f"(n={n}; resume-request->first-instruction-result per cycle)."
                )

        log.info("resume gap-probe verdict: %s — %s", verdict, excerpt)
        return verdict, excerpt, sla_metrics
    finally:
        _cleanup(custom, sandbox_name=sandbox_name)
