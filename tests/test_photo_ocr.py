from __future__ import annotations

import unittest
from pathlib import Path

from mvp.full_report_photo import (
    _photo_subject,
    associate_rows_to_photos,
    compare_date_with_photo,
    compare_placeholder_with_photo,
    compare_with_photo_text,
)
from mvp.photo_ocr import available_backends, build_record, keyed_values, normalize_text


ROOT = Path(__file__).resolve().parents[1]


def item(text: str, x: float, y: float, w: float = 200, h: float = 20) -> dict:
    return {"text": text, "confidence": 1.0, "bbox": [x, y, x + w, y + h]}


ENTRY = {"pdf_page": 9, "image_index": 1, "number": 2, "image_rect": [10, 10, 300, 200], "title_rect": [10, 5, 100, 9]}


def pairs_from(**channels) -> list:
    return [(ENTRY, build_record(pdf_page=9, image_index=1, channels=channels))]


class KeyedExtractionTests(unittest.TestCase):
    def test_key_value_on_same_box_and_next_box(self) -> None:
        values = keyed_values([
            item("产品名称：心脏脉冲电场消融仪", 0, 0),
            item("型号:", 0, 30),
            item("471006", 120, 30),
            item("SN:PFAB00001", 0, 60),
            item("LOT", 0, 90),
            item("76220002", 60, 90),
            item("生产日期：2025-10-16", 0, 120),
        ])
        self.assertEqual(values["部件名称"], ["心脏脉冲电场消融仪"])
        self.assertEqual(values["规格型号"], ["471006"])
        self.assertEqual(values["序列号/批号"], ["PFAB00001", "76220002"])
        self.assertEqual(values["生产日期"], ["2025-10-16"])

    def test_words_that_merely_start_like_a_key_are_ignored(self) -> None:
        values = keyed_values([item("SNAPSHOT MODE", 0, 0), item("LOTUS", 0, 30)])
        self.assertEqual(values["序列号/批号"], [])

    def test_multi_column_text_keeps_wrapped_address_together(self) -> None:
        record = build_record(pdf_page=1, image_index=1, channels={"rapidocr": [
            item("住所：中国(上海)自由贸易试验区泰谷路169号", 800, 20, 400),
            item("丁医疗用品（上海）有限公司", 20, 40, 400),
            item("B楼四层402室电话:(86)21-2306-7588", 800, 70, 400),
        ]})
        channel = record["channels"]["rapidocr"]
        address = normalize_text("中国（上海）自由贸易试验区泰谷路169号B楼四层402室")
        self.assertNotIn(address, channel["text"])
        self.assertIn(address, channel["text_columns"])


class PhotoComparisonTests(unittest.TestCase):
    def test_report_value_found_in_any_channel_passes(self) -> None:
        pairs = pairs_from(rapidocr=[item("组件号：ASM-00094", 0, 0)])
        result = compare_with_photo_text("规格型号", "ASM-00094", pairs)
        self.assertEqual((result["status"], result["reason_code"]), ("pass", "photo_text_contains_report_value"))
        # 全/半角和排版空格不算差异。
        result = compare_with_photo_text("部件名称", "心脏脉冲电场消融仪（主机）", pairs_from(rapidocr=[item("产品描述：心脏脉冲电场消融仪(主机)", 0, 0)]))
        self.assertEqual(result["status"], "pass")

    def test_report_key_prefix_is_ignored(self) -> None:
        result = compare_with_photo_text("序列号/批号", "批号：76220002", pairs_from(rapidocr=[item("LOT", 0, 0), item("76220002", 60, 0)]))
        self.assertEqual(result["status"], "pass")

    def test_single_channel_difference_is_manual_with_candidates(self) -> None:
        result = compare_with_photo_text("规格型号", "RMC01", pairs_from(rapidocr=[item("型号：RMCO1", 0, 0)]))
        self.assertEqual(result["status"], "manual")
        self.assertEqual(result["candidates"], {"rapidocr": ["RMCO1"]})

    def test_two_agreeing_channels_make_a_serial_mismatch_an_error(self) -> None:
        pairs = pairs_from(
            apple_vision=[item("序列号：20539799", 0, 0)],
            rapidocr=[item("序列号：20539799", 0, 0)],
        )
        result = compare_with_photo_text("序列号/批号", "20539798", pairs)
        self.assertEqual((result["status"], result["reason_code"]), ("error", "two_ocr_channels_agree_label_differs"))
        disagreeing = pairs_from(
            apple_vision=[item("序列号：20539799", 0, 0)],
            rapidocr=[item("序列号：2O539799", 0, 0)],
        )
        self.assertEqual(compare_with_photo_text("序列号/批号", "20539798", disagreeing)["status"], "manual")

    def test_names_are_never_errors_from_ocr_alone(self) -> None:
        pairs = pairs_from(
            apple_vision=[item("产品名称：甲设备", 0, 0)],
            rapidocr=[item("产品名称：甲设备", 0, 0)],
        )
        self.assertEqual(compare_with_photo_text("部件名称", "乙设备", pairs)["status"], "manual")

    def test_bilingual_names_may_swap_order(self) -> None:
        pairs = pairs_from(rapidocr=[item("注册人/生产企业名称：示例医疗公司 Example Surgical, Inc.", 0, 0)])
        result = compare_with_photo_text("生产单位", "Example Surgical, Inc. 示例医疗公司", pairs, strict=False)
        self.assertEqual((result["status"], result["reason_code"]), ("pass", "name_parts_found_in_photo_text"))

    def test_placeholder_passes_only_when_label_has_no_value(self) -> None:
        empty = pairs_from(rapidocr=[item("使用期限：/", 0, 0), item("其它内容详见说明书", 0, 30)])
        self.assertEqual(compare_placeholder_with_photo("生产日期", "/", empty)["status"], "pass")
        filled = pairs_from(rapidocr=[item("生产日期：2025-04-19", 0, 0)])
        result = compare_placeholder_with_photo("生产日期", "/", filled)
        self.assertEqual((result["status"], result["reason_code"]), ("manual", "report_placeholder_but_photo_has_value"))
        self.assertEqual(compare_placeholder_with_photo("生产日期", "/", [])["reason_code"], "photo_ocr_not_available")

    def test_date_value_and_format_must_both_match(self) -> None:
        self.assertEqual(
            compare_date_with_photo("生产日期", "2025/05/21", pairs_from(rapidocr=[item("生产日期：2025/05/21", 0, 0)]))["status"],
            "pass",
        )
        same_day = pairs_from(
            apple_vision=[item("生产日期：2025-10-09", 0, 0)],
            rapidocr=[item("生产日期：2025-10-09", 0, 0)],
        )
        result = compare_date_with_photo("生产日期", "2025-10-9", same_day)
        self.assertEqual((result["status"], result["reason_code"]), ("error", "date_format_differs_from_label"))
        single = pairs_from(rapidocr=[item("生产日期：2025-10-09", 0, 0)])
        self.assertEqual(compare_date_with_photo("生产日期", "2025-10-9", single)["status"], "manual")


class CaptionAndAssociationTests(unittest.TestCase):
    def test_label_caption_variants(self) -> None:
        self.assertEqual(_photo_subject("№5 心脏脉冲电场消融仪-主机 中文标签样张"), ("心脏脉冲电场消融仪-主机", "label"))
        self.assertEqual(_photo_subject("№5 消化道脉冲电场消融仪 主机中文标签"), ("消化道脉冲电场消融仪 主机", "label"))
        self.assertEqual(_photo_subject("№4 一次性使用磁电定位心脏脉冲电场消融导管连接尾线 中文标"), ("一次性使用磁电定位心脏脉冲电场消融导管连接尾线", "label"))
        self.assertEqual(_photo_subject("№3 消化道脉冲电场消融仪 主机侧面"), ("消化道脉冲电场消融仪 主机", "object"))
        self.assertEqual(_photo_subject("№15 软件发布版本")[1], "other")

    def test_caption_with_component_suffix_uses_unique_name_prefix(self) -> None:
        rows = [
            {"sequence": 1, "fields": {"部件名称": "消化道脉冲电场 消融仪"}},
            {"sequence": 2, "fields": {"部件名称": "尾线"}},
        ]
        entries = [
            {"subject": "消化道脉冲电场消融仪 主机", "number": 5},
            {"subject": "尾线", "number": 7},
        ]
        associations = associate_rows_to_photos(rows, entries)
        self.assertEqual([entry["number"] for entry in associations[1]], [5])
        self.assertEqual(associations[1][0]["association_method"], "unique_row_name_prefix")
        self.assertEqual([entry["number"] for entry in associations[2]], [7])


@unittest.skipUnless(
    (ROOT / "素材/report/1539").is_dir() and "rapidocr" in available_backends(),
    "需要本地 1539 素材和 RapidOCR",
)
class LivePhotoOcrTests(unittest.TestCase):
    def test_1539_label_fields_are_confirmed_by_local_ocr(self) -> None:
        try:
            import pymupdf as fitz
        except ImportError:  # pragma: no cover
            import fitz
        from mvp.full_report_photo import analyze_report_photo_rules
        from mvp.photo_ocr import PhotoOcrProvider

        report = next((ROOT / "素材/report/1539").glob("*.pdf"))
        with fitz.open(report) as document:
            analysis = analyze_report_photo_rules(report, None, None, ocr_provider=PhotoOcrProvider(document, ["rapidocr"]))
        statuses = {item["id"]: item["status"] for item in analysis["findings"]}
        self.assertEqual(statuses["REPORT-R02"], "pass")
        self.assertEqual(statuses["REPORT-R03"], "pass")
        self.assertEqual(statuses["REPORT-R04"], "pass")
        self.assertEqual(analysis["page3_photo_finding"]["status"], "pass")
        self.assertTrue(analysis["ocr_records"])


if __name__ == "__main__":
    unittest.main()
