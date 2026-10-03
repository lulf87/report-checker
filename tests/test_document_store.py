from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import fitz

from mvp.document_store import DocumentStore
from mvp.run_store import RunStore, RunStoreError


def one_page_pdf() -> bytes:
    document = fitz.open()
    document.new_page().insert_text((72, 72), "test")
    content = document.tobytes()
    document.close()
    return content


class DocumentStoreTests(unittest.TestCase):
    def test_pdf_is_content_addressed_and_role_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = RunStore(root / "state.sqlite3")
            documents = DocumentStore(store, root / "blobs")
            case = store.create_case("case")
            content = one_page_pdf()
            report = documents.upload_pdf(
                case_id=case["id"], role="report", original_filename="report.pdf", content=content
            )
            record = documents.upload_pdf(
                case_id=case["id"], role="record_9706_1", original_filename="record.pdf", content=content
            )
            self.assertEqual(report["blob"]["sha256"], record["blob"]["sha256"])
            self.assertEqual(report["blob"]["id"], record["blob"]["id"])
            self.assertNotIn("storage_relpath", report["blob"])
            self.assertEqual(documents.read_content(report["id"]), content)
            snapshot = documents.validate_inputs(
                case_id=case["id"],
                mode="report_record_9706_1",
                inputs={"report_document_id": report["id"], "record_document_id": record["id"]},
            )
            self.assertEqual(snapshot["report"]["blob_sha256"], snapshot["record_9706_1"]["blob_sha256"])
            with self.assertRaises(RunStoreError) as mismatch:
                documents.validate_inputs(
                    case_id=case["id"],
                    mode="report_self",
                    inputs={"report_document_id": record["id"]},
                )
            self.assertEqual(mismatch.exception.code, "DOCUMENT_ROLE_MISMATCH")
            store.close()

    def test_invalid_pdf_is_rejected_before_document_insert(self) -> None:
        store = RunStore()
        documents = DocumentStore(store, tempfile.mkdtemp())
        case = store.create_case("case")
        with self.assertRaises(RunStoreError) as error:
            documents.upload_pdf(case_id=case["id"], role="report", original_filename="x.pdf", content=b"bad")
        self.assertEqual(error.exception.code, "INVALID_PDF")
        self.assertEqual(documents.list_documents(case["id"]), [])
        store.close()


if __name__ == "__main__":
    unittest.main()
