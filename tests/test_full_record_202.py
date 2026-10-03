from __future__ import annotations

import unittest
from collections import Counter
from copy import deepcopy
from pathlib import Path

from mvp.checker import SAMPLE_CONFIGS, sha256_file
from mvp.full_record_202 import (
    EXPECTED_COMPARISON_UNITS,
    EXPECTED_ITEM_ROW_COUNTS,
    EXPECTED_NUMBER_ROWS,
    EXPECTED_REPORT_PHYSICAL_ROWS,
    FROZEN_RECORD_ITEM_IDENTITIES,
    _compare_one,
    _findings_from_result,
    _has_nonconforming_candidate,
    _item_template_fingerprint,
    _resolved_record_symbol,
    compare_measurements,
    compare_record_202_sample,
    run_record_202_full,
)


ROOT = Path(__file__).resolve().parents[1]


class FullRecord202Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.paths = {}
        cls.hashes = {}
        cls.results = {}
        for sample in ("1539", "2795", "2948"):
            config = SAMPLE_CONFIGS[sample]
            assert config.record202 is not None
            record_path = ROOT / config.record202
            report_path = ROOT / config.report
            cls.paths[sample] = (record_path, report_path)
            cls.hashes[sample] = (sha256_file(record_path), sha256_file(report_path))
            cls.results[sample] = compare_record_202_sample(ROOT, sample)

    def test_all_samples_emit_the_complete_table_2_ledger(self) -> None:
        for sample, result in self.results.items():
            with self.subTest(sample=sample):
                self.assertEqual(result["extraction"]["page_count"], 24)
                self.assertEqual(result["extraction"]["item_count"], 38)
                self.assertEqual(result["extraction"]["item_row_counts"], EXPECTED_ITEM_ROW_COUNTS)
                body = [row for row in result["ledger"] if row["entry_type"] == "body"]
                numbers = [row for row in result["ledger"] if row["entry_type"] == "number"]
                self.assertEqual(len(body), EXPECTED_COMPARISON_UNITS)
                self.assertEqual(len(numbers), EXPECTED_NUMBER_ROWS)
                self.assertEqual(len(result["ledger"]), 199)
                self.assertEqual(result["coverage"]["emitted_comparison_units"], 199)
                self.assertTrue(result["coverage"]["conserved"])
                self.assertEqual(sum(result["coverage"]["decision_counts"].values()), 199)
                self.assertEqual(result["coverage"]["source_rows"]["eligible"], 199)
                self.assertEqual(result["coverage"]["source_rows"]["accounted"], 199)
                self.assertEqual(result["coverage"]["report_rows"]["eligible"], 177)
                self.assertEqual(result["coverage"]["report_rows"]["accounted"], 177)
                self.assertEqual(len(result["coverage"]["source_rows"]["row_ids"]), 199)
                self.assertEqual(len(result["coverage"]["report_rows"]["row_ids"]), 177)
                self.assertEqual(result["coverage"]["entries"], 200)

    def test_measurement_column_is_located_from_header_on_six_and_seven_column_pages(self) -> None:
        for sample, result in self.results.items():
            with self.subTest(sample=sample):
                shapes = result["extraction"]["table_shapes"]
                self.assertIn(6, {shape[1] for shape in shapes.values()})
                self.assertIn(7, {shape[1] for shape in shapes.values()})
                self.assertEqual(
                    result["extraction"]["column_detection"],
                    "实测数据_header_text_and_table_geometry",
                )
                self.assertEqual(len(result["extraction"]["result_columns"]), 24)

    def test_item_8_structural_rows_are_excluded_and_item_16_is_one_to_many(self) -> None:
        for sample, result in self.results.items():
            body = [row for row in result["ledger"] if row["entry_type"] == "body"]
            item8 = [row for row in body if row["record"]["item"] == 8]
            item16 = [row for row in body if row["record"]["item"] == 16]
            with self.subTest(sample=sample):
                self.assertEqual(len(item8), 5)
                self.assertEqual([len(row["target_row_ids"]) for row in item8], [1] * 5)
                self.assertEqual(len(item16), 2)
                self.assertEqual([len(row["target_row_ids"]) for row in item16], [2, 1])
                report_row_ids = {
                    target_id for row in body for target_id in row["target_row_ids"]
                }
                self.assertEqual(len(report_row_ids), EXPECTED_REPORT_PHYSICAL_ROWS)
                self.assertTrue(all(row["mapping"]["valid"] for row in body))

    def test_status_clustering_reduces_manual_rows_on_all_frozen_samples(self) -> None:
        expected_decisions = {
            "1539": {"match": 151, "manual": 8, "mismatch": 16},
            "2795": {"match": 144, "manual": 11, "mismatch": 20},
            "2948": {"match": 145, "manual": 13, "mismatch": 17},
        }
        manual_total = 0
        for sample, result in self.results.items():
            body = [row for row in result["ledger"] if row["entry_type"] == "body"]
            decisions = Counter(row["decision"] for row in body)
            manual_total += decisions["manual"]
            with self.subTest(sample=sample):
                self.assertEqual(dict(decisions), expected_decisions[sample])
                self.assertFalse(
                    {
                        "record_ink_count_not_one",
                        "record_ink_geometry_unresolved",
                    }
                    & {
                        row["reason_code"]
                        for row in body
                        if row["decision"] == "manual"
                    }
                )
                self.assertFalse(
                    any(
                        str(key).startswith("_")
                        for row in body
                        for ink in row["record"]["inks"]
                        for key in ink
                    )
                )
        self.assertEqual(manual_total, 32)

    def test_five_frozen_open_triangles_are_resolved_without_global_relaxation(self) -> None:
        expected = {
            "2795": {(11, 15), (13, 6), (20, 3)},
            "2948": {(11, 15), (36, 5)},
        }
        for sample, keys in expected.items():
            by_key = {
                (row["record"]["item"], row["record"]["logical_row"]): row
                for row in self.results[sample]["ledger"]
                if row["entry_type"] == "body"
            }
            with self.subTest(sample=sample):
                self.assertTrue(
                    all(by_key[key]["comparison"]["record_symbol"] == "△" for key in keys)
                )

    def test_each_record_page_number_is_a_ledger_row_against_one_report_target(self) -> None:
        for sample, result in self.results.items():
            numbers = [row for row in result["ledger"] if row["entry_type"] == "number"]
            with self.subTest(sample=sample):
                self.assertEqual([row["record"]["pdf_page"] for row in numbers], list(range(1, 25)))
                self.assertEqual(len({row["source_row_id"] for row in numbers}), 24)
                self.assertEqual(len({tuple(row["target_row_ids"]) for row in numbers}), 1)
                self.assertTrue(all(row["rule_id"] == "RECORD202-NUMBER" for row in numbers))
                self.assertTrue(all(row["record_location"] and row["report_location"] for row in numbers))

    def test_2795_known_status_mismatches_and_blank_numbers(self) -> None:
        result = self.results["2795"]
        by_key = {
            (row["record"]["item"], row["record"]["logical_row"]): row
            for row in result["ledger"]
            if row["entry_type"] == "body"
        }
        clause_37_9 = by_key[(37, 10)]
        item38_first = by_key[(38, 1)]
        item38_second = by_key[(38, 2)]
        self.assertEqual(clause_37_9["comparison"]["record_symbol"], "√")
        self.assertEqual(clause_37_9["report"]["results"], ["——"])
        self.assertEqual(clause_37_9["decision"], "mismatch")
        self.assertEqual(item38_first["comparison"]["record_symbol"], "△")
        self.assertEqual(item38_second["comparison"]["record_symbol"], "△")
        self.assertEqual(item38_first["report"]["results"], ["/"])
        self.assertEqual(item38_second["report"]["results"], ["/"])
        self.assertEqual((item38_first["decision"], item38_second["decision"]), ("mismatch", "mismatch"))
        self.assertEqual(result["number_check"]["status"], "error")
        self.assertEqual(result["number_check"]["missing_pages"], list(range(1, 25)))

    def test_2948_cross_row_tick_is_owned_by_item_38_second_row(self) -> None:
        rows = {
            row["record"]["logical_row"]: row
            for row in self.results["2948"]["ledger"]
            if row["entry_type"] == "body" and row["record"]["item"] == 38
        }
        self.assertEqual(rows[1]["comparison"]["record_symbol"], "△")
        self.assertEqual(rows[1]["report"]["results"], ["/"])
        self.assertEqual(rows[1]["decision"], "mismatch")
        self.assertEqual(rows[2]["comparison"]["record_symbol"], "√")
        self.assertEqual(rows[2]["report"]["results"], ["符合要求"])
        self.assertEqual(rows[2]["decision"], "match")

    def test_table_3_is_excluded_but_table_2_reference_rows_remain(self) -> None:
        result = self.results["1539"]
        self.assertIn("GB 9706.202 table-3 documents and pages", result["scope"]["excluded"])
        references = [
            row
            for row in result["ledger"]
            if row["entry_type"] == "body" and "表3" in row["record"]["native_result_text"]
        ]
        self.assertTrue(references)
        self.assertTrue(all(row["comparison"]["numeric"]["decision"] == "excluded" for row in references))
        self.assertTrue(all(row["comparison"]["numeric"]["reason_code"] == "TABLE3_OUT_OF_SCOPE" for row in references))
        resolved_status_rows = [
            row for row in references if row["comparison"]["status"]["decision"] == "match"
        ]
        self.assertTrue(resolved_status_rows)
        self.assertTrue(all(row["decision"] == "match" for row in resolved_status_rows))
        self.assertTrue(
            all(row["reason_code"] == "record_report_status_match" for row in resolved_status_rows)
        )
        unresolved_status_rows = [
            row for row in references if row["comparison"]["status"]["decision"] == "manual"
        ]
        self.assertTrue(unresolved_status_rows)
        self.assertTrue(
            all(row["reason_code"] != "TABLE3_OUT_OF_SCOPE" for row in unresolved_status_rows)
        )

    def test_frozen_requirement_identity_and_project_name_diagnostic(self) -> None:
        for sample, result in self.results.items():
            body = [row for row in result["ledger"] if row["entry_type"] == "body"]
            with self.subTest(sample=sample):
                self.assertTrue(all(row["mapping"]["valid"] for row in body))
                project_differences = [
                    row for row in body if not row["mapping"]["project_name_match"]
                ]
                self.assertTrue(project_differences)
                self.assertTrue(all(row["decision"] == "mismatch" for row in project_differences))
                self.assertTrue(all(row["comparison"]["project"]["reason_code"] == "project_name_difference" for row in project_differences))

        item1 = [
            row["record"]
            for row in self.results["1539"]["ledger"]
            if row["entry_type"] == "body" and row["record"]["item"] == 1
        ]
        self.assertEqual(_item_template_fingerprint(item1), FROZEN_RECORD_ITEM_IDENTITIES[1])
        mutated = [{**item1[0], "requirement": "完全不同的要求"}]
        self.assertNotEqual(_item_template_fingerprint(mutated), FROZEN_RECORD_ITEM_IDENTITIES[1])

    def test_source_pdf_hashes_are_unchanged(self) -> None:
        for sample, paths in self.paths.items():
            with self.subTest(sample=sample):
                self.assertEqual(
                    (sha256_file(paths[0]), sha256_file(paths[1])),
                    self.hashes[sample],
                )

    def test_supported_si_prefix_conversion_uses_report_precision(self) -> None:
        comparison = compare_measurements("509µA", "0.5mA")
        self.assertEqual(comparison["decision"], "match")
        self.assertEqual(comparison["record_in_report_unit"], "0.509")
        self.assertEqual(comparison["rounded_to_report_precision"], "0.5")
        self.assertEqual(compare_measurements("300uA", "0.3mA")["decision"], "match")
        self.assertEqual(compare_measurements("300uA", "300mA")["decision"], "mismatch")

    def test_strict_tick_and_nonconforming_status_use_public_record_rules(self) -> None:
        base_record = {
            "inks": [{"classified_symbol": "√"}],
            "native_result_text": "",
            "project": "A",
        }
        self.assertEqual(
            _compare_one(base_record, [{"result": "-", "requirement": "", "project": "A"}])["decision"],
            "mismatch",
        )
        self.assertEqual(
            _compare_one(base_record, [{"result": "A1", "requirement": "", "project": "A"}])["decision"],
            "match",
        )
        nonconforming = _compare_one(
            {"inks": [{"classified_symbol": "×"}], "native_result_text": "", "project": "A"},
            [{"result": "不符合要求", "requirement": "", "project": "A"}],
        )
        self.assertEqual(nonconforming["decision"], "match")
        self.assertTrue(nonconforming["nonconforming_alert"])

        synthetic = deepcopy(self.results["1539"])
        body_row = next(row for row in synthetic["ledger"] if row["entry_type"] == "body")
        body_row["comparison"]["nonconforming_alert"] = True
        warnings = [
            finding
            for finding in _findings_from_result(synthetic)
            if finding["rule_id"] == "RECORD202-NONCONFORMING"
        ]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["status"], "warning")

    def test_unique_large_status_cluster_is_separated_from_written_content(self) -> None:
        inks = [
            {
                "bbox": [10, 10, 60, 60],
                "classified_symbol": "√",
                "stroke_count": 1,
            },
            {
                "bbox": [85, 18, 96, 34],
                "classified_symbol": "其他",
                "stroke_count": 1,
            },
            {
                "bbox": [99, 17, 111, 35],
                "classified_symbol": "其他",
                "stroke_count": 1,
            },
        ]
        self.assertEqual(_resolved_record_symbol(inks), ("√", None))

    def test_multiple_legal_clusters_and_cross_row_candidates_stay_manual(self) -> None:
        conflicting = [
            {
                "bbox": [10, 10, 60, 60],
                "classified_symbol": "√",
                "stroke_count": 1,
            },
            {
                "bbox": [80, 10, 110, 35],
                "classified_symbol": "△",
                "stroke_count": 1,
            },
        ]
        self.assertEqual(
            _resolved_record_symbol(conflicting),
            (None, "record_status_symbol_conflict"),
        )

        cross_row = [
            {
                "bbox": [10, 10, 60, 60],
                "classified_symbol": "√",
                "stroke_count": 1,
                "cross_row_ambiguous": True,
            },
            {
                "bbox": [85, 18, 96, 34],
                "classified_symbol": "其他",
                "stroke_count": 1,
            },
        ]
        self.assertEqual(
            _resolved_record_symbol(cross_row),
            (None, "record_ink_cross_row_ambiguous"),
        )

    def test_two_crossing_stroke_annotations_form_one_nonconforming_cluster(self) -> None:
        inks = [
            {
                "bbox": [10, 10, 40, 40],
                "classified_symbol": "/",
                "stroke_count": 1,
                "_strokes": [[(10.0, 10.0), (40.0, 40.0)]],
            },
            {
                "bbox": [10, 10, 40, 40],
                "classified_symbol": "/",
                "stroke_count": 1,
                "_strokes": [[(10.0, 40.0), (40.0, 10.0)]],
            },
        ]
        self.assertEqual(_resolved_record_symbol(inks), ("×", None))
        self.assertTrue(_has_nonconforming_candidate(inks))

        overlapping_but_parallel = [
            {
                **inks[0],
                "_strokes": [[(10.0, 10.0), (40.0, 40.0)]],
            },
            {
                **inks[1],
                "_strokes": [[(10.0, 20.0), (30.0, 40.0)]],
            },
        ]
        self.assertFalse(_has_nonconforming_candidate(overlapping_but_parallel))

    def test_five_known_open_triangle_geometry_window_is_narrow_and_single_ink_only(self) -> None:
        open_triangle = {
            "bbox": [0, 0, 31, 30],
            "classified_symbol": "其他",
            "stroke_count": 1,
            "open_ratio": 0.4079,
            "end_path_ratio": 0.1914,
        }
        self.assertEqual(_resolved_record_symbol([open_triangle]), ("△", None))
        self.assertEqual(
            _resolved_record_symbol(
                [open_triangle, {"bbox": [50, 0, 60, 10], "classified_symbol": "其他"}]
            ),
            (None, "record_status_symbol_not_found"),
        )
        not_a_triangle = {**open_triangle, "open_ratio": 0.60, "end_path_ratio": 0.30}
        self.assertEqual(
            _resolved_record_symbol([not_a_triangle]),
            (None, "record_ink_geometry_unresolved"),
        )

    def test_percentage_range_compares_both_recorded_endpoints(self) -> None:
        matched = _compare_one(
            {
                "inks": [{"classified_symbol": "√"}],
                "native_result_text": "-5%～+4%",
            },
            [{"result": "-5%～+4%", "requirement": ""}],
        )
        mismatched = _compare_one(
            {
                "inks": [{"classified_symbol": "√"}],
                "native_result_text": "-5%～+4%",
            },
            [{"result": "-4%～+4%", "requirement": ""}],
        )
        self.assertEqual(matched["numeric"]["decision"], "match")
        self.assertEqual(mismatched["numeric"]["decision"], "mismatch")

    def test_stable_run_interface_has_only_record_findings_and_two_sided_evidence(self) -> None:
        report_path = self.paths["2795"][1]
        record_path = self.paths["2795"][0]
        result = run_record_202_full(report_path, record_path)
        self.assertEqual(result["mode"], "report_record_9706_202")
        self.assertTrue(all(row["unchanged"] for row in result["source_integrity"]))
        self.assertEqual(result["coverage"]["emitted_comparison_units"], 199)
        self.assertTrue(result["ledger"])
        self.assertEqual(result["coverage"]["source_rows"]["accounted"], 199)
        self.assertEqual(result["coverage"]["report_rows"]["accounted"], 177)
        self.assertTrue(result["findings"])
        self.assertTrue(all(row["rule_id"].startswith("RECORD202-") for row in result["findings"]))
        for finding in result["findings"]:
            if finding["status"] not in {"error", "manual"}:
                continue
            self.assertEqual(
                {location["role"] for location in finding["evidence_locations"]},
                {"report", "record_9706_202"},
            )
        for entry in result["ledger"]:
            self.assertIn(entry["disposition"], {"matched", "mismatch", "manual"})
            self.assertIn("rule_id", entry)
            self.assertTrue(entry["target_row_ids"])
            if entry["disposition"] not in {"mismatch", "manual"}:
                continue
            self.assertIsNotNone(entry["record_location"])
            self.assertIsNotNone(entry["report_location"])


if __name__ == "__main__":
    unittest.main()
