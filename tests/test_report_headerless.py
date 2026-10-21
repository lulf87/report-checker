from __future__ import annotations

import unittest

try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover
    import fitz

from mvp.checker import (
    _page_ranges,
    _r07_message,
    cover_value_after_label,
    report_page_tables,
    scan_report_items,
)

COLUMNS = [45, 72, 126, 176, 399, 453, 495, 548]
HEADER = ["序号", "检验项目", "标准条款", "标准要求", "检验结果", "单项结论", "备注"]


def _table(page: fitz.Page, top: float, rows: list[list[str]]) -> None:
    height = 28
    shape = page.new_shape()
    bottom = top + height * len(rows)
    for x in COLUMNS:
        shape.draw_line((x, top), (x, bottom))
    for index in range(len(rows) + 1):
        shape.draw_line((COLUMNS[0], top + index * height), (COLUMNS[-1], top + index * height))
    shape.finish(color=(0, 0, 0), width=0.6)
    shape.commit()
    for row_index, row in enumerate(rows):
        for column, text in enumerate(row):
            if text:
                page.insert_text(
                    (COLUMNS[column] + 2, top + row_index * height + 16),
                    text,
                    fontname="china-s",
                    fontsize=8,
                )


def _report(headerless_second_page: bool) -> fitz.Document:
    document = fitz.open()
    first = document.new_page(width=595, height=842)
    first.insert_text((450, 160), "共 2 页 第 1 页", fontname="china-s", fontsize=8)
    _table(first, 175, [HEADER, ["1", "项目一", "4.1", "要求一", "符合要求", "符合", "/"], ["2", "项目二", "4.2", "要求二", "——", "/", "/"]])
    second = document.new_page(width=595, height=842)
    second.insert_text((450, 160), "共 2 页 第 2 页", fontname="china-s", fontsize=8)
    rows = [["3", "项目三", "4.3", "要求三", "符合要求", "符合", "/"], ["4", "项目四", "4.4", "要求四", "——", "/", "/"]]
    _table(second, 175, rows if headerless_second_page else [HEADER, *rows])
    return document


class HeaderlessTableTests(unittest.TestCase):
    def test_headerless_page_is_read_and_reported(self) -> None:
        document = _report(headerless_second_page=True)
        tables = report_page_tables(document[1])
        self.assertTrue(any(getattr(table, "headerless", False) for table in tables))
        items, diagnostics = scan_report_items(document)
        self.assertEqual(sorted(items), [1, 2, 3, 4])
        self.assertEqual([entry["pdf_page"] for entry in diagnostics["headerless_pages"]], [2])
        self.assertEqual(diagnostics["headerless_pages"][0]["printed_page"], 2)
        self.assertEqual(items[3]["conclusions"], ["符合"])

    def test_pages_with_header_are_unchanged(self) -> None:
        document = _report(headerless_second_page=False)
        items, diagnostics = scan_report_items(document)
        self.assertEqual(sorted(items), [1, 2, 3, 4])
        self.assertEqual(diagnostics["headerless_pages"], [])

    def test_other_tables_are_not_adopted(self) -> None:
        document = _report(headerless_second_page=True)
        page = document.new_page(width=595, height=842)
        shape = page.new_shape()
        for x in (68, 114, 251, 342, 447, 525):
            shape.draw_line((x, 175), (x, 260))
        for y in (175, 203, 231, 260):
            shape.draw_line((68, y), (525, y))
        shape.finish(color=(0, 0, 0), width=0.6)
        shape.commit()
        page.insert_text((70, 191), "32", fontname="china-s", fontsize=8)
        self.assertFalse(any(getattr(table, "headerless", False) for table in report_page_tables(page)))


class ReportMessageTests(unittest.TestCase):
    def test_messages_say_what_is_wrong(self) -> None:
        self.assertIn("实际写“/”", _r07_message("符合", "/", ["——", "符合要求"], False, []))
        conflict = _r07_message(
            "符合",
            "符合",
            ["符合要求", "——"],
            True,
            [
                {"pdf_page": 66, "printed_page": 64, "conclusion": "符合"},
                {"pdf_page": 67, "printed_page": 65, "conclusion": "/"},
            ],
        )
        self.assertIn("第65页（PDF第67页）写“/”", conflict)
        self.assertIn("检验结果全部为", _r07_message("/", "符合", ["——", "——"], False, []))

    def test_page_ranges(self) -> None:
        self.assertEqual(_page_ranges([88, 89, 90, 95, 97, 98]), "88～90、95、97～98")


class CoverWrapTests(unittest.TestCase):
    def test_wrapped_cover_value_is_joined(self) -> None:
        document = fitz.open()
        page = document.new_page(width=595, height=842)
        page.insert_text((140, 450), "委 托 方 上海某某医疗机器人（集团）股份有限公", fontname="china-s", fontsize=12)
        page.insert_text((360, 482), "司", fontname="china-s", fontsize=12)
        page.insert_text((140, 516), "样品名称 胸腹腔内窥镜手术系统", fontname="china-s", fontsize=12)
        value, _ = cover_value_after_label(page, "委托方")
        self.assertEqual(value, "上海某某医疗机器人（集团）股份有限公司")
        value, _ = cover_value_after_label(page, "样品名称")
        self.assertEqual(value, "胸腹腔内窥镜手术系统")


if __name__ == "__main__":
    unittest.main()
