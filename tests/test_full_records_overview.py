from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mvp.full_records_overview import generate_overview


MODE_61 = "report-record-9706-1"
MODE_202 = "report-record-9706-202"


def _write_result(
    root: Path,
    *,
    sample: str,
    mode: str,
    result_mode: str,
    finding_statuses: list[str],
    ledger_dispositions: list[str],
    finding_dispositions: list[str | None] | None = None,
    source_coverage: tuple[int, int, bool] = (5, 5, True),
    report_coverage: tuple[int, int, bool] = (4, 4, True),
    write_index: bool = True,
) -> None:
    output = root / sample / mode
    output.mkdir(parents=True)
    finding_dispositions = finding_dispositions or [None] * len(finding_statuses)
    payload = {
        "mode": result_mode,
        "findings": [
            {
                "id": f"F{index}",
                "status": status,
                "details": (
                    {"disposition": disposition}
                    if disposition is not None
                    else {}
                ),
            }
            for index, (status, disposition) in enumerate(
                zip(finding_statuses, finding_dispositions, strict=True), start=1
            )
        ],
        "ledger": [
            {"entry_id": f"L{index}", "disposition": disposition}
            for index, disposition in enumerate(ledger_dispositions, start=1)
        ],
        "coverage": {
            "source_rows": {
                "eligible": source_coverage[0],
                "accounted": source_coverage[1],
                "conserved": source_coverage[2],
            },
            "report_rows": {
                "eligible": report_coverage[0],
                "accounted": report_coverage[1],
                "conserved": report_coverage[2],
            },
        },
    }
    (output / "result.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    if write_index:
        (output / "index.html").write_text("<p>detail</p>", encoding="utf-8")


class FullRecordsOverviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_generates_three_formats_with_separate_counts_coverage_and_links(self) -> None:
        _write_result(
            self.root,
            sample="1001",
            mode=MODE_61,
            result_mode="report_record_9706_1",
            finding_statuses=["pass", "pass", "pass", "error", "warning", "manual"],
            finding_dispositions=[None, "not_applicable", "excluded", None, None, None],
            ledger_dispositions=[
                "matched",
                "matched",
                "mismatch",
                "manual",
                "not_applicable",
                "excluded",
            ],
            source_coverage=(6, 6, True),
            report_coverage=(5, 5, True),
        )
        _write_result(
            self.root,
            sample="1001",
            mode=MODE_202,
            result_mode="report_record_9706_202",
            finding_statuses=["pass"],
            ledger_dispositions=["matched"],
            source_coverage=(1, 1, True),
            report_coverage=(1, 1, True),
        )
        expected = (("1001", MODE_61), ("1001", MODE_202), ("1002", MODE_202))

        summary = generate_overview(
            self.root,
            expected_runs=expected,
            include_discovered=False,
            generated_at="2026-09-30T00:00:00+00:00",
        )

        self.assertTrue((self.root / "summary.json").is_file())
        self.assertTrue((self.root / "summary.md").is_file())
        self.assertTrue((self.root / "index.html").is_file())
        saved = json.loads((self.root / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(saved, summary)

        first = summary["runs"][0]
        self.assertEqual(
            first["categories"],
            {
                "automatic_match": {
                    "label": "自动一致",
                    "finding_count": 1,
                    "ledger_count": 2,
                },
                "confirmed_mismatch": {
                    "label": "确定不一致",
                    "finding_count": 1,
                    "ledger_count": 1,
                },
                "warning": {"label": "警示", "finding_count": 1, "ledger_count": 0},
                "manual_review": {
                    "label": "待人工复核",
                    "finding_count": 1,
                    "ledger_count": 1,
                },
                "not_applicable": {
                    "label": "不适用",
                    "finding_count": 1,
                    "ledger_count": 1,
                },
                "excluded": {"label": "排除", "finding_count": 1, "ledger_count": 1},
            },
        )
        self.assertEqual(first["coverage"]["source_rows"]["eligible"], 6)
        self.assertEqual(first["coverage"]["source_rows"]["accounted"], 6)
        self.assertEqual(first["coverage"]["report_rows"]["eligible"], 5)
        self.assertEqual(first["coverage"]["report_rows"]["accounted"], 5)
        self.assertEqual(first["machine_overall_status"], "error")
        self.assertFalse(first["is_pass"])

        passed = summary["runs"][1]
        self.assertEqual(passed["machine_overall_status"], "pass")
        self.assertTrue(passed["is_pass"])
        missing = summary["runs"][2]
        self.assertEqual(missing["run_state"], "missing")
        self.assertEqual(missing["machine_overall_status"], "missing")
        self.assertFalse(missing["is_pass"])
        self.assertIsNone(
            missing["categories"]["automatic_match"]["finding_count"]
        )
        self.assertEqual(
            summary["totals"],
            {
                "expected_runs": 3,
                "complete_runs": 2,
                "incomplete_runs": 0,
                "missing_runs": 1,
                "invalid_runs": 0,
                "passed_runs": 1,
                "non_passed_runs": 2,
                "coverage_incomplete_runs": 0,
                "detail_missing_runs": 0,
            },
        )

        markdown = (self.root / "summary.md").read_text(encoding="utf-8")
        page = (self.root / "index.html").read_text(encoding="utf-8")
        for label in ("自动一致", "确定不一致", "警示", "待人工复核", "不适用", "排除"):
            self.assertIn(label, markdown)
            self.assertIn(label, page)
        self.assertIn("6 / 6（守恒）", markdown)
        self.assertIn("[打开 index.html](1001/report-record-9706-1/index.html)", markdown)
        self.assertIn('href="1001/report-record-9706-1/index.html"', page)
        self.assertIn("结果缺失", markdown)
        self.assertIn("结果缺失", page)

    def test_invalid_or_incomplete_result_never_counts_as_pass(self) -> None:
        invalid = self.root / "1001" / MODE_61
        invalid.mkdir(parents=True)
        (invalid / "result.json").write_text("{bad json", encoding="utf-8")
        (invalid / "index.html").write_text("detail", encoding="utf-8")
        _write_result(
            self.root,
            sample="1002",
            mode=MODE_202,
            result_mode="report_record_9706_202",
            finding_statuses=["pass"],
            ledger_dispositions=["matched"],
            source_coverage=(1, 0, False),
            report_coverage=(1, 1, True),
            write_index=False,
        )

        summary = generate_overview(
            self.root,
            expected_runs=(("1001", MODE_61), ("1002", MODE_202)),
            include_discovered=False,
        )

        invalid_run, incomplete_run = summary["runs"]
        self.assertEqual(invalid_run["run_state"], "invalid")
        self.assertFalse(invalid_run["is_pass"])
        self.assertEqual(incomplete_run["run_state"], "incomplete")
        self.assertIn("index.html 缺失", incomplete_run["state_message"])
        self.assertIn("coverage 未完整守恒", incomplete_run["state_message"])
        self.assertFalse(incomplete_run["is_pass"])
        self.assertEqual(summary["totals"]["passed_runs"], 0)
        self.assertEqual(summary["totals"]["invalid_runs"], 1)
        self.assertEqual(summary["totals"]["incomplete_runs"], 1)


if __name__ == "__main__":
    unittest.main()
