import json
import tempfile
import unittest
from pathlib import Path

import pymupdf as fitz

from mvp.checker import sha256_file
from mvp.full_report_photo import (
    analyze_report_photo_rules,
    associate_rows_to_photos,
    compare_date_text,
    date_format,
    load_ocr_records,
    parse_photo_entries,
    parse_sample_description,
    photo_scope,
    report_photo_check,
)


ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "素材/report/2795/QW2025-2795 Draft.pdf"
LABEL_OCR = ROOT / "tmp/pdfs/2795/full-attempt/label-ocr.json"
OBJECT_OCR = ROOT / "tmp/pdfs/2795/full-attempt/object-ocr.json"


class PhotoRuleUnitTests(unittest.TestCase):
    def test_date_requires_same_value_and_format(self) -> None:
        self.assertEqual(date_format("2025-04-19"), "YYYY-MM-DD")
        self.assertEqual(date_format("2025.04.19"), "YYYY.MM.DD")
        self.assertEqual(date_format("2025/04/19"), "YYYY/MM/DD")
        self.assertEqual(date_format("20250419"), "YYYYMMDD")
        self.assertEqual(date_format("2025年04月19日"), "YYYY年MM月DD日")
        self.assertIsNone(date_format("2025-02-30"))
        self.assertEqual(compare_date_text("2025-04-19", "2025-04-19")["status"], "pass")
        different_format = compare_date_text("2025-04-19", "2025.04.19")
        self.assertEqual(different_format["status"], "manual")
        self.assertEqual(different_format["reason_code"], "single_ocr_source_date_disagrees")
        self.assertEqual(compare_date_text("/", "见实物")["status"], "manual")

    def test_name_fallback_is_narrow_and_unique(self) -> None:
        rows = [
            {"sequence": 1, "fields": {"部件名称": "心脏脉冲电场消融仪-触摸屏"}},
            {
                "sequence": 2,
                "fields": {"部件名称": "心脏脉冲电场消融仪-触摸屏连接线缆（30m）（可选）"},
            },
            {"sequence": 3, "fields": {"部件名称": "光接收器（可选）"}},
        ]
        entries = [
            {"subject": "心脏脉冲电场消融仪-触摸屏", "number": 1},
            {"subject": "触摸屏连接线缆（30m）（可选）", "number": 2},
            {"subject": "光接收器", "number": 3},
        ]
        associations = associate_rows_to_photos(rows, entries)
        self.assertEqual([item["number"] for item in associations[1]], [1])
        self.assertEqual([item["number"] for item in associations[2]], [2])
        self.assertEqual([item["number"] for item in associations[3]], [3])

    def test_malformed_ocr_is_not_guessed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "ocr.json"
            source.write_text(json.dumps({"pdf_page": 1}), encoding="utf-8")
            records, diagnostics = load_ocr_records(source)
        self.assertEqual(records, [])
        self.assertTrue(any("顶层不是数组" in item for item in diagnostics))

    def test_photo_scope_excludes_explicitly_unused_and_non_physical_rows(self) -> None:
        self.assertEqual(
            photo_scope({"fields": {"部件名称": "心脏脉冲电场消融仪", "备注": "本次检测未使用"}}),
            (False, "sample_marked_not_used"),
        )
        self.assertEqual(
            photo_scope({"fields": {"部件名称": "控制软件模块", "备注": ""}}),
            (False, "non_physical_sample_entry"),
        )
        self.assertEqual(
            photo_scope({"fields": {"部件名称": "心脏脉冲电场消融仪", "备注": ""}}),
            (True, None),
        )


class FullReportPhoto2795IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.before_hash = sha256_file(REPORT)
        cls.analysis = analyze_report_photo_rules(REPORT, LABEL_OCR, OBJECT_OCR)
        cls.after_hash = sha256_file(REPORT)

    def test_source_pdf_remains_read_only(self) -> None:
        self.assertEqual(self.before_hash, self.after_hash)

    def test_sample_table_and_photo_page_discovery(self) -> None:
        with fitz.open(REPORT) as document:
            rows, row_diagnostics = parse_sample_description(document)
            photos, photo_diagnostics = parse_photo_entries(document)
        self.assertEqual(row_diagnostics, [])
        self.assertEqual(photo_diagnostics, [])
        self.assertEqual([row["sequence"] for row in rows], list(range(1, 22)))
        self.assertEqual({row["pdf_page"] for row in rows}, {4, 5})
        self.assertEqual([entry["number"] for entry in photos], list(range(1, 28)))
        self.assertEqual(sum(entry["kind"] == "object" for entry in photos), 14)
        self.assertEqual(sum(entry["kind"] == "label" for entry in photos), 12)
        self.assertTrue(all(entry["title_rect"] for entry in photos))
        self.assertTrue(all(entry["image_rect"] for entry in photos))

    def test_all_five_rules_are_attempted_with_expected_rule_status(self) -> None:
        findings = {item["id"]: item for item in self.analysis["findings"]}
        self.assertEqual(
            {rule: findings[rule]["status"] for rule in findings},
            {
                "REPORT-R02": "manual",
                "REPORT-R03": "manual",
                "REPORT-R04": "manual",
                "REPORT-R05": "pass",
                "REPORT-R06": "pass",
            },
        )
        self.assertEqual(len(report_photo_check(REPORT, LABEL_OCR, OBJECT_OCR)), 5)

    def test_object_and_label_presence_is_derived_from_captions(self) -> None:
        findings = {item["id"]: item for item in self.analysis["findings"]}
        for rule in ("REPORT-R05", "REPORT-R06"):
            objects = findings[rule]["details"]["objects"]
            passed = [item["sequence"] for item in objects if item["status"] == "pass"]
            missing = [item["sequence"] for item in objects if item["status"] == "error"]
            not_applicable = [item["sequence"] for item in objects if item["status"] == "not_applicable"]
            self.assertEqual(passed, [1, 2, 3, 4, 5, 8, 9, 10, 11, 14, 16, 18])
            self.assertEqual(missing, [])
            self.assertEqual(not_applicable, [6, 7, 12, 13, 15, 17, 19, 20, 21])

    def test_r02_and_r03_preserve_ocr_uncertainty(self) -> None:
        findings = {item["id"]: item for item in self.analysis["findings"]}
        r02_objects = findings["REPORT-R02"]["details"]["objects"]
        self.assertEqual(
            {status: sum(item["status"] == status for item in r02_objects) for status in ("pass", "manual", "error")},
            {"pass": 7, "manual": 5, "error": 0},
        )
        self.assertEqual(
            sum(item["status"] == "not_applicable" for item in r02_objects),
            9,
        )
        r03 = findings["REPORT-R03"]["details"]["comparisons"]
        self.assertEqual(sum(item["status"] == "pass" for item in r03), 7)
        self.assertEqual(sum(item["status"] == "manual" for item in r03), 5)
        self.assertEqual(sum(item["status"] == "not_applicable" for item in r03), 9)
        placeholders = [
            item for item in r03 if item["reason_code"] == "date_not_stated_on_both_sides"
        ]
        self.assertEqual([item["sequence"] for item in placeholders], [10, 11, 14, 16, 18])

    def test_every_issue_has_a_real_report_or_photo_coordinate(self) -> None:
        for finding in self.analysis["findings"]:
            if finding["status"] == "pass":
                continue
            self.assertTrue(finding["evidence"], finding["id"])
            for evidence in finding["evidence"]:
                self.assertIsInstance(evidence["pdf_page"], int)
                self.assertGreater(evidence["pdf_page"], 0)
                self.assertEqual(len(evidence["rects"]), 1)
                x0, y0, x1, y1 = evidence["rects"][0]
                self.assertLess(x0, x1)
                self.assertLess(y0, y1)
                if evidence["source"] == "photo_image":
                    self.assertEqual(evidence["coordinate_precision"], "image_region")

    def test_r04_keeps_unused_rows_as_not_applicable(self) -> None:
        r04 = next(item for item in self.analysis["findings"] if item["id"] == "REPORT-R04")
        not_applicable_name_sequences = [
            item["sequence"]
            for item in r04["details"]["cell_checks"]
            if item["field"] == "部件名称" and item["status"] == "not_applicable"
        ]
        self.assertEqual(not_applicable_name_sequences, [6, 7, 12, 13, 15, 17, 19, 20, 21])
        self.assertIn("明确未使用", r04["details"]["rule"])


if __name__ == "__main__":
    unittest.main()
