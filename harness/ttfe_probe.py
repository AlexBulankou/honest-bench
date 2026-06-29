"""Time-To-First-Execution (TTFE) exec probe — the create->first-instruction-result primitive.

TTFE is *create-to-first-instruction-result*, NOT create-to-Ready. A sandbox
that reports Ready is schedulable; a sandbox that returns the result of its
first executed instruction is *usable*. The gap between those two is exactly
the thing an AI-agent platform pays on every cold request, so TTFE — not
Ready-latency — is the honest activation metric, and it is the spine the
throughput-under-threshold and execution-success cells all derive from.

This module is the single primitive every TTFE-measuring scenario calls: it
runs one trivial instruction inside an already-Ready sandbox's backing Pod over
the Kubernetes exec channel, and reports (ttfe_ms, exec_ok). The scenarios own
the t0 (create()-return monotonic clock) and the Ready/bind wait; this module
owns only the exec round-trip and the timestamp of its result.

## Split: pure core vs. one I/O wrapper

The classification and timing arithmetic is pure and fully offline-testable:

  - ``first_instruction()``      — the canonical (argv, expected-token) pair.
  - ``classify_exec()``          — did the instruction's stdout carry the token?
  - ``ttfe_ms()``                — monotonic span create->result, in ms.
  - ``resolve_probe_result()``   — combine the two into (ttfe_ms_or_None, exec_ok),
                                   the exact shape a scenario feeds the metrics core.

Only ``probe_first_instruction()`` touches the cluster, and it lazily imports
``kubernetes.stream`` *inside* the call so importing this module (e.g. for the
offline tests, or the stdlib-only renderer path) never needs the kubernetes
client installed.

## Honest failure posture

An exec that errors, times out, or returns the wrong/empty stdout is a real
execution failure — the sandbox went Ready but could not run an instruction.
The I/O wrapper NEVER raises on that: it returns ``(None, False)`` so the
caller records a failed execution (dragging exec_success_rate down) rather than
crashing the whole scenario. It raises only on a programming error in its own
arguments (e.g. a negative monotonic span), never on cluster/exec conditions.
"""

from __future__ import annotations

# A trivial, side-effect-free instruction whose stdout is a fixed sentinel. We
# assert the sentinel is present rather than merely that exec returned, so a
# shell that starts but produces no/garbled output counts as a FAILED execution
# (the honest signal), not a spurious success.
_PROBE_TOKEN = "ttfe-probe-ok"
_PROBE_ARGV = ["sh", "-c", "printf %s ttfe-probe-ok"]


def first_instruction() -> tuple[list[str], str]:
    """Return the canonical (argv, expected-token) for the first instruction.

    A fresh copy of the argv list is returned each call so a caller mutating it
    cannot poison the module constant.
    """
    return list(_PROBE_ARGV), _PROBE_TOKEN


def classify_exec(stdout: object, expected_token: str) -> bool:
    """True iff ``expected_token`` appears in the instruction's stdout.

    Substring (not equality) so incidental channel framing/whitespace around
    the token does not flip a genuine success to a failure. A non-string
    stdout (None, bytes-that-failed-to-decode, etc.) is a failed execution.
    """
    if not isinstance(stdout, str):
        return False
    if not expected_token:
        return False
    return expected_token in stdout


def ttfe_ms(create_monotonic: float, result_monotonic: float) -> float:
    """Milliseconds from create()-return (t0) to first-instruction-result (t1).

    Both args are ``time.monotonic()`` readings. ``result_monotonic`` must be
    at or after ``create_monotonic`` — a negative span is a caller bug (clocks
    swapped / t0 never recorded), so we raise rather than emit a nonsense
    negative latency into the histogram.
    """
    create_f = float(create_monotonic)
    result_f = float(result_monotonic)
    span = result_f - create_f
    if span < 0:
        raise ValueError(
            f"negative TTFE span: result_monotonic ({result_f}) precedes "
            f"create_monotonic ({create_f})"
        )
    return span * 1000.0


def resolve_probe_result(
    stdout: object,
    expected_token: str,
    create_monotonic: float,
    result_monotonic: float,
) -> tuple[float | None, bool]:
    """Combine classification + timing into the scenario-facing result shape.

    Returns ``(ttfe_ms, exec_ok)``:
      - exec_ok      — whether the first instruction's stdout carried the token.
      - ttfe_ms      — the create->result latency in ms when exec_ok, else None.

    A FAILED execution contributes to exec_success_rate (as a 0) but NOT to the
    TTFE histogram — a sandbox that never ran an instruction has no honest
    first-instruction latency to record, so its ttfe_ms is None and the caller
    drops it from the percentile/throughput inputs. This keeps the latency
    distribution to genuine successful executions while the failure still drags
    the success rate, exactly as the doc's resume row does (TTFE p50/p95 over
    the 1277 that succeeded; success-rate 1277/1376).

    Pure: the timing arithmetic still validates (a negative span raises) even on
    a failed exec, so a clock bug surfaces regardless of exec outcome.
    """
    ok = classify_exec(stdout, expected_token)
    span_ms = ttfe_ms(create_monotonic, result_monotonic)
    if not ok:
        return None, False
    return span_ms, True


def probe_first_instruction(
    core_v1,
    *,
    pod_name: str,
    namespace: str,
    create_monotonic: float,
    container: str | None = None,
    timeout_s: float = 30.0,
):
    """Run the first instruction in ``pod_name`` and report (ttfe_ms, exec_ok).

    The ONE I/O surface. ``core_v1`` is a ``kubernetes.client.CoreV1Api``;
    ``create_monotonic`` is the scenario's t0 (its ``time.monotonic()`` taken
    right after the sandbox create() returned). ``container`` pins the exec
    target when the backing Pod has more than one container; left None the
    cluster picks the default.

    Returns ``(ttfe_ms_or_None, exec_ok)`` — never raises on an exec/cluster
    error. A timeout, websocket error, RBAC denial, or wrong/empty stdout all
    collapse to ``(None, False)``: a real, recorded execution failure rather
    than a scenario crash. The result timestamp t1 is taken the instant the
    exec call returns its captured stdout.

    The ``kubernetes.stream`` import is lazy (inside the function) so importing
    this module never requires the kubernetes client — the offline tests and
    the stdlib-only renderer path import ``ttfe_probe`` for its pure functions
    without pulling the client in.
    """
    import time

    try:
        from kubernetes.stream import stream as _k8s_stream
    except Exception:
        # Client not installed / import broke — treat as a failed execution
        # rather than crashing a caller that only wanted the best-effort probe.
        return None, False

    argv, token = first_instruction()
    exec_kwargs = dict(
        command=argv,
        stderr=True,
        stdin=False,
        stdout=True,
        tty=False,
        _request_timeout=timeout_s,
    )
    if container is not None:
        exec_kwargs["container"] = container

    try:
        stdout = _k8s_stream(
            core_v1.connect_get_namespaced_pod_exec,
            pod_name,
            namespace,
            **exec_kwargs,
        )
    except Exception:
        # Exec channel failed to open / errored / timed out — failed execution.
        return None, False

    # t1: the instant the instruction's result is in hand.
    result_monotonic = time.monotonic()
    return resolve_probe_result(stdout, token, create_monotonic, result_monotonic)
