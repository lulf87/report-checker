"""A Report clause that prints the clauses it refers to (``并符合2.1.4 及2.6.1
的要求`` followed by 2.1.4 and 2.6.1 in full)."""

from __future__ import annotations

import unittest

from mvp.ptr_compare import _split_expanded_references
from mvp.ptr_layout import _expanding_referrer


def _node(title: str, *parts: str) -> dict:
    return {"title": title, "parts": [title, *parts]}


class ExpandingReferrerTests(unittest.TestCase):
    def test_repeated_heading_belongs_to_the_later_clause_that_refers_to_it(self) -> None:
        nodes = {
            "2.1.6": _node("操控性"),
            "2.1.6.1": _node("", "推拉手柄时，应调控自如。"),
            "2.1.6.2": _node("", "旋转手柄时，应调控自如。"),
            "2.1.7": _node("弯曲疲劳", "外观应无断裂，并符合2.1.6 及2.3.1 的要求。"),
        }
        self.assertIs(_expanding_referrer(nodes, "2.1.6"), nodes["2.1.7"])
        self.assertIs(_expanding_referrer(nodes, "2.1.6.2"), nodes["2.1.7"])

    def test_ordinary_repeated_heading(self) -> None:
        nodes = {"2.1.6": _node("操控性"), "2.1.7": _node("弯曲疲劳", "外观应无断裂。")}
        self.assertIsNone(_expanding_referrer(nodes, "2.1.6"))

    def test_reference_made_before_the_clause_is_not_an_expansion(self) -> None:
        # 2.1.5 refers to 2.1.6, which is then printed for the first time and repeated.
        nodes = {"2.1.5": _node("总则", "应符合2.1.6 的要求。"), "2.1.6": _node("操控性")}
        self.assertIsNone(_expanding_referrer(nodes, "2.1.6"))

    def test_new_clause_and_own_children_are_not_expansions(self) -> None:
        nodes = {"2.1.6": _node("操控性"), "2.1.6.1": _node("", "见2.1.6 的总则。")}
        self.assertIsNone(_expanding_referrer(nodes, "2.1.8"))
        self.assertIsNone(_expanding_referrer(nodes, "2.1.6"))


class SplitExpandedReferencesTests(unittest.TestCase):
    KNOWN = {"2.1.4": {}, "2.6.1": {}, "2.5.7": {}, "2.5.7a": {}, "2.9": {}}

    def _split(self, ptr_text: str, report_text: str, clause: str = "2.1.5"):
        return _split_expanded_references(
            {"clause_id": clause, "text": ptr_text}, {"text": report_text}, {"ptr_nodes_by_clause": self.KNOWN}
        )

    def test_two_referenced_clauses_printed_in_full(self) -> None:
        own, blocks = self._split(
            "弯曲疲劳\n外观应无断裂，并符合2.1.4 及2.6.1 的要求。",
            "弯曲疲劳\n外观应无断裂，并符合2.1.4 及2.6.1 的要求。\n2.1.4 操控性\n应调控自如。\n2.6.1 直流电阻\n应不大于10\nΩ。\n单位：Ω",
        )
        self.assertEqual(own, "弯曲疲劳\n外观应无断裂，并符合2.1.4 及2.6.1 的要求。")
        self.assertEqual(blocks, [("2.1.4", "操控性\n应调控自如。"), ("2.6.1", "直流电阻\n应不大于10\nΩ。\n单位：Ω")])

    def test_lettered_item(self) -> None:
        own, blocks = self._split(
            "显示延迟\n应满足 2.5.7.a)的要求。", "显示延迟\n应满足 2.5.7.a)的要求。\n2.5.7 a)平均延迟：≤ 85ms。", "2.13"
        )
        self.assertEqual(blocks, [("2.5.7a", "平均延迟：≤ 85ms。")])

    def test_wrapped_reference_sentence_is_not_a_block(self) -> None:
        self.assertIsNone(self._split("时间差应满足 2.5.7.b)的要求", "时间差应满足\n2.5.7.b)的要求。", "2.13"))
        self.assertIsNone(self._split("应符合2.1.4 的要求。", "应符合\n2.1.4 的要求。"))

    def test_clause_not_referred_to_is_not_a_block(self) -> None:
        self.assertIsNone(self._split("应符合2.1.4 的要求。", "应符合2.1.4 的要求。\n2.9 其他要求\n应清洁。"))

    def test_no_reference_in_the_ptr(self) -> None:
        self.assertIsNone(self._split("外观应无断裂。", "外观应无断裂。\n2.1.4 操控性\n应调控自如。"))


if __name__ == "__main__":
    unittest.main()
