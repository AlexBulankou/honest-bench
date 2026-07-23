#!/usr/bin/env python3
"""Offline unit tests for gate_d_corpus_baseline.py.

Tests the join / classify / stat LOGIC on synthetic fixtures — deliberately NOT
against the live sandbox/records corpus, whose numbers change on every re-record
(and would otherwise red this merge gate for unrelated PRs). The live-corpus
numbers are the *finding*; this test pins the *computation*.
"""
import importlib.util
import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "gate_d_corpus_baseline", os.path.join(_HERE, "gate_d_corpus_baseline.py"))
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


class TestRelDiff(unittest.TestCase):
    def test_agreeing_legs_zero(self):
        self.assertEqual(mod.rel_diff(1.0, 1.0), 0.0)

    def test_double_count_half(self):
        # ctrl double-counts: 2.0 vs 1.0 -> |1-2|/2 = 0.5
        self.assertAlmostEqual(mod.rel_diff(1.0, 2.0), 0.5)

    def test_missing_leg_none(self):
        self.assertIsNone(mod.rel_diff(None, 1.0))
        self.assertIsNone(mod.rel_diff(1.0, 0))

    def test_nonpositive_none(self):
        self.assertIsNone(mod.rel_diff(0, 0))


class TestClassifyRegime(unittest.TestCase):
    def test_cold_from_filename(self):
        self.assertEqual(mod.classify_regime("permode-legD-kata-cold-x.json", 0.05), "cold")

    def test_warm_high_at_threshold(self):
        self.assertEqual(mod.classify_regime("permode-goal-warm.json", 10.0), "warm_high")
        self.assertEqual(mod.classify_regime("permode-goal-warm.json", 30.0), "warm_high")

    def test_warm_sub_below_threshold(self):
        self.assertEqual(mod.classify_regime("permode-legA-gvisor-warm.json", 2.0), "warm_sub")

    def test_cold_wins_over_high_rate(self):
        # a cold record is cold even at a high offered rate
        self.assertEqual(mod.classify_regime("x-cold.json", 30.0), "cold")


class TestExtractRungs(unittest.TestCase):
    def _record(self):
        # one warm rung with the classic 2x double-count: acq_n=36, ctrl_event_count=72
        return {
            "steps": [
                {"controller_completed_per_s": 0.24663799, "ctrl_event_count": 72, "claims_created": 36},
            ],
            "literal_ttfe": {
                "pareto": [
                    {"offered_rate_per_s": 0.12, "acq_fulfilled_per_s": 0.1233,
                     "controller_completed_per_s": 0.24663799, "acq_n": 36},
                ]
            },
        }

    def test_join_and_ratio(self):
        rungs = mod.extract_rungs("permode-ladder-kata-warm.json", self._record())
        self.assertEqual(len(rungs), 1)
        r = rungs[0]
        self.assertEqual(r["ctrl_event_count"], 72)
        self.assertEqual(r["acq_n"], 36)
        self.assertAlmostEqual(r["ratio"], 2.0)          # 72 / 36
        self.assertEqual(r["regime"], "warm_sub")
        self.assertGreater(r["rel_diff"], 0.10)          # fails the gate, as expected

    def test_unjoinable_ctrl_gives_none_ratio(self):
        rec = self._record()
        rec["steps"] = []  # no ctrl counts to join
        r = mod.extract_rungs("x-warm.json", rec)[0]
        self.assertIsNone(r["ratio"])
        self.assertEqual(r["ctrl_event_count"], None)


class TestSummarize(unittest.TestCase):
    def test_pass_count_and_regime_split(self):
        rungs = [
            {"rel_diff": 0.5, "ratio": 2.0, "regime": "warm_sub"},   # fail gate
            {"rel_diff": 0.0, "ratio": 1.0, "regime": "cold"},       # pass gate
            {"rel_diff": None, "ratio": None, "regime": "warm_high"},
        ]
        s = mod.summarize(rungs)
        self.assertEqual(s["rel_diff_total"], 2)
        self.assertEqual(s["rel_diff_pass_gate"], 1)
        self.assertEqual(s["ratio_by_regime"]["warm_sub"]["median"], 2.0)
        self.assertEqual(s["ratio_by_regime"]["cold"]["median"], 1.0)
        self.assertIsNone(s["ratio_by_regime"]["warm_high"])


def _rungs(warm_ratios=(), warm_rels=(), cold_ratios=()):
    """Build a synthetic rung list for verdict() from the axes it reads."""
    out = []
    for ratio, rel in zip(warm_ratios, warm_rels):
        out.append({"regime": "warm_sub", "ratio": ratio, "rel_diff": rel})
    for ratio in cold_ratios:
        out.append({"regime": "cold", "ratio": ratio, "rel_diff": 0.0})
    return out


class TestVerdict(unittest.TestCase):
    def test_signature_present_pre_fix(self):
        # high warm ratio + mostly-failing gate + isolated cold control
        v = mod.verdict(_rungs(warm_ratios=[1.75, 1.8, 1.7],
                               warm_rels=[0.5, 0.5, 0.5],
                               cold_ratios=[1.0]))
        self.assertEqual(v["state"], "SIGNATURE_PRESENT")

    def test_signature_cleared_post_fix(self):
        # warm ratio collapsed to ~1.0 AND gate now passes on warm rungs
        v = mod.verdict(_rungs(warm_ratios=[1.0, 1.05, 0.98],
                               warm_rels=[0.02, 0.03, 0.05],
                               cold_ratios=[1.0]))
        self.assertEqual(v["state"], "SIGNATURE_CLEARED")

    def test_cleared_only_state_that_exits_zero(self):
        # fail-closed: only CLEARED graduates the cell (exit 0)
        self.assertEqual(mod.VERDICT_EXIT["SIGNATURE_CLEARED"], 0)
        for state in ("SIGNATURE_PRESENT", "AMBIGUOUS", "INSUFFICIENT_DATA"):
            self.assertNotEqual(mod.VERDICT_EXIT[state], 0)

    def test_cold_out_of_band_is_ambiguous(self):
        # warm looks pre-fix, but the CONTROL drifted -> mechanism not
        # isolated -> withhold the verdict even though warm reads "present"
        v = mod.verdict(_rungs(warm_ratios=[1.75, 1.8, 1.7],
                               warm_rels=[0.5, 0.5, 0.5],
                               cold_ratios=[1.5]))
        self.assertEqual(v["state"], "AMBIGUOUS")
        self.assertIn("cold control", v["reason"])

    def test_dead_band_ratio_is_ambiguous(self):
        # warm median 1.30 sits between CLEARED(<=1.20) and PRESENT(>=1.40)
        v = mod.verdict(_rungs(warm_ratios=[1.3, 1.3, 1.3],
                               warm_rels=[0.2, 0.2, 0.2],
                               cold_ratios=[1.0]))
        self.assertEqual(v["state"], "AMBIGUOUS")

    def test_high_ratio_but_passing_is_ambiguous(self):
        # ratio still high (1.75) yet gate passes -> the AND-gate blocks a
        # spurious PRESENT; not CLEARED either (ratio too high) -> AMBIGUOUS.
        # Proves ratio and pass-fraction must AGREE for a definitive call.
        v = mod.verdict(_rungs(warm_ratios=[1.75, 1.75, 1.75],
                               warm_rels=[0.02, 0.02, 0.02],
                               cold_ratios=[1.0]))
        self.assertEqual(v["state"], "AMBIGUOUS")

    def test_low_ratio_but_failing_is_ambiguous(self):
        # ratio collapsed (1.0) but gate still fails -> not CLEARED (pass too
        # low) and not PRESENT (ratio too low) -> AMBIGUOUS.
        v = mod.verdict(_rungs(warm_ratios=[1.0, 1.0, 1.0],
                               warm_rels=[0.5, 0.5, 0.5],
                               cold_ratios=[1.0]))
        self.assertEqual(v["state"], "AMBIGUOUS")

    def test_too_few_warm_rungs_insufficient(self):
        v = mod.verdict(_rungs(warm_ratios=[1.75, 1.8],
                               warm_rels=[0.5, 0.5],
                               cold_ratios=[1.0]))
        self.assertEqual(v["state"], "INSUFFICIENT_DATA")

    def test_no_cold_control_insufficient(self):
        v = mod.verdict(_rungs(warm_ratios=[1.75, 1.8, 1.7],
                               warm_rels=[0.5, 0.5, 0.5],
                               cold_ratios=[]))
        self.assertEqual(v["state"], "INSUFFICIENT_DATA")


if __name__ == "__main__":
    unittest.main()
