"""Offline tests for badge_scope emit-path wiring (#3948) — no cluster, no I/O.

Run with bare python3 (no pytest, so the auto-refresh GH-runner needs nothing
extra):  python3 -m harness.test_emit_badge_scope
or directly:               python3 harness/test_emit_badge_scope.py

These close the loop the honest-bench#35 incident exposed: the harness emit path
never carried badge_scope, so every fresh fire silently dropped
"badge_scope": "control-plane" from the two NetworkPolicy isolation cells and the
public page rendered a bare PASS — an over-claim of data-plane enforcement
(#2082/#3907). The fix makes badge_scope a static Cell property the run loop injects
BY CONSTRUCTION; these assert (a) the two isolation cells declare it and perf cells
do not, and (b) _run_one injects it onto a freshly-run outcome (and never onto a
cell that does not declare one).
"""

from __future__ import annotations

# Make this file runnable BOTH as `python3 harness/test_x.py` and
# `python3 -m harness.test_x` by putting the repo root on sys.path before
# the absolute `from harness import ...` below (mirrors test_warm_vs_cold.py).
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from harness import run
from harness.scenario_map import SANDBOX_CELLS, Cell


def _check(cond, msg):
    if not cond:
        raise AssertionError(msg)


_ISOLATION = {"cross_tenant_network_isolation", "default_deny_egress"}


def test_isolation_cells_declare_control_plane():
    by_name = {c.module: c for c in SANDBOX_CELLS}
    for name in _ISOLATION:
        _check(name in by_name, f"{name} must be a registered sandbox cell")
        _check(by_name[name].badge_scope == "control-plane",
               f"{name} must declare badge_scope=control-plane, got "
               f"{by_name[name].badge_scope!r}")


def test_perf_cells_have_no_badge_scope():
    # A perf/throughput cell asserts nothing about isolation — it must stay clean so
    # the renderer never suffixes a scope onto a non-isolation PASS.
    by_name = {c.module: c for c in SANDBOX_CELLS}
    for name in ("burst_create", "warmpool_cold_start", "native_digest_cold",
                 "suspend_resume"):
        _check(by_name[name].badge_scope is None,
               f"{name} must NOT declare a badge_scope, got {by_name[name].badge_scope!r}")


class _FakeMod:
    """A scenario module stub whose run() returns a fixed (outcome, excerpt, sla)."""

    def __init__(self, outcome, sla=None):
        self.outcome = outcome
        self.sla = sla if sla is not None else {}

    def run(self, name):
        return self.outcome, "excerpt-dropped", dict(self.sla)


def _run_one_with_stub(cell, substrate, outcome, sla=None):
    """Drive _run_one with importlib stubbed so no scenario module / cluster is hit."""
    saved = run.importlib.import_module
    try:
        run.importlib.import_module = lambda _modpath: _FakeMod(outcome, sla)
        return run._run_one(cell, substrate)
    finally:
        run.importlib.import_module = saved


def test_run_one_injects_badge_scope_on_isolation_cell():
    cell = Cell("cross_tenant_network_isolation",
                requires_substrate="gke-sandbox",
                pending_reason="requires-gke",
                badge_scope="control-plane")
    raw = _run_one_with_stub(cell, "gke-sandbox", "PASS")
    _check(raw.get("badge_scope") == "control-plane",
           f"_run_one must inject the cell's badge_scope, got {raw!r}")
    _check(raw["outcome"] == "PASS", "outcome preserved")


def test_run_one_omits_badge_scope_when_cell_has_none():
    cell = Cell("burst_create")  # perf cell, no badge_scope
    raw = _run_one_with_stub(cell, "kind", "PASS")
    _check("badge_scope" not in raw,
           f"a cell with no badge_scope must not emit the key, got {raw!r}")


def test_pending_cell_does_not_carry_badge_scope():
    # On a substrate that cannot satisfy the cell, _run_one early-returns a pending
    # dict before running — render ignores badge_scope on pending, so the early
    # return stays minimal (no scope on a cell that asserts nothing yet).
    cell = Cell("cross_tenant_network_isolation",
                requires_substrate="gke-sandbox",
                pending_reason="requires-gke",
                badge_scope="control-plane")
    raw = run._run_one(cell, "kind")  # kind does not satisfy gke-sandbox
    _check(raw["outcome"] == "pending", "isolation cell pends on kind")
    _check("badge_scope" not in raw,
           f"pending early-return stays minimal (no badge_scope), got {raw!r}")


def test_run_one_lifts_runtime_enforced_badge_pair():
    # #4629: the data-plane probe (#3907) returns badge_scope='enforced' AND its
    # companion badge_construction='standard-np' as one atomic pair in the third
    # tuple element. _run_one must lift BOTH to the top-level raw dict — lifting
    # only badge_scope leaves a naked 'enforced' that the #4051 over-claim guard
    # rejects, crashing the whole run.
    cell = Cell("cross_tenant_network_isolation",
                requires_substrate="gke-sandbox",
                pending_reason="requires-gke",
                badge_scope="control-plane")
    raw = _run_one_with_stub(
        cell, "gke-sandbox", "PASS",
        sla={"badge_scope": "enforced", "badge_construction": "standard-np"},
    )
    _check(raw.get("badge_scope") == "enforced",
           f"runtime badge_scope upgrade must win over the static cell value, got {raw!r}")
    _check(raw.get("badge_construction") == "standard-np",
           f"_run_one must lift badge_construction alongside badge_scope, got {raw!r}")
    _check("badge_construction" not in raw.get("sla_metrics", {}),
           f"badge_construction must be popped out of sla_metrics, got {raw!r}")


def test_run_one_enforced_pair_survives_build_results():
    # End-to-end: the lifted enforced pair must round-trip through build_results
    # without tripping the #4051 over-claim guard (the exact crash #4629 hit).
    from harness import results_schema
    from harness.test_results_schema import _prov, GEN_AT
    cell = Cell("cross_tenant_network_isolation",
                requires_substrate="gke-sandbox",
                pending_reason="requires-gke",
                badge_scope="control-plane")
    raw = _run_one_with_stub(
        cell, "gke-sandbox", "PASS",
        sla={"badge_scope": "enforced", "badge_construction": "standard-np"},
    )
    results = results_schema.build_results([raw], _prov(), GEN_AT, product="sandbox")
    sc = results["scenarios"][0]
    _check(sc["badge_scope"] == "enforced", f"scope preserved through build, got {sc!r}")
    _check(sc["badge_construction"] == "standard-np",
           f"construction preserved through build, got {sc!r}")


def test_run_one_omits_badge_construction_without_runtime_upgrade():
    # No armed probe → no runtime badge dict → the control-plane cell emits its
    # static badge_scope and NO badge_construction (control-plane needs none).
    cell = Cell("cross_tenant_network_isolation",
                requires_substrate="gke-sandbox",
                pending_reason="requires-gke",
                badge_scope="control-plane")
    raw = _run_one_with_stub(cell, "gke-sandbox", "PASS")
    _check(raw.get("badge_scope") == "control-plane", f"static scope, got {raw!r}")
    _check("badge_construction" not in raw,
           f"no runtime upgrade must emit no badge_construction, got {raw!r}")


def _all_tests():
    return [v for k, v in sorted(globals().items())
            if k.startswith("test_") and callable(v)]


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
