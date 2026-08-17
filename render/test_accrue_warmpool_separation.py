"""Accrual tests for the warmpool-separation-ratio measurement history (#6890 item 3).
Dependency-free: `python3 test_accrue_warmpool_separation.py` (exit 0 = pass).

These assert the sole-writer contract: append-only keyed by run_id (never upserted by digest —
a second measurement of the same build must NOT overwrite the first, since same-build variance
is exactly the signal this store exists to disclose), honest-skip when the gate metric was never
computed, closed-schema on the way in (only WARMPOOL_SEPARATION_HISTORY_FIELDS reach the file),
and ordering by generated_at so the file reads as a timeline.
"""

import json
import os
import tempfile

import accrue_warmpool_separation


def _latest(ratio=1.06, n=30, digest="sha256:" + "a" * 64,
            generated_at="2026-08-17T14:42:40Z", outcome="PASS", run_id="run-a"):
    # ratio=None mirrors a warmpool_cold_start cell that never computed the gate metric at all
    # (sla_metrics=={}) — the true CASE-1 honest-skip. This is NOT an outcome check: the scenario
    # surfaces the ratio on BOTH PASS and FAIL, so a measured ratio on either outcome DOES chart.
    metrics = {} if ratio is None else {"warmpool_gate_separation_ratio": ratio}
    return {
        "product": "sandbox",
        "generated_at": generated_at,
        "provenance": {
            "cluster_substrate": "gke-sandbox",
            "controller_digest": digest,
            "suite_git_sha": "c88d857",
            "run_id": run_id,
        },
        "scenarios": [
            {
                "name": "warmpool_cold_start",
                "outcome": outcome,
                "n": n,
                "sla_metrics": metrics,
            }
        ],
    }


def test_extract_row_happy_path():
    row = accrue_warmpool_separation.extract_row(_latest())
    assert row is not None
    assert set(row) == set(accrue_warmpool_separation.WARMPOOL_SEPARATION_HISTORY_FIELDS)
    assert row["separation_ratio"] == 1.06
    assert row["controller_digest"] == "sha256:" + "a" * 64
    assert row["run_id"] == "run-a"


def test_extract_row_honest_skip_when_ratio_never_measured():
    # CASE 1: sla_metrics=={} — the gate metric was never computed, so nothing to chart.
    assert accrue_warmpool_separation.extract_row(_latest(ratio=None)) is None


def test_extract_row_charts_fail_outcome_with_measured_ratio():
    # A below-gate ratio is exactly what FAILs the scenario and exactly the signal this store
    # exists to chart — outcome alone is not a skip condition.
    row = accrue_warmpool_separation.extract_row(_latest(outcome="FAIL", ratio=0.27))
    assert row is not None
    assert row["separation_ratio"] == 0.27
    assert row["outcome"] == "FAIL"


def test_extract_row_skip_when_no_warmpool_cold_start():
    res = {"product": "sandbox", "generated_at": "2026-08-17T14:42:40Z",
           "provenance": {}, "scenarios": [{"name": "burst_create", "outcome": "PASS"}]}
    assert accrue_warmpool_separation.extract_row(res) is None


def test_extract_row_skip_on_bad_required_field():
    # A digest that fails the predicate cannot anchor the row to a build ⇒ skip.
    assert accrue_warmpool_separation.extract_row(_latest(digest="sha256:NOT-HEX")) is None


def test_candidate_row_none_when_ratio_never_measured():
    # CASE 1: no measurable ratio at all ⇒ no candidate.
    assert accrue_warmpool_separation._candidate_row(_latest(ratio=None)) is None


def test_candidate_row_present_even_when_provenance_bad():
    # CASE 2 is distinguishable from CASE 1: a measured ratio with unanchorable provenance still
    # produces a CANDIDATE (the ratio exists) — only validation, not measurement, fails.
    cand = accrue_warmpool_separation._candidate_row(_latest(digest="sha256:NOT-HEX"))
    assert cand is not None
    assert cand["separation_ratio"] == 1.06


def test_validate_row_names_failing_provenance_field():
    cand = accrue_warmpool_separation._candidate_row(_latest(digest="sha256:NOT-HEX"))
    row, bad_key = accrue_warmpool_separation._validate_row(cand)
    assert row is None
    assert bad_key == "controller_digest"


def test_validate_row_clean_returns_row_and_no_bad_key():
    row, bad_key = accrue_warmpool_separation._validate_row(
        accrue_warmpool_separation._candidate_row(_latest()))
    assert bad_key is None
    assert set(row) == set(accrue_warmpool_separation.WARMPOOL_SEPARATION_HISTORY_FIELDS)


def _read(path):
    with open(path) as fh:
        return [json.loads(line) for line in fh if line.strip()]


def test_append_new_run_id_appends():
    with tempfile.TemporaryDirectory() as d:
        h = os.path.join(d, "history.jsonl")
        accrue_warmpool_separation.append(accrue_warmpool_separation.extract_row(
            _latest(run_id="run-a", generated_at="2026-08-17T10:00:00Z")), h)
        accrue_warmpool_separation.append(accrue_warmpool_separation.extract_row(
            _latest(run_id="run-b", generated_at="2026-08-17T18:00:00Z")), h)
        rows = _read(h)
        assert len(rows) == 2
        # ordered by generated_at (oldest first)
        assert rows[0]["run_id"] == "run-a"
        assert rows[1]["run_id"] == "run-b"


def test_append_same_digest_different_run_id_appends_both():
    # #6890's own headline finding: the SAME controller_digest fired twice must produce TWO
    # rows (0.27x then 1.06x), not an upsert-by-digest overwrite that erases the variance.
    with tempfile.TemporaryDirectory() as d:
        h = os.path.join(d, "history.jsonl")
        dig = "sha256:" + "a" * 64
        accrue_warmpool_separation.append(accrue_warmpool_separation.extract_row(
            _latest(digest=dig, run_id="fire-1", ratio=0.27,
                    generated_at="2026-08-17T15:23:36Z")), h)
        accrue_warmpool_separation.append(accrue_warmpool_separation.extract_row(
            _latest(digest=dig, run_id="fire-2", ratio=1.06,
                    generated_at="2026-08-17T18:20:45Z")), h)
        rows = _read(h)
        assert len(rows) == 2
        ratios = {r["separation_ratio"] for r in rows}
        assert ratios == {0.27, 1.06}


def test_append_same_run_id_is_idempotent_noop():
    # Re-running accrual against the same fire's latest.json must not duplicate the row.
    with tempfile.TemporaryDirectory() as d:
        h = os.path.join(d, "history.jsonl")
        row = accrue_warmpool_separation.extract_row(_latest(run_id="run-a"))
        accrue_warmpool_separation.append(row, h)
        accrue_warmpool_separation.append(row, h)
        rows = _read(h)
        assert len(rows) == 1


def test_append_only_schema_fields_written():
    with tempfile.TemporaryDirectory() as d:
        h = os.path.join(d, "history.jsonl")
        accrue_warmpool_separation.append(
            accrue_warmpool_separation.extract_row(_latest()), h)
        rows = _read(h)
        assert set(rows[0]) == set(accrue_warmpool_separation.WARMPOOL_SEPARATION_HISTORY_FIELDS)


def test_load_history_drops_malformed_lines():
    with tempfile.TemporaryDirectory() as d:
        h = os.path.join(d, "history.jsonl")
        good = accrue_warmpool_separation.extract_row(_latest())
        with open(h, "w") as fh:
            fh.write(json.dumps(good) + "\n")
            fh.write("{not valid json\n")
            fh.write(json.dumps({"controller_digest": "sha256:" + "a" * 64}) + "\n")  # missing fields
        rows = accrue_warmpool_separation.load_history(h)
        assert len(rows) == 1
        assert rows[0]["run_id"] == good["run_id"]


def test_main_honest_skip_exit_zero_no_write():
    with tempfile.TemporaryDirectory() as d:
        latest = os.path.join(d, "latest.json")
        history = os.path.join(d, "warmpool-separation-history.jsonl")
        with open(latest, "w") as fh:
            json.dump(_latest(ratio=None), fh)
        rc = accrue_warmpool_separation.main(["sandbox", "--latest", latest, "--history", history])
        assert rc == 0
        assert not os.path.exists(history)  # honest-skip: no file written


def test_main_loud_fail_on_empty_digest():
    # A measured ratio with controller_digest="" cannot anchor to a build ⇒ fail LOUD + closed
    # (rc=3), not the false "no measurable ratio" honest-skip.
    with tempfile.TemporaryDirectory() as d:
        latest = os.path.join(d, "latest.json")
        history = os.path.join(d, "warmpool-separation-history.jsonl")
        with open(latest, "w") as fh:
            json.dump(_latest(digest=""), fh)
        rc = accrue_warmpool_separation.main(["sandbox", "--latest", latest, "--history", history])
        assert rc == 3
        assert not os.path.exists(history)  # fail-closed: no write


def test_main_loud_fail_on_bad_digest():
    with tempfile.TemporaryDirectory() as d:
        latest = os.path.join(d, "latest.json")
        history = os.path.join(d, "warmpool-separation-history.jsonl")
        with open(latest, "w") as fh:
            json.dump(_latest(digest="sha256:NOT-HEX"), fh)
        rc = accrue_warmpool_separation.main(["sandbox", "--latest", latest, "--history", history])
        assert rc == 3
        assert not os.path.exists(history)


def test_main_loud_fail_on_missing_run_id():
    res = _latest()
    del res["provenance"]["run_id"]
    with tempfile.TemporaryDirectory() as d:
        latest = os.path.join(d, "latest.json")
        history = os.path.join(d, "warmpool-separation-history.jsonl")
        with open(latest, "w") as fh:
            json.dump(res, fh)
        rc = accrue_warmpool_separation.main(["sandbox", "--latest", latest, "--history", history])
        assert rc == 3
        assert not os.path.exists(history)


def test_main_writes_and_is_idempotent():
    with tempfile.TemporaryDirectory() as d:
        latest = os.path.join(d, "latest.json")
        history = os.path.join(d, "warmpool-separation-history.jsonl")
        with open(latest, "w") as fh:
            json.dump(_latest(), fh)
        accrue_warmpool_separation.main(["sandbox", "--latest", latest, "--history", history])
        accrue_warmpool_separation.main(["sandbox", "--latest", latest, "--history", history])
        rows = _read(history)
        assert len(rows) == 1  # same run_id re-run ⇒ still one row


def test_main_no_latest_json_returns_zero():
    with tempfile.TemporaryDirectory() as d:
        latest = os.path.join(d, "latest.json")  # never created
        history = os.path.join(d, "warmpool-separation-history.jsonl")
        rc = accrue_warmpool_separation.main(["sandbox", "--latest", latest, "--history", history])
        assert rc == 0
        assert not os.path.exists(history)


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok: {fn.__name__}")
    print(f"test_accrue_warmpool_separation: all {len(fns)} assertions passed")


if __name__ == "__main__":
    _run_all()
