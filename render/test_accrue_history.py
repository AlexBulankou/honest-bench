"""Accrual tests for the build-over-build throughput history (#3918). Dependency-free:
`python3 test_accrue_history.py` (exit 0 = pass).

These assert the sole-writer contract: upsert-by-digest (latest measurement of a build wins,
no duplicate rows), honest-skip when the burst_create COUNT was not measured, closed-schema
on the way in (only HISTORY_FIELDS reach the file), and ordering by generated_at so the file
reads as a build-over-build timeline.
"""

import json
import os
import tempfile

import accrue_history


def _latest(count=9, density=0.45, n=10, digest="sha256:" + "a" * 64,
            generated_at="2026-06-28T14:42:40Z", outcome="PASS"):
    # count=None mirrors a genuine all-cold burst: the scenario itself emits an EMPTY
    # sla_metrics (no fabricated number), independent of outcome — the true CASE-1 honest-skip.
    metrics = {} if count is None else {"sandboxes_ready_under_1s": count, "density_per_vcpu": density}
    return {
        "product": "sandbox",
        "generated_at": generated_at,
        "provenance": {
            "cluster_substrate": "gke-sandbox",
            "controller_digest": digest,
            "suite_git_sha": "c88d857",
            "run_id": "a0e4f0ffae12440a826ac40a277f21f3",
        },
        "scenarios": [
            {
                "name": "burst_create",
                "outcome": outcome,
                "n": n,
                "sla_metrics": metrics,
            }
        ],
    }


def test_extract_row_happy_path():
    row = accrue_history.extract_row(_latest())
    assert row is not None
    assert set(row) == set(accrue_history.HISTORY_FIELDS)
    assert row["sandboxes_ready_under_1s"] == 9
    assert row["controller_digest"] == "sha256:" + "a" * 64


def test_extract_row_honest_skip_when_all_cold():
    # A genuine all-cold burst (count==0, sla_metrics=={}) carries no measurable COUNT — no
    # row (you cannot chart a COUNT that was not measured). This is NOT an outcome check.
    assert accrue_history.extract_row(_latest(count=None)) is None


def test_extract_row_charts_fail_outcome_with_measured_count():
    # #546: a FAIL burst_create that DID measure a count (some-but-not-enough delivery) is
    # measurable and MUST chart — outcome alone is not a skip condition.
    row = accrue_history.extract_row(_latest(outcome="FAIL", count=2))
    assert row is not None
    assert row["sandboxes_ready_under_1s"] == 2
    assert row["outcome"] == "FAIL"


def test_extract_row_skip_when_no_burst_create():
    res = {"product": "sandbox", "generated_at": "2026-06-28T14:42:40Z",
           "provenance": {}, "scenarios": [{"name": "warmpool_cold_start", "outcome": "PASS"}]}
    assert accrue_history.extract_row(res) is None


def test_extract_row_skip_on_bad_required_field():
    # A digest that fails the predicate cannot anchor the row to a build ⇒ skip.
    assert accrue_history.extract_row(_latest(digest="sha256:NOT-HEX")) is None


def test_candidate_row_none_when_no_count():
    # CASE 1: an all-cold burst_create carries no measurable COUNT ⇒ no candidate at all.
    assert accrue_history._candidate_row(_latest(count=None)) is None


def test_candidate_row_present_even_when_provenance_bad():
    # CASE 2 is distinguishable from CASE 1: a measured COUNT with unanchorable provenance still
    # produces a CANDIDATE (the COUNT exists) — only validation, not measurement, fails.
    cand = accrue_history._candidate_row(_latest(digest="sha256:NOT-HEX"))
    assert cand is not None
    assert cand["sandboxes_ready_under_1s"] == 9


def test_validate_row_names_failing_provenance_field():
    cand = accrue_history._candidate_row(_latest(digest="sha256:NOT-HEX"))
    row, bad_key = accrue_history._validate_row(cand)
    assert row is None
    assert bad_key == "controller_digest"


def test_validate_row_clean_returns_row_and_no_bad_key():
    row, bad_key = accrue_history._validate_row(accrue_history._candidate_row(_latest()))
    assert bad_key is None
    assert set(row) == set(accrue_history.HISTORY_FIELDS)


def _read(path):
    with open(path) as fh:
        return [json.loads(line) for line in fh if line.strip()]


def test_upsert_new_digest_appends():
    with tempfile.TemporaryDirectory() as d:
        h = os.path.join(d, "history.jsonl")
        accrue_history.upsert(accrue_history.extract_row(
            _latest(digest="sha256:" + "a" * 64, generated_at="2026-06-27T10:00:00Z")), h)
        accrue_history.upsert(accrue_history.extract_row(
            _latest(digest="sha256:" + "b" * 64, generated_at="2026-06-28T10:00:00Z")), h)
        rows = _read(h)
        assert len(rows) == 2
        # ordered by generated_at (oldest first)
        assert rows[0]["controller_digest"] == "sha256:" + "a" * 64
        assert rows[1]["controller_digest"] == "sha256:" + "b" * 64


def test_upsert_same_digest_refreshes_in_place():
    with tempfile.TemporaryDirectory() as d:
        h = os.path.join(d, "history.jsonl")
        dig = "sha256:" + "a" * 64
        accrue_history.upsert(accrue_history.extract_row(_latest(digest=dig, count=9)), h)
        # re-run on the SAME build with a newer measurement — refreshes, does NOT duplicate
        accrue_history.upsert(accrue_history.extract_row(_latest(digest=dig, count=14)), h)
        rows = _read(h)
        assert len(rows) == 1
        assert rows[0]["sandboxes_ready_under_1s"] == 14


def test_upsert_only_schema_fields_written():
    with tempfile.TemporaryDirectory() as d:
        h = os.path.join(d, "history.jsonl")
        accrue_history.upsert(accrue_history.extract_row(_latest()), h)
        rows = _read(h)
        assert set(rows[0]) == set(accrue_history.HISTORY_FIELDS)


def test_load_history_drops_malformed_lines():
    with tempfile.TemporaryDirectory() as d:
        h = os.path.join(d, "history.jsonl")
        good = accrue_history.extract_row(_latest())
        with open(h, "w") as fh:
            fh.write(json.dumps(good) + "\n")
            fh.write("{not valid json\n")
            fh.write(json.dumps({"controller_digest": "sha256:" + "a" * 64}) + "\n")  # missing fields
        rows = accrue_history.load_history(h)
        assert len(rows) == 1
        assert rows[0]["controller_digest"] == good["controller_digest"]


def test_main_honest_skip_exit_zero_no_write(capsys=None):
    with tempfile.TemporaryDirectory() as d:
        latest = os.path.join(d, "latest.json")
        history = os.path.join(d, "history.jsonl")
        with open(latest, "w") as fh:
            json.dump(_latest(count=None), fh)
        rc = accrue_history.main(["sandbox", "--latest", latest, "--history", history])
        assert rc == 0
        assert not os.path.exists(history)  # honest-skip: no file written


def test_main_loud_fail_on_empty_digest_the_real_world_flake():
    # The production failure mode: BENCH_CONTROLLER_DIGEST capture flaked, so provenance carries
    # controller_digest="" while the burst COUNT WAS measured. Must fail LOUD + closed (rc=3),
    # NOT emit the false "no measurable COUNT" honest-skip that froze the trend since 2026-06-28.
    with tempfile.TemporaryDirectory() as d:
        latest = os.path.join(d, "latest.json")
        history = os.path.join(d, "history.jsonl")
        with open(latest, "w") as fh:
            json.dump(_latest(digest=""), fh)
        rc = accrue_history.main(["sandbox", "--latest", latest, "--history", history])
        assert rc == 3
        assert not os.path.exists(history)  # fail-closed: no write


def test_main_loud_fail_on_bad_digest():
    with tempfile.TemporaryDirectory() as d:
        latest = os.path.join(d, "latest.json")
        history = os.path.join(d, "history.jsonl")
        with open(latest, "w") as fh:
            json.dump(_latest(digest="sha256:NOT-HEX"), fh)
        rc = accrue_history.main(["sandbox", "--latest", latest, "--history", history])
        assert rc == 3
        assert not os.path.exists(history)


def test_main_loud_fail_on_missing_provenance_key():
    # controller_digest ABSENT from provenance entirely (vs empty-string) — still a measured
    # COUNT that cannot anchor ⇒ loud fail, not a false no-COUNT skip.
    res = _latest()
    del res["provenance"]["controller_digest"]
    with tempfile.TemporaryDirectory() as d:
        latest = os.path.join(d, "latest.json")
        history = os.path.join(d, "history.jsonl")
        with open(latest, "w") as fh:
            json.dump(res, fh)
        rc = accrue_history.main(["sandbox", "--latest", latest, "--history", history])
        assert rc == 3
        assert not os.path.exists(history)


def test_main_writes_and_is_idempotent():
    with tempfile.TemporaryDirectory() as d:
        latest = os.path.join(d, "latest.json")
        history = os.path.join(d, "history.jsonl")
        with open(latest, "w") as fh:
            json.dump(_latest(), fh)
        accrue_history.main(["sandbox", "--latest", latest, "--history", history])
        accrue_history.main(["sandbox", "--latest", latest, "--history", history])
        rows = _read(history)
        assert len(rows) == 1  # same build re-run ⇒ still one row


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok: {fn.__name__}")
    print(f"test_accrue_history: all {len(fns)} assertions passed")


if __name__ == "__main__":
    _run_all()
