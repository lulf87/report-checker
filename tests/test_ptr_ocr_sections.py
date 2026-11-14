from __future__ import annotations

import unittest

from mvp.ptr_ocr_sections import _heading, select_ocr_requirement_section


def lines(*texts: str, channel: str = "ocr:apple_vision") -> list[tuple]:
    return [(1 + index // 20, [10.0, 10.0 + index, 200.0, 20.0 + index], text, channel, 0.9)
            for index, text in enumerate(texts)]


class OcrRequirementSectionTests(unittest.TestCase):
    def test_clause_number_may_end_with_a_dot(self) -> None:
        self.assertEqual(_heading("2.1.1. 输出电压范围"), ("2.1.1", "输出电压范围"))
        self.assertEqual(_heading("2.1.10.最大输出能量"), ("2.1.10", "最大输出能量"))
        self.assertEqual(_heading("2.1.1．输出电压范围"), ("2.1.1", "输出电压范围"))  # OCR full-width dot
        self.assertEqual(_heading("2.1.1 输出电压范围"), ("2.1.1", "输出电压范围"))
        # A dot followed by a digit continues the number.
        self.assertEqual(_heading("2.1.5.3 导丝兼容性"), ("2.1.5.3", "导丝兼容性"))
        self.assertIsNone(_heading("2.2±20%"))
        self.assertIsNone(_heading("2.76. 5"))

    def test_body_with_dot_terminated_headings_is_selected(self) -> None:
        observations = lines(
            "1. 产品型号/规格",
            "2. 性能指标",
            "2.1. 输出参数",
            "2.1.1. 输出电压范围",
            "输出电压应在 500V～2000V 范围内可调，误差不大于±10%。",
            "2.2. 外观",
            "外观应整洁，无明显划痕。",
            "3. 检验方法",
            "3.1. 输出电压范围",
            "按 2.1.1 的方法测量，结果应符合",
            "2.1.1的要求。",
        )
        section = select_ocr_requirement_section(observations)
        self.assertEqual(section["status"], "selected_body")
        self.assertEqual(section["body_indices"], [1, 2, 3, 4, 5, 6])
        body = section["candidates"][0]
        self.assertEqual([item["clause_id"] for item in body["headings"]], ["2.1", "2.1.1", "2.2"])

    def test_outline_without_requirement_sentences_is_not_a_body(self) -> None:
        section = select_ocr_requirement_section(lines("2. 性能指标", "2.1. 输出参数", "2.2. 外观", "3. 检验方法"))
        self.assertEqual(section["status"], "body_not_detected")
        self.assertEqual(section["reason_code"], "PTR_OCR_BODY_NOT_DETECTED")

    def test_mixed_channels_are_rejected(self) -> None:
        mixed = lines("2.1. 输出参数") + lines("输出电压应可调。", channel="ocr:rapidocr")
        self.assertEqual(select_ocr_requirement_section(mixed)["reason_code"], "PTR_OCR_CHANNEL_AMBIGUOUS")


if __name__ == "__main__":
    unittest.main()
