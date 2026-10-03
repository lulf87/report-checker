from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf as fitz

from mvp.run_full_records import main, run_full_records


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_pdf(path: Path, label: str) -> None:
    with fitz.open() as document:
        for page_number in range(1, 9):
            page = document.new_page()
            page.insert_text((72, 72), f"{label} {page_number}")
        document.save(path)


def _scanner_payload(prefix: str) -> dict:
    record_role = "record_9706_1" if prefix == "RECORD61-" else "record_9706_202"
    return {
        "findings": [
            {
                "id": f"{prefix}BODY:p002:r001",
                "rule_id": f"{prefix}BODY",
                "status": "error",
                "title": "Record与Report不一致",
                "summary": "状态不一致",
                "evidence_locations": [
                    {"role": "report", "pdf_page": 1, "bbox": [50, 60, 70, 80]},
                    {"role": record_role, "pdf_page": 1, "bbox": [10, 20, 30, 40]},
                ],
            },
            {
                "id": f"{prefix}NUMERIC:p003:r002",
                "rule_id": f"{prefix}NUMERIC",
                "status": "manual",
                "title": "数值待复核",
                "summary": "手写值不清晰",
                "evidence_locations": [
                    {"role": "report", "pdf_page": 1, "bbox": [51, 61, 71, 81]},
                    {"role": record_role, "pdf_page": 1, "bbox": [11, 21, 31, 41]},
                ],
            },
            {
                "id": f"{prefix}BODY:p004:r003",
                "rule_id": f"{prefix}BODY",
                "status": "pass",
                "title": "Record与Report一致",
                "summary": "状态一致",
                "evidence_locations": [
                    {"role": "report", "pdf_page": 1, "bbox": [52, 62, 72, 82]},
                    {"role": record_role, "pdf_page": 1, "bbox": [12, 22, 32, 42]},
                ],
            },
        ],
        "ledger": [
            {
                "entry_id": "L1",
                "rule_id": f"{prefix}BODY",
                "source_row_id": "record-row-1",
                "target_row_id": "report-row-1",
                "disposition": "mismatch",
                "reason_code": "status_result_mismatch",
                "record_location": {"pdf_page": 2, "bbox": [10, 20, 30, 40]},
                "report_location": {"pdf_page": 5, "bbox": [50, 60, 70, 80]},
            },
            {
                "entry_id": "L2",
                "rule_id": f"{prefix}NUMERIC",
                "source_row_id": "record-row-2",
                "target_row_id": "report-row-2",
                "disposition": "manual",
                "reason_code": "handwriting_unresolved",
                "record_evidence": {"pdf_page": 3, "rects": [[11, 21, 31, 41]]},
                "report_evidence": {"pdf_page": 6, "rects": [[51, 61, 71, 81]]},
            },
            {
                "entry_id": "L3",
                "rule_id": f"{prefix}BODY",
                "source_row_id": "record-row-3",
                "target_row_id": "report-row-3",
                "disposition": "matched",
                "reason_code": "status_result_matched",
                "record_location": {"pdf_page": 4, "bbox": [12, 22, 32, 42]},
                "report_location": {"pdf_page": 7, "bbox": [52, 62, 72, 82]},
            },
        ],
        "coverage": {
            "source_rows": {
                "eligible": 3,
                "accounted": 3,
                "conserved": True,
                "row_ids": ["record-row-1", "record-row-2", "record-row-3"],
                "dispositions": {"matched": 1, "mismatch": 1, "manual": 1, "not_applicable": 0, "excluded": 0},
            },
            "report_rows": {
                "eligible": 3,
                "accounted": 3,
                "conserved": True,
                "row_ids": ["report-row-1", "report-row-2", "report-row-3"],
                "dispositions": {"matched": 1, "mismatch": 1, "manual": 1, "not_applicable": 0, "excluded": 0},
            },
        },
    }


class RunFullRecordsContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.report = self.root / "report.pdf"
        self.record = self.root / "record.pdf"
        _write_pdf(self.report, "report")
        _write_pdf(self.record, "record")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _run(self, mode: str) -> tuple[dict, list[dict]]:
        calls: list[dict] = []
        prefix = "RECORD61-" if mode.endswith("9706_1") else "RECORD202-"

        def scanner(**kwargs):
            calls.append(kwargs)
            return _scanner_payload(prefix)

        function_name = "run_record_61_full" if mode.endswith("9706_1") else "run_record_202_full"
        module = types.SimpleNamespace(**{function_name: scanner})
        output = self.root / mode
        with patch("mvp.run_full_records.importlib.import_module", return_value=module) as importer:
            result = run_full_records(
                mode=mode,
                report_path=self.report,
                record_path=self.record,
                output_dir=output,
            )
        expected_module = "mvp.full_record_61" if mode.endswith("9706_1") else "mvp.full_record_202"
        importer.assert_called_once_with(expected_module)
        return result, calls

    def test_modes_lazy_load_only_their_own_scanner(self) -> None:
        for mode in ("report_record_9706_1", "report_record_9706_202"):
            with self.subTest(mode=mode):
                result, calls = self._run(mode)
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0]["report_path"], self.report.resolve())
                self.assertEqual(calls[0]["record_path"], self.record.resolve())
                self.assertEqual(result["mode"], mode)
                self.assertFalse(result["engine"]["llm_used"])
                self.assertFalse(result["engine"]["agent_used"])

    def test_writes_json_and_html_with_counts_coverage_and_both_locations(self) -> None:
        result, _ = self._run("report_record_9706_1")
        output = self.root / "report_record_9706_1"
        saved = json.loads((output / "result.json").read_text(encoding="utf-8"))
        page = (output / "index.html").read_text(encoding="utf-8")
        self.assertEqual(saved["status_counts"], {"pass": 1, "warning": 0, "manual": 1, "error": 1})
        self.assertEqual(
            saved["ledger_status_counts"],
            {
                "pass": 1,
                "warning": 0,
                "manual": 1,
                "error": 1,
                "not_applicable": 0,
                "excluded": 0,
            },
        )
        self.assertEqual(saved["machine_overall_status"], "error")
        self.assertEqual(result["coverage"]["source_rows"]["accounted"], 3)
        self.assertIn("Coverage守恒", page)
        self.assertIn("Record第2页 / bbox [10.0,20.0,30.0,40.0]", page)
        self.assertIn("Report第5页 / bbox [50.0,60.0,70.0,80.0]", page)
        self.assertIn("Record第3页 / bbox [11.0,21.0,31.0,41.0]", page)
        self.assertIn("Report第6页 / bbox [51.0,61.0,71.0,81.0]", page)
        self.assertNotIn("REPORT-R", page)

    def test_rejects_report_self_findings(self) -> None:
        payload = _scanner_payload("RECORD61-")
        payload["findings"][0]["id"] = "REPORT-R07"
        module = types.SimpleNamespace(run_record_61_full=lambda **_: payload)
        with patch("mvp.run_full_records.importlib.import_module", return_value=module):
            with self.assertRaisesRegex(ValueError, "outside the selected mode"):
                run_full_records(
                    mode="report_record_9706_1",
                    report_path=self.report,
                    record_path=self.record,
                    output_dir=self.root / "bad-report-rule",
                )

    def test_rejects_unconserved_or_empty_coverage(self) -> None:
        for coverage in (
            {
                "source_rows": {"eligible": 3, "accounted": 2, "conserved": False, "row_ids": ["record-row-1", "record-row-2", "record-row-3"], "dispositions": {}},
                "report_rows": {"eligible": 3, "accounted": 3, "conserved": True, "row_ids": ["report-row-1", "report-row-2", "report-row-3"], "dispositions": {}},
            },
            {
                "source_rows": {"eligible": 0, "accounted": 0, "conserved": True, "row_ids": [], "dispositions": {"matched": 0, "mismatch": 0, "manual": 0, "not_applicable": 0, "excluded": 0}},
                "report_rows": {"eligible": 0, "accounted": 0, "conserved": True, "row_ids": [], "dispositions": {"matched": 0, "mismatch": 0, "manual": 0, "not_applicable": 0, "excluded": 0}},
            },
        ):
            with self.subTest(coverage=coverage):
                payload = _scanner_payload("RECORD202-")
                payload["coverage"] = coverage
                module = types.SimpleNamespace(run_record_202_full=lambda **_: payload)
                with patch("mvp.run_full_records.importlib.import_module", return_value=module):
                    with self.assertRaises(ValueError):
                        run_full_records(
                            mode="report_record_9706_202",
                            report_path=self.report,
                            record_path=self.record,
                            output_dir=self.root / "bad-coverage",
                        )

    def test_error_and_manual_require_report_and_record_page_bbox(self) -> None:
        payload = _scanner_payload("RECORD61-")
        del payload["ledger"][0]["record_location"]
        module = types.SimpleNamespace(run_record_61_full=lambda **_: payload)
        with patch("mvp.run_full_records.importlib.import_module", return_value=module):
            with self.assertRaisesRegex(ValueError, "record"):
                run_full_records(
                    mode="report_record_9706_1",
                    report_path=self.report,
                    record_path=self.record,
                    output_dir=self.root / "missing-location",
                )

    def test_rejects_self_reported_coverage_that_does_not_match_ledger(self) -> None:
        payload = _scanner_payload("RECORD202-")
        payload["ledger"] = payload["ledger"][:1]
        module = types.SimpleNamespace(run_record_202_full=lambda **_: payload)
        with patch("mvp.run_full_records.importlib.import_module", return_value=module):
            with self.assertRaisesRegex(ValueError, "ledger mismatch"):
                run_full_records(
                    mode="report_record_9706_202",
                    report_path=self.report,
                    record_path=self.record,
                    output_dir=self.root / "forged-coverage",
                )

    def test_recomputes_one_to_many_report_row_coverage(self) -> None:
        payload = _scanner_payload("RECORD202-")
        payload["ledger"][0]["target_row_ids"] = ["report-row-1", "report-row-1b"]
        payload["coverage"]["report_rows"].update(
            {
                "eligible": 4,
                "accounted": 4,
                "row_ids": [
                    "report-row-1",
                    "report-row-1b",
                    "report-row-2",
                    "report-row-3",
                ],
                "dispositions": {
                    "matched": 1,
                    "mismatch": 2,
                    "manual": 1,
                    "not_applicable": 0,
                    "excluded": 0,
                },
            }
        )
        module = types.SimpleNamespace(run_record_202_full=lambda **_: payload)
        with patch("mvp.run_full_records.importlib.import_module", return_value=module):
            result = run_full_records(
                mode="report_record_9706_202",
                report_path=self.report,
                record_path=self.record,
                output_dir=self.root / "one-to-many",
            )
        self.assertEqual(result["coverage"]["source_rows"]["eligible"], 3)
        self.assertEqual(result["coverage"]["report_rows"]["eligible"], 4)

    def test_rejects_out_of_bounds_evidence_and_missing_ledger_rule(self) -> None:
        payload = _scanner_payload("RECORD61-")
        payload["ledger"][0]["record_location"]["pdf_page"] = 999
        module = types.SimpleNamespace(run_record_61_full=lambda **_: payload)
        with patch("mvp.run_full_records.importlib.import_module", return_value=module):
            with self.assertRaisesRegex(ValueError, "record"):
                run_full_records(
                    mode="report_record_9706_1",
                    report_path=self.report,
                    record_path=self.record,
                    output_dir=self.root / "bad-evidence-page",
                )

        payload = _scanner_payload("RECORD61-")
        del payload["ledger"][0]["rule_id"]
        module = types.SimpleNamespace(run_record_61_full=lambda **_: payload)
        with patch("mvp.run_full_records.importlib.import_module", return_value=module):
            with self.assertRaisesRegex(ValueError, "outside the selected mode"):
                run_full_records(
                    mode="report_record_9706_1",
                    report_path=self.report,
                    record_path=self.record,
                    output_dir=self.root / "missing-ledger-rule",
                )

    def test_rejects_invalid_primary_location_even_with_valid_fallback(self) -> None:
        payload = _scanner_payload("RECORD61-")
        payload["ledger"][1]["record_location"] = {
            "pdf_page": 999,
            "bbox": [1, 2, 3, 4],
        }
        module = types.SimpleNamespace(run_record_61_full=lambda **_: payload)
        with patch("mvp.run_full_records.importlib.import_module", return_value=module):
            with self.assertRaisesRegex(ValueError, "record_location"):
                run_full_records(
                    mode="report_record_9706_1",
                    report_path=self.report,
                    record_path=self.record,
                    output_dir=self.root / "invalid-primary-valid-fallback",
                )

    def test_rejects_invalid_location_on_matched_entry(self) -> None:
        payload = _scanner_payload("RECORD202-")
        payload["ledger"][2]["record_location"] = {
            "pdf_page": True,
            "bbox": [12, 22, 32, 42],
        }
        module = types.SimpleNamespace(run_record_202_full=lambda **_: payload)
        with patch("mvp.run_full_records.importlib.import_module", return_value=module):
            with self.assertRaisesRegex(ValueError, "record_location"):
                run_full_records(
                    mode="report_record_9706_202",
                    report_path=self.report,
                    record_path=self.record,
                    output_dir=self.root / "invalid-matched-location",
                )

    def test_checks_source_hash_when_scanner_raises(self) -> None:
        def scanner(**kwargs):
            record_path = Path(kwargs["record_path"])
            with fitz.open() as replacement:
                replacement.new_page().insert_text((72, 72), "changed")
                replacement.save(record_path.with_suffix(".replacement.pdf"))
            record_path.write_bytes(record_path.with_suffix(".replacement.pdf").read_bytes())
            raise RuntimeError("scanner failed")

        module = types.SimpleNamespace(run_record_61_full=scanner)
        with patch("mvp.run_full_records.importlib.import_module", return_value=module):
            with self.assertRaisesRegex(RuntimeError, "changed during a failed"):
                run_full_records(
                    mode="report_record_9706_1",
                    report_path=self.report,
                    record_path=self.record,
                    output_dir=self.root / "scanner-failure",
                )

    def test_rejects_forged_edges_ids_counts_warning_and_mode(self) -> None:
        base = _scanner_payload("RECORD61-")
        cases: list[tuple[str, dict]] = []

        duplicate_edge = copy.deepcopy(base)
        duplicate = copy.deepcopy(duplicate_edge["ledger"][0])
        duplicate["entry_id"] = "L1-duplicate-edge"
        duplicate_edge["ledger"].append(duplicate)
        cases.append(("duplicate coverage edge", duplicate_edge))

        missing_row = copy.deepcopy(base)
        missing_row["coverage"]["source_rows"]["row_ids"].append("record-row-missing")
        missing_row["coverage"]["source_rows"]["eligible"] = 4
        missing_row["coverage"]["source_rows"]["accounted"] = 4
        cases.append(("ledger mismatch", missing_row))

        forged_dispositions = copy.deepcopy(base)
        forged_dispositions["coverage"]["report_rows"]["dispositions"].update(
            {"matched": 3, "mismatch": 0, "manual": 0}
        )
        cases.append(("dispositions", forged_dispositions))

        warning_without_evidence = copy.deepcopy(base)
        warning_without_evidence["findings"].append(
            {
                "id": "RECORD61-NONCONFORMING:test",
                "rule_id": "RECORD61-NONCONFORMING",
                "status": "warning",
                "title": "不符合提示",
            }
        )
        cases.append(("lacks evidence_locations", warning_without_evidence))

        wrong_mode_rule = copy.deepcopy(base)
        wrong_mode_rule["ledger"][0]["rule_id"] = "RECORD202-BODY"
        cases.append(("outside the selected mode", wrong_mode_rule))

        for expected, payload in cases:
            with self.subTest(expected=expected):
                module = types.SimpleNamespace(run_record_61_full=lambda **_: payload)
                with patch("mvp.run_full_records.importlib.import_module", return_value=module):
                    with self.assertRaisesRegex(ValueError, expected):
                        run_full_records(
                            mode="report_record_9706_1",
                            report_path=self.report,
                            record_path=self.record,
                            output_dir=self.root / f"forged-{len(expected)}",
                        )

    def test_finding_rects_are_all_checked(self) -> None:
        payload = _scanner_payload("RECORD202-")
        payload["findings"][0]["evidence_locations"][0] = {
            "role": "report",
            "pdf_page": 1,
            "rects": [[1, 1, 2, 2], [1, 1, 9999, 9999]],
        }
        module = types.SimpleNamespace(run_record_202_full=lambda **_: payload)
        with patch("mvp.run_full_records.importlib.import_module", return_value=module):
            with self.assertRaisesRegex(ValueError, "out of bounds"):
                run_full_records(
                    mode="report_record_9706_202",
                    report_path=self.report,
                    record_path=self.record,
                    output_dir=self.root / "bad-finding-rects",
                )

    def test_source_pdfs_remain_unchanged(self) -> None:
        before = (_sha256(self.report), _sha256(self.record))
        result, _ = self._run("report_record_9706_202")
        self.assertEqual((_sha256(self.report), _sha256(self.record)), before)
        self.assertTrue(all(item["unchanged"] for item in result["source_integrity"]))

    def test_cli_contract(self) -> None:
        module = types.SimpleNamespace(
            run_record_61_full=lambda **_: _scanner_payload("RECORD61-")
        )
        output = self.root / "cli"
        stdout = io.StringIO()
        with (
            patch("mvp.run_full_records.importlib.import_module", return_value=module),
            contextlib.redirect_stdout(stdout),
        ):
            exit_code = main(
                [
                    "--mode",
                    "report_record_9706_1",
                    "--report",
                    str(self.report),
                    "--record",
                    str(self.record),
                    "--output",
                    str(output),
                ]
            )
        self.assertEqual(exit_code, 0)
        summary = json.loads(stdout.getvalue())
        self.assertEqual(summary["mode"], "report_record_9706_1")
        self.assertTrue((output / "result.json").is_file())
        self.assertTrue((output / "index.html").is_file())


if __name__ == "__main__":
    unittest.main()
