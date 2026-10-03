from __future__ import annotations

import unittest
from decimal import Decimal
from pathlib import Path

from mvp.checker import SAMPLE_CONFIGS
from mvp.record_full import (
    CoverageEntry,
    CoverageInvariantError,
    Observation,
    RecordRow,
    compare_final_percentage,
    compare_numeric_observation,
    compare_record202_status_result,
    compare_record61_status_result,
    convert_decimal,
    expected_report_conclusion_from_record_statuses,
    extract_final_percentage,
    extract_percentage_values,
    normalize_unit,
    parse_report_numeric,
    quantize_for_report,
    scan_report_rows,
    validate_coverage,
)


ROOT = Path(__file__).resolve().parents[1]


class RecordFullRuleTests(unittest.TestCase):
    def test_contract_dataclasses_are_serializable(self) -> None:
        observation = Observation(
            observation_id="o1",
            kind="numeric",
            raw="509",
            value=Decimal("509"),
            unit="uA",
            bbox=(1.0, 2.0, 3.0, 4.0),
        )
        row = RecordRow(
            row_id="r1",
            record_item="8.7",
            row_ordinal=1,
            pdf_page=10,
            clause_raw="8.7.3",
            requirement_raw="unit test",
            observations=(observation,),
        )
        payload = row.to_dict()
        self.assertEqual(payload["observations"][0]["value"], "509")
        self.assertEqual(payload["observations"][0]["bbox"], [1.0, 2.0, 3.0, 4.0])

    def test_61_status_mapping_is_strict(self) -> None:
        self.assertEqual(compare_record61_status_result("符合", "符合要求").decision, "match")
        self.assertEqual(compare_record61_status_result("符合", "0.3").decision, "match")
        self.assertEqual(compare_record61_status_result("不适用", "——").decision, "match")
        self.assertEqual(compare_record61_status_result("不符合", "不符合要求").decision, "match")
        self.assertEqual(compare_record61_status_result("不适用", "/").decision, "mismatch")
        self.assertEqual(compare_record61_status_result("符合", "-").decision, "mismatch")
        self.assertEqual(compare_record61_status_result("合格", "符合要求").decision, "manual")

    def test_202_symbol_mapping_is_strict(self) -> None:
        cases = (
            ("√", "符合要求", "match"),
            ("√", "112.5", "match"),
            ("×", "不符合要求", "match"),
            ("△", "——", "match"),
            ("/", "/", "match"),
            ("△", "/", "mismatch"),
            ("/", "——", "mismatch"),
            ("", "——", "manual"),
        )
        for symbol, result, expected in cases:
            with self.subTest(symbol=symbol, result=result):
                self.assertEqual(compare_record202_status_result(symbol, result).decision, expected)

    def test_status_aggregation_uses_report_r07_semantics(self) -> None:
        self.assertEqual(
            expected_report_conclusion_from_record_statuses(
                ["不适用", "符合"], mode="9706.1"
            ),
            "符合",
        )
        self.assertEqual(
            expected_report_conclusion_from_record_statuses(
                ["不适用", "不适用"], mode="9706.1"
            ),
            "/",
        )
        self.assertEqual(
            expected_report_conclusion_from_record_statuses(["△", "/"], mode="9706.202"),
            "/",
        )
        self.assertEqual(
            expected_report_conclusion_from_record_statuses(["√", "×"], mode="9706.202"),
            "不符合",
        )
        self.assertIsNone(
            expected_report_conclusion_from_record_statuses(["其他"], mode="9706.202")
        )

    def test_microamp_aliases_and_decimal_conversion(self) -> None:
        self.assertEqual(normalize_unit("uA"), "uA")
        self.assertEqual(normalize_unit("µA"), "uA")
        self.assertEqual(normalize_unit("μA"), "uA")
        for unit in ("uA", "µA", "μA"):
            with self.subTest(unit=unit):
                self.assertEqual(convert_decimal("509", unit, "mA"), Decimal("0.509"))
        self.assertEqual(convert_decimal("0.5", "mA", "uA"), Decimal("5E+2"))
        with self.assertRaises(ValueError):
            convert_decimal("1", "mA", "V")

    def test_report_precision_uses_round_half_up(self) -> None:
        self.assertEqual(quantize_for_report(Decimal("0.005"), 2), Decimal("0.01"))
        self.assertEqual(quantize_for_report(Decimal("0.004"), 2), Decimal("0.00"))
        self.assertEqual(
            compare_numeric_observation("509", "uA", "0.5", "mA").decision,
            "match",
        )
        self.assertEqual(
            compare_numeric_observation("910.2", "μA", "0.91 mA").decision,
            "match",
        )
        self.assertEqual(
            compare_numeric_observation("337.8", "µA", "0.34", "mA").decision,
            "match",
        )
        self.assertEqual(
            compare_numeric_observation("509", "uA", "0.52", "mA").decision,
            "mismatch",
        )

    def test_numeric_threshold_and_parse(self) -> None:
        parsed = parse_report_numeric("＜0.01", "mA")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.comparator, "<")
        self.assertEqual(parsed.decimal_places, 2)
        self.assertEqual(
            compare_numeric_observation("9", "uA", "＜0.01", "mA").decision,
            "match",
        )
        self.assertEqual(
            compare_numeric_observation("10", "uA", "＜0.01", "mA").decision,
            "mismatch",
        )
        self.assertEqual(
            compare_numeric_observation("1", "mA", "1 V").reason_code,
            "unit_conversion_unresolved",
        )

    def test_percentage_compares_only_last_recorded_percentage(self) -> None:
        self.assertEqual(extract_final_percentage("初值1.2%，最终0.30%"), ("0.30%", Decimal("0.30")))
        self.assertEqual(
            compare_final_percentage("初值1.2%，最终0.30%", "0.3%").decision,
            "match",
        )
        self.assertEqual(compare_final_percentage("0.3%", "0.4%").decision, "mismatch")
        self.assertEqual(compare_final_percentage("0.3%", "符合要求").reason_code, "report_percentage_missing")

    def test_percentage_range_compares_both_copied_endpoints(self) -> None:
        self.assertEqual(
            extract_percentage_values("-5%～+4%"),
            (Decimal("-5"), Decimal("4")),
        )
        self.assertEqual(compare_final_percentage("-5～+4%", "-5%～+4%").decision, "match")
        self.assertEqual(compare_final_percentage("-5%～+4%", "-4%～+4%").decision, "mismatch")

    def test_coverage_conservation_and_one_to_many_mapping(self) -> None:
        entries = [
            CoverageEntry("s1", "r1", "matched", "paired"),
            CoverageEntry("s1", "r2", "matched", "one_to_many"),
            CoverageEntry("s2", None, "excluded", "TABLE3_OUT_OF_SCOPE"),
            CoverageEntry(None, "r3", "mismatch", "report_without_record_basis", ("Q3",)),
        ]
        coverage = validate_coverage(["s1", "s2"], ["r1", "r2", "r3"], entries)
        self.assertTrue(coverage["source_rows"]["conserved"])
        self.assertTrue(coverage["report_rows"]["conserved"])
        self.assertEqual(coverage["source_rows"]["eligible"], 2)
        self.assertEqual(coverage["report_rows"]["dispositions"]["mismatch"], 1)

    def test_coverage_rejects_silent_or_unknown_rows(self) -> None:
        with self.assertRaises(CoverageInvariantError):
            validate_coverage(["s1", "s2"], ["r1"], [CoverageEntry("s1", "r1", "matched", "paired")])
        with self.assertRaises(CoverageInvariantError):
            validate_coverage(["s1"], ["r1"], [CoverageEntry("unknown", "r1", "manual", "bad")])
        with self.assertRaises(CoverageInvariantError):
            validate_coverage([], [], [CoverageEntry(None, None, "manual", "bad")])


class ReportRowScannerIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.rows = scan_report_rows(
            ROOT / SAMPLE_CONFIGS["1539"].report,
            (155, 155),
        )

    def test_scans_every_physical_row_for_selected_sequence(self) -> None:
        self.assertEqual(len(self.rows), 10)
        self.assertEqual({row.sequence for row in self.rows}, {155})
        self.assertEqual([row.row_ordinal for row in self.rows], list(range(1, 11)))
        self.assertEqual(len({row.row_id for row in self.rows}), 10)

    def test_preserves_values_and_coordinate_identity(self) -> None:
        self.assertTrue(self.rows[0].requirement_raw.startswith("201.15.101.1"))
        self.assertEqual(self.rows[0].result_raw, "符合要求")
        self.assertEqual(self.rows[-1].result_raw, "——")
        for row in self.rows:
            self.assertIsNotNone(row.requirement_rect)
            self.assertIsNotNone(row.result_rect)
            self.assertEqual(row.clause_raw.replace(" ", ""), "201.15.101")


if __name__ == "__main__":
    unittest.main()
