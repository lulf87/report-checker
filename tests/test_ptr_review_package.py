from __future__ import annotations

import json
import csv
import tempfile
import unittest
from pathlib import Path

from mvp.ptr_compare import EXCLUDED, MANUAL, MATCHED, MISMATCH
from mvp.ptr_review_package import apply_review_csv, build_ptr_review_package


ROOT = Path(__file__).resolve().parents[1]


class PtrReviewPackageTests(unittest.TestCase):
    def test_package_is_reviewable_but_not_publishable(self) -> None:
        report = next((ROOT / "素材/report/1539").glob("*.pdf"))
        ptr = next((ROOT / "素材/ptr/1539").glob("*.pdf"))
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "review"
            manifest = build_ptr_review_package(
                report_path=report,
                ptr_path=ptr,
                output_dir=output,
                ocr_mode="never",
            )
            self.assertFalse(manifest["publishable"])
            self.assertTrue((output / "manifest.json").is_file())
            self.assertTrue((output / "review.csv").is_file())
            self.assertTrue((output / "review.md").is_file())
            saved = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["schema_version"], manifest["schema_version"])
            self.assertTrue(saved["rows"])
            self.assertEqual(saved["coverage_audit"]["status"], "conserved")
            self.assertEqual(saved["reference_audit"]["status"], "resolved")
            self.assertTrue(all(row["review_status"] in {"pending", "excluded"} for row in saved["rows"]))
            self.assertTrue(all(item["source_sha256"] for item in saved["inputs"].values()))

            filled = output / "filled.csv"
            rows = list(csv.DictReader((output / "review.csv").read_text(encoding="utf-8").splitlines()))
            for row in rows:
                machine = row["decision"]
                if machine == EXCLUDED:
                    row["review_status"] = "excluded"
                    row["review_decision"] = "confirm_excluded"
                else:
                    row["review_status"] = "confirmed"
                    row["review_decision"] = {
                        MATCHED: "confirm_match",
                        MISMATCH: "confirm_mismatch",
                        MANUAL: "confirm_manual",
                    }[machine]
                row["review_note"] = "fixture review"
            with filled.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
                writer.writeheader()
                writer.writerows(rows)
            reviewed = apply_review_csv(
                manifest_path=output / "manifest.json",
                csv_path=filled,
                output_path=output / "reviewed.json",
                reviewer="test-reviewer",
            )
            self.assertTrue(reviewed["review"]["review_complete"])
            self.assertFalse(reviewed["publishable"])
            self.assertEqual(len(reviewed["review"]["actions"]), len(saved["rows"]))

            tampered = output / "tampered.csv"
            tampered_rows = [dict(row) for row in rows]
            tampered_rows[0]["reason_code"] = "TAMPERED"
            with tampered.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=tampered_rows[0].keys())
                writer.writeheader()
                writer.writerows(tampered_rows)
            with self.assertRaises(ValueError):
                apply_review_csv(
                    manifest_path=output / "manifest.json",
                    csv_path=tampered,
                    output_path=output / "tampered-reviewed.json",
                    reviewer="test-reviewer",
                )


if __name__ == "__main__":
    unittest.main()
