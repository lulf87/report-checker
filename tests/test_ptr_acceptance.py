from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from mvp.ptr_acceptance import audit_review_batch, ptr_lifecycle_gate_diagnostics
from mvp.ptr_compare import EXCLUDED, MANUAL, MATCHED, MISMATCH
from mvp.ptr_review_package import apply_review_csv, build_ptr_review_package


ROOT = Path(__file__).resolve().parents[1]


class PtrAcceptanceGateTests(unittest.TestCase):
    def test_missing_reviewed_package_cannot_enable_ptr(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = audit_review_batch(root=ROOT, batch_dir=Path(temporary), samples=("1539",))
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["can_enable_report_ptr"])
        self.assertEqual(result["samples"][0]["reason_code"], "REVIEWED_PACKAGE_MISSING")
        self.assertEqual(result["capability_gate"]["next_action"], "COMPLETE_TRIAL_REVIEW")

    def test_complete_reviewed_package_is_ready_only_for_signoff(self) -> None:
        report = next((ROOT / "素材/report/1539").glob("*.pdf"))
        ptr = next((ROOT / "素材/ptr/1539").glob("*.pdf"))
        with tempfile.TemporaryDirectory() as temporary:
            batch = Path(temporary)
            sample_dir = batch / "1539"
            manifest = build_ptr_review_package(report_path=report, ptr_path=ptr, output_dir=sample_dir, ocr_mode="never")
            rows = list(csv.DictReader((sample_dir / "review.csv").read_text(encoding="utf-8").splitlines()))
            for row in rows:
                machine = row["decision"]
                row["review_status"] = "excluded" if machine == EXCLUDED else "confirmed"
                row["review_decision"] = {
                    EXCLUDED: "confirm_excluded",
                    MATCHED: "confirm_match",
                    MISMATCH: "confirm_mismatch",
                    MANUAL: "confirm_manual",
                }[machine]
            filled = sample_dir / "filled.csv"
            with filled.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
                writer.writeheader()
                writer.writerows(rows)
            apply_review_csv(
                manifest_path=sample_dir / "manifest.json",
                csv_path=filled,
                output_path=sample_dir / "reviewed.json",
                reviewer="Codex test reviewer",
            )
            result = audit_review_batch(root=ROOT, batch_dir=batch, samples=("1539",))
        self.assertEqual(result["status"], "ready_for_signoff")
        self.assertFalse(result["can_enable_report_ptr"])
        gate = result["capability_gate"]
        self.assertEqual(gate["next_action"], "BUSINESS_SIGNOFF_REQUIRED")
        self.assertEqual(gate["catalog_status"], "disabled")
        self.assertFalse(gate["enabled"])
        self.assertEqual(gate["disabled_reason_code"], "PTR_NOT_VALIDATED")
        self.assertEqual(
            set(gate["operations"]),
            {"preflight", "create_run", "worker_job", "coordinator", "publish_result"},
        )
        self.assertTrue(all(not check["allowed"] for check in gate["operations"].values()))
        self.assertTrue(all(check["code"] == "MODE_DISABLED" for check in gate["operations"].values()))
        self.assertEqual(gate["operations"]["preflight"]["effect"], "blocked_plan")
        self.assertTrue(
            all(
                gate["operations"][name]["effect"] == "rejected"
                for name in ("create_run", "worker_job", "coordinator", "publish_result")
            )
        )

    def test_lifecycle_diagnostic_is_closed_over_the_formal_boundaries(self) -> None:
        diagnostic = ptr_lifecycle_gate_diagnostics()
        self.assertEqual(diagnostic["mode"], "report_ptr")
        self.assertFalse(diagnostic["enabled"])
        self.assertEqual(diagnostic["catalog_status"], "disabled")
        self.assertEqual(diagnostic["disabled_reason_code"], "PTR_NOT_VALIDATED")
        for operation, check in diagnostic["operations"].items():
            with self.subTest(operation=operation):
                self.assertFalse(check["allowed"])
                self.assertEqual(check["code"], "MODE_DISABLED")
                self.assertEqual(check["disabled_reason_code"], "PTR_NOT_VALIDATED")

    def test_acceptance_rejects_a_reviewed_package_marked_publishable(self) -> None:
        report = next((ROOT / "素材/report/1539").glob("*.pdf"))
        ptr = next((ROOT / "素材/ptr/1539").glob("*.pdf"))
        with tempfile.TemporaryDirectory() as temporary:
            batch = Path(temporary)
            sample_dir = batch / "1539"
            build_ptr_review_package(report_path=report, ptr_path=ptr, output_dir=sample_dir, ocr_mode="never")
            rows = list(csv.DictReader((sample_dir / "review.csv").read_text(encoding="utf-8").splitlines()))
            for row in rows:
                machine = row["decision"]
                row["review_status"] = "excluded" if machine == EXCLUDED else "confirmed"
                row["review_decision"] = {
                    EXCLUDED: "confirm_excluded",
                    MATCHED: "confirm_match",
                    MISMATCH: "confirm_mismatch",
                    MANUAL: "confirm_manual",
                }[machine]
            filled = sample_dir / "filled.csv"
            with filled.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
                writer.writeheader()
                writer.writerows(rows)
            apply_review_csv(
                manifest_path=sample_dir / "manifest.json",
                csv_path=filled,
                output_path=sample_dir / "reviewed.json",
                reviewer="Codex test reviewer",
            )
            reviewed_path = sample_dir / "reviewed.json"
            reviewed = json.loads(reviewed_path.read_text(encoding="utf-8"))
            reviewed["publishable"] = True
            reviewed_path.write_text(json.dumps(reviewed, ensure_ascii=False), encoding="utf-8")
            result = audit_review_batch(root=ROOT, batch_dir=batch, samples=("1539",))
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["samples"][0]["reason_code"], "REVIEWED_PACKAGE_PUBLISHABLE")
        self.assertFalse(result["samples"][0]["package_policy_ok"])

    def test_acceptance_rejects_a_reviewed_package_from_an_old_rule_version(self) -> None:
        report = next((ROOT / "素材/report/1539").glob("*.pdf"))
        ptr = next((ROOT / "素材/ptr/1539").glob("*.pdf"))
        with tempfile.TemporaryDirectory() as temporary:
            batch = Path(temporary)
            sample_dir = batch / "1539"
            build_ptr_review_package(report_path=report, ptr_path=ptr, output_dir=sample_dir, ocr_mode="never")
            manifest_path = sample_dir / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["rule"]["version"] = "0.3.0-draft"
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
            rows = list(csv.DictReader((sample_dir / "review.csv").read_text(encoding="utf-8").splitlines()))
            for row in rows:
                machine = row["decision"]
                row["review_status"] = "excluded" if machine == EXCLUDED else "confirmed"
                row["review_decision"] = {
                    EXCLUDED: "confirm_excluded",
                    MATCHED: "confirm_match",
                    MISMATCH: "confirm_mismatch",
                    MANUAL: "confirm_manual",
                }[machine]
            filled = sample_dir / "filled.csv"
            with filled.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
                writer.writeheader()
                writer.writerows(rows)
            apply_review_csv(
                manifest_path=manifest_path,
                csv_path=filled,
                output_path=sample_dir / "reviewed.json",
                reviewer="Codex test reviewer",
            )
            result = audit_review_batch(root=ROOT, batch_dir=batch, samples=("1539",))
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["samples"][0]["reason_code"], "REVIEW_RULE_VERSION_MISMATCH")
        self.assertFalse(result["samples"][0]["rule_version_ok"])

    def test_acceptance_rejects_machine_coverage_that_was_not_conserved(self) -> None:
        report = next((ROOT / "素材/report/1539").glob("*.pdf"))
        ptr = next((ROOT / "素材/ptr/1539").glob("*.pdf"))
        with tempfile.TemporaryDirectory() as temporary:
            batch = Path(temporary)
            sample_dir = batch / "1539"
            build_ptr_review_package(report_path=report, ptr_path=ptr, output_dir=sample_dir, ocr_mode="never")
            manifest_path = sample_dir / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["coverage_audit"]["status"] = "incomplete"
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
            rows = list(csv.DictReader((sample_dir / "review.csv").read_text(encoding="utf-8").splitlines()))
            for row in rows:
                machine = row["decision"]
                row["review_status"] = "excluded" if machine == EXCLUDED else "confirmed"
                row["review_decision"] = {
                    EXCLUDED: "confirm_excluded",
                    MATCHED: "confirm_match",
                    MISMATCH: "confirm_mismatch",
                    MANUAL: "confirm_manual",
                }[machine]
            filled = sample_dir / "filled.csv"
            with filled.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
                writer.writeheader()
                writer.writerows(rows)
            apply_review_csv(
                manifest_path=manifest_path,
                csv_path=filled,
                output_path=sample_dir / "reviewed.json",
                reviewer="Codex test reviewer",
            )
            result = audit_review_batch(root=ROOT, batch_dir=batch, samples=("1539",))
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["samples"][0]["reason_code"], "MACHINE_COVERAGE_INCOMPLETE")
        self.assertFalse(result["samples"][0]["machine_coverage_ok"])


if __name__ == "__main__":
    unittest.main()
