#!/usr/bin/env python3
"""Explicit cloudbuild-*.yaml -> trigger(s) mapping for the auto-rebake CD step
(hb#8239: the last manual step left in the trigger-drift fix cycle).

Every trigger listed here is a MANUAL or auto-fire trigger that carries a
FROZEN INLINE `build` config (the trusted-ref boundary — see
scripts/rebake-manual-trigger.sh's header and the mapped repo's
`scripts/check-hb-trigger-drift.py` for the full rationale). A push to `main`
that changes one of these files updates the REPO file immediately but not the
LIVE trigger definition until something re-bakes it — this map is what lets
the CD step in cloudbuild-rebake-manual-triggers.yaml know which trigger(s)
to re-bake for a given changed file, without hardcoding the pairing inline in
that config. Adding a 3rd/4th manual or frozen-inline trigger later is a
one-line diff here.

DELIBERATELY EXCLUDED: cloudbuild-refresh-gke-sandbox.yaml / hb-refresh-gke-sandbox.
That trigger was permanently converted to `filename`-mode (repo file read at
fire time, no inline `build`) via a prior change (2026-08-14) — it can
never drift, so it has nothing to rebake. cloudbuild-render-autoheal.yaml is
also excluded: it isn't a manual/frozen-inline trigger at all (a plain
push-triggered inline-config trigger that already re-bakes itself via
`triggers update` on every `setup-cloud-build-triggers.sh` re-run, same as
the PR-gate triggers) and isn't tracked by the drift detector.

Kept in sync BY HAND with AlexBulankou/a's scripts/check-hb-trigger-drift.py
TRIGGERS dict (that script lives in a different repo and reads this repo's
files over the GitHub API, so the two can't literally share one source without
a larger cross-repo refactor -- out of scope for this map, tracked as a
follow-up idea, not a blocker).
"""
from __future__ import annotations

REBAKE_MAP: dict[str, list[str]] = {
    "cloudbuild-refresh-gke-kata.yaml": ["hb-refresh-gke-kata"],
    "cloudbuild-diagnostic-lineage-gate.yaml": ["hb-diagnostic-lineage-gate"],
    "cloudbuild-north-star-flip-gate.yaml": ["hb-north-star-flip-gate"],
    "cloudbuild-unit-tests.yaml": ["hb-unit-tests", "hb-unit-tests-main"],
}


def main() -> None:
    """Print "<file> <trigger>" pairs (one per line) for each changed file
    passed as argv that has a mapping entry. Unmapped files (e.g. a push that
    also touched cloudbuild-refresh-gke-sandbox.yaml or
    cloudbuild-render-autoheal.yaml) are silently skipped -- not every
    cloudbuild-*.yaml change needs a rebake, only the ones in REBAKE_MAP."""
    import sys

    for changed_file in sys.argv[1:]:
        for trigger in REBAKE_MAP.get(changed_file, []):
            print(f"{changed_file} {trigger}")


if __name__ == "__main__":
    main()
