"""Offline portability lock for the kind-reproducible scenario suite — no cluster,
no network, no I/O beyond reading the harness source tree.

Run with bare python3 (no pytest, so the auto-refresh GH-runner needs nothing
extra):  python3 -m harness.test_scenario_portability
or directly:               python3 harness/test_scenario_portability.py

WHY THIS EXISTS — the honest-by-construction kind path's blind spot.
`harness.run.run_suite` wraps each cell in `try/except` and turns ANY scenario
exception into an `outcome: "fail"` cell (a crash is a FAIL cell, not a suite
abort). That is the right liveness posture for a live run, but it has a honesty
cost on the PUBLIC page: a scenario that crashes on the fresh kind GitHub-runner
for a HARNESS reason — a top-level import of a dependency not in
`harness/requirements.txt`, or a missing `run` entrypoint — renders as a `FAIL`
cell that is indistinguishable from a real measured failure. The reproduce recipe
promises "every published cell comes from the commands below … if you get a
different number, that is a bug worth an issue" — a fake FAIL silently breaks that
promise. Nothing offline caught this class before this test.

WHAT IT LOCKS (all pure — import + AST static analysis, zero cluster calls):

  test_every_registered_cell_module_imports_with_run_contract
    Every cell in scenario_map.CELLS_BY_PRODUCT imports cleanly under the package
    path AND exposes a callable `run` taking one positional arg (the
    `run(scenario_name) -> (outcome, excerpt, sla_metrics)` contract run._run_one
    drives). A top-level import crash or a missing/!callable run fails HERE,
    offline, instead of as a fake `fail` cell on the kind page.

  test_no_undeclared_thirdparty_import_in_reproducible_surface
    The kind GH-runner does `pip install -r harness/requirements.txt` and nothing
    else, so the suite's ONLY third-party dependency must be the declared set
    (today: kubernetes). This walks EVERY import (top-level AND deferred-in-function
    — a deferred `import google.cloud.storage` would crash mid-run on kind exactly
    as a top-level one would) across BOTH packages the refresh runs — harness/
    (`python3 -m harness.run`, step 2) AND render/ (`python3 -m render.generate`,
    step 3) — and asserts each import name is stdlib, a local module, or declared in
    requirements.txt. An undeclared dep in a scenario renders one fake FAIL cell; in
    render/ it aborts the WHOLE refresh (render.generate is not try/except-wrapped).
    A future PR that adds an undeclared dep to either surface fails HERE.

  test_cell_field_invariants
    Each Cell's substrate-gate fields are well-formed so the run loop's pending /
    badge paths stay honest: requires_substrate is a known level; pending_reason is
    present iff requires_substrate is (a gated cell must say why) and is in
    PENDING_REASON_ENUM; badge_scope, when set, is in BADGE_SCOPE_ENUM.

  test_substrate_gate_semantics
    substrate_satisfies matches the kind-vs-GKE portability fact: a requires=None
    cell runs on kind; a gke cell pends on kind but runs on gke/gke-sandbox; a
    gke-sandbox cell runs only on gke-sandbox. This is what decides, per cell,
    whether the kind run measures it for real or renders honest-pending.
"""

from __future__ import annotations

# Make this file runnable BOTH as `python3 harness/test_x.py` and
# `python3 -m harness.test_x` by putting the repo root on sys.path before
# the absolute `from harness import ...` below (mirrors test_warm_vs_cold.py).
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import ast
import importlib
import inspect
import pathlib

from harness import results_schema
from harness.scenario_map import (
    CELLS_BY_PRODUCT,
    _SUBSTRATE_SATISFIES,
    Cell,
    substrate_satisfies,
)

_HARNESS_DIR = pathlib.Path(__file__).resolve().parent
_SCENARIOS_PKG = "harness.scenarios"
_REPO_ROOT = _HARNESS_DIR.parent
# The reproducible surface the kind GH-runner executes is BOTH packages: it runs
# `python3 -m harness.run` (step 2) AND `python3 -m render.generate` (step 3),
# with only `pip install -r harness/requirements.txt` and nothing else. So the
# dep-lock must walk render/ too — an undeclared import there crashes the refresh
# at render.generate, which (unlike a per-cell scenario crash) is NOT wrapped in
# try/except, so it aborts the whole run rather than rendering one fake FAIL cell.
_RENDER_DIR = _REPO_ROOT / "render"
_REPRODUCIBLE_DIRS = [_HARNESS_DIR, _RENDER_DIR]


def _check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _registered_modules() -> list[str]:
    """Every distinct scenario module basename across all product suites."""
    return sorted({c.module for cells in CELLS_BY_PRODUCT.values() for c in cells})


# --- import + contract -------------------------------------------------------


def test_every_registered_cell_module_imports_with_run_contract():
    for module in _registered_modules():
        try:
            mod = importlib.import_module(f"{_SCENARIOS_PKG}.{module}")
        except Exception as exc:  # noqa: BLE001 — any import crash is the bug
            raise AssertionError(
                f"scenario module {module!r} failed to import "
                f"({type(exc).__name__}: {exc}) — on the kind runner this would "
                f"render as a fake FAIL cell, not honest-pending"
            )
        run = getattr(mod, "run", None)
        _check(callable(run), f"scenario {module!r} has no callable run entrypoint")
        # run(scenario_name) — exactly one required positional param (the contract
        # run._run_one calls as mod.run(cell.module)).
        params = [
            p
            for p in inspect.signature(run).parameters.values()
            if p.kind
            in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
            and p.default is p.empty
        ]
        _check(
            len(params) == 1,
            f"scenario {module!r} run must take exactly one required positional "
            f"arg (scenario_name); got {len(params)}",
        )


# --- declared-dependency lock ------------------------------------------------


def _declared_thirdparty() -> set[str]:
    """Top-level package names declared in harness/requirements.txt."""
    req = _HARNESS_DIR / "requirements.txt"
    out: set[str] = set()
    for line in req.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        # strip version specifiers / extras: "kubernetes>=29.0.0" -> "kubernetes"
        name = line
        for sep in ("==", ">=", "<=", "~=", ">", "<", "!=", "[", ";", " "):
            name = name.split(sep, 1)[0]
        name = name.strip()
        if name:
            out.add(name)
    return out


def _local_module_names() -> set[str]:
    """Module basenames resolvable within the reproducible surface (harness/ +
    render/), so script-mode bare imports like `import metrics` / `from _kube
    import ...` are recognised as local, not third-party."""
    names = {"harness", "scenarios", "render"}
    for base in _REPRODUCIBLE_DIRS:
        for path in base.rglob("*.py"):
            names.add(path.stem)
            if path.name == "__init__.py":
                names.add(path.parent.name)
    return names


def _imported_top_names(tree: ast.AST) -> set[str]:
    """All absolute (level-0) import top-names anywhere in the module — including
    deferred imports inside functions, which crash on kind just the same."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:  # skip relative (.x / ..x) imports
                names.add(node.module.split(".", 1)[0])
    return names


def test_no_undeclared_thirdparty_import_in_reproducible_surface():
    stdlib = set(_sys.stdlib_module_names)
    declared = _declared_thirdparty()
    local = _local_module_names()
    allowed = stdlib | declared | local
    offenders: list[str] = []
    for base in _REPRODUCIBLE_DIRS:
        for path in sorted(base.rglob("*.py")):
            if path.name.startswith("test_"):
                continue
            tree = ast.parse(path.read_text())
            for name in sorted(_imported_top_names(tree)):
                if name not in allowed:
                    rel = path.relative_to(_REPO_ROOT)
                    offenders.append(f"{rel}: imports {name!r}")
    _check(
        not offenders,
        "undeclared third-party import(s) in the kind-reproducible surface "
        "(harness/ + render/, both run by the GH-runner with only "
        "`pip install -r harness/requirements.txt`) — add to "
        "harness/requirements.txt, or the refresh crashes: an undeclared import "
        "in a scenario renders one fake FAIL cell, but one in render/ aborts the "
        "WHOLE refresh at render.generate (not try/except-wrapped):\n  "
        + "\n  ".join(offenders),
    )


# --- cell field invariants ---------------------------------------------------


def _all_cells() -> list[Cell]:
    return [c for cells in CELLS_BY_PRODUCT.values() for c in cells]


def test_cell_field_invariants():
    for c in _all_cells():
        _check(
            c.requires_substrate in _SUBSTRATE_SATISFIES,
            f"cell {c.module!r} requires_substrate {c.requires_substrate!r} not a "
            f"known level {sorted(k for k in _SUBSTRATE_SATISFIES if k)}",
        )
        if c.requires_substrate is None:
            _check(
                c.pending_reason is None,
                f"cell {c.module!r} runs on every substrate but declares a "
                f"pending_reason {c.pending_reason!r} (it never pends)",
            )
        else:
            _check(
                c.pending_reason is not None,
                f"cell {c.module!r} is substrate-gated but declares no "
                f"pending_reason — a gated cell must say why it pends",
            )
            _check(
                c.pending_reason in results_schema.PENDING_REASON_ENUM,
                f"cell {c.module!r} pending_reason {c.pending_reason!r} not in "
                f"{results_schema.PENDING_REASON_ENUM}",
            )
        if c.badge_scope is not None:
            _check(
                c.badge_scope in results_schema.BADGE_SCOPE_ENUM,
                f"cell {c.module!r} badge_scope {c.badge_scope!r} not in "
                f"{results_schema.BADGE_SCOPE_ENUM}",
            )


# --- substrate gate semantics ------------------------------------------------


def test_substrate_gate_semantics():
    any_cell = Cell("x", requires_substrate=None)
    gke_cell = Cell("y", requires_substrate="gke", pending_reason="requires-gke")
    gvisor_cell = Cell(
        "z", requires_substrate="gke-sandbox", pending_reason="requires-gvisor-runtime"
    )
    kata_cell = Cell(
        "k", requires_substrate="gke-kata", pending_reason="requires-kata-runtime"
    )
    # requires=None: runs everywhere (the kind perf-matrix path) — incl. gke-kata,
    # so the Kata EMIT measures the matrix rows for real rather than pending.
    for sub in ("kind", "gke", "gke-sandbox", "gke-kata"):
        _check(substrate_satisfies(any_cell, sub), f"None-cell must run on {sub}")
    # requires=gke: pends on kind, runs on every real GKE node (gke, gke-sandbox,
    # gke-kata).
    _check(not substrate_satisfies(gke_cell, "kind"), "gke-cell must pend on kind")
    _check(substrate_satisfies(gke_cell, "gke"), "gke-cell must run on gke")
    _check(
        substrate_satisfies(gke_cell, "gke-sandbox"),
        "gke-cell must run on gke-sandbox (superset)",
    )
    _check(
        substrate_satisfies(gke_cell, "gke-kata"),
        "gke-cell must run on gke-kata (real GKE node)",
    )
    # requires=gke-sandbox: only gke-sandbox satisfies (gke-kata has Kata, not gVisor).
    _check(
        not substrate_satisfies(gvisor_cell, "kind"), "gvisor-cell must pend on kind"
    )
    _check(
        not substrate_satisfies(gvisor_cell, "gke"),
        "gvisor-cell must pend on plain gke (no runsc)",
    )
    _check(
        substrate_satisfies(gvisor_cell, "gke-sandbox"),
        "gvisor-cell must run on gke-sandbox",
    )
    _check(
        not substrate_satisfies(gvisor_cell, "gke-kata"),
        "gvisor-cell must pend on gke-kata (Kata, not gVisor)",
    )
    # requires=gke-kata: only gke-kata satisfies (gke-sandbox has gVisor, not Kata).
    _check(not substrate_satisfies(kata_cell, "kind"), "kata-cell must pend on kind")
    _check(
        not substrate_satisfies(kata_cell, "gke"),
        "kata-cell must pend on plain gke (no Kata)",
    )
    _check(
        not substrate_satisfies(kata_cell, "gke-sandbox"),
        "kata-cell must pend on gke-sandbox (gVisor, not Kata)",
    )
    _check(
        substrate_satisfies(kata_cell, "gke-kata"),
        "kata-cell must run on gke-kata",
    )


# --- cross-contract: harness emit enums ⊆ render allow-lists ------------------


def test_harness_enums_subset_of_render_allowlists():
    """The harness EMITS these enum values; the render closed-schema allow-list
    DROPS anything not in its set (render.py maps an unknown pending_reason to
    None, and a substrate value absent from CLUSTER_SUBSTRATES is stripped). So a
    value the harness can emit but render does not accept silently loses its
    reason/substrate on the public page — breaking the exact-match invariant
    (results_schema.py). This is the offline guard that catches that drift at
    commit time (caught the requires-kata-runtime vs requires-kata-microvm drift
    in #3942). Direction is one-way by design: render MAY carry extra render-only
    seed tokens (e.g. requires-kata-microvm) the harness never emits."""
    from render import schema as render_schema

    missing_reasons = set(results_schema.PENDING_REASON_ENUM) - render_schema.PENDING_REASONS
    _check(
        not missing_reasons,
        f"harness PENDING_REASON_ENUM values not in render PENDING_REASONS "
        f"(would render no reason → break exact-match): {sorted(missing_reasons)}",
    )
    missing_subs = set(results_schema.CLUSTER_SUBSTRATE_ENUM) - render_schema.CLUSTER_SUBSTRATES
    _check(
        not missing_subs,
        f"harness CLUSTER_SUBSTRATE_ENUM values not in render CLUSTER_SUBSTRATES "
        f"(would be stripped at render): {sorted(missing_subs)}",
    )
    missing_products = set(results_schema.PRODUCT_ENUM) - render_schema.PRODUCTS
    _check(
        not missing_products,
        f"harness PRODUCT_ENUM values not in render PRODUCTS: {sorted(missing_products)}",
    )


def _all_tests():
    return [
        v
        for k, v in sorted(globals().items())
        if k.startswith("test_") and callable(v)
    ]


def main() -> int:
    failures = 0
    for t in _all_tests():
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {t.__name__}: {e}")
    print(f"\n{len(_all_tests()) - failures}/{len(_all_tests())} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
