from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import unittest
from pathlib import Path, PurePosixPath
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
HTML_PATH = ROOT / "docs" / "prototypes" / "workbench-preview.html"
RESULT_2795_PATH = ROOT / "output" / "mvp-2795" / "result.json"


def _module_source(html: str) -> str:
    match = re.search(
        r"<script\b[^>]*\btype=[\"']module[\"'][^>]*>(.*?)</script\s*>",
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if match is None:
        raise AssertionError("workbench prototype must contain a module script")
    return match.group(1)


def _fixture_source(module_source: str) -> str:
    start_marker = "const sampleFixtures ="
    end_marker = "const state ="
    start = module_source.find(start_marker)
    end = module_source.find(end_marker, start + len(start_marker))
    if start < 0 or end < 0:
        raise AssertionError(
            "module script must keep sampleFixtures and cases before the state declaration"
        )
    return module_source[start:end]


def _evaluate_fixtures(module_source: str) -> dict[str, Any]:
    node = shutil.which("node")
    if node is None:
        raise AssertionError("Node.js is required to evaluate the prototype fixture data")

    trailer = r"""
globalThis.__workbenchContract = {
  sampleFixtures,
  cases,
  derived: {
    findings2948Record,
    findings2795Record,
    findings2948Self,
    findings2795Self
  },
  modeProbes: {
    "2948-record61": modeFindings("2948", "RECORD61-"),
    "2795-record202": modeFindings("2795", "RECORD202-"),
    "2948-self": modeFindings("2948", "REPORT-"),
    "2795-self": modeFindings("2795", "REPORT-")
  }
};
"""
    wrapper = r"""
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(0, "utf8");
const context = vm.createContext({});
new vm.Script(source, { filename: "workbench-contract-fixtures.js" })
  .runInContext(context, { timeout: 2000 });
process.stdout.write(JSON.stringify(context.__workbenchContract));
"""
    completed = subprocess.run(
        [node, "-e", wrapper],
        input=_fixture_source(module_source) + trailer,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            "could not evaluate the workbench fixture block with Node vm:\n"
            + completed.stderr.strip()
        )
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise AssertionError("Node vm returned invalid fixture JSON") from error


def _finding(findings: list[dict[str, Any]], rule_id: str) -> dict[str, Any]:
    matches = [item for item in findings if item.get("ruleId") == rule_id]
    if len(matches) != 1:
        raise AssertionError(f"expected exactly one prototype finding for {rule_id}")
    return matches[0]


def _machine_finding(result: dict[str, Any], finding_id: str) -> dict[str, Any]:
    matches = [item for item in result["findings"] if item.get("id") == finding_id]
    if len(matches) != 1:
        raise AssertionError(f"expected exactly one machine finding for {finding_id}")
    return matches[0]


def _page_number(label: object) -> int:
    match = re.search(r"\d+", str(label))
    if match is None:
        raise AssertionError(f"pane page label has no page number: {label!r}")
    return int(match.group())


def _valid_rect(rect: object) -> bool:
    if not isinstance(rect, list) or len(rect) != 4:
        return False
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        for value in rect
    ):
        return False
    left, top, right, bottom = rect
    return left < right and top < bottom


def _valid_rects(rects: object) -> bool:
    return isinstance(rects, list) and bool(rects) and all(_valid_rect(rect) for rect in rects)


def _rect_within(inner: list[float], outer: list[float], tolerance: float = 0.01) -> bool:
    return (
        inner[0] >= outer[0] - tolerance
        and inner[1] >= outer[1] - tolerance
        and inner[2] <= outer[2] + tolerance
        and inner[3] <= outer[3] + tolerance
    )


def _rule_ids(items: list[dict[str, Any]]) -> list[str]:
    return [str(item.get("ruleId", "")) for item in items]


class WorkbenchContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = HTML_PATH.read_text(encoding="utf-8")
        cls.module_source = _module_source(cls.html)
        cls.contract = _evaluate_fixtures(cls.module_source)
        cls.result_2795 = json.loads(RESULT_2795_PATH.read_text(encoding="utf-8"))

    def test_local_pdfjs_assets_exist_and_are_referenced(self) -> None:
        import_match = re.search(
            r"import\s+\*\s+as\s+pdfjsLib\s+from\s+[\"']([^\"']+)[\"']",
            self.module_source,
        )
        worker_match = re.search(
            r"pdfjsLib\.GlobalWorkerOptions\.workerSrc\s*=\s*[\"']([^\"']+)[\"']",
            self.module_source,
        )
        self.assertIsNotNone(import_match, "prototype must import its local PDF.js module")
        self.assertIsNotNone(worker_match, "prototype must configure its local PDF.js worker")

        references = {
            "main": import_match.group(1),
            "worker": worker_match.group(1),
        }
        expected_names = {"main": "pdf.min.mjs", "worker": "pdf.worker.min.mjs"}
        for role, reference in references.items():
            with self.subTest(role=role):
                self.assertNotIn("://", reference, "PDF.js must be vendored, not loaded remotely")
                asset = (HTML_PATH.parent / reference).resolve()
                self.assertEqual(asset.name, expected_names[role])
                self.assertTrue(asset.is_file(), f"missing vendored PDF.js {role} asset: {asset}")
                self.assertGreater(asset.stat().st_size, 0)

    def test_self_mode_has_single_document_full_width_switching(self) -> None:
        css_rule = re.search(
            r"\.evidence-grid\.single-document\s*\{(?P<body>[^}]*)\}",
            self.html,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(css_rule, "single-document grid CSS is missing")
        self.assertRegex(
            css_rule.group("body"),
            r"grid-template-columns\s*:\s*(?:minmax\(\s*0\s*,\s*1fr\s*\)|1fr)\s*;",
        )

        toggle = re.search(
            r"\b[\w$.]+\.classList\.toggle\(\s*[\"']single-document[\"']\s*,",
            self.module_source,
        )
        self.assertIsNotNone(
            toggle,
            "rendering logic must toggle the single-document class from the active case",
        )

        cases = self.contract["cases"]
        for case_id in ("2948-self", "2795-self"):
            with self.subTest(case=case_id):
                self.assertEqual(cases[case_id]["mode"], "self")
                self.assertEqual(set(cases[case_id]["documents"]), {"report"})

    def test_finding_renderer_mounts_pdfjs_viewers_without_iframe_fallback(self) -> None:
        render_match = re.search(
            r"function\s+renderEvidence\s*\([^)]*\)\s*\{(?P<body>.*?)\n\s*\}\n\n\s*function\s+statusLead",
            self.module_source,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(render_match, "renderEvidence function is missing")
        body = render_match.group("body")
        self.assertIn("disposePdfViewers()", body)
        self.assertIn("classList.toggle('single-document'", body)
        self.assertIn("mountPdfViewers(", body)
        self.assertIn("evidencePanes", body)
        self.assertNotIn("frame-", body)
        self.assertNotIn("iframe", body.lower())

        self.assertNotRegex(self.html, r"<iframe\b")
        self.assertNotIn("behavior: 'instant'", self.module_source)
        self.assertIn("initialFocusPending", self.module_source)
        self.assertIn("preserveScroll", self.module_source)
        self.assertIn("safeRenderPdfPage", self.module_source)
        self.assertIn("prefers-reduced-motion: reduce", self.module_source)

    def test_record_mode_copy_does_not_aggregate_report_self_findings(self) -> None:
        self.assertNotIn("报告公共基线", self.html)
        self.assertNotIn("但 REPORT-R07", self.html)
        self.assertNotIn("即使本项经人工复核为一致，REPORT-R07", self.html)
        self.assertIn("本 Record 比对模式不聚合 Report 自检问题", self.html)
        self.assertIn('id="keep-manual"', self.html)
        self.assertIn("无法辨认，保持待复核", self.html)
        self.assertIn("演示重算（不保存）", self.html)

    def test_fill_demo_clears_stale_manual_review_message(self) -> None:
        fill_handler = re.search(
            r"getElementById\('fill-demo'\)\.addEventListener\('click',\s*\(\)\s*=>\s*\{"
            r"(?P<body>.*?)\n\s*\}\);",
            self.module_source,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(fill_handler, "fill-demo click handler is missing")
        body = fill_handler.group("body")
        self.assertIn("state.reviewDeferred = false;", body)
        self.assertIn("validationMessage.textContent = '';", body)
        self.assertLess(
            body.index("validationMessage.textContent = '';"),
            body.index("observedInputs.forEach"),
            "the stale manual-review message must clear before demo values are exposed",
        )

    def test_document_switcher_restores_selection_after_responsive_resize(self) -> None:
        switcher = re.search(
            r"function\s+renderDocumentSwitcher\([^)]*\)\s*\{(?P<body>.*?)"
            r"\n\s*\}\n\n\s*const\s+pdfDocumentCache",
            self.module_source,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(switcher, "renderDocumentSwitcher function is missing")
        body = switcher.group("body")
        self.assertIn("let selectedIndex = preferredIndex;", body)
        self.assertIn("selectedIndex = index;", body)
        self.assertIn("window.addEventListener('resize', restoreSelectionAfterResize)", body)
        self.assertIn("window.removeEventListener('resize', restoreSelectionAfterResize)", body)
        self.assertIn("scrollToDocument(targetIndex, false);", body)
        self.assertIn("restoringAfterResize", body)
        self.assertIn("compactDocuments.matches !== wasCompact", body)
        self.assertIn("programmaticTargetIndex = index;", body)
        self.assertIn("programmaticTargetIndex ?? selectedIndex", body)
        self.assertIn("Math.abs(evidenceGrid.scrollLeft - documentLeft(targetIndex)) <= 2", body)
        self.assertIn("evidenceGrid.addEventListener('pointerdown', clearProgrammaticTarget)", body)
        self.assertIn("cancelAnimationFrame(restoreBehaviorFrame)", body)

    def test_pdf_resize_preserves_normalized_content_anchor(self) -> None:
        renderer = re.search(
            r"async\s+function\s+renderPdfPage\([^)]*\)\s*\{(?P<body>.*?)"
            r"\n\s*\}\n\n\s*function\s+safeRenderPdfPage",
            self.module_source,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(renderer, "renderPdfPage function is missing")
        body = renderer.group("body")
        self.assertIn("normalizedScrollAnchor", body)
        self.assertIn("instance.scrollAnchor || readPdfScrollAnchor", body)
        self.assertIn("scrollPdfToAnchor(viewportElement, stage, normalizedScrollAnchor)", body)
        self.assertIn("instance.scrollAnchor = normalizedScrollAnchor", body)
        self.assertIn("finishScrollPosition(true)", body)
        self.assertNotIn(
            "focusEvidence: true, preserveScroll: true",
            body,
            "resize preservation must not force the user back to a Q highlight",
        )
        self.assertIn("instance.viewportElement?.addEventListener('scroll'", self.module_source)
        self.assertIn("instance.viewportElement?.removeEventListener('scroll'", self.module_source)
        self.assertIn("instance.restoringScrollAnchor = true;", self.module_source)

    def test_r07_highlights_target_the_actual_conclusion_glyphs(self) -> None:
        expected = {
            "2948-r07-132": [
                [[472.06, 411.099, 482.62, 421.659]],
                [[472.06, 423.219, 482.62, 433.779]],
            ],
            "2948-r07-140": [
                [[464.26, 450.839, 490.54, 461.399]],
                [[464.26, 252.309, 490.54, 262.869]],
            ],
            "2795-r07-3": [
                [[472.143, 697.982, 482.673, 708.542]],
                [[464.268, 266.462, 490.548, 277.022]],
            ],
        }
        fixtures = self.contract["sampleFixtures"]
        findings_by_id = {
            finding["id"]: finding
            for fixture in fixtures.values()
            for finding in fixture["findings"]
        }
        for finding_id, expected_rects in expected.items():
            with self.subTest(finding=finding_id):
                self.assertEqual(
                    [pane["rects"] for pane in findings_by_id[finding_id]["panes"]],
                    expected_rects,
                )

    def test_mode_findings_keep_record_and_self_runs_isolated(self) -> None:
        expected = {
            "2948": ("record61", "RECORD61-", "2948-record61", "findings2948Record"),
            "2795": ("record202", "RECORD202-", "2795-record202", "findings2795Record"),
            "2948-self": ("self", "REPORT-", "2948-self", "findings2948Self"),
            "2795-self": ("self", "REPORT-", "2795-self", "findings2795Self"),
        }
        cases = self.contract["cases"]
        for case_id, (mode, prefix, probe_name, derived_name) in expected.items():
            with self.subTest(case=case_id):
                findings = cases[case_id]["findings"]
                self.assertTrue(findings, f"{case_id} must expose at least one finding")
                self.assertEqual(cases[case_id]["mode"], mode)
                self.assertTrue(all(rule_id.startswith(prefix) for rule_id in _rule_ids(findings)))
                self.assertEqual(findings, self.contract["modeProbes"][probe_name])
                self.assertEqual(findings, self.contract["derived"][derived_name])

        for case_id, expected_rule in (
            ("2948-self", "REPORT-R07"),
            ("2795-self", "REPORT-R07"),
        ):
            with self.subTest(case=case_id, rule=expected_rule):
                self.assertIn(expected_rule, _rule_ids(cases[case_id]["findings"]))

    def test_every_error_or_manual_pane_has_coordinates_or_is_protected(self) -> None:
        for case_id, case_data in self.contract["cases"].items():
            for finding in case_data["findings"]:
                if finding["status"] not in {"error", "manual"}:
                    continue
                panes = finding.get("panes")
                self.assertIsInstance(panes, list, f"{case_id}/{finding['id']} panes missing")
                self.assertTrue(panes, f"{case_id}/{finding['id']} must have evidence panes")
                for index, pane in enumerate(panes):
                    with self.subTest(case=case_id, finding=finding["id"], pane=index):
                        self.assertTrue(
                            pane.get("protected") is True or _valid_rects(pane.get("rects")),
                            "each error/manual pane needs real rects or an explicit protected target",
                        )

    def test_2795_number_coordinates_match_machine_evidence(self) -> None:
        prototype = _finding(
            self.contract["sampleFixtures"]["2795"]["findings"],
            "RECORD202-NUMBER",
        )
        machine = _machine_finding(self.result_2795, "RECORD202-NUMBER")
        machine_by_image = {
            PurePosixPath(item["image"]).name: item for item in machine["evidence"]
        }

        self.assertEqual(len(prototype["panes"]), 2)
        for pane in prototype["panes"]:
            image_name = PurePosixPath(pane["src"]).name
            with self.subTest(image=image_name):
                self.assertIn(image_name, machine_by_image)
                machine_evidence = machine_by_image[image_name]
                self.assertEqual(_page_number(pane["page"]), machine_evidence["pdf_page"])
                self.assertEqual(pane["rects"], machine_evidence["rects"])

    def test_2795_symbol_coordinates_match_machine_evidence_and_mismatch(self) -> None:
        prototype = _finding(
            self.contract["sampleFixtures"]["2795"]["findings"],
            "RECORD202-SYMBOLS",
        )
        machine = _machine_finding(self.result_2795, "RECORD202-SYMBOLS")
        machine_by_image = {
            PurePosixPath(item["image"]).name: item for item in machine["evidence"]
        }

        self.assertEqual(len(prototype["panes"]), 2)
        prototype_pages: set[int] = set()
        for pane in prototype["panes"]:
            image_name = PurePosixPath(pane["src"]).name
            with self.subTest(image=image_name):
                self.assertIn(image_name, machine_by_image)
                machine_evidence = machine_by_image[image_name]
                pane_page = _page_number(pane["page"])
                prototype_pages.add(pane_page)
                self.assertEqual(pane_page, machine_evidence["pdf_page"])
                self.assertTrue(_valid_rects(pane["rects"]))
                for rect in pane["rects"]:
                    self.assertTrue(
                        any(_rect_within(rect, source_rect) for source_rect in machine_evidence["rects"]),
                        f"prototype highlight {rect!r} must stay inside machine evidence",
                    )

        mismatches = [
            item
            for item in machine["details"]["comparisons"]
            if item.get("decision") == "mismatch"
        ]
        self.assertEqual(len(mismatches), 1)
        mismatch = mismatches[0]
        self.assertEqual(mismatch["clause"], "201.15.101.9")
        self.assertEqual(mismatch["record_symbol"], "√")
        self.assertEqual(mismatch["report_result"], "——")
        self.assertTrue(_valid_rect(mismatch["record_cell"]))
        self.assertEqual(
            prototype_pages,
            {mismatch["record_pdf_page"], mismatch["report_pdf_page"]},
        )


if __name__ == "__main__":
    unittest.main()
