from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mvp.capabilities import MODE_CATALOG, RULE_CATALOG
from mvp.checker import REPORT_SELF_RULE_ORDER
from mvp.run_report_self import run_report_self


ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "素材/report/2795/QW2025-2795 Draft.pdf"


class ExpandedReportScopeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.output = Path(cls.temporary.name) / "expanded-report"
        cls.result = run_report_self(report_path=REPORT, output_dir=cls.output)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def test_twelve_rules_are_called_by_the_formal_report_only_runner(self) -> None:
        self.assertEqual(len(REPORT_SELF_RULE_ORDER), 12)
        self.assertEqual([item["id"] for item in self.result["findings"]], list(REPORT_SELF_RULE_ORDER))
        self.assertEqual(MODE_CATALOG["report_self"]["rule_ids"], REPORT_SELF_RULE_ORDER)
        self.assertTrue(all(rule_id in RULE_CATALOG for rule_id in REPORT_SELF_RULE_ORDER))
        self.assertEqual(self.result["coverage"]["planned"], 12)
        self.assertEqual(self.result["coverage"]["completed"], 12)
        self.assertEqual(self.result["coverage"]["missing"], [])

    def test_ocr_dependency_is_explicit_and_never_fabricated_as_a_pass(self) -> None:
        findings = {item["id"]: item for item in self.result["findings"]}
        r02 = findings["REPORT-R02"]["details"]["objects"]
        label_found = [item for item in r02 if item["reason_code"] == "label_photo_found_but_ocr_record_missing"]
        self.assertTrue(label_found)
        self.assertTrue(all(item["status"] == "manual" for item in label_found))
        self.assertEqual(findings["REPORT-R03"]["status"], "manual")
        self.assertEqual(findings["REPORT-R07-B"]["details"]["numeric_measurements"], 61)
        self.assertEqual(findings["REPORT-R08"]["details"]["items_checked"], 163)

    def test_identity_extension_and_issue_evidence_are_reviewable(self) -> None:
        identity = self.result["findings"][0]
        self.assertIn("产品编号/批号", identity["details"]["page3_fields"])
        self.assertIn("委托方地址", identity["details"]["page3_fields"])
        self.assertEqual(identity["details"]["missing_page3_fields"], [])
        for finding in self.result["findings"]:
            if finding["status"] not in {"error", "manual"}:
                continue
            self.assertTrue(finding["evidence"], finding["id"])
            self.assertTrue(all((self.output / evidence["image"]).is_file() for evidence in finding["evidence"]))

    def test_scope_ledger_has_all_report_scope_objects_and_locations(self) -> None:
        self.assertEqual(self.result["schema_version"], "report-self-1.1")
        scope = self.result["scope_coverage"]
        self.assertEqual(scope["scope_ids"], [f"S{number:02d}" for number in range(9, 22)])
        self.assertTrue(scope["conserved"])
        self.assertEqual(len(scope["entries"]), 13)
        self.assertTrue(all(item["eligible"] == item["accounted"] for item in scope["entries"]))
        self.assertGreaterEqual(len(self.result["ledger"]), 12)
        self.assertTrue(
            all(
                item["disposition"]
                in {"matched", "mismatch", "manual", "warning", "not_applicable"}
                for item in self.result["ledger"]
            )
        )
        self.assertGreater(
            sum(item["disposition"] == "not_applicable" for item in self.result["ledger"]),
            0,
        )
        self.assertTrue(all(item["report_location"] and item["report_location"]["pdf_page"] > 0 for item in self.result["ledger"]))
        photo_entries = [item for item in self.result["ledger"] if item["rule_id"] in {"REPORT-R02", "REPORT-R05", "REPORT-R06"}]
        self.assertTrue(photo_entries)
        self.assertTrue(any(item.get("photo_locations") for item in photo_entries))
        self.assertTrue(all(location["pdf_page"] > 0 for item in photo_entries for location in item.get("photo_locations", [])))

    def test_template_variants_discover_real_sample_objects(self) -> None:
        from mvp.checker import SAMPLE_CONFIGS
        from mvp.full_report_photo import analyze_report_photo_rules

        expected = {"1347": 4, "1539": 5, "2948": 12}
        for sample, count in expected.items():
            analysis = analyze_report_photo_rules(SAMPLE_CONFIGS[sample].report, None, None)
            with self.subTest(sample=sample):
                self.assertEqual(len(analysis["sample_rows"]), count)
                for rule_id in ("REPORT-R02", "REPORT-R03", "REPORT-R04", "REPORT-R05", "REPORT-R06"):
                    finding = next(item for item in analysis["findings"] if item["id"] == rule_id)
                    key = {"REPORT-R02": "objects", "REPORT-R03": "comparisons", "REPORT-R04": "cell_checks", "REPORT-R05": "objects", "REPORT-R06": "objects"}[rule_id]
                    self.assertGreaterEqual(len(finding["details"][key]), count)
                    self.assertEqual(
                        {item["sequence"] for item in finding["details"][key]},
                        set(range(1, count + 1)),
                    )

    def test_numeric_scope_records_discovery_state(self) -> None:
        finding = next(item for item in self.result["findings"] if item["id"] == "REPORT-R07-B")
        self.assertGreater(finding["details"]["numeric_measurements"], 0)
        self.assertFalse(finding["details"]["zero_discovery"])
        self.assertTrue(finding["details"]["comparisons"])

    def test_numeric_parser_keeps_units_ranges_and_inequality_symbols(self) -> None:
        from mvp.full_report_attempt import evaluate_numeric_measurement, numeric_result_kind

        self.assertEqual(numeric_result_kind("≤10"), "scalar")
        self.assertEqual(numeric_result_kind("1%～2%"), "range")
        self.assertEqual(
            evaluate_numeric_measurement("≤10mA", "11")["status"],
            "error",
        )


if __name__ == "__main__":
    unittest.main()
