from __future__ import annotations

import unittest

try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover
    import fitz

from mvp.full_record_202 import (
    _compare_record_to_acceptance,
    _is_hooked_v_tick,
    _number_field_line,
    _record_number_state,
    _resolved_record_symbol,
    accept_handwritten_number,
)


def line(a, b, n=6):
    return [(a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n) for i in range(n + 1)]


def ink(points, symbol="其他", runner_up=0):
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    return {
        "bbox": [min(xs), min(ys), max(xs), max(ys)],
        "_strokes": [points],
        "_points": points,
        "classified_symbol": symbol,
        "stroke_count": 1,
        "runner_up_point_count": runner_up,
    }


# A narrow tick as written in sample 3944: a small hook where the pen lands,
# a 51 degree V, and a tiny hook where it lifts.
HOOKED_TICK = (
    [(401.3, 541.9), (400.8, 541.0), (400.5, 540.2), (400.5, 539.9)]
    + line((400.5, 539.9), (407.4, 553.3))[1:]
    + line((407.4, 553.3), (416.8, 532.5))[1:]
    + [(416.3, 533.0), (415.8, 533.8)]
)


class HookedTickTests(unittest.TestCase):
    def test_tick_with_pen_hooks_is_recognised(self) -> None:
        self.assertTrue(_is_hooked_v_tick(HOOKED_TICK))
        self.assertEqual(_resolved_record_symbol([ink(HOOKED_TICK)]), ("√", None))

    def test_letters_and_lines_are_not_ticks(self) -> None:
        four = line((10, 0), (0, 14)) + line((0, 14), (14, 14))[1:]
        self.assertFalse(_is_hooked_v_tick(four))
        self.assertFalse(_is_hooked_v_tick(line((0, 0), (20, 20))))
        caret = line((0, 19), (8, 0)) + line((8, 0), (16, 19))[1:]
        self.assertFalse(_is_hooked_v_tick(caret))
        w = line((0, 0), (4, 16)) + line((4, 16), (8, 4))[1:] + line((8, 4), (12, 16))[1:] + line((12, 16), (16, 0))[1:]
        self.assertFalse(_is_hooked_v_tick(w))

    def test_tick_beside_handwritten_text(self) -> None:
        p_stem = ink(line((380, 535), (381, 552)))
        self.assertEqual(_resolved_record_symbol([p_stem, ink(HOOKED_TICK)]), ("√", None))

    def test_stray_stroke_into_next_row_is_ignored(self) -> None:
        stray_tick = line((372, 245), (378, 262)) + line((378, 262), (388, 243))[1:]
        candidates = [ink(stray_tick, runner_up=12), ink(HOOKED_TICK)]
        self.assertEqual(_resolved_record_symbol(candidates), ("√", None))

    def test_two_ticks_inside_the_row_stay_manual(self) -> None:
        other = [(x + 30, y) for x, y in HOOKED_TICK]
        self.assertIsNone(_resolved_record_symbol([ink(HOOKED_TICK), ink(other)])[0])


class HandwrittenNumberTests(unittest.TestCase):
    def test_two_agreeing_channels_are_accepted(self) -> None:
        self.assertEqual(
            accept_handwritten_number({"apple_vision": ["68.2"], "tesseract": [], "ios": ["68.2"]}),
            ("68.2", "handwriting_two_channel_agreement"),
        )

    def test_third_channel_cannot_veto_two_agreeing(self) -> None:
        value, _ = accept_handwritten_number({"apple_vision": ["42.4"], "tesseract": ["8"], "ios": ["42.4"]})
        self.assertEqual(value, "42.4")

    def test_single_channel_or_disagreement_stays_manual(self) -> None:
        self.assertIsNone(accept_handwritten_number({"apple_vision": ["18"], "tesseract": [], "ios": []})[0])
        self.assertEqual(
            accept_handwritten_number({"apple_vision": ["18"], "ios": ["8"]}),
            (None, "handwriting_channels_disagree"),
        )

    def test_channel_with_several_numbers_abstains(self) -> None:
        self.assertIsNone(
            accept_handwritten_number({"apple_vision": ["4.24", "100", "24.5"], "ios": ["24.5"]})[0]
        )

    def test_a_weighted_level_meets_db_limit(self) -> None:
        requirement = "声源产生的声级至少应为 65dB（A 计权）。 单位：dB(A)"
        self.assertEqual(_compare_record_to_acceptance("68.2", requirement)["decision"], "match")
        self.assertEqual(_compare_record_to_acceptance("62.1", requirement)["decision"], "mismatch")


class RecordNumberFieldTests(unittest.TestCase):
    def _page(self, fill: bool) -> fitz.Page:
        document = fitz.open()
        page = document.new_page(width=595, height=842)
        page.insert_text((360, 124), "编号：国医检(设)字QW", fontname="china-s", fontsize=10.5)
        page.insert_text((490, 124), "第", fontname="china-s", fontsize=10.5)
        page.insert_text((530, 124), "号", fontname="china-s", fontsize=10.5)
        page.insert_text((60, 200), "检验项目", fontname="china-s", fontsize=10.5)
        if fill:
            # Written after the template, so it comes last in reading order.
            page.insert_text((474, 124), "2024", fontname="tiro", fontsize=10.5)
            page.insert_text((508, 124), "3944", fontname="tiro", fontsize=10.5)
        self._document = document
        return page

    def test_number_filled_as_separate_text_is_read(self) -> None:
        page = self._page(fill=True)
        text, bbox = _number_field_line(page)
        self.assertIn("QW2024第3944号", text)
        self.assertIsNotNone(bbox)
        state, _, _ = _record_number_state(page)
        self.assertEqual(state["number"], (2024, 3944))

    def test_blank_number_is_still_reported(self) -> None:
        state, _, _ = _record_number_state(self._page(fill=False))
        self.assertEqual(state["state"], "explicit_missing")


if __name__ == "__main__":
    unittest.main()
