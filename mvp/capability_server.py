"""本机只读能力与规则目录服务。

这是阶段三的轻量 HTTP 入口，供工作台读取模式和规则契约。运行创建、文件
上传、复核写入等正式 API 仍由后续服务层实现；本服务不会写入文件或状态。
"""

from __future__ import annotations

import argparse
import copy
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Sequence
from urllib.parse import urlsplit
from uuid import uuid4

from mvp.capabilities import capabilities_payload, rules_payload


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class CapabilityRequestHandler(BaseHTTPRequestHandler):
    server_version = "ReportCheckerCapability/1.0"

    def _response_headers(self) -> dict[str, str]:
        """Headers added by an adapter without changing the JSON contract."""

        return {}

    def _send_json(self, status: HTTPStatus, payload: Any, *, allow: str | None = None) -> None:
        if status >= HTTPStatus.BAD_REQUEST and isinstance(payload, dict) and isinstance(payload.get("error"), dict):
            payload = copy.deepcopy(payload)
            payload["error"]["request_id"] = uuid4().hex
        body = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Request-ID", uuid4().hex)
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in self._response_headers().items():
            self.send_header(name, value)
        if allow is not None:
            self.send_header("Allow", allow)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _dispatch(self) -> tuple[HTTPStatus, Any]:
        path = urlsplit(self.path).path
        if path == "/healthz":
            return HTTPStatus.OK, {"status": "ok"}
        if path == "/api/v1/capabilities":
            return HTTPStatus.OK, capabilities_payload()
        if path == "/api/v1/rules":
            return HTTPStatus.OK, rules_payload()
        return HTTPStatus.NOT_FOUND, {
            "error": {
                "code": "NOT_FOUND",
                "message": "资源不存在",
                "path": path,
            }
        }

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler hook
        status, payload = self._dispatch()
        self._send_json(status, payload)

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib handler hook
        status, payload = self._dispatch()
        self._send_json(status, payload)

    def _method_not_allowed(self) -> None:
        self._send_json(
            HTTPStatus.METHOD_NOT_ALLOWED,
            {
                "error": {
                    "code": "METHOD_NOT_ALLOWED",
                    "message": "只支持 GET 和 HEAD",
                }
            },
            allow="GET, HEAD",
        )

    do_POST = _method_not_allowed
    do_PUT = _method_not_allowed
    do_PATCH = _method_not_allowed
    do_DELETE = _method_not_allowed
    do_OPTIONS = _method_not_allowed

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        # BaseHTTPRequestHandler otherwise emits an HTML 501 for an unknown
        # verb. Keep this JSON service's method contract uniform.
        if code == HTTPStatus.NOT_IMPLEMENTED:
            self._method_not_allowed()
            return
        super().send_error(code, message, explain)

    def log_message(self, format: str, *args: Any) -> None:
        # The API is normally embedded in a local workbench; avoid noisy access
        # logs while preserving BaseHTTPRequestHandler's error reporting.
        return None


def create_server(host: str = "127.0.0.1", port: int = 8766) -> ThreadingHTTPServer:
    if host not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("capability server only binds to the local host")
    return ThreadingHTTPServer((host, port), CapabilityRequestHandler)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve the local read-only capability catalog.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8766, type=int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    server = create_server(args.host, args.port)
    print(f"capability API listening on http://{args.host}:{server.server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
