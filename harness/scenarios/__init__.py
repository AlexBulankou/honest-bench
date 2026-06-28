"""Portable benchmark scenario modules.

Each module exposes `run(scenario_name) -> (outcome, excerpt, sla_metrics)`,
driven by the harness loop. `excerpt` is classification-only and is never written
to results.json.
"""
