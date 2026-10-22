from __future__ import annotations

import unittest

from mvp.handwriting_glyphs import classify_stroke, read_stroke_glyphs

CELL = (0.0, 0.0, 60.0, 20.0)


def shifted(points, dx=0.0, dy=0.0):
    return [(x + dx, y + dy) for x, y in points]


# Shapes traced from the 1998 Record (contact-current cells written "<1").
ANGLE_LT = [(6.4, 0.3), (6.0, 0.6), (3.7, 3.6), (0.6, 7.5), (0.1, 8.9), (1.9, 9.3), (4.2, 8.9), (7.3, 7.9), (9.9, 7.3), (12.5, 7.0), (14.3, 9.6)]
OPEN_LT = [(10.0, 1.0), (6.0, 3.5), (1.0, 6.0), (6.0, 8.5), (10.0, 11.0)]
ONE = [(0.5, 0.0), (0.6, 4.0), (0.8, 8.0), (1.0, 12.0)]
HOOKED_ONE = [(0.0, 2.0), (2.0, 0.0), (2.6, 3.0), (2.8, 7.0), (3.0, 11.6)]
ZERO = [(4.0, 0.0), (1.0, 2.0), (0.0, 6.0), (1.0, 10.0), (4.0, 12.0), (7.0, 10.0), (8.0, 6.0), (7.0, 2.0), (4.3, 0.2)]
TWO = [(0.5, 2.0), (3.0, 0.0), (6.5, 1.0), (7.0, 4.0), (4.0, 8.0), (0.0, 12.0), (4.0, 12.0), (8.0, 12.0)]
DOT = [(0.0, 0.0), (0.5, 0.6)]
FLAGGED_ONE = [(3.1, 0.3), (2.2, 0.2), (1.2, 0.0), (0.5, 0.0), (0.1, 0.4), (0.1, 1.4), (0.3, 2.5), (0.7, 3.8), (0.9, 4.7), (1.2, 6.6), (1.5, 8.6)]


class StrokeClassificationTests(unittest.TestCase):
    def test_glyphs(self) -> None:
        self.assertEqual(classify_stroke(ANGLE_LT, 20.0), "<")
        self.assertEqual(classify_stroke(OPEN_LT, 20.0), "<")
        self.assertEqual(classify_stroke(ONE, 20.0), "1")
        self.assertEqual(classify_stroke(HOOKED_ONE, 20.0), "1")
        self.assertEqual(classify_stroke(FLAGGED_ONE, 20.0), "1")
        self.assertEqual(classify_stroke(ZERO, 20.0), "0")
        self.assertEqual(classify_stroke(DOT, 20.0), ".")

    def test_other_digits_are_not_guessed(self) -> None:
        self.assertIsNone(classify_stroke(TWO, 20.0))


class CellReadingTests(unittest.TestCase):
    def test_less_than_one(self) -> None:
        reading = read_stroke_glyphs([shifted(ANGLE_LT, 5, 4), shifted(ONE, 24, 4)], CELL)
        self.assertEqual(reading["value"], "<1")

    def test_order_is_left_to_right(self) -> None:
        reading = read_stroke_glyphs([shifted(ZERO, 30, 4), shifted(ONE, 20, 4), shifted(OPEN_LT, 4, 4)], CELL)
        self.assertEqual(reading["value"], "<10")

    def test_decimal_point_between_digits_and_specks_elsewhere(self) -> None:
        reading = read_stroke_glyphs([shifted(ZERO, 4, 4), shifted(DOT, 14, 15), shifted(ONE, 20, 4)], CELL)
        self.assertEqual(reading["value"], "0.1")
        reading = read_stroke_glyphs([shifted(DOT, 1, 1), shifted(ANGLE_LT, 5, 4), shifted(ONE, 24, 4), shifted(DOT, 30, 16)], CELL)
        self.assertEqual(reading["value"], "<1")

    def test_any_unknown_stroke_means_no_reading(self) -> None:
        reading = read_stroke_glyphs([shifted(TWO, 4, 4), shifted(ONE, 20, 4)], CELL)
        self.assertIsNone(reading["value"])
        self.assertIsNone(read_stroke_glyphs([], CELL))


if __name__ == "__main__":
    unittest.main()
