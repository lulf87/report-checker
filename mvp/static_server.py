"""Loopback-only static server with an explicit path allowlist.

The development workbenches contain references to real PDFs and generated
results.  ``python -m http.server`` is deliberately not used for those pages:
it is too easy to point it at the project root and publish ``.git``, ``.aws``,
SQLite files, or unrelated material.  This module exposes only files selected
for the requested workbench mode and never provides directory listings.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterable, Mapping
from urllib.parse import unquote, urlsplit


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROTOTYPE_ROOT = PROJECT_ROOT / "docs" / "prototypes"
MATERIAL_ROOT = PROJECT_ROOT / "素材"

_MANIFEST_RE = re.compile(r'resultPath:\s*"\.\./\.\./(output/[^"\\]+/result\.json)"')
_IMAGE_SUFFIXES = {".gif", ".jpeg", ".jpg", ".png", ".webp"}
_BLOCKED_SUFFIXES = {".db", ".sqlite", ".sqlite3"}


def _resolved(path: Path) -> Path:
    return path.resolve()


def _under(path: Path, root: Path) -> bool:
    try:
        _resolved(path).relative_to(_resolved(root))
    except ValueError:
        return False
    return True


def _safe_relative(value: str) -> str | None:
    """Return a normalised URL-relative path, rejecting traversal."""
    value = value.replace("\\", "/").lstrip("/")
    parts = value.split("/")
    if not value or any(part in {"", ".", ".."} for part in parts):
        return None
    if any(part.startswith(".") for part in parts):
        return None
    return "/".join(parts)


def _add(mapping: dict[str, Path], url_path: str, path: Path, *, root: Path) -> None:
    if not path.is_file() or not _under(path, root):
        return
    rel = _safe_relative(url_path.lstrip("/"))
    if rel is None or path.suffix.lower() in _BLOCKED_SUFFIXES:
        return
    mapping["/" + rel] = _resolved(path)


def _json_paths(result: object) -> Iterable[str]:
    """Yield material paths declared by a result's files/source_files fields."""
    if not isinstance(result, dict):
        return
    for key in ("files", "source_files"):
        value = result.get(key)
        if isinstance(value, list):
            values = value
        elif isinstance(value, dict):
            values = value.values()
        else:
            values = ()
        for item in values:
            if isinstance(item, dict) and isinstance(item.get("path"), str):
                yield item["path"]


def _json_images(result: object) -> Iterable[str]:
    if not isinstance(result, dict):
        return
    findings = result.get("findings")
    if not isinstance(findings, list):
        return
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        evidence = finding.get("evidence")
        if isinstance(evidence, list):
            for item in evidence:
                if isinstance(item, dict) and isinstance(item.get("image"), str):
                    yield item["image"]


def build_allowlist(project_root: Path = PROJECT_ROOT, mode: str = "upload") -> dict[str, Path]:
    """Build the exact URL-to-file mapping for ``upload`` or ``real`` mode."""
    project_root = _resolved(Path(project_root))
    prototype_root = project_root / "docs" / "prototypes"
    material_root = project_root / "素材"
    mapping: dict[str, Path] = {}

    for path in prototype_root.rglob("*"):
        if not path.is_file() or not _under(path, prototype_root):
            continue
        rel = path.relative_to(prototype_root).as_posix()
        safe = _safe_relative(rel)
        if safe is not None and path.suffix.lower() not in _BLOCKED_SUFFIXES:
            resolved = _resolved(path)
            # The short paths are the canonical URLs used by the workbench.
            # Keep the repository's historical /docs/prototypes/... URLs as
            # aliases too: users often have those links bookmarked, and the
            # HTML assets use relative vendor URLs that must resolve under the
            # same prefix.
            mapping["/" + safe] = resolved
            mapping["/docs/prototypes/" + safe] = resolved

    if mode == "upload":
        return mapping
    if mode != "real":
        raise ValueError(f"unsupported static server mode: {mode}")

    workbench = prototype_root / "workbench-real.html"
    manifest_text = workbench.read_text(encoding="utf-8")
    result_paths = sorted(set(_MANIFEST_RE.findall(manifest_text)))
    for result_rel in result_paths:
        result_path = project_root / result_rel
        if not result_path.is_file() or not _under(result_path, project_root / "output"):
            continue
        _add(mapping, "/" + result_rel, result_path, root=project_root / "output")
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        run_root = result_path.parent
        run_rel = run_root.relative_to(project_root).as_posix()
        for image in _json_images(result):
            image_rel = _safe_relative(image.replace("\\", "/"))
            if image_rel is None or Path(image_rel).suffix.lower() not in _IMAGE_SUFFIXES:
                continue
            _add(mapping, f"/{run_rel}/{image_rel}", run_root / image_rel, root=run_root)
        for raw_path in _json_paths(result):
            material_path = Path(raw_path).expanduser()
            if not material_path.is_absolute():
                continue
            if not _under(material_path, material_root) or material_path.suffix.lower() != ".pdf":
                continue
            material_rel = material_path.resolve().relative_to(material_root).as_posix()
            _add(mapping, f"/素材/{material_rel}", material_path, root=material_root)
    return mapping


def is_loopback_host(host: str | None) -> bool:
    if not host:
        return False
    value = host.strip()
    if value.startswith("["):
        end = value.find("]")
        if end < 0:
            return False
        address, remainder = value[1:end], value[end + 1 :]
        if remainder:
            if not remainder.startswith(":") or not remainder[1:].isdigit():
                return False
            if not 0 < int(remainder[1:]) <= 65535:
                return False
    else:
        if value.count(":") > 1:
            return False
        address, port = (value.rsplit(":", 1) if ":" in value else (value, ""))
        if ":" in value and (not port.isdigit() or not 0 < int(port) <= 65535):
            return False
    return address.lower() in {"localhost", "127.0.0.1", "::1"}


def is_local_request(headers: Mapping[str, str], port: int) -> bool:
    if not is_loopback_host(headers.get("Host")):
        return False
    fetch_site = headers.get("Sec-Fetch-Site")
    if fetch_site and fetch_site.strip().lower() not in {"same-origin", "same-site", "none"}:
        return False
    origin = headers.get("Origin")
    if not origin:
        return True
    try:
        parsed = urlsplit(origin)
        return (
            parsed.scheme == "http"
            and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
            and parsed.port == port
            and not (parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment)
        )
    except ValueError:
        return False


class SafeStaticHandler(BaseHTTPRequestHandler):
    server: "SafeStaticServer"
    protocol_version = "HTTP/1.1"

    def _send_error(self, status: HTTPStatus) -> None:
        body = f"{status.value} {status.phrase}\n".encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _resolve_request(self) -> Path | None:
        if not is_local_request(self.headers, self.server.server_port):
            self._send_error(HTTPStatus.FORBIDDEN)
            return None
        parsed = urlsplit(self.path)
        decoded = unquote(parsed.path)
        if not decoded.startswith("/") or "\\" in decoded:
            self._send_error(HTTPStatus.NOT_FOUND)
            return None
        key = "/" + decoded.lstrip("/")
        if key == "/":
            key = "/workbench-upload.html" if self.server.mode == "upload" else "/workbench-real.html"
        path = self.server.allowlist.get(key)
        if path is None:
            self._send_error(HTTPStatus.NOT_FOUND)
            return None
        return path

    def _serve(self) -> None:
        path = self._resolve_request()
        if path is None:
            return
        try:
            size = path.stat().st_size
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(size))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                with path.open("rb") as stream:
                    while chunk := stream.read(1024 * 1024):
                        self.wfile.write(chunk)
        except OSError:
            self._send_error(HTTPStatus.NOT_FOUND)

    def do_GET(self) -> None:  # noqa: N802
        self._serve()

    def do_HEAD(self) -> None:  # noqa: N802
        self._serve()

    def log_message(self, format: str, *args: object) -> None:
        return


class SafeStaticServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int], mode: str, project_root: Path = PROJECT_ROOT):
        if address[0] not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("static server must bind to loopback")
        self.mode = mode
        self.allowlist = build_allowlist(project_root, mode)
        super().__init__(address, SafeStaticHandler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve an allowlisted workbench on loopback")
    parser.add_argument("--mode", choices=("upload", "real"), default="upload")
    parser.add_argument("--bind", "--host", dest="host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("--bind must be 127.0.0.1, localhost, or ::1")
    server = SafeStaticServer((args.host, args.port), args.mode)
    print(f"Serving {args.mode} workbench on http://{args.host}:{args.port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
