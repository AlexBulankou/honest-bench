"""Offline unit tests for harness/concurrent_load.py (hb#880).

Fully offline: fakes the BatchV1Api ``list_namespaced_job`` return with plain
attribute-bag objects, never a live cluster. Pins the honesty spine that mirrors
``_sample_node_count`` / ``ttfe_stamp``:
  - unconfigured (no namespace) => None (feature off, not a fake 0);
  - list-call exception => None (best-effort, never fail the fire);
  - a genuine empty scan (namespace present, zero active jobs) => a real
    zero-count snapshot, NOT None (measured-zero != not-measured);
and the peak (not mean) fold + slug union in aggregate.
"""
import os as _os
import sys as _sys

_HB_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _HB_ROOT)

from harness.concurrent_load import (  # noqa: E402
    aggregate_concurrent_load,
    config_from_env,
    sample_concurrent_load,
)


class _Meta:
    def __init__(self, labels):
        self.labels = labels


class _Status:
    def __init__(self, active):
        self.active = active


class _Job:
    def __init__(self, active, labels=None):
        self.status = _Status(active)
        self.metadata = _Meta(labels or {})


class _JobList:
    def __init__(self, items):
        self.items = items


class _FakeBatch:
    """Fake BatchV1Api: returns a fixed job list, or raises if ``boom`` set."""

    def __init__(self, jobs=None, boom=False):
        self._jobs = jobs or []
        self._boom = boom

    def list_namespaced_job(self, namespace=None):
        if self._boom:
            raise RuntimeError("simulated list failure")
        return _JobList(self._jobs)


# ---------------------------------------------------------------- config_from_env


def test_config_from_env_unset_is_none(monkeypatch):
    monkeypatch.delenv("CONCURRENT_LOAD_NAMESPACE", raising=False)
    monkeypatch.delenv("CONCURRENT_LOAD_SCENARIO_LABEL", raising=False)
    assert config_from_env() == (None, None)


def test_config_from_env_blank_is_none(monkeypatch):
    monkeypatch.setenv("CONCURRENT_LOAD_NAMESPACE", "   ")
    monkeypatch.setenv("CONCURRENT_LOAD_SCENARIO_LABEL", "")
    assert config_from_env() == (None, None)


def test_config_from_env_reads_values(monkeypatch):
    monkeypatch.setenv("CONCURRENT_LOAD_NAMESPACE", " some-ns ")
    monkeypatch.setenv("CONCURRENT_LOAD_SCENARIO_LABEL", " some/label ")
    assert config_from_env() == ("some-ns", "some/label")


# ---------------------------------------------------------- sample_concurrent_load


def test_sample_unconfigured_namespace_is_none():
    # None namespace => feature inert => None (not a fake 0).
    assert sample_concurrent_load(_FakeBatch(jobs=[_Job(1)]), None) is None


def test_sample_none_client_is_none():
    assert sample_concurrent_load(None, "some-ns") is None


def test_sample_list_exception_is_none():
    # Best-effort: a client/list hiccup must never fail the fire.
    assert sample_concurrent_load(_FakeBatch(boom=True), "some-ns") is None


def test_sample_genuine_empty_is_real_zero():
    # Namespace scanned, zero active jobs => measured-zero snapshot, NOT None.
    out = sample_concurrent_load(_FakeBatch(jobs=[]), "some-ns")
    assert out == {"active_count": 0, "scenario_slugs": []}


def test_sample_counts_only_active_jobs():
    jobs = [_Job(1), _Job(None), _Job(0), _Job(2)]
    out = sample_concurrent_load(_FakeBatch(jobs=jobs), "some-ns")
    assert out == {"active_count": 2, "scenario_slugs": []}


def test_sample_slugs_deduped_sorted_from_label():
    jobs = [
        _Job(1, {"scn": "bravo"}),
        _Job(1, {"scn": "alpha"}),
        _Job(1, {"scn": "bravo"}),  # dup
        _Job(1, {}),                 # active but no label -> counted, no slug
        _Job(0, {"scn": "charlie"}),  # inactive -> excluded entirely
    ]
    out = sample_concurrent_load(_FakeBatch(jobs=jobs), "some-ns", scenario_label="scn")
    assert out == {"active_count": 4, "scenario_slugs": ["alpha", "bravo"]}


def test_sample_no_label_arg_yields_no_slugs():
    jobs = [_Job(1, {"scn": "alpha"})]
    out = sample_concurrent_load(_FakeBatch(jobs=jobs), "some-ns")
    assert out == {"active_count": 1, "scenario_slugs": []}


# ------------------------------------------------------- aggregate_concurrent_load


def test_aggregate_all_none_is_none():
    assert aggregate_concurrent_load([None, None]) is None
    assert aggregate_concurrent_load([]) is None
    assert aggregate_concurrent_load(None) is None


def test_aggregate_peak_and_slug_union():
    samples = [
        {"active_count": 2, "scenario_slugs": ["alpha"]},
        None,  # a failed boundary sample is ignored, not fatal
        {"active_count": 5, "scenario_slugs": ["bravo", "alpha"]},
        {"active_count": 1, "scenario_slugs": ["charlie"]},
    ]
    out = aggregate_concurrent_load(samples)
    assert out == {
        "peak_active_scenario_jobs": 5,  # peak, not mean
        "scenario_slugs": ["alpha", "bravo", "charlie"],
        "n_samples": 3,  # only the 3 real (non-None) samples
    }


def test_aggregate_measured_zero_is_real():
    # A run whose every boundary measured zero folds to a real zero-peak
    # snapshot (n_samples>0), distinct from the all-None "never measured".
    out = aggregate_concurrent_load([
        {"active_count": 0, "scenario_slugs": []},
        {"active_count": 0, "scenario_slugs": []},
    ])
    assert out == {
        "peak_active_scenario_jobs": 0,
        "scenario_slugs": [],
        "n_samples": 2,
    }
