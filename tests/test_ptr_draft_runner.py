from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mvp.ptr_draft_runner import DRAFT_MODE, run_ptr_draft


ROOT = Path(__file__).resolve().parents[1]


class PtrDraftRunnerTests(unittest.TestCase):
    def test_real_2795_writes_reviewable_non_publishable_result(self) -> None:
        report = next((ROOT / "素材/report/2795").glob("*.pdf"))
        ptr = next((ROOT / "素材/ptr/2795").glob("*.pdf"))
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "draft"
            result = run_ptr_draft(report_path=report, ptr_path=ptr, output_dir=output, ocr_mode="never")
            self.assertEqual(result["mode"], DRAFT_MODE)
            self.assertFalse(result["publishable"])
            self.assertEqual(result["rule_execution_states"]["PTR-P01"]["state"], "succeeded")
            self.assertTrue(result["source_integrity"])
            self.assertTrue(result["coverage_ledger"])
            self.assertEqual(result["coverage_audit"]["status"], "conserved")
            self.assertEqual(result["reference_audit"]["status"], "resolved")
            self.assertTrue((output / "result.json").is_file())
            self.assertTrue((output / "index.html").is_file())
            saved = json.loads((output / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["mode"], DRAFT_MODE)
            self.assertFalse(saved["publishable"])

    def test_draft_result_keeps_report_and_ptr_roles(self) -> None:
        report = next((ROOT / "素材/report/1539").glob("*.pdf"))
        ptr = next((ROOT / "素材/ptr/1539").glob("*.pdf"))
        with tempfile.TemporaryDirectory() as temporary:
            result = run_ptr_draft(report_path=report, ptr_path=ptr, output_dir=Path(temporary) / "draft", ocr_mode="never")
        self.assertEqual([item["role"] for item in result["files"]], ["report", "ptr"])
        self.assertTrue(all(item["unchanged"] for item in result["source_integrity"]))
        # Vp/Ap and V/A are equivalent here because both sides explicitly
        # state 峰值; the remaining table item stays in the manual queue.
        self.assertEqual(result["status_counts"]["error"], 0)
        self.assertEqual(result["status_counts"]["manual"], 1)


if __name__ == "__main__":
    unittest.main()
