"""Cluster-free tests for the resource_limit_enforcement cell (#5634).

Dependency-free: `python3 test_resource_limit_enforcement.py` (exit 0 = pass).
Two concerns:

  1. The pure core (classify_containment / classify_enforcement /
     memory_overshoot_command / overshoot_probe_enabled) decides the verdict and
     is fully offline-testable — no cluster, no kubernetes import.
  2. The arm gate default (BENCH_RLIMIT_OVERSHOOT_PROBE off) keeps run() a pure
     stdlib no-op that reports pending(not-yet-measured) and touches no cluster,
     so the substrate-scoped default renders an honest pending cell at zero cost.

Import-purity is asserted structurally: the module is stdlib-only at import
(kubernetes is lazy-imported INSIDE run()), so importing it for offline tests or
the stdlib-only renderer never pulls the client.
"""

import os

try:  # cwd == scenarios/ (dependency-free `python3 test_resource_limit_enforcement.py`)
    import resource_limit_enforcement as cell
except ModuleNotFoundError:  # repo-root pytest: scenarios/ is a package, not on sys.path
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import resource_limit_enforcement as cell


# --- arm gate off (INERT default): pending(not-yet-measured), no cluster ------


def _with_probe_env(value):
    """Context-free helper: set/clear the arm-gate env var, return the prior value."""
    prior = os.environ.get(cell._PROBE_ENV)
    if value is None:
        os.environ.pop(cell._PROBE_ENV, None)
    else:
        os.environ[cell._PROBE_ENV] = value
    return prior


def _restore_probe_env(prior):
    if prior is None:
        os.environ.pop(cell._PROBE_ENV, None)
    else:
        os.environ[cell._PROBE_ENV] = prior


def test_run_returns_pending_not_yet_measured_when_unarmed():
    prior = _with_probe_env(None)
    try:
        outcome, excerpt, sla = cell.run("resource_limit_enforcement")
        assert outcome == "pending"
        assert sla == {"pending_reason": "not-yet-measured"}
        assert isinstance(excerpt, str) and excerpt
    finally:
        _restore_probe_env(prior)


def test_run_returns_three_tuple_when_unarmed():
    prior = _with_probe_env(None)
    try:
        result = cell.run("resource_limit_enforcement")
        assert isinstance(result, tuple) and len(result) == 3
    finally:
        _restore_probe_env(prior)


# --- arm gate flag parsing ---------------------------------------------------


def test_overshoot_probe_enabled_reads_env():
    prior = _with_probe_env(None)
    try:
        for truthy in ("1", "true", "TRUE", "yes", "on", " On "):
            os.environ[cell._PROBE_ENV] = truthy
            assert cell.overshoot_probe_enabled() is True, truthy
        for falsy in ("0", "false", "no", "off", "", "  "):
            os.environ[cell._PROBE_ENV] = falsy
            assert cell.overshoot_probe_enabled() is False, falsy
        os.environ.pop(cell._PROBE_ENV, None)
        assert cell.overshoot_probe_enabled() is False  # unset ⇒ off
    finally:
        _restore_probe_env(prior)


# --- pure core: memory_overshoot_command -------------------------------------


def test_memory_overshoot_command_shape():
    argv = cell.memory_overshoot_command(256)
    assert argv[:2] == ["python3", "-c"]
    body = argv[2]
    # allocates the requested MiB of real, page-touched anon memory then prints the sentinel
    assert "256*1024*1024" in body
    assert "bytearray(n)" in body
    assert "range(0,n,4096)" in body
    assert cell._ALLOCATED_SENTINEL in body


def test_memory_overshoot_command_coerces_int():
    # a str MiB (e.g. straight from an env knob) must not inject into the argv body
    argv = cell.memory_overshoot_command("64")  # type: ignore[arg-type]
    assert "64*1024*1024" in argv[2]


# --- pure core: classify_containment -----------------------------------------


def test_classify_containment_oomkilled_is_contained():
    assert cell.classify_containment("OOMKilled", 137) is True
    assert cell.classify_containment("OOMKilled", None) is True  # reason wins


def test_classify_containment_exit_137_is_contained():
    assert cell.classify_containment("Error", 137) is True
    assert cell.classify_containment(None, 137) is True


def test_classify_containment_exit_0_is_breach():
    assert cell.classify_containment("Completed", 0) is False
    assert cell.classify_containment(None, 0) is False


def test_classify_containment_other_is_inconclusive():
    assert cell.classify_containment("Error", 1) is None
    assert cell.classify_containment(None, None) is None
    assert cell.classify_containment("Error", 143) is None


def test_classify_containment_bool_exit_code_not_aliased():
    # bool is an int subclass; True/False must NOT alias 1/0 → inconclusive
    assert cell.classify_containment(None, True) is None
    assert cell.classify_containment(None, False) is None


# --- pure core: classify_enforcement -----------------------------------------


def test_classify_enforcement_contained_is_pass_enforced_pair():
    outcome, scope, construction = cell.classify_enforcement(True)
    assert outcome == "PASS"
    assert scope == "enforced"
    assert construction == "footprint-overshoot"


def test_classify_enforcement_breach_is_fail_no_badge():
    outcome, scope, construction = cell.classify_enforcement(False)
    assert outcome == "FAIL"
    assert scope is None and construction is None


def test_classify_enforcement_inconclusive_is_pending_no_badge():
    outcome, scope, construction = cell.classify_enforcement(None)
    assert outcome == "pending"
    assert scope is None and construction is None


# --- schema parity: the emitted enum values are in the render/harness allow-lists


def test_emitted_badge_values_in_harness_enums():
    import importlib
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    results_schema = importlib.import_module("results_schema")
    _, scope, construction = cell.classify_enforcement(True)
    assert scope in results_schema.BADGE_SCOPE_ENUM
    assert construction in results_schema.BADGE_CONSTRUCTION_ENUM
    assert cell._PENDING_NOT_MEASURED in results_schema.PENDING_REASON_ENUM
    assert cell._PENDING_INCONCLUSIVE in results_schema.PENDING_REASON_ENUM


# --- import purity: stdlib-only at module load (kubernetes lazy inside run()) -


def test_module_imports_no_kubernetes():
    # The module must be stdlib-only AT IMPORT: kubernetes and the sibling
    # ._apiversion / ._kube helpers are lazy-imported INSIDE run(), so importing
    # this module (offline tests / stdlib-only renderer) never pulls the client or
    # needs a package context. A stray MODULE-LEVEL third-party import would slow
    # the kind runner and violate the reproducible-surface declared-dependency lock.
    #
    # We inspect only TOP-LEVEL statements (tree.body), NOT ast.walk — the lazy
    # imports inside run()/_cleanup are the design and must be permitted; only the
    # module-load surface is asserted stdlib-only.
    import ast
    import pathlib

    src = pathlib.Path(cell.__file__).read_text()
    tree = ast.parse(src)
    imported = set()
    for node in tree.body:  # top-level only — do not descend into function bodies
        if isinstance(node, ast.Import):
            for a in node.names:
                imported.add(a.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".", 1)[0])
    assert "kubernetes" not in imported
    assert imported <= {
        "__future__",
        "typing",
        "logging",
        "os",
        "time",
        "uuid",
    }, f"unexpected module-level imports: {imported}"


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok: {fn.__name__}")
    print(f"test_resource_limit_enforcement: all {len(fns)} assertions passed")


if __name__ == "__main__":
    _run_all()
