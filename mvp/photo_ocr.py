"""Report 照片页图片的本机 OCR。

* ``apple_vision``：macOS 自带的 Vision 文字识别（通过 ``ocrmac``），只在 Mac 上可用。
* ``rapidocr``：开源的 RapidOCR（``rapidocr_onnxruntime``，模型随包安装），跨平台、离线可用。

两个引擎都是可选的：装了哪个就用哪个，两个都在时各识别一遍，作为两路独立读数。
识别只在本机进行，不联网、不调用大模型。环境变量 ``REPORT_PHOTO_OCR=off`` 可整体关闭。

识别结果按“页码 + 图片序号”缓存在内存中，运行结束后可写成 JSON 制品，供复核和复现。
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import platform
import re
import tempfile
import unicodedata
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover
    import fitz


BACKEND_ORDER = ("apple_vision", "rapidocr")
MAX_RENDER_SIDE = 3000
MAX_RENDER_SCALE = 4.0

# 标签字段的关键字；同一字段按从长到短匹配，避免“批号”抢先匹配“批号/序列号”。
FIELD_KEYS: dict[str, tuple[str, ...]] = {
    "部件名称": ("产品名称", "器械名称", "产品描述", "注册名称", "部件名称", "样品名称", "名称"),
    "规格型号": ("规格型号", "型号规格", "产品型号", "组件号", "物料编码", "型号", "REF"),
    "序列号/批号": (
        "批号/序列号", "序列号/批号", "批次号/序列号", "序列号/批次号", "生产批号",
        "序列号", "批次号", "批号", "S/N", "SN", "LOT",
    ),
    "生产日期": ("生产日期", "制造日期", "MFG", "MFD"),
    "失效日期": ("失效日期", "有效期至", "有效日期", "EXP"),
    "生产单位": (
        "注册人/生产企业名称", "注册人名称/生产企业名称/售后服务单位", "注册人名称/生产企业名称",
        "生产企业名称", "注册人名称", "制造商名称", "制造商", "生产企业", "生产单位",
    ),
}
_ASCII_KEYS = {"REF", "S/N", "SN", "LOT", "MFG", "MFD", "EXP"}
_ALL_KEYS = sorted({key for keys in FIELD_KEYS.values() for key in keys}, key=len, reverse=True)


def normalize_text(value: Any) -> str:
    """Width-fold (NFKC), drop layout whitespace and trademark marks for value comparison."""

    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    text = re.sub(r"[™®©]", "", text)
    return re.sub(r"\s+", "", text)


def photo_ocr_enabled() -> bool:
    return os.environ.get("REPORT_PHOTO_OCR", "on").strip().lower() not in {"0", "off", "false", "no"}


def available_backends() -> list[str]:
    backends: list[str] = []
    if platform.system() == "Darwin" and importlib.util.find_spec("ocrmac") is not None:
        backends.append("apple_vision")
    if importlib.util.find_spec("rapidocr_onnxruntime") is not None:
        backends.append("rapidocr")
    return backends


# ---------------------------------------------------------------------------
# OCR engines
# ---------------------------------------------------------------------------

_RAPID_ENGINE = None


def _rapid_engine():
    global _RAPID_ENGINE
    if _RAPID_ENGINE is None:
        from rapidocr_onnxruntime import RapidOCR

        _RAPID_ENGINE = RapidOCR()
    return _RAPID_ENGINE


def _ocr_rapid(png: bytes, width: int, height: int) -> list[dict[str, Any]]:
    result, _elapsed = _rapid_engine()(png)
    items: list[dict[str, Any]] = []
    for box, text, score in result or []:
        xs = [float(point[0]) for point in box]
        ys = [float(point[1]) for point in box]
        items.append({
            "text": str(text),
            "confidence": round(float(score), 4),
            "bbox": [min(xs), min(ys), max(xs), max(ys)],
        })
    return items


def _ocr_vision(png: bytes, width: int, height: int) -> list[dict[str, Any]]:
    from ocrmac.ocrmac import text_from_image

    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "photo.png"
        path.write_bytes(png)
        recognized = text_from_image(
            str(path),
            recognition_level="accurate",
            language_preference=["zh-Hans", "en-US"],
            confidence_threshold=0.0,
        )
    items: list[dict[str, Any]] = []
    for entry in recognized:
        text, confidence = entry[0], entry[1]
        bbox = None
        if len(entry) > 2 and entry[2] is not None:
            x, y, w, h = (float(value) for value in entry[2])
            # Vision 坐标为归一化、原点在左下角。
            bbox = [x * width, (1 - y - h) * height, (x + w) * width, (1 - y) * height]
        items.append({"text": str(text), "confidence": round(float(confidence), 4), "bbox": bbox})
    return items


_ENGINES = {"apple_vision": _ocr_vision, "rapidocr": _ocr_rapid}


def _reading_order(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Sort OCR boxes top-to-bottom, left-to-right, grouping boxes on the same visual row."""

    boxed = [dict(item) for item in items if item.get("bbox")]
    unboxed = [dict(item) for item in items if not item.get("bbox")]
    if not boxed:
        return unboxed
    heights = sorted(item["bbox"][3] - item["bbox"][1] for item in boxed)
    tolerance = max(4.0, heights[len(heights) // 2] * 0.5)
    boxed.sort(key=lambda item: (item["bbox"][1] + item["bbox"][3]) / 2)
    rows: list[list[dict[str, Any]]] = []
    for item in boxed:
        center = (item["bbox"][1] + item["bbox"][3]) / 2
        if rows:
            row_center = sum((other["bbox"][1] + other["bbox"][3]) / 2 for other in rows[-1]) / len(rows[-1])
            if abs(center - row_center) <= tolerance:
                rows[-1].append(item)
                continue
        rows.append([item])
    ordered: list[dict[str, Any]] = []
    for row_index, row in enumerate(rows):
        for item in sorted(row, key=lambda value: value["bbox"][0]):
            item["row"] = row_index
            ordered.append(item)
    return ordered + unboxed


def _column_order(items: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    boxed = [item for item in items if item.get("bbox")]
    if not boxed:
        return list(items)
    widths = sorted(item["bbox"][2] - item["bbox"][0] for item in boxed)
    tolerance = max(8.0, (max(item["bbox"][2] for item in boxed) - min(item["bbox"][0] for item in boxed)) * 0.02)
    columns: list[list[Mapping[str, Any]]] = []
    for item in sorted(boxed, key=lambda value: value["bbox"][0]):
        if columns and abs(item["bbox"][0] - columns[-1][0]["bbox"][0]) <= tolerance:
            columns[-1].append(item)
        else:
            columns.append([item])
    ordered: list[Mapping[str, Any]] = []
    for column in columns:
        ordered.extend(sorted(column, key=lambda value: value["bbox"][1]))
    return ordered


# ---------------------------------------------------------------------------
# 字段抽取
# ---------------------------------------------------------------------------

def _match_key(text: str) -> tuple[str, str] | None:
    """Return ``(key, remainder)`` when ``text`` starts with a known label key."""

    normalized = normalize_text(text)
    for key in _ALL_KEYS:
        if not normalized.upper().startswith(key.upper() if key in _ASCII_KEYS else key):
            continue
        rest = normalized[len(key):]
        if key in _ASCII_KEYS:
            # “SN:123”“LOT 76220002”“LOT76220002” 均可；“SNAPSHOT” 这类单词不算。
            if rest and not re.match(r"[:：]|[0-9]", rest):
                continue
        elif rest and not rest.startswith((":", "：")):
            continue
        return key, rest.lstrip(":：")
    return None


def keyed_values(items: Sequence[Mapping[str, Any]]) -> dict[str, list[str]]:
    """Extract ``key：value`` pairs, including keys whose value is the next box."""

    values: dict[str, list[str]] = {field: [] for field in FIELD_KEYS}
    key_to_field = {key: field for field, keys in FIELD_KEYS.items() for key in keys}
    ordered = list(items)
    for index, item in enumerate(ordered):
        matched = _match_key(item.get("text", ""))
        if matched is None:
            continue
        key, rest = matched
        value = rest
        if not value:
            following = ordered[index + 1] if index + 1 < len(ordered) else None
            if following is not None and _match_key(following.get("text", "")) is None:
                value = normalize_text(following.get("text"))
        if value:
            values[key_to_field[key]].append(value)
    return values


def build_record(
    *,
    pdf_page: int,
    image_index: int | None,
    channels: Mapping[str, Sequence[Mapping[str, Any]]],
    title: str | None = None,
    kind: str | None = None,
    image_sha256: str | None = None,
    path: str | None = None,
) -> dict[str, Any]:
    """Assemble the record consumed by the photo rules from per-engine OCR items."""

    channel_payload: dict[str, dict[str, Any]] = {}
    for name, items in channels.items():
        ordered = _reading_order(items)
        channel_payload[name] = {
            "items": ordered,
            "text": normalize_text("".join(str(item.get("text", "")) for item in ordered)),
            # 多栏标签（如右上角的地址换行）按“同一左边界的列”再拼一遍，避免被左栏文字打断。
            "text_columns": normalize_text("".join(str(item.get("text", "")) for item in _column_order(ordered))),
            "keyed": keyed_values(ordered),
        }
    return {
        "pdf_page": pdf_page,
        "image_index": image_index,
        "title": title,
        "kind": kind,
        "image_sha256": image_sha256,
        "path": path,
        "backend": "+".join(channel_payload),
        "channels": channel_payload,
    }


class PhotoOcrProvider:
    """Lazy OCR of photo-page images; each image is recognised at most once per engine."""

    def __init__(self, document: fitz.Document, backends: Sequence[str] | None = None) -> None:
        self.document = document
        self.backends = [name for name in (backends if backends is not None else available_backends()) if name in _ENGINES]
        self._cache: dict[tuple[int, int], dict[str, Any] | None] = {}
        self.errors: list[str] = []

    @property
    def available(self) -> bool:
        return bool(self.backends)

    def _render(self, entry: Mapping[str, Any]) -> tuple[bytes, int, int] | None:
        page_number = entry.get("pdf_page")
        rect = entry.get("image_rect")
        if not isinstance(page_number, int) or not rect:
            return None
        page = self.document[page_number - 1]
        clip = fitz.Rect(rect) & page.rect
        if clip.is_empty:
            return None
        scale = min(MAX_RENDER_SCALE, MAX_RENDER_SIDE / max(clip.width, clip.height, 1.0))
        pixmap = page.get_pixmap(clip=clip, matrix=fitz.Matrix(scale, scale), annots=False, alpha=False)
        return pixmap.tobytes("png"), pixmap.width, pixmap.height

    def record_for(self, entry: Mapping[str, Any]) -> dict[str, Any] | None:
        page_number, image_index = entry.get("pdf_page"), entry.get("image_index")
        if not self.backends or not isinstance(page_number, int) or not isinstance(image_index, int):
            return None
        key = (page_number, image_index)
        if key in self._cache:
            return self._cache[key]
        rendered = self._render(entry)
        if rendered is None:
            self._cache[key] = None
            return None
        png, width, height = rendered
        channels: dict[str, list[dict[str, Any]]] = {}
        for backend in self.backends:
            try:
                channels[backend] = _ENGINES[backend](png, width, height)
            except Exception as exc:  # 一个引擎失败不影响另一个
                self.errors.append(f"{backend} 识别第{page_number}页图片{image_index}失败：{type(exc).__name__}: {exc}")
        record = build_record(
            pdf_page=page_number,
            image_index=image_index,
            channels=channels,
            title=entry.get("title"),
            kind=entry.get("kind"),
            image_sha256=hashlib.sha256(png).hexdigest(),
        ) if channels else None
        self._cache[key] = record
        return record

    def records(self) -> list[dict[str, Any]]:
        return [record for _key, record in sorted(self._cache.items()) if record is not None]


def records_by_key(records: Iterable[Mapping[str, Any]]) -> dict[tuple[int, int], Mapping[str, Any]]:
    keyed: dict[tuple[int, int], Mapping[str, Any]] = {}
    for record in records:
        page, image_index = record.get("pdf_page"), record.get("image_index")
        if isinstance(page, int) and isinstance(image_index, int):
            keyed[(page, image_index)] = record
    return keyed


def engine_versions(backends: Sequence[str]) -> dict[str, str | None]:
    versions: dict[str, str | None] = {}
    for backend in backends:
        package = {"apple_vision": "ocrmac", "rapidocr": "rapidocr_onnxruntime"}[backend]
        try:
            from importlib.metadata import version

            versions[backend] = version(package)
        except Exception:
            versions[backend] = None
    return versions
