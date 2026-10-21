from __future__ import annotations

import unittest

from mvp.full_record_202 import _is_v_tick, _resolved_record_symbol


def line(a, b, n=6):
    return [(a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n) for i in range(n + 1)]


def ink(points):
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    return {"bbox": [min(xs), min(ys), max(xs), max(ys)], "_strokes": [points], "_points": points,
            "classified_symbol": "其他", "stroke_count": 1}


class VTickTests(unittest.TestCase):
    V = line((0, 0), (8, 19)) + line((8, 19), (16, 0))[1:]

    def test_symmetric_v_and_check_mark_are_ticks(self) -> None:
        self.assertTrue(_is_v_tick(self.V))
        check = line((0, 10), (5, 18)) + line((5, 18), (20, 0))[1:]
        self.assertTrue(_is_v_tick(check))

    def test_lines_loops_and_inverted_v_are_not_ticks(self) -> None:
        self.assertFalse(_is_v_tick(line((0, 0), (20, 20))))
        loop = line((0, 0), (10, 0)) + line((10, 0), (10, 10))[1:] + line((10, 10), (0, 10))[1:] + line((0, 10), (0, 1))[1:]
        self.assertFalse(_is_v_tick(loop))
        caret = line((0, 19), (8, 0)) + line((8, 0), (16, 19))[1:]
        self.assertFalse(_is_v_tick(caret))

    def test_single_small_v_resolves_to_check(self) -> None:
        self.assertEqual(_resolved_record_symbol([ink(self.V)]), ("√", None))
        tiny = [(x / 3, y / 3) for x, y in self.V]
        self.assertEqual(_resolved_record_symbol([ink(tiny)])[0], None)


if __name__ == "__main__":
    unittest.main()
