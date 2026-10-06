from __future__ import annotations

import unittest
from pathlib import Path

from mvp.checker import SAMPLE_CONFIGS
from mvp.full_record_202 import (
    RECORD202_IDENTITY_FIELDS,
    RECORD202_METADATA_FIELDS,
    compare_record_202_sample,
    _acceptance_constraints,
    _record_202_inventory_diagnostics,
)


ROOT = Path(__file__).resolve().parents[1]


class Record202ExpandedScopeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # One frozen sample is enough for the schema contract; the existing
        # full-record tests continue to exercise all three approved samples.
        cls.result = compare_record_202_sample(ROOT, "1539")

    def test_scope_coverage_is_conserved_and_categorized(self) -> None:
        coverage = self.result["scope_coverage"]
        self.assertTrue(coverage["conserved"])
        self.assertEqual(coverage["source_rows"]["eligible"], coverage["source_rows"]["accounted"])
        self.assertEqual(coverage["report_rows"]["eligible"], coverage["report_rows"]["accounted"])
        self.assertEqual(coverage["categories"]["mapping"], 175)
        self.assertEqual(coverage["categories"]["structure"], 1)
        self.assertEqual(coverage["categories"]["identity"], len(RECORD202_IDENTITY_FIELDS))
        self.assertEqual(coverage["categories"]["metadata"], len(RECORD202_METADATA_FIELDS))
        self.assertGreater(coverage["categories"]["numeric_target"], 0)

    def test_mapping_objects_keep_actual_fields_and_strict_project_decision(self) -> None:
        mappings = [entry for entry in self.result["scope_ledger"] if entry["scope_category"] == "mapping"]
        self.assertEqual(len(mappings), 175)
        for entry in mappings:
            self.assertIn(entry["rule_id"], {"RECORD202-SCOPE"})
            self.assertIn("field_comparisons", entry)
            self.assertIn("record_value", entry["field_comparisons"]["project"])
            self.assertIn("report_values", entry["field_comparisons"]["project"])
            self.assertIn("record_value", entry["field_comparisons"]["parent_clause"])
            self.assertIn("record_value", entry["field_comparisons"]["requirement"])
            self.assertIn("record_value", entry["field_comparisons"]["occurrence"])
        differences = [
            entry
            for entry in mappings
            if entry["field_comparisons"]["project"]["reason_code"] == "project_difference"
        ]
        self.assertTrue(differences)
        self.assertTrue(all(entry["disposition"] == "mismatch" for entry in differences))

    def test_numeric_targets_preserve_result_and_acceptance_text(self) -> None:
        targets = [entry for entry in self.result["scope_ledger"] if entry["scope_category"] == "numeric_target"]
        self.assertTrue(targets)
        for entry in targets:
            self.assertIn(entry["rule_id"], {"RECORD202-BODY-NUMERIC", "RECORD202-BODY-PERCENT"})
            self.assertIn("raw_result", entry["report"])
            self.assertIn("result_tokens", entry["report"])
            self.assertIn("acceptance_text", entry["report"])
            self.assertIn("acceptance_tokens", entry["report"])
            self.assertIn(entry["disposition"], {"matched", "mismatch", "manual", "excluded"})
        table3_targets = [entry for entry in targets if entry["reason_code"] == "TABLE3_OUT_OF_SCOPE"]
        self.assertTrue(table3_targets)
        self.assertTrue(all(entry["disposition"] == "excluded" for entry in table3_targets))

    def test_structure_and_fixed_fields_are_explicit_objects(self) -> None:
        structure = [entry for entry in self.result["scope_ledger"] if entry["scope_category"] == "structure"]
        self.assertEqual(len(structure), 1)
        self.assertIn("required_headers", structure[0]["record"])
        self.assertIn("formal_table_pages", structure[0]["report"])
        self.assertTrue(structure[0]["record"]["validated"])
        identity = [entry for entry in self.result["scope_ledger"] if entry["scope_category"] == "identity"]
        metadata = [entry for entry in self.result["scope_ledger"] if entry["scope_category"] == "metadata"]
        self.assertEqual({entry["record"]["field"] for entry in identity}, set(RECORD202_IDENTITY_FIELDS))
        self.assertEqual({entry["record"]["field"] for entry in metadata}, set(RECORD202_METADATA_FIELDS))
        self.assertTrue(all(entry["record"]["value"] == "" for entry in identity + metadata))
        self.assertTrue(all(entry["disposition"] in {"not_applicable", "manual"} for entry in identity + metadata))
        self.assertTrue(all(entry["report"]["field"] and "value" in entry["report"] for entry in identity + metadata))

    def test_inventory_drift_is_explicit_and_item_16_one_to_many_is_allowed(self) -> None:
        record_rows = [
            {"item": 1, "logical_row": 1},
            {"item": 16, "logical_row": 1},
        ]
        report_groups = {
            1: [[{"row_id": "report:1", "item": 119}]],
            16: [[{"row_id": "report:16a"}, {"row_id": "report:16b"}], [{"row_id": "report:16c"}]],
        }
        inventory = _record_202_inventory_diagnostics(record_rows, report_groups)
        self.assertFalse(inventory["valid"])
        self.assertTrue(any(row["reason_code"] == "record_logical_row_missing" for row in inventory["missing_record_rows"]))
        self.assertTrue(any(row["reason_code"] == "report_physical_row_missing" for row in inventory["missing_report_rows"]))
        self.assertFalse(inventory["ambiguous_rows"], "Item 16's approved one-to-many mapping is not ambiguous")

        ambiguous = _record_202_inventory_diagnostics(
            [{"item": 1, "logical_row": 1}],
            {1: [[{"row_id": "report:a"}, {"row_id": "report:b"}]]},
        )
        self.assertTrue(ambiguous["ambiguous_rows"])
        self.assertEqual(ambiguous["ambiguous_rows"][0]["reason_code"], "report_mapping_ambiguous_one_to_many")

    def test_acceptance_ledger_exposes_units_and_precision(self) -> None:
        acceptance = _acceptance_constraints("单位：mA，0.50~1.00 mA，允许误差±0.05 mA")
        self.assertEqual(acceptance["unit_context"], "mA")
        self.assertEqual(acceptance["ranges"][0]["precision"], 2)
        self.assertEqual(acceptance["tolerances"][0]["precision"], 2)
        self.assertEqual(acceptance["precision"], 2)


if __name__ == "__main__":
    unittest.main()
