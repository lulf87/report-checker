"""不可变 PDF Blob 与 Case Document 存储。

Document 只保存角色、文件元数据和 Blob 哈希；原始 PDF 按内容哈希写入受控的
相对目录，Run 创建时把哈希快照和角色绑定到 RunInput。
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

try:
    import pymupdf as fitz
except ImportError:
    import fitz

from mvp.capabilities import DOCUMENT_ROLES, MODE_CATALOG
from mvp.run_store import RunStore, RunStoreError, _json, _now


MAX_FILE_BYTES = 524_288_000
MAX_PAGES = 2_000

_INPUT_ROLE_KEYS = {
    "report": "report_document_id",
    "ptr": "ptr_document_id",
    "record_9706_1": "record_document_id",
    "record_9706_202": "record_document_id",
}


class DocumentStore:
    """Store uploaded PDFs and their immutable database metadata."""

    def __init__(self, store: RunStore, storage_root: str | Path = "output/blob-store") -> None:
        self.store = store
        self.storage_root = Path(storage_root).expanduser().resolve()
        self.storage_root.mkdir(parents=True, exist_ok=True)

    def upload_pdf(
        self,
        *,
        case_id: str,
        role: str,
        original_filename: str,
        content: bytes,
    ) -> dict[str, Any]:
        self.store.get_case(case_id)
        if role not in DOCUMENT_ROLES:
            raise RunStoreError("INVALID_DOCUMENT_ROLE", "Document role 不受支持", details={"role": role})
        if not isinstance(original_filename, str) or not original_filename.strip() or len(original_filename) > 255:
            raise RunStoreError("INVALID_DOCUMENT_FILENAME", "原始文件名必须为 1–255 个字符")
        if not isinstance(content, bytes) or not content:
            raise RunStoreError("EMPTY_DOCUMENT", "上传文件不能为空")
        if len(content) > MAX_FILE_BYTES:
            raise RunStoreError("DOCUMENT_TOO_LARGE", "PDF 超过 500 MiB 限制", status=413)
        try:
            document = fitz.open(stream=content, filetype="pdf")
        except Exception as exc:
            raise RunStoreError("INVALID_PDF", "上传文件不是可读取的 PDF") from exc
        try:
            encrypted = bool(document.is_encrypted)
            if encrypted or document.needs_pass:
                raise RunStoreError("ENCRYPTED_PDF_UNSUPPORTED", "不支持加密 PDF")
            page_count = int(document.page_count)
            if page_count < 1:
                raise RunStoreError("EMPTY_PDF", "PDF 必须至少包含一页")
            if page_count > MAX_PAGES:
                raise RunStoreError("PDF_TOO_MANY_PAGES", "PDF 页数超过 2000 页限制")
            try:
                pdf_version = str(document.pdf_version())
            except Exception:
                pdf_version = None
        finally:
            document.close()

        digest = hashlib.sha256(content).hexdigest()
        relative_path = Path(digest[:2]) / f"{digest}.pdf"
        absolute_path = self.storage_root / relative_path
        absolute_path.parent.mkdir(parents=True, exist_ok=True)
        if not absolute_path.exists():
            temp_name: str | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="wb", dir=absolute_path.parent, prefix=f".{digest}.", suffix=".upload", delete=False
                ) as handle:
                    temp_name = handle.name
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_name, absolute_path)
                temp_name = None
            finally:
                if temp_name:
                    try:
                        os.unlink(temp_name)
                    except FileNotFoundError:
                        pass

        timestamp = _now()
        preflight = {
            "status": "passed",
            "media_type": "application/pdf",
            "page_count": page_count,
            "encrypted": encrypted,
        }
        document_id = str(uuid4())
        with self.store._lock:
            self.store._connection.execute("BEGIN IMMEDIATE")
            try:
                blob = self.store._connection.execute(
                    "SELECT id, sha256, size_bytes, media_type, storage_relpath, created_at FROM blobs WHERE sha256 = ?",
                    (digest,),
                ).fetchone()
                if blob is None:
                    blob_id = str(uuid4())
                    self.store._connection.execute(
                        """INSERT INTO blobs(id, sha256, size_bytes, media_type, storage_relpath, created_at)
                           VALUES(?, ?, ?, 'application/pdf', ?, ?)""",
                        (blob_id, digest, len(content), str(relative_path), timestamp),
                    )
                else:
                    blob_id = str(blob["id"])
                self.store._connection.execute(
                    """INSERT INTO documents(
                        id, case_id, blob_id, role, original_filename, page_count, pdf_version,
                        encrypted, preflight_status, preflight_json, created_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        document_id,
                        case_id,
                        blob_id,
                        role,
                        original_filename.strip(),
                        page_count,
                        pdf_version,
                        int(encrypted),
                        "passed",
                        _json(preflight),
                        timestamp,
                    ),
                )
                self.store._connection.execute(
                    "UPDATE cases SET revision = revision + 1, updated_at = ? WHERE id = ?",
                    (timestamp, case_id),
                )
                self.store._connection.execute("COMMIT")
            except Exception:
                self.store._connection.execute("ROLLBACK")
                raise
        return self.get_document(document_id)

    def get_document(self, document_id: str) -> dict[str, Any]:
        with self.store._lock:
            row = self.store._connection.execute(
                """SELECT d.*, b.sha256, b.size_bytes, b.media_type, b.storage_relpath, b.id AS blob_id
                   FROM documents d JOIN blobs b ON b.id = d.blob_id WHERE d.id = ?""",
                (document_id,),
            ).fetchone()
            if row is None:
                raise RunStoreError("DOCUMENT_NOT_FOUND", "Document 不存在", status=404, details={"document_id": document_id})
            return self._document_from_row(row)

    def list_documents(self, case_id: str, *, role: str | None = None) -> list[dict[str, Any]]:
        self.store.get_case(case_id)
        if role is not None and role not in DOCUMENT_ROLES:
            raise RunStoreError("INVALID_DOCUMENT_ROLE", "Document role 不受支持", details={"role": role})
        with self.store._lock:
            if role is None:
                rows = self.store._connection.execute(
                    """SELECT d.*, b.sha256, b.size_bytes, b.media_type, b.storage_relpath, b.id AS blob_id
                       FROM documents d JOIN blobs b ON b.id = d.blob_id
                       WHERE d.case_id = ? ORDER BY d.created_at, d.id""",
                    (case_id,),
                ).fetchall()
            else:
                rows = self.store._connection.execute(
                    """SELECT d.*, b.sha256, b.size_bytes, b.media_type, b.storage_relpath, b.id AS blob_id
                       FROM documents d JOIN blobs b ON b.id = d.blob_id
                       WHERE d.case_id = ? AND d.role = ? ORDER BY d.created_at, d.id""",
                    (case_id, role),
                ).fetchall()
            return [self._document_from_row(row) for row in rows]

    def source_path(self, document_id: str) -> Path:
        document = self.get_document(document_id)
        with self.store._lock:
            row = self.store._connection.execute(
                "SELECT storage_relpath FROM blobs WHERE id = ?", (document["blob"]["id"],)
            ).fetchone()
        if row is None:
            raise RunStoreError("DOCUMENT_STORAGE_INVALID", "Document Blob 引用无效", status=500)
        relative = Path(str(row["storage_relpath"]))
        if relative.is_absolute() or ".." in relative.parts:
            raise RunStoreError("DOCUMENT_STORAGE_INVALID", "Document 存储引用无效", status=500)
        path = (self.storage_root / relative).resolve()
        if self.storage_root not in path.parents or not path.is_file():
            raise RunStoreError("DOCUMENT_CONTENT_MISSING", "Document 内容不存在", status=500)
        return path

    def read_content(self, document_id: str) -> bytes:
        try:
            return self.source_path(document_id).read_bytes()
        except OSError as exc:
            raise RunStoreError("DOCUMENT_CONTENT_MISSING", "Document 内容读取失败", status=500) from exc

    def validate_inputs(
        self,
        *,
        case_id: str,
        mode: str,
        inputs: Mapping[str, Any],
    ) -> dict[str, Any]:
        self.store.get_case(case_id)
        if mode not in MODE_CATALOG:
            raise RunStoreError("UNSUPPORTED_MODE", "模式不受支持", details={"mode": mode})
        normalized = self.store._normalize_inputs(mode, inputs)
        snapshot: dict[str, Any] = {}
        for role in MODE_CATALOG[mode]["required_roles"]:
            key = _INPUT_ROLE_KEYS[role]
            document = self.get_document(normalized[key])
            if document["case_id"] != case_id:
                raise RunStoreError(
                    "DOCUMENT_CASE_MISMATCH",
                    "输入 Document 不属于当前 Case",
                    details={"document_id": document["id"], "case_id": case_id},
                )
            if document["role"] != role:
                raise RunStoreError(
                    "DOCUMENT_ROLE_MISMATCH",
                    "输入 Document 角色与模式不匹配",
                    details={"document_id": document["id"], "expected_role": role, "actual_role": document["role"]},
                )
            snapshot[role] = {
                "document_id": document["id"],
                "blob_sha256": document["blob"]["sha256"],
                "size_bytes": document["blob"]["size_bytes"],
                "media_type": document["blob"]["media_type"],
                "original_filename": document["original_filename"],
                "page_count": document["page_count"],
                "pdf_version": document["pdf_version"],
                "preflight_status": document["preflight_status"],
            }
        return snapshot

    def bind_run_inputs(
        self,
        *,
        run_id: str,
        case_id: str,
        mode: str,
        inputs: Mapping[str, Any],
    ) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if run["case_id"] != case_id or run["mode"] != mode:
            raise RunStoreError("RUN_INPUT_BINDING_MISMATCH", "Run 与输入绑定的 Case 或模式不一致", status=409)
        if run["lifecycle_status"] != "queued":
            raise RunStoreError("RUN_INPUTS_IMMUTABLE", "只有 queued Run 可以绑定输入", status=409)
        snapshot = self.validate_inputs(case_id=case_id, mode=mode, inputs=inputs)
        with self.store._lock:
            self.store._connection.execute("BEGIN IMMEDIATE")
            try:
                for role, item in snapshot.items():
                    self.store._connection.execute(
                        """INSERT INTO run_inputs(run_id, role, document_id, blob_sha256)
                           VALUES(?, ?, ?, ?)""",
                        (run_id, role, item["document_id"], item["blob_sha256"]),
                    )
                self.store._connection.execute("COMMIT")
            except Exception:
                self.store._connection.execute("ROLLBACK")
                raise
        return snapshot

    @staticmethod
    def _document_from_row(row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "case_id": row["case_id"],
            "role": row["role"],
            "original_filename": row["original_filename"],
            "page_count": row["page_count"],
            "pdf_version": row["pdf_version"],
            "encrypted": bool(row["encrypted"]),
            "preflight_status": row["preflight_status"],
            "preflight": _json_load(row["preflight_json"]),
            "created_at": row["created_at"],
            "blob": {
                "id": row["blob_id"],
                "sha256": row["sha256"],
                "size_bytes": row["size_bytes"],
                "media_type": row["media_type"],
            },
        }


def _json_load(value: str) -> Any:
    import json

    return json.loads(value)
