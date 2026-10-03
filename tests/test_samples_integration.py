import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf

from mvp.checker import (
    SAMPLE_CONFIGS,
    SourceFile,
    cover_value_after_label,
    page_three_fields,
    record_202_check,
    record_61_check,
    scan_report_items,
    sha256_file,
)


ROOT = Path(__file__).resolve().parents[1]


class MultiSampleIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.report_data = {}
        for sample in ("1539", "2948", "2795", "1347"):
            report_path = ROOT / SAMPLE_CONFIGS[sample].report
            with pymupdf.open(report_path) as document:
                cls.report_data[sample] = scan_report_items(document)

    def test_report_structure_and_r07_matrix(self) -> None:
        expected = {
            "1539": (159, 93, []),
            "2948": (162, 93, [132, 140]),
            "2795": (163, 91, [3]),
            "1347": (157, 91, []),
        }
        for sample, (item_count, table_pages, mismatch_sequences) in expected.items():
            with self.subTest(sample=sample):
                items, diagnostics = self.report_data[sample]
                self.assertEqual(len(items), item_count)
                self.assertEqual(len(diagnostics["page_sequences"]), table_pages)
                self.assertEqual(diagnostics["sequence_gaps"], [])
                self.assertEqual(diagnostics["continuation_errors"], [])
                self.assertEqual(
                    [item["sequence"] for item in diagnostics["result_mismatches"]],
                    mismatch_sequences,
                )

        mismatch_132, mismatch_140 = self.report_data["2948"][1]["result_mismatches"]
        self.assertEqual((mismatch_132["expected"], mismatch_132["actual"]), ("符合", "/"))
        self.assertEqual(mismatch_132["pdf_pages"], [89, 90])
        self.assertEqual((mismatch_140["expected"], mismatch_140["actual"]), ("/", "符合"))
        self.assertEqual(mismatch_140["results"], ["——"] * 8)

        mismatch_3 = self.report_data["2795"][1]["result_mismatches"][0]
        self.assertTrue(mismatch_3["conclusion_conflict"])
        self.assertEqual(mismatch_3["all_conclusions"], ["/", "符合"])
        self.assertEqual(mismatch_3["pdf_pages"], [16, 17])

    def test_1347_cover_value_on_same_line(self) -> None:
        report_path = ROOT / SAMPLE_CONFIGS["1347"].report
        with pymupdf.open(report_path) as document:
            value, rect = cover_value_after_label(document[0], "样品名称")
            page_three_value = page_three_fields(document[2])["样品名称"][0]
        self.assertEqual(value, "一次性使用压力监测磁定位射频消融导管")
        self.assertIsNotNone(rect)
        self.assertEqual(page_three_value, value)

    def test_202_continuation_and_mismatch_matrix(self) -> None:
        expected_symbols = {
            "1539": ["√"] + ["△"] * 9,
            "2948": ["△"] * 10,
            "2795": ["△"] * 9 + ["√"],
        }
        expected_status = {"1539": "pass", "2948": "pass", "2795": "error"}
        clauses = [
            "201.15.101.1",
            "201.15.101.2",
            "201.15.101.3",
            "201.15.101.4",
            "201.15.101.5",
            "201.15.101.6",
            "201.15.101.6",
            "201.15.101.7",
            "201.15.101.8",
            "201.15.101.9",
        ]
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            for sample in ("1539", "2948", "2795"):
                config = SAMPLE_CONFIGS[sample]
                report = SourceFile("report", ROOT / config.report)
                record = SourceFile("record_9706_202", ROOT / config.record202)
                findings = record_202_check(
                    record,
                    report,
                    temporary_root / sample,
                    self.report_data[sample][0],
                )
                number = next(item for item in findings if item["id"] == "RECORD202-NUMBER")
                symbols = next(item for item in findings if item["id"] == "RECORD202-SYMBOLS")
                comparisons = symbols["details"]["comparisons"]
                with self.subTest(sample=sample):
                    self.assertEqual(len(comparisons), 10)
                    self.assertEqual([item["clause"] for item in comparisons], clauses)
                    self.assertEqual([item["record_pdf_page"] for item in comparisons], [23] * 7 + [24] * 3)
                    self.assertEqual([item["record_symbol"] for item in comparisons], expected_symbols[sample])
                    self.assertEqual(symbols["status"], expected_status[sample])
                    self.assertTrue(symbols["details"]["legend_validated"])
                    self.assertEqual(symbols["details"]["legend_source"], "native_pdf_text")
                    self.assertEqual(symbols["details"]["unresolved_count"], 0)
                    for evidence in symbols["evidence"]:
                        self.assertGreater(Path(evidence["image"]).stat().st_size, 0)
                if sample == "2795":
                    self.assertEqual(number["status"], "error")
                    self.assertEqual(number["details"]["matched_pages"], 0)
                    self.assertEqual(number["details"]["missing_pages"], list(range(1, 25)))
                    self.assertEqual(symbols["details"]["mismatch_count"], 1)
                    mismatch = symbols["details"]["mismatches"][0]
                    self.assertEqual(
                        (
                            mismatch["clause"],
                            mismatch["record_pdf_page"],
                            mismatch["record_symbol"],
                            mismatch["report_pdf_page"],
                            mismatch["report_result"],
                        ),
                        ("201.15.101.9", 24, "√", 104, "——"),
                    )
                else:
                    self.assertEqual(number["status"], "pass")

    def test_202_unverified_legend_never_auto_decides(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            with (
                patch("mvp.checker.verified_202_legend_mapping", return_value=None),
                patch("mvp.checker.local_text_ocr", return_value={"lines": []}),
            ):
                for sample in ("1539", "2795"):
                    config = SAMPLE_CONFIGS[sample]
                    findings = record_202_check(
                        SourceFile("record_9706_202", ROOT / config.record202),
                        SourceFile("report", ROOT / config.report),
                        temporary_root / sample,
                        self.report_data[sample][0],
                    )
                    symbols = next(item for item in findings if item["id"] == "RECORD202-SYMBOLS")
                    with self.subTest(sample=sample):
                        self.assertEqual(symbols["status"], "manual")
                        self.assertFalse(symbols["details"]["legend_validated"])
                        self.assertEqual(symbols["details"]["legend_source"], "unresolved")
                        self.assertIsNone(symbols["details"]["legend_mapping"])
                        self.assertEqual(symbols["details"]["matched_count"], 0)
                        self.assertEqual(symbols["details"]["mismatch_count"], 0)
                        self.assertEqual(symbols["details"]["unresolved_count"], 10)
                        self.assertTrue(all(item["decision"] == "manual" for item in symbols["details"]["comparisons"]))

    def test_1347_report_driven_numeric_probe(self) -> None:
        config = SAMPLE_CONFIGS["1347"]
        report = SourceFile("report", ROOT / config.report)
        record = SourceFile("record_9706_1", ROOT / config.record61)
        before = sha256_file(record.path)
        with tempfile.TemporaryDirectory() as temporary:
            findings = record_61_check(
                record,
                report,
                self.report_data["1347"][0],
                Path(temporary),
            )
            status = next(item for item in findings if item["id"] == "RECORD61-STATUS")
            numeric = next(item for item in findings if item["id"] == "RECORD61-NUMERIC")
            self.assertEqual(status["status"], "pass")
            self.assertEqual(numeric["status"], "manual")
            self.assertEqual(numeric["details"]["probe_id"], "patient-leakage-after-sfc-cf-ac")
            self.assertEqual(numeric["details"]["record_pdf_page"], 107)
            self.assertEqual(len(numeric["details"]["measurement_cells"]), 4)
            self.assertEqual(numeric["details"]["report_pdf_page"], 33)
            self.assertEqual(numeric["details"]["report_value"], "0.01")
            self.assertIsNone(numeric["details"]["accepted_record_value"])
            self.assertIsNone(numeric["details"]["automatic_comparison"])
            self.assertEqual(numeric["details"]["skipped_supported_probes"][0]["report_value"], "——")
        self.assertEqual(sha256_file(record.path), before)


if __name__ == "__main__":
    unittest.main()
