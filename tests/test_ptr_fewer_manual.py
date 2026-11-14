"""Clauses that used to stay “待确认” although the content can be verified."""

from __future__ import annotations

import unittest

from mvp.ptr_compare import (
    _safe_text_equivalence,
    _drop_copied_table_note,
    _drop_report_result_labels,
    _strip_punctuation,
    _with_model_numbers,
    normalize_comparison_text,
)
from mvp.ptr_layout import _model_number_cells


class SingleResultLabelTests(unittest.TestCase):
    PTR = ["力值准确度", "在±X、±Y 和±Z 方向，测得力和施加力的差值不大于0.4N + 施加力数值的5%。"]

    def test_lone_label_before_the_unit_line(self) -> None:
        lines = [*self.PTR, "±X 方向：", "单位：N"]
        kept, changed = _drop_report_result_labels(lines, self.PTR)
        self.assertTrue(changed)
        self.assertEqual(kept, [*self.PTR, "单位：N"])

    def test_lone_label_at_the_end(self) -> None:
        kept, changed = _drop_report_result_labels([*self.PTR, "±Y 方向："], self.PTR)
        self.assertEqual((kept, changed), (self.PTR, True))

    def test_label_that_is_a_line_of_the_ptr_is_kept(self) -> None:
        ptr = ["尺寸", "a）最大宽度：≤ 8.7 mm。", "b）工作长度："]
        self.assertEqual(_drop_report_result_labels(list(ptr), ptr), (ptr, False))

    def test_label_with_other_words_is_kept(self) -> None:
        lines = [*self.PTR, "斜向：", "单位：N"]
        self.assertEqual(_drop_report_result_labels(list(lines), self.PTR), (lines, False))

    def test_label_followed_by_more_text_is_kept(self) -> None:
        lines = [*self.PTR, "±X 方向：", "另行约定的要求"]
        self.assertEqual(_drop_report_result_labels(list(lines), self.PTR), (lines, False))

    def test_without_the_ptr_lines_nothing_changes(self) -> None:
        lines = [*self.PTR, "±X 方向：", "单位：N"]
        self.assertEqual(_drop_report_result_labels(list(lines)), (lines, False))


class CircledNoteTests(unittest.TestCase):
    # As ``ptr_tagged_notes`` stores it: the PTR prints the note on one line.
    NOTES = {
        "①": [_strip_punctuation(normalize_comparison_text("①逸搏间期：300-2000ms，不可直接程控，公式：60000ms/基础频率。"))]
    }
    TEXT = "逸搏间期应符合表1 中的数值。\n①逸搏间期：300-2000ms，不可直接程控，\n公式：60000ms/基础频率。"

    def test_note_of_the_number_on_the_row_name(self) -> None:
        reduced, note = _drop_copied_table_note(self.TEXT, "逸搏间期", self.NOTES, "逸搏间期(ms)①")
        self.assertEqual(reduced, "逸搏间期应符合表1 中的数值。")
        self.assertEqual(len(note), 2)

    def test_row_without_that_number_keeps_the_note(self) -> None:
        self.assertEqual(_drop_copied_table_note(self.TEXT, "逸搏间期", self.NOTES, "逸搏间期(ms)"), (self.TEXT, []))
        self.assertEqual(_drop_copied_table_note(self.TEXT, "逸搏间期", self.NOTES, "逸搏间期(ms)②"), (self.TEXT, []))

    def test_changed_note_is_kept(self) -> None:
        text = self.TEXT.replace("300-2000", "300-1000")
        self.assertEqual(_drop_copied_table_note(text, "逸搏间期", self.NOTES, "逸搏间期(ms)①"), (text, []))


class LinesMovedTests(unittest.TestCase):
    """2026-11-14 ruling: whole lines printed at another place, nothing
    added, dropped or changed, are 一致."""

    PTR = (
        "扳手尺寸\n杆头截面尺寸应符合以下要求：\n0.88 毫米≤A ≤0.89 毫米\n0.96 毫米≤B ≤1 毫米\n"
        "图2-1 扳手示意图\nB\nA\n注：A. 平口尺寸\nB. 尖端到尖端距离\n图2-2 尺寸测量示意图"
    )
    REPORT = (
        "扳手\n尺寸\n杆头截面尺寸应符合以下要求：\n图2-1 扳手示意图\n注：A. 平口尺寸\nB. 尖端到尖端距离\n"
        "图2-2 尺寸测量示意图\n单位：mm\n0.88 毫米≤A≤0.89 毫米\n0.96 毫米≤B≤1 毫米"
    )

    def test_two_lines_moved_behind_the_figure(self) -> None:
        rules, _ = _safe_text_equivalence(self.PTR, self.REPORT)
        self.assertEqual(rules, ["REPORT_UNIT_LINE", "PTR_FIGURE_LABELS", "REPORT_LINES_MOVED"])

    def test_moved_and_changed_is_not_equivalent(self) -> None:
        self.assertEqual(_safe_text_equivalence(self.PTR, self.REPORT.replace("0.89", "0.98")), (None, False))
        self.assertEqual(_safe_text_equivalence(self.PTR, self.REPORT.replace("尖端到尖端", "尖端")), (None, False))

    def test_moved_with_a_line_left_out_is_not_equivalent(self) -> None:
        report = self.REPORT.replace("\n0.96 毫米≤B≤1 毫米", "")
        self.assertEqual(_safe_text_equivalence(self.PTR, report), (None, False))

    def test_part_of_a_line_moved_is_not_equivalent(self) -> None:
        ptr = "要求\n甲部件应牢固，乙部件应灵活。\n丙部件应清洁。"
        report = "要求\n乙部件应灵活，甲部件应牢固。\n丙部件应清洁。"
        self.assertEqual(_safe_text_equivalence(ptr, report), (None, False))

    def test_whole_lines_moved_without_a_figure(self) -> None:
        ptr = "要求\n甲部件应牢固。\n乙部件应灵活。\n丙部件应清洁。"
        report = "要求\n乙部件应灵活。\n丙部件应清洁。\n甲部件应牢固。"
        self.assertEqual(_safe_text_equivalence(ptr, report)[0], ["REPORT_LINES_MOVED"])


class ModelNumberKeyTests(unittest.TestCase):
    def test_bare_model_number_removed_from_the_text_is_kept_by_column(self) -> None:
        cells = [
            {"text": "", "raw_text": "477049 ", "column_index": 0},
            {"text": "夹钳甲", "raw_text": "夹钳甲", "column_index": 1},
            {"text": "8", "raw_text": "8", "column_index": 2},
        ]
        self.assertEqual(_model_number_cells(cells), {0: "477049"})
        # A value that survived the cleaning, or text that is not a bare number, is not a key.
        self.assertEqual(_model_number_cells([{"text": "30", "raw_text": "30", "column_index": 2}]), {})
        self.assertEqual(_model_number_cells([{"text": "", "raw_text": "左路", "column_index": 0}]), {})

    def test_numbers_are_put_back_only_into_empty_cells(self) -> None:
        report = {
            "text": "夹持力",
            "table_rows": [
                {"matrix_row": ["型号", "名称", "夹持力"], "rows": [["型号", "名称", "夹持力"]]},
                {"matrix_row": ["", "夹钳甲", "8"], "rows": [["", "夹钳甲", "8"]], "model_number_cells": {0: "477049"}},
            ],
        }
        keyed = _with_model_numbers(report)
        self.assertEqual(keyed["table_rows"][1]["matrix_row"], ["477049", "夹钳甲", "8"])
        self.assertEqual(keyed["table_rows"][1]["rows"], [["477049", "夹钳甲", "8"]])
        # The original node is untouched: it is the fallback comparison.
        self.assertEqual(report["table_rows"][1]["matrix_row"], ["", "夹钳甲", "8"])

    def test_node_without_model_numbers(self) -> None:
        self.assertIsNone(_with_model_numbers({"table_rows": [{"matrix_row": ["a", "b"]}]}))
        self.assertIsNone(_with_model_numbers({"text": "x"}))


if __name__ == "__main__":
    unittest.main()
