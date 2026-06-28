# Honest benchmarks — GKE agent sandbox & substrate

These tables are **machine-rendered, never hand-entered**. A measured cell traces to a
schema-validated field of a real harness run; anything the schema does not declare is
dropped before it can reach this page. We publish only what we actually measured on the
substrate named in each build banner — a `kind` run is labelled `kind`, so a local number
is never presented as a production SLA.

A cell we have not yet measured renders `pending (<reason>)` — never a guess and never a
false PASS/FAIL. **This is the seed state: every cell currently reads `pending
(not-yet-measured)` (or a structural reason such as `requires-gvisor-runtime`), and no
build banner renders because nothing has been measured yet.** The auto-refresh Action
replaces each pending cell with a real measurement on its first run against a live
substrate; until then the page honestly shows that the suite has been wired but not run.

- **Measured (N)** is the value we observed, with the sample size.
- **Committed / Target / North-Star** render `(non-public)` here by construction — internal
  goal numbers never ship to this repo.

Reproduce any row yourself — the suite is honest by construction:

```
bash recipe/install-controller-from-main.sh   # OSS controller from upstream main
python3 -m sandbox.harness.run                # run the portable suite (substrate=kind)
python3 -m render.generate                    # regenerate these tables
bash scripts/check-public-safety.sh           # fail-closed public-safety scan
```

## sandbox

| Scenario | Measured (N) | Committed | Target | North-Star |
|---|---|---|---|---|
| Warm-pool activation (hit) | pending (not-yet-measured) (n=0) | (non-public) | (non-public) | (non-public) |
| Unique-image cold start | pending (not-yet-measured) (n=0) | (non-public) | (non-public) | (non-public) |
| Resume from suspend | pending (not-yet-measured) (n=0) | (non-public) | (non-public) | (non-public) |
| Cross-tenant network isolation | pending (requires-gke) (n=0) | (non-public) | (non-public) | (non-public) |
| Default-deny egress | pending (requires-gke) (n=0) | (non-public) | (non-public) | (non-public) |
| gVisor isolation canary | pending (requires-gvisor-runtime) (n=0) | (non-public) | (non-public) | (non-public) |

_generated-at: 2026-06-28T03:00:00Z_

## substrate

| Scenario | Measured (N) | Committed | Target | North-Star |
|---|---|---|---|---|
| Cold reconcile | pending (not-yet-measured) (n=0) | (non-public) | (non-public) | (non-public) |
| Suspend/resume carry-over | pending (not-yet-measured) (n=0) | (non-public) | (non-public) | (non-public) |
| Agent-identity (Pod-Certificate) | pending (not-yet-measured) (n=0) | (non-public) | (non-public) | (non-public) |

_generated-at: 2026-06-28T03:00:00Z_
