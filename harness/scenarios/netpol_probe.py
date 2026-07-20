"""Data-plane connectivity probe for the NetworkPolicy isolation cells (#3907).

The two NetworkPolicy cells (cross_tenant_network_isolation, default_deny_egress)
publish a CONTROL-PLANE badge today: the policy is admitted, correctly shaped, and
binds the right Pod(s). That proves the boundary is *declared*, not that a packet is
*dropped on the wire*. This module is the data-plane next rung — an in-Pod exec
connectivity probe that upgrades the badge from ``control-plane`` to ``enforced`` only
when traffic the policy should block is actually blocked AND traffic it should permit
still flows.

## Split: pure core vs. thin I/O (mirrors ttfe_probe)

Everything that decides the verdict is pure and fully offline-testable:

  - ``dataplane_probe_enabled()`` — the default-off flag gate.
  - ``connection_command()``      — the canonical connect-and-print-sentinel argv.
  - ``classify_connection()``     — did the probe's stdout say CONNECTED / REFUSED?
  - ``classify_dataplane()``      — map the (deny, control) halves to a verdict +
                                    badge_scope override.

Only ``exec_connection()`` / ``start_listener()`` / ``stop_listener()`` touch the
cluster, and they lazy-import ``kubernetes.stream`` *inside* the call so importing
this module (offline tests, stdlib-only renderer) never needs the kubernetes client.

## Honest failure posture

An exec that errors, times out, or returns neither sentinel is INCONCLUSIVE
(``None``), never a crash and never a fabricated enforcement claim. An inconclusive
probe degrades the cell to the existing control-plane badge — exactly what an
unset flag produces — so a flaky exec surface can never manufacture an ``enforced``
badge nor a false FAIL. The only states that move the badge are a clean two-sided
confirmation (``enforced``) or a clean breach (deny path flowed -> FAIL).

## Default-off + #2082 provenance (corrected 2026-07-18 — see #3950/#291)

Gated behind ``BENCH_NETPOL_DATAPLANE_PROBE`` (default-off). Unset -> the cells never
call in here and behave byte-identically to the control-plane badge.

Both scored cells (cross_tenant_network_isolation, default_deny_egress) build and bind
their OWN scenario-scoped ``standard-np`` NetworkPolicy — keyed on bench-owned
``honest-bench/scenario``(+``/tenant``) labels the harness itself sets on
podTemplate.metadata — and already verify that binding at admission time (that is the
existing control-plane badge). **Neither cell probes the gke-sandbox controller's
auto-managed NetworkPolicy** (the #2082 subject, keyed on
``agents.x-k8s.io/sandbox-template-ref-hash``). The paragraph this replaces claimed the
armed probe was "EXPECTED to read a breach" because "the managed-NetworkPolicy
podSelector keys on a label the bound Pods do not carry" — that described a mechanism
these cells do not actually exercise; corrected by a4s2 (source read of both scenario
files, 2026-07-18) after a4s1 independently found live evidence the managed-NP label
mismatch is itself now resolved upstream (#1067, verified against controller digest
``b54aefdf``). Either way, #2082's status was never load-bearing for this flip — these
cells were never coupled to the managed-NP mechanism.

So arming the probe answered an EMPIRICAL question, not a known-outcome flip: whether
the cluster's CNI (Calico / GKE Dataplane V2) actually enforces the already-admitted,
already-correctly-bound standard-np policy on the wire. The first armed fire
(the persistent internal cluster, 2026-07-18) read a clean two-sided confirmation for both
cells -> ``PASS (enforced)`` at ``badge_construction=standard-np`` (see the
``dataplane_probe_enabled()`` docstring below). The never-raises -> inconclusive ->
control-plane safety net means a mis-tuned probe degrades safely rather than
mispublishing either way. The charter-#5-gated page-flip decision (owned by the lead,
tracked in #3950) is about the ``badge_construction`` disclosure (standard-np vs
managed-np) that accompanies the flip — already emitted by both cells today — not about
a managed-NP dependency.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

log = logging.getLogger("sandbox-scenario.netpol-probe")

# Sentinels the connect command prints so stdout classification is a substring test
# (no exit-code channel parsing) — same robustness trick as ttfe_probe's token.
_CONN_OK = "NETPOL_CONN_OK"
_CONN_REFUSED = "NETPOL_CONN_REFUSED"

_PROBE_ENV = "BENCH_NETPOL_DATAPLANE_PROBE"


def dataplane_probe_enabled() -> bool:
    """True iff BENCH_NETPOL_DATAPLANE_PROBE selects the data-plane probe.

    ARMED in the publish path (#3950, 2026-07-18): the gke-sandbox refresh CI sets
    BENCH_NETPOL_DATAPLANE_PROBE=1 (cloudbuild-refresh-gke-sandbox.yaml, measure
    step), so the two isolation cells publish "enforced" BY CONSTRUCTION when the
    in-Pod probe confirms the CNI blocks the traffic on the wire. Empirically
    verified ENFORCED on the persistent internal cluster 2026-07-18 (both cells PASS
    enforced/standard-np). The cells build their OWN standard NetworkPolicy, never
    the gke-sandbox managed-NP, so charter-#5 (#139) managed-NP disclosure never
    bound this arm. The per-invocation default stays OFF because kind/CI-unit runs
    have no CNI enforcement to probe (an armed probe there would only ever go
    inconclusive) — off-by-default is the correct substrate-scoped default, not a
    dormant gate; the publish path arms it explicitly. Built in #3907.
    """
    return os.environ.get(_PROBE_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def connection_command(host: str, port: int, *, timeout_s: int = 3) -> list[str]:
    """argv that attempts a TCP connection and prints a fixed sentinel either way.

    Prints ``NETPOL_CONN_OK`` on a successful connect, ``NETPOL_CONN_REFUSED`` on a
    refused/timed-out connect — so the caller classifies the result from stdout, not
    from an exec exit-code channel (busybox nc's exit semantics vary by build). The
    ``-w`` timeout bounds a silently-dropped connection (the enforced case) so the
    probe returns within ``timeout_s`` rather than hanging.
    """
    target = f"nc -w {int(timeout_s)} {host} {int(port)} </dev/null 2>/dev/null"
    script = f"if {target}; then echo {_CONN_OK}; else echo {_CONN_REFUSED}; fi"
    return ["sh", "-c", script]


def listener_command(port: int) -> list[str]:
    """argv for a best-effort TCP listener on ``port`` that survives repeat connects.

    Loops a one-shot busybox ``nc -l`` so the listener accepts more than one probe
    connection within a fire (the blocked-source probe then the allowed-source
    probe). Run detached; ``stop_listener`` closes the stream. The exact invocation
    is tuned on the first armed fire — a listener that never comes up makes every
    connect read REFUSED, which the classifier maps to inconclusive/over-block, never
    a false ``enforced``.
    """
    return ["sh", "-c", f"while true; do nc -l -p {int(port)} >/dev/null 2>&1 || break; done"]


def classify_connection(stdout: object) -> Optional[bool]:
    """Map a connect probe's stdout to connected / refused / inconclusive.

    Returns True iff stdout carries the CONNECTED sentinel, False iff it carries the
    REFUSED sentinel, None if it carries neither (exec errored, container lacked nc,
    output garbled) — the inconclusive case. CONNECTED wins if both somehow appear
    (a real connection is the stronger signal).
    """
    if not isinstance(stdout, str):
        return None
    if _CONN_OK in stdout:
        return True
    if _CONN_REFUSED in stdout:
        return False
    return None


def classify_dataplane(
    deny_blocked: Optional[bool], control_allowed: Optional[bool]
) -> tuple[str, Optional[str]]:
    """Map the two probe halves to a (verdict, badge_scope_override) pair.

    deny_blocked    — did the connection the policy SHOULD block get blocked?
                      True=blocked (good), False=flowed (breach), None=inconclusive.
    control_allowed — did the connection that SHOULD still work actually work?
                      True=reachable (good), False=blocked (over-restrictive),
                      None=inconclusive.

    Precedence (loudest failure first):
      ("breach", None)        — deny_blocked is False: policy-blocked traffic flowed.
                                The cell FAILs (real enforcement gap, e.g. #2082).
      ("over-block", None)    — control_allowed is False: the policy also blocked
                                traffic it must permit. The cell FAILs.
      ("enforced","enforced") — both halves True: blocks the denied path AND permits
                                the control path. Cell stays PASS, badge -> enforced.
      ("inconclusive", None)  — any remaining None: probe could not run cleanly. Cell
                                stays PASS at the control-plane badge (degrade to the
                                admission proof — never a fabricated enforced/FAIL).

    badge_scope is ``"enforced"`` ONLY for the enforced verdict; every other verdict
    returns None so the caller keeps the static control-plane badge (or FAILs).
    """
    if deny_blocked is False:
        return ("breach", None)
    if control_allowed is False:
        return ("over-block", None)
    if deny_blocked is True and control_allowed is True:
        return ("enforced", "enforced")
    return ("inconclusive", None)


def exec_connection(
    core_v1,
    *,
    namespace: str,
    pod_name: str,
    host: str,
    port: int,
    container: Optional[str] = None,
    timeout_s: int = 3,
) -> Optional[bool]:
    """Run the connect probe in ``pod_name`` against host:port; report connected/refused/None.

    The ONE connect I/O surface. Returns True (connected), False (refused/timeout), or
    None (exec could not run / neither sentinel seen) — NEVER raises on a cluster/exec
    error, so an inconclusive exec degrades the probe to the control-plane badge rather
    than crashing the cell. Lazy-imports ``kubernetes.stream`` so the offline classifier
    tests need no client (mirrors ttfe_probe.probe_first_instruction).
    """
    try:
        from kubernetes.stream import stream as _k8s_stream
    except Exception:
        return None

    argv = connection_command(host, port, timeout_s=timeout_s)
    exec_kwargs = dict(
        command=argv,
        stderr=True,
        stdin=False,
        stdout=True,
        tty=False,
        _request_timeout=timeout_s + 5,
    )
    if container is not None:
        exec_kwargs["container"] = container
    try:
        stdout = _k8s_stream(
            core_v1.connect_get_namespaced_pod_exec, pod_name, namespace, **exec_kwargs
        )
    except Exception as exc:
        log.warning("netpol connect probe exec failed (%s) — inconclusive", exc)
        return None
    return classify_connection(stdout)


def start_listener(
    core_v1,
    *,
    namespace: str,
    pod_name: str,
    port: int,
    container: Optional[str] = None,
):
    """Start a detached TCP listener in ``pod_name``; return a handle or None.

    Best-effort: opens a non-preloaded exec stream running ``listener_command`` and
    returns the stream object (the caller passes it to ``stop_listener``). Returns
    None on any error — a failed listener simply makes the subsequent connects read
    REFUSED, which the classifier degrades to inconclusive/over-block, never a false
    ``enforced``. Never raises.
    """
    try:
        from kubernetes.stream import stream as _k8s_stream
    except Exception:
        return None
    exec_kwargs = dict(
        command=listener_command(port),
        stderr=True,
        stdin=False,
        stdout=True,
        tty=False,
        _preload_content=False,
    )
    if container is not None:
        exec_kwargs["container"] = container
    try:
        resp = _k8s_stream(
            core_v1.connect_get_namespaced_pod_exec, pod_name, namespace, **exec_kwargs
        )
        return resp
    except Exception as exc:
        log.warning("netpol listener exec failed to start (%s) — connects will read refused", exc)
        return None


def stop_listener(handle) -> None:
    """Best-effort close of a ``start_listener`` handle. Never raises."""
    if handle is None:
        return
    try:
        handle.close()
    except Exception:
        pass
