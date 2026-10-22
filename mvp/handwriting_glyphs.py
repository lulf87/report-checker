"""Read very simple handwritten numbers from pen strokes.

Inspectors often write a below-range reading as "<1" (for example contact
leakage current below the instrument's resolution).  OCR engines read
neither the hand-drawn "<" nor a lone "1" stroke reliably, yet both are
unambiguous in stroke geometry:

* ``<``  one stroke whose leftmost point is a corner in the middle of the
  stroke, with both ends well to the right of it, one above and one below
  (or level with) the corner;
* ``1``  one nearly straight, steep stroke;
* ``0``  one closed loop;
* ``.``  a tiny stroke (between digits; elsewhere a pen touch).

Anything else is left unread.  A reading is produced only when *every*
stroke in the cell is one of these glyphs, so "<1", "1", "10", "<10" or
"0.1" can be read, while "4.21" (containing 4 and 2) is left to OCR.

This is a single reading channel.  Callers accept it only when it agrees
with an OCR channel or when it reproduces the Report value exactly.
"""

from __future__ import annotations

import math
import re
from typing import Any, Iterable, Sequence

Point = tuple[float, float]

READER_VERSION = "1.0.0"
_VALUE_RE = re.compile(r"^<?\d+(?:\.\d+)?$")


def _bbox(points: Sequence[Point]) -> tuple[float, float, float, float]:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def _length(points: Sequence[Point]) -> float:
    return sum(math.dist(points[i], points[i + 1]) for i in range(len(points) - 1))


def _max_deviation(points: Sequence[Point]) -> float:
    (x0, y0), (x1, y1) = points[0], points[-1]
    span = math.dist(points[0], points[-1])
    if span == 0:
        return max((math.dist(points[0], p) for p in points), default=0.0)
    return max(abs((x1 - x0) * (y0 - y) - (x0 - x) * (y1 - y0)) / span for x, y in points)


def classify_stroke(points: Sequence[Point], cell_height: float) -> str | None:
    """Return one of ``<``, ``1``, ``0``, ``.`` or ``None``."""

    if len(points) < 2:
        return None
    x0, y0, x1, y1 = _bbox(points)
    width, height = x1 - x0, y1 - y0
    size = max(width, height)
    if size <= max(1.2, 0.10 * cell_height):
        return "."
    if size <= 0.20 * cell_height:
        # A pen speck: a decimal point between digits, otherwise ignored.
        return "·"
    if height < 0.22 * cell_height:
        return None
    length = _length(points)

    # Closed loop: a 0.
    if (
        math.dist(points[0], points[-1]) <= 0.30 * size
        and width >= 0.35 * height
        and length >= 2.2 * size
    ):
        return "0"

    # Steep straight stroke: a 1.
    deviation = _max_deviation(points)
    if (width <= 0.45 * height and deviation <= 0.22 * height) or (
        width <= 0.40 * height and deviation <= 0.35 * height
    ):
        return "1"

    # Corner on the left with both ends to its right: a <.
    corner_index = min(range(len(points)), key=lambda i: points[i][0])
    travelled = _length(points[: corner_index + 1])
    if 0.15 * length <= travelled <= 0.85 * length and width >= 0.70 * height:
        corner = points[corner_index]
        first, last = points[0], points[-1]
        # Handwritten "<" is often an angle "∠": a short upper leg and a long
        # lower leg along the baseline.
        reach = sorted((first[0] - corner[0], last[0] - corner[0]))
        ends_right = reach[0] >= 0.30 * width and reach[1] >= 0.70 * width
        upper, lower = (first, last) if first[1] <= last[1] else (last, first)
        opens = lower[1] - upper[1] >= 0.35 * height
        corner_between = upper[1] - 0.15 * height <= corner[1] <= lower[1] + 0.35 * height
        legs_straight = (
            _max_deviation(points[: corner_index + 1]) <= 0.25 * size
            and _max_deviation(points[corner_index:]) <= 0.25 * size
        )
        if ends_right and opens and corner_between and legs_straight:
            return "<"
    return None


def read_stroke_glyphs(
    pen_paths: Iterable[Sequence[Point]],
    cell_bbox: Sequence[float],
) -> dict[str, Any] | None:
    """Read the cell's strokes left to right; ``None`` when nothing is read."""

    paths = [list(path) for path in pen_paths if len(path) >= 2]
    if not paths:
        return None
    cell_height = float(cell_bbox[3]) - float(cell_bbox[1])
    glyphs: list[tuple[float, str]] = []
    for path in paths:
        glyph = classify_stroke(path, cell_height)
        if glyph is None:
            return {"value": None, "glyphs": None, "reason": "unrecognised_stroke", "stroke_count": len(paths)}
        x0, _, x1, _ = _bbox(path)
        glyphs.append(((x0 + x1) / 2, glyph))
    glyphs.sort()
    text = "".join(glyph for _, glyph in glyphs)
    # Specks and dots that cannot be decimal points (at either end, or next
    # to "<") are pen touches.
    text = text.strip(".·")
    text = re.sub(r"<[.·]+", "<", text)
    text = text.replace("·", ".")
    if not _VALUE_RE.match(text):
        return {"value": None, "glyphs": text, "reason": "not_a_number", "stroke_count": len(paths)}
    return {"value": text, "glyphs": text, "reason": None, "stroke_count": len(paths), "reader_version": READER_VERSION}
