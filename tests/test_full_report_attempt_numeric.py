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
        # 2026-10-20：偏差写法、SI 词头换算和带括号符号的结果已能确定判定。
        self.assertEqual(self.result["counts"], {"pass": 61, "error": 0, "manual": 0})
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

    def test_former_manual_results_are_now_decided_with_explicit_reasons(self) -> None:
        def reason(pdf_page: int, sequence: int, result: str) -> str:
            item = next(
                entry for entry in self.result["comparisons"]
                if entry["pdf_page"] == pdf_page and entry["sequence"] == sequence and entry["result"] == result
            )
            self.assertEqual(item["status"], "pass")
            return item["reason"]

        self.assertEqual(reason(43, 59, "0.05 (&)"), "annotated_value_within_bound")
        self.assertEqual(reason(102, 152, "300"), "value_in_tolerance")
        self.assertEqual(reason(105, 157, "+71"), "signed_deviation_within_tolerance")
        self.assertEqual(reason(105, 157, "+0.099"), "signed_deviation_within_tolerance")
        self.assertEqual(reason(106, 161, "1.1～2.6"), "range_within_bound")
        insulation = next(entry for entry in self.result["comparisons"] if entry["pdf_page"] == 106 and entry["sequence"] == 161)
        self.assertEqual(insulation["criterion"]["converted_from"], "MΩ")
        self.assertEqual(insulation["criterion"]["value"], "0.005")

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


class NumericCriterionExtensionTests(unittest.TestCase):
    def check(self, requirement: str, result: str) -> tuple[str, str]:
        decision = evaluate_numeric_measurement(requirement, result)
        return decision["status"], decision["reason"]

    def test_signed_results_are_deviations_only_when_unambiguous(self) -> None:
        self.assertEqual(self.check("标称值：1700V±100V； 单位：V", "+71"), ("pass", "signed_deviation_within_tolerance"))
        self.assertEqual(self.check("标称值：1700V±100V； 单位：V", "+171"), ("error", "signed_deviation_exceeds_tolerance"))
        # 按绝对值理解也可能合格时不猜。
        self.assertEqual(self.check("标称值：1700V±100V； 单位：V", "+1690")[0], "manual")
        self.assertEqual(self.check("1.12msec±4μsec 单位：μs", "+0"), ("pass", "signed_deviation_within_tolerance"))
        self.assertEqual(self.check("允差： ±15ms | @240Ω 单位：ms", "-16～+1"), ("error", "signed_deviation_exceeds_tolerance"))
        self.assertEqual(self.check("@240Ω | 0.2V：±50mV 单位：mV", "-26"), ("pass", "signed_deviation_within_tolerance"))
        # “以下要求”不是限值措辞。
        self.assertEqual(self.check("包括以下要求： ●ESC 波长：785nm ±10nm 单位：nm", "-4")[0], "pass")
        # 没有标称值也没有“允差/误差”字样时不按偏差处理。
        self.assertEqual(self.check("±10 单位：V", "+3")[0], "manual")

    def test_percent_deviation_and_asymmetric_tolerance(self) -> None:
        self.assertEqual(self.check("脉冲宽 度 | 0.9μsec±20%", "-9%"), ("pass", "percent_deviation_within_tolerance"))
        self.assertEqual(self.check("脉冲宽 度 | 0.9μsec±20%", "-25%"), ("error", "percent_deviation_exceeds_tolerance"))
        self.assertEqual(self.check("@240Ω | 0.4V～7.5V：+20 %/-25%", "-24%～-14%")[0], "pass")
        self.assertEqual(self.check("@240Ω | 0.4V～7.5V：+20 %/-25%", "+21%")[0], "error")
        self.assertEqual(self.check("精度± 1 个或± 10%，取最大值。", "+0%")[0], "pass")

    def test_more_chinese_bound_wording(self) -> None:
        self.assertEqual(self.check("单个脉冲最大输出能量应小于258mJ。 单位：mJ", "159")[0], "pass")
        self.assertEqual(self.check("单个脉冲最大输出能量应小于258mJ。 单位：mJ", "258")[0], "error")
        self.assertEqual(self.check("但不应将声级降到40dB（A 计权）以下。 单位：dB(A)", "41")[0], "pass")
        self.assertEqual(self.check("但不应将声级降到40dB（A 计权）以下。 单位：dB(A)", "39")[0], "error")
        self.assertEqual(self.check("最小脉冲幅度应不低于最大脉冲幅度的80%。", "84%")[0], "pass")
        self.assertEqual(self.check("衰减应在10%内。", "11%")[0], "error")
        # “不小于”不能被误读为上限“小于”。
        self.assertEqual(self.check("绝缘电阻应不小于5MΩ 单位：MΩ", "6")[0], "pass")

    def test_si_prefix_conversion(self) -> None:
        self.assertEqual(self.check("绝缘电阻应不小于 5MΩ. 单位：GΩ", "1.1～2.6")[0], "pass")
        self.assertEqual(self.check("绝缘电阻应不小于 5MΩ. 单位：GΩ", "0.004")[0], "error")
        self.assertEqual(self.check("声音输出的主要能量应包含在 100Hz~3KHz 的频段内， 单位：Hz", "358")[0], "pass")
        self.assertEqual(self.check("声音输出的主要能量应包含在 100Hz~3KHz 的频段内， 单位：Hz", "3500")[0], "error")

    def test_annotation_suffix_only_for_upper_bounds(self) -> None:
        self.assertEqual(self.check("患者漏电流 | CF≤0.05mA", "0.05 (−)"), ("pass", "annotated_value_within_bound"))
        self.assertEqual(self.check("患者漏电流 | CF≤0.05mA", "0.06 (−)"), ("error", "annotated_value_exceeds_bound"))
        self.assertEqual(self.check("患者漏电流 | CF≤0.05mA", "0.05 (x)")[0], "manual")
        self.assertEqual(self.check("绝缘电阻应不小于5MΩ 单位：MΩ", "6 (−)")[0], "manual")


if __name__ == "__main__":
    unittest.main()
