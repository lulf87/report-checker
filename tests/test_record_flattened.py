from __future__ import annotations

import gc
import unittest
from pathlib import Path

try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover
    import fitz

from mvp.record_flattened import adapt_flattened_record, document_has_ink, is_handwriting_drawing


ROOT = Path(__file__).resolve().parents[1]


def _flattened_page() -> fitz.Document:
    """One page shaped like an iOS 27 export: curve-filled strokes, an
    invisible OCR text layer and an inset duplicate of a cell shading."""

    document = fitz.open()
    page = document.new_page(width=595, height=842)
    page.insert_text((100, 100), "Template text", fontname="tiro", fontsize=11)
    page.insert_text((430, 160), "OCR", fontname="cour", fontsize=9, render_mode=3)

    # A tick: closed outline made of Bezier curves, filled black.
    shape = page.new_shape()
    shape.draw_bezier((426, 158), (430, 162), (436, 150), (449, 145))
    shape.draw_bezier((449, 145), (437, 152), (431, 164), (426, 158))
    shape.finish(fill=(0, 0, 0), color=None, closePath=True)
    # A straight stroke drawn with the line tool (0.85 pt).
    shape.draw_line((290, 192), (548, 270))
    shape.finish(color=(0, 0, 0), width=0.85)
    # Cell shading plus an inset copy of the same colour.
    shape.draw_rect(fitz.Rect(99, 520, 548, 546))
    shape.finish(fill=(0.9, 0.9, 0.9), color=None)
    shape.draw_rect(fitz.Rect(104, 520, 543, 546))
    shape.finish(fill=(0.9, 0.9, 0.9), color=None)
    # A printed table rule (0.48 pt) is not handwriting.
    shape.draw_line((51, 600), (548, 600))
    shape.finish(color=(0, 0, 0), width=0.48)
    shape.commit()
    return document


class FlattenedRecordAdapterTests(unittest.TestCase):
    def test_strokes_become_hidden_ink_and_noise_is_removed(self) -> None:
        document = _flattened_page()
        self.assertFalse(document_has_ink(document))
        summary = adapt_flattened_record(document)
        self.assertEqual(summary["handwriting_source"], "page_vector_strokes")
        self.assertEqual(summary["strokes"], 2)
        self.assertEqual(summary["invisible_text_blocks_removed"], 1)
        self.assertEqual(summary["nested_shading_removed"], 1)
        gc.collect()

        page = document[0]
        inks = [annotation for annotation in page.annots() if annotation.type[1] == "Ink"]
        self.assertEqual(len(inks), 2)
        tick = min(inks, key=lambda annotation: annotation.rect.width)
        # The annotation rectangle hugs the stroke (no synthetic padding).
        self.assertLess(abs(tick.rect.x0 - 426), 1.5)
        self.assertLess(abs(tick.rect.x1 - 449), 1.5)
        self.assertTrue(tick.flags & fitz.PDF_ANNOT_IS_HIDDEN)
        self.assertGreaterEqual(sum(len(stroke) for stroke in tick.vertices), 2)

        text = page.get_text()
        self.assertIn("Template text", text)
        self.assertNotIn("OCR", text)
        shading = [
            drawing["rect"]
            for drawing in page.get_drawings()
            if drawing.get("fill") and min(drawing["fill"]) > 0.3
        ]
        self.assertEqual(len(shading), 1)
        self.assertEqual(round(shading[0].x0), 99)

    def test_documents_with_ink_are_left_untouched(self) -> None:
        document = _flattened_page()
        document[0].add_ink_annot([[(10, 10), (20, 20)]])
        before = document[0].get_text()
        summary = adapt_flattened_record(document)
        self.assertEqual(summary["handwriting_source"], "ink_annotations")
        self.assertEqual(document[0].get_text(), before)
        self.assertEqual(len(list(document[0].annots())), 1)

    def test_blank_record_reports_no_handwriting(self) -> None:
        document = fitz.open()
        document.new_page().insert_text((72, 72), "blank", fontname="tiro")
        self.assertEqual(adapt_flattened_record(document)["handwriting_source"], "none")

    def test_template_rules_are_not_handwriting(self) -> None:
        rule = {"type": "s", "width": 0.48, "items": [("l", fitz.Point(0, 0), fitz.Point(10, 0))], "rect": fitz.Rect(0, 0, 10, 0)}
        user_line = dict(rule, width=0.85)
        self.assertFalse(is_handwriting_drawing(rule))
        self.assertTrue(is_handwriting_drawing(user_line))


def _flattened_sample() -> Path | None:
    folder = ROOT / "素材/record/3944/9706.1"
    pdfs = sorted(folder.glob("*.pdf")) if folder.is_dir() else []
    return pdfs[0] if pdfs else None


@unittest.skipUnless(_flattened_sample(), "本地 3944 拼合 Record 素材不存在")
class FlattenedRecord3944Tests(unittest.TestCase):
    def test_status_rows_are_read_from_page_strokes(self) -> None:
        from mvp.full_record_61 import extract_record_61_status_rows

        with fitz.open(_flattened_sample()) as document:
            summary = adapt_flattened_record(document)
            rows, extraction = extract_record_61_status_rows(document)
        self.assertEqual(summary["handwriting_source"], "page_vector_strokes")
        self.assertGreater(summary["strokes"], 6000)
        self.assertGreater(len(rows), 800)
        blank = [row for row in rows if not row.get("status")]
        # 9.6.2.2 (p61) is genuinely left blank in this Record.
        self.assertLessEqual(len(blank), 3)


if __name__ == "__main__":
    unittest.main()
