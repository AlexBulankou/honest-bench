"""Scenario map — public benchmark cell -> portable scenario module, per product.

The harness is subtraction, not rewrite: it reuses the existing scenario bodies
(stripped of their four internal bindings — see harness README) and drives them
through one in-process loop. This map is the single source of which cells each
product's matrix renders and which module produces each.

Per-product: `CELLS_BY_PRODUCT` keys a closed cell-suite per product, and
`cells_for_product()` is the only accessor. A product with no registered suite is
NOT runnable — the accessor raises rather than returning an empty tuple — so
`run --product X` can never overwrite a hand-seeded `X/results/latest.json` with an
empty scenarios list. The substrate axis (agent_identity_podcert, ...) registers
its suite here when its modules land (#3868).

`requires_substrate` encodes the kind-vs-GKE portability fact: these isolation
cells need a `gke-sandbox` node, which vanilla kind lacks, so on a `kind` run they
render `pending` (with a per-cell `pending_reason`) instead of a false FAIL; full
isolation badges go live on the `gke-sandbox` substrate (Phase 2). The reason is
cell-specific: the gVisor canary reports `requires-gvisor-runtime` (it needs
`runsc` on the node), while the NetworkPolicy cells report `requires-gke` (they
need a policy-enforcing CNI — kindnet does not enforce NetworkPolicy). A cell with
`requires_substrate=None` runs on every substrate.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Cell:
    module: str         # scenario module basename under scenarios/; ALSO the canonical
                        # scenario name emitted to results.json. render/schema.py keys
                        # SCENARIO_LABELS on this exact basename, so emitter and renderer
                        # share one vocabulary (no public-vs-internal name divergence).
    requires_substrate: str | None = None  # None = any; else min substrate needed
    pending_reason: str | None = None      # rendered when substrate unmet


# Sandbox Phase-1 MVP: the perf matrix + the isolation badges. The module basenames
# are exactly render/schema.py's sandbox SCENARIO_LABELS keys — converging the emitter
# and renderer on one vocabulary so every measured row renders (no closed-schema drop).
SANDBOX_CELLS = (
    # --- perf matrix (substrate-agnostic: run on kind, gke, or gke-sandbox) ---
    Cell("warmpool_cold_start"),
    Cell("native_digest_cold"),
    Cell("suspend_resume"),
    # --- isolation badges (need gVisor; pending on vanilla kind) ---
    Cell(
        "gvisor_canary",
        requires_substrate="gke-sandbox",
        pending_reason="requires-gvisor-runtime",
    ),
    Cell(
        "cross_tenant_network_isolation",
        requires_substrate="gke-sandbox",
        pending_reason="requires-gke",
    ),
    Cell(
        "default_deny_egress",
        requires_substrate="gke-sandbox",
        pending_reason="requires-gke",
    ),
)

# Per-product cell suites. Only a registered product is runnable; the substrate
# axis registers its suite (agent_identity_podcert, ...) here when its modules
# land at harness/scenarios/ (#3868). Registering an empty/absent product is
# deliberately NOT done — see cells_for_product.
CELLS_BY_PRODUCT = {
    "sandbox": SANDBOX_CELLS,
}


def cells_for_product(product: str) -> tuple[Cell, ...]:
    """Return the cell suite for `product`; raise if none is registered.

    Raising (rather than returning ()) is load-bearing: an empty suite would make
    the runner emit a zero-scenario results.json and OVERWRITE a hand-seeded
    `<product>/results/latest.json`. Failing closed protects that seed until a real
    suite is registered above.
    """
    try:
        return CELLS_BY_PRODUCT[product]
    except KeyError:
        raise SystemExit(
            f"no harness cells registered for product {product!r}; "
            f"registered products: {sorted(CELLS_BY_PRODUCT)}"
        )


# Substrates that satisfy a gVisor-isolation requirement. kind does NOT — its
# nodes have no runsc — so an isolation cell on kind renders pending.
_SUBSTRATE_SATISFIES = {
    None: ("kind", "gke", "gke-sandbox"),
    "gke-sandbox": ("gke-sandbox",),
}


def substrate_satisfies(cell: Cell, substrate: str) -> bool:
    return substrate in _SUBSTRATE_SATISFIES[cell.requires_substrate]
