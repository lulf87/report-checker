from __future__ import annotations

import unittest
from pathlib import Path

from mvp.checker import _report_r08_finding

ROOT = Path(__file__).resolve().parents[1]


def _item(sequence: int) -> dict:
    return {
        "project": "项目",
        "clause": "2.2",
        "requirements": ["要求"],
        "results": ["符合要求"],
        "conclusions": ["符合"],
        "pages": [33],
        "row_locations": [],
    }


class R08FormatDefectTests(unittest.TestCase):
    def test_defects_make_the_item_an_error(self) -> None:
        defects = {
            48: [{"kind": "conclusion_cell_not_covering_item", "message": "序号48……", "location": {"pdf_page": 33, "rect": [454, 216, 495, 627]}}],
            42: [{"kind": "numeric_result_without_unit", "message": "序号42……", "location": {"pdf_page": 31, "rect": [399, 479, 453, 507]}}],
        }
        finding = _report_r08_finding({42: _item(42), 48: _item(48), 49: _item(49)}, defects)
        self.assertEqual(finding["status"], "error")
        statuses = {item["sequence"]: item["status"] for item in finding["details"]["items"]}
        self.assertEqual(statuses, {42: "error", 48: "error", 49: "pass"})
        self.assertIn("单项结论未覆盖整个序号：序号48", finding["summary"])
        self.assertIn("缺少“单位：”：序号42", finding["summary"])

    def test_no_defects_keeps_the_old_summary(self) -> None:
        finding = _report_r08_finding({1: _item(1)}, {})
        self.assertEqual(finding["status"], "pass")
        self.assertEqual(finding["details"]["format_defects"], [])


SAMPLE_0483 = Path("/home/claude/s0483/report.pdf")


@unittest.skipUnless(SAMPLE_0483.exists(), "private sample 0483 is not available")
class Sample0483Tests(unittest.TestCase):
    def test_conclusion_cells_and_missing_units(self) -> None:
        from mvp.report_format_rules import report_format_defects

        defects = report_format_defects(SAMPLE_0483)
        kinds = {(sequence, item["kind"]) for sequence, items in defects.items() for item in items}
        self.assertIn((48, "conclusion_cell_not_covering_item"), kinds)
        self.assertIn((51, "conclusion_cell_not_covering_item"), kinds)
        self.assertIn((42, "numeric_result_without_unit"), kinds)
        # 序号 50/51 的“单位：mV”写在上一行（允差行），视为已写单位。
        self.assertNotIn((51, "numeric_result_without_unit"), kinds)
        self.assertNotIn((49, "conclusion_cell_not_covering_item"), kinds)
        # 计数结果“1269 个”豁免（用户 2026-10-23）。
        self.assertNotIn((10, "numeric_result_without_unit"), kinds)


if __name__ == "__main__":
    unittest.main()
