"""Local OCR observations for scanned PTR PDFs.

This module is intentionally an observation adapter.  OCR text is never a
business conclusion: callers must keep OCR-derived clauses in the manual
review queue until a human or a separately validated rule confirms them.
The adapter reuses the project's local OCR backends and stores PDF-point
coordinates so a later review can jump back to the source page.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover - compatibility with older environments
    import fitz


PTR_OCR_SCHEMA_VERSION = "ptr-ocr-0.1"
MAX_RENDER_SIDE = 3200
MAX_RENDER_SCALE = 4.0


def available_ptr_ocr_backends() -> list[str]:
    """Return locally installed OCR engines in the project order."""

    try:
        from mvp.photo_ocr import available_backends

        return list(available_backends())
    except Exception:  # pragma: no cover - dependency probing is best effort
        return []


def _run_backend(backend: str, png: bytes, width: int, height: int) -> list[dict[str, Any]]:
    from mvp.photo_ocr import _ENGINES

    engine = _ENGINES.get(backend)
    if engine is None:
        raise ValueError(f"未知 OCR 后端：{backend}")
    return [dict(item) for item in engine(png, width, height)]


def _render_page(page: fitz.Page) -> tuple[bytes, int, int, float, float]:
    scale = min(MAX_RENDER_SCALE, MAX_RENDER_SIDE / max(page.rect.width, page.rect.height, 1.0))
    pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), annots=False, alpha=False)
    scale_x = float(page.rect.width) / max(float(pixmap.width), 1.0)
    scale_y = float(page.rect.height) / max(float(pixmap.height), 1.0)
    return pixmap.tobytes("png"), pixmap.width, pixmap.height, scale_x, scale_y


def _pdf_bbox(
    value: Sequence[Any] | None,
    *,
    scale_x: float,
    scale_y: float,
    page: fitz.Page,
) -> list[float]:
    if value is None or len(value) != 4:
        return [0.0, 0.0, round(float(page.rect.width), 3), round(float(page.rect.height), 3)]
    x0, y0, x1, y1 = (float(item) for item in value)
    rect = fitz.Rect(x0 * scale_x, y0 * scale_y, x1 * scale_x, y1 * scale_y) & page.rect
    if rect.is_empty:
        rect = page.rect
    return [round(float(rect.x0), 3), round(float(rect.y0), 3), round(float(rect.x1), 3), round(float(rect.y1), 3)]


def _row_lines(items: Iterable[Mapping[str, Any]], *, tolerance: float = 0.5) -> list[dict[str, Any]]:
    """Group OCR boxes into reading-order lines without changing their text."""

    rows: list[list[dict[str, Any]]] = []
    boxed = [dict(item) for item in items if item.get("bbox") and item.get("text")]
    unboxed = [dict(item) for item in items if item.get("text") and not item.get("bbox")]
    heights = [max(float(item["bbox"][3]) - float(item["bbox"][1]), 1.0) for item in boxed]
    center_tolerance = max(4.0, sorted(heights)[len(heights) // 2] * tolerance) if heights else 4.0
    for item in sorted(boxed, key=lambda value: (float(value["bbox"][1]), float(value["bbox"][0]))):
        center = (float(item["bbox"][1]) + float(item["bbox"][3])) / 2
        if rows:
            previous = rows[-1]
            previous_center = sum(
                (float(other["bbox"][1]) + float(other["bbox"][3])) / 2 for other in previous
            ) / len(previous)
            if abs(center - previous_center) <= center_tolerance:
                previous.append(item)
                continue
        rows.append([item])
    result: list[dict[str, Any]] = []
    for row in rows:
        ordered = sorted(row, key=lambda value: float(value["bbox"][0]))
        texts = [str(item.get("text") or "").strip() for item in ordered if str(item.get("text") or "").strip()]
        if not texts:
            continue
        result.append(
            {
                "text": "".join(texts),
                "bbox": [
                    round(min(float(item["bbox"][0]) for item in ordered), 3),
                    round(min(float(item["bbox"][1]) for item in ordered), 3),
                    round(max(float(item["bbox"][2]) for item in ordered), 3),
                    round(max(float(item["bbox"][3]) for item in ordered), 3),
                ],
                "confidence": round(
                    sum(float(item.get("confidence", 0.0) or 0.0) for item in ordered) / len(ordered), 4
                ),
            }
        )
    for item in unboxed:
        result.append(
            {
                "text": str(item.get("text") or "").strip(),
                "bbox": None,
                "confidence": float(item.get("confidence", 0.0) or 0.0),
            }
        )
    return result


def iter_ptr_ocr_lines(
    path: str | Path,
    *,
    backends: Sequence[str] | None = None,
) -> tuple[list[tuple[int, list[float], str, str, float]], dict[str, Any]]:
    """Return OCR lines as ``(page, bbox, text, extraction_method)`` tuples.

    When no backend is available the function returns an empty list and a
    stable diagnostic code.  Backend failures are recorded per page and do
    not cause the caller to invent text.
    """

    source = Path(path).expanduser().resolve()
    requested = list(backends) if backends is not None else available_ptr_ocr_backends()
    requested = [str(name) for name in requested if str(name)]
    diagnostics: dict[str, Any] = {
        "schema_version": PTR_OCR_SCHEMA_VERSION,
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "requested_backends": requested,
        "used_backends": [],
        "page_count": 0,
        "pages_with_text": [],
        "errors": [],
        "status": "unavailable" if not requested else "no_text",
        "reason_code": "PTR_OCR_UNAVAILABLE" if not requested else "PTR_OCR_NO_TEXT",
    }
    lines: list[tuple[int, list[float], str, str, float]] = []
    if not requested:
        return lines, diagnostics
    with fitz.open(source) as document:
        diagnostics["page_count"] = document.page_count
        for page_number, page in enumerate(document, start=1):
            try:
                png, width, height, scale_x, scale_y = _render_page(page)
            except Exception as exc:
                diagnostics["errors"].append(
                    {"page": page_number, "backend": None, "reason": f"render:{type(exc).__name__}: {exc}"}
                )
                continue
            page_had_text = False
            for backend in requested:
                try:
                    items = _run_backend(backend, png, width, height)
                except Exception as exc:
                    diagnostics["errors"].append(
                        {"page": page_number, "backend": backend, "reason": f"{type(exc).__name__}: {exc}"}
                    )
                    continue
                if backend not in diagnostics["used_backends"]:
                    diagnostics["used_backends"].append(backend)
                rows = _row_lines(items)
                for row in rows:
                    text = str(row.get("text") or "").strip()
                    if not text:
                        continue
                    bbox = _pdf_bbox(row.get("bbox"), scale_x=scale_x, scale_y=scale_y, page=page)
                    lines.append((page_number, bbox, text, f"ocr:{backend}", round(float(row.get("confidence", 0.0) or 0.0), 4)))
                    page_had_text = True
            if page_had_text:
                diagnostics["pages_with_text"].append(page_number)
    if lines:
        diagnostics["status"] = "observed"
        diagnostics["reason_code"] = None
    elif diagnostics["errors"]:
        diagnostics["status"] = "error"
        diagnostics["reason_code"] = "PTR_OCR_FAILED"
    return lines, diagnostics


__all__ = ["PTR_OCR_SCHEMA_VERSION", "available_ptr_ocr_backends", "iter_ptr_ocr_lines"]
