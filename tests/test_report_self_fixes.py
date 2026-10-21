from __future__ import annotations

import unittest
from pathlib import Path

from mvp.checker import cross_reference_conclusion


ROOT = Path(__file__).resolve().parents[1]


class CrossReferenceConclusionTests(unittest.TestCase):
    ITEMS = {
        32: {
            "requirements": ["20.1 试验和符合性应符合YY/T 1874-2023。", "20.2 试验和符合性应符合YY/T 1874-2023。"],
            "results": ["/", "/"],
            "conclusions": ["/"],
        },
        38: {"requirements": ["脉冲幅度"], "results": ["符合要求"], "conclusions": ["符合"]},
        39: {"requirements": ["脉冲宽度"], "results": ["-2%～+6%"], "conclusions": ["符合"]},
        40: {"requirements": ["基础频率"], "results": ["不符合要求"], "conclusions": ["不符合"]},
    }

    def test_subclause_reference_takes_the_referenced_rows(self) -> None:
        self.assertEqual(cross_reference_conclusion(["见序号32 中20.1", "见序号32 中20.2"], self.ITEMS), "/")

    def test_range_reference_aggregates_referenced_conclusions(self) -> None:
        self.assertEqual(cross_reference_conclusion(["见序号38～序号39"], self.ITEMS), "符合")
        self.assertEqual(cross_reference_conclusion(["见序号38～序号40"], self.ITEMS), "不符合")

    def test_unresolved_or_mixed_results_keep_the_original_rule(self) -> None:
        self.assertIsNone(cross_reference_conclusion(["见序号99"], self.ITEMS))
        self.assertIsNone(cross_reference_conclusion(["见序号32 中21.1"], self.ITEMS))
        self.assertIsNone(cross_reference_conclusion(["见序号38", "符合要求"], self.ITEMS))


@unittest.skipUnless((ROOT / "素材/report/5332").is_dir(), "本地 Report 素材不存在")
class CoverWrapTests(unittest.TestCase):
    def test_wrapped_cover_sample_name_is_joined(self) -> None:
        try:
            import pymupdf as fitz
        except ImportError:  # pragma: no cover
            import fitz
        from mvp.checker import cover_value_after_label

        report = next((ROOT / "素材/report/5332").glob("*.pdf"))
        with fitz.open(report) as document:
            value, _rect = cover_value_after_label(document[0], "样品名称")
        self.assertEqual(value, "一次性使用磁电定位心脏脉冲电场消融导管")


if __name__ == "__main__":
    unittest.main()
