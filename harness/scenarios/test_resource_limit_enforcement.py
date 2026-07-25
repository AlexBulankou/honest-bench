"""Cluster-free tests for the INERT resource_limit_enforcement cell (#5634).

Dependency-free: `python3 test_resource_limit_enforcement.py` (exit 0 = pass).
Asserts the born-pending contract: run() reports pending(not-yet-measured),
returns the standard 3-tuple, and touches no cluster (stdlib-only, no kubernetes
import) so the INERT slice renders an honest pending cell at zero fire cost.
"""

try:  # cwd == scenarios/ (dependency-free `python3 test_resource_limit_enforcement.py`)
    import resource_limit_enforcement as cell
except ModuleNotFoundError:  # repo-root pytest: scenarios/ is a package, not on sys.path
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import resource_limit_enforcement as cell


def test_run_returns_pending_not_yet_measured():
    outcome, excerpt, sla = cell.run("resource_limit_enforcement")
    assert outcome == "pending"
    assert sla == {"pending_reason": "not-yet-measured"}
    assert isinstance(excerpt, str) and excerpt


def test_run_returns_three_tuple():
    result = cell.run("resource_limit_enforcement")
    assert isinstance(result, tuple) and len(result) == 3


def test_module_imports_no_kubernetes():
    # The INERT cell must not pull the kubernetes client (or any third-party dep):
    # it is a pure stdlib no-op until the fill-fire. A stray import would both slow
    # the kind runner and violate the reproducible-surface declared-dependency lock.
    import ast
    import pathlib

    src = pathlib.Path(cell.__file__).read_text()
    tree = ast.parse(src)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                imported.add(a.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".", 1)[0])
    assert "kubernetes" not in imported
    assert imported <= {"__future__", "logging"}, f"unexpected imports: {imported}"


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok: {fn.__name__}")
    print(f"test_resource_limit_enforcement: all {len(fns)} assertions passed")


if __name__ == "__main__":
    _run_all()
