from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from mvp.capabilities import MODE_CATALOG
from mvp.ptr_compare import PTR_COMPARE_RULE_ID, PTR_COMPARE_RULE_VERSION
from mvp.ptr_enablement import PTR_SIGNOFF_SCHEMA_VERSION, verify_ptr_enablement


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


class PtrEnablementTests(unittest.TestCase):
    def _gate(self) -> dict:
        return {
            "status": "ready_for_signoff",
            "can_enable_report_ptr": False,
            "capability_gate": {"enabled": False, "disabled_reason_code": "PTR_NOT_VALIDATED"},
            "samples": [{"sample": sample, "ready": True} for sample in ("2948", "0775", "1539")],
        }

    def _signoff(self, gate_path: Path) -> dict:
        return {
            "schema_version": PTR_SIGNOFF_SCHEMA_VERSION,
            "mode": "report_ptr",
            "approved": True,
            "reviewer": "业务负责人",
            "signed_at": "2026-10-24T22:00:00Z",
            "acceptance_gate_sha256": _sha256(gate_path),
            "rule": {"id": PTR_COMPARE_RULE_ID, "version": PTR_COMPARE_RULE_VERSION},
            "samples": [{"sample": sample, "approved": True} for sample in ("2948", "0775", "1539")],
        }

    def test_missing_signoff_is_blocked_without_mutating_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            gate = root / "gate.json"
            gate.write_text(json.dumps(self._gate()), encoding="utf-8")
            result = verify_ptr_enablement(acceptance_gate_path=gate, signoff_path=root / "missing.json")
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["can_enable_report_ptr"])
        self.assertEqual(result["reason_code"], "BUSINESS_SIGNOFF_REQUIRED")
        self.assertTrue(MODE_CATALOG["report_ptr"]["enabled"])

    def test_valid_signoff_only_returns_an_enablement_decision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            gate = root / "gate.json"
            signoff = root / "signoff.json"
            gate.write_text(json.dumps(self._gate(), ensure_ascii=False), encoding="utf-8")
            signoff.write_text(json.dumps(self._signoff(gate), ensure_ascii=False), encoding="utf-8")
            result = verify_ptr_enablement(acceptance_gate_path=gate, signoff_path=signoff)
        self.assertEqual(result["status"], "approved_for_enablement")
        self.assertTrue(result["can_enable_report_ptr"])
        self.assertEqual(result["mutation"], "none")
        self.assertTrue(result["current_catalog"]["enabled"])
        self.assertTrue(MODE_CATALOG["report_ptr"]["enabled"])

    def test_signoff_hash_binding_rejects_a_different_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            gate = root / "gate.json"
            signoff = root / "signoff.json"
            gate.write_text(json.dumps(self._gate(), ensure_ascii=False), encoding="utf-8")
            payload = self._signoff(gate)
            payload["acceptance_gate_sha256"] = "0" * 64
            signoff.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            result = verify_ptr_enablement(acceptance_gate_path=gate, signoff_path=signoff)
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["can_enable_report_ptr"])
        self.assertFalse(next(item for item in result["checks"] if item["id"] == "acceptance_gate_hash_bound")["passed"])


if __name__ == "__main__":
    unittest.main()
