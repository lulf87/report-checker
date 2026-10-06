from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf as fitz

from mvp.input_variants import Record61StatusInventoryError
from mvp.run_coordinator import _rule_execution_outcomes
from mvp.run_full_records import run_full_records
from mvp.capabilities import MODE_CATALOG


def _write_pdf(path: Path, pages: int = 8) -> None:
    with fitz.open() as document:
        for number in range(pages):
            page = document.new_page()
            page.insert_text((72, 72), f"page {number + 1}")
        document.save(path)


class InputVariantTests(unittest.TestCase):
    def test_inventory_mismatch_becomes_manual_result_with_explicit_unsupported_rules(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = root / "report.pdf"
            record = root / "record.pdf"
            output = root / "run"
            _write_pdf(report)
            _write_pdf(record)
            error = Record61StatusInventoryError(total=813, native=812, alternate=1)
            scanner = lambda **_: (_ for _ in ()).throw(error)
            with patch("mvp.run_full_records._load_scanner", return_value=scanner):
                result = run_full_records(
                    mode="report_record_9706_1",
                    report_path=report,
                    record_path=record,
                    output_dir=output,
                )
            self.assertEqual(result["machine_overall_status"], "manual")
            self.assertEqual(result["status_counts"], {"pass": 0, "warning": 0, "manual": 1, "error": 0})
            self.assertFalse(result["comparison_complete"])
            self.assertEqual(result["findings"][0]["reason_code"], "RECORD61_STATUS_INVENTORY_UNSUPPORTED")
            self.assertEqual(result["input_variant"]["inventory"]["observed"], {"total": 813, "native": 812, "alternate": 1})
            states = result["rule_execution_states"]
            self.assertEqual(states["RECORD61-STRUCTURE"]["state"], "succeeded")
            self.assertEqual(
                {item["state"] for rule_id, item in states.items() if rule_id != "RECORD61-STRUCTURE"},
                {"unsupported"},
            )
            self.assertTrue((output / "result.json").is_file())
            self.assertEqual(json.loads((output / "result.json").read_text())["input_variant"]["reason_code"], error.code)

    def test_coordinator_requires_reason_for_unsupported_and_count_for_success(self) -> None:
        rule_ids = MODE_CATALOG["report_record_9706_1"]["rule_ids"]
        counts = {rule_id: 0 for rule_id in rule_ids}
        declared = {
            rule_id: {
                "state": "succeeded" if rule_id == "RECORD61-STRUCTURE" else "unsupported",
                **({} if rule_id == "RECORD61-STRUCTURE" else {"reason_code": "RECORD61_STATUS_INVENTORY_UNSUPPORTED"}),
            }
            for rule_id in rule_ids
        }
        counts["RECORD61-STRUCTURE"] = 1
        outcomes = _rule_execution_outcomes({"rule_execution_states": declared}, counts)
        self.assertEqual(outcomes["RECORD61-STRUCTURE"]["state"], "succeeded")
        self.assertEqual(outcomes["RECORD61-BODY-STATUS"]["state"], "unsupported")
        bad = dict(declared)
        bad["RECORD61-BODY-STATUS"] = {"state": "unsupported"}
        with self.assertRaisesRegex(Exception, "requires zero Findings"):
            _rule_execution_outcomes({"rule_execution_states": bad}, counts)

    def test_other_scanner_errors_are_not_reclassified_as_input_variants(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = root / "report.pdf"
            record = root / "record.pdf"
            _write_pdf(report)
            _write_pdf(record)
            scanner = lambda **_: (_ for _ in ()).throw(ValueError("table parser broke"))
            with patch("mvp.run_full_records._load_scanner", return_value=scanner):
                with self.assertRaisesRegex(ValueError, "table parser broke"):
                    run_full_records(
                        mode="report_record_9706_1",
                        report_path=report,
                        record_path=record,
                        output_dir=root / "run",
                    )


if __name__ == "__main__":
    unittest.main()
