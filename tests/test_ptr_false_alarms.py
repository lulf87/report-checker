"""Regression tests for parsing problems that produced false 不一致 results."""

from __future__ import annotations

import unittest

from mvp.ptr_compare import (
    _numeric_signature,
    _report_parent_title_equivalence,
    _unit_labels_match_numeric,
    normalize_comparison_text,
)
from mvp.ptr_layout import report_clause_cell


class ClauseCellTests(unittest.TestCase):
    def test_letter_on_its_own_line(self) -> None:
        self.assertEqual(report_clause_cell("2.3.4.3 \nc)"), "2.3.4.3c")
        self.assertEqual(report_clause_cell("2.1.13 \nb）"), "2.1.13b")

    def test_wrapped_long_number(self) -> None:
        self.assertEqual(report_clause_cell("2.3.13.1\n2 a)"), "2.3.13.12a")

    def test_plain_clause_is_unchanged(self) -> None:
        self.assertEqual(report_clause_cell("2.3.5.1"), "2.3.5.1")
        self.assertEqual(report_clause_cell("2.6"), "2.6")


class NumericUnitTests(unittest.TestCase):
    def test_prefix_split_from_ohm(self) -> None:
        self.assertEqual(_numeric_signature("绝缘电阻应大于5M Ω。"), _numeric_signature("绝缘电阻应大于5MΩ。"))

    def test_micro_prefix_split_by_line_break(self) -> None:
        self.assertEqual(_numeric_signature("应不超过10μ\ng/g。"), _numeric_signature("应不超过10μg/g。"))

    def test_scientific_notation_with_lost_superscript(self) -> None:
        self.assertEqual(_numeric_signature("能承受3.6×103 Pa 压强"), [("", "3600", "Pa")])
        self.assertEqual(_numeric_signature("能承受 3.6×103Pa 压强"), [("", "3600", "Pa")])
        self.assertNotEqual(_numeric_signature("3.6×104 Pa"), _numeric_signature("3.6×103 Pa"))

    def test_plain_multiplication_is_not_scientific(self) -> None:
        self.assertEqual(_numeric_signature("放大 2 x 10 倍"), [("", "2", "x"), ("", "10", "")])


class UnitLabelTests(unittest.TestCase):
    def test_label_for_values_not_in_text_is_for_review(self) -> None:
        # ``单位：°`` belongs to an angle table; ``单位：mm`` matches ±1 mm.
        self.assertEqual(_unit_labels_match_numeric(["mm", "°"], ["mm"], peak_context=False),
                         (False, "UNIT_LABEL_WITHOUT_PRINTED_VALUE"))

    def test_lone_wrong_label_is_still_a_contradiction(self) -> None:
        self.assertEqual(_unit_labels_match_numeric(["mm"], ["°"], peak_context=False),
                         (False, "UNIT_LABEL_CONTRADICTS_NUMERIC"))

    def test_scale_mismatch_is_still_reported(self) -> None:
        self.assertEqual(_unit_labels_match_numeric(["GΩ", "mm"], ["MΩ", "mm"], peak_context=False),
                         (False, "UNIT_SCALE_MISMATCH"))


class ParentTitleTests(unittest.TestCase):
    context = {"ptr_title_by_clause": {"2.1.13": "耐腐蚀性能"}}

    def check(self, ptr: str, report: str):
        return _report_parent_title_equivalence(
            {"clause_id": "2.1.13b"}, normalize_comparison_text(ptr), normalize_comparison_text(report), self.context
        )

    def test_parent_title_and_letter_before_item(self) -> None:
        self.assertEqual(
            self.check("其余材料按照说明书中指定的方法进行消毒和灭菌后，应无腐蚀现象。",
                       "耐腐蚀性能\nb）其余材料按照说明书中指定的方法进行消毒和灭菌后，应无腐蚀现象。"),
            ["REPORT_PARENT_TITLE", "PUNCTUATION_ONLY"],
        )

    def test_other_title_or_changed_text_is_not_accepted(self) -> None:
        self.assertIsNone(self.check("其余材料应无腐蚀现象。", "耐腐蚀要求\n其余材料应无腐蚀现象。"))
        self.assertIsNone(self.check("其余材料应无腐蚀现象。", "耐腐蚀性能\n其余材料应无明显腐蚀现象。"))


if __name__ == "__main__":
    unittest.main()
