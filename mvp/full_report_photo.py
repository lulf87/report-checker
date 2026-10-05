from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:
    import pymupdf as fitz
except ImportError:
    import fitz


SAMPLE_COLUMNS = ("序号", "部件名称", "规格型号", "序列号/批号", "生产日期", "失效日期", "备注")
CHECKED_SAMPLE_COLUMNS = SAMPLE_COLUMNS[1:]
SAMPLE_HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "序号": ("序号", "编号", "项目序号"),
    "部件名称": ("部件名称", "名称", "样品名称", "项目名称"),
    "规格型号": ("规格型号", "型号", "型号规格", "物料编码/型号规格", "组件号", "组件编号"),
    "序列号/批号": ("序列号/批号", "批号/序列号", "序列号", "批号", "产品编号/批号"),
    "生产日期": ("生产日期", "生产日期/有效期", "生产日期（年月日）"),
    "失效日期": ("失效日期", "有效期至", "有效日期"),
    "备注": ("备注", "说明", "其他说明"),
}
PHOTO_ORIENTATIONS = ("正面", "背面", "左面", "右面")
DATE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("YYYY-MM-DD", r"\d{4}-\d{2}-\d{2}"),
    ("YYYY.MM.DD", r"\d{4}\.\d{2}\.\d{2}"),
    ("YYYY/MM/DD", r"\d{4}/\d{2}/\d{2}"),
    ("YYYYMMDD", r"\d{8}"),
    ("YYYY年MM月DD日", r"\d{4}年\d{2}月\d{2}日"),
)
STATUS_RANK = {"pass": 0, "manual": 1, "error": 2}
PHOTO_ITEM_STATUSES = ("pass", "manual", "error", "not_applicable")


def photo_scope(row: Mapping[str, Any]) -> tuple[bool, str | None]:
    """Return whether a sample-description row requires physical-photo checks.

    A Report can list software features, certificates, upgrade packages, and
    optional items that the report explicitly says were not used. Those rows
    remain in the coverage details, but requiring a physical object or label
    photo for them creates a false mismatch.
    """

    name = compact_layout((row.get("fields") or {}).get("部件名称"))
    notes = compact_layout((row.get("fields") or {}).get("备注"))
    if "本次检测未使用" in notes or "本次检验未使用" in notes:
        return False, "sample_marked_not_used"
    if any(token in name for token in ("软件", "模块", "升级包", "证书", "功能")):
        return False, "non_physical_sample_entry"
    return True, None


def compact_layout(value: Any) -> str:
    """Remove layout whitespace only; technical punctuation remains significant."""

    if value is None:
        return ""
    return re.sub(r"\s+", "", str(value))


def display_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def rect_list(rect: fitz.Rect | Sequence[float] | None) -> list[float] | None:
    if rect is None:
        return None
    value = fitz.Rect(rect)
    return [round(value.x0, 3), round(value.y0, 3), round(value.x1, 3), round(value.y1, 3)]


def _status_from(values: Iterable[str]) -> str:
    statuses = [value for value in values if value != "not_applicable"]
    if not statuses:
        return "pass"
    return max(statuses, key=lambda value: STATUS_RANK.get(value, STATUS_RANK["manual"]))


def _cell_rect(table: fitz.table.Table, row_index: int, column_index: int) -> list[float] | None:
    try:
        return rect_list(table.rows[row_index].cells[column_index])
    except (IndexError, TypeError):
        return None


def _header_mapping(row: Sequence[Any]) -> dict[str, int] | None:
    normalized = [compact_layout(value) for value in row]
    mapping: dict[str, int] = {}
    for column, aliases in SAMPLE_HEADER_ALIASES.items():
        for index, value in enumerate(normalized):
            if value in {compact_layout(alias) for alias in aliases}:
                mapping[column] = index
                break
    # Name, model/code and serial/batch identify a sample table.  Date and
    # notes are optional because several accepted templates omit them.
    if not {"序号", "部件名称", "规格型号", "序列号/批号"}.issubset(mapping):
        return None
    return mapping


def _looks_like_sample_continuation(
    rows: Sequence[Sequence[Any]],
    column_count: int,
    expected_column_count: int | None = None,
) -> bool:
    if column_count < 4 or not rows or (expected_column_count is not None and column_count != expected_column_count):
        return False
    return all(
        len(row) >= 4 and re.fullmatch(r"\d+", compact_layout(row[0]))
        for row in rows
    )


def _sample_row(
    page: fitz.Page,
    table: fitz.table.Table,
    row_index: int,
    row: Sequence[Any],
    mapping: Mapping[str, int],
) -> dict[str, Any] | None:
    sequence_text = compact_layout(row[mapping["序号"]])
    if not re.fullmatch(r"\d+", sequence_text):
        return None
    fields: dict[str, str] = {}
    raw_fields: dict[str, str] = {}
    locations: dict[str, dict[str, Any]] = {}
    for column in SAMPLE_COLUMNS:
        column_index = mapping.get(column)
        raw = "" if column_index is None or column_index >= len(row) or row[column_index] is None else str(row[column_index])
        fields[column] = display_text(raw)
        raw_fields[column] = raw
        locations[column] = {
            "document_role": "report",
            "pdf_page": page.number + 1,
            "rect": _cell_rect(table, row_index, column_index),
            "source": "sample_description_cell",
        }
    return {
        "sequence": int(sequence_text),
        "fields": fields,
        "raw_fields": raw_fields,
        "pdf_page": page.number + 1,
        "locations": locations,
    }


def parse_sample_description(document: fitz.Document) -> tuple[list[dict[str, Any]], list[str]]:
    """Discover and extract the sample-description table and its headerless continuation."""

    rows_found: list[dict[str, Any]] = []
    diagnostics: list[str] = []
    mapping: dict[str, int] | None = None
    started = False
    last_sample_page: int | None = None

    for page in document:
        page_added = False
        for table in page.find_tables().tables:
            extracted = table.extract()
            if not extracted:
                continue
            candidate_mapping = _header_mapping(extracted[0])
            if candidate_mapping is not None:
                if started:
                    diagnostics.append(
                        f"PDF第{page.number + 1}页发现第二个样品描述表头，未自动合并"
                    )
                    continue
                started = True
                mapping = candidate_mapping
                data_rows = list(enumerate(extracted[1:], start=1))
            elif (
                started
                and mapping is not None
                and last_sample_page is not None
                and page.number <= last_sample_page + 1
                and _looks_like_sample_continuation(
                    extracted,
                    table.col_count,
                    expected_column_count=(max(mapping.values()) + 1 if mapping else None),
                )
            ):
                data_rows = list(enumerate(extracted))
            else:
                continue

            for row_index, row in data_rows:
                parsed = _sample_row(page, table, row_index, row, mapping)
                if parsed is not None:
                    rows_found.append(parsed)
                    page_added = True
            if page_added:
                last_sample_page = page.number

        if started and last_sample_page is not None and page.number > last_sample_page + 1:
            break

    if not rows_found:
        diagnostics.append("未找到可可靠解析的样品描述表")
        return [], diagnostics

    sequences = [row["sequence"] for row in rows_found]
    if len(sequences) != len(set(sequences)):
        diagnostics.append("样品描述表存在重复序号")
    if sequences != sorted(sequences):
        diagnostics.append("样品描述表序号未按升序出现")
    return rows_found, diagnostics


def parse_report_identity(document: fitz.Document) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Read fields from the page explicitly titled 检验报告首页."""

    wanted = {"委托方", "委托方地址"}
    diagnostics: list[str] = []
    for page in document:
        if "检验报告首页" not in compact_layout(page.get_text("text")):
            continue
        for table in page.find_tables().tables:
            extracted = table.extract()
            for row_index, row in enumerate(extracted):
                for column_index, value in enumerate(row):
                    key = compact_layout(value)
                    if key not in wanted or column_index + 1 >= len(row):
                        continue
                    observed = display_text(row[column_index + 1])
                    fields = {
                        key: {
                            "value": observed,
                            "location": {
                                "document_role": "report",
                                "pdf_page": page.number + 1,
                                "rect": _cell_rect(table, row_index, column_index + 1),
                                "source": "report_identity_cell",
                            },
                        }
                    }
                    # Keep scanning because the two wanted fields are on different rows.
                    existing = locals().get("identity_fields")
                    if existing is None:
                        identity_fields = {}
                    identity_fields.update(fields)
            if "identity_fields" in locals() and wanted.issubset(identity_fields):
                return identity_fields, diagnostics
        diagnostics.append(f"PDF第{page.number + 1}页首页表格未完整解析委托方及地址")
        return locals().get("identity_fields", {}), diagnostics
    diagnostics.append("未找到标题为“检验报告首页”的页面")
    return {}, diagnostics


def _text_lines(page: fitz.Page) -> list[tuple[str, list[float]]]:
    lines: list[tuple[str, list[float]]] = []
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            text = "".join(span.get("text", "") for span in line.get("spans", [])).strip()
            if text:
                lines.append((text, rect_list(line.get("bbox")) or []))
    return lines


def _photo_subject(title: str) -> tuple[str, str]:
    subject = re.sub(r"^№\s*\d+\s*", "", title).strip()
    if "中文标签样张" in subject or "标签样张" in subject:
        return subject.replace("中文标签样张", "").replace("标签样张", "").strip(), "label"
    for suffix in PHOTO_ORIENTATIONS:
        if compact_layout(subject).endswith(suffix):
            subject = subject[: -len(suffix)].strip()
            break
    if "软件版本" in compact_layout(subject):
        return subject, "other"
    return subject, "object"


def parse_photo_entries(document: fitz.Document) -> tuple[list[dict[str, Any]], list[str]]:
    """Extract № captions and pair them with actual image rectangles on photo pages."""

    entries: list[dict[str, Any]] = []
    diagnostics: list[str] = []
    for page in document:
        page_text = compact_layout(page.get_text("text"))
        if "检验报告照片页" not in page_text:
            continue
        page_entries: list[dict[str, Any]] = []
        for text, bbox in _text_lines(page):
            match = re.match(r"^№\s*(\d+)\s*(.*)$", text)
            if not match:
                continue
            subject, kind = _photo_subject(text)
            # A bare numbered caption is an unresolved object/photo rather
            # than a missing entry; keep it for manual association.
            if not subject:
                kind = "other"
            page_entries.append(
                {
                    "number": int(match.group(1)),
                    "title": text,
                    "subject": subject,
                    "kind": kind,
                    "pdf_page": page.number + 1,
                    "title_rect": bbox,
                    "image_index": None,
                    "image_rect": None,
                }
            )
        page_entries.sort(key=lambda item: item["number"])
        image_info = sorted(
            page.get_image_info(xrefs=True),
            key=lambda item: (float(item["bbox"][1]), float(item["bbox"][0])),
        )
        if len(image_info) != len(page_entries):
            diagnostics.append(
                f"PDF第{page.number + 1}页有{len(page_entries)}个照片标题、{len(image_info)}个图像区域"
            )
        for index, entry in enumerate(page_entries):
            if index < len(image_info):
                entry["image_index"] = index + 1
                entry["image_rect"] = rect_list(image_info[index]["bbox"])
                entry["image_xref"] = image_info[index].get("xref")
            entries.append(entry)
    if not entries:
        diagnostics.append("未找到可可靠解析的照片页标题")
    numbers = [entry["number"] for entry in entries]
    if len(numbers) != len(set(numbers)):
        diagnostics.append("照片页存在重复照片序号")
    return entries, diagnostics


def _image_index_from_path(value: Any) -> int | None:
    match = re.search(r"image-(\d+)", str(value or ""))
    return int(match.group(1)) if match else None


def _field_from_lines(
    lines: Sequence[Mapping[str, Any]], labels: Sequence[str]
) -> dict[str, Any] | None:
    for line in lines:
        text = display_text(line.get("text"))
        normalized = compact_layout(text)
        for label in labels:
            label_normalized = compact_layout(label)
            match = re.match(rf"^{re.escape(label_normalized)}[:：](.*)$", normalized)
            if match:
                return {
                    "value": match.group(1),
                    "raw_text": text,
                    "confidence": line.get("confidence"),
                }
    return None


def load_ocr_records(path: Path | str | None) -> tuple[list[dict[str, Any]], list[str]]:
    """Load reproducible OCR derivatives without calling an OCR engine."""

    if path is None:
        return [], ["未提供OCR派生文件"]
    source = Path(path)
    if not source.is_file():
        return [], [f"OCR派生文件不存在：{source}"]
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return [], [f"OCR派生文件无法读取：{source}（{type(exc).__name__}）"]
    if not isinstance(payload, list):
        return [], [f"OCR派生文件顶层不是数组：{source}"]

    records: list[dict[str, Any]] = []
    diagnostics: list[str] = []
    for item_index, item in enumerate(payload):
        if not isinstance(item, dict) or not isinstance(item.get("pdf_page"), int):
            diagnostics.append(f"OCR派生文件第{item_index + 1}项缺少有效pdf_page")
            continue
        ocr = item.get("ocr")
        if not isinstance(ocr, dict) or not isinstance(ocr.get("lines"), list):
            diagnostics.append(f"OCR派生文件第{item_index + 1}项缺少有效ocr.lines")
            lines: list[dict[str, Any]] = []
            backend = None
        else:
            lines = [line for line in ocr["lines"] if isinstance(line, dict) and line.get("text")]
            backend = ocr.get("backend")
        all_text = "".join(display_text(line.get("text")) for line in lines)
        record = {
            "pdf_page": item["pdf_page"],
            "image_index": item.get("image_index") or _image_index_from_path(item.get("path")),
            "path": item.get("path"),
            "backend": backend,
            "lines": lines,
            "all_text": all_text,
            "fields": {
                "部件名称": _field_from_lines(lines, ("产品名称", "样品名称")),
                "规格型号": _field_from_lines(lines, ("规格型号", "型号规格", "型号")),
                "序列号/批号": _field_from_lines(lines, ("序列号", "批号", "产品编号")),
                "生产日期": _field_from_lines(lines, ("生产日期",)),
                "失效日期": _field_from_lines(lines, ("失效日期", "有效期至", "有效日期", "使用期限")),
                "使用期限": _field_from_lines(lines, ("使用期限",)),
            },
        }
        records.append(record)
    return records, diagnostics


def _name_segments(subject: str) -> list[str]:
    return [compact_layout(part) for part in re.split(r"\s*及\s*", subject) if compact_layout(part)]


def _identity_key(name: str) -> str:
    value = compact_layout(name)
    value = re.sub(r"^心脏脉冲电场消融仪-", "", value)
    value = re.sub(r"（可选）$", "", value)
    return value


def associate_rows_to_photos(
    rows: Sequence[Mapping[str, Any]], entries: Sequence[Mapping[str, Any]]
) -> dict[int, list[dict[str, Any]]]:
    """Associate captions to rows with exact names, then a narrow unique-name fallback."""

    row_keys = defaultdict(list)
    for row in rows:
        row_keys[_identity_key(row["fields"]["部件名称"])].append(row["sequence"])

    associations: dict[int, list[dict[str, Any]]] = {row["sequence"]: [] for row in rows}
    for row in rows:
        name = compact_layout(row["fields"]["部件名称"])
        exact: list[dict[str, Any]] = []
        fallback: list[dict[str, Any]] = []
        for source_entry in entries:
            entry = dict(source_entry)
            segments = _name_segments(entry["subject"])
            if name in segments:
                entry["association_method"] = "exact_caption_segment"
                exact.append(entry)
                continue
            key = _identity_key(row["fields"]["部件名称"])
            if (
                key
                and len(row_keys[key]) == 1
                and any(_identity_key(segment) == key for segment in segments)
            ):
                entry["association_method"] = "unique_device_prefix_or_optional_suffix"
                fallback.append(entry)
        associations[row["sequence"]] = exact or fallback
    return associations


def _record_key(record: Mapping[str, Any]) -> tuple[int, int] | None:
    page = record.get("pdf_page")
    image_index = record.get("image_index")
    if isinstance(page, int) and isinstance(image_index, int):
        return page, image_index
    return None


def _ocr_for_entry(
    entry: Mapping[str, Any], records_by_key: Mapping[tuple[int, int], Mapping[str, Any]]
) -> Mapping[str, Any] | None:
    image_index = entry.get("image_index")
    page = entry.get("pdf_page")
    if not isinstance(page, int) or not isinstance(image_index, int):
        return None
    return records_by_key.get((page, image_index))


def photo_location(entry: Mapping[str, Any], source: str = "photo_image") -> dict[str, Any]:
    rect = entry.get("image_rect") if source == "photo_image" else entry.get("title_rect")
    return {
        "document_role": "report",
        "pdf_page": entry["pdf_page"],
        "rect": rect,
        "source": source,
        "photo_number": entry["number"],
        "coordinate_precision": "image_region" if source == "photo_image" else "native_text",
    }


def _field_comparison(
    expected: str,
    observed_field: Mapping[str, Any] | None,
    *,
    missing_reason: str = "ocr_field_not_resolved",
) -> dict[str, Any]:
    if observed_field is None:
        return {
            "status": "manual",
            "report_value": expected,
            "observed_value": None,
            "reason_code": missing_reason,
        }
    observed = display_text(observed_field.get("value"))
    if compact_layout(expected) == compact_layout(observed):
        return {
            "status": "pass",
            "report_value": expected,
            "observed_value": observed,
            "reason_code": "exact_after_layout_whitespace_only",
            "ocr_confidence": observed_field.get("confidence"),
        }
    if compact_layout(expected) == "/" and compact_layout(observed) == "见实物":
        return {
            "status": "manual",
            "report_value": expected,
            "observed_value": observed,
            "reason_code": "report_placeholder_label_refers_to_physical_object",
            "ocr_confidence": observed_field.get("confidence"),
        }
    return {
        "status": "manual",
        "report_value": expected,
        "observed_value": observed,
        "reason_code": "single_ocr_source_disagrees",
        "ocr_confidence": observed_field.get("confidence"),
    }


def date_format(value: str) -> str | None:
    normalized = compact_layout(value)
    parse_formats = {
        "YYYY-MM-DD": "%Y-%m-%d",
        "YYYY.MM.DD": "%Y.%m.%d",
        "YYYY/MM/DD": "%Y/%m/%d",
        "YYYYMMDD": "%Y%m%d",
        "YYYY年MM月DD日": "%Y年%m月%d日",
    }
    for label, pattern in DATE_PATTERNS:
        if not re.fullmatch(pattern, normalized):
            continue
        try:
            datetime.strptime(normalized, parse_formats[label])
        except ValueError:
            return None
        return label
    return None


def compare_date_text(report_value: str, label_value: str | None) -> dict[str, Any]:
    report_normalized = compact_layout(report_value)
    label_normalized = compact_layout(label_value)
    if not label_normalized:
        return {
            "status": "manual",
            "report_value": report_value,
            "label_value": label_value,
            "report_format": date_format(report_value),
            "label_format": None,
            "reason_code": "label_date_not_resolved",
        }
    if report_normalized in {"/", "见实物"} or label_normalized in {"/", "见实物"}:
        status = "pass" if report_normalized == label_normalized == "/" else "manual"
        return {
            "status": status,
            "report_value": report_value,
            "label_value": label_value,
            "report_format": None,
            "label_format": None,
            "reason_code": (
                "matching_placeholder" if status == "pass" else "date_not_stated_on_both_sides"
            ),
        }
    report_format = date_format(report_value)
    observed_format = date_format(label_value or "")
    if report_format is None or observed_format is None:
        return {
            "status": "manual",
            "report_value": report_value,
            "label_value": label_value,
            "report_format": report_format,
            "label_format": observed_format,
            "reason_code": "date_format_not_supported_or_ocr_uncertain",
        }
    if report_normalized == label_normalized and report_format == observed_format:
        return {
            "status": "pass",
            "report_value": report_value,
            "label_value": label_value,
            "report_format": report_format,
            "label_format": observed_format,
            "reason_code": "date_value_and_format_exact",
        }
    return {
        "status": "manual",
        "report_value": report_value,
        "label_value": label_value,
        "report_format": report_format,
        "label_format": observed_format,
        "reason_code": "single_ocr_source_date_disagrees",
    }


def _with_locations(
    comparison: dict[str, Any],
    row: Mapping[str, Any],
    field: str,
    photo: Mapping[str, Any] | None,
) -> dict[str, Any]:
    comparison["report_location"] = row["locations"].get(field)
    comparison["photo_location"] = photo_location(photo) if photo is not None else None
    return comparison


def _r02_objects(
    rows: Sequence[Mapping[str, Any]],
    identity: Mapping[str, Mapping[str, Any]],
    label_associations: Mapping[int, Sequence[Mapping[str, Any]]],
    label_ocr_by_key: Mapping[tuple[int, int], Mapping[str, Any]],
) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []
    for row in rows:
        applicable, not_applicable_reason = photo_scope(row)
        if not applicable:
            objects.append(
                {
                    "sequence": row["sequence"],
                    "name": row["fields"]["部件名称"],
                    "status": "not_applicable",
                    "reason_code": not_applicable_reason,
                    "field_comparisons": [],
                    "report_location": row["locations"]["部件名称"],
                    "photo_locations": [],
                }
            )
            continue
        entries = list(label_associations.get(row["sequence"], []))
        if not entries:
            objects.append(
                {
                    "sequence": row["sequence"],
                    "name": row["fields"]["部件名称"],
                    "status": "error",
                    "reason_code": "chinese_label_photo_missing",
                    "field_comparisons": [],
                    "report_location": row["locations"]["部件名称"],
                    "photo_locations": [],
                }
            )
            continue
        entry = entries[0]
        ocr = _ocr_for_entry(entry, label_ocr_by_key)
        if ocr is None:
            objects.append(
                {
                    "sequence": row["sequence"],
                    "name": row["fields"]["部件名称"],
                    "status": "manual",
                    "reason_code": "label_photo_found_but_ocr_record_missing",
                    "field_comparisons": [],
                    "report_location": row["locations"]["部件名称"],
                    "photo_locations": [photo_location(entry)],
                }
            )
            continue

        comparisons: list[dict[str, Any]] = []
        for field in ("部件名称", "规格型号", "序列号/批号", "生产日期"):
            result = _field_comparison(row["fields"][field], ocr["fields"].get(field))
            result["field"] = field
            result["report_location"] = row["locations"][field]
            result["photo_location"] = photo_location(entry)
            comparisons.append(result)

        all_ocr = compact_layout(ocr.get("all_text"))
        for field in ("委托方", "委托方地址"):
            report_field = identity.get(field)
            expected = display_text(report_field.get("value")) if report_field else ""
            if not expected:
                status, reason = "manual", "report_identity_field_not_resolved"
            elif compact_layout(expected) in all_ocr:
                status, reason = "pass", "exact_substring_after_layout_whitespace_only"
            else:
                status, reason = "manual", "single_ocr_source_identity_field_not_resolved"
            comparisons.append(
                {
                    "field": field,
                    "status": status,
                    "report_value": expected or None,
                    "observed_value": None if status == "manual" else expected,
                    "reason_code": reason,
                    "report_location": report_field.get("location") if report_field else None,
                    "photo_location": photo_location(entry),
                }
            )
        objects.append(
            {
                "sequence": row["sequence"],
                "name": row["fields"]["部件名称"],
                "status": _status_from(item["status"] for item in comparisons),
                "reason_code": "field_comparisons_completed",
                "field_comparisons": comparisons,
                "report_location": row["locations"]["部件名称"],
                "photo_locations": [photo_location(entry)],
                "ocr_backend": ocr.get("backend"),
            }
        )
    return objects


def _r03_dates(
    rows: Sequence[Mapping[str, Any]],
    label_associations: Mapping[int, Sequence[Mapping[str, Any]]],
    label_ocr_by_key: Mapping[tuple[int, int], Mapping[str, Any]],
) -> list[dict[str, Any]]:
    comparisons: list[dict[str, Any]] = []
    date_fields = ["生产日期"]
    if any(compact_layout(row["fields"].get("失效日期")) for row in rows):
        date_fields.append("失效日期")
    for row in rows:
        applicable, not_applicable_reason = photo_scope(row)
        entries = list(label_associations.get(row["sequence"], []))
        for field in date_fields:
            if not applicable:
                comparison = {
                    "status": "not_applicable",
                    "report_value": row["fields"].get(field, ""),
                    "label_value": None,
                    "report_format": date_format(row["fields"].get(field, "")),
                    "label_format": None,
                    "reason_code": not_applicable_reason,
                }
                comparisons.append(_with_locations(comparison, row, field, None))
                comparisons[-1].update(sequence=row["sequence"], name=row["fields"]["部件名称"], field=field)
                continue
            if not entries:
                comparison = compare_date_text(row["fields"].get(field, ""), None)
                comparison["reason_code"] = "chinese_label_photo_missing"
                comparisons.append(_with_locations(comparison, row, field, None))
                comparisons[-1].update(sequence=row["sequence"], name=row["fields"]["部件名称"], field=field)
                continue
            entry = entries[0]
            ocr = _ocr_for_entry(entry, label_ocr_by_key)
            observed = None
            confidence = None
            if ocr is not None and ocr["fields"].get(field) is not None:
                observed_field = ocr["fields"][field]
                observed = observed_field["value"]
                confidence = observed_field.get("confidence")
            comparison = compare_date_text(row["fields"].get(field, ""), observed)
            comparison["ocr_confidence"] = confidence
            comparisons.append(_with_locations(comparison, row, field, entry))
            comparisons[-1].update(sequence=row["sequence"], name=row["fields"]["部件名称"], field=field)
    return comparisons


def _observed_values_for_field(
    field: str,
    entries: Sequence[Mapping[str, Any]],
    ocr_by_key: Mapping[tuple[int, int], Mapping[str, Any]],
) -> list[str]:
    observed: list[str] = []
    for entry in entries:
        if field == "部件名称":
            observed.append(entry["subject"])
        record = _ocr_for_entry(entry, ocr_by_key)
        if record is None:
            continue
        ocr_field = record["fields"].get(field)
        if ocr_field is not None:
            observed.append(display_text(ocr_field["value"]))
        if field == "备注":
            observed.extend(display_text(line.get("text")) for line in record.get("lines", []))
    return observed


def _r04_cells(
    rows: Sequence[Mapping[str, Any]],
    all_associations: Mapping[int, Sequence[Mapping[str, Any]]],
    all_ocr_by_key: Mapping[tuple[int, int], Mapping[str, Any]],
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for row in rows:
        applicable, not_applicable_reason = photo_scope(row)
        entries = list(all_associations.get(row["sequence"], []))
        for field in CHECKED_SAMPLE_COLUMNS:
            expected = row["fields"][field]
            if not expected and field not in {"生产日期", "失效日期"}:
                continue
            base = {
                "sequence": row["sequence"],
                "name": row["fields"]["部件名称"],
                "field": field,
                "report_value": expected,
                "report_location": row["locations"][field],
                "photo_locations": [photo_location(entry) for entry in entries],
            }
            if not applicable:
                base.update(status="not_applicable", observed_values=[], reason_code=not_applicable_reason)
                checks.append(base)
                continue
            if not expected and field in {"生产日期", "失效日期"}:
                base.update(status="manual", observed_values=[], reason_code="report_date_not_stated")
                checks.append(base)
                continue
            if not entries:
                base.update(status="error", observed_values=[], reason_code="no_associated_photo_evidence")
                checks.append(base)
                continue

            observed = _observed_values_for_field(field, entries, all_ocr_by_key)
            expected_compact = compact_layout(expected)
            matched = any(expected_compact == compact_layout(value) for value in observed)
            if matched:
                base.update(
                    status="pass",
                    observed_values=observed,
                    reason_code="exact_after_layout_whitespace_only",
                )
            elif expected_compact == "/":
                if field in {"序列号/批号", "生产日期"} and any(
                    compact_layout(value) == "见实物" for value in observed
                ):
                    reason = "report_placeholder_photo_refers_to_physical_object"
                else:
                    reason = "placeholder_content_not_reliably_localized"
                base.update(status="manual", observed_values=observed, reason_code=reason)
            elif observed:
                # OCR-only disagreement does not directly become an error.
                base.update(
                    status="manual",
                    observed_values=observed,
                    reason_code="single_ocr_source_did_not_find_exact_content",
                )
            else:
                base.update(
                    status="manual",
                    observed_values=[],
                    reason_code="associated_photo_found_but_content_not_resolved",
                )
            checks.append(base)
    return checks


def _presence_checks(
    rows: Sequence[Mapping[str, Any]],
    associations: Mapping[int, Sequence[Mapping[str, Any]]],
    *,
    missing_reason: str,
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for row in rows:
        applicable, not_applicable_reason = photo_scope(row)
        entries = list(associations.get(row["sequence"], []))
        checks.append(
            {
                "sequence": row["sequence"],
                "name": row["fields"]["部件名称"],
                "status": "pass" if applicable and entries else "error" if applicable else "not_applicable",
                "reason_code": "associated_photo_found" if applicable and entries else missing_reason if applicable else not_applicable_reason,
                "report_location": row["locations"]["部件名称"],
                "photo_locations": [photo_location(entry) for entry in entries],
                "photo_numbers": [entry["number"] for entry in entries],
                "association_methods": sorted(
                    {entry.get("association_method", "unknown") for entry in entries}
                ),
            }
        )
    return checks


def _issue_evidence(items: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()

    def add(location: Mapping[str, Any] | None, item: Mapping[str, Any]) -> None:
        if not location or not isinstance(location.get("pdf_page"), int) or not location.get("rect"):
            return
        key = (
            location.get("pdf_page"),
            tuple(location.get("rect")),
            location.get("source"),
            item.get("sequence"),
            item.get("field"),
        )
        if key in seen:
            return
        seen.add(key)
        evidence.append(
            {
                **dict(location),
                "rects": [location["rect"]],
                "sequence": item.get("sequence"),
                "field": item.get("field"),
            }
        )

    for item in items:
        if item.get("status") in {"pass", "not_applicable"}:
            continue
        add(item.get("report_location"), item)
        add(item.get("photo_location"), item)
        for location in item.get("photo_locations", []):
            add(location, item)
        for comparison in item.get("field_comparisons", []):
            if comparison.get("status") == "pass":
                continue
            add(comparison.get("report_location"), {**item, "field": comparison.get("field")})
            add(comparison.get("photo_location"), {**item, "field": comparison.get("field")})
    return evidence


def _count_status(items: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    return {status: sum(item.get("status") == status for item in items) for status in PHOTO_ITEM_STATUSES}


def _finding(
    rule_id: str,
    title: str,
    items: list[dict[str, Any]],
    summary: str,
    *,
    details_key: str,
    additional_details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    counts = _count_status(items)
    details: dict[str, Any] = {
        "counts": counts,
        details_key: items,
    }
    if additional_details:
        details.update(additional_details)
    return {
        "id": f"REPORT-{rule_id}",
        "title": title,
        "status": _status_from(item["status"] for item in items),
        "summary": summary.format(**counts),
        "details": details,
        "evidence": _issue_evidence(items),
    }


def analyze_report_photo_rules(
    report_pdf: Path | str,
    label_ocr_json: Path | str | None,
    object_ocr_json: Path | str | None = None,
) -> dict[str, Any]:
    """Run R02-R06 from the PDF plus already-produced, reproducible OCR JSON."""

    report_path = Path(report_pdf)
    with fitz.open(report_path) as document:
        rows, sample_diagnostics = parse_sample_description(document)
        identity, identity_diagnostics = parse_report_identity(document)
        photos, photo_diagnostics = parse_photo_entries(document)

    label_ocr, label_ocr_diagnostics = load_ocr_records(label_ocr_json)
    object_ocr, object_ocr_diagnostics = load_ocr_records(object_ocr_json)
    label_ocr_by_key = {
        key: record for record in label_ocr if (key := _record_key(record)) is not None
    }
    object_ocr_by_key = {
        key: record for record in object_ocr if (key := _record_key(record)) is not None
    }
    all_ocr_by_key = {**object_ocr_by_key, **label_ocr_by_key}

    label_photos = [entry for entry in photos if entry["kind"] == "label"]
    object_photos = [entry for entry in photos if entry["kind"] == "object"]
    label_associations = associate_rows_to_photos(rows, label_photos)
    object_associations = associate_rows_to_photos(rows, object_photos)
    all_associations = {
        row["sequence"]: [
            *object_associations.get(row["sequence"], []),
            *label_associations.get(row["sequence"], []),
        ]
        for row in rows
    }

    r02_objects = _r02_objects(rows, identity, label_associations, label_ocr_by_key)
    r03_dates = _r03_dates(rows, label_associations, label_ocr_by_key)
    r04_cells = _r04_cells(rows, all_associations, all_ocr_by_key)
    r05_objects = _presence_checks(
        rows,
        object_associations,
        missing_reason="physical_object_photo_missing",
    )
    r06_labels = _presence_checks(
        rows,
        label_associations,
        missing_reason="chinese_label_photo_missing",
    )

    findings = [
        _finding(
            "R02",
            "首页/样品描述字段与中文标签",
            r02_objects,
            "{pass}个对象字段一致，{manual}个待人工复核，{error}个缺少可核对中文标签，{not_applicable}个不适用",
            details_key="objects",
            additional_details={
                "rule": "仅忽略排版空白；OCR单源差异不直接判错；法律角色不自动合并；未使用或非实物条目保留为不适用",
                "identity_fields": identity,
            },
        ),
        _finding(
            "R03",
            "生产日期值与格式一致性",
            r03_dates,
            "{pass}个日期值和格式一致，{manual}个无法自动确认，{error}个确定不一致",
            details_key="comparisons",
            additional_details={
                "rule": "生产日期值及书写格式必须同时一致，不做格式等价转换",
                "accepted_formats": [label for label, _ in DATE_PATTERNS],
            },
        ),
        _finding(
            "R04",
            "样品描述内容在照片页的覆盖",
            r04_cells,
            "{pass}个非序号单元格找到可靠证据，{manual}个待人工复核，{error}个未找到关联照片证据，{not_applicable}个不适用",
            details_key="cell_checks",
            additional_details={
                "rule": "样品描述中除序号外的每个非空单元格均纳入；明确未使用或非实物条目记录为不适用，实物条目按照片证据核对",
            },
        ),
        _finding(
            "R05",
            "每个对象的实物照片",
            r05_objects,
            "{pass}个对象有可关联实物照片，{error}个对象缺少实物照片，{manual}个待人工复核，{not_applicable}个不适用",
            details_key="objects",
            additional_details={
                "rule": "仅对实物对象核对；标签、铭牌或包装特写不单独计为实物照片；组合照片可覆盖多个明确对象",
            },
        ),
        _finding(
            "R06",
            "每个对象的中文标签照片",
            r06_labels,
            "{pass}个对象有可关联中文标签照片，{error}个对象缺少中文标签照片，{manual}个待人工复核，{not_applicable}个不适用",
            details_key="objects",
            additional_details={
                "rule": "仅对实物对象核对；器械本体或最小销售包装上的可读中文标签均可；归属不清不自动通过",
            },
        ),
    ]

    return {
        "report_pdf": str(report_path),
        "rules_attempted": ["R02", "R03", "R04", "R05", "R06"],
        "findings": findings,
        "sample_rows": rows,
        "photo_entries": photos,
        "diagnostics": {
            "sample_description": sample_diagnostics,
            "report_identity": identity_diagnostics,
            "photo_pages": photo_diagnostics,
            "label_ocr": label_ocr_diagnostics,
            "object_ocr": object_ocr_diagnostics,
            "label_ocr_records": len(label_ocr),
            "object_ocr_records": len(object_ocr),
        },
    }


def report_photo_check(
    report_pdf: Path | str,
    label_ocr_json: Path | str | None,
    object_ocr_json: Path | str | None = None,
) -> list[dict[str, Any]]:
    return analyze_report_photo_rules(report_pdf, label_ocr_json, object_ocr_json)["findings"]
