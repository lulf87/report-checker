from __future__ import annotations

import unittest
from pathlib import Path

from mvp.checker import SAMPLE_CONFIGS
from mvp.full_record_61 import (
    _identity_decision,
    _numeric_discovery_targets,
    run_record_61_full,
)
from mvp.record_full import ReportRow


ROOT = Path(__file__).resolve().parents[1]


class Record61IdentityEvidenceTests(unittest.TestCase):
    def test_single_low_confidence_candidate_stays_manual(self) -> None:
        decision = _identity_decision(
            "上海测试单位",
            {
                "attempted": True,
                "text_ocr": {"lines": [{"text": "另一单位", "confidence": 0.99}]},
                "tesseract": {"candidates": []},
            },
            ink_count=1,
        )
        self.assertEqual(decision[0], "manual")

    def test_two_independent_complete_channels_can_decide(self) -> None:
        recognition = {
            "attempted": True,
            "text_ocr": {"lines": [{"text": "上海测试单位", "confidence": 0.99}]},
            "tesseract": {"candidates": [{"text": "上海测试单位"}]},
        }
        self.assertEqual(_identity_decision("上海测试单位", recognition, ink_count=1)[0], "matched")
        self.assertEqual(_identity_decision("另一单位", recognition, ink_count=1)[0], "mismatch")

    def test_report_number_suffix_requires_agreement(self) -> None:
        recognition = {
            "attempted": True,
            "text_ocr": {"lines": [{"text": "1347", "confidence": 0.95}]},
            "tesseract": {"candidates": [{"text": "1347"}]},
        }
        self.assertEqual(_identity_decision("QW2026-1347", recognition, ink_count=1)[0], "matched")
        self.assertEqual(_identity_decision("QW2026-9999", recognition, ink_count=1)[0], "mismatch")


class Record61ScopeExpandedTests(unittest.TestCase):
    def test_numeric_discovery_emits_unvalidated_numeric_targets(self) -> None:
        row = ReportRow(
            row_id="report:s99", sequence=99, row_ordinal=1, pdf_page=10,
            project_raw="项目", clause_raw="99", requirement_raw="要求",
            result_raw="5 mA", conclusion_raw="符合", unit_context="mA",
            condition_tokens=(), requirement_rect=(1, 1, 2, 2),
            result_rect=(1, 1, 2, 2), conclusion_rect=(1, 1, 2, 2),
        )
        targets = _numeric_discovery_targets([row], set())
        self.assertEqual([item.row_id for item in targets], ["report:s99"])

    def test_real_sample_emits_field_payloads_and_conserved_metadata_ledger(self) -> None:
        config = SAMPLE_CONFIGS["1347"]
        result = run_record_61_full(ROOT / config.report, ROOT / config.record61)
        body = [item for item in result["ledger"] if item.get("rule_id") == "RECORD61-BODY-STATUS"]
        self.assertTrue(body)
        self.assertTrue({"project", "clause", "requirement", "condition", "unit", "result"} <= set(body[0]["record"]))
        self.assertTrue({"project", "clause", "requirement", "condition", "unit", "result"} <= set(body[0]["report"]))
        metadata = [item for item in result["ledger"] if item.get("rule_id") == "RECORD61-METADATA"]
        self.assertEqual(len(metadata), 6)
        self.assertTrue(result["coverage"]["source_rows"]["conserved"])
        self.assertTrue(result["coverage"]["report_rows"]["conserved"])
        self.assertTrue(all(item.get("scope_ids") for item in metadata))


if __name__ == "__main__":
    unittest.main()
