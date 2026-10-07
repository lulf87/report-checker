from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML_PATH = ROOT / "docs" / "prototypes" / "workbench-upload.html"
ROOT_ENTRY_PATH = ROOT / "index.html"


def _module_source(html: str) -> str:
    match = re.search(
        r"<script\b[^>]*\btype=[\"']module[\"'][^>]*>(.*?)</script\s*>",
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if match is None:
        raise AssertionError("public upload workbench must contain a module script")
    return match.group(1)


class UploadWorkbenchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = HTML_PATH.read_text(encoding="utf-8")
        cls.module_source = _module_source(cls.html)

    def test_public_page_has_no_private_sample_dependencies(self) -> None:
        self.assertNotIn("素材/", self.html)
        self.assertNotIn("output/", self.html)
        self.assertNotIn("workbench-real", self.html)
        self.assertNotRegex(self.module_source, r"RUN_MANIFEST|sampleFixtures|素材/")

    def test_root_entry_opens_public_upload_workbench(self) -> None:
        root_entry = ROOT_ENTRY_PATH.read_text(encoding="utf-8")
        self.assertIn("/workbench-upload.html", root_entry)
        self.assertIn("/docs/prototypes/workbench-upload.html", root_entry)
        self.assertIn("window.location.replace", root_entry)

    def test_upload_form_exposes_all_modes_and_pdf_roles(self) -> None:
        self.assertIn('id="upload-form"', self.html)
        self.assertIn('data-role="report-file"', self.html)
        self.assertIn('data-role="record-file"', self.html)
        file_inputs = re.findall(
            r"<input\b(?=[^>]*\btype\s*=\s*[\"']file[\"'])[^>]*>",
            self.html,
            flags=re.IGNORECASE,
        )
        self.assertEqual(len(file_inputs), 2)
        for tag in file_inputs:
            accept = re.search(r"\baccept\s*=\s*[\"']([^\"']+)", tag, flags=re.IGNORECASE)
            self.assertIsNotNone(accept)
            self.assertRegex(accept.group(1).lower(), r"application/pdf|\.pdf")
        mode_select = re.search(
            r"<select\b[^>]*data-role=[\"']mode-select[\"'][^>]*>(.*?)</select>",
            self.html,
            flags=re.DOTALL | re.IGNORECASE,
        )
        self.assertIsNotNone(mode_select)
        modes = set(
            re.findall(
                r"<option\b[^>]*\bvalue=[\"']([^\"']+)[\"']",
                mode_select.group(1),
                flags=re.IGNORECASE,
            )
        )
        self.assertEqual(modes, {"self", "record61", "record202", "ptr"})
        self.assertRegex(
            mode_select.group(1),
            r"<option\b[^>]*value=[\"']ptr[\"'][^>]*\bdisabled\b",
        )

    def test_upload_flow_uses_session_documents_preflight_and_run_api(self) -> None:
        for required in (
            "/api/v1/session",
            "/documents",
            ":preflight",
            "/runs",
            "/findings",
            "FormData",
            "X-CSRF-Token",
            "csrf_token",
            "machine_status",
            "machine_overall_status",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.module_source)
        self.assertRegex(self.module_source, r"\.files\?\.\[0\]")
        self.assertRegex(self.module_source, r"addEventListener\(\s*[\"']change[\"']")
        self.assertRegex(self.module_source, r"method\s*:\s*[\"']POST[\"']")
        self.assertIn("recordFile.required = needsRecord", self.module_source)

    def test_warning_status_has_distinct_run_badge(self) -> None:
        self.assertIn(".status-label.warning", self.html)
        self.assertIn('overall === "warning"', self.module_source)
        self.assertIn('? "warning"', self.module_source)

    def test_module_script_has_valid_javascript_syntax(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is unavailable")
        with tempfile.NamedTemporaryFile(mode="w", suffix=".mjs", encoding="utf-8", delete=False) as handle:
            handle.write(self.module_source)
            path = Path(handle.name)
        try:
            completed = subprocess.run(
                [node, "--check", str(path)],
                text=True,
                capture_output=True,
                check=False,
            )
        finally:
            path.unlink(missing_ok=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_comparison_view_uses_api_documents_and_evidence_locations(self) -> None:
        for required in (
            'id="comparison-card"',
            'data-pane-role="report"',
            'data-pane-role="record"',
            'pdf.min.mjs',
            'pdf.worker.min.mjs',
            '/api/v1/documents/',
            'pdf_page',
            'bbox',
            'evidence-highlight',
            'goToPage',
            'setZoom',
            'showFindingComparison',
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.html if required.startswith(('id=', 'data-pane')) else self.module_source)
        self.assertIn("comparisonCard.scrollIntoView", self.module_source)

    def test_comparison_view_is_split_into_findings_and_pdf_stage(self) -> None:
        for required in (
            'id="results-workspace"',
            'class="card findings-card"',
            'id="findings"',
            'class="card comparison-card"',
            'id="comparison-card"',
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.html)
        self.assertRegex(
            self.html,
            r'id="results-workspace"[\s\S]*class="card findings-card"[\s\S]*id="findings"[\s\S]*class="card comparison-card"',
        )
        self.assertIn("grid-template-columns: minmax(280px, 34%) minmax(0, 1fr)", self.html)


if __name__ == "__main__":
    unittest.main()
