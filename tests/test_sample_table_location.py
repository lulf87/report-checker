from __future__ import annotations

import unittest

import fitz

from mvp.full_report_photo import parse_sample_description


def _table(page: fitz.Page, top: float, rows: list[list[str]]) -> float:
    widths = [40, 150, 90, 110, 90]
    height = 22
    x0 = 50
    for r, row in enumerate(rows):
        y = top + r * height
        x = x0
        for c, text in enumerate(row):
            page.draw_rect(fitz.Rect(x, y, x + widths[c], y + height), color=(0, 0, 0), width=0.8)
            page.insert_text((x + 3, y + 15), text, fontname="china-s", fontsize=9)
            x += widths[c]
    return top + len(rows) * height


class SampleTableLocationTests(unittest.TestCase):
    """Sample 1998: the auxiliary-equipment table must not replace the sample table."""

    def build(self) -> fitz.Document:
        document = fitz.open()
        page = document.new_page(width=595, height=842)
        page.insert_text((50, 80), "样品描述", fontname="china-s", fontsize=11)
        _table(page, 100, [
            ["序号", "名称", "型号", "批号/生产编号", "生产日期"],
            ["1", "导管接口单元", "CIU", "26032501", "2026-03-25"],
            ["2", "能量发生单元", "GEN", "26032502", "2026-03-25"],
        ])
        page = document.new_page(width=595, height=842)
        page.insert_text((50, 80), "型号规格或其他说明", fontname="china-s", fontsize=11)
        page.insert_text((50, 110), "5、本次检验，配合检测的设备有：", fontname="china-s", fontsize=9)
        _table(page, 130, [
            ["序号", "名称", "型号", "批号/序列号", "制造商"],
            ["1", "消融导管", "316JJ2025", "WTYSP31626", "某公司"],
        ])
        return document

    def test_sample_table_is_the_one_under_sample_description(self) -> None:
        rows, diagnostics = parse_sample_description(self.build())
        self.assertEqual(diagnostics, [])
        self.assertEqual([row["fields"]["部件名称"] for row in rows], ["导管接口单元", "能量发生单元"])
        self.assertEqual(rows[0]["fields"]["序列号/批号"], "26032501")
        self.assertEqual({row["pdf_page"] for row in rows}, {1})

    def test_auxiliary_equipment_table_alone_is_not_a_sample_table(self) -> None:
        document = self.build()
        document.delete_page(0)
        rows, diagnostics = parse_sample_description(document)
        self.assertEqual(rows, [])
        self.assertIn("未找到可可靠解析的样品描述表", diagnostics)


if __name__ == "__main__":
    unittest.main()
