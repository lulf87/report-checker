from __future__ import annotations

import unittest

from mvp.ptr_compare import _flag_clause_number_shifts
from mvp.ptr_table_rows import (
    _model_applies,
    _rows_with_merged_cells,
    compare_multi_row_expansion,
    compare_parameter_column,
    compare_row_expansion,
    report_text_without_expansion,
)

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



# One parameter printed per product model, a vertically merged tolerance cell
# and a first cell that names two parameters.
MODEL_TABLE = {
    "pdf_page": 4,
    "bbox": [0, 0, 100, 100],
    "rows": [
        ["参数", "型号", "常规数值", "标准设置", "允许误差"],
        ["心房感知灵敏度\n(mV)", "Model A\nModel\nA-T", "AUTO; 0.5 ... 7.5", "AUTO", "±20%"],
        ["", "Model B\nModel\nB-T", "AUTO; 0.1 ... 7.5", "AUTO", "±0.1mV"],
        ["右心室感知灵敏\n度(mV)", "全部型号", "0.5 ... 7.5", "AUTO", "± 20%"],
        ["房室间期(ms)\n感知后的房室间\n期(ms)", "Model A\nModel\nA-T", "不适用", "", ""],
        ["", "Model B\nModel\nB-T", "低，中，高", "低", "/"],
        ["", "", "20 ... (5) ... 350", "180", "± 20"],
        ["", "", "CLS 模式下：20 ... (5) ... 350", "150", ""],
        ["PVARP(ms)", "全部型号", "175 ... 600", "225", "± 20"],
    ],
    # None = no cell of its own (covered by a merged cell above or beside).
    "cell_boxes": [
        [[0, 0, 10, 10]] * 5,
        [[0, 10, 10, 30], [10, 10, 20, 20], [20, 10, 30, 20], [30, 10, 40, 20], [40, 10, 50, 20]],
        [None, [10, 20, 20, 30], [20, 20, 30, 30], [30, 20, 40, 30], [40, 20, 50, 30]],
        [[0, 30, 10, 40], [10, 30, 20, 40], [20, 30, 30, 40], [30, 30, 40, 40], [40, 30, 50, 40]],
        [[0, 40, 10, 80], [10, 40, 20, 50], [20, 40, 50, 50], None, None],
        [None, [10, 50, 20, 80], [20, 50, 30, 60], [30, 50, 40, 60], [40, 50, 50, 60]],
        [None, None, [20, 60, 30, 70], [30, 60, 40, 70], [40, 60, 50, 80]],
        [None, None, [20, 70, 30, 80], [30, 70, 40, 80], None],
        [[0, 80, 10, 90], [10, 80, 20, 90], [20, 80, 30, 90], [30, 80, 40, 90], [40, 80, 50, 90]],
    ],
}
SENSITIVITY_B = "心房感知灵敏度(mV)\n常规数值：AUTO; 0.1 ... 7.5\n标准设置：AUTO\n允许误差：±0.1mV\n"
SENSITIVITY_A = "心房感知灵敏度(mV)\n常规数值：AUTO; 0.5 ... 7.5\n标准设置：AUTO\n允许误差：±20%\n"
VENTRICLE = "右心室感知灵敏度(mV)\n常规数值：0.5 ... 7.5\n标准设置：AUTO\n允许误差：± 20%"


def model_ptr(title: str) -> dict:
    return {"title": title, "text": f"{title}：应符合表1 中的数值。", "table_rows": [MODEL_TABLE]}


class MultiRowExpansionTests(unittest.TestCase):
    def sensitivity(self, body: str, models, **extra) -> dict:
        report = {"text": "灵敏度\n(mV)\n心房感知灵敏度和心室感知灵敏度应符合表1 中的数值。\n" + body, **extra}
        return compare_multi_row_expansion(model_ptr("灵敏度(mV)"), report, models)

    def test_rows_of_the_submitted_model_must_be_copied_in_full(self) -> None:
        result = self.sensitivity(SENSITIVITY_B + VENTRICLE, ["Model B-T"])
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["row_coverage"]["reason_code"], "ROWS_OF_SUBMITTED_MODELS")
        self.assertEqual((result["row_coverage"]["ptr_rows"], result["row_coverage"]["required_rows"]), (3, 2))
        self.assertEqual({cell["status"] for cell in result["cells"]}, {"matched"})
        self.assertEqual(result["consumed_report_lines"], [3, 4, 5, 6, 7, 8, 9, 10])
        report = {"text": "灵敏度\n(mV)\n心房感知灵敏度和心室感知灵敏度应符合表1 中的数值。\n" + SENSITIVITY_B + VENTRICLE}
        self.assertEqual(report_text_without_expansion(report, result),
                         "灵敏度\n(mV)\n心房感知灵敏度和心室感知灵敏度应符合表1 中的数值。")

    def test_other_models_row_or_unknown_model_is_not_passed(self) -> None:
        # The values are a PTR row, but of a model that was not submitted.
        wrong_model = self.sensitivity(SENSITIVITY_A + VENTRICLE, ["Model B-T"])
        self.assertEqual(wrong_model["status"], "manual")
        self.assertEqual(wrong_model["row_coverage"]["reason_code"], "REPORT_BLOCKS_NOT_THE_REQUIRED_ROWS")
        # Page 3 does not name the model: only a full copy of all rows passes.
        self.assertEqual(self.sensitivity(SENSITIVITY_B + VENTRICLE, [])["status"], "manual")
        self.assertEqual(self.sensitivity(SENSITIVITY_A + SENSITIVITY_B + VENTRICLE, [])["status"], "matched")
        # A model that is not in the table selects no model row.
        self.assertEqual(self.sensitivity(SENSITIVITY_B + VENTRICLE, ["Model C"])["status"], "manual")
        # A name must stand on its own; a wrapped name still counts.
        self.assertTrue(_model_applies("Alpha 8 DR\nAlpha 8\nDR-T", ["Alpha 8 DR-T"]))
        self.assertFalse(_model_applies("Alpha 8 SR\nAlpha 8\nSR-T", ["Alpha 8 DR-T"]))
        self.assertFalse(_model_applies("Alpha 8 SR-T", ["R-T"]))
        self.assertFalse(_model_applies("62320", ["6232"]))
        self.assertTrue(_model_applies("全部型号", ["6232"]))

    def test_missing_block_missing_cell_or_changed_value_is_not_passed(self) -> None:
        self.assertEqual(self.sensitivity(SENSITIVITY_B, ["Model B-T"])["status"], "manual")
        without_tolerance = VENTRICLE.replace("\n允许误差：± 20%", "")
        result = self.sensitivity(SENSITIVITY_B + without_tolerance, ["Model B-T"])
        self.assertEqual(result["status"], "manual")
        self.assertEqual(result["cells"][-1]["reason_code"], "CELL_NOT_COPIED")
        changed = self.sensitivity(SENSITIVITY_B + VENTRICLE.replace("± 20%", "± 25%"), ["Model B-T"])
        self.assertNotEqual(changed["status"], "matched")

    def test_value_in_the_result_table_label_column_counts_once(self) -> None:
        without_tolerance = VENTRICLE.replace("\n允许误差：± 20%", "")
        side = [{"matrix_row": ["允许误\n差：±\n20%", "正向"]}]
        result = self.sensitivity(SENSITIVITY_B + without_tolerance, ["Model B-T"], table_rows=side)
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["cells"][-1]["reason_code"], "CELL_EQUAL_IN_RESULT_TABLE")
        wrong = [{"matrix_row": ["允许误\n差：±\n25%", "正向"]}]
        self.assertEqual(self.sensitivity(SENSITIVITY_B + without_tolerance, ["Model B-T"], table_rows=wrong)["status"], "manual")

    def test_merged_cells_and_a_first_cell_naming_two_parameters(self) -> None:
        self.assertEqual(_rows_with_merged_cells(MODEL_TABLE)[7][1:], ["Model B\nModel\nB-T", "CLS 模式下：20 ... (5) ... 350", "150", "± 20"])
        # A horizontally merged cell (不适用) is not copied sideways.
        self.assertEqual(_rows_with_merged_cells(MODEL_TABLE)[4][3:], ["", ""])
        blocks = (
            "房室间期(ms)\n常规数值：低，中，高\n标准设置：低\n允许误差：/\n"
            "房室间期(ms)\n常规数值：20 ... (5) ... 350\n标准设置：180\n允许误差：±20ms\n单位：ms\n"
            "房室间期(ms)\n常规数值：CLS 模式下：20 ... (5) ... 350\n标准设置：150\n允许误差：±20ms\n单位：ms"
        )
        report = {"text": "房室间期\n(ms)\n房室间期应符合表1 中的数值。\n" + blocks}
        result = compare_multi_row_expansion(model_ptr("房室间期(ms)"), report, ["Model B-T"])
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["row_coverage"]["required_rows"], 3)
        # The third row's tolerance comes from the merged cell; a different one is not passed.
        wrong = {"text": report["text"].rsplit("允许误差：±20ms", 1)[0] + "允许误差：±30ms\n单位：ms"}
        self.assertNotEqual(compare_multi_row_expansion(model_ptr("房室间期(ms)"), wrong, ["Model B-T"])["status"], "matched")

    def test_single_row_clause_is_left_to_the_single_row_rule(self) -> None:
        report = {"text": "心室后心\n房不应期\n（PVARP）\n心室后心房不应期应符合表1 中的数值。\nPVARP(ms)\n常规数值：175 ... 600\n标准设置：225\n允许误差：±20ms\n单位：ms"}
        node = model_ptr("心室后心房不应期（PVARP）")
        self.assertIsNone(compare_multi_row_expansion(node, report, ["Model B-T"]))
        # The bracketed alias in the clause title selects the row named PVARP.
        result = compare_row_expansion(node, report)
        self.assertEqual(result["status"], "matched")
        self.assertEqual(report_text_without_expansion(report, result),
                         "心室后心\n房不应期\n（PVARP）\n心室后心房不应期应符合表1 中的数值。")

    def test_value_line_may_carry_the_cell_label(self) -> None:
        table = {"pdf_page": 1, "bbox": [0, 0, 1, 1], "rows": [
            ["参数", "型号一", "型号二"], ["磁频率（min-1）", "BOS：96min-1", "BOS：96min-1"]]}
        node = {"title": "磁频率", "text": "磁频率\n应符合表3 的要求。", "table_rows": [table]}
        report = {"text": "磁频率\n应符合表3 的要求。\n磁频率（min−1）:\nBOS：96min−1\n单位：min−1"}
        self.assertEqual(compare_row_expansion(node, report)["status"], "matched")
        changed = {"text": report["text"].replace("96", "90")}
        self.assertNotEqual((compare_row_expansion(node, changed) or {}).get("status"), "matched")


class ParameterColumnAndPageBreakTests(unittest.TestCase):
    def test_row_cut_by_a_page_break_is_one_row(self) -> None:
        first = {"pdf_page": 2, "bbox": [0, 0, 1, 1], "table_id": "t1", "rows": [
            ["参数", "设置", "标称值", "允差"],
            ["心室感知不应期", "125；157；190；220；", "250ms", "±5ms"]]}
        second = {"pdf_page": 3, "bbox": [0, 0, 1, 1], "continuation_of": "t1", "rows": [
            ["（ms）", "250（自动感知参数应\n设置为[打开]）", "", ""],
            ["心房感知不应期\n（ms）", "93；125", "93ms", "±5ms"]]}
        node = {"title": "心室感知不应期", "text": "心室感知不应期\n应符合表2-1规定的要求。", "table_rows": [first, second]}
        report = {"text": "心室感知\n不应期\n应符合表2-1 规定的要求。\n心室感知不应期（ms）\n"
                          "设置：125；157；190；220；250（自动感知\n参数应设置为[打开]）\n标称值：250ms\n允差：±5ms\n单位：ms"}
        self.assertEqual(compare_row_expansion(node, report)["status"], "matched")
        # Without the second page the PTR cell is known to be incomplete.
        alone = compare_row_expansion({**node, "table_rows": [first]}, report)
        self.assertEqual(alone["status"], "manual")
        # A first cell with a name of its own starts a new row.
        named = {**second, "rows": [["心室起搏不应期（ms）", "250（自动感知参数应\n设置为[打开]）", "", ""]]}
        self.assertEqual(compare_row_expansion({**node, "table_rows": [first, named]}, report)["status"], "manual")

    def column_node(self) -> dict:
        first = {"label": "表4", "pdf_page": 2, "bbox": [0, 0, 1, 1], "table_id": "t4", "rows": [
            ["腔室", "模式", "感知不应期"], ["心室", "VVI", "95 ms ±10 ms"]]}
        second = {"label": "表4", "pdf_page": 3, "bbox": [0, 0, 1, 1], "continuation_of": "t4",
                  "rows": [["心房", "AAI", "345 ms -10/+15 ms"]]}
        return {"title": "感知不应期", "text": "感知不应期\n感知不应期的数值和允差，应符合表4 的要求。",
                "references": {"standards": [], "tables": ["表4"]}, "table_rows": [first, second]}

    def test_parameter_column_is_copied_row_by_row(self) -> None:
        text = "感知不应期\n感知不应期的数值和允差，应符\n合表 4 的要求。\n感知不应期：\n心室：95 ms ±10 ms\n单位：ms\n心房：345ms -10/+15 ms\n单位：ms"
        result = compare_parameter_column(self.column_node(), {"text": text})
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["unchecked_ptr_columns"], ["模式"])
        self.assertEqual(report_text_without_expansion({"text": text}, result),
                         "感知不应期\n感知不应期的数值和允差，应符\n合表 4 的要求。")
        changed = compare_parameter_column(self.column_node(), {"text": text.replace("345ms", "355ms")})
        self.assertEqual(changed["status"], "mismatch")
        missing = compare_parameter_column(self.column_node(), {"text": text.split("心房")[0]})
        self.assertEqual((missing["status"], missing["cells"][1]["reason_code"]), ("manual", "CELL_NOT_COPIED"))
        twice = compare_parameter_column(self.column_node(), {"text": text + "\n心室：95 ms ±10 ms"})
        self.assertEqual(twice["status"], "manual")
        # No column is named after the clause: not this layout.
        other = {**self.column_node(), "title": "起搏不应期"}
        self.assertIsNone(compare_parameter_column(other, {"text": text}))

    def test_merged_value_cell_counts_for_every_row_it_spans(self) -> None:
        table = {"pdf_page": 4, "bbox": [0, 0, 1, 1], "binding": "physical", "rows": [
            ["试验段", "断裂力（N）"], ["头端与管身", "≥10"], ["管身与可调弯管身", "≥15"], ["外管管身", ""], ["外管管身与手柄", ""]],
            "cell_boxes": [[[0, 0, 5, 10], [5, 0, 9, 10]], [[0, 10, 5, 20], [5, 10, 9, 20]],
                           [[0, 20, 5, 30], [5, 20, 9, 50]], [[0, 30, 5, 40], None], [[0, 40, 5, 50], None]]}
        ptr = {"text": "断裂力\n应符合下表要求。", "references": {"standards": [], "tables": []}, "table_rows": [table]}
        report = report_rows(["头端与管身", "≥10"], ["管身与可调弯管身", "≥15"], ["外管管身", "≥15"], ["外管管身与手柄", "≥15"])
        result = compare_table_cells(ptr, report)
        self.assertEqual(result["status"], "matched")
        self.assertEqual({cell["reason_code"] for cell in result["cells"]}, {"CELL_EQUAL"})
        wrong = report_rows(["头端与管身", "≥10"], ["管身与可调弯管身", "≥15"], ["外管管身", "≥10"], ["外管管身与手柄", "≥15"])
        self.assertNotEqual(compare_table_cells(ptr, wrong)["status"], "matched")


class SubmittedModelTests(unittest.TestCase):
    """One parameter row with a different value per product model."""

    ROWS = {"pdf_page": 4, "bbox": [0, 0, 1, 1], "table_id": "t1", "rows": [
        ["参数", "型号", "常规数值", "标准设置", "允许误差"],
        ["PVARP(ms)", "Model A\nModel\nA-T", "150 ... 500", "200", "± 20"],
        ["", "Model B", "175 ... 600", "225", "± 20"]]}
    NEXT_PAGE = {"pdf_page": 5, "bbox": [0, 0, 1, 1], "continuation_of": "t1", "rows": [
        ["", "Model\nB-T", "", "", ""], ["上限频率(bpm)", "全部型号", "90 ... 200", "130", "± 20"]]}
    COLUMNS = {"pdf_page": 1, "bbox": [0, 0, 1, 1], "rows": [
        ["型号", "6131", "6232"], ["基本频率（min-1）", "30-95\n出厂值：70min-1", "30-95\n出厂值：60min-1"]]}

    def rows(self, value: str, models, tables=None):
        node = {"title": "PVARP", "text": "PVARP：应符合表1 中的数值。", "table_rows": tables or [self.ROWS, self.NEXT_PAGE]}
        report = {"text": f"PVARP\n应符合表1 中的数值。\nPVARP(ms)\n{value}\n允许误差：±20ms\n单位：ms"}
        return compare_row_expansion(node, report, models)

    def test_value_must_belong_to_a_registered_model(self) -> None:
        b_values, a_values = "常规数值：175 ... 600\n标准设置：225", "常规数值：150 ... 500\n标准设置：200"
        # ``Model B`` / ``Model⏎B-T`` is one model cell cut by the page break.
        self.assertEqual(self.rows(b_values, ["Model B-T"])["status"], "matched")
        other = self.rows(a_values, ["Model B-T"])
        self.assertEqual(other["status"], "manual")
        self.assertEqual([cell["reason_code"] for cell in other["cells"]],
                         ["CELL_OF_ANOTHER_MODEL", "CELL_OF_ANOTHER_MODEL", "CELL_EQUAL"])  # ±20 is the same for both
        self.assertEqual(other["cells"][0]["ptr_models"], ["Model A Model A-T"])
        unknown = self.rows(b_values, [])
        self.assertEqual((unknown["status"], unknown["cells"][0]["reason_code"]), ("manual", "SUBMITTED_MODEL_UNKNOWN"))
        # Without the next page the cell reads ``Model B``: not the registered ``Model B-T``.
        self.assertEqual(self.rows(b_values, ["Model B-T"], [self.ROWS])["status"], "manual")
        # Callers that do not know the Report's models keep the old behaviour.
        self.assertEqual(self.rows(a_values, None)["status"], "matched")

    def test_one_column_per_model(self) -> None:
        node = {"title": "基本频率", "text": "基本频率\n应符合表3 的要求。", "table_rows": [self.COLUMNS]}

        def run(factory: str, models):
            report = {"text": f"基本频率\n应符合表3 的要求。\n基本频率（min−1）：30-95\n出厂值：{factory}min−1\n单位：min−1"}
            return compare_row_expansion(node, report, models)

        self.assertEqual(run("60", ["6232"])["status"], "matched")
        wrong = run("70", ["6232"])
        self.assertEqual(wrong["status"], "manual")
        self.assertEqual([c["reason_code"] for c in wrong["cells"] if c["status"] != "matched"], ["CELL_OF_ANOTHER_MODEL"])
        self.assertEqual(run("60", [])["status"], "manual")


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

    def test_trailing_zero_must_be_copied(self) -> None:
        ptr = ptr_table([["模式 光照均匀度的标称值", ""], ["非Firefly模式 0.75", ""], ["Firefly模式", "0.70"]], "表9")
        report = report_rows(["模式", "光照均匀度的标称值"], ["非\nFirefl\ny 模式", "0.75"], ["Firefl\ny 模式", "0.7"])
        result = compare_table_cells(ptr, report)
        self.assertEqual(result["status"], "mismatch")
        self.assertIn("CELL_NUMBER_WRITTEN_DIFFERENTLY", {cell["reason_code"] for cell in result["cells"]})


class TableRowNumberAndTextRowsTests(unittest.TestCase):
    def test_row_number_column_is_not_the_row_name(self) -> None:
        ptr = ptr_table([["编号 描述 ROM 允差", "", "", ""], ["1 头枕：前后移动 56 mm ±5 mm", "", "", ""],
                         ["6", "踏板控制面板：前后移动", "285.6 mm", "+5/-10 mm"]], "表2")
        report = report_rows(["描述", "ROM", "允差"], ["头枕：\n前后移\n动", "56 mm", "±5 mm\n单位：mm"],
                             ["踏板控\n制面板：前\n后移动", "285.6\nmm", "+5/-10 mm"])
        self.assertEqual(compare_table_cells(ptr, report)["status"], "matched")

    def test_report_row_found_only_in_ptr_running_text(self) -> None:
        ptr = ptr_table([["关节运动名称 运动范围 允差", "", ""], ["水平延伸 360mm ±2mm", "", ""], ["垂直升降 254mm ±2mm", "", ""]], "表3")
        ptr["text"] += "\n1号臂 100°\n1 柔性旋转 ±1°"
        report = report_rows(["水平延\n伸", "360mm", "±2mm"], ["垂直升\n降", "254mm", "±2mm"],
                             ["柔性旋\n转", "1 号臂\n100°", "±1°"], ["柔性旋转", "1 号臂\n101°", "±1°"])
        result = compare_table_cells(ptr, report)
        reasons = [cell["reason_code"] for cell in result["cells"] if cell["column"] == "Report 表格行"]
        self.assertEqual(reasons, ["ROW_CELLS_IN_PTR_TEXT", "TABLE_ROW_NOT_IN_PTR"])
