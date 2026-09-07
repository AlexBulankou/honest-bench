"""hb#823: class-level guard against the single-poll pool-warm-wait defect.

hb#379/hb#818/hb#820 fixed the SAME defect four separate times, one call site at
a time: a function that polls SandboxWarmPool.status.readyReplicas in a `while`
loop and gates its return/raise on a single instantaneous `>= target` comparison
declares the pool "warm" on a momentary peak that may already be draining (the
upstream reconciler gates refill on a COUNT of extant sandboxes, not
readyReplicas, so it can stall mid-drain). Point-fixing hb#379 -> hb#818 ->
hb#820 -> hb#823 closed four instances but never the CLASS: nothing stopped a
fifth pool-warm-wait function from being added tomorrow with the same
single-poll bug.

This lint closes the class. It statically finds every "pool-warm wait gate"
function under harness/scenarios/*.py — defined as: a function containing a
`while` loop that, somewhere in its body, compares a value against an operand
whose name contains "target" using `>=` (or the reversed `<=`) — and asserts
its signature carries a `stability_polls` parameter. A future call site with
the same single-poll shape fails this test immediately, by construction,
without needing its own hand-written regression test.

## Why AST, not a text/regex grep

A naive `grep -l readyReplicas | grep -l while` over-fires: warmpool_cold_start.py
has two DIAGNOSTIC samplers (`_sample_pool_ready`, `_run_pool_ready_sampler`) that
both mention "readyReplicas" and run inside a `while` loop, but neither gates a
return/raise on a `>= target` comparison — they unconditionally sample forever
until an external stop_event fires. A text-based lint would falsely flag both.
The AST walk keys on the actual gating shape (a `>=`/`<=` comparison against a
"target"-named operand, nested inside the function's `while` loop) rather than
substring co-occurrence, so it fires exactly on genuine wait-gates and is silent
on samplers, sleep-loops, and anything else that merely mentions the same words.

## The second false-positive class: edge-detection measurements

The while-loop + target-threshold shape alone still over-fires on one more kind
of function: session_turnover.py's `_measure_one_refill` polls readyReplicas
against a target too, but it is not a "wait until warm, then proceed" gate — it
is a two-phase drop-then-refill EDGE DETECTOR that IS the scenario's headline
latency metric (it records `t_drop` on the instant readyReplicas first falls
below target, then returns the elapsed time to the instant it first recovers).
Requiring N consecutive polls here would corrupt the metric: it would add
artificial latency to every measured refill time, which is the opposite of
what hb#823 is trying to fix. The tell is structural: a genuine wait-gate has
no notion of "already dropped" — it only ever asks "are we at/above target
right now". An edge detector necessarily carries a `drop`-named state variable
(`t_drop` here) that its threshold branch is conditioned on. Excluding any
candidate whose function body references a `drop`-named identifier keys on
that structural difference rather than hand-listing `_measure_one_refill` by
name, so the exclusion still holds if the function is ever renamed or a
similar edge-detector is added elsewhere.

Dependency-free: `python3 test_pool_warm_stability_lint.py` (exit 0 = pass).
"""

import ast
import pathlib

_SCENARIOS_DIR = pathlib.Path(__file__).resolve().parent
_SELF_NAME = pathlib.Path(__file__).name


def _target_named(node: ast.AST) -> bool:
    """True if `node` is a Name/Attribute whose identifier contains 'target'."""
    if isinstance(node, ast.Name):
        return "target" in node.id.lower()
    if isinstance(node, ast.Attribute):
        return "target" in node.attr.lower()
    return False


def _has_target_threshold_compare(node: ast.AST) -> bool:
    """True if `node` (or anything nested under it) is a Compare using >=/<=
    where one side is a 'target'-named operand — the pool-warm gate shape."""
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Compare):
            continue
        operands = [sub.left, *sub.comparators]
        ops = sub.ops
        for i, op in enumerate(ops):
            if not isinstance(op, (ast.GtE, ast.LtE)):
                continue
            lhs, rhs = operands[i], operands[i + 1]
            if _target_named(lhs) or _target_named(rhs):
                return True
    return False


def _tracks_drop_state(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True if `func` references any Name/Attribute whose identifier contains
    'drop' — the tell of an edge-detection measurement (records the instant a
    value first falls below a threshold) rather than a steady-state wait-gate.
    See the module docstring's "second false-positive class" section."""
    for sub in ast.walk(func):
        if isinstance(sub, ast.Name) and "drop" in sub.id.lower():
            return True
        if isinstance(sub, ast.Attribute) and "drop" in sub.attr.lower():
            return True
    return False


def _is_pool_warm_wait_gate(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True if `func` contains a `while` loop that itself contains a
    target-threshold comparison — the shape common to every fixed instance
    (hb#379/hb#818/hb#820/hb#823) of the single-poll defect — and `func` is
    not itself an edge-detection measurement (see `_tracks_drop_state`)."""
    if _tracks_drop_state(func):
        return False
    for node in ast.walk(func):
        if isinstance(node, ast.While) and _has_target_threshold_compare(node):
            return True
    return False


def _param_names(func: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    args = func.args
    names = {a.arg for a in args.args}
    names |= {a.arg for a in args.posonlyargs}
    names |= {a.arg for a in args.kwonlyargs}
    return names


def _find_gate_functions(path: pathlib.Path):
    """Yield (function_name, has_stability_polls) for every pool-warm wait-gate
    function defined at any nesting level in `path`."""
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not _is_pool_warm_wait_gate(node):
            continue
        yield node.name, "stability_polls" in _param_names(node)


def _scenario_files():
    for path in sorted(_SCENARIOS_DIR.glob("*.py")):
        if path.name == _SELF_NAME:
            continue
        if path.name.startswith("test_"):
            continue
        if path.name.startswith("_"):
            continue
        yield path


def test_every_pool_warm_wait_gate_has_stability_polls_param():
    violations = []
    scanned_any_gate = False
    for path in _scenario_files():
        for func_name, has_param in _find_gate_functions(path):
            scanned_any_gate = True
            if not has_param:
                violations.append(f"{path.name}:{func_name}")
    assert scanned_any_gate, (
        "expected at least one pool-warm wait-gate function across "
        "harness/scenarios/*.py — the discriminator itself may be broken"
    )
    assert not violations, (
        "pool-warm wait-gate function(s) missing a `stability_polls` param "
        "(hb#379/hb#818/hb#820/hb#823 defect class): " + ", ".join(violations)
    )


def test_known_current_gate_functions_are_detected():
    # Pins the discriminator against the 4 known call sites (hb#823 body) so a
    # future refactor that accidentally narrows the AST match still gets caught
    # even if every gate happens to already carry stability_polls (which would
    # otherwise make the assertion above vacuously pass with 0 gates scanned).
    found = {}
    for path in _scenario_files():
        for func_name, _has_param in _find_gate_functions(path):
            found.setdefault(path.name, set()).add(func_name)

    expected = {
        "burst_create.py": "_wait_for_pool_warm",
        "scale_slope.py": "_wait_for_pool_warm",
        "session_turnover.py": "_wait_pool_at_least",
        "warmpool_cold_start.py": "_wait_for_pool_warm",
    }
    for filename, func_name in expected.items():
        assert filename in found, f"expected {filename} to be scanned"
        assert func_name in found[filename], (
            f"expected {filename} to have detected gate function {func_name}, "
            f"got {found[filename]}"
        )


def test_diagnostic_samplers_are_not_falsely_flagged():
    # warmpool_cold_start.py's two diagnostic samplers mention readyReplicas and
    # loop in a `while`, but never gate on a target-threshold comparison — the
    # AST discriminator must stay silent on them (a naive text/regex lint would
    # not).
    path = _SCENARIOS_DIR / "warmpool_cold_start.py"
    found_names = {name for name, _has_param in _find_gate_functions(path)}
    assert "_sample_pool_ready" not in found_names
    assert "_run_pool_ready_sampler" not in found_names


def test_edge_detection_measurement_is_not_falsely_flagged():
    # session_turnover.py's _measure_one_refill polls readyReplicas against a
    # target too, but it's a drop-then-refill edge-detection latency
    # measurement (the scenario's headline metric), not a wait-gate — forcing
    # stability_polls onto it would corrupt the metric. The `drop`-state-var
    # exclusion in _tracks_drop_state must keep it out of the flagged set.
    path = _SCENARIOS_DIR / "session_turnover.py"
    found_names = {name for name, _has_param in _find_gate_functions(path)}
    assert "_measure_one_refill" not in found_names
    assert "_wait_pool_at_least" in found_names


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok: {fn.__name__}")
    print(f"test_pool_warm_stability_lint: all {len(fns)} assertions passed")


if __name__ == "__main__":
    _run_all()
