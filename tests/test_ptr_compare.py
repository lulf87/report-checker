from __future__ import annotations

import unittest
from pathlib import Path

from mvp.ptr_compare import (
    EXCLUDED,
    MANUAL,
    MISMATCH,
    compare_ptr_report,
    extract_ptr_clauses,
    normalize_comparison_text,
)


ROOT = Path(__file__).resolve().parents[1]
MATERIALS_AVAILABLE = (ROOT / "素材" / "ptr").is_dir() and (ROOT / "素材" / "report").is_dir()


def sample_paths(sample: str) -> tuple[Path, Path]:
    ptr = next((ROOT / "素材" / "ptr" / sample).glob("*.pdf"))
    report = next((ROOT / "素材" / "report" / sample).glob("*.pdf"))
    return report, ptr


class PtrCompareNormalizationTests(unittest.TestCase):
    def test_layout_and_controlled_unit_spelling_are_normalized(self) -> None:
        self.assertEqual(normalize_comparison_text("10ug/g"), "10μg/g")
        self.assertEqual(normalize_comparison_text("10 μg／g"), "10μg/g")
        self.assertEqual(
            normalize_comparison_text("细菌内毒素： 应不超过 20 EU/件"),
            normalize_comparison_text("细菌内毒素 应不超过20EU/件"),
        )

    def test_numbers_and_decimal_versions_are_not_erased(self) -> None:
        self.assertIn("2.76μs", normalize_comparison_text("2.76 μs"))
        self.assertIn("9706.202-2021", normalize_comparison_text("GB 9706.202-2021"))


@unittest.skipUnless(MATERIALS_AVAILABLE, "本地 PTR/Report 素材不存在")
class PtrCompareSampleTests(unittest.TestCase):
    def test_0596_keeps_deep_clause_tree_and_evidence(self) -> None:
        _, ptr_path = sample_paths("0596")
        result = extract_ptr_clauses(ptr_path)
        self.assertEqual(result["diagnostics"]["status"], "parsed")
        self.assertEqual(result["diagnostics"]["candidate_count"], 65)
        ids = {node["clause_id"] for node in result["nodes"]}
        self.assertIn("2.8.1.2", ids)
        self.assertNotIn("2.76", ids)
        self.assertNotIn("2.0", ids)
        node = next(node for node in result["nodes"] if node["clause_id"] == "2.8.1.2")
        self.assertEqual(node["parent_clause_id"], "2.8.1")
        self.assertTrue(node["evidence"][0]["source_sha256"])
        self.assertEqual(node["evidence"][0]["role"], "ptr")

    def test_1539_known_numeric_differences_remain_hard_mismatches(self) -> None:
        report_path, ptr_path = sample_paths("1539")
        rules = compare_ptr_report(report_path, ptr_path, comparison_mode="rules")
        by_clause = {entry["clause_id"]: entry for entry in rules["coverage_ledger"]}
        self.assertEqual(rules["scope"]["included_clause_ids"], ["2.2", "2.5", "2.6"])
        self.assertEqual(by_clause["2.2.2"]["decision"], MISMATCH)
        self.assertEqual(by_clause["2.2.2"]["reason_code"], "NUMERIC_FACT_MISMATCH")
        self.assertEqual(by_clause["2.2.6"]["decision"], MISMATCH)
        self.assertEqual(by_clause["2.2.6"]["reason_code"], "NUMERIC_FACT_MISMATCH")
        self.assertEqual(by_clause["2.6"]["decision"], MISMATCH)
        self.assertEqual(by_clause["2.1"]["decision"], EXCLUDED)
        self.assertTrue(rules["summary"]["conserved"])
        self.assertFalse(rules["publishable"])

    def test_rules_plus_llm_only_reviews_text_uncertainty(self) -> None:
        report_path, ptr_path = sample_paths("1539")
        result = compare_ptr_report(report_path, ptr_path, comparison_mode="rules+llm")
        by_clause = {entry["clause_id"]: entry for entry in result["coverage_ledger"]}
        self.assertEqual(by_clause["2.2.2"]["decision"], MISMATCH)
        self.assertEqual(by_clause["2.2.6"]["decision"], MISMATCH)
        self.assertEqual(by_clause["2.6"]["decision"], MANUAL)
        self.assertEqual(by_clause["2.6"]["reason_code"], "LLM_REVIEW_PENDING")
        self.assertFalse(result["llm"]["used"])
        self.assertFalse(result["publishable"])

    def test_2795_reports_unit_conflict_and_table_manual_queue(self) -> None:
        report_path, ptr_path = sample_paths("2795")
        result = compare_ptr_report(report_path, ptr_path)
        by_clause = {entry["clause_id"]: entry for entry in result["coverage_ledger"]}
        self.assertEqual(
            result["scope"]["included_clause_ids"],
            ["2.1", "2.2", "2.3", "2.4", "2.5", "2.6", "2.7"],
        )
        self.assertEqual(by_clause["2.5"]["decision"], MISMATCH)
        self.assertEqual(by_clause["2.5"]["reason_code"], "UNIT_MISMATCH")
        self.assertEqual(by_clause["2.6.1"]["decision"], MANUAL)
        self.assertEqual(by_clause["2.6.1"]["reason_code"], "TABLE_CONTENT_NOT_EXTRACTED")
        self.assertEqual(result["summary"]["eligible"], result["summary"]["accounted"])
        self.assertTrue(all(entry["evidence"] for entry in result["coverage_ledger"]))

    def test_5782_without_text_layer_is_manual_instead_of_missing_or_mismatch(self) -> None:
        report_path, ptr_path = sample_paths("5782")
        result = compare_ptr_report(report_path, ptr_path)
        self.assertEqual(result["ptr"]["diagnostics"]["reason_code"], "PTR_TEXT_LAYER_MISSING")
        eligible = [entry for entry in result["coverage_ledger"] if entry["decision"] != EXCLUDED]
        self.assertTrue(eligible)
        self.assertTrue(all(entry["decision"] == MANUAL for entry in eligible))
        self.assertEqual(result["summary"]["counts"][MISMATCH], 0)
        self.assertEqual(result["summary"]["decision"], MANUAL)
        self.assertFalse(result["publishable"])


if __name__ == "__main__":
    unittest.main()
