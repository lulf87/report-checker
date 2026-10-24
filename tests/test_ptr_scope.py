from __future__ import annotations

import unittest
import json
from pathlib import Path

from mvp.ptr_scope import extract_report_scope, parse_scope_expression


ROOT = Path(__file__).resolve().parents[1]


class PtrScopeExpressionTests(unittest.TestCase):
    def test_numeric_and_letter_ranges_expand(self) -> None:
        result = parse_scope_expression("2.1～2.3、2.4.1a)～c)（除生物相容性）")
        self.assertEqual(result["status"], "parsed")
        self.assertEqual(
            result["included_clause_ids"],
            ["2.1", "2.2", "2.3", "2.4.1a", "2.4.1b", "2.4.1c"],
        )
        self.assertEqual(result["excluded"][0]["raw"], "生物相容性")
        self.assertEqual(result["unresolved"], [])

    def test_only_standard_selectors_are_kept_separately(self) -> None:
        result = parse_scope_expression(
            "2.6（仅检GB 9706.1-2020 中8.7、GB 9706.202-2021 中201.8.7.1、201.8.7.3）"
        )
        self.assertEqual(result["status"], "parsed")
        self.assertEqual(result["included_clause_ids"], ["2.6"])
        self.assertEqual(
            [(item["standard"], item["clause_ids"]) for item in result["only"]],
            [
                ("GB 9706.1-2020", ["8.7"]),
                ("GB 9706.202-2021", ["201.8.7.1", "201.8.7.3"]),
            ],
        )
        self.assertEqual(result["only"][0]["applies_to_clause_ids"], ["2.6"])
        self.assertEqual(result["only"][1]["applies_to_clause_ids"], ["2.6"])

    def test_text_only_qualifier_is_preserved_without_fake_standard(self) -> None:
        result = parse_scope_expression("2.3（仅检PVC 反应）")
        self.assertEqual(result["status"], "parsed")
        self.assertEqual(result["included_clause_ids"], ["2.3"])
        self.assertEqual(result["only"][0]["kind"], "text")
        self.assertEqual(result["only"][0]["raw"], "PVC 反应")
        self.assertEqual(result["only"][0]["applies_to_clause_ids"], ["2.3"])

    def test_unresolved_clause_is_manual(self) -> None:
        result = parse_scope_expression("2.1、未知范围（除生物相容性）")
        self.assertEqual(result["status"], "manual")
        self.assertEqual(result["included_clause_ids"], ["2.1"])
        self.assertEqual(result["unresolved"][0]["reason"], "clause_token_unparsed")


@unittest.skipUnless((ROOT / "素材/report").is_dir(), "本地 Report 素材不存在")
class PtrScopeSampleTests(unittest.TestCase):
    def extract(self, sample: str) -> dict:
        report = next((ROOT / "素材/report" / sample).glob("*.pdf"))
        return extract_report_scope(report)

    def fixture(self, sample: str) -> dict:
        return json.loads((ROOT / "tests/fixtures/ptr_scope" / f"{sample}.json").read_text(encoding="utf-8"))

    def assert_matches_fixture(self, sample: str) -> None:
        result = self.extract(sample)
        expected = self.fixture(sample)
        self.assertEqual(result["schema_version"], expected["schema_version"])
        self.assertEqual(result["status"], expected["status"])
        self.assertEqual(result["raw_text"], expected["raw_text"])
        self.assertEqual(result["included_clause_ids"], expected["included_clause_ids"])
        self.assertEqual(
            [
                {"raw": item["raw"], "kind": item["kind"], "clause_ids": item.get("clause_ids", [])}
                for item in result["excluded"]
            ],
            expected["excluded"],
        )
        self.assertEqual(
            [
                {"raw": item["raw"], "standard": item["standard"], "clause_ids": item["clause_ids"]}
                for item in result["only"]
            ],
            expected["only"],
        )
        self.assertEqual(result["unresolved"], expected["unresolved"])

    def test_0596_scope_is_table_cell_and_has_explicit_exclusions(self) -> None:
        result = self.extract("0596")
        self.assert_matches_fixture("0596")
        self.assertEqual(result["status"], "parsed")
        self.assertEqual(result["source"]["pdf_page"], 3)
        self.assertTrue(result["source"]["extraction_method"].startswith("pymupdf_table_cell"))
        self.assertEqual(
            result["included_clause_ids"],
            ["2.1", "2.5", "2.6", "2.7", "2.8.1.2", "2.10", "2.11"],
        )
        self.assertEqual(len(result["excluded"]), 3)

    def test_1539_and_2795_scope_sets(self) -> None:
        expected = {
            "1539": ["2.2", "2.5", "2.6"],
            "2795": ["2.1", "2.2", "2.3", "2.4", "2.5", "2.6", "2.7"],
        }
        for sample, clauses in expected.items():
            with self.subTest(sample=sample):
                result = self.extract(sample)
                self.assert_matches_fixture(sample)
                self.assertEqual(result["status"], "parsed")
                self.assertEqual(result["included_clause_ids"], clauses)
                self.assertEqual(len(result["excluded"]), 2)

    def test_0780_keeps_deep_and_lettered_scope_items(self) -> None:
        result = self.extract("0780")
        self.assertEqual(result["status"], "parsed")
        self.assertIn("2.3.6.6a", result["included_clause_ids"])
        self.assertIn("2.3.6.6e", result["included_clause_ids"])
        self.assertIn("2.3.13.1a", result["included_clause_ids"])
        self.assertIn("2.3.13.1c", result["included_clause_ids"])
        self.assertEqual(result["only"][0]["standard"], "GB 9706.1-2020")
        self.assertEqual(result["only"][0]["applies_to_clause_ids"], ["2.6"])


if __name__ == "__main__":
    unittest.main()
