from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mvp.capabilities import MODE_CATALOG, RULE_CATALOG
from mvp.ptr_compare import MATCHED
from mvp.run_ptr_report import FORMAL_RESULT_SCHEMA_VERSION, PtrFormalRunnerError, run_ptr_report


class PtrFormalRunnerTests(unittest.TestCase):
    def _comparison(self, report: Path, ptr: Path) -> dict:
        evidence = [
            {"role": "ptr", "pdf_page": 1, "bbox": [1, 1, 10, 10], "source_sha256": "p" * 64, "text": "PTR"},
            {"role": "report", "pdf_page": 1, "bbox": [2, 2, 11, 11], "source_sha256": "r" * 64, "text": "Report"},
        ]
        entry = {
            "id": "comparison:2.1:ptr:clause:2.1",
            "clause_id": "2.1",
            "decision": MATCHED,
            "reason_code": "EXACT_NORMALIZED_MATCH",
            "scope_decision": "eligible",
            "scope_reason_code": "SCOPE_INCLUDED",
            "ptr_node_id": "ptr:clause:2.1",
            "report_item_ids": ["report:clause:2.1"],
            "evidence": evidence,
            "title_comparison": {"status": MATCHED},
            "text_comparison": {"status": MATCHED},
            "numeric_comparison": {"status": MATCHED},
            "unit_comparison": {"status": MATCHED},
            "table_comparison": {"status": MATCHED},
            "reference_comparison": {"status": MATCHED},
        }
        return {
            "rule": {"id": "PTR-P01", "version": "0.4.0-draft"},
            "scope": {"status": "parsed"},
            "coverage_ledger": [entry],
            "summary": {"counts": {"matched": 1, "mismatch": 0, "manual": 0, "excluded": 0}, "eligible": 1, "accounted": 1, "conserved": True},
            "coverage_audit": {"status": "conserved"},
            "reference_audit": {"status": "resolved"},
            "llm": {"used": False, "status": "disabled", "review_queue": []},
        }

    def test_formal_runner_requires_explicit_enablement_argument(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(PtrFormalRunnerError):
                run_ptr_report(
                    report_path=root / "report.pdf",
                    ptr_path=root / "ptr.pdf",
                    output_dir=root / "output",
                )
            self.assertFalse((root / "output").exists())

    def test_formal_runner_materializes_publishable_role_bound_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = root / "report.pdf"
            ptr = root / "ptr.pdf"
            report.write_bytes(b"report")
            ptr.write_bytes(b"ptr")
            output = root / "output"
            with patch.dict(MODE_CATALOG["report_ptr"], {"enabled": True, "status": "enabled"}), patch.dict(
                RULE_CATALOG["PTR-P01"], {"enabled": True, "status": "validated", "catalog_status": "enabled"}
            ), patch("mvp.run_ptr_report.compare_ptr_report", return_value=self._comparison(report, ptr)):
                result = run_ptr_report(
                    report_path=report,
                    ptr_path=ptr,
                    output_dir=output,
                    allow_formal_publish=True,
                )
            self.assertTrue((output / "result.json").is_file())
            saved = json.loads((output / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(result["schema_version"], FORMAL_RESULT_SCHEMA_VERSION)
        self.assertEqual(result["mode"], "report_ptr")
        self.assertTrue(result["publishable"])
        self.assertEqual(result["findings"][0]["evidence_locations"][0]["role"], "ptr")
        self.assertTrue(saved["publishable"])


if __name__ == "__main__":
    unittest.main()
