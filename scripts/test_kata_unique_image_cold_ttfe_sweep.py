"""Offline unit tests for scripts/kata_unique_image_cold_ttfe_sweep.py's pure planning.

Fully offline: `_plan_rung_pool_indices` is a pure function over `RUNG_SIZES`-shaped
lists, no cluster/network access. This is a regression guard for a real live-fire
crash: an earlier version of `main()` passed the per-claim `claim_seq` counter (which is
NOT advanced during the provisioning loop) into `provision_one_claim_pool` as the
Template/WarmPool name suffix, so any rung with more than one pool issued two identical
names and the second `create_namespaced_custom_object` call 409-Conflicted. `RUNG_SIZES
== [1, 2]` hid the bug from every prior manual/CI check because rung 0 (n=1) can never
collide -- only rung 1 (n=2) can, and only against a LIVE cluster (no unit test existed
for this script at all before this file). `_plan_rung_pool_indices` is the extracted,
pure replacement for that inline counter logic: it hands `main()` a plan of
globally-unique flat indices up front, so the uniqueness property is checkable offline
instead of only observable as a live 409.

The module sets three os.environ knobs at import (runtime/substrate/namespace) via
setdefault, and its sibling test_kata_cold_ttfe_sweep.py sets the same
WARMPOOL_COLD_START_RUNTIME_CLASS var with a DIFFERENT default ("gvisor" vs "kata-clh")
via its own setdefault -- under a full-suite `pytest scripts/` run, whichever module
collects first wins the setdefault race. Force the value explicitly (not setdefault)
right before the import below so this module's constant is correct regardless of
collection order.
"""
import os as _os
import sys as _sys

_HB_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _HB_ROOT)
_sys.path.insert(0, _os.path.join(_HB_ROOT, "scripts"))

_os.environ["WARMPOOL_COLD_START_RUNTIME_CLASS"] = "kata-clh"
_os.environ["BENCH_CLUSTER_SUBSTRATE"] = "gke-kata"
import kata_unique_image_cold_ttfe_sweep as sweep  # noqa: E402


def _check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def test_plan_rung_pool_indices_matches_module_rung_shape():
    # The module's own RUNG_SIZES == [1, 2] is exactly the shape that hid the bug
    # (rung 0 can't collide with itself; only rung 1's n=2 exposes a repeated index).
    plan = sweep._plan_rung_pool_indices(sweep.RUNG_SIZES)
    _check(plan == [[0], [1, 2]],
           f"expected [[0], [1, 2]] for RUNG_SIZES={sweep.RUNG_SIZES}, got {plan}")


def test_plan_rung_pool_indices_all_unique_and_flat():
    for rung_sizes in ([1, 2], [1, 1, 1], [3], [2, 2, 2], []):
        plan = sweep._plan_rung_pool_indices(rung_sizes)
        _check(len(plan) == len(rung_sizes), "one index-list per rung")
        for rung, n in zip(plan, rung_sizes):
            _check(len(rung) == n, f"rung of size {n} gets exactly {n} indices, got {rung}")
        flat = [idx for rung in plan for idx in rung]
        _check(len(flat) == len(set(flat)),
               f"every index is globally unique across all rungs, got {flat}")
        _check(flat == sorted(flat), "indices are assigned in ascending, contiguous order")
        if flat:
            _check(flat == list(range(len(flat))),
                   f"indices are a dense 0..N-1 range with no gaps, got {flat}")


def test_plan_rung_pool_indices_covers_all_unique_image_tags():
    # Regression pin: the number of indices the plan hands out must never exceed the
    # number of UNIQUE_IMAGE_TAGS available, mirroring main()'s own preflight check.
    plan = sweep._plan_rung_pool_indices(sweep.RUNG_SIZES)
    total = sum(len(rung) for rung in plan)
    _check(total == sum(sweep.RUNG_SIZES), "plan issues exactly one index per claim")
    _check(total <= len(sweep.UNIQUE_IMAGE_TAGS),
           f"plan never asks for more indices ({total}) than available tags "
           f"({len(sweep.UNIQUE_IMAGE_TAGS)})")


if __name__ == "__main__":
    test_plan_rung_pool_indices_matches_module_rung_shape()
    test_plan_rung_pool_indices_all_unique_and_flat()
    test_plan_rung_pool_indices_covers_all_unique_image_tags()
    print("OK")
