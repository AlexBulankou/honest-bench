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
    cgroup limits. A memory overshoot should trigger a host OOM-kill of the runsc
    process (container OOMKilled, exit 137); a CPU overshoot should hit CFS
    throttling (cgroup `nr_throttled` rising) without starving neighbors. gVisor
    packs ~5x denser, so the honest question is exactly: does a runaway sandbox
    get contained (protecting its ~5 neighbors) or does it degrade the node?
  - **Kata + microVM** — each sandbox is a real VM with a hypervisor-hard
    memory/vCPU allocation. Enforcement is a hardware-ish boundary, so the 1.26
    figure is inherently capacity-guaranteed; this cell verifies the guest
    contains the overshoot rather than the host node.

## INERT until the fill-fire (why run() returns pending here)

This module is registered as a born-pending cell (#5634) following the
session_turnover INERT-first precedent: the Cell + SCENARIO_LABELS vocabulary +
seed row ship first as a complete, honest `pending (not-yet-measured)` slice
with ZERO fire cost, and the controlled-overshoot probe is filled in on a later
coordinated cross-cluster fire (peer collision-ack, fire-day HIGH
effort). Until then `run()` is a pure stdlib no-op that touches no cluster and
reports not-yet-measured — the satisfied-substrate stub state. On kind the cell
never reaches this module: run._run_one short-circuits on the unmet
`requires_substrate` gate and pends requires-gvisor-runtime / requires-kata-runtime.

## Planned fill-fire probe (documented so the pending row is honest, not a stub-forever)

1. Deploy a sandbox with a declared footprint (`requests`/`limits` cpu+mem).
2. Drive a controlled overshoot INSIDE it — a `stress`-style memory balloon past
   `limits.memory`, and a busy-loop past `limits.cpu`.
3. Assert the observed containment:
     - mem  → container OOMKilled (exit 137 / `reason: OOMKilled`) within a bound.
     - cpu  → CFS-throttled (cgroup `nr_throttled` rising), no neighbor starvation.
4. Emit a bounded-enum verdict (never harness free-text — Layer-1 PII guard).

## Why a badge, not a latency

Like gvisor_canary, this is a binary correctness/honesty assertion (confined vs
not), not a perf number — so the filled cell will render an enforced/FAIL badge
(mirroring the netpol #3950 control-plane/enforced split), not a millisecond
figure. The badge_scope="enforced" split + its construction enum value land with
the fill-fire PR: a badge renders only on PASS, and emitting a naked
badge_scope="enforced" before its construction value exists would trip the #4629
over-claim guard. This module returning pending emits no badge at all.
"""
from __future__ import annotations

import logging

log = logging.getLogger("sandbox-scenario.resource-limit-enforcement")

# Bounded-enum pending reason (⊆ render's PENDING_REASON allow-list). The
# satisfied-substrate stub state: the cell is registered and the substrate gate
# is met, but the controlled-overshoot probe is not yet implemented — an honest
# not-yet-measured, distinct from the kind-side requires-*-runtime short-circuit
# that run._run_one emits before this module is ever imported.
_PENDING_REASON = "not-yet-measured"


def run(scenario_name: str) -> tuple[str, str, dict]:
    """INERT until the fill-fire: report pending(not-yet-measured), touch nothing.

    Returns the standard 3-tuple (outcome, excerpt, sla_metrics). No pod, no
    cluster, no third-party import — a pure stdlib no-op so the cell renders a
    complete, honest `pending (not-yet-measured)` slice at zero fire cost. The
    controlled-overshoot probe (see module docstring) fills this in on a later
    coordinated fire; run._run_one carries the pending_reason through sla_metrics.
    """
    log.info(
        "resource_limit_enforcement is INERT (pending %s) — the controlled-"
        "overshoot probe lands with the fill-fire (#5634); no cluster touched",
        _PENDING_REASON,
    )
    excerpt = (
        "not-yet-measured: resource-limit-enforcement axis registered INERT; "
        "controlled-overshoot confinement probe (OOM-kill / CFS-throttle) "
        "pending the coordinated fill-fire"
    )
    return "pending", excerpt, {"pending_reason": _PENDING_REASON}
