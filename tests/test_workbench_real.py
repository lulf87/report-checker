from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from mvp.checker import REPORT_SELF_RULE_ORDER


ROOT = Path(__file__).resolve().parents[1]
HTML_PATH = ROOT / "docs" / "prototypes" / "workbench-real.html"


def _module_source(html: str) -> str:
    match = re.search(
        r"<script\b[^>]*\btype=[\"']module[\"'][^>]*>(.*?)</script\s*>",
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if match is None:
        raise AssertionError("real workbench must contain a module script")
    return match.group(1)


def _manifest_entries(module_source: str) -> list[dict[str, str]]:
    manifest = re.search(
        r"const\s+RUN_MANIFEST\s*=\s*Object\.freeze\(\[(?P<body>.*?)\]\);",
        module_source,
        flags=re.DOTALL,
    )
    if manifest is None:
        raise AssertionError("RUN_MANIFEST is missing")
    entries: list[dict[str, str]] = []
    for entry in re.finditer(r"\{(?P<body>[^{}]+)\}", manifest.group("body")):
        fields = dict(
            re.findall(
                r"\b(id|sample|mode|resultPath)\s*:\s*[\"']([^\"']+)[\"']",
                entry.group("body"),
            )
        )
        if fields:
            entries.append(fields)
    return entries


class RealWorkbenchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not HTML_PATH.is_file():
            raise unittest.SkipTest("private real workbench fixture is unavailable")
        cls.html = HTML_PATH.read_text(encoding="utf-8")
        cls.module_source = _module_source(cls.html)
        cls.entries = _manifest_entries(cls.module_source)

    def test_manifest_declares_ten_real_runs_across_self_and_record_modes(self) -> None:
        self.assertEqual(len(self.entries), 10)
        self.assertEqual(len({entry["id"] for entry in self.entries}), 10)
        self.assertEqual(
            {(entry["sample"], entry["mode"]) for entry in self.entries},
            {
                ("1347", "self"),
                ("1539", "self"),
                ("2795", "self"),
                ("2948", "self"),
                ("1347", "record61"),
                ("1539", "record61"),
                ("2948", "record61"),
                ("1539", "record202"),
                ("2795", "record202"),
                ("2948", "record202"),
            },
        )

    def test_every_manifest_result_and_source_pdf_exists(self) -> None:
        expected_modes = {
            "self": "report_self",
            "record61": "report_record_9706_1",
            "record202": "report_record_9706_202",
        }
        for entry in self.entries:
            with self.subTest(run=entry["id"]):
                result_path = (HTML_PATH.parent / entry["resultPath"]).resolve()
                self.assertTrue(result_path.is_file(), result_path)
                result = json.loads(result_path.read_text(encoding="utf-8"))
                self.assertEqual(result.get("mode"), expected_modes[entry["mode"]])
                self.assertIsInstance(result.get("findings"), list)
                self.assertGreater(len(result["findings"]), 0)
                if entry["mode"] == "self":
                    expected_prefix = "REPORT-"
                    self.assertEqual(result["coverage"]["planned"], len(REPORT_SELF_RULE_ORDER))
                    self.assertEqual(result["coverage"]["attempted"], len(REPORT_SELF_RULE_ORDER))
                    self.assertEqual(result["coverage"]["completed"], len(REPORT_SELF_RULE_ORDER))
                    self.assertEqual(result["coverage"]["missing"], [])
                    executions = result["coverage"].get("rule_executions")
                    self.assertIsInstance(executions, list)
                    self.assertEqual(
                        [item["rule_id"] for item in executions],
                        list(REPORT_SELF_RULE_ORDER),
                    )
                    finding_statuses = {finding["rule_id"]: finding["status"] for finding in result["findings"]}
                    self.assertEqual(
                        {item["rule_id"]: item["machine_status"] for item in executions},
                        finding_statuses,
                    )
                    self.assertEqual(
                        set(result["files"][0]),
                        {"role", "path", "sha256", "size"},
                    )
                    self.assertEqual([item["role"] for item in result["files"]], ["report"])
                    source_path = Path(result["files"][0]["path"])
                    self.assertTrue(source_path.is_file(), source_path)
                    self.assertEqual(source_path.suffix.lower(), ".pdf")
                    self.assertTrue(all(item["unchanged"] for item in result["source_integrity"]))
                    if entry["sample"] in {"2795", "2948"}:
                        r07 = next(finding for finding in result["findings"] if finding["id"] == "REPORT-R07")
                        self.assertEqual(r07["status"], "error")
                        self.assertTrue(r07["evidence_locations"])
                        self.assertTrue(all(location["role"] == "report" for location in r07["evidence_locations"]))
                    for finding in result["findings"]:
                        if finding["status"] == "pass":
                            continue
                        with self.subTest(run=entry["id"], finding=finding["id"]):
                            locations = finding.get("evidence_locations", [])
                            self.assertTrue(locations, "非通过 Report Finding 必须带可定位证据")
                            self.assertTrue(all(location.get("role") == "report" for location in locations))
                            self.assertTrue(
                                all(
                                    isinstance(location.get("pdf_page"), int)
                                    and location["pdf_page"] > 0
                                    and len(location.get("bbox", [])) == 4
                                    and float(location["bbox"][0]) < float(location["bbox"][2])
                                    and float(location["bbox"][1]) < float(location["bbox"][3])
                                    for location in locations
                                )
                            )
                else:
                    expected_prefix = "RECORD61-" if entry["mode"] == "record61" else "RECORD202-"
                if entry["mode"] == "self":
                    self.assertTrue(
                        all(
                            finding.get("id", "").startswith(expected_prefix)
                            and finding.get("rule_id", "").startswith(expected_prefix)
                            for finding in result["findings"]
                        )
                    )
                else:
                    self.assertTrue(
                        all(
                            finding.get("id", "").startswith(expected_prefix)
                            and finding.get("rule_id", "").startswith(expected_prefix)
                            and not finding.get("id", "").startswith("REPORT-")
                            and not finding.get("rule_id", "").startswith("REPORT-")
                            for finding in result["findings"]
                        )
                    )
                expected_finding_counts = Counter(finding["status"] for finding in result["findings"])
                self.assertEqual(
                    result.get("status_counts"),
                    {
                        "pass": expected_finding_counts["pass"],
                        "warning": expected_finding_counts["warning"],
                        "manual": expected_finding_counts["manual"],
                        "error": expected_finding_counts["error"],
                    },
                )
                if entry["mode"] == "self":
                    continue
                disposition_to_status = {
                    "matched": "pass",
                    "warning": "warning",
                    "manual": "manual",
                    "mismatch": "error",
                    "not_applicable": "not_applicable",
                    "excluded": "excluded",
                }
                expected_ledger_counts = Counter(
                    disposition_to_status[ledger_entry["disposition"]]
                    for ledger_entry in result["ledger"]
                )
                self.assertEqual(
                    result.get("ledger_status_counts"),
                    {
                        "pass": expected_ledger_counts["pass"],
                        "warning": expected_ledger_counts["warning"],
                        "manual": expected_ledger_counts["manual"],
                        "error": expected_ledger_counts["error"],
                        "not_applicable": expected_ledger_counts["not_applicable"],
                        "excluded": expected_ledger_counts["excluded"],
                    },
                )
                self.assertEqual(set(result.get("source_files", {})), {"report", "record"})
                for source in result["source_files"].values():
                    source_path = Path(source["path"])
                    self.assertTrue(source_path.is_file(), source_path)
                    self.assertEqual(source_path.suffix.lower(), ".pdf")

    def test_page_contains_no_demo_fixture_or_static_finding_dataset(self) -> None:
        self.assertNotIn("sampleFixtures", self.html)
        self.assertNotRegex(self.html, r"(?i)\bdemo\b")
        self.assertNotIn('"findings": [', self.module_source)
        self.assertNotRegex(self.module_source, r"const\s+findings\s*=\s*\[")

    def test_known_mismatches_keep_real_page_and_bbox_evidence(self) -> None:
        known = {
            "2948-record61": {
                "RECORD61-BODY-S052-E037",
                "RECORD61-BODY-S052-E038",
                "RECORD61-BODY-S061-E008",
            },
            "2795-record202": {
                "RECORD202-NUMBER",
                "RECORD202-I35-R01",
                "RECORD202-I37-R10",
                "RECORD202-I38-R01",
                "RECORD202-I38-R02",
            },
            "2948-record202": {"RECORD202-I38-R01"},
        }
        entries = {entry["id"]: entry for entry in self.entries}
        for run_id, finding_ids in known.items():
            result_path = (HTML_PATH.parent / entries[run_id]["resultPath"]).resolve()
            result = json.loads(result_path.read_text(encoding="utf-8"))
            by_id = {finding["id"]: finding for finding in result["findings"]}
            with self.subTest(run=run_id):
                self.assertTrue(finding_ids.issubset(by_id))
            for finding_id in finding_ids:
                with self.subTest(run=run_id, finding=finding_id):
                    finding = by_id[finding_id]
                    self.assertEqual(finding["status"], "error")
                    locations = finding.get("evidence_locations", [])
                    self.assertGreaterEqual(len(locations), 2)
                    self.assertIn("report", {location["role"] for location in locations})
                    for location in locations:
                        self.assertGreater(int(location["pdf_page"]), 0)
                        bbox = location["bbox"]
                        self.assertEqual(len(bbox), 4)
                        self.assertLess(float(bbox[0]), float(bbox[2]))
                        self.assertLess(float(bbox[1]), float(bbox[3]))

        number_finding = next(
            finding
            for finding in json.loads(
                ((HTML_PATH.parent / entries["2795-record202"]["resultPath"]).resolve()).read_text(
                    encoding="utf-8"
                )
            )["findings"]
            if finding["id"] == "RECORD202-NUMBER"
        )
        record_locations = [
            location
            for location in number_finding["evidence_locations"]
            if location["role"] == "record_9706_202"
        ]
        self.assertEqual([location["pdf_page"] for location in record_locations], list(range(1, 25)))
        self.assertTrue(all(location["bbox"] == [352.6, 106.288, 571.35, 134.738] for location in record_locations))

    def test_results_are_loaded_dynamically_and_validated(self) -> None:
        self.assertIn("fetch(resultUrl", self.module_source)
        self.assertIn("response.json()", self.module_source)
        self.assertIn("result.findings", self.module_source)
        self.assertIn("result.source_files", self.module_source)
        self.assertIn("result.files", self.module_source)
        self.assertIn("finding.evidence_locations", self.module_source)
        self.assertIn("result.mode !== MODE_META[run.mode].resultMode", self.module_source)
        self.assertIn('startsWith("REPORT-")', self.module_source)
        self.assertIn('const expectedPrefix = MODE_META[run.mode].recordEvidenceRole === "record_9706_1" ? "RECORD61-" : "RECORD202-"', self.module_source)
        self.assertIn('run.mode === "self"', self.module_source)
        self.assertIn("Report 自检规则 Coverage 不完整", self.module_source)
        self.assertIn("foreignFinding", self.module_source)
        self.assertIn("result.status_counts", self.module_source)
        self.assertIn("result.ledger_status_counts", self.module_source)
        self.assertIn("recountFindingStatuses", self.module_source)
        self.assertIn("recountLedgerStatuses", self.module_source)
        self.assertIn("status_counts 与 Finding 明细不一致", self.module_source)
        self.assertIn("ledger_status_counts 与 Ledger 明细不一致", self.module_source)
        self.assertIn("cache: \"no-store\"", self.module_source)

    def test_report_self_scope_ledger_and_warning_status_are_first_class(self) -> None:
        self.assertIn('data-status="warning"', self.html)
        self.assertIn('warning: { label: "警示", short: "警示" }', self.module_source)
        self.assertIn('if (status === "warning" || disposition === "warning") return "warning";', self.module_source)
        self.assertIn("REPORT_SELF_RULE_IDS", self.module_source)
        self.assertIn("result.ledger_status_counts !== undefined", self.module_source)
        self.assertIn("result.scope_coverage.conserved !== true", self.module_source)
        self.assertNotIn('if (result.ledger !== undefined || result.ledger_status_counts !== undefined)', self.module_source)

    def test_compact_summary_separates_finding_and_ledger_counts(self) -> None:
        self.assertIn("Finding/Q", self.html)
        self.assertIn("Ledger", self.html)
        self.assertIn("问题Q", self.module_source)
        self.assertIn("没问题Q", self.module_source)
        self.assertIn("ledgerCounts.not_applicable", self.module_source)
        self.assertIn("ledgerCounts.excluded", self.module_source)
        self.assertIn("findingCounts.error", self.module_source)
        self.assertIn("ledgerCounts.error", self.module_source)

        entries = {entry["id"]: entry for entry in self.entries}
        result_2795 = json.loads(
            (HTML_PATH.parent / entries["2795-record202"]["resultPath"]).resolve().read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(result_2795["status_counts"]["error"], 21)
        self.assertEqual(result_2795["ledger_status_counts"]["error"], 44)

        result_1347 = json.loads(
            (HTML_PATH.parent / entries["1347-record61"]["resultPath"]).resolve().read_text(
                encoding="utf-8"
            )
        )
        # Counts are derived from the regenerated official artifact; verify
        # the two published views stay internally consistent as scope grows.
        self.assertEqual(
            result_1347["status_counts"]["pass"],
            sum(item["status"] == "pass" for item in result_1347["findings"]),
        )
        self.assertEqual(
            result_1347["ledger_status_counts"]["pass"],
            sum(item.get("disposition") == "matched" for item in result_1347["ledger"]),
        )
        self.assertEqual(result_1347["ledger_status_counts"]["not_applicable"], 1)
        self.assertIn("RECORD61-METADATA", {item["rule_id"] for item in result_1347["ledger"]})
        self.assertTrue(result_1347["coverage"]["numeric_discovery"]["conserved"])

        result_202 = json.loads(
            (HTML_PATH.parent / entries["1539-record202"]["resultPath"]).resolve().read_text(
                encoding="utf-8"
            )
        )
        self.assertTrue(result_202["scope_coverage"]["conserved"])
        scope_rules = {item["rule_id"] for item in result_202["scope_ledger"]}
        self.assertTrue({"RECORD202-SCOPE", "RECORD202-IDENTITY", "RECORD202-METADATA"} <= scope_rules)

    def test_stale_evidence_selection_cannot_overwrite_current_panes(self) -> None:
        self.assertIn("evidenceToken", self.module_source)
        self.assertIn("function evidenceSelectionIsCurrent(selection)", self.module_source)
        for required_guard in (
            "selection.token === state.evidenceToken",
            "selection.mode === state.mode",
            "selection.runId === currentRun()?.id",
            "selection.findingId === modeState[state.mode].selectedFindingId",
        ):
            self.assertIn(required_guard, self.module_source)
        load_index = self.module_source.index("reportPane.loadDocument")
        guard_index = self.module_source.index(
            "if (!evidenceSelectionIsCurrent(selection)) return;", load_index
        )
        set_index = self.module_source.index("reportPane.setFinding", guard_index)
        self.assertLess(load_index, guard_index)
        self.assertLess(guard_index, set_index)
        self.assertIn("state.evidenceToken += 1;", self.module_source)
        self.assertIn("this.renderTask?.cancel();", self.module_source)
        self.assertIn('error?.name === "RenderingCancelledException"', self.module_source)

    def test_modes_status_filters_and_read_only_identity_are_present(self) -> None:
        self.assertIn('data-mode="self"', self.html)
        self.assertIn('data-mode="ptr"', self.html)
        self.assertIn("PTR_NOT_VALIDATED", self.html)
        self.assertIn('data-mode="record61"', self.html)
        self.assertIn('data-mode="record202"', self.html)
        for status in ("all", "error", "manual", "pass"):
            self.assertIn(f'data-status="{status}"', self.html)
        self.assertIn("真实数据", self.html)
        self.assertIn("只读", self.html)
        self.assertIn("modeState", self.module_source)
        self.assertIn("recordEvidenceRole", self.module_source)
        self.assertIn("singleDocument", self.module_source)
        self.assertIn('resultMode: "report_self"', self.module_source)

    def test_upload_surface_exposes_report_and_record_pdf_inputs(self) -> None:
        """The real workbench must accept user PDFs instead of only static runs.

        This is deliberately a source contract: it does not open or parse a PDF.
        The role markers allow the implementation to choose either stable ids or
        data attributes while keeping Report/Record routing unambiguous.
        """
        file_inputs = re.findall(
            r"<input\b(?=[^>]*\btype\s*=\s*[\"']file[\"'])[^>]*>",
            self.html,
            flags=re.IGNORECASE,
        )
        self.assertGreaterEqual(
            len(file_inputs),
            2,
            "upload UI must expose separate Report and Record file inputs",
        )
        report_inputs = [
            tag for tag in file_inputs if re.search(r"report|报告", tag, flags=re.IGNORECASE)
        ]
        record_inputs = [
            tag for tag in file_inputs if re.search(r"record|记录", tag, flags=re.IGNORECASE)
        ]
        self.assertTrue(report_inputs, "one file input must identify the Report role")
        self.assertTrue(record_inputs, "one file input must identify the Record role")
        for tag in file_inputs:
            with self.subTest(input=tag):
                accept = re.search(r"\baccept\s*=\s*[\"']([^\"']+)", tag, flags=re.IGNORECASE)
                self.assertIsNotNone(accept, "PDF upload input must declare an accept filter")
                self.assertRegex(accept.group(1).lower(), r"application/pdf|\.pdf")

    def test_upload_and_mode_selection_are_wired_to_run_api(self) -> None:
        """Client-side upload flow must preserve selected mode and CSRF/API contract."""
        source = self.module_source
        self.assertRegex(source, r"\bFormData\b", "multipart upload should use FormData")
        self.assertRegex(source, r"\.files\b", "file change handlers must read selected files")
        self.assertRegex(
            source,
            r"addEventListener\(\s*[\"']change[\"'][\s\S]{0,1200}\.files\b",
            "file inputs must have a change listener",
        )
        for required in (
            "/api/v1/session",
            "/documents",
            ":preflight",
            "/runs",
            "X-CSRF-Token",
            "csrf_token",
        ):
            with self.subTest(required=required):
                self.assertIn(required, source)
        self.assertRegex(
            source,
            r"fetch\([\s\S]{0,1800}method\s*:\s*[\"']POST[\"']",
            "upload/run writes must use explicit POST requests",
        )

    def test_upload_mode_contract_maps_roles_and_keeps_ptr_disabled(self) -> None:
        source = self.module_source
        # The enabled mode values are the same values accepted by capabilities
        # and the Run state API.  Checking the source keeps this test independent
        # of the availability of a live API server or sample PDFs.
        for mode in ("report_self", "report_record_9706_1", "report_record_9706_202"):
            with self.subTest(mode=mode):
                self.assertIn(mode, source)
        for role in ("report", "record_9706_1", "record_9706_202"):
            with self.subTest(role=role):
                self.assertIn(role, source)
        self.assertIn("PTR_NOT_VALIDATED", source)
        mode_select = re.search(
            r"<select\b[^>]*data-role=[\"']mode-select[\"'][^>]*>(?P<body>.*?)</select>",
            self.html,
            flags=re.IGNORECASE | re.DOTALL,
        )
        self.assertIsNotNone(mode_select, "upload dialog must expose a mode selector")
        options = {
            value
            for value in re.findall(
                r"<option\b[^>]*\bvalue=[\"']([^\"']+)[\"']",
                mode_select.group("body"),
                flags=re.IGNORECASE,
            )
        }
        self.assertTrue({"self", "record61", "record202", "ptr"} <= options)
        self.assertRegex(
            mode_select.group("body"),
            r"<option\b[^>]*\bvalue=[\"']ptr[\"'][^>]*\bdisabled\b",
            "PTR upload option must remain disabled",
        )
        self.assertIn("recordFile.required = needsRecord", source)
        ptr_tab = re.search(
            r"<button\b[^>]*data-mode=[\"']ptr[\"'][^>]*>",
            self.html,
            flags=re.IGNORECASE,
        )
        self.assertIsNotNone(ptr_tab, "PTR mode control must remain visible for its disabled reason")
        self.assertRegex(ptr_tab.group(0), r"\bdisabled\b")
        self.assertRegex(source, r"report_ptr|PTR_NOT_VALIDATED")

    def test_local_pdfjs_and_precise_highlight_pipeline(self) -> None:
        import_match = re.search(
            r"import\s+\*\s+as\s+pdfjsLib\s+from\s+[\"']([^\"']+)[\"']",
            self.module_source,
        )
        worker_match = re.search(
            r"pdfjsLib\.GlobalWorkerOptions\.workerSrc\s*=\s*[\"']([^\"']+)[\"']",
            self.module_source,
        )
        self.assertIsNotNone(import_match)
        self.assertIsNotNone(worker_match)
        for reference in (import_match.group(1), worker_match.group(1)):
            self.assertNotIn("://", reference)
            self.assertTrue((HTML_PATH.parent / reference).resolve().is_file())

        for required in (
            "pdf_page",
            "bbox",
            "isValidBbox",
            "validEvidenceLocations",
            "convertToViewportRectangle",
            "evidence-highlight",
            "selectedEvidenceIndex",
            "goToEvidence",
            "goToPage",
            "setZoom",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.html)

    def test_each_pdf_pane_has_navigation_zoom_and_evidence_controls(self) -> None:
        self.assertEqual(self.html.count('class="evidence-picker"'), 2)
        self.assertEqual(self.html.count('class="page-input"'), 2)
        self.assertEqual(self.html.count('class="tool-button previous-page"'), 2)
        self.assertEqual(self.html.count('class="tool-button next-page"'), 2)
        self.assertEqual(self.html.count('class="tool-button zoom-out"'), 2)
        self.assertEqual(self.html.count('class="tool-button zoom-in"'), 2)
        self.assertEqual(self.html.count('class="pdf-canvas"'), 2)
        self.assertNotRegex(self.html, r"<iframe\b")

    def test_file_protocol_failure_has_http_recovery_instruction(self) -> None:
        self.assertIn('window.location.protocol === "file:"', self.html)
        self.assertIn(
            "python3 -m mvp.static_server --mode real --bind 127.0.0.1 --port 8765",
            self.html,
        )
        self.assertIn(
            "http://127.0.0.1:8765/workbench-real.html",
            self.html,
        )

    def test_inline_module_has_valid_javascript_syntax(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is unavailable")
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".mjs", encoding="utf-8", delete=False
        ) as temporary:
            temporary.write(self.module_source)
            temporary_path = Path(temporary.name)
        try:
            completed = subprocess.run(
                [node, "--check", str(temporary_path)],
                text=True,
                capture_output=True,
                check=False,
            )
        finally:
            temporary_path.unlink(missing_ok=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
