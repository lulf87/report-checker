from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mvp.ptr_review_package import build_ptr_review_package


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
            self.assertTrue(all(row["review_status"] in {"pending", "excluded"} for row in saved["rows"]))
            self.assertTrue(all(item["source_sha256"] for item in saved["inputs"].values()))


if __name__ == "__main__":
    unittest.main()
