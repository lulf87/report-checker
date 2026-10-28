from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pymupdf as fitz

from mvp.report_standard_scope import (
    ReportScopeSelectionRequired,
    extract_report_standard_scopes,
    resolve_report_scope,
    scopes_for_mode,
    validate_report_scope_for_mode,
)


class ReportStandardScopeTests(unittest.TestCase):
    def make_report(self) -> Path:
        handle = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        handle.close()
        path = Path(handle.name)
        with fitz.open() as document:
            page = document.new_page()
            page.insert_text(
                (72, 72),
                "型号规格或其他说明\n序号1~序号118是GB9706.1-2020的内容\n"
                "序号119~序号156是射频消融9706.202-2021的内容\n"
                "序号157~序号194是GB9706.202-2021的内容",
                fontname="china-s",
                fontsize=10,
            )
            document.save(path)
        self.addCleanup(path.unlink, missing_ok=True)
        return path

    def test_extracts_repeated_standard_ranges_in_declaration_order(self) -> None:
        scopes = extract_report_standard_scopes(self.make_report())
        self.assertEqual(
            [(item["standard"], item["sequence_start"], item["sequence_end"]) for item in scopes["scopes"]],
            [
                ("GB 9706.1-2020", 1, 118),
                ("GB 9706.202-2021", 119, 156),
                ("GB 9706.202-2021", 157, 194),
            ],
        )
        self.assertEqual(len(scopes_for_mode(scopes["scopes"], "report_record_9706_202")), 2)
        self.assertEqual(scopes["scopes"][0]["source"]["pdf_page"], 1)

    def test_repeated_standard_requires_selection(self) -> None:
        report = self.make_report()
        with self.assertRaises(ReportScopeSelectionRequired) as error:
            resolve_report_scope(report, mode="report_record_9706_202")
        self.assertEqual(error.exception.code, "REPORT_SCOPE_SELECTION_REQUIRED")
        self.assertEqual(
            [(item["sequence_start"], item["sequence_end"]) for item in error.exception.details["candidates"]],
            [(119, 156), (157, 194)],
        )

    def test_selected_second_range_maps_to_record_relative_scope(self) -> None:
        report = self.make_report()
        scopes = extract_report_standard_scopes(report)["scopes"]
        selected = resolve_report_scope(
            report,
            mode="report_record_9706_202",
            selection={"scope_id": scopes[2]["scope_id"]},
        )
        self.assertEqual((selected["sequence_start"], selected["sequence_end"]), (157, 194))
        validate_report_scope_for_mode(selected, "report_record_9706_202")

    def test_single_9706_1_range_is_selected_automatically(self) -> None:
        report = self.make_report()
        selected = resolve_report_scope(report, mode="report_record_9706_1")
        self.assertEqual((selected["sequence_start"], selected["sequence_end"]), (1, 118))
        validate_report_scope_for_mode(selected, "report_record_9706_1")


if __name__ == "__main__":
    unittest.main()
