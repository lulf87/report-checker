from __future__ import annotations

import unittest
from pathlib import Path

import pymupdf as fitz

from mvp.checker import SAMPLE_CONFIGS
from mvp.full_record_61 import (
    _mapping_reason_code,
    _paired_field_comparisons,
    _status_from_ink,
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
    def test_status_semantics_distinguish_blank_void_ambiguous_and_alternate(self) -> None:
        status_cell = fitz.Rect(0, 0, 100, 10)
        boxes = [fitz.Rect(5, 1, 15, 9), fitz.Rect(45, 1, 55, 9), fitz.Rect(85, 1, 95, 9)]
        blank = _status_from_ink(status_cell, boxes, [])
        self.assertEqual((blank["status"], blank["status_class"]), (None, "blank"))
        void = _status_from_ink(
            status_cell,
            boxes,
            [{"centroid_x": 50, "centroid_y": 5, "bbox": [0, 3, 100, 7], "point_count": 2}],
        )
        self.assertEqual((void["status"], void["status_class"]), (None, "void_or_crossed_out"))
        ambiguous = _status_from_ink(
            status_cell,
            boxes,
            [
                {"centroid_x": 10, "centroid_y": 5, "bbox": [6, 3, 14, 8], "point_count": 2},
                {"centroid_x": 90, "centroid_y": 5, "bbox": [86, 3, 94, 8], "point_count": 2},
            ],
            glyph_encoding="alternate_first_square_triplet",
        )
        self.assertEqual((ambiguous["status"], ambiguous["status_class"]), (None, "ambiguous"))
        self.assertEqual(ambiguous["glyph_variant"], "alternate")

    def test_field_comparisons_keep_suggestion_condition_unit_and_result(self) -> None:
        record = {
            "project": "项目",
            "clause": "8.1",
            "requirement": "要求",
            "suggestion": "",
            "condition": "",
            "unit": None,
            "result": "符合",
        }
        report = {
            "project": "项目",
            "clause": "8.1",
            "requirement": "要求",
            "suggestion": "",
            "condition": "",
            "unit": None,
            "result": "符合要求",
        }
        comparisons = _paired_field_comparisons(
            record,
            report,
            result_comparison={"decision": "match", "reason_code": "status_result_matched"},
        )
        self.assertEqual(
            set(comparisons),
            {"project", "clause", "requirement", "suggestion", "condition", "unit", "result"},
        )
        self.assertEqual(comparisons["result"]["decision"], "match")
        self.assertEqual(comparisons["suggestion"]["decision"], "not_applicable")
        unresolved = _paired_field_comparisons(record, report, mapped=False)
        self.assertTrue(all(item["decision"] == "manual" for item in unresolved.values()))

    def test_mapping_reason_codes_distinguish_missing_extra_and_ambiguous(self) -> None:
        target = ReportRow(
            row_id="report:1", sequence=1, row_ordinal=1, pdf_page=1,
            project_raw="", clause_raw="8.1", requirement_raw="唯一要求文本",
            result_raw="符合要求", conclusion_raw="符合", unit_context=None,
            condition_tokens=(), requirement_rect=(1, 1, 2, 2),
            result_rect=(2, 1, 3, 2), conclusion_rect=(3, 1, 4, 2),
        )
        source = {"row_id": "record:1", "clause": "8.1", "requirement": "其他要求"}
        self.assertEqual(
            _mapping_reason_code(source, None, [source], [target], "legacy"),
            "record_row_missing_in_report",
        )
        self.assertEqual(
            _mapping_reason_code(None, target, [source], [target], "legacy"),
            "report_row_extra_vs_record",
        )
        first = {"row_id": "record:a", "clause": "8.1", "requirement": "唯一要求文本"}
        second = {"row_id": "record:b", "clause": "8.1", "requirement": "唯一要求文本"}
        self.assertEqual(
            _mapping_reason_code(None, target, [first, second], [target], "legacy"),
            "report_row_ambiguous_mapping",
        )

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
