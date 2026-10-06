from __future__ import annotations

import unittest
from decimal import Decimal

from mvp.record_full import (
    aggregate_numeric_observations,
    compare_numeric_observation,
    parse_numeric_constraint,
)


class RecordFullNumericSemanticsTests(unittest.TestCase):
    def test_explicit_bracket_interval_converts_units_and_respects_endpoints(self) -> None:
        parsed = parse_numeric_constraint("[0.5, 1.0]mA")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.kind, "interval")
        self.assertEqual(parsed.unit, "mA")
        self.assertTrue(parsed.bounds_explicit)
        self.assertEqual(parsed.lower, Decimal("0.5"))
        self.assertEqual(parsed.upper, Decimal("1.0"))
        self.assertEqual(
            compare_numeric_observation("500", "uA", "[0.5, 1.0]mA").decision,
            "match",
        )
        self.assertEqual(
            compare_numeric_observation("1000", "uA", "[0.5, 1.0]mA").decision,
            "match",
        )
        self.assertEqual(
            compare_numeric_observation("400", "uA", "[0.5, 1.0]mA").decision,
            "mismatch",
        )

    def test_open_interval_is_honoured(self) -> None:
        self.assertEqual(
            compare_numeric_observation("500", "uA", "(0.5mA,1.0mA]").decision,
            "mismatch",
        )
        self.assertEqual(
            compare_numeric_observation("501", "uA", "(0.5mA,1.0mA]").decision,
            "match",
        )

    def test_unbracketed_range_is_explicitly_manual(self) -> None:
        parsed = parse_numeric_constraint("0.5~1.0mA")
        self.assertIsNotNone(parsed)
        self.assertFalse(parsed.bounds_explicit)
        result = compare_numeric_observation("500", "uA", "0.5~1.0mA")
        self.assertEqual(result.decision, "manual")
        self.assertEqual(result.reason_code, "numeric_interval_bounds_unresolved")

    def test_threshold_and_precision_remain_deterministic(self) -> None:
        self.assertEqual(
            compare_numeric_observation("9", "uA", "<0.01", "mA").decision,
            "match",
        )
        self.assertEqual(
            compare_numeric_observation("10", "uA", "<0.01", "mA").decision,
            "mismatch",
        )
        self.assertEqual(
            compare_numeric_observation("0.005", "mA", "0.01mA").decision,
            "match",
        )

    def test_polarity_requires_explicit_record_evidence(self) -> None:
        unresolved = compare_numeric_observation("0.5", "mA", "0.5mA(−)")
        self.assertEqual(unresolved.decision, "manual")
        self.assertEqual(unresolved.reason_code, "numeric_polarity_unresolved")
        self.assertEqual(
            compare_numeric_observation(
                "0.5", "mA", "0.5mA(−)", record_polarity="−"
            ).decision,
            "match",
        )
        self.assertEqual(
            compare_numeric_observation(
                "0.5", "mA", "0.5mA(−)", record_polarity="+"
            ).reason_code,
            "numeric_polarity_mismatch",
        )

    def test_maximum_aggregation_converts_before_selecting(self) -> None:
        result = aggregate_numeric_observations(
            [("500", "uA"), ("0.7", "mA")], target_unit="mA"
        )
        self.assertEqual(result["decision"], "match")
        self.assertEqual(result["selected_index"], 1)
        self.assertEqual(result["selected_value"], "0.7")

    def test_aggregation_requires_units_when_values_are_mixed(self) -> None:
        unresolved = aggregate_numeric_observations([("500", "uA"), ("0.7", "mA")])
        self.assertEqual(unresolved["decision"], "manual")
        self.assertEqual(unresolved["reason_code"], "numeric_aggregation_unit_unresolved")
        incompatible = aggregate_numeric_observations(
            [("500", "uA"), ("0.7", "V")], target_unit="mA"
        )
        self.assertEqual(incompatible["decision"], "manual")
        self.assertEqual(incompatible["reason_code"], "numeric_aggregation_unit_unresolved")


if __name__ == "__main__":
    unittest.main()
