from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import mvp.ptr_gold as gold


def entry(clause, decision, reason="EXACT_NORMALIZED_MATCH", **extra):
    return {"clause_id": clause, "decision": decision, "reason_code": reason, **extra}


class PtrGoldTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(gold, "GOLD_DIR", Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def run_of(self, *entries):
        return {"samples": {"9999": {"rule_version": "x", "inputs": {}, "coverage_ledger": list(entries)}}}

    def test_duplicate_clause_numbers_get_distinct_keys(self) -> None:
        keys = [k for k, _ in gold.keyed_entries([entry("2.1", "matched"), entry("2.2", "matched"), entry("2.1", "manual")])]
        self.assertEqual(keys, ["2.1#1", "2.2", "2.1#2"])

    def test_risk(self) -> None:
        self.assertEqual(gold.risk_of(entry("2.1", "mismatch")), "A")
        self.assertEqual(gold.risk_of(entry("2.1", "matched")), "D")
        self.assertEqual(
            gold.risk_of(entry("2.1", "matched", "SAFE_TEXT_EQUIVALENCE",
                               text_comparison={"equivalence_rules": [{"rule": "PUNCTUATION_ONLY"}]})),
            "C",
        )

    def test_draft_merge_check(self) -> None:
        run = self.run_of(entry("2.1", "matched"), entry("2.2", "matched"), entry("2.3", "manual"))
        gold.draft(run)
        gold.merge({"verdicts": [
            {"sample": "9999", "key": "2.2", "expected": "mismatch", "issue": "单位 MΩ ↔ GΩ"},
            {"sample": "9999", "key": "2.3", "expected": "matched", "acceptable": ["manual"]},
        ]})
        # Confirmed entries survive a later draft.
        gold.draft(run)
        result = gold.check(run)
        self.assertEqual(result["totals"], {"draft": 1, "confirmed": 2, "confirmed_correct": 1})
        self.assertEqual([i["key"] for i in result["findings"]["missed"]], ["2.2"])
        # A changed unreviewed entry shows up as drift.
        drifted = self.run_of(entry("2.1", "manual"), entry("2.2", "mismatch"), entry("2.3", "manual"))
        result = gold.check(drifted)
        self.assertEqual(list(result["findings"]), ["draft_drift"])


if __name__ == "__main__":
    unittest.main()
