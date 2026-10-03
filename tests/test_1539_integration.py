import unittest
from pathlib import Path

import pymupdf

from mvp.checker import report_202_rows, scan_report_items


ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "素材/report/1539/QW2025-1539 Draft.pdf"


class Sample1539IntegrationTests(unittest.TestCase):
    def test_report_sequence_and_result_aggregation(self) -> None:
        with pymupdf.open(REPORT) as document:
            items, diagnostics = scan_report_items(document)

        self.assertEqual(sorted(items), list(range(1, 160)))
        self.assertEqual(len(diagnostics["page_sequences"]), 93)
        self.assertTrue(all(page["explicit"] for page in diagnostics["page_sequences"]))
        self.assertEqual(diagnostics["sequence_gaps"], [])
        self.assertEqual(diagnostics["continuation_errors"], [])
        self.assertEqual(diagnostics["result_mismatches"], [])
        self.assertEqual(
            diagnostics["expected_conclusion_counts"],
            {"符合": 104, "/": 55, "不符合": 0, "缺失": 0},
        )

    def test_202_mapping_ignores_cross_references(self) -> None:
        with pymupdf.open(REPORT) as document:
            index = report_202_rows(document)

        expected = {
            "201.15.101.1": [(97, "符合要求")],
            "201.15.101.2": [(97, "——")],
            "201.15.101.3": [(97, "——")],
            "201.15.101.4": [(98, "——")],
            "201.15.101.5": [(98, "——")],
            "201.15.101.6": [(98, "——"), (98, "——")],
            "201.15.101.7": [(98, "——")],
            "201.15.101.8": [(98, "——")],
            "201.15.101.9": [(98, "——")],
        }
        actual = {
            clause: [(row["pdf_page"], row["result"]) for row in rows]
            for clause, rows in index.items()
        }
        self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
