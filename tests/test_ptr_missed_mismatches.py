"""Differences that used to stay “待确认” although the answer is “不一致”."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mvp.ptr_compare import (
    _drop_copied_table_note,
    figure_caption_differences,
    ptr_tagged_notes,
)
from mvp.ptr_table_rows import _few_characters_different

try:
    import pymupdf
except ImportError:  # pragma: no cover - compatibility with older environments
    pymupdf = None


def _diff(ptr: str, report: str) -> list[dict[str, str]]:
    from difflib import SequenceMatcher

    return [
        {"op": op, "ptr": ptr[a:b], "report": report[c:d]}
        for op, a, b, c, d in SequenceMatcher(None, ptr, report, autojunk=False).get_opcodes()
        if op != "equal"
    ]


class FigureCaptionTests(unittest.TestCase):
    PTR = "输出波形图和波形参数\n输出波形图见图1，波形参数应满足表6 的要求。\n脉冲宽度\n图1 消融仪MODE 3 预设输出波形图"

    def test_caption_with_words_left_out(self) -> None:
        found = figure_caption_differences(self.PTR, "输出波形图见图1，波形参数应满足表6 的要求。\n图1 是消融仪输出波形图。")
        self.assertEqual(found, [{"figure": "图1", "ptr": "消融仪MODE 3 预设输出波形图", "report": "消融仪输出波形图"}])

    def test_same_caption_is_not_reported(self) -> None:
        self.assertEqual(figure_caption_differences(self.PTR, "图1 消融仪MODE 3 预设输出波形图"), [])
        self.assertEqual(figure_caption_differences(self.PTR, "图1 是消融仪MODE3预设输出波形图。"), [])

    def test_wrapped_caption_is_left_for_the_text_rules(self) -> None:
        # Only the first line of a wrapped caption: shortened or wrapped cannot be told apart.
        self.assertEqual(figure_caption_differences(self.PTR, "图1 消融仪MODE 3\n预设输出波形图"), [])

    def test_other_figure_or_no_caption(self) -> None:
        self.assertEqual(figure_caption_differences(self.PTR, "图2 手柄示意图"), [])
        self.assertEqual(figure_caption_differences(self.PTR, "输出波形图见图1。"), [])
        # A PTR sentence about the figure is not its caption.
        self.assertEqual(figure_caption_differences("图1 所示为波形", "图1 手柄示意图"), [])


class FewCharactersTests(unittest.TestCase):
    PTR = "DDD、DDDR、DDD/DDIR、ModeA、ModeA-R、ModeB-R、0ModeB/DDIR、DDTA"

    def test_one_character_dropped_from_a_long_cell(self) -> None:
        report = self.PTR.replace("0ModeB", "ModeB")
        self.assertTrue(_few_characters_different(self.PTR, report, _diff(self.PTR, report)))

    def test_longer_difference_stays_for_review(self) -> None:
        report = self.PTR.replace("、DDTA", "")
        self.assertFalse(_few_characters_different(self.PTR, report, _diff(self.PTR, report)))

    def test_short_cell_stays_for_review(self) -> None:
        self.assertFalse(_few_characters_different("±15ms", "±10ms", _diff("±15ms", "±10ms")))

    def test_equal_cells_are_not_a_difference(self) -> None:
        self.assertFalse(_few_characters_different(self.PTR, self.PTR, []))


class CopiedTableNoteTests(unittest.TestCase):
    NOTES = {"逸搏间期": ["[逸搏间期]逸搏间期本身不可程控逸搏间期=60000/基本频率"]}

    def test_note_of_the_row_copied_over_two_lines(self) -> None:
        text = "逸搏间期\n符合表2-1 规定的要求。\n[逸搏间期]逸搏间期本身不可程控，逸搏间期\n=60000/基本频率。"
        reduced, note = _drop_copied_table_note(text, "逸搏间期", self.NOTES)
        self.assertEqual(reduced, "逸搏间期\n符合表2-1 规定的要求。")
        self.assertEqual(len(note), 2)

    def test_changed_note_is_kept(self) -> None:
        text = "符合表2-1 规定的要求。\n[逸搏间期]逸搏间期本身可程控，逸搏间期\n=60000/基本频率。"
        self.assertEqual(_drop_copied_table_note(text, "逸搏间期", self.NOTES), (text, []))

    def test_note_of_another_row_is_kept(self) -> None:
        text = "符合表2-1 规定的要求。\n[逸搏间期]逸搏间期本身不可程控，逸搏间期=60000/基本频率。"
        self.assertEqual(_drop_copied_table_note(text, "基本频率", self.NOTES), (text, []))

    def test_text_after_the_note_is_kept(self) -> None:
        text = "[逸搏间期]逸搏间期本身不可程控，逸搏间期=60000/基本频率。\n另行约定的要求"
        reduced, _ = _drop_copied_table_note(text, "逸搏间期", self.NOTES)
        self.assertEqual(reduced, "另行约定的要求")

    @unittest.skipUnless(pymupdf, "PyMuPDF is not installed")
    def test_notes_are_read_from_the_ptr_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ptr.pdf"
            document = pymupdf.open()
            page = document.new_page()
            lines = ["[Sensitivity] auto is not a setting,", "it is shown on the button.", "[Interval] fixed, 60000/rate.", "2.1.1 Rate"]
            for index, line in enumerate(lines):
                page.insert_text((72, 72 + 20 * index), line)
            document.save(path)
            document.close()
            notes = ptr_tagged_notes(path)
        # A note runs over wrapped lines and ends at the next note or clause.
        self.assertEqual(
            notes,
            {
                "Sensitivity": ["[Sensitivity]autoisnotasettingitisshownonthebutton"],
                "Interval": ["[Interval]fixed60000/rate"],
            },
        )

if __name__ == "__main__":
    unittest.main()
