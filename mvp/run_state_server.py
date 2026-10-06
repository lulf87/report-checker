"""本机 Case/Document/Run 状态 API 与单 Worker 调度入口。"""

from __future__ import annotations

import argparse
import base64
import json
from email.parser import BytesParser
from email.policy import default as email_default_policy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import parse_qs, quote, urlsplit
from uuid import uuid4

from mvp.capabilities import MODE_CATALOG, RULE_BUNDLE_ID, preflight_plan_hash, rule_bundle_sha256, validate_mode
from mvp.capability_server import CapabilityRequestHandler
from mvp.document_store import DocumentStore, MAX_FILE_BYTES
from mvp.run_coordinator import CoordinatorError, execute_persisted_run
from mvp.run_store import RunStore, RunStoreError


MAX_JSON_BYTES = 1024 * 1024
MAX_UPLOAD_BODY_BYTES = MAX_FILE_BYTES + 16 * 1024 * 1024
INPUT_ROLE_KEYS = {
    "report": "report_document_id",
    "ptr": "ptr_document_id",
    "record_9706_1": "record_document_id",
    "record_9706_202": "record_document_id",
}


class RunStateHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        store: RunStore,
        *,
        storage_root: str = "output/blob-store",
        output_root: str = "output/runs",
        allow_unresolved_inputs: bool = False,
    ) -> None:
        # ``socketserver.TCPServer.__init__`` calls ``server_close`` when
        # binding or activation fails.  Seed the cleanup flags before the
        # parent constructor so a rejected bind cannot trip over attributes
        # that are initialized only after a successful bind.
        self.store = store
        self._executor = None
        self._executor_closed = True
        self._store_closed = False
        super().__init__(server_address, RunStateRequestHandler)
        self.documents = DocumentStore(store, storage_root)
        self.allow_unresolved_inputs = allow_unresolved_inputs
        self.output_root = Path(output_root).expanduser().resolve()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="report-checker-worker")
        self._executor_closed = False
        self._store_closed = False
        self.csrf_token = uuid4().hex
        self.session_id = uuid4().hex
        self.csrf_expires_at = datetime.now(timezone.utc) + timedelta(hours=8)

    def close(self) -> None:
        self.shutdown()
        self.server_close()

    def server_close(self) -> None:
        super().server_close()
        if not self._executor_closed and self._executor is not None:
            # Do not close SQLite while the background worker can still be
            # reading or publishing a Run. HTTP has already stopped accepting
            # requests at this point, so waiting gives the worker a clean
            # chance to finish and preserves the publish transaction.
            self._executor.shutdown(wait=True, cancel_futures=False)
            self._executor_closed = True
        if not self._store_closed:
            self.store.close()
            self._store_closed = True

    def submit_run(self, run_id: str) -> None:
        if self._executor_closed:
            raise RunStoreError("SERVER_CLOSED", "状态服务已关闭", status=503)
        self._executor.submit(self._execute_run, run_id)

    def _execute_run(self, run_id: str) -> None:
        try:
            run = self.store.get_run(run_id)
            if any(item.get("unresolved") for item in (run.get("input_snapshot") or {}).values()):
                return
            inputs = run["inputs"]
            report_path = self.documents.source_path(inputs["report_document_id"])
            record_path = None
            if run["mode"] != "report_self":
                record_key = "record_document_id"
                record_path = self.documents.source_path(inputs[record_key])
            execute_persisted_run(
                self.store,
                run_id,
                report_path=report_path,
                record_path=record_path,
                output_dir=self.output_root / run_id,
            )
        except (CoordinatorError, RunStoreError, OSError) as error:
            # The coordinator persists a stable failed state. The background
            # thread has no client to notify directly.
            try:
                current = self.store.get_run(run_id)
                if current["lifecycle_status"] == "queued":
                    self.store.transition_run(run_id, "running")
                    for execution in self.store.get_rule_executions(run_id):
                        if execution["execution_state"] == "pending":
                            self.store.transition_rule_execution(
                                run_id,
                                execution["rule_id"],
                                "failed",
                                reason_code="INPUT_CONTENT_UNAVAILABLE",
                                reason_detail={"message": str(error)[:500]},
                            )
                    self.store.transition_run(
                        run_id,
                        "failed",
                        reason_code="INPUT_CONTENT_UNAVAILABLE",
                        reason_message=str(error)[:1000],
                    )
            except RunStoreError:
                pass
            return


class RunStateRequestHandler(CapabilityRequestHandler):
    server: RunStateHTTPServer

    def _allowed_origin(self, origin: str | None) -> str | None:
        """Return a trusted loopback UI origin for CORS, if present.

        The workbench is served by the static server on port 8765 while this
        state API normally listens on 8767. Both are local-only services, so
        the browser needs an explicit CORS allowance for the workbench origin
        while arbitrary cross-site origins remain rejected.
        """

        if not origin:
            return None
        try:
            parsed = urlsplit(origin)
        except ValueError:
            return None
        if parsed.scheme != "http" or parsed.path or parsed.query or parsed.fragment or parsed.username:
            return None
        if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            return None
        try:
            port = parsed.port
        except ValueError:
            return None
        if port not in {self.server.server_port, 8765}:
            return None
        return origin

    def _response_headers(self) -> dict[str, str]:
        origin = self._allowed_origin(self.headers.get("Origin"))
        if origin is None:
            return {}
        return {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Headers": "Content-Type, X-CSRF-Token",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Expose-Headers": "Content-Disposition, Content-Range, Accept-Ranges, X-Request-ID",
            "Vary": "Origin",
        }

    def _write_error(self, error: RunStoreError) -> None:
        self._send_json(error.status, {"error": error.as_dict()})

    def _read_json(self, *, allow_empty: bool = False) -> dict[str, Any]:
        length_text = self.headers.get("Content-Length")
        try:
            length = int(length_text or "0")
        except ValueError as exc:
            raise RunStoreError("INVALID_JSON", "Content-Length 无效") from exc
        if length == 0 and allow_empty:
            return {}
        if length < 1 or length > MAX_JSON_BYTES:
            raise RunStoreError("INVALID_JSON", "请求体必须为 1 字节至 1 MiB")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RunStoreError("INVALID_JSON", "请求体不是有效 JSON") from exc
        if not isinstance(payload, dict):
            raise RunStoreError("INVALID_JSON", "请求体必须是 JSON 对象")
        return payload

    def _read_upload(self) -> tuple[str, str, bytes]:
        length_text = self.headers.get("Content-Length")
        try:
            length = int(length_text or "0")
        except ValueError as exc:
            raise RunStoreError("INVALID_UPLOAD", "Content-Length 无效") from exc
        if length < 1 or length > MAX_UPLOAD_BODY_BYTES:
            raise RunStoreError("INVALID_UPLOAD", "上传请求体超过限制", status=413)
        body = self.rfile.read(length)
        content_type = self.headers.get("Content-Type", "")
        if content_type.startswith("application/json"):
            try:
                payload = json.loads(body.decode("utf-8"))
                role = payload["role"]
                filename = payload["original_filename"]
                content = base64.b64decode(payload["content_base64"], validate=True)
            except (UnicodeDecodeError, json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
                raise RunStoreError("INVALID_UPLOAD", "JSON 上传必须包含有效的 role、original_filename 和 content_base64") from exc
            if not isinstance(role, str) or not isinstance(filename, str):
                raise RunStoreError("INVALID_UPLOAD", "JSON 上传字段类型无效")
            return role, filename, content
        if not content_type.startswith("multipart/form-data"):
            raise RunStoreError("INVALID_UPLOAD", "上传必须使用 multipart/form-data 或 application/json")
        envelope = (
            f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8") + body
        )
        message = BytesParser(policy=email_default_policy).parsebytes(envelope)
        role: str | None = None
        filename: str | None = None
        content: bytes | None = None
        for part in message.iter_parts():
            field = part.get_param("name", header="content-disposition")
            if field == "role":
                value = part.get_content()
                role = value.strip() if isinstance(value, str) else None
            elif field == "file":
                filename = part.get_filename() or "upload.pdf"
                content = part.get_payload(decode=True)
        if role is None or filename is None or content is None:
            raise RunStoreError("INVALID_UPLOAD", "multipart 上传必须包含 role 字段和 file 文件")
        return role, filename, content

    def _send_bytes(
        self,
        status: HTTPStatus,
        content: bytes,
        *,
        media_type: str,
        filename: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", media_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Accept-Ranges", "bytes")
        for name, value in self._response_headers().items():
            self.send_header(name, value)
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        if filename:
            safe_name = filename.replace('"', "'").replace("\r", "").replace("\n", "")
            # Header values must stay ASCII on the stdlib HTTP server. Keep a
            # readable fallback and expose the UTF-8 filename separately so a
            # non-ASCII upload filename cannot corrupt the PDF response.
            ascii_name = safe_name.encode("ascii", "ignore").decode("ascii") or "document.pdf"
            encoded_name = quote(safe_name, safe="")
            self.send_header(
                "Content-Disposition",
                f'inline; filename="{ascii_name}"; filename*=UTF-8\'\'{encoded_name}',
            )
        self.end_headers()
        self.wfile.write(content)

    def _require_local_request(self) -> None:
        """Reject requests whose network/browser context is not loopback."""

        host_header = self.headers.get("Host", "").strip()
        if not host_header:
            raise RunStoreError("INVALID_LOCAL_HOST", "状态服务必须提供本机 Host", status=403)
        if host_header.startswith("["):
            closing = host_header.find("]")
            if closing < 0:
                raise RunStoreError("INVALID_LOCAL_HOST", "状态服务只接受有效的本机 Host", status=403)
            host = host_header[1:closing].lower()
            suffix = host_header[closing + 1 :]
            if suffix and (not suffix.startswith(":") or not suffix[1:].isdigit()):
                raise RunStoreError("INVALID_LOCAL_HOST", "状态服务只接受有效的本机 Host", status=403)
        else:
            if host_header.count(":") > 1:
                raise RunStoreError("INVALID_LOCAL_HOST", "IPv6 Host 必须使用方括号", status=403)
            if ":" in host_header:
                host, port = host_header.rsplit(":", 1)
                if not port.isdigit():
                    raise RunStoreError("INVALID_LOCAL_HOST", "状态服务只接受有效的本机 Host", status=403)
            else:
                host = host_header
            host = host.lower()
        if host not in {"localhost", "127.0.0.1", "::1"}:
            raise RunStoreError("INVALID_LOCAL_HOST", "状态服务只接受本机 Host", status=403)
        origin = self.headers.get("Origin")
        if origin and self._allowed_origin(origin) is None:
            raise RunStoreError("CROSS_SITE_REQUEST_BLOCKED", "请求来源不是受信任的本机 Origin", status=403)
        fetch_site = self.headers.get("Sec-Fetch-Site")
        if fetch_site and fetch_site.strip().lower() not in {"same-origin", "same-site", "none"}:
            raise RunStoreError("CROSS_SITE_REQUEST_BLOCKED", "拒绝跨站写请求", status=403)

    def _require_csrf(self) -> None:
        self._require_local_request()
        if datetime.now(timezone.utc) >= self.server.csrf_expires_at:
            raise RunStoreError("CSRF_TOKEN_EXPIRED", "本机会话 CSRF token 已过期", status=403)
        if self.headers.get("X-CSRF-Token") != self.server.csrf_token:
            raise RunStoreError("CSRF_VALIDATION_FAILED", "缺少或无效的本机 CSRF token", status=403)

    def _unresolved_snapshot(self, mode: str, normalized: dict[str, Any]) -> dict[str, Any]:
        return {
            role: {"document_id": normalized[INPUT_ROLE_KEYS[role]], "unresolved": True}
            for role in MODE_CATALOG[mode]["required_roles"]
        }

    def _dispatch(self) -> tuple[HTTPStatus, Any]:
        path = urlsplit(self.path).path
        if path == "/api/v1/session":
            return HTTPStatus.OK, {
                "csrf_token": self.server.csrf_token,
                "session_id": self.server.session_id,
                "expires_at": self.server.csrf_expires_at.isoformat().replace("+00:00", "Z"),
            }
        if path == "/api/v1/cases":
            query = parse_qs(urlsplit(self.path).query)
            limit = int(query.get("limit", ["25"])[0])
            cases = self.server.store.list_cases(query=(query.get("query") or [None])[0], limit=limit)
            return HTTPStatus.OK, {"items": cases, "next_cursor": None}
        if path.startswith("/api/v1/cases/"):
            parts = path.split("/")
            if len(parts) == 5 and parts[4]:
                return HTTPStatus.OK, self.server.store.get_case(parts[4])
            if len(parts) == 6 and parts[4] and parts[5] == "runs":
                return HTTPStatus.OK, {
                    "items": self.server.store.list_runs(parts[4]),
                    "next_cursor": None,
                }
            if len(parts) == 6 and parts[4] and parts[5] == "documents":
                query = parse_qs(urlsplit(self.path).query)
                return HTTPStatus.OK, {
                    "items": self.server.documents.list_documents(
                        parts[4], role=(query.get("role") or [None])[0]
                    ),
                    "next_cursor": None,
                }
        if path.startswith("/api/v1/documents/"):
            parts = path.split("/")
            if len(parts) == 5 and parts[4]:
                return HTTPStatus.OK, self.server.documents.get_document(parts[4])
        if path.startswith("/api/v1/runs/"):
            parts = path.split("/")
            if len(parts) == 5 and parts[4]:
                return HTTPStatus.OK, self.server.store.get_run(parts[4])
            if len(parts) == 6 and parts[4] and parts[5] == "events":
                return HTTPStatus.OK, {"items": self.server.store.list_events(parts[4]), "next_cursor": None}
            if len(parts) == 6 and parts[4] and parts[5] == "rule-executions":
                return HTTPStatus.OK, {
                    "items": self.server.store.get_rule_executions(parts[4]),
                    "next_cursor": None,
                }
            if len(parts) == 6 and parts[4] and parts[5] == "findings":
                return HTTPStatus.OK, {
                    "items": self.server.store.list_findings(parts[4]),
                    "next_cursor": None,
                }
            if len(parts) == 6 and parts[4] and parts[5] == "reviews":
                return HTTPStatus.OK, {
                    "items": self.server.store.list_reviews(parts[4]),
                    "next_cursor": None,
                }
        if path.startswith("/api/v1/findings/"):
            parts = path.split("/")
            if len(parts) == 5 and parts[4]:
                return HTTPStatus.OK, self.server.store.get_finding(parts[4])
        return super()._dispatch()

    def _preflight(self, case_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.server.store.get_case(case_id)
        mode = payload.get("mode")
        decision = validate_mode(mode, operation="preflight") if isinstance(mode, str) else {
            "enabled": False,
            "code": "UNSUPPORTED_MODE",
            "message": "模式不受支持",
            "status": 422,
            "details": {"mode": mode, "operation": "preflight"},
            "capability": None,
        }
        if not isinstance(mode, str) or mode not in MODE_CATALOG:
            raise RunStoreError(
                str(decision["code"]),
                str(decision["message"]),
                status=int(decision["status"]),
                details=decision["details"],
            )
        inputs = payload.get("inputs", {})
        normalized = self.server.store._normalize_inputs(mode, inputs)
        capability = MODE_CATALOG[mode]
        input_snapshot = {}
        if capability["enabled"]:
            try:
                input_snapshot = self.server.documents.validate_inputs(
                    case_id=case_id,
                    mode=mode,
                    inputs=normalized,
                )
            except RunStoreError as error:
                if not self.server.allow_unresolved_inputs or error.code != "DOCUMENT_NOT_FOUND":
                    raise
                input_snapshot = self._unresolved_snapshot(mode, normalized)
        enabled = bool(capability["enabled"])
        result: dict[str, Any] = {
            "case_id": case_id,
            "mode": mode,
            "enabled": enabled,
            "can_create_run": enabled,
            "rule_bundle": {"id": RULE_BUNDLE_ID, "sha256": rule_bundle_sha256()},
            "planned_rules": [
                {
                    "rule_id": rule_id,
                    "rule_version": "1.0.0" if rule_id != "PTR-P01" else None,
                    "source": "report_baseline" if rule_id.startswith("REPORT-") else "mode_specific",
                    "disposition": "ready" if enabled else "blocked",
                    "reason_code": None if enabled else capability.get("disabled_reason_code"),
                }
                for rule_id in capability["rule_ids"]
            ],
            "coverage": {
                "planned": len(capability["rule_ids"]),
                "ready": len(capability["rule_ids"]) if enabled else 0,
                "not_applicable": 0,
                "unsupported": 0,
                "blocked": 0 if enabled else len(capability["rule_ids"]),
            },
            "inputs": normalized,
            "input_snapshot": input_snapshot,
            "blocking_issues": [] if enabled else ["PTR_NOT_VALIDATED"],
        }
        result["plan_hash"] = preflight_plan_hash(mode, normalized, input_snapshot)
        return result

    def do_OPTIONS(self) -> None:  # noqa: N802 - browser CORS preflight hook
        """Accept preflight requests from the local workbench only."""

        try:
            self._require_local_request()
        except RunStoreError as error:
            self._write_error(error)
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in self._response_headers().items():
            self.send_header(name, value)
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler hook
        try:
            self._require_local_request()
            path = urlsplit(self.path).path
            content_parts = path.split("/")
            if (
                path.startswith("/api/v1/documents/")
                and len(content_parts) == 6
                and content_parts[5] == "content"
            ):
                document = self.server.documents.get_document(content_parts[4])
                content = self.server.documents.read_content(content_parts[4])
                total_size = len(content)
                range_header = self.headers.get("Range")
                status = HTTPStatus.OK
                extra_headers: dict[str, str] = {}
                if range_header:
                    if not range_header.startswith("bytes=") or "," in range_header:
                        raise RunStoreError("INVALID_RANGE", "只支持单个 bytes Range", status=416)
                    range_value = range_header[6:]
                    try:
                        start_text, end_text = range_value.split("-", 1)
                        if start_text:
                            start = int(start_text)
                            end = int(end_text) if end_text else len(content) - 1
                        else:
                            suffix = int(end_text)
                            if suffix < 1:
                                raise ValueError
                            start = max(0, len(content) - suffix)
                            end = len(content) - 1
                        if start < 0 or end < start or start >= len(content):
                            raise ValueError
                        end = min(end, len(content) - 1)
                    except (TypeError, ValueError) as exc:
                        raise RunStoreError("INVALID_RANGE", "Range 超出 PDF 内容范围", status=416) from exc
                    content = content[start : end + 1]
                    status = HTTPStatus.PARTIAL_CONTENT
                    extra_headers["Content-Range"] = f"bytes {start}-{end}/{total_size}"
                self._send_bytes(
                    status,
                    content,
                    media_type=document["blob"]["media_type"],
                    filename=document["original_filename"],
                    extra_headers=extra_headers,
                )
                return
            status, payload = self._dispatch()
            if payload is None:
                return
            self._send_json(status, payload)
        except RunStoreError as error:
            self._write_error(error)
        except (TypeError, ValueError) as error:
            self._write_error(RunStoreError("INVALID_REQUEST", str(error)))

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler hook
        try:
            self._require_csrf()
            path = urlsplit(self.path).path
            if path.startswith("/api/v1/cases/") and path.endswith("/documents"):
                case_id = path.split("/")[4]
                role, filename, content = self._read_upload()
                document = self.server.documents.upload_pdf(
                    case_id=case_id,
                    role=role,
                    original_filename=filename,
                    content=content,
                )
                self._send_json(HTTPStatus.CREATED, document)
                return
            payload = self._read_json(allow_empty=path.endswith(":cancel"))
            if path == "/api/v1/cases":
                case = self.server.store.create_case(payload.get("name", ""), payload.get("description"))
                self._send_json(HTTPStatus.CREATED, case)
                return
            if path.startswith("/api/v1/cases/") and path.endswith("/runs:preflight"):
                case_id = path.split("/")[4]
                self._send_json(HTTPStatus.OK, self._preflight(case_id, payload))
                return
            if path.startswith("/api/v1/cases/") and path.endswith("/runs"):
                case_id = path.split("/")[4]
                mode = payload.get("mode")
                inputs = payload.get("inputs", {})
                decision = validate_mode(mode, operation="create_run") if isinstance(mode, str) else {
                    "enabled": False,
                    "code": "UNSUPPORTED_MODE",
                    "message": "模式不受支持",
                    "status": 422,
                    "details": {"mode": mode, "operation": "create_run"},
                    "capability": None,
                }
                if not decision["enabled"]:
                    raise RunStoreError(
                        str(decision["code"]),
                        str(decision["message"]),
                        status=int(decision["status"]),
                        details=decision["details"],
                    )
                normalized = self.server.store._normalize_inputs(mode, inputs)
                try:
                    input_snapshot = self.server.documents.validate_inputs(
                        case_id=case_id,
                        mode=mode,
                        inputs=normalized,
                    )
                except RunStoreError as error:
                    if not self.server.allow_unresolved_inputs or error.code != "DOCUMENT_NOT_FOUND":
                        raise
                    input_snapshot = self._unresolved_snapshot(mode, normalized)
                expected_plan_hash = preflight_plan_hash(mode, normalized, input_snapshot)
                submitted_plan_hash = payload.get("preflight_plan_hash")
                if not submitted_plan_hash:
                    raise RunStoreError("RUN_PREFLIGHT_REQUIRED", "创建 Run 前必须完成 preflight")
                if submitted_plan_hash != expected_plan_hash:
                    raise RunStoreError(
                        "RUN_PLAN_CHANGED",
                        "Run 计划已变化，请重新执行 preflight",
                        status=409,
                        details={"expected_plan_hash": expected_plan_hash},
                    )
                run = self.server.store.create_run(
                    case_id=case_id,
                    mode=mode,
                    inputs=normalized,
                    input_snapshot=input_snapshot,
                    preflight_plan_hash=submitted_plan_hash,
                    rule_bundle_id=RULE_BUNDLE_ID,
                )
                if not any(item.get("unresolved") for item in input_snapshot.values()):
                    try:
                        self.server.documents.bind_run_inputs(
                            run_id=run["id"],
                            case_id=case_id,
                            mode=mode,
                            inputs=normalized,
                        )
                        self.server.submit_run(run["id"])
                    except (RunStoreError, OSError) as error:
                        # A failed bind/submit must not leave an orphaned
                        # queued Run that can be mistaken for a recoverable
                        # job on the next process start.
                        current = self.server.store.get_run(run["id"])
                        if current["lifecycle_status"] == "queued":
                            self.server.store.transition_run(
                                run["id"],
                                "failed",
                                reason_code="RUN_DISPATCH_FAILED",
                                reason_message=str(error)[:1000],
                            )
                        raise
                run = self.server.store.get_run(run["id"])
                self._send_json(HTTPStatus.ACCEPTED, run)
                return
            if path.startswith("/api/v1/findings/") and path.endswith("/reviews"):
                finding_id = path.split("/")[4]
                readonly_fields = {
                    "actor_id",
                    "machine_status",
                    "resolved_status",
                    "review_resolution",
                    "overall_status",
                    "comparison",
                    "normalized_values",
                }
                submitted_readonly = sorted(readonly_fields.intersection(payload))
                if submitted_readonly:
                    raise RunStoreError(
                        "READ_ONLY_FIELD_SUBMITTED",
                        "客户端不能提交由服务端派生的复核字段",
                        details={"fields": submitted_readonly},
                    )
                action = payload.get("action_type", payload.get("action"))
                if not isinstance(action, str):
                    raise RunStoreError("INVALID_REVIEW_ACTION", "action/action_type 必须是字符串")
                base_revision = payload.get("base_review_revision", payload.get("base_run_review_revision"))
                observation = payload.get("observation")
                if observation is None:
                    control_fields = {
                        "action",
                        "action_type",
                        "actor_id",
                        "base_review_revision",
                        "base_run_review_revision",
                        "finding_version",
                        "note",
                    }
                    observation = {
                        key: value
                        for key, value in payload.items()
                        if key not in control_fields and key not in readonly_fields
                    }
                    if not observation:
                        observation = None
                response = self.server.store.append_review_action(
                    finding_id,
                    action,
                    actor_id=self.server.session_id,
                    base_review_revision=base_revision,
                    observation=observation,
                    note=payload.get("note"),
                )
                self._send_json(HTTPStatus.CREATED, response)
                return
            if path.startswith("/api/v1/runs/") and path.endswith(":cancel"):
                run_id = path.split("/")[4][:-7]
                current = self.server.store.get_run(run_id)
                if current["lifecycle_status"] == "queued":
                    target = "cancelled"
                elif current["lifecycle_status"] == "running":
                    target = "cancel_requested"
                elif current["lifecycle_status"] in {"cancel_requested", "cancelled"}:
                    self._send_json(HTTPStatus.OK, current)
                    return
                else:
                    raise RunStoreError(
                        "RUN_NOT_CANCELLABLE",
                        "Run 已进入终态，不能取消",
                        status=409,
                        details={"run_id": run_id, "lifecycle_status": current["lifecycle_status"]},
                    )
                run = self.server.store.transition_run(
                    run_id,
                    target,
                    reason_code="USER_CANCELLED" if target == "cancelled" else "USER_CANCEL_REQUESTED",
                    reason_message=payload.get("reason"),
                )
                self._send_json(HTTPStatus.OK if target == "cancelled" else HTTPStatus.ACCEPTED, run)
                return
            raise RunStoreError("NOT_FOUND", "资源不存在", status=404)
        except RunStoreError as error:
            self._write_error(error)
        except (TypeError, ValueError) as error:
            self._write_error(RunStoreError("INVALID_REQUEST", str(error)))


def create_server(
    *,
    host: str = "127.0.0.1",
    port: int = 8767,
    database: str = ":memory:",
    storage_root: str = "output/blob-store",
    output_root: str = "output/runs",
    allow_unresolved_inputs: bool | None = None,
) -> RunStateHTTPServer:
    if host not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("run state server only binds to the local host")
    if allow_unresolved_inputs and str(database) != ":memory:":
        raise ValueError("allow_unresolved_inputs 只允许内存测试服务使用")
    store = RunStore(database)
    store.recover_interrupted_runs()
    if allow_unresolved_inputs is None:
        # Production and test servers use the same strict input contract. A
        # legacy unresolved-input fixture must opt in explicitly.
        allow_unresolved_inputs = False
    server = RunStateHTTPServer(
        (host, port),
        store,
        storage_root=storage_root,
        output_root=output_root,
        allow_unresolved_inputs=allow_unresolved_inputs,
    )
    # A queued Run is durable work, not an in-memory promise.  Resubmit it
    # after the server and single-worker executor are ready; unresolved test
    # fixtures remain queued for the caller to bind later.
    for run in store.list_queued_runs():
        if any(item.get("unresolved") for item in (run.get("input_snapshot") or {}).values()):
            continue
        try:
            server.submit_run(run["id"])
        except (RunStoreError, OSError) as error:
            current = store.get_run(run["id"])
            if current["lifecycle_status"] == "queued":
                store.transition_run(
                    run["id"],
                    "failed",
                    reason_code="RUN_DISPATCH_FAILED",
                    reason_message=str(error)[:1000],
                )
    return server


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve the local persistent Case/Run state API.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8767, type=int)
    parser.add_argument("--database", default="output/run-state.sqlite3")
    parser.add_argument("--storage-root", default="output/blob-store")
    parser.add_argument("--output-root", default="output/runs")
    parser.add_argument("--allow-unresolved-inputs", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    server = create_server(
        host=args.host,
        port=args.port,
        database=args.database,
        storage_root=args.storage_root,
        output_root=args.output_root,
        allow_unresolved_inputs=args.allow_unresolved_inputs,
    )
    print(f"run state API listening on http://{args.host}:{server.server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
