from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mvp.checker import SAMPLE_CONFIGS, sha256_file
from mvp.run_full_2795 import REPORT_RULE_ORDER, run_full_2795_attempt


ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / SAMPLE_CONFIGS["2795"].report
RECORD = ROOT / SAMPLE_CONFIGS["2795"].record202


class Full2795AttemptIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.output_dir = Path(cls.temporary.name) / "full-attempt"
        cls.report_hash_before = sha256_file(REPORT)
        cls.record_hash_before = sha256_file(RECORD)
        cls.manifest = run_full_2795_attempt(ROOT, cls.output_dir)
        cls.report_result = json.loads(
            (cls.output_dir / "report-self/result.json").read_text(encoding="utf-8")
        )
        cls.record_result = json.loads(
            (cls.output_dir / "report-record-9706-202/result.json").read_text(
                encoding="utf-8"
            )
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def test_report_self_attempts_all_twelve_logical_rules(self) -> None:
        findings = self.report_result["findings"]
        self.assertEqual([item["id"] for item in findings], list(REPORT_RULE_ORDER))
        self.assertEqual(self.report_result["coverage"]["planned"], 12)
        self.assertEqual(self.report_result["coverage"]["completed"], 12)
        self.assertEqual(
            self.report_result["status_counts"],
            {"pass": 7, "warning": 0, "manual": 4, "error": 1},
        )
        self.assertEqual(self.report_result["machine_overall_status"], "error")

    def test_expected_2795_rule_statuses_and_counts(self) -> None:
        findings = {item["id"]: item for item in self.report_result["findings"]}
        self.assertEqual(
            {rule_id: findings[rule_id]["status"] for rule_id in REPORT_RULE_ORDER},
            {
                "REPORT-R01": "pass",
                "REPORT-R02": "manual",
                "REPORT-R03": "manual",
                "REPORT-R04": "manual",
                "REPORT-R05": "pass",
                "REPORT-R06": "pass",
                "REPORT-R07": "error",
                "REPORT-R07-B": "manual",
                "REPORT-R08": "pass",
                "REPORT-R09": "pass",
                "REPORT-R10": "pass",
                "REPORT-R11": "pass",
            },
        )
        self.assertEqual(
            findings["REPORT-R07-B"]["details"]["counts"],
            {"pass": 53, "error": 0, "manual": 8},
        )
        self.assertEqual(findings["REPORT-R08"]["details"]["items_checked"], 163)

    def test_record_run_is_independent_from_report_self_check(self) -> None:
        findings = self.record_result["findings"]
        self.assertEqual(
            [item["id"] for item in findings],
            ["RECORD202-NUMBER", "RECORD202-SYMBOLS"],
        )
        self.assertFalse(any(item["id"].startswith("REPORT-") for item in findings))
        self.assertEqual(self.record_result["status_counts"]["error"], 2)
        self.assertEqual(self.record_result["machine_overall_status"], "error")
        self.assertEqual(
            {
                evidence["document_role"]
                for finding in findings
                for evidence in finding["evidence"]
            },
            {"report", "record_9706_202"},
        )
        number = next(item for item in findings if item["id"] == "RECORD202-NUMBER")
        page_states = number["details"]["page_states"]
        self.assertEqual([item["pdf_page"] for item in page_states], list(range(1, 25)))
        self.assertTrue(all(item["evidence_rects"] for item in page_states))
        self.assertTrue(
            all(item["document_role"] == "record_9706_202" for item in page_states)
        )

    def test_every_report_error_or_manual_has_rendered_evidence(self) -> None:
        run_dir = self.output_dir / "report-self"
        for finding in self.report_result["findings"]:
            if finding["status"] not in {"error", "manual"}:
                continue
            self.assertTrue(finding["evidence"], finding["id"])
            for evidence in finding["evidence"]:
                image = run_dir / evidence["image"]
                self.assertTrue(image.is_file(), image)
                self.assertGreater(image.stat().st_size, 0)
                self.assertTrue(evidence["rects"])
                self.assertEqual(evidence["document_role"], "report")

    def test_ocr_cache_is_bound_to_embedded_report_images(self) -> None:
        binding = self.report_result["artifacts"]["ocr_source_binding"]
        self.assertEqual(binding["status"], "pass")
        self.assertEqual(binding["image_bindings_checked"], 17)
        self.assertTrue(
            all(item["matches_embedded_report_image"] for item in binding["image_bindings"])
        )

    def test_original_files_and_output_contract(self) -> None:
        self.assertEqual(sha256_file(REPORT), self.report_hash_before)
        self.assertEqual(sha256_file(RECORD), self.record_hash_before)
        self.assertTrue(all(item["unchanged"] for item in self.manifest["source_integrity"]))
        for path in (
            self.output_dir / "manifest.json",
            self.output_dir / "index.html",
            self.output_dir / "report-self/index.html",
            self.output_dir / "report-record-9706-202/index.html",
        ):
            self.assertTrue(path.is_file(), path)
            self.assertGreater(path.stat().st_size, 0)


if __name__ == "__main__":
    unittest.main()
