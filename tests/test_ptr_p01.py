from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from mvp.capabilities import MODE_CATALOG
from mvp.ptr_p01 import (
    PTR_P01_DISABLED_REASON,
    PTR_P01_RULE_ID,
    PTR_P01_RULE_VERSION,
    PtrP01DraftError,
    build_ptr_p01_draft,
    validate_ptr_p01_draft,
    write_ptr_p01_draft,
)
from mvp.ptr_report_contract import (
    PTR_REPORT_CONTRACT_SCHEMA_VERSION,
    PTR_REPORT_INPUT_FIELDS,
    validate_ptr_report_contract,
)


ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "素材/report/0596/0596报告.pdf"
PTR = ROOT / "素材/ptr/0596/0596技术要求.pdf"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class PtrP01DraftTests(unittest.TestCase):
    def test_real_sample_draft_is_disabled_and_auditable(self) -> None:
        report_before = sha256(REPORT)
        ptr_before = sha256(PTR)
        result = build_ptr_p01_draft(REPORT, PTR, root=ROOT)
        validate_ptr_p01_draft(result)
        self.assertFalse(result["publishable"])
        self.assertEqual(result["rule"]["id"], PTR_P01_RULE_ID)
        self.assertEqual(result["rule"]["version"], PTR_P01_RULE_VERSION)
        self.assertEqual(result["rule"]["disabled_reason_code"], PTR_P01_DISABLED_REASON)
        self.assertEqual(result["contract"]["schema_version"], PTR_REPORT_CONTRACT_SCHEMA_VERSION)
        validate_ptr_report_contract(result["contract"], expected_mode="report_ptr")
        self.assertEqual(
            {slot["role"]: slot["field"] for slot in result["input_contract"]["slots"]},
            PTR_REPORT_INPUT_FIELDS,
        )
        self.assertEqual(result["rule_execution"]["state"], "unsupported")
        self.assertEqual(result["rule_execution"]["finding_count"], 0)
        self.assertEqual(result["findings"], [])
        graph = result["requirement_graph"]
        self.assertEqual(graph["status"], "draft_not_validated")
        self.assertGreater(len(graph["report_declared_scope_candidates"]), 0)
        self.assertGreater(len(graph["ptr_clause_candidates"]), 0)
        self.assertGreater(len(graph["reference_candidates"]), 0)
        scope = graph["report_declared_scope_candidates"][0]
        self.assertEqual(scope["scope_set"]["status"], "parsed")
        self.assertEqual(scope["scope_set"]["included_clause_ids"], ["2.1", "2.5", "2.6", "2.7", "2.8.1.2", "2.10", "2.11"])
        self.assertTrue(scope["evidence"][0]["extraction_method"].startswith("pymupdf_table_cell"))
        self.assertEqual(graph["coverage_audit"]["status"], "not_evaluated")
        self.assertEqual(sha256(REPORT), report_before)
        self.assertEqual(sha256(PTR), ptr_before)

    def test_finding_template_matches_evidence_contract_but_is_not_publishable(self) -> None:
        result = build_ptr_p01_draft(REPORT, PTR, root=ROOT)
        template = result["finding_template"]
        self.assertFalse(template["publishable"])
        self.assertEqual(template["rule_id"], PTR_P01_RULE_ID)
        self.assertEqual(template["rule_version"], PTR_P01_RULE_VERSION)
        self.assertEqual(template["status"], "manual")
        self.assertEqual(template["reason_code"], PTR_P01_DISABLED_REASON)
        self.assertTrue(template["details"]["review_required"])
        self.assertEqual(template["machine_status"], "manual")
        self.assertEqual(template["evidence_ids"], [])
        self.assertTrue(template["evidence_locations"])
        for location in template["evidence_locations"]:
            self.assertIn(location["role"], {"report", "ptr"})
            self.assertEqual(len(location["bbox"]), 4)
            self.assertTrue(location["source_sha256"])

    def test_invalid_evidence_geometry_is_rejected(self) -> None:
        result = build_ptr_p01_draft(REPORT, PTR, root=ROOT)
        broken = copy.deepcopy(result)
        location = broken["finding_template"]["evidence_locations"][0]
        location["bbox"][2] = 10**9
        with self.assertRaises(PtrP01DraftError):
            validate_ptr_p01_draft(broken)

    def test_parent_edges_keep_unresolved_parents_manual(self) -> None:
        result = build_ptr_p01_draft(REPORT, PTR, root=ROOT)
        edges = result["requirement_graph"]["edges"]
        self.assertTrue(edges)
        self.assertTrue(any(edge["validation_status"] == "manual_required" for edge in edges))
        self.assertTrue(all(edge["relation"] == "parent_child_candidate" for edge in edges))

    def test_cli_writer_is_deterministic_contract_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "draft.json"
            result = write_ptr_p01_draft(REPORT, PTR, target, root=ROOT)
            self.assertTrue(target.is_file())
            payload = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], result["schema_version"])
            self.assertFalse(payload["publishable"])
            self.assertEqual(payload["findings"], [])

    def test_capability_remains_disabled(self) -> None:
        self.assertFalse(MODE_CATALOG["report_ptr"]["enabled"])
        self.assertEqual(MODE_CATALOG["report_ptr"]["runner"], "mvp.run_ptr_report:run_ptr_report")
        self.assertEqual(MODE_CATALOG["report_ptr"]["disabled_reason_code"], PTR_P01_DISABLED_REASON)
        self.assertFalse(MODE_CATALOG["report_ptr_report"]["enabled"])


if __name__ == "__main__":
    unittest.main()
