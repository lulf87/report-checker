"""Scanned PTR: one OCR engine reads the clause as the Report prints it, the
other misreads single characters (2026-11-14 ruling: 一致)."""

from __future__ import annotations

import unittest

from mvp.ptr_compare import (
    MATCHED,
    OCR_DUAL_ENGINE_MATCH,
    OCR_ENGINE_MATCH_OTHER_MISREAD,
    _ocr_dual_engine_confirmation,
    _compare_text,
    _unit_equivalent,
    ocr_character_slips,
)
from mvp.ptr_layout import report_item_title


def _ptr(text: str, engine: str) -> dict:
    return {
        "role": "ptr", "clause_id": "2.1.1", "text": text, "extraction_issues": ["OCR_REVIEW_REQUIRED"],
        "evidence": [{"extraction_method": f"ocr:{engine}"}], "table_rows": [],
    }


def _report(text: str) -> dict:
    return {"role": "report", "clause_id": "2.1.1", "text": text,
            "evidence": [{"extraction_method": "report_requirement_cell"}], "table_rows": []}


def _confirm(primary_text: str, second_text: str, report_text: str) -> dict:
    primary, report = _ptr(primary_text, "engine_a"), _report(report_text)
    comparison = _compare_text(primary, report, has_children=False, context={})
    return _ocr_dual_engine_confirmation(
        primary, report, comparison, {"engine_b": {"2.1.1": [_ptr(second_text, "engine_b")]}},
        has_children=False, context={},
    )


class CharacterSlipTests(unittest.TestCase):
    def test_misread_symbols(self) -> None:
        self.assertEqual(ocr_character_slips("误差不大于士20%", "误差不大于±20%"), [{"read": "士", "other": "±"}])
        self.assertEqual(len(ocr_character_slips("阻值应不大于1002。", "阻值应不大于10Ω。")), 1)
        self.assertEqual(len(ocr_character_slips("绝缘电阻应大于 5MS2。", "绝缘电阻应大于5MΩ。")), 1)

    def test_identical_apart_from_spacing_and_punctuation(self) -> None:
        self.assertEqual(ocr_character_slips("拉力应 ≥15N", "拉力应≥15N。"), [])

    def test_a_line_only_one_engine_read_is_refused(self) -> None:
        self.assertIsNone(ocr_character_slips("上升时间≤700ns。\n21 13", "上升时间≤700ns。"))
        self.assertIsNone(ocr_character_slips("拉力应≥15N。", "拉力应≥15N。夹持力应≥30N。"))

    def test_too_many_differences_are_refused(self) -> None:
        self.assertIsNone(ocr_character_slips("甲1乙2丙3丁4戊5己6庚7辛8壬9", "甲a乙b丙c丁d戊e己f庚g辛h壬i"))

    def test_two_plausible_values_are_refused(self) -> None:
        # A digit against another digit, letters against other letters: either
        # reading may be what the PTR says.
        self.assertIsNone(ocr_character_slips("应不小于 6MΩ。", "应不小于 5MΩ。"))
        self.assertIsNone(ocr_character_slips("应不小于 5GΩ。", "应不小于 5MΩ。"))
        # A digit against a letter or a symbol is a misreading.
        self.assertEqual(ocr_character_slips("型号：RMDO1", "型号：RMD01"), [{"read": "O", "other": "0"}])

    def test_empty_reading_is_refused(self) -> None:
        self.assertIsNone(ocr_character_slips("", "拉力应≥15N"))


class OneEngineExactTests(unittest.TestCase):
    REPORT = "峰值拉力\n各试验段的峰值拉力应≥15N。"

    def test_both_engines_exact_keeps_the_dual_rule(self) -> None:
        result = _confirm(self.REPORT, self.REPORT, self.REPORT)
        self.assertEqual((result["status"], result["rule"]), ("confirmed", OCR_DUAL_ENGINE_MATCH))

    def test_primary_misreads_one_symbol(self) -> None:
        result = _confirm(self.REPORT.replace("≥15N", "215N"), self.REPORT, self.REPORT)
        self.assertEqual((result["status"], result["rule"]), ("confirmed", OCR_ENGINE_MATCH_OTHER_MISREAD))
        self.assertEqual(result["matched_engine"], "engine_b")
        self.assertEqual(result["misreadings"], [{"engine": "engine_a", "differences": [{"read": "2", "other": "≥"}]}])
        # The comparison shown is the one of the engine that read it right.
        self.assertEqual(result["matched_comparison"]["decision"], MATCHED)

    def test_second_engine_misreads_one_symbol(self) -> None:
        result = _confirm(self.REPORT, self.REPORT.replace("≥15N", "215N"), self.REPORT)
        self.assertEqual((result["status"], result["rule"]), ("confirmed", OCR_ENGINE_MATCH_OTHER_MISREAD))
        self.assertEqual(result["matched_engine"], "engine_a")
        self.assertIsNone(result["matched_comparison"])

    def test_neither_engine_exact_stays_manual(self) -> None:
        result = _confirm(self.REPORT.replace("≥15N", "215N"), self.REPORT.replace("≥15N", "≥I5N"), self.REPORT)
        self.assertEqual(result["status"], "not_confirmed")

    def test_other_engine_reads_different_content_stays_manual(self) -> None:
        result = _confirm(self.REPORT + "\n夹持力应不小于30N。", self.REPORT, self.REPORT)
        self.assertEqual(result["status"], "not_confirmed")

    def test_engines_reading_two_different_numbers_stay_manual(self) -> None:
        result = _confirm(self.REPORT.replace("15N", "16N"), self.REPORT, self.REPORT)
        self.assertEqual(result["status"], "not_confirmed")

    def test_report_that_differs_from_both_stays_manual(self) -> None:
        # A real difference (the Report prints another value) is never passed.
        result = _confirm(self.REPORT, self.REPORT, self.REPORT.replace("15N", "10N"))
        self.assertEqual(result["status"], "not_confirmed")


class ReportTitleAndUnitTests(unittest.TestCase):
    def test_own_clause_number_in_front_of_the_title(self) -> None:
        self.assertEqual(report_item_title("2.7. 电\n磁兼容", "2.7"), "电\n磁兼容")
        self.assertEqual(report_item_title("2.7 电磁兼容", "2.7"), "电磁兼容")

    def test_other_numbers_stay(self) -> None:
        self.assertEqual(report_item_title("电磁兼容", "2.7"), "电磁兼容")
        self.assertEqual(report_item_title("2.7.1 发射", "2.7"), "2.7.1 发射")
        self.assertEqual(report_item_title("2.75 mm 导丝", "2.7"), "2.75 mm 导丝")

    def test_litre_spelling(self) -> None:
        self.assertTrue(_unit_equivalent("mL", "ml")[0])
        self.assertTrue(_unit_equivalent("μg/mL", "μg/ml")[0])
        self.assertFalse(_unit_equivalent("mL", "L")[0])
        self.assertFalse(_unit_equivalent("mL", "mm")[0])


if __name__ == "__main__":
    unittest.main()
