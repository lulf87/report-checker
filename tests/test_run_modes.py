from __future__ import annotations

import contextlib
import io
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf as fitz

from mvp.checker import REPORT_SELF_RULE_ORDER, SAMPLE_CONFIGS
from mvp.run_modes import (
    MODE_CAPABILITIES,
    ModeDisabledError,
    build_parser,
    main,
    run_mode,
)
from mvp.run_report_self import run_report_self


def _write_pdf(path: Path, pages: int = 3) -> None:
    with fitz.open() as document:
        for index in range(pages):
            page = document.new_page()
            page.insert_text((72, 72), f"page {index + 1}")
        document.save(path)


def _self_findings() -> list[dict]:
    findings = []
    for rule_id in REPORT_SELF_RULE_ORDER:
        findings.append(
            {
                "id": rule_id,
                "title": rule_id,
                "status": "error" if rule_id == "REPORT-R07" else "pass",
                "summary": "不一致" if rule_id == "REPORT-R07" else "一致",
                "details": {},
                "evidence": [],
            }
        )
    findings[0]["evidence"] = [
        {"pdf_page": 1, "rects": [[1, 1, 20, 20]], "image": "unused.png"}
    ]
    return findings


class ReportSelfRunnerTests(unittest.TestCase):
    def test_report_self_writes_report_only_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = root / "report.pdf"
            output = root / "self"
            _write_pdf(report)
            def fake_self_check(_source, output_dir):
                output_dir.mkdir(parents=True, exist_ok=True)
                evidence = output_dir / "evidence.png"
                evidence.write_bytes(b"png-placeholder")
                findings = _self_findings()
                findings[0]["evidence"][0]["image"] = str(evidence)
                return findings, {}

            with patch("mvp.run_report_self.report_self_check", side_effect=fake_self_check):
                result = run_report_self(report_path=report, output_dir=output)

            self.assertEqual(result["mode"], "report_self")
            self.assertEqual(result["machine_overall_status"], "error")
            self.assertEqual(result["status_counts"], {"pass": 11, "warning": 0, "manual": 0, "error": 1})
            self.assertEqual(result["coverage"]["planned"], 12)
            self.assertEqual(result["coverage"]["completed"], 12)
            self.assertEqual([item["role"] for item in result["files"]], ["report"])
            self.assertTrue(result["source_integrity"][0]["unchanged"])
            self.assertTrue(all(item["id"].startswith("REPORT-") for item in result["findings"]))
            self.assertEqual(result["findings"][0]["evidence"][0]["document_role"], "report")
            self.assertEqual(result["findings"][0]["evidence_locations"][0]["role"], "report")
            self.assertEqual(result["findings"][0]["evidence_locations"][0]["bbox"], [1.0, 1.0, 20.0, 20.0])
            self.assertTrue((output / "result.json").is_file())
            self.assertTrue((output / "index.html").is_file())
            saved = json.loads((output / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["mode"], "report_self")

    def test_four_real_reports_keep_the_frozen_baseline(self) -> None:
        root = Path(__file__).resolve().parents[1]
        expected = {
            "1347": {"pass": 8, "warning": 0, "manual": 4, "error": 0},
            "1539": {"pass": 8, "warning": 0, "manual": 4, "error": 0},
            "2795": {"pass": 7, "warning": 0, "manual": 4, "error": 1},
            "2948": {"pass": 7, "warning": 0, "manual": 4, "error": 1},
        }
        with tempfile.TemporaryDirectory() as temporary:
            for sample, counts in expected.items():
                with self.subTest(sample=sample):
                    result = run_report_self(
                        report_path=root / SAMPLE_CONFIGS[sample].report,
                        output_dir=Path(temporary) / sample,
                    )
                    self.assertEqual(result["mode"], "report_self")
                    self.assertEqual(result["status_counts"], counts)
                    self.assertEqual(result["coverage"]["planned"], 12)
                    self.assertEqual(result["coverage"]["completed"], 12)
                    self.assertTrue(all(item["unchanged"] for item in result["source_integrity"]))


class FourModeDispatcherTests(unittest.TestCase):
    def test_capability_registry_has_four_modes(self) -> None:
        self.assertEqual(
            tuple(MODE_CAPABILITIES),
            (
                "report_self",
                "report_ptr",
                "report_record_9706_1",
                "report_record_9706_202",
            ),
        )
        self.assertTrue(MODE_CAPABILITIES["report_self"]["enabled"])
        self.assertFalse(MODE_CAPABILITIES["report_ptr"]["enabled"])

    def test_ptr_is_stably_disabled_without_creating_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "ptr"
            with self.assertRaises(ModeDisabledError) as context:
                run_mode(
                    mode="report_ptr",
                    report_path=Path(temporary) / "missing-report.pdf",
                    record_path=Path(temporary) / "missing-ptr.pdf",
                    output_dir=output,
                )
            error = context.exception
            self.assertEqual(error.code, "MODE_DISABLED")
            self.assertEqual(error.reason_code, "PTR_NOT_VALIDATED")
            self.assertFalse(output.exists())

    def test_report_self_dispatches_without_record(self) -> None:
        expected = {"mode": "report_self", "machine_overall_status": "pass"}
        with patch("mvp.run_modes.run_report_self", return_value=expected) as runner:
            result = run_mode(
                mode="report_self",
                report_path="report.pdf",
                output_dir="output",
            )
        self.assertEqual(result, expected)
        runner.assert_called_once_with(report_path="report.pdf", output_dir="output")

    def test_report_self_rejects_record_input(self) -> None:
        with self.assertRaisesRegex(ValueError, "only a Report"):
            run_mode(
                mode="report_self",
                report_path="report.pdf",
                record_path="record.pdf",
                output_dir="output",
            )

    def test_cli_exposes_all_four_modes(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            ["--mode", "report_ptr", "--report", "report.pdf", "--output", "out"]
        )
        self.assertEqual(args.mode, "report_ptr")

    def test_cli_returns_structured_ptr_error(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            exit_code = main(
                ["--mode", "report_ptr", "--report", "report.pdf", "--output", "out"]
            )
        self.assertEqual(exit_code, 2)
        payload = json.loads(stderr.getvalue())
        self.assertEqual(payload["error"]["code"], "MODE_DISABLED")
        self.assertEqual(
            payload["error"]["details"]["disabled_reason_code"],
            "PTR_NOT_VALIDATED",
        )


if __name__ == "__main__":
    unittest.main()
