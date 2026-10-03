from __future__ import annotations

import unittest
from collections import Counter
from pathlib import Path

from mvp.full_report_attempt import (
    evaluate_numeric_measurement,
    scan_report_numeric_file,
)


ROOT = Path(__file__).resolve().parents[1]
REPORT_2795 = ROOT / "素材/report/2795/QW2025-2795 Draft.pdf"


class FullReportNumericAttemptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        before = REPORT_2795.stat()
        cls.result = scan_report_numeric_file(REPORT_2795)
        after = REPORT_2795.stat()
        cls.before_source_fingerprint = (before.st_size, before.st_mtime_ns)
        cls.after_source_fingerprint = (after.st_size, after.st_mtime_ns)

    def comparison(self, pdf_page: int, row_index: int) -> dict:
        return next(
            item
            for item in self.result["comparisons"]
            if item["pdf_page"] == pdf_page and item["row_index"] == row_index
        )

    def test_2795_numeric_inventory_and_status_counts(self) -> None:
        self.assertEqual(self.result["digit_containing_result_cells"], 64)
        self.assertEqual(self.result["numeric_measurements"], 61)
        self.assertEqual(self.result["counts"], {"pass": 53, "error": 0, "manual": 8})
        self.assertEqual(
            [(item["sequence"], item["result"]) for item in self.result["excluded_numeric_tokens"]],
            [
                (18, "IPX0"),
                (162, "见序号1～ 序号118"),
                (162, "见序号 119～序号 156"),
            ],
        )

    def test_scan_does_not_modify_source_pdf(self) -> None:
        self.assertEqual(self.before_source_fingerprint, self.after_source_fingerprint)

    def test_nested_acceptance_columns_are_joined_without_page_rules(self) -> None:
        two_columns = self.comparison(42, 9)
        self.assertEqual(two_columns["standard_columns"], [3, 4])
        self.assertEqual(two_columns["requirement_components"], ["无频率 加权漏 电流", "≤10mA"])
        self.assertEqual(two_columns["status"], "pass")

        three_columns = self.comparison(43, 3)
        self.assertEqual(three_columns["standard_columns"], [3, 4, 5])
        self.assertEqual(
            three_columns["requirement_components"],
            ["患者漏 电流", "直流", "正常状态下≤0.01mA"],
        )
        self.assertEqual(three_columns["result"], "＜0.01")
        self.assertEqual(three_columns["status"], "pass")
        self.assertEqual(three_columns["printed_page"], 41)

    def test_manual_results_are_limited_to_three_conservative_gates(self) -> None:
        manual = [item for item in self.result["comparisons"] if item["status"] == "manual"]
        self.assertEqual(
            Counter(item["reason"] for item in manual),
            {
                "numeric_result_has_unconfirmed_suffix": 2,
                "unit_prefix_conversion_required_but_disabled": 2,
                "signed_result_may_be_deviation_or_absolute_value": 4,
            },
        )
        self.assertEqual(
            [(item["pdf_page"], item["sequence"], item["result"]) for item in manual],
            [
                (43, 59, "0.05 (&)"),
                (44, 59, "0.05 (&)"),
                (102, 152, "300"),
                (105, 157, "+71"),
                (105, 157, "+61"),
                (105, 157, "+0.16"),
                (105, 157, "+0.099"),
                (106, 161, "1.1～2.6"),
            ],
        )

    def test_percentage_range_and_derived_limits_are_deterministic(self) -> None:
        percentage = self.comparison(100, 7)
        self.assertEqual(percentage["status"], "pass")
        self.assertEqual(percentage["criterion"]["kind"], "interval")
        self.assertEqual((percentage["criterion"]["low"], percentage["criterion"]["high"]), ("-20", "20"))

        creepage = self.comparison(94, 1)
        self.assertEqual(creepage["status"], "pass")
        self.assertEqual(creepage["criterion"]["value"], "5.4")

        capacitance = self.comparison(97, 6)
        self.assertEqual(capacitance["status"], "pass")
        self.assertEqual(capacitance["criterion"]["value"], "2244")

    def test_strict_and_inclusive_boundaries_remain_distinct(self) -> None:
        strict = evaluate_numeric_measurement("接受标准：<0.5A 单位：A", "0.5")
        inclusive = evaluate_numeric_measurement("接受标准：≤0.5A 单位：A", "0.5")
        censored_pass = evaluate_numeric_measurement("接受标准：≤0.01mA 单位：mA", "＜0.01")
        censored_manual = evaluate_numeric_measurement("接受标准：≤0.5mA 单位：mA", "＜0.6")

        self.assertEqual((strict["status"], strict["reason"]), ("error", "value_exceeds_bound"))
        self.assertEqual((inclusive["status"], inclusive["reason"]), ("pass", "value_within_bound"))
        self.assertEqual(censored_pass["status"], "pass")
        self.assertEqual((censored_manual["status"], censored_manual["reason"]), ("manual", "censored_result_not_decisive"))


if __name__ == "__main__":
    unittest.main()
