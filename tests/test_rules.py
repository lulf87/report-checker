import unittest
from decimal import Decimal, ROUND_HALF_UP

from mvp.checker import (
    EXPECTED_202_LEGEND_MAPPING,
    accepted_numeric_candidate,
    classify_report_number_field,
    compare_record_61_statuses,
    deterministic_comparison_status,
    expected_report_conclusion,
    extract_report_number,
    leading_202_clause,
    verified_202_legend_mapping,
)


class RuleTests(unittest.TestCase):
    def test_report_number(self) -> None:
        self.assertEqual(extract_report_number("编号：国医检(设)字 QW2025 第1539 号"), (2025, 1539))
        self.assertEqual(
            classify_report_number_field("编号：国医检(设)字 QW    第    号")["state"],
            "explicit_missing",
        )

    def test_numeric_candidate_requires_agreement(self) -> None:
        self.assertIsNone(
            accepted_numeric_candidate(
                {
                    "apple_vision": [{"text": "910.2", "confidence": 1.0}],
                    "tesseract": [{"psm": 7, "text": "910.3"}],
                }
            )
        )
        self.assertEqual(
            accepted_numeric_candidate(
                {
                    "apple_vision": [{"text": "910.2", "confidence": 1.0}],
                    "tesseract": [{"psm": 7, "text": "910.2"}],
                }
            ),
            "910.2",
        )
        self.assertIsNone(
            accepted_numeric_candidate(
                {
                    "apple_vision": [],
                    "tesseract": [
                        {"psm": 7, "text": "910.2"},
                        {"psm": 13, "text": "910.2"},
                    ],
                }
            )
        )

    def test_unit_conversion_and_report_precision(self) -> None:
        normalized = Decimal("910.2") / Decimal(1000)
        self.assertEqual(normalized.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), Decimal("0.91"))

    def test_202_clause_must_be_leading_text(self) -> None:
        self.assertEqual(leading_202_clause("201.15.101.4 中性电极电缆的绝缘"), "201.15.101.4")
        self.assertIsNone(leading_202_clause("要求和试验在201.8.8.3.101和201.15.101.4中给出"))
        self.assertIsNone(leading_202_clause("本文件中201.15.101.5所列限值"))

    def test_report_result_aggregation(self) -> None:
        self.assertEqual(expected_report_conclusion(["——", "/"]), "/")
        self.assertEqual(expected_report_conclusion(["——", "0.3"]), "符合")
        self.assertEqual(expected_report_conclusion(["符合要求", "——"]), "符合")
        self.assertEqual(expected_report_conclusion(["符合要求", "不符合要求"]), "不符合")
        self.assertEqual(expected_report_conclusion([]), "<检验结果缺失>")

    def test_deterministic_mismatch_outranks_manual(self) -> None:
        self.assertEqual(
            deterministic_comparison_status([{"decision": "mismatch"}, {"decision": "manual"}]),
            "error",
        )
        self.assertEqual(deterministic_comparison_status([{"decision": "manual"}]), "manual")
        self.assertEqual(
            deterministic_comparison_status([{"decision": "match"}, {"decision": "match"}]),
            "pass",
        )

    def test_record_61_three_state_aggregation(self) -> None:
        cases = [
            (("符合", "符合"), "符合"),
            (("符合", "不适用"), "符合"),
            (("不适用", "符合"), "符合"),
            (("不适用", "不适用"), "/"),
            (("不符合", "符合"), "不符合"),
            (("符合", "不符合"), "不符合"),
            (("不符合", "不适用"), "不符合"),
            (("不适用", "不符合"), "不符合"),
            (("不符合", "不符合"), "不符合"),
        ]
        for statuses, expected in cases:
            with self.subTest(statuses=statuses):
                result = compare_record_61_statuses([[statuses[0]], [statuses[1]]], [expected])
                self.assertEqual(result["status"], "pass")
                self.assertEqual(result["expected_report_conclusion"], expected)

        self.assertEqual(compare_record_61_statuses([["不适用"], ["不适用"]], ["符合"])["status"], "error")
        self.assertEqual(compare_record_61_statuses([["符合"], ["不适用"]], ["/"])["status"], "error")
        self.assertEqual(compare_record_61_statuses([["不符合"], ["符合"]], ["符合"])["status"], "error")
        self.assertEqual(compare_record_61_statuses([[], ["符合"]], ["符合"])["status"], "manual")
        self.assertEqual(compare_record_61_statuses([["符合", "不适用"], ["符合"]], ["符合"])["status"], "manual")
        self.assertEqual(compare_record_61_statuses([["符合"]], ["符合"])["status"], "manual")
        self.assertEqual(compare_record_61_statuses([["符合"], ["符合"]], ["符合", "不符合"])["status"], "error")
        self.assertEqual(compare_record_61_statuses([["符合"], ["符合"]], ["——"])["status"], "error")

    def test_202_legend_requires_verified_symbol_meaning_pairs(self) -> None:
        valid = '注：“√”为符合要求，“×”为不符合要求，“∆”为不适用，“/”为此项空白。'
        self.assertEqual(verified_202_legend_mapping([valid]), EXPECTED_202_LEGEND_MAPPING)
        self.assertEqual(
            verified_202_legend_mapping(['√为符合要求，×为不符合要求，Δ为不适用，/为此项空白']),
            EXPECTED_202_LEGEND_MAPPING,
        )
        self.assertIsNone(verified_202_legend_mapping(['符合要求 不符合要求 不适用 此项空白']))
        self.assertIsNone(verified_202_legend_mapping(['√为不适用，×为不符合要求，△为符合要求，/为此项空白']))
        self.assertIsNone(verified_202_legend_mapping(['要求，x为不符合要求，A为不适用，p功此项空白']))
        self.assertIsNone(
            verified_202_legend_mapping([
                '√为符合要求，√为不适用，×为不符合要求，△为不适用，/为此项空白'
            ])
        )


if __name__ == "__main__":
    unittest.main()
