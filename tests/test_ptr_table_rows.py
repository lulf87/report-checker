from __future__ import annotations

import unittest

from mvp.ptr_compare import _flag_clause_number_shifts
from mvp.ptr_table_rows import compare_row_expansion, report_text_without_expansion

TABLE = {
    "pdf_page": 2,
    "bbox": [0, 0, 100, 100],
    "rows": [
        ["参数", "设置", "标称值", "允差"],
        ["基本频率（min-1）", "30-130 ，步幅为5 ；\n140-170，步幅为10", "60min-1", "±15ms"],
        ["脉冲振幅（心房/右\n心室/左心室）（V）", "0.25-4.0，步幅为0.25", "2.5V", "240 Ω：-38%/+20%\n500 Ω：-27%/+20%"],
        ["心室感知不应期", "125；157；190；220；", "250ms", "±5ms"],
    ],
}


def ptr(title: str) -> dict:
    return {"title": title, "text": f"{title}\n应符合表2-1规定的要求。", "table_rows": [TABLE]}


class RowExpansionTests(unittest.TestCase):
    def test_labelled_values_match_cells(self) -> None:
        report = {"text": "基本频率\n应符合表2-1 规定的要求。\n基本频率（min−1）\n设置：30-130，步幅为5；140-170，步幅为\n10\n标称值：60min−1\n允差：±15ms\n@240Ω\n单位：ms"}
        result = compare_row_expansion(ptr("基本频率"), report)
        self.assertEqual(result["status"], "matched")
        self.assertEqual(report_text_without_expansion(report, result), "基本频率\n应符合表2-1 规定的要求。")

    def test_changed_value_is_a_cell_mismatch(self) -> None:
        report = {"text": "基本频率\n应符合表2-1 规定的要求。\n基本频率（min−1）\n标称值：70min−1"}
        result = compare_row_expansion(ptr("基本频率"), report)
        self.assertEqual(result["status"], "mismatch")
        self.assertEqual(result["cells"][0]["reason_code"], "CELL_VALUE_MISMATCH")

    def test_pieces_must_reassemble_the_ptr_cell(self) -> None:
        good = {"text": "脉冲振幅\n应符合表2-1 规定的要求。\n脉冲振幅（心房）（V）\n允差：\n240 Ω：-\n38%/+20%\n0.25V：±0.25 V\n允差：\n500 Ω：-27%/+20%"}
        self.assertEqual(compare_row_expansion(ptr("脉冲振幅"), good)["status"], "matched")
        missing = {"text": "脉冲振幅\n应符合表2-1 规定的要求。\n脉冲振幅（心房）（V）\n允差：\n240 Ω：-38%/+20%"}
        self.assertEqual(compare_row_expansion(ptr("脉冲振幅"), missing)["status"], "manual")

    def test_truncated_ptr_cell_stays_manual(self) -> None:
        report = {"text": "心室感知不应期\n应符合表2-1 规定的要求。\n心室感知不应期（ms）\n设置：125；157；190；220；250"}
        result = compare_row_expansion(ptr("心室感知不应期"), report)
        self.assertEqual(result["cells"][0]["reason_code"], "CELL_PTR_CONTINUES")

    def test_report_must_name_the_row(self) -> None:
        # Clause numbers line up but the Report row is another parameter.
        report = {"text": "室后房不应期\n应符合表2-1 规定的要求。\n标称值：250ms"}
        self.assertIsNone(compare_row_expansion(ptr("基本频率"), report))

    def test_test_load_must_match_loads_named_by_ptr(self) -> None:
        node = ptr("基本频率")
        node["text"] += "\n240Ω和1000Ω负载下允许误差±35%。"
        report = {"text": "基本频率\n应符合表2-1 规定的要求。\n基本频率（min−1）\n标称值：60min−1\n@2000Ω"}
        result = compare_row_expansion(node, report)
        self.assertEqual(result["status"], "manual")
        self.assertEqual(result["cells"][-1]["missing_numbers"], ["2000"])


class ClauseShiftTests(unittest.TestCase):
    def test_renumbered_report_clause_is_named(self) -> None:
        ptr_nodes = {
            "2.1.8": {"title": "输入阻抗"},
            "2.1.9": {"title": "房室间期(只适用于双腔起搏器)"},
        }
        reports = {"2.1.8": [{"title": "房室间期（只适用于双腔起搏器）"}]}
        ledger = [{"clause_id": "2.1.8", "decision": "manual", "reason_code": "TEXT_REVIEW_REQUIRED"}]
        _flag_clause_number_shifts(ledger, ptr_nodes, reports)
        self.assertEqual(ledger[0]["reason_code"], "CLAUSE_NUMBER_SHIFTED")
        self.assertEqual(ledger[0]["clause_shift"]["ptr_clause_with_report_title"], "2.1.9")


if __name__ == "__main__":
    unittest.main()


from mvp.ptr_table_rows import compare_table_cells


def ptr_table(rows, label="表1"):
    return {"title": "运动范围", "text": f"运动范围应不小于下表的规定：\n{label} 关节运动范围",
            "references": {"tables": [label]},
            "table_rows": [{"label": label, "pdf_page": 4, "bbox": [0, 0, 1, 1], "rows": rows}]}


def report_rows(*rows):
    return {"text": "运动范围应不小于下表的规定：\n表1 关节运动范围",
            "table_rows": [{"matrix_row": list(row)} for row in rows]}


class TableByRowNameTests(unittest.TestCase):
    PTR = [["关节编号 关节运动名称 左侧MTM 右侧MTM", "", ""], ["肩部左右伸展 116° 116°", "", ""], ["腕夹 28° 28°", "", ""]]

    def test_report_may_split_columns_into_two_tables(self) -> None:
        report = report_rows(
            ["关节编\n号", "关节运\n动名称", "左侧MTM"], ["θ1", "肩部左\n右伸展", "116°\n单位：°"], ["θ8", "腕夹", "28°"],
            ["关节编\n号", "关节运\n动名称", "右侧MTM"], ["θ1", "肩部左\n右伸展", "116°"], ["θ8", "腕夹", "28°"],
        )
        self.assertEqual(compare_table_cells(ptr_table(self.PTR), report)["status"], "matched")

    def test_changed_value_and_missing_row(self) -> None:
        report = report_rows(["θ1", "肩部左右伸展", "116°"], ["θ1", "肩部左右伸展", "118°"])
        result = compare_table_cells(ptr_table(self.PTR), report)
        by_row = {cell["column"]: cell for cell in result["cells"]}
        self.assertEqual(by_row["肩部左右伸展"]["reason_code"], "CELL_VALUE_MISMATCH")
        self.assertEqual(by_row["腕夹"]["reason_code"], "TABLE_ROW_MISSING_IN_REPORT")
        self.assertEqual(result["status"], "mismatch")

    def test_trailing_zero_is_the_same_value(self) -> None:
        ptr = ptr_table([["模式 光照均匀度的标称值", ""], ["非Firefly模式 0.75", ""], ["Firefly模式", "0.70"]], "表9")
        report = report_rows(["模式", "光照均匀度的标称值"], ["非\nFirefl\ny 模式", "0.75"], ["Firefl\ny 模式", "0.7"])
        result = compare_table_cells(ptr, report)
        self.assertEqual(result["status"], "matched")
        self.assertIn("CELL_NUMERIC_EQUAL", {cell["reason_code"] for cell in result["cells"]})
