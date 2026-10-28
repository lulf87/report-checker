from __future__ import annotations

import unittest

from mvp.ptr_compare import safe_text_equivalence
from mvp.ptr_layout import CAPTION, RESULT_QUALIFIER, VALUE_NOT_CLAUSE

HEADERS = {"示例公司专有机密文件", "文件:1234567-01", "修订版:A"}


class SafeEquivalenceTests(unittest.TestCase):
    def test_running_header_lines_are_ignored(self) -> None:
        ptr = "系统应提供该功能。\n示例公司专有机密文件\n文件：1234567-01 修订版：A"
        self.assertEqual(
            safe_text_equivalence(ptr, "系统应提供\n该功能。", ptr_headers=HEADERS),
            ["PTR_RUNNING_HEADER"],
        )

    def test_figure_labels_are_ignored(self) -> None:
        ptr = "运动范围应不小于下表的规定：\n肩部伸展θ1\n腕旋转θ7\n图1 关节示意图\n表1 关节运动范围"
        report = "运动范围应不小于下表的规定：\n表1 关节运动范围"
        self.assertEqual(safe_text_equivalence(ptr, report), ["PTR_FIGURE_LABELS"])

    def test_unit_line_only_when_unit_is_in_ptr(self) -> None:
        ptr = "实测值应不大于标称值的1.05倍。"
        self.assertEqual(safe_text_equivalence(ptr, ptr + "\n单位：倍"), ["REPORT_UNIT_LINE"])
        self.assertIsNone(safe_text_equivalence(ptr, ptr + "\n单位：像素"))

    def test_parallel_result_labels_after_unit_line(self) -> None:
        ptr = "a.跟随：≤12Nm b.摆位：≤155N"
        report = "a.跟随：≤ 12Nm\n单位：N·m\nYaw\nPitch\nb.摆位：≤ 155N\n单位：N"
        self.assertEqual(
            safe_text_equivalence(ptr, report), ["REPORT_UNIT_LINE", "REPORT_RESULT_LABELS"]
        )

    def test_extra_requirement_line_is_not_a_label(self) -> None:
        ptr = "表面应光滑。"
        self.assertIsNone(safe_text_equivalence(ptr, "表面应光滑。\n且无破损"))
        self.assertIsNone(safe_text_equivalence(ptr, "表面应光滑。\n无裂纹\n无毛刺"))

    def test_punctuation_and_bullets(self) -> None:
        self.assertEqual(
            safe_text_equivalence("● 系统能识别器械。", "系统能识别器械"), ["PUNCTUATION_ONLY"]
        )
        # A decimal point is a value, not punctuation.
        self.assertIsNone(safe_text_equivalence("允差为1.5mm", "允差为15mm"))

    def test_repeated_requirement(self) -> None:
        ptr = "欠压补充时间\n欠压补充时间的标称值为5秒。实测值应不大于标称值。"
        report = (
            "欠压补充时间\n欠压补充时间的标称值为5 秒。实测值应不大\n于标称值。\n单位：s\n"
            "欠压补充时间的标称值为5 秒。实测值应不\n大于标称值。\n单位：s"
        )
        self.assertEqual(safe_text_equivalence(ptr, report), ["REPORT_REPEATED_TEXT"])
        # A repeated sentence with a different value is still a difference.
        self.assertIsNone(safe_text_equivalence(ptr, report.replace("不\n大于标称值", "不\n大于标称值的2倍")))

    def test_expanded_reference_is_checked_against_ptr(self) -> None:
        ptr = "图像显示延迟应满足 2.5.7.a)的要求。"
        report = ptr + "\n2.5.7 a)平均显示延迟时间：≤ 85ms，可见光模式下。"
        clauses = {"2.5.7a": "平均显示延迟时间:≤85ms,可见光模式下。"}
        self.assertEqual(
            safe_text_equivalence(ptr, report, ptr_text_by_clause=clauses),
            ["REPORT_EXPANDED_REFERENCE"],
        )
        wrong = {"2.5.7a": "平均显示延迟时间:≤80ms,可见光模式下。"}
        self.assertIsNone(safe_text_equivalence(ptr, report, ptr_text_by_clause=wrong))


class LayoutPatternTests(unittest.TestCase):
    def test_caption_with_full_width_colon(self) -> None:
        self.assertTrue(CAPTION.match("表1：参数表"))

    def test_model_line_with_requirement_is_kept(self) -> None:
        self.assertTrue(RESULT_QUALIFIER.match("470066 左路"))
        self.assertTrue(RESULT_QUALIFIER.match("右路"))
        self.assertFalse(RESULT_QUALIFIER.match("470066：0°±3°。"))

    def test_value_is_not_a_clause_title(self) -> None:
        self.assertTrue(VALUE_NOT_CLAUSE.match("个周期/°（SFR 50%和30%）"))
        self.assertFalse(VALUE_NOT_CLAUSE.match("对非电离电磁辐射的防护"))


if __name__ == "__main__":
    unittest.main()


class TableAsTextTests(unittest.TestCase):
    TABLE = [["设备名称 患者手术平台", "", ""], ["接口 协议 用途", "", ""], ["RS485", "RS485", "用于手术台和患者手术平台"]]

    @staticmethod
    def rows(table):
        from mvp.ptr_compare import _strip_punctuation, normalize_comparison_text

        return [_strip_punctuation(normalize_comparison_text("".join(row))) for row in table]

    def test_untitled_table_written_out_as_text(self) -> None:
        report = "患者手术平台\n设备名称\n患者手术平台\n接口\n协议\n用途\nRS485\nRS485\n用于手术台\n和患者手术\n平台"
        self.assertEqual(
            safe_text_equivalence("患者手术平台", report, ptr_table_texts=[self.rows(self.TABLE)]),
            ["REPORT_TABLE_AS_TEXT"],
        )

    def test_wrapped_cell_read_in_another_order_but_rows_cannot_swap(self) -> None:
        table = [["接口 用途", ""], ["光纤 连接到无线电力传输", ""], ["网口 售后维护用", ""]]
        reordered = "平台\n接口\n用途\n光纤\n无线电力传输\n连接到\n网口\n售后维护用"
        self.assertEqual(
            safe_text_equivalence("平台", reordered, ptr_table_texts=[self.rows(table)]), ["REPORT_TABLE_AS_TEXT"]
        )
        swapped = "平台\n接口\n用途\n光纤\n售后维护用\n网口\n连接到无线电力传输"
        self.assertIsNone(safe_text_equivalence("平台", swapped, ptr_table_texts=[self.rows(table)]))

    def test_repeated_standard_line(self) -> None:
        ptr = "执行GB 9706.1-2020规定的所有要求。"
        self.assertEqual(
            safe_text_equivalence(ptr, ptr + "\nGB 9706.1-2020"), ["REPORT_REPEATED_TEXT"]
        )


class TocAndReferenceTests(unittest.TestCase):
    def test_table_of_contents_lines(self) -> None:
        from mvp.ptr_layout import TOC_LINE

        self.assertTrue(TOC_LINE.search("2.1.1 压力导管......................................................................"))
        self.assertTrue(TOC_LINE.search("2.2.2 连接线缆................................................... 错误！未定义书签。"))
        self.assertTrue(TOC_LINE.search("3 试验方法.................................. 12"))
        self.assertFalse(TOC_LINE.search("2.1.1 压力导管的长度应为1200 mm。"))

    def test_attached_table_caption(self) -> None:
        self.assertEqual(CAPTION.match("附表1 一次性使用压力监测射频脉冲消融导管型号规格表").group(1), "表1")

    def test_appendix_of_a_standard_is_not_a_document_table(self) -> None:
        from mvp.ptr_compare import _references

        refs = _references("当按照YY0285.1-2017 中附录C 给出的方法试验时，产品特性见附录A。")
        self.assertEqual(refs["tables"], ["附录A"])

    def test_report_writes_out_another_ptr_clause(self) -> None:
        ptr = "试验后导线的直流电阻应符合2.3.1.1 的要求。"
        report = ptr + "\n2.3.1.1 直流电阻\n各电极与对应插头芯脚之间的直流电\n阻值应不大于10Ω。\n单位：Ω"
        clauses = {"2.3.1.1": "直流电阻各电极与对应插头芯脚之间的直流电阻值应不大于10Ω。"}
        self.assertIn("REPORT_EXPANDED_REFERENCE", safe_text_equivalence(ptr, report, ptr_text_by_clause=clauses))
        changed = {"2.3.1.1": "直流电阻各电极与对应插头芯脚之间的直流电阻值应不大于5Ω。"}
        self.assertIsNone(safe_text_equivalence(ptr, report, ptr_text_by_clause=changed))
