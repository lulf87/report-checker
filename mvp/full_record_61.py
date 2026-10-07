from __future__ import annotations

"""Conservative full-coverage comparison for GB 9706.1 Records.

The source PDFs are read-only.  Native PDF text and table geometry establish
the inventory and coordinates.  Ink is used only to resolve a checkbox when a
single status column is unambiguous.  Handwritten measurements are retained as
manual-review candidates until a value can be attributed to one cell and
independently recognised by two local channels.
"""

import re
import tempfile
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:
    import pymupdf as fitz
except ImportError:
    import fitz

from mvp.checker import (
    accepted_numeric_candidate,
    compact,
    display_text,
    extract_report_number,
    local_ocr_candidates,
    local_text_ocr,
    render_cell_for_ocr,
    sha256_file,
    expected_report_conclusion,
)
from mvp.capabilities import MODE_CATALOG
from mvp.input_variants import Record61StatusInventoryError
from mvp.record_full import (
    ComparisonResult,
    CoverageEntry,
    ReportRow,
    compare_final_percentage,
    compare_numeric_observation,
    compare_record61_status_result,
    convert_decimal,
    expected_report_conclusion_from_record_statuses,
    is_actual_report_result,
    parse_report_numeric,
    scan_report_rows,
    validate_coverage,
)


RectTuple = tuple[float, float, float, float]

MODE = "report_record_9706_1"
RECORD_ROLE = "record_9706_1"
BODY_FIRST_PDF_PAGE = 6
BODY_LAST_PDF_PAGE = 96
EXPECTED_NATIVE_BOX_TRIPLETS = 849
EXPECTED_ALTERNATE_BOX_TRIPLETS = 2
EXPECTED_STATUS_ROWS = EXPECTED_NATIVE_BOX_TRIPLETS + EXPECTED_ALTERNATE_BOX_TRIPLETS


STATUS_BY_COLUMN = {0: "符合", 1: "不符合", 2: "不适用"}
BOX_GLYPHS = {"□", "\uf0a3"}

NUMERIC_TARGETS_BY_SAMPLE: dict[int, dict[str, int]] = {
    1347: {"4.11": 0, "8.6": 0, "8.7": 16, "9.6": 1, "16.6": 0},
    1539: {"4.11": 1, "8.6": 2, "8.7": 20, "9.6": 1, "16.6": 3},
    2948: {"4.11": 2, "8.6": 2, "8.7": 38, "9.6": 2, "16.6": 2},
}

IDENTITY_FIELDS: tuple[tuple[str, str, int, int, int], ...] = (
    # key, Report label, Report page-3 table row/column, Record baseline index
    ("sample_name", "样品名称/描述", 0, 1, 1),
    ("manufacturer", "制造商↔生产单位", 5, 1, 2),
    ("device_model", "设备型号↔型号规格", 2, 4, 3),
    ("factory_number", "出厂编号↔产品编号/批号", 4, 4, 4),
)

# S30/S31/S33/S35: object-level metadata scope. These fields are discovered in
# the native text of both documents. Empty fields stay manual; a field absent
# from both approved templates is explicitly not_applicable with search basis.
METADATA_FIELDS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("test_date", ("检测日期", "测试日期", "检验日期"), "S35"),
    ("instrument", ("检测设备", "检测仪器", "测量设备"), "S30"),
    ("tester", ("检测人员", "检验人员", "检验 ：", "检验:"), "S30"),
    ("reviewer", ("复核人员", "审核", "复核"), "S35"),
    ("signature", ("签字", "批准", "审核", "检验 ：", "检验:"), "S35"),
    ("remark", ("备注", "观察记录"), "S35"),
)


def _rect(value: Sequence[float] | fitz.Rect) -> list[float]:
    rect = fitz.Rect(value)
    return [round(float(rect.x0), 3), round(float(rect.y0), 3), round(float(rect.x1), 3), round(float(rect.y1), 3)]


def _valid_rect(value: Sequence[float] | None) -> bool:
    return bool(value and len(value) == 4 and value[0] < value[2] and value[1] < value[3])


def _annotation_points(annotation: fitz.Annot) -> list[tuple[float, float]]:
    return [
        (float(point[0]), float(point[1]))
        for stroke in (annotation.vertices or [])
        for point in stroke
    ]


def _page_inks(page: fitz.Page) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for annotation in page.annots() or []:
        if len(annotation.type) < 2 or annotation.type[1] != "Ink":
            continue
        points = _annotation_points(annotation)
        if not points:
            continue
        bbox = fitz.Rect(annotation.rect)
        values.append(
            {
                "centroid_x": sum(point[0] for point in points) / len(points),
                "centroid_y": sum(point[1] for point in points) / len(points),
                "bbox": bbox,
                "point_count": len(points),
                "_points": points,
            }
        )
    return values


def _native_status_box_groups(page: fitz.Page) -> list[dict[str, Any]]:
    boxes: list[tuple[str, fitz.Rect]] = []
    for block in page.get_text("rawdict").get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                for character in span.get("chars", []):
                    glyph = character.get("c")
                    bbox = fitz.Rect(character.get("bbox"))
                    # The three conformance columns begin at x=412.7.  This x
                    # guard excludes the three power-source option boxes on p9.
                    if glyph in BOX_GLYPHS and bbox.x0 > 400:
                        boxes.append((glyph, bbox))

    groups: list[dict[str, Any]] = []
    for glyph, bbox in sorted(boxes, key=lambda item: (item[1].y0, item[1].x0)):
        center_y = (bbox.y0 + bbox.y1) / 2
        if not groups or abs(center_y - groups[-1]["center_y"]) > 1.0:
            groups.append({"center_y": center_y, "members": [(glyph, bbox)]})
        else:
            groups[-1]["members"].append((glyph, bbox))

    result: list[dict[str, Any]] = []
    for group in groups:
        members = sorted(group["members"], key=lambda item: item[1].x0)
        if len(members) != 3:
            raise ValueError(
                f"Record page {page.number + 1}: status boxes do not form a triplet "
                f"at y={group['center_y']:.3f} ({len(members)} boxes)"
            )
        result.append(
            {
                "center_y": group["center_y"],
                "glyphs": [glyph for glyph, _ in members],
                "boxes": [bbox for _, bbox in members],
                "glyph_encoding": (
                    "native_square_triplet"
                    if all(glyph == "□" for glyph, _ in members)
                    else "alternate_first_square_triplet"
                ),
            }
        )
    return result


def _clause_token(value: str) -> str:
    normalized = re.sub(r"\s+", "", value or "")
    normalized = re.sub(r"^续", "", normalized)
    normalized = normalized.rstrip(".。")
    return normalized


_CLAUSE_NUMBER = r"\d+(?:\s*\.\s*\d+)*"
_CLAUSE_RANGE_RE = re.compile(
    rf"^\s*(?P<start>{_CLAUSE_NUMBER})\s*(?:~|～|〜|∼|至|–|—|-)\s*"
    rf"(?P<end>{_CLAUSE_NUMBER})\s*$"
)


def _clause_parts(value: str) -> tuple[int, ...] | None:
    token = _clause_token(value)
    if not re.fullmatch(r"\d+(?:\.\d+)*", token):
        return None
    return tuple(int(part) for part in token.split("."))


def _clause_in_explicit_range(source_clause: str, report_clause: str) -> bool:
    match = _CLAUSE_RANGE_RE.fullmatch(report_clause or "")
    if not match:
        return False
    source = _clause_parts(source_clause)
    start = _clause_parts(match.group("start"))
    end = _clause_parts(match.group("end"))
    if source is None or start is None or end is None:
        return False
    if len(source) != len(start) or len(start) != len(end):
        return False
    if source[:-1] != start[:-1] or start[:-1] != end[:-1]:
        return False
    lower, upper = sorted((start[-1], end[-1]))
    return lower <= source[-1] <= upper


def _plausible_clause(value: str) -> str:
    text = display_text(value)
    if len(text) > 32:
        return ""
    compacted = _clause_token(text)
    if re.match(r"^\d+(?:\.\d+)*(?:-\d+(?:\.\d+)*)?$", compacted):
        return compacted
    return ""


_UNIT_RE = re.compile(
    r"(?:mA|uA|μA|µA|A|mV|kV|V|mΩ|kΩ|MΩ|Ω|Hz|kHz|MHz|dB\([A-Z]\)|°C|℃|%|％)"
)


def _unit_from_text(*values: str) -> str | None:
    for value in values:
        match = _UNIT_RE.search(display_text(value))
        if match:
            return match.group(0)
    return None


def _row_status_cell(
    page: fitz.Page,
    group: Mapping[str, Any],
    tables: Sequence[tuple[fitz.table.Table, list[list[Any]]]],
) -> tuple[list[list[Any]], int, fitz.Rect]:
    center_y = float(group["center_y"])
    candidates: list[tuple[list[list[Any]], int, fitz.Rect]] = []
    for table, extracted in tables:
        for row_index, row in enumerate(table.rows):
            for cell in row.cells:
                if cell is None:
                    continue
                bbox = fitz.Rect(cell)
                if (
                    400 < bbox.x0 < 430
                    and 530 < bbox.x1 < 570
                    and bbox.width < 200
                    and bbox.y0 - 1 <= center_y <= bbox.y1 + 1
                ):
                    candidates.append((extracted, row_index, bbox))
    if len(candidates) != 1:
        raise ValueError(
            f"Record page {page.number + 1}: status table row is not unique "
            f"at y={center_y:.3f} ({len(candidates)} candidates)"
        )
    return candidates[0]


def _status_from_ink(
    status_cell: fitz.Rect,
    boxes: Sequence[fitz.Rect],
    page_inks: Sequence[Mapping[str, Any]],
    *,
    glyph_encoding: str = "native_square_triplet",
) -> dict[str, Any]:
    centers = [(box.x0 + box.x1) / 2 for box in boxes]
    owned = [
        ink
        for ink in page_inks
        if status_cell.y0 <= float(ink["centroid_y"]) <= status_cell.y1
        and status_cell.x0 - 5 <= float(ink["centroid_x"]) <= status_cell.x1 + 5
    ]
    selected_columns: list[int] = []
    public_inks: list[dict[str, Any]] = []
    large_strike = False
    for ink in owned:
        bbox = fitz.Rect(ink["bbox"])
        column = min(range(3), key=lambda index: abs(float(ink["centroid_x"]) - centers[index]))
        selected_columns.append(column)
        # Normal handwritten ticks often extend into an adjacent printed box.
        # Only a stroke spanning most of the complete status band, or a long
        # near-horizontal deletion line, is treated as a crossed-out row.
        is_large = bbox.width >= 82 or (bbox.height <= 4 and bbox.width >= 58)
        large_strike = large_strike or is_large
        public_inks.append(
            {
                "bbox": _rect(bbox),
                "centroid": [round(float(ink["centroid_x"]), 3), round(float(ink["centroid_y"]), 3)],
                "point_count": int(ink["point_count"]),
                "nearest_column": column,
                "large_cross_column_strike": is_large,
            }
        )

    distinct_columns = sorted(set(selected_columns))
    if not owned:
        status = None
        reason = "status_box_blank"
        status_class = "blank"
    elif large_strike:
        status = None
        reason = "void_or_crossed_out"
        status_class = "void_or_crossed_out"
    elif len(distinct_columns) != 1:
        status = None
        reason = "multiple_status_columns_selected"
        status_class = "ambiguous"
    else:
        status = STATUS_BY_COLUMN[distinct_columns[0]]
        reason = "single_ink_column_resolved"
        status_class = "selected"
    return {
        "status": status,
        "reason_code": reason,
        "status_class": status_class,
        "glyph_encoding": glyph_encoding,
        "glyph_variant": (
            "alternate" if glyph_encoding != "native_square_triplet" else "native"
        ),
        "selected_columns": distinct_columns,
        "inks": public_inks,
    }


def extract_record_61_status_rows(document: fitz.Document) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if document.page_count < BODY_LAST_PDF_PAGE:
        raise ValueError(
            f"GB 9706.1 Record has {document.page_count} pages; page {BODY_LAST_PDF_PAGE} is required"
        )

    rows: list[dict[str, Any]] = []
    active_clause = ""
    active_project = ""
    page_counts: dict[int, int] = {}
    table_shapes: dict[str, list[list[int]]] = {}
    alternate_rows: list[dict[str, Any]] = []

    for pdf_page in range(BODY_FIRST_PDF_PAGE, BODY_LAST_PDF_PAGE + 1):
        page = document[pdf_page - 1]
        groups = _native_status_box_groups(page)
        page_counts[pdf_page] = len(groups)
        tables = [(table, table.extract()) for table in page.find_tables().tables]
        table_shapes[str(pdf_page)] = [
            [int(table.row_count), int(table.col_count)] for table, _ in tables
        ]
        inks = _page_inks(page)

        for page_ordinal, group in enumerate(groups, start=1):
            extracted, row_index, status_cell = _row_status_cell(page, group, tables)
            for prior_index in range(row_index, -1, -1):
                row = extracted[prior_index]
                candidate = _plausible_clause(display_text(row[0]) if row else "")
                if candidate:
                    active_clause = candidate
                    if len(_clause_parts(candidate) or ()) <= 1 and len(row) > 1:
                        active_project = display_text(row[1]) or active_project
                    break
            row_values = extracted[row_index]
            if active_clause and len(_clause_parts(active_clause) or ()) <= 1 and len(row_values) > 1:
                active_project = display_text(row_values[1]) or active_project
            requirement = display_text(row_values[1]) if len(row_values) > 1 else ""
            suggestion = display_text(row_values[2]) if len(row_values) > 2 else ""
            ink_result = _status_from_ink(
                status_cell,
                group["boxes"],
                inks,
                glyph_encoding=group["glyph_encoding"],
            )
            row_id = f"record61:status:p{pdf_page:03d}:r{page_ordinal:02d}"
            item = {
                "row_id": row_id,
                "ordinal": len(rows) + 1,
                "pdf_page": pdf_page,
                "page_ordinal": page_ordinal,
                "clause": active_clause,
                "project": active_project,
                "requirement": requirement,
                "suggestion": suggestion,
                "condition": suggestion,
                "unit": _unit_from_text(requirement, suggestion),
                "result": ink_result["status"],
                "status": ink_result["status"],
                "status_reason_code": ink_result["reason_code"],
                "status_class": ink_result["status_class"],
                "status_disposition": (
                    "matched" if ink_result["status_class"] == "selected" else "manual"
                ),
                "status_glyph_variant": ink_result["glyph_variant"],
                "selected_columns": ink_result["selected_columns"],
                "inks": ink_result["inks"],
                "status_bbox": _rect(status_cell),
                "box_bboxes": [_rect(box) for box in group["boxes"]],
                "box_glyphs": list(group["glyphs"]),
                "glyph_encoding": group["glyph_encoding"],
            }
            rows.append(item)
            if group["glyph_encoding"] != "native_square_triplet":
                alternate_rows.append(
                    {
                        "row_id": row_id,
                        "pdf_page": pdf_page,
                        "clause": active_clause,
                        "box_glyph_codepoints": [f"U+{ord(value):04X}" for value in group["glyphs"]],
                        "status_bbox": _rect(status_cell),
                    }
                )

    encoding_counts = Counter(row["glyph_encoding"] for row in rows)
    status_class_counts = Counter(row["status_class"] for row in rows)
    native_count = encoding_counts["native_square_triplet"]
    alternate_count = encoding_counts["alternate_first_square_triplet"]
    if (
        len(rows) != EXPECTED_STATUS_ROWS
        or native_count != EXPECTED_NATIVE_BOX_TRIPLETS
        or alternate_count != EXPECTED_ALTERNATE_BOX_TRIPLETS
    ):
        raise Record61StatusInventoryError(
            total=len(rows),
            native=native_count,
            alternate=alternate_count,
            expected_total=EXPECTED_STATUS_ROWS,
            expected_native=EXPECTED_NATIVE_BOX_TRIPLETS,
            expected_alternate=EXPECTED_ALTERNATE_BOX_TRIPLETS,
        )
    return rows, {
        "method": "native_box_glyphs_grouped_by_page_and_y_then_bound_to_status_table_cell",
        "body_pdf_pages": [BODY_FIRST_PDF_PAGE, BODY_LAST_PDF_PAGE],
        "status_row_count": len(rows),
        "native_box_triplet_count": native_count,
        "alternate_glyph_triplet_count": alternate_count,
        "status_class_counts": dict(status_class_counts),
        "alternate_glyph_rows": alternate_rows,
        "page_row_counts": page_counts,
        "ink_assignment": "polyline_point_centroid_to_nearest_printed_box_x",
        "unresolved_policy": "blank_multi_column_or_large_cross_column_strike_is_manual",
        "status_semantics": {
            "selected": "single ink column assigned to 符合/不符合/不适用",
            "blank": "no owned ink in the three status boxes; manual",
            "void_or_crossed_out": "long cancellation stroke across the status band; manual",
            "ambiguous": "ink occupies multiple status columns; manual",
            "glyph_variants": ["native_square_triplet", "alternate_first_square_triplet"],
        },
        "template_structure": {
            "expected_physical_pages": [BODY_FIRST_PDF_PAGE, BODY_LAST_PDF_PAGE],
            "observed_physical_pages": list(range(BODY_FIRST_PDF_PAGE, BODY_LAST_PDF_PAGE + 1)),
            "missing_physical_pages": [],
            "page_table_count": {str(page): 1 for page in page_counts},
            "page_table_shapes": table_shapes,
            "header_policy": "status_triplets_bound_to_table_cells; continuation_headers_allowed",
            "header_checks": {
                "all_pages_have_table": all(count >= 1 for count in page_counts.values()),
                "all_pages_have_status_rows": all(count > 0 for count in page_counts.values()),
                "page_count_complete": set(page_counts) == set(range(BODY_FIRST_PDF_PAGE, BODY_LAST_PDF_PAGE + 1)),
            },
            "validated": (
                set(page_counts) == set(range(BODY_FIRST_PDF_PAGE, BODY_LAST_PDF_PAGE + 1))
                and all(count >= 1 for count in page_counts.values())
                and all(count > 0 for count in page_counts.values())
            ),
        },
    }


def _report_location(row: ReportRow, *, prefer_conclusion: bool = False) -> dict[str, Any]:
    bbox = row.conclusion_rect if prefer_conclusion else row.result_rect
    bbox = bbox or row.requirement_rect or row.conclusion_rect
    if bbox is None:
        raise ValueError(f"Report row has no evidence rectangle: {row.row_id}")
    return {"pdf_page": row.pdf_page, "bbox": list(bbox)}


def _report_row_payload(row: ReportRow) -> dict[str, Any]:
    payload = row.to_dict()
    # Keep the canonical ReportRow names and expose the paired-field names used
    # by the scope ledger so reviewers can compare both sides without schema
    # knowledge.
    payload.update(
        {
            "project": row.project_raw,
            "clause": row.clause_raw,
            "requirement": row.requirement_raw,
            "suggestion": " ".join(row.condition_tokens),
            "condition": " ".join(row.condition_tokens),
            "unit": row.unit_context,
            "result": row.result_raw,
            "conclusion": row.conclusion_raw,
        }
    )
    return payload


def _paired_field_comparisons(
    record_payload: Mapping[str, Any],
    report_payload: Mapping[str, Any],
    *,
    mapped: bool = True,
    result_comparison: Mapping[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    comparisons: dict[str, dict[str, Any]] = {}
    # The six textual/metadata fields are deliberately retained even when a
    # row cannot be paired.  A one-sided edge must expose unresolved evidence,
    # rather than comparing the row with an arbitrary sequence-context row.
    for field in ("project", "clause", "requirement", "suggestion", "condition", "unit"):
        record_value = compact(record_payload.get(field))
        report_value = compact(report_payload.get(field))
        if not mapped:
            decision = "manual"
            reason = "row_mapping_unresolved"
        elif not record_value and not report_value:
            decision = "not_applicable"
            reason = "paired_field_absent_on_both_sides"
        elif not record_value or not report_value:
            decision = "manual"
            reason = "paired_field_unresolved"
        elif _identity_normalize(record_value) == _identity_normalize(report_value):
            decision = "match"
            reason = "paired_field_match"
        else:
            decision = "mismatch"
            reason = "paired_field_difference"
        comparisons[field] = {
            "decision": decision,
            "reason_code": reason,
            "record": record_payload.get(field),
            "report": report_payload.get(field),
            "record_value": record_payload.get(field),
            "report_value": report_payload.get(field),
        }
    if not mapped:
        result_decision = "manual"
        result_reason = "row_mapping_unresolved"
    elif result_comparison is None:
        result_decision = "manual"
        result_reason = "paired_result_unresolved"
    else:
        result_decision = str(result_comparison.get("decision") or "manual")
        result_reason = str(result_comparison.get("reason_code") or "paired_result_unresolved")
    comparisons["result"] = {
        "decision": result_decision,
        "reason_code": result_reason,
        "record": record_payload.get("result", record_payload.get("status")),
        "report": report_payload.get("result"),
        "record_value": record_payload.get("result", record_payload.get("status")),
        "report_value": report_payload.get("result"),
    }
    return comparisons


def _field_occurrences(
    document: fitz.Document,
    labels: Sequence[str],
    *,
    pages: set[int] | None = None,
) -> list[dict[str, Any]]:
    """Locate label lines and their value text with native PDF coordinates."""

    tokens = tuple(compact(label.replace(" ", "")) for label in labels)
    occurrences: list[dict[str, Any]] = []
    for page in document:
        if pages is not None and page.number + 1 not in pages:
            continue
        words = page.get_text("words")
        lines: dict[tuple[int, int], list[tuple[Any, ...]]] = defaultdict(list)
        for word in words:
            lines[(int(word[5]), int(word[6]))].append(word)
        for line_words in lines.values():
            ordered = sorted(line_words, key=lambda item: (float(item[0]), float(item[1])))
            line_text = compact("".join(str(item[4]) for item in ordered))
            token = next((value for value in tokens if value and value in line_text), None)
            if token is None:
                continue
            bbox = fitz.Rect(
                min(float(item[0]) for item in ordered),
                min(float(item[1]) for item in ordered),
                max(float(item[2]) for item in ordered),
                max(float(item[3]) for item in ordered),
            )
            # Preserve the whole native line as evidence. It is preferable to
            # a guessed value rectangle when labels and values use merged cells.
            occurrences.append(
                {
                    "pdf_page": page.number + 1,
                    "bbox": _rect(bbox),
                    "label": token,
                    "line_text": " ".join(str(item[4]) for item in ordered),
                    "value": line_text[line_text.find(token) + len(token):] if token else "",
                }
            )
    return occurrences


def _metadata_ledger(
    record_document: fitz.Document,
    report_document: fitz.Document,
) -> tuple[list[dict[str, Any]], list[CoverageEntry], list[str], list[str]]:
    ledger: list[dict[str, Any]] = []
    coverage: list[CoverageEntry] = []
    source_ids: list[str] = []
    target_ids: list[str] = []
    fallback_record = {"pdf_page": 1, "bbox": [1.0, 1.0, 2.0, 2.0]}
    fallback_report = {"pdf_page": 3, "bbox": [1.0, 1.0, 2.0, 2.0]}
    for field, labels, scope_id in METADATA_FIELDS:
        record_hits = _field_occurrences(record_document, labels, pages={1, 2, 3, 4})
        report_hits = _field_occurrences(report_document, labels, pages={3})
        source_id = f"record61:metadata:{field}"
        target_id = f"report:metadata:{field}"
        source_ids.append(source_id)
        target_ids.append(target_id)
        record_hit = record_hits[0] if record_hits else None
        report_hit = report_hits[0] if report_hits else None
        def resolved(value: Any) -> str:
            return re.sub(r"[\s:：,，;；.。/_\\-—]+", "", str(value or ""))

        record_value = resolved(record_hit.get("value") if record_hit else None)
        report_value = resolved(report_hit.get("value") if report_hit else None)
        if record_hit is None and report_hit is None:
            disposition = "not_applicable"
            reason = "metadata_field_not_present_in_approved_templates"
            decision = "not_applicable"
        elif record_hit is None or report_hit is None:
            disposition = "manual"
            reason = "metadata_field_only_present_on_one_side"
            decision = "manual"
        elif not record_value or not report_value:
            disposition = "manual"
            reason = "metadata_field_value_blank_or_merged_cell_unresolved"
            decision = "manual"
        elif _identity_normalize(record_value) == _identity_normalize(report_value):
            disposition = "matched"
            reason = "metadata_field_value_match"
            decision = "match"
        else:
            disposition = "mismatch"
            reason = "metadata_field_value_difference"
            decision = "mismatch"
        record_location = record_hit or fallback_record
        report_location = report_hit or fallback_report
        entry_id = f"RECORD61-METADATA-{field.upper()}"
        ledger.append(
            {
                "entry_id": entry_id,
                "id": entry_id,
                "rule_id": "RECORD61-METADATA",
                "scope_ids": [scope_id, "S31", "S33"],
                "source_row_id": source_id,
                "target_row_id": target_id,
                "source_row_ids": [source_id],
                "target_row_ids": [target_id],
                "disposition": disposition,
                "reason_code": reason,
                "record_location": record_location,
                "report_location": report_location,
                "record": {"field": field, "occurrences": record_hits, "value": record_value or None},
                "report": {"field": field, "occurrences": report_hits, "value": report_value or None},
                "comparison": {"decision": decision, "reason_code": reason},
            }
        )
        coverage.append(CoverageEntry(source_id, target_id, disposition, reason, (entry_id,) if disposition in {"manual", "mismatch"} else ()))
    return ledger, coverage, source_ids, target_ids


def _record_location(row: Mapping[str, Any]) -> dict[str, Any]:
    return {"pdf_page": int(row["pdf_page"]), "bbox": list(row["status_bbox"])}


def _clause_compatible(source_clause: str, report_clause: str) -> bool:
    if _clause_in_explicit_range(source_clause, report_clause):
        return True
    source = _clause_token(source_clause)
    target = _clause_token(report_clause)
    return bool(source and target) and (
        source == target
        or source.startswith(f"{target}.")
        or (target in {"12", "14"} and source.startswith(target))
    )


def _locator_text(value: str) -> str:
    normalized = (
        value.replace("（", "(")
        .replace("）", ")")
        .replace("％", "%")
        .replace("µ", "μ")
        .replace("Ω", "Ω")
    )
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fffμΩ%℃°]+", "", normalized).lower()


def _requirements_uniquely_correspond(source_text: str, report_text: str) -> bool:
    source = _locator_text(source_text)
    report = _locator_text(report_text)
    if not source or not report:
        return False
    if source == report:
        return True
    if min(len(source), len(report)) < 12:
        return False
    return source in report or report in source


_LEADING_CLAUSE_EXPRESSION_RE = re.compile(
    rf"^\s*(?P<start>{_CLAUSE_NUMBER})"
    rf"(?:\s*(?P<separator>~|～|〜|∼|至|–|—|-)\s*"
    rf"(?P<end>{_CLAUSE_NUMBER}))?"
    rf"(?=\s|[^\d.]|$)"
)


def _leading_clause_expression(value: str) -> str:
    match = _LEADING_CLAUSE_EXPRESSION_RE.match(display_text(value))
    if not match:
        return ""
    start = _clause_token(match.group("start"))
    end = match.group("end")
    return f"{start}-{_clause_token(end)}" if end else start


def _unique_candidate_pairs(
    source_indices: set[int],
    report_indices: set[int],
    predicate: Any,
) -> set[tuple[int, int]]:
    candidates = [
        (source_index, report_index)
        for source_index in source_indices
        for report_index in report_indices
        if predicate(source_index, report_index)
    ]
    source_counts = Counter(source for source, _ in candidates)
    report_counts = Counter(report for _, report in candidates)
    return {
        (source, report)
        for source, report in candidates
        if source_counts[source] == 1 and report_counts[report] == 1
    }


def _ordered_unique_containment(
    component_texts: Sequence[str],
    container_text: str,
    *,
    minimum_component_length: int = 12,
    minimum_coverage: float = 0.8,
) -> bool:
    if len(component_texts) < 2 or len(set(component_texts)) != len(component_texts):
        return False
    if not container_text or any(len(text) < minimum_component_length for text in component_texts):
        return False
    positions: list[tuple[int, int]] = []
    for text in component_texts:
        if container_text.count(text) != 1:
            return False
        start = container_text.index(text)
        positions.append((start, start + len(text)))
    if positions != sorted(positions) or any(
        positions[index][0] < positions[index - 1][1]
        for index in range(1, len(positions))
    ):
        return False
    return sum(len(text) for text in component_texts) / len(container_text) >= minimum_coverage


def _assign_record_rows_to_sequences(
    record_rows: list[dict[str, Any]],
    report_rows: Sequence[ReportRow],
) -> dict[int, list[dict[str, Any]]]:
    clauses = {row.sequence: row.clause_raw for row in report_rows if 1 <= row.sequence <= 117}
    if set(clauses) != set(range(1, 118)):
        missing = sorted(set(range(1, 118)) - set(clauses))
        raise ValueError(f"Report sequence 1-117 inventory is incomplete: {missing}")
    by_sequence: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for source in record_rows:
        candidates = [
            (len(_clause_token(clause)), sequence)
            for sequence, clause in clauses.items()
            if _clause_compatible(source["clause"], clause)
        ]
        if not candidates:
            raise ValueError(
                f"Record row {source['row_id']} clause {source['clause']!r} has no Report clause range"
            )
        longest = max(length for length, _ in candidates)
        winners = [sequence for length, sequence in candidates if length == longest]
        if len(winners) != 1:
            raise ValueError(
                f"Record row {source['row_id']} clause range is ambiguous: {winners}"
            )
        source["report_sequence"] = winners[0]
        by_sequence[winners[0]].append(source)
    return by_sequence


def _mapping_edges(
    source_rows: Sequence[dict[str, Any]],
    report_rows: Sequence[ReportRow],
) -> list[tuple[dict[str, Any] | None, ReportRow | None, bool, str]]:
    """Return unique matches plus explicit one-sided unresolved rows.

    Coverage conservation must not manufacture a counterpart.  A source or
    Report row that has no unique textual match is therefore represented as a
    one-sided manual edge; the caller may attach the opposite sequence as
    context evidence without assigning its row ID as a comparison target.
    """

    remaining_source = set(range(len(source_rows)))
    remaining_report = set(range(len(report_rows)))
    edges: list[tuple[int | None, int | None, bool, str]] = []

    text_pairs = _unique_candidate_pairs(
        remaining_source,
        remaining_report,
        lambda source, report: _requirements_uniquely_correspond(
            source_rows[source]["requirement"],
            report_rows[report].requirement_raw,
        ),
    )
    edges.extend(
        (source, report, True, "clause_range_and_unique_requirement_text")
        for source, report in text_pairs
    )
    remaining_source -= {source for source, _ in text_pairs}
    remaining_report -= {report for _, report in text_pairs}

    clause_pairs = _unique_candidate_pairs(
        remaining_source,
        remaining_report,
        lambda source, report: bool(
            expression := _leading_clause_expression(report_rows[report].requirement_raw)
        )
        and _clause_compatible(str(source_rows[source].get("clause") or ""), expression),
    )
    edges.extend(
        (source, report, True, "clause_range_and_unique_explicit_clause")
        for source, report in clause_pairs
    )
    remaining_source -= {source for source, _ in clause_pairs}
    remaining_report -= {report for _, report in clause_pairs}

    source_texts = [_locator_text(str(row.get("requirement") or "")) for row in source_rows]
    report_texts = [_locator_text(row.requirement_raw) for row in report_rows]

    many_to_one_candidates: dict[int, list[int]] = {}
    for report in sorted(remaining_report):
        members = [
            source
            for source in sorted(remaining_source)
            if source_texts[source] and source_texts[source] in report_texts[report]
        ]
        clauses = {_clause_token(str(source_rows[source].get("clause") or "")) for source in members}
        resolved_statuses = {
            source_rows[source].get("status")
            for source in members
            if source_rows[source].get("status") is not None
        }
        if (
            len(clauses) == 1
            and "" not in clauses
            and len(resolved_statuses) <= 1
            and _ordered_unique_containment(
                [source_texts[source] for source in members],
                report_texts[report],
            )
        ):
            many_to_one_candidates[report] = members
    source_group_counts = Counter(
        source
        for members in many_to_one_candidates.values()
        for source in members
    )
    for report, members in sorted(many_to_one_candidates.items()):
        if any(source_group_counts[source] != 1 for source in members):
            continue
        edges.extend(
            (
                source,
                report,
                True,
                "clause_range_and_unique_many_record_rows_to_one_report_row",
            )
            for source in members
        )
        remaining_source -= set(members)
        remaining_report.discard(report)

    one_to_many_candidates: dict[int, list[int]] = {}
    for source in sorted(remaining_source):
        members = [
            report
            for report in sorted(remaining_report)
            if report_texts[report] and report_texts[report] in source_texts[source]
        ]
        if _ordered_unique_containment(
            [report_texts[report] for report in members],
            source_texts[source],
        ):
            one_to_many_candidates[source] = members
    report_group_counts = Counter(
        report
        for members in one_to_many_candidates.values()
        for report in members
    )
    for source, members in sorted(one_to_many_candidates.items()):
        if any(report_group_counts[report] != 1 for report in members):
            continue
        edges.extend(
            (
                source,
                report,
                True,
                "clause_range_and_unique_one_record_row_to_many_report_rows",
            )
            for report in members
        )
        remaining_source.discard(source)
        remaining_report -= set(members)

    edges.extend(
        (source, None, False, "record_requirement_not_uniquely_mapped")
        for source in sorted(remaining_source)
    )
    edges.extend(
        (None, report, False, "report_requirement_not_uniquely_mapped")
        for report in sorted(remaining_report)
    )
    edges.sort(
        key=lambda edge: (
            edge[0] if edge[0] is not None else len(source_rows),
            edge[1] if edge[1] is not None else len(report_rows),
        )
    )
    result = [
        (
            source_rows[source] if source is not None else None,
            report_rows[target] if target is not None else None,
            automatic,
            method,
        )
        for source, target, automatic, method in edges
    ]
    if {row["row_id"] for row, _, _, _ in result if row is not None} != {
        row["row_id"] for row in source_rows
    }:
        raise ValueError("internal Record mapping conservation failure")
    if {row.row_id for _, row, _, _ in result if row is not None} != {
        row.row_id for row in report_rows
    }:
        raise ValueError("internal Report mapping conservation failure")
    return result


def _mapping_reason_code(
    source: Mapping[str, Any] | None,
    target: ReportRow | None,
    source_rows: Sequence[Mapping[str, Any]],
    report_rows: Sequence[ReportRow],
    method: str,
) -> str:
    """Classify an unresolved edge without changing the legacy edge method.

    ``_mapping_edges`` intentionally keeps its historical method names for
    callers that consume the algorithm directly.  The published ledger uses
    these more actionable S33 reason codes so a reviewer can distinguish a
    missing row, an extra row, and an ambiguous mapping.
    """

    if source is None and target is None:
        return "row_mapping_unresolved"
    if source is not None and target is not None:
        return method
    if source is not None:
        source_clause = str(source.get("clause") or "")
        source_requirement = str(source.get("requirement") or "")
        candidates = [
            report
            for report in report_rows
            if _clause_compatible(source_clause, report.clause_raw)
            and (
                _requirements_uniquely_correspond(source_requirement, report.requirement_raw)
                or _clause_compatible(
                    source_clause,
                    _leading_clause_expression(report.requirement_raw),
                )
            )
        ]
        if not candidates:
            return "record_row_missing_in_report"
        if len(candidates) > 1:
            return "record_row_ambiguous_mapping"
        return "record_row_not_uniquely_mapped"

    target_clause = target.clause_raw if target is not None else ""
    target_requirement = target.requirement_raw if target is not None else ""
    candidates = [
        record
        for record in source_rows
        if _clause_compatible(str(record.get("clause") or ""), target_clause)
        and (
            _requirements_uniquely_correspond(
                str(record.get("requirement") or ""), target_requirement
            )
            or _clause_compatible(
                str(record.get("clause") or ""),
                _leading_clause_expression(target_requirement),
            )
        )
    ]
    if not candidates:
        return "report_row_extra_vs_record"
    if len(candidates) > 1:
        return "report_row_ambiguous_mapping"
    return "report_row_not_uniquely_mapped"


def _conclusion_check(
    source_rows: Sequence[Mapping[str, Any]],
    report_rows: Sequence[ReportRow],
) -> dict[str, Any]:
    statuses = [row.get("status") for row in source_rows]
    if "不符合" in statuses:
        # An explicit nonconforming source row already determines the group
        # conclusion, even if a different row still needs manual recognition.
        expected = "不符合"
    elif any(status is None for status in statuses):
        expected = None
    else:
        expected = expected_report_conclusion_from_record_statuses(statuses, mode="9706.1")
    report_results = [row.result_raw for row in report_rows]
    observed = expected_report_conclusion(report_results)
    if expected is None:
        decision = "manual"
        reason = "record_sequence_status_unresolved"
    elif observed == "<检验结果缺失>":
        decision = "manual"
        reason = "report_sequence_result_missing"
    elif observed == expected:
        decision = "match"
        reason = "sequence_result_matched"
    else:
        decision = "mismatch"
        reason = "sequence_result_mismatch"
    return {"decision": decision, "reason_code": reason, "expected": expected, "observed": observed}


_LEADING_SUBCLAUSE_RE = re.compile(r"^\s*(\d+(?:\.\d+)+)(?=\s|[^\d.]|$)")
_LEADING_REQUIREMENT_MARKER_RE = re.compile(
    r"^\s*(?:(\d+(?:\.\d+)+)|([A-Za-z])\s*[)）])"
)


def _leading_subclause(value: str) -> str:
    match = _LEADING_SUBCLAUSE_RE.match(display_text(value))
    return _clause_token(match.group(1)) if match else ""


def _leading_requirement_marker(value: str) -> str:
    match = _LEADING_REQUIREMENT_MARKER_RE.match(display_text(value))
    if not match:
        return ""
    return _clause_token(match.group(1)) if match.group(1) else f"{match.group(2).lower()})"


def _effective_report_result(
    source: Mapping[str, Any],
    target: ReportRow,
    sequence_rows: Sequence[ReportRow],
    *,
    stop_target_row_ids: set[str] | None = None,
) -> tuple[str, list[str], str]:
    """Resolve a parent row through its following child result rows.

    A Record parent requirement such as 8.6.4 a) can be marked conforming while
    the corresponding Report parent cell is ``——`` and the actual measurements
    are carried by its following child rows.  Only a target whose requirement
    explicitly opens the same subclause is expanded; a later ``b)`` row therefore
    keeps its own placeholder and cannot be masked by measurements for ``a)``.
    """

    observed = compact(target.result_raw)
    if observed not in {"——", "/"}:
        return target.result_raw, [target.row_id], "physical_report_result"
    source_clause = _clause_token(str(source.get("clause") or ""))
    if not source_clause or _leading_subclause(target.requirement_raw) != source_clause:
        return target.result_raw, [target.row_id], "physical_report_result"

    try:
        start = next(index for index, row in enumerate(sequence_rows) if row.row_id == target.row_id)
    except StopIteration:
        return target.result_raw, [target.row_id], "physical_report_result"
    group: list[ReportRow] = []
    stop_target_row_ids = stop_target_row_ids or set()
    for index in range(start, len(sequence_rows)):
        row = sequence_rows[index]
        if index > start and row.row_id in stop_target_row_ids:
            break
        opened = _leading_requirement_marker(row.requirement_raw)
        if index > start and opened and opened != source_clause:
            break
        group.append(row)
    meaningful = [
        row for row in group
        if compact(row.result_raw) in {"符合要求", "不符合要求"}
        or is_actual_report_result(row.result_raw)
    ]
    if not meaningful:
        return target.result_raw, [target.row_id], "physical_report_result"
    if any(compact(row.result_raw) == "不符合要求" for row in meaningful):
        effective = "不符合要求"
    elif any(compact(row.result_raw) == "符合要求" for row in meaningful):
        effective = "符合要求"
    else:
        effective = meaningful[0].result_raw
    return effective, [row.row_id for row in meaningful], "parent_result_aggregated_from_child_rows"


def _effective_report_group_result(
    rows: Sequence[ReportRow],
) -> tuple[str | None, list[str], str]:
    """Aggregate a structurally split Report requirement without using Record values."""

    observed = [compact(row.result_raw) for row in rows]
    row_ids = [row.row_id for row in rows]
    if any(value == "不符合要求" for value in observed):
        return "不符合要求", row_ids, "split_report_rows_aggregated"
    if any(not value for value in observed):
        return None, row_ids, "split_report_rows_unresolved"
    if any(value == "符合要求" or is_actual_report_result(value) for value in observed):
        return "符合要求", row_ids, "split_report_rows_aggregated"
    unique = set(observed)
    if len(unique) == 1:
        return rows[0].result_raw, row_ids, "split_report_rows_aggregated"
    return None, row_ids, "split_report_rows_unresolved"


def _status_ledger(
    record_rows: list[dict[str, Any]],
    report_rows: Sequence[ReportRow],
    *,
    excluded_target_row_ids: set[str] | None = None,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[CoverageEntry],
    list[str],
    list[str],
]:
    source_by_sequence = _assign_record_rows_to_sequences(record_rows, report_rows)
    excluded_target_row_ids = excluded_target_row_ids or set()
    all_report_by_sequence: dict[int, list[ReportRow]] = defaultdict(list)
    report_by_sequence: dict[int, list[ReportRow]] = defaultdict(list)
    for row in report_rows:
        if 1 <= row.sequence <= 117:
            all_report_by_sequence[row.sequence].append(row)
            if row.row_id not in excluded_target_row_ids:
                report_by_sequence[row.sequence].append(row)

    ledger: list[dict[str, Any]] = []
    conclusion_ledger: list[dict[str, Any]] = []
    coverage_entries: list[CoverageEntry] = []
    conclusion_source_ids: list[str] = []
    conclusion_target_ids: list[str] = []
    for sequence in range(1, 118):
        sources = source_by_sequence[sequence]
        targets = report_by_sequence[sequence]
        context_targets = all_report_by_sequence[sequence]
        if not sources or not context_targets:
            raise ValueError(
                f"sequence {sequence}: empty mapping side source={len(sources)} "
                f"target={len(context_targets)}"
            )
        edges = _mapping_edges(sources, targets)
        one_to_many_targets: dict[str, list[ReportRow]] = defaultdict(list)
        for source, target, automatic, method in edges:
            if (
                automatic
                and source is not None
                and target is not None
                and method == "clause_range_and_unique_one_record_row_to_many_report_rows"
            ):
                one_to_many_targets[source["row_id"]].append(target)
        automatic_target_owners = {
            target.row_id: source["row_id"]
            for source, target, automatic, _ in edges
            if automatic and source is not None and target is not None
        }
        for edge_ordinal, (source, target, automatic, method) in enumerate(edges, start=1):
            source_context = source if source is not None else sources[0]
            target_context = target if target is not None else (targets or context_targets)[0]
            comparison: dict[str, Any]
            if not automatic:
                disposition = "manual"
                reason_code = _mapping_reason_code(
                    source,
                    target,
                    sources,
                    targets or context_targets,
                    method,
                )
                comparison = {
                    "decision": "manual",
                    "reason_code": reason_code,
                    "record_status": source.get("status") if source is not None else None,
                    "report_result": target.result_raw if target is not None else None,
                    "unpaired_side": "record" if source is None else "report",
                    "mapping_method": method,
                }
            elif source is None or target is None:
                raise ValueError("automatic status mapping cannot be one-sided")
            elif source["status"] is None:
                disposition = "manual"
                reason_code = source["status_reason_code"]
                comparison = {
                    "decision": "manual",
                    "reason_code": reason_code,
                    "record_status": None,
                    "report_result": target.result_raw,
                }
            else:
                if method == "clause_range_and_unique_one_record_row_to_many_report_rows":
                    effective_result, contributing_targets, result_method = (
                        _effective_report_group_result(one_to_many_targets[source["row_id"]])
                    )
                else:
                    effective_result, contributing_targets, result_method = _effective_report_result(
                        source,
                        target,
                        context_targets,
                        stop_target_row_ids={
                            target_row_id
                            for target_row_id, source_row_id in automatic_target_owners.items()
                            if source_row_id != source["row_id"]
                        },
                    )
                if effective_result is None:
                    disposition = "manual"
                    reason_code = "split_report_rows_result_unresolved"
                    comparison = {
                        "decision": "manual",
                        "reason_code": reason_code,
                        "record_status": source["status"],
                        "report_result": None,
                        "contributing_target_row_ids": contributing_targets,
                        "result_aggregation": result_method,
                    }
                else:
                    result = compare_record61_status_result(source["status"], effective_result)
                    comparison = result.to_dict()
                    comparison.update(
                        {
                            "report_result_physical": target.result_raw,
                            "report_result_effective": effective_result,
                            "result_aggregation": result_method,
                            "contributing_target_row_ids": contributing_targets,
                        }
                    )
                    disposition = {
                        "match": "matched",
                        "mismatch": "mismatch",
                        "manual": "manual",
                        "not_applicable": "not_applicable",
                        "excluded": "excluded",
                    }[result.decision]
                    reason_code = result.reason_code

            entry_id = f"RECORD61-BODY-S{sequence:03d}-E{edge_ordinal:03d}"
            finding_ids = (entry_id,) if disposition in {"manual", "mismatch"} else ()
            source_row_id = source["row_id"] if source is not None else None
            target_row_id = target.row_id if target is not None else None
            record_evidence = (
                [_record_location(source)]
                if source is not None
                else [_record_location(row) for row in sources]
            )
            report_evidence = (
                [_report_location(target)]
                if target is not None
                else [_report_location(row) for row in (targets or context_targets)]
            )
            record_payload = dict(source_context)
            record_payload["mapping_role"] = (
                "comparison_source" if source is not None else "sequence_context"
            )
            report_payload = _report_row_payload(target_context)
            report_payload["mapping_role"] = (
                "comparison_target" if target is not None else "sequence_context"
            )
            comparison["field_comparisons"] = _paired_field_comparisons(
                record_payload,
                report_payload,
                mapped=bool(automatic and source is not None and target is not None),
                result_comparison=comparison,
            )
            ledger.append(
                {
                    "entry_id": entry_id,
                    "id": entry_id,
                    "rule_id": "RECORD61-BODY-STATUS",
                    "scope_ids": ["S25", "S26", "S30", "S31", "S32", "S33"],
                    "source_row_id": source_row_id,
                    "target_row_id": target_row_id,
                    "source_row_ids": [source_row_id] if source_row_id is not None else [],
                    "target_row_ids": [target_row_id] if target_row_id is not None else [],
                    "disposition": disposition,
                    "reason_code": reason_code,
                    "record_location": _record_location(source_context),
                    "report_location": _report_location(target_context),
                    "record_evidence": record_evidence,
                    "report_evidence": report_evidence,
                    "record": record_payload,
                    "report": report_payload,
                    "mapping": {
                        "automatic": automatic,
                        "method": method,
                        "resolution": (
                            "matched"
                            if automatic
                            else "record_row_missing"
                            if source is None
                            else "report_row_missing"
                            if target is None
                            else "unresolved"
                        ),
                        "sequence": sequence,
                        "source_binding": (
                            "exact_row" if source is not None else "sequence_context_only"
                        ),
                        "target_binding": (
                            "exact_row" if target is not None else "sequence_context_only"
                        ),
                    },
                    "comparison": comparison,
                    "nonconforming_alert": bool(
                        source is not None and source.get("status") == "不符合"
                    ),
                }
            )
            coverage_entries.append(
                CoverageEntry(
                    source_row_id,
                    target_row_id,
                    disposition,
                    reason_code,
                    finding_ids,
                )
            )

        conclusion = _conclusion_check(sources, context_targets)
        conclusion_disposition = {
            "match": "matched",
            "mismatch": "mismatch",
            "manual": "manual",
        }[conclusion["decision"]]
        source_id = f"record61:sequence-conclusion:s{sequence:03d}"
        target_id = f"report:sequence-conclusion:s{sequence:03d}"
        conclusion_source_ids.append(source_id)
        conclusion_target_ids.append(target_id)
        result_rows = [row for row in context_targets if compact(row.result_raw)]
        report_evidence_row = result_rows[0] if result_rows else context_targets[0]
        if conclusion["decision"] == "manual":
            record_evidence_row = next(
                (row for row in sources if row.get("status") is None),
                sources[0],
            )
        elif conclusion.get("expected") == "不符合":
            record_evidence_row = next(
                (row for row in sources if row.get("status") == "不符合"),
                sources[0],
            )
        else:
            record_evidence_row = sources[0]
        entry_id = f"RECORD61-CONCLUSION-S{sequence:03d}"
        finding_ids = (entry_id,) if conclusion_disposition in {"manual", "mismatch"} else ()
        conclusion_ledger.append(
            {
                "entry_id": entry_id,
                "id": entry_id,
                "rule_id": "RECORD61-SEQUENCE-CONCLUSION",
                "scope_ids": ["S25", "S33"],
                "source_row_id": source_id,
                "target_row_id": target_id,
                "source_row_ids": [source_id],
                "target_row_ids": [target_id],
                "disposition": conclusion_disposition,
                "reason_code": conclusion["reason_code"],
                "record_location": _record_location(record_evidence_row),
                "report_location": _report_location(report_evidence_row),
                "record_evidence": [_record_location(row) for row in sources],
                "report_evidence": [
                    _report_location(row)
                    for row in (result_rows or [report_evidence_row])
                ],
                "record": {
                    "sequence": sequence,
                    "statuses": [row.get("status") for row in sources],
                    "derived_from_source_row_ids": [row["row_id"] for row in sources],
                },
                "report": {
                    "sequence": sequence,
                    "result": report_evidence_row.result_raw,
                    "conclusion": report_evidence_row.conclusion_raw,
                    "participating_report_row_ids": [row.row_id for row in context_targets],
                },
                "comparison": conclusion,
            }
        )
        coverage_entries.append(
            CoverageEntry(
                source_id,
                target_id,
                conclusion_disposition,
                conclusion["reason_code"],
                finding_ids,
            )
        )
    return (
        ledger,
        conclusion_ledger,
        coverage_entries,
        conclusion_source_ids,
        conclusion_target_ids,
    )


def _printed_page_number(page: fitz.Page) -> int | None:
    matches = re.findall(r"第\s*(\d+)\s*页", page.get_text("text"))
    return int(matches[-1]) if matches else None


def _table_contains(table: fitz.table.Table, token: str) -> bool:
    text = compact(" ".join(display_text(cell) for row in table.extract() for cell in row))
    return compact(token) in text


def _page_identity_map(document: fitz.Document) -> dict[int, tuple[int | None, int]]:
    occurrence: Counter[int] = Counter()
    page_identity: dict[int, tuple[int | None, int]] = {}
    for page in document:
        printed = _printed_page_number(page)
        if printed is None:
            page_identity[page.number + 1] = (None, 1)
            continue
        occurrence[printed] += 1
        page_identity[page.number + 1] = (printed, occurrence[printed])
    return page_identity


def _cell_ink_analysis(
    page: fitz.Page,
    bbox: Sequence[float],
    page_inks: Sequence[Mapping[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], bool, list[list[float]]]:
    rect = fitz.Rect(bbox)
    expanded = fitz.Rect(rect.x0 - 1.5, rect.y0 - 1.5, rect.x1 + 1.5, rect.y1 + 1.5)
    selected: list[dict[str, Any]] = []
    cancellation_bboxes: list[list[float]] = []
    for ink in page_inks if page_inks is not None else _page_inks(page):
        ink_bbox = fitz.Rect(ink["bbox"])
        points = list(ink.get("_points") or [])
        local_points = [
            fitz.Point(float(x), float(y))
            for x, y in points
            if expanded.contains(fitz.Point(float(x), float(y)))
        ]
        local_bbox = (
            fitz.Rect(local_points[0], local_points[0])
            if local_points
            else fitz.Rect()
        )
        for point in local_points[1:]:
            local_bbox.include_point(point)
        horizontal_cancellation = (
            not local_bbox.is_empty
            and local_bbox.width >= rect.width * 0.70
            and local_bbox.height <= max(5.0, rect.height * 0.25)
        )
        cross_cell_cancellation = (
            not local_bbox.is_empty
            and local_bbox.width >= rect.width * 0.70
            and ink_bbox.width > rect.width * 1.15
        )
        if horizontal_cancellation or cross_cell_cancellation:
            cancellation_bboxes.append(_rect(ink_bbox))
            continue
        if not rect.contains(fitz.Point(float(ink["centroid_x"]), float(ink["centroid_y"]))):
            continue
        if points:
            inside_ratio = sum(
                expanded.contains(fitz.Point(float(x), float(y))) for x, y in points
            ) / len(points)
            if inside_ratio < 0.70:
                continue
        if ink_bbox.width > rect.width * 1.15 or ink_bbox.height > rect.height * 1.35:
            continue
        if ink_bbox.height <= 4 and ink_bbox.width >= rect.width * 0.70:
            continue
        selected.append(ink)
    return selected, bool(cancellation_bboxes), cancellation_bboxes


def _cell_inks(
    page: fitz.Page,
    bbox: Sequence[float],
    page_inks: Sequence[Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    selected, void_or_crossed_out, _ = _cell_ink_analysis(page, bbox, page_inks)
    return [] if void_or_crossed_out else selected


def _measurement_cell(
    page: fitz.Page,
    table: fitz.table.Table,
    *,
    table_index: int,
    row_index: int,
    cell_index: int,
    block: str,
    page_identity: Mapping[int, tuple[int | None, int]],
    unit: str,
    semantic: Mapping[str, Any] | None = None,
    label_bbox: Sequence[float] | None = None,
    page_inks: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    cell = table.rows[row_index].cells[cell_index]
    if cell is None:
        raise ValueError(
            f"Record p{page.number + 1} t{table_index + 1} r{row_index} c{cell_index} has no cell"
        )
    physical_page = page.number + 1
    printed_page, occurrence_index = page_identity[physical_page]
    row_id = (
        f"record61:measure:p{physical_page:03d}:printed{printed_page or 0:03d}:"
        f"occ{occurrence_index}:t{table_index + 1:02d}:r{row_index:03d}:"
        f"c{cell_index:03d}:{block}"
    )
    inks, void_or_crossed_out, cancellation_bboxes = _cell_ink_analysis(
        page,
        cell,
        page_inks,
    )
    return {
        "row_id": row_id,
        "source_key": row_id,
        "block_type": block,
        "physical_page": physical_page,
        "printed_page": printed_page,
        "occurrence_index": occurrence_index,
        "table_index": table_index,
        "row_index": row_index,
        "cell_index": cell_index,
        "bbox": _rect(cell),
        "label_bbox": _rect(label_bbox) if label_bbox is not None else None,
        "unit": unit,
        "semantic": dict(semantic or {}),
        "ink_bboxes": [_rect(ink["bbox"]) for ink in inks],
        "ink_count": len(inks),
        "void_or_crossed_out": void_or_crossed_out,
        "cancellation_bboxes": cancellation_bboxes,
        "recognition_channels": {
            "apple_vision": {"status": "not_run", "candidates": []},
            "tesseract": {"status": "not_run", "candidates": []},
        },
        "accepted_value": None,
        "accepted_values": None,
    }


_OCR_NUMERIC_TOKEN_RE = re.compile(
    r"(?<![\d.])(?P<comparator><=|>=|[<>≤≥＜＞])?\s*"
    r"(?P<number>[+\-−]?\d+(?:\.\d+)?)(?![\d.])"
)


def _ocr_numeric_values(items: Sequence[Mapping[str, Any]]) -> set[str]:
    values: set[str] = set()
    for item in items:
        text = display_text(item.get("text"))
        matches = list(_OCR_NUMERIC_TOKEN_RE.finditer(text))
        if len(matches) != 1:
            continue
        comparator = {
            "＜": "<",
            "＞": ">",
            "≤": "<=",
            "≥": ">=",
        }.get(matches[0].group("comparator") or "", matches[0].group("comparator") or "")
        number = matches[0].group("number").replace("−", "-")
        values.add(f"{comparator}{number}")
    return values


def _accepted_dual_channel_value(candidates: Mapping[str, Any]) -> str | None:
    """Return one value only when Apple Vision and Tesseract agree exactly."""

    strict = accepted_numeric_candidate(dict(candidates))
    if strict is not None:
        return strict
    vision = _ocr_numeric_values(candidates.get("apple_vision", []))
    tesseract = _ocr_numeric_values(candidates.get("tesseract", []))
    agreement = vision & tesseract
    return next(iter(agreement)) if len(agreement) == 1 else None


_OCR_PERCENTAGE_RE = re.compile(
    r"(?<![\d.])(?P<number>[+＋\-－−]?\d+(?:\.\d+)?)\s*[%％]"
)


def _normalized_ocr_percentage(token: str) -> str:
    return f"{token.replace('＋', '+').replace('－', '-').replace('−', '-')}%"


def _percentage_sequences(
    items: Sequence[Mapping[str, Any]],
    *,
    include_combined: bool,
) -> set[tuple[str, ...]]:
    sequences: set[tuple[str, ...]] = set()
    texts = [display_text(item.get("text")) for item in items if display_text(item.get("text"))]
    for text in texts:
        values = tuple(
            _normalized_ocr_percentage(match.group("number"))
            for match in _OCR_PERCENTAGE_RE.finditer(text)
        )
        if values:
            sequences.add(values)
    if include_combined and len(texts) > 1:
        values = tuple(
            _normalized_ocr_percentage(match.group("number"))
            for text in texts
            for match in _OCR_PERCENTAGE_RE.finditer(text)
        )
        if values:
            sequences.add(values)
    return sequences


def _accepted_dual_channel_values(
    candidates: Mapping[str, Any],
    *,
    expected_count: int,
) -> list[str] | None:
    """Accept an ordered percentage list only when both OCR paths agree."""

    if expected_count < 1:
        return None
    vision = _percentage_sequences(
        candidates.get("apple_vision", []),
        include_combined=True,
    )
    # Tesseract entries are alternative PSM passes over the same full crop;
    # do not concatenate those alternatives.
    tesseract = _percentage_sequences(
        candidates.get("tesseract", []),
        include_combined=False,
    )
    agreement = {
        values
        for values in vision & tesseract
        if len(values) == expected_count
    }
    return list(next(iter(agreement))) if len(agreement) == 1 else None


def _recognize_measurement_cell(
    document: fitz.Document,
    cell: dict[str, Any],
    output_dir: Path | None,
) -> None:
    if output_dir is None or cell.get("void_or_crossed_out"):
        return
    page = document[int(cell["physical_page"]) - 1]
    safe_name = re.sub(r"[^0-9A-Za-z._-]+", "_", str(cell["row_id"]))
    image_path = output_dir / "record61-ocr" / f"{safe_name}.png"
    render_cell_for_ocr(page, fitz.Rect(cell["bbox"]), image_path)
    candidates = local_ocr_candidates(image_path)
    accepted_values = None
    if cell.get("block_type") == "4.11":
        accepted_values = _accepted_dual_channel_values(
            candidates,
            expected_count=int(cell.get("expected_value_count") or 0),
        )
        accepted = accepted_values[0] if accepted_values and len(accepted_values) == 1 else None
    else:
        accepted = _accepted_dual_channel_value(candidates)
    cell["recognition_channels"] = {
        "apple_vision": {
            "status": "available" if "apple_vision_error" not in candidates else "unavailable",
            "candidates": list(candidates.get("apple_vision", [])),
            "error": candidates.get("apple_vision_error"),
        },
        "tesseract": {
            "status": "available" if candidates.get("tesseract") else "no_candidate",
            "candidates": list(candidates.get("tesseract", [])),
        },
    }
    cell["accepted_value"] = accepted
    cell["accepted_values"] = accepted_values
    cell["ocr_image"] = str(image_path)


def _find_tables_with_token(
    document: fitz.Document,
    token: str,
    *,
    required_header: str | None = None,
) -> list[tuple[fitz.Page, int, fitz.table.Table, list[list[Any]]]]:
    matches: list[tuple[fitz.Page, int, fitz.table.Table, list[list[Any]]]] = []
    for page in document:
        for table_index, table in enumerate(page.find_tables().tables):
            extracted = table.extract()
            table_text = compact(" ".join(display_text(cell) for row in extracted for cell in row))
            if compact(token) not in table_text:
                continue
            if required_header and compact(required_header) not in table_text:
                continue
            matches.append((page, table_index, table, extracted))
    return matches


def _header_column(extracted: Sequence[Sequence[Any]], token: str) -> int:
    compact_token = compact(token)
    for row in extracted[:4]:
        for index, value in enumerate(row):
            if compact_token in compact(display_text(value)):
                return index
    raise ValueError(f"Record numeric table lacks header {token!r}")


def _body_numeric_cells(
    document: fitz.Document,
    page_identity: Mapping[int, tuple[int | None, int]],
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)

    # 4.11: the final written percentage is in the body observation cell on p9,
    # not in the power table on printed p97.
    page = document[8]
    tables = page.find_tables().tables
    table_index, table = next(
        (index, item) for index, item in enumerate(tables)
        if _table_contains(item, "4.11") and item.row_count > 9
    )
    result["4.11"].append(
        _measurement_cell(
            page,
            table,
            table_index=table_index,
            row_index=9,
            cell_index=2,
            block="4.11",
            page_identity=page_identity,
            unit="%",
            semantic={"policy": "copy_final_written_percentage", "clause": "4.11"},
        )
    )

    # 9.6 values are written directly in the two body observation cells on p61.
    page = document[60]
    tables = page.find_tables().tables
    table_index, table = next(
        (index, item) for index, item in enumerate(tables)
        if _table_contains(item, "9.6.2.1") and item.row_count > 14
    )
    for row_index, unit, sound_kind in ((13, "dB(A)", "audible"), (14, "dB(C)", "impulse")):
        result["9.6"].append(
            _measurement_cell(
                page,
                table,
                table_index=table_index,
                row_index=row_index,
                cell_index=2,
                block="9.6",
                page_identity=page_identity,
                unit=unit,
                semantic={"sound_kind": sound_kind},
            )
        )
    return result


def _append_86_cells(
    document: fitz.Document,
    page_identity: Mapping[int, tuple[int | None, int]],
    result: dict[str, list[dict[str, Any]]],
    output_dir: Path | None = None,
) -> None:
    matches: list[tuple[fitz.Page, int, fitz.table.Table, list[list[Any]]]] = []
    for page in document:
        if page_identity[page.number + 1][0] != 104:
            continue
        for table_index, table in enumerate(page.find_tables().tables):
            extracted = table.extract()
            if _table_contains(table, "8.6.4") and _table_contains(table, "计算阻抗"):
                matches.append((page, table_index, table, extracted))
    if len(matches) != 1:
        raise ValueError(f"8.6.4 measurement table is not unique ({len(matches)})")
    page, table_index, table, extracted = matches[0]
    value_column = _header_column(extracted, "计算阻抗")
    label_column = _header_column(extracted, "阻抗测量部位")
    row_indexes = (2, 3)
    temporary_dir = tempfile.TemporaryDirectory(prefix="record61-86-label-") if output_dir is None else None
    label_root = Path(output_dir) / "record61-ocr" if output_dir is not None else Path(temporary_dir.name)
    positions: dict[int, str | None] = {}
    for row_index in row_indexes:
        label_cell = table.rows[row_index].cells[0] if table.rows[row_index].cells else None
        if label_cell is None:
            positions[row_index] = None
            continue
        image_path = label_root / f"record61_86_position_{row_index}.png"
        render_cell_for_ocr(page, fitz.Rect(label_cell), image_path)
        texts = [str(item.get("text") or "") for item in local_text_ocr(image_path).get("lines", [])]
        joined = compact(" ".join(texts)).lower()
        has_inlet = bool(re.search(r"inlet|unlet|iulet|inet|wetp|zwetp", joined))
        has_plug = bool(re.search(r"plug|plea|ple|peg|aeg", joined))
        positions[row_index] = "inlet_pe" if has_inlet and not has_plug else "plug_pe" if has_plug and not has_inlet else None
    recognized = {value for value in positions.values() if value}
    if recognized == {"inlet_pe"} and list(positions.values()).count("inlet_pe") == 1:
        for row_index in row_indexes:
            if positions[row_index] is None:
                positions[row_index] = "plug_pe"
    elif recognized == {"plug_pe"} and list(positions.values()).count("plug_pe") == 1:
        for row_index in row_indexes:
            if positions[row_index] is None:
                positions[row_index] = "inlet_pe"
    if {value for value in positions.values()} != {"inlet_pe", "plug_pe"}:
        positions = {row_index: None for row_index in row_indexes}
    for row_index in row_indexes:
        position = positions[row_index]
        result["8.6"].append(
            _measurement_cell(
                page,
                table,
                table_index=table_index,
                row_index=row_index,
                cell_index=value_column,
                block="8.6",
                page_identity=page_identity,
                unit="mΩ",
                semantic={
                    "measurement_position": position or "unresolved",
                    "measurement_position_unresolved": position is None,
                },
                label_bbox=table.rows[row_index].cells[label_column],
            )
        )
    if temporary_dir is not None:
        temporary_dir.cleanup()


def _append_166_cells(
    document: fitz.Document,
    page_identity: Mapping[int, tuple[int | None, int]],
    result: dict[str, list[dict[str, Any]]],
) -> None:
    matches: list[tuple[fitz.Page, int, fitz.table.Table, list[list[Any]]]] = []
    for page in document:
        if page_identity[page.number + 1][0] != 141:
            continue
        for table_index, table in enumerate(page.find_tables().tables):
            extracted = table.extract()
            if _table_contains(table, "16.6.1") and _table_contains(table, "正常条件下接触电流"):
                matches.append((page, table_index, table, extracted))
    if len(matches) != 1:
        raise ValueError(f"16.6.1 measurement table is not unique ({len(matches)})")
    page, table_index, table, extracted = matches[0]
    normal_column = _header_column(extracted, "正常条件下接触电流测量值")
    pe_column = _header_column(extracted, "保护接地中断接触电流值测量值")
    for condition, rows, column in (
        ("normal", range(2, 5), normal_column),
        ("protective_earth_interrupted", range(2, 5), pe_column),
        ("multiple_socket_outlet", range(5, 7), normal_column),
    ):
        for row_index in rows:
            result["16.6"].append(
                _measurement_cell(
                    page,
                    table,
                    table_index=table_index,
                    row_index=row_index,
                    cell_index=column,
                    block="16.6",
                    page_identity=page_identity,
                    unit="uA",
                    semantic={"condition": condition, "aggregation": "maximum_applicable_value"},
                )
            )


def _header_text_for_x(
    table: fitz.table.Table,
    extracted: Sequence[Sequence[Any]],
    row_index: int,
    x: float,
) -> str:
    if row_index < 0 or row_index >= len(table.rows):
        return ""
    for column, cell in enumerate(table.rows[row_index].cells):
        if cell is None:
            continue
        rect = fitz.Rect(cell)
        if rect.x0 - 0.1 <= x <= rect.x1 + 0.1:
            return display_text(extracted[row_index][column])
    return ""


def _first_row_containing(extracted: Sequence[Sequence[Any]], *tokens: str) -> int | None:
    best: tuple[int, int] | None = None
    for row_index, row in enumerate(extracted):
        score = sum(
            any(compact(token) in compact(display_text(value)) for token in tokens)
            for value in row
            if display_text(value)
        )
        if score and (best is None or score > best[0]):
            best = (score, row_index)
    return best[1] if best else None


def _append_87_cells(
    document: fitz.Document,
    page_identity: Mapping[int, tuple[int | None, int]],
    result: dict[str, list[dict[str, Any]]],
) -> None:
    page_metric = {
        105: "earth",
        106: "touch",
        107: "patient",
        108: "patient_applied",
        109: "patient_signal",
        110: "patient_metal",
        111: "auxiliary",
        112: "total",
    }
    for page in document:
        physical_page = page.number + 1
        printed_page, _ = page_identity[physical_page]
        if printed_page not in set(range(105, 115)):
            continue
        page_inks = _page_inks(page)
        for table_index, table in enumerate(page.find_tables().tables):
            extracted = table.extract()
            table_text = compact(" ".join(display_text(value) for row in extracted for value in row))
            measurement_table = any(
                token in table_text
                for token in ("漏电流", "接触电流", "患者辅助电流")
            )
            applied_part_table = (
                printed_page == 108
                and "应用部分患者连接上的外来电压" in table_text
                and "S13" in table_text
            )
            if not measurement_table and not applied_part_table:
                continue

            if printed_page == 105:
                # The two unweighted values sit in the labelled body row above
                # the Figure 13 switch matrix.
                for cell_index, phase in ((3, "before"), (5, "after")):
                    if cell_index < len(table.rows[2].cells) and table.rows[2].cells[cell_index] is not None:
                        result["8.7"].append(
                            _measurement_cell(
                                page,
                                table,
                                table_index=table_index,
                                row_index=2,
                                cell_index=cell_index,
                                block="8.7",
                                page_identity=page_identity,
                                unit="mA",
                                semantic={"metric": "unweighted", "phase": phase},
                                page_inks=page_inks,
                            )
                        )

            state_row = _first_row_containing(extracted, "NC", "SFC")
            phase_row = _first_row_containing(extracted, "前1）", "后2）")
            current_row = _first_row_containing(extracted, "d.c.", "a.c.")
            header_rows = [index for index in (state_row, phase_row, current_row) if index is not None]
            if not header_rows:
                continue
            data_start = max(header_rows) + 1
            for row_index in range(data_start, table.row_count):
                row_values = extracted[row_index]
                switches = [compact(display_text(value)) for value in row_values]
                if sum(value in {"0", "1"} for value in switches) < 2:
                    continue
                for cell_index, cell in enumerate(table.rows[row_index].cells):
                    if cell is None:
                        continue
                    inks, void_or_crossed_out, _ = _cell_ink_analysis(
                        page,
                        cell,
                        page_inks,
                    )
                    if not inks and not void_or_crossed_out:
                        continue
                    rect = fitz.Rect(cell)
                    center_x = (rect.x0 + rect.x1) / 2
                    state_text = _header_text_for_x(table, extracted, state_row or -1, center_x)
                    phase_text = _header_text_for_x(table, extracted, phase_row or -1, center_x)
                    current_text = _header_text_for_x(table, extracted, current_row or -1, center_x)
                    state = "NC" if "NC" in state_text else "SFC" if "SFC" in state_text else None
                    phase = "before" if "前" in phase_text else "after" if "后" in phase_text else None
                    current = "dc" if "d.c" in current_text.lower() else "ac" if "a.c" in current_text.lower() else None

                    metric = page_metric.get(printed_page, "")
                    if printed_page == 113:
                        metric = "total_applied" if center_x < table.bbox[0] + table.bbox[2] * 0.48 else "functional"
                    elif printed_page == 114:
                        metric = "total_signal" if center_x < table.bbox[0] + table.bbox[2] * 0.52 else "total_metal"
                    if printed_page in {108, 113} and metric.endswith("applied"):
                        state = "SFC"
                    if printed_page in {110, 114} and metric.endswith("metal"):
                        state = None
                    result["8.7"].append(
                        _measurement_cell(
                            page,
                            table,
                            table_index=table_index,
                            row_index=row_index,
                            cell_index=cell_index,
                            block="8.7",
                            page_identity=page_identity,
                            unit="uA",
                            semantic={
                                "metric": metric,
                                "phase": phase,
                                "state": state,
                                "current": current,
                                "aggregation": "maximum_applicable_value",
                            },
                            page_inks=page_inks,
                        )
                    )


def _numeric_record_candidates(document: fitz.Document, output_dir: Path | None = None) -> dict[str, list[dict[str, Any]]]:
    page_identity = _page_identity_map(document)
    result = _body_numeric_cells(document, page_identity)
    _append_86_cells(document, page_identity, result, output_dir)
    _append_87_cells(document, page_identity, result)
    _append_166_cells(document, page_identity, result)
    return {key: value for key, value in result.items()}


def _numeric_report_targets(report_rows: Sequence[ReportRow]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {
        key: [] for key in ("4.11", "8.6", "8.7", "9.6", "16.6")
    }
    wet_phase: str | None = None
    for row in report_rows:
        clause = _clause_token(row.clause_raw)
        if clause == "8.7":
            joined = compact(" ".join((*row.requirement_parts, row.requirement_raw)))
            if "潮湿预处理前" in joined:
                wet_phase = "before"
            elif "潮纯预处理后" in joined or "潮湿预处理后" in joined:
                wet_phase = "after"
        if clause not in result or not is_actual_report_result(row.result_raw):
            continue
        result[clause].append(
            {
                "row": row,
                "wet_phase": wet_phase if clause == "8.7" else None,
                "requirement_path": list(row.requirement_path),
            }
        )
    for targets in result.values():
        for ordinal, target in enumerate(targets, start=1):
            target["block_ordinal"] = ordinal
            target["block_target_count"] = len(targets)
    return result


_DISCOVERY_NUMERIC_RE = re.compile(
    r"(?<![\d.])[+\-−]?\d+(?:\.\d+)?\s*(?:mA|uA|μA|µA|A|mV|kV|V|mΩ|kΩ|MΩ|Ω|Hz|kHz|MHz|dB\([A-Z]\)|°C|℃|%|％)"
)


def _numeric_discovery_targets(
    report_rows: Sequence[ReportRow],
    known_target_ids: set[str],
) -> list[ReportRow]:
    """Find numeric/percentage Report results outside validated semantic blocks."""

    return [
        row
        for row in report_rows
        if 1 <= row.sequence <= 117
        and row.row_id not in known_target_ids
        and is_actual_report_result(row.result_raw)
        and _DISCOVERY_NUMERIC_RE.search(row.result_raw)
    ]


def _numeric_discovery_ledger(
    targets: Sequence[ReportRow],
    record_rows: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[CoverageEntry], list[str]]:
    ledger: list[dict[str, Any]] = []
    coverage: list[CoverageEntry] = []
    source_ids: list[str] = []
    for ordinal, target in enumerate(targets, start=1):
        candidates = [
            row for row in record_rows
            if _clause_compatible(str(row.get("clause") or ""), target.clause_raw)
        ]
        source = candidates[0] if candidates else None
        source_id = str(source["row_id"]) if source is not None else f"record61:numeric-discovery:{ordinal:04d}"
        if source is None:
            source_ids.append(source_id)
        record_location = _record_location(source) if source is not None else {"pdf_page": BODY_FIRST_PDF_PAGE, "bbox": [1.0, 1.0, 2.0, 2.0]}
        entry_id = f"RECORD61-NUMERIC-DISCOVERY-{ordinal:04d}"
        reason = "numeric_target_outside_validated_semantics_manual"
        ledger.append(
            {
                "entry_id": entry_id,
                "id": entry_id,
                "rule_id": "RECORD61-NUMERIC-DISCOVERY",
                "scope_ids": ["S26", "S30", "S31", "S32", "S33"],
                "source_row_id": source_id,
                "target_row_id": target.row_id,
                "source_row_ids": [source_id],
                "target_row_ids": [target.row_id],
                "disposition": "manual",
                "reason_code": reason,
                "record_location": record_location,
                "report_location": _report_location(target),
                "record": {"clause": source.get("clause") if source else None, "value": None, "candidate": None},
                "report": _report_row_payload(target),
                "comparison": {"decision": "manual", "reason_code": reason, "report_value": target.result_raw},
            }
        )
        coverage.append(CoverageEntry(source_id, target.row_id, "manual", reason, (entry_id,)))
    return ledger, coverage, source_ids


def _numeric_report_target_row_ids(report_rows: Sequence[ReportRow]) -> set[str]:
    return {
        target["row"].row_id
        for targets in _numeric_report_targets(report_rows).values()
        for target in targets
    }


def _select_87_cells(target: Mapping[str, Any], cells: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    row: ReportRow = target["row"]
    path = [compact(value) for value in row.requirement_path]
    joined = "".join(path)
    metric_text = path[0] if path else compact(row.requirement_raw)
    metric = {
        "无频率加权漏电流": "unweighted",
        "对地漏电流": "earth",
        "接触电流": "touch",
        "患者漏电流": "patient",
        "患者辅助电流": "auxiliary",
        "总患者漏电流": "total",
        "功能接地漏电流": "functional",
    }.get(metric_text)
    if metric is None:
        raise ValueError(f"unsupported 8.7 Report metric path: {row.requirement_path!r}")
    if "应用部分加压状态" in joined:
        metric = f"{metric}_applied"
    elif "未保护接地的金属可触及部分加压状态" in joined:
        metric = f"{metric}_metal"

    phase = target.get("wet_phase")
    state = "NC" if "正常状态" in joined else "SFC" if "单一故障状态" in joined else None
    current = "dc" if "直流" in joined else "ac" if "交流" in joined else None
    selected = []
    for cell in cells:
        semantic = cell["semantic"]
        if semantic.get("metric") != metric:
            continue
        if phase and semantic.get("phase") != phase:
            continue
        if state and semantic.get("state") != state:
            continue
        if current and semantic.get("current") != current:
            continue
        selected.append(cell)
    if not selected:
        raise ValueError(
            f"8.7 target {row.row_id} has no real Record cells for "
            f"metric={metric}, phase={phase}, state={state}, current={current}"
        )
    return selected


def _target_source_cells(
    block: str,
    target: Mapping[str, Any],
    candidates: Mapping[str, Sequence[dict[str, Any]]],
) -> list[dict[str, Any]]:
    row: ReportRow = target["row"]
    # A full-cell strike/cancellation is an explicit void marker, not a
    # measured value.  Keep its geometry in extraction diagnostics, but do not
    # let it enter a target's applicable numeric set or force the comparison to
    # manual merely because OCR correctly has no number to read.
    cells = [
        cell
        for cell in candidates.get(block, ())
        if not cell.get("void_or_crossed_out")
    ]
    if block == "4.11":
        return cells
    if block == "8.6":
        requirement = compact(row.requirement_raw)
        position = "inlet_pe" if "器具输入插座" in requirement else "plug_pe"
        selected = [cell for cell in cells if cell["semantic"].get("measurement_position") == position]
        if not selected:
            selected = [cell for cell in cells if cell["semantic"].get("measurement_position_unresolved")]
        return selected
    if block == "8.7":
        return _select_87_cells(target, cells)
    if block == "9.6":
        kind = "impulse" if "dB(C)" in row.requirement_raw else "audible"
        return [cell for cell in cells if cell["semantic"].get("sound_kind") == kind]
    requirement = compact(row.requirement_raw)
    if "多位插座" in requirement or "16.6.2" in requirement:
        condition = "multiple_socket_outlet"
    elif "中断" in requirement:
        condition = "protective_earth_interrupted"
    else:
        condition = "normal"
    return [cell for cell in cells if cell["semantic"].get("condition") == condition]


def _numeric_manual_comparison(
    block: str,
    target: ReportRow,
    cells: Sequence[Mapping[str, Any]],
    *,
    target_ordinal: int = 1,
    target_count: int = 1,
) -> dict[str, Any]:
    # Some handwritten Record values are legible to Apple Vision but not to
    # Tesseract (or vice versa).  For the two 8.6.4 resistance rows we can
    # resolve that disagreement safely when exactly one OCR candidate converts
    # to the Report result.  This handles the real 100 mΩ -> 0.10 Ω case while
    # still keeping ambiguous candidates in manual review.
    if block == "8.6" and len(cells) == 1:
        cell = cells[0]
        channels = cell.get("recognition_channels") or {}
        candidate_values = _ocr_numeric_values(
            [
                *list((channels.get("apple_vision") or {}).get("candidates", [])),
                *list((channels.get("tesseract") or {}).get("candidates", [])),
            ]
        )
        guided_matches: dict[str, ComparisonResult] = {}
        for candidate in sorted(candidate_values):
            comparison = compare_numeric_observation(
                candidate,
                cell.get("unit"),
                target.result_raw,
                target.unit_context,
            )
            if comparison.decision == "match":
                guided_matches[candidate] = comparison
        if len(guided_matches) == 1:
            candidate, comparison = next(iter(guided_matches.items()))
            result = comparison.to_dict()
            result["record_candidate"] = candidate
            result["recognition_method"] = "report_guided_ocr"
            return result
    if any(cell.get("semantic", {}).get("measurement_position_unresolved") for cell in cells):
        return {
            "decision": "manual",
            "reason_code": "record_measurement_position_unresolved",
            "report_value": target.result_raw,
            "record_values": [cell.get("accepted_value") for cell in cells],
        }
    accepted = [cell.get("accepted_value") for cell in cells]
    if block == "4.11" and len(cells) == 1:
        occurrence_values = cells[0].get("accepted_values")
        if (
            isinstance(occurrence_values, Sequence)
            and not isinstance(occurrence_values, (str, bytes))
            and len(occurrence_values) >= target_ordinal
            and isinstance(occurrence_values[target_ordinal - 1], str)
        ):
            result = compare_final_percentage(
                str(occurrence_values[target_ordinal - 1]),
                target.result_raw,
            ).to_dict()
            result["record_value_ordinal"] = target_ordinal
            result["record_value_count"] = len(occurrence_values)
            return result
        if target_count == 1 and isinstance(accepted[0], str):
            if "%" in accepted[0] or "％" in accepted[0]:
                return compare_final_percentage(accepted[0], target.result_raw).to_dict()
            return {
                "decision": "manual",
                "reason_code": "record_percentage_unit_not_dual_recognised",
                "report_value": target.result_raw,
                "record_value": accepted[0],
            }
        return {
            "decision": "manual",
            "reason_code": "record_percentage_occurrence_not_dual_recognised",
            "report_value": target.result_raw,
            "record_value_ordinal": target_ordinal,
            "record_value_count": (
                len(occurrence_values)
                if isinstance(occurrence_values, Sequence)
                and not isinstance(occurrence_values, (str, bytes))
                else None
            ),
        }
    if accepted and all(value is not None for value in accepted):
        numeric_values = [str(value) for value in accepted]
        if re.search(r"\([+\-−]\)", compact(target.result_raw)):
            return {
                "decision": "manual",
                "reason_code": "report_polarity_has_no_uniquely_attributed_record_evidence",
                "report_value": target.result_raw,
                "record_values": numeric_values,
            }
        individual = []
        for value, cell in zip(numeric_values, cells):
            parsed_record = parse_report_numeric(value, cell.get("unit"))
            parsed_report = parse_report_numeric(target.result_raw, target.unit_context)
            if parsed_record is None:
                return {
                    "decision": "manual",
                    "reason_code": "record_numeric_syntax_unresolved",
                    "report_value": target.result_raw,
                    "record_values": numeric_values,
                }
            if parsed_record.comparator is None:
                individual.append(
                    compare_numeric_observation(
                        parsed_record.value,
                        parsed_record.unit,
                        target.result_raw,
                        target.unit_context,
                    )
                )
                continue
            if parsed_report is None or parsed_report.comparator is None:
                return {
                    "decision": "manual",
                    "reason_code": "record_interval_cannot_prove_report_exact_value",
                    "report_value": target.result_raw,
                    "record_values": numeric_values,
                }
            same_direction = (
                parsed_record.comparator in {"<", "<="}
                and parsed_report.comparator in {"<", "<="}
            ) or (
                parsed_record.comparator in {">", ">="}
                and parsed_report.comparator in {">", ">="}
            )
            if not same_direction:
                return {
                    "decision": "manual",
                    "reason_code": "record_and_report_intervals_have_different_directions",
                    "report_value": target.result_raw,
                    "record_values": numeric_values,
                }
            try:
                bound = convert_decimal(
                    parsed_record.value,
                    parsed_record.unit,
                    parsed_report.unit,
                )
            except (TypeError, ValueError):
                return {
                    "decision": "manual",
                    "reason_code": "unit_conversion_unresolved",
                    "report_value": target.result_raw,
                    "record_values": numeric_values,
                }
            if parsed_report.comparator in {"<", "<="}:
                matched = bound < parsed_report.value or (
                    bound == parsed_report.value
                    and (parsed_record.comparator == "<" or parsed_report.comparator == "<=")
                )
            else:
                matched = bound > parsed_report.value or (
                    bound == parsed_report.value
                    and (parsed_record.comparator == ">" or parsed_report.comparator == ">=")
                )
            individual.append(
                ComparisonResult(
                    "match" if matched else "mismatch",
                    "numeric_interval_implies_report" if matched else "numeric_interval_mismatch",
                    f"{parsed_report.comparator}{parsed_report.value}",
                    f"{parsed_record.comparator}{bound}",
                    bound,
                )
            )
        if any(result.decision == "manual" for result in individual):
            return {
                "decision": "manual",
                "reason_code": "one_or_more_numeric_cells_could_not_be_compared",
                "record_values": numeric_values,
                "individual": [result.to_dict() for result in individual],
            }
        if any(result.expected and result.expected[0] in "<>" for result in individual):
            matched = all(result.decision == "match" for result in individual)
            return {
                "decision": "match" if matched else "mismatch",
                "reason_code": "numeric_threshold_matched" if matched else "numeric_threshold_mismatch",
                "record_values": numeric_values,
                "individual": [result.to_dict() for result in individual],
            }
        # For a reported measured value, the template records the maximum
        # applicable observation for that semantic condition.
        maximum = max(numeric_values, key=lambda value: abs(Decimal(value)))
        return compare_numeric_observation(
            str(abs(Decimal(maximum))),
            cells[0].get("unit"),
            target.result_raw,
            target.unit_context,
        ).to_dict()
    return {
        "decision": "manual",
        "reason_code": "record_cells_real_but_dual_recognition_unresolved",
        "report_value": target.result_raw,
        "record_values": accepted,
        "aggregation": "maximum_applicable_value" if len(cells) > 1 else "single_cell",
        "percentage_policy": "copy_final_recorded_percentage_only" if block == "4.11" else None,
    }


def _numeric_ledger(
    document: fitz.Document,
    report_rows: Sequence[ReportRow],
    report_number: tuple[int, int] | None,
    output_dir: Path | None = None,
) -> tuple[list[dict[str, Any]], list[CoverageEntry], dict[str, Any], list[str]]:
    candidates = _numeric_record_candidates(document, output_dir)
    targets = _numeric_report_targets(report_rows)
    counts = {block: len(rows) for block, rows in targets.items()}
    sample_number = report_number[1] if report_number else None
    expected = NUMERIC_TARGETS_BY_SAMPLE.get(sample_number)
    if expected is not None and counts != expected:
        raise ValueError(
            f"unexpected numeric target inventory for {sample_number}: {counts}, expected {expected}"
        )

    ledger: list[dict[str, Any]] = []
    coverage_entries: list[CoverageEntry] = []
    source_ids: list[str] = []
    used_cells_by_block: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    recognized_source_ids: set[str] = set()
    for block in ("4.11", "8.6", "8.7", "9.6", "16.6"):
        for ordinal, target_info in enumerate(targets[block], start=1):
            target: ReportRow = target_info["row"]
            cells = _target_source_cells(block, target_info, candidates)
            if not cells:
                raise ValueError(f"numeric target {target.row_id} in {block} has no real Record source cell")
            cell_ids = [cell["row_id"] for cell in cells]
            if len(cell_ids) != len(set(cell_ids)):
                raise ValueError(f"numeric target {target.row_id} repeats a Record source cell")
            for cell in cells:
                if block == "4.11":
                    cell["expected_value_count"] = len(targets[block])
                used_cells_by_block[block][cell["row_id"]] = cell
                if cell["row_id"] not in source_ids:
                    source_ids.append(cell["row_id"])
                if cell["row_id"] not in recognized_source_ids:
                    _recognize_measurement_cell(document, cell, output_dir)
                    recognized_source_ids.add(cell["row_id"])

            comparison = _numeric_manual_comparison(
                block,
                target,
                cells,
                target_ordinal=int(target_info.get("block_ordinal") or ordinal),
                target_count=int(target_info.get("block_target_count") or len(targets[block])),
            )
            disposition = {
                "match": "matched",
                "mismatch": "mismatch",
                "manual": "manual",
                "not_applicable": "not_applicable",
                "excluded": "excluded",
            }[comparison["decision"]]
            reason_code = comparison["reason_code"]
            entry_id = f"RECORD61-NUMERIC-{block.replace('.', '_')}-{ordinal:02d}"
            finding_ids = (entry_id,) if disposition in {"manual", "mismatch"} else ()
            record_evidence = [
                {"pdf_page": cell["physical_page"], "bbox": list(cell["bbox"])}
                for cell in cells
            ]
            report_location = _report_location(target)
            ledger.append(
                {
                    "entry_id": entry_id,
                    "id": entry_id,
                    "rule_id": "RECORD61-BODY-PERCENT" if block == "4.11" else "RECORD61-BODY-NUMERIC",
                    "scope_ids": ["S26", "S30", "S31", "S32", "S33"],
                    "source_row_id": cell_ids[0],
                    "target_row_id": target.row_id,
                    "source_row_ids": cell_ids,
                    "target_row_ids": [target.row_id],
                    "disposition": disposition,
                    "reason_code": reason_code,
                    "record_location": record_evidence[0],
                    "report_location": report_location,
                    "record_evidence": record_evidence,
                    "report_evidence": report_location,
                    "record": {
                        "block_type": block,
                        "source_cells": cells,
                        "automatic_value": None,
                    },
                    "report": target.to_dict(),
                    "mapping": {
                        "method": "template_semantics_to_real_measurement_cells",
                        "wet_phase": target_info.get("wet_phase"),
                        "record_value_ordinal": (
                            target_info.get("block_ordinal") if block == "4.11" else None
                        ),
                    },
                    "comparison": comparison,
                }
            )
            for source_id in cell_ids:
                coverage_entries.append(
                    CoverageEntry(source_id, target.row_id, disposition, reason_code, finding_ids)
                )
    used_candidates = {
        block: list(rows.values()) for block, rows in used_cells_by_block.items()
    }
    return (
        ledger,
        coverage_entries,
        {
            "target_counts": counts,
            "expected_target_counts": expected or counts,
            "total_target_count": sum(counts.values()),
            "record_candidate_counts": {key: len(value) for key, value in used_candidates.items()},
            "record_candidates": used_candidates,
            "recognition_policy": "manual_until_cell_attribution_and_two_local_recognition_channels_agree",
            "percentage_policy": "compare_only_the_final_percentage_written_in_the_record_body_cell",
            "source_identity": "physical_page+printed_page+occurrence+table+row+cell",
            "dual_recognition": {
                "attempted": output_dir is not None,
                "cell_count": len(recognized_source_ids),
                "resolved_cell_count": sum(
                    cell.get("accepted_value") is not None
                    for rows in used_candidates.values()
                    for cell in rows
                ),
            },
        },
        source_ids,
    )


def _report_identity_cells(document: fitz.Document) -> tuple[fitz.Page, list[list[Any]], fitz.table.Table]:
    if document.page_count < 3:
        raise ValueError("Report page 3 is required for identity comparison")
    page = document[2]
    tables = [table for table in page.find_tables().tables if table.row_count >= 7 and table.col_count >= 5]
    if len(tables) != 1:
        raise ValueError(f"Report identity table is not unique ({len(tables)})")
    table = tables[0]
    return page, table.extract(), table


def _record_identity_evidence(page: fitz.Page) -> list[dict[str, Any]]:
    """Bind every handwritten cover field to its printed underline.

    The underline geometry is part of the Record template and is more stable
    than a hard-coded rectangular band.  Ink annotations are assigned by
    centroid to the nearest of the five baselines, then the evidence rectangle
    is the union of that baseline and all owned strokes.  This preserves long
    values that extend past the printed line (the 1347 sample name does so).
    """

    line_candidates: list[fitz.Rect] = []
    for drawing in page.get_drawings():
        for item in drawing.get("items", []):
            if not item or item[0] != "l":
                continue
            start, end = item[1], item[2]
            x0, x1 = sorted((float(start.x), float(end.x)))
            y0, y1 = float(start.y), float(end.y)
            if (
                abs(y0 - y1) <= 0.5
                and 180.0 <= x0 <= 200.0
                and x1 - x0 >= 280.0
                and 195.0 <= y0 <= 340.0
            ):
                line_candidates.append(fitz.Rect(x0, y0, x1, y1))

    baselines: list[fitz.Rect] = []
    for line in sorted(line_candidates, key=lambda value: (value.y0, value.x0, value.x1)):
        if baselines and abs(line.y0 - baselines[-1].y0) <= 0.75:
            baselines[-1].include_rect(line)
        else:
            baselines.append(fitz.Rect(line))
    if len(baselines) != 5:
        raise ValueError(
            f"Record cover identity underlines are not the expected five fields ({len(baselines)})"
        )

    owned: list[list[Mapping[str, Any]]] = [[] for _ in baselines]
    min_x = min(line.x0 for line in baselines) - 6.0
    max_x = page.rect.x1 - 8.0
    # Handwriting sits above its underline.  The samples' visual centre is
    # about 11 pt above the baseline; measuring distance to that centre avoids
    # assigning the upper strokes of the next line to the previous field.
    expected_centers = [line.y0 - 11.0 for line in baselines]
    min_y = expected_centers[0] - 18.0
    max_y = expected_centers[-1] + 18.0
    for ink in _page_inks(page):
        cx = float(ink["centroid_x"])
        cy = float(ink["centroid_y"])
        if not (min_x <= cx <= max_x and min_y <= cy <= max_y):
            continue
        nearest = min(range(len(baselines)), key=lambda index: abs(cy - expected_centers[index]))
        if abs(cy - expected_centers[nearest]) <= 18.0:
            owned[nearest].append(ink)

    evidence: list[dict[str, Any]] = []
    for index, (baseline, inks) in enumerate(zip(baselines, owned, strict=True)):
        bbox = fitz.Rect(baseline.x0, baseline.y0 - 0.25, baseline.x1, baseline.y0 + 0.25)
        for ink in inks:
            bbox.include_rect(fitz.Rect(ink["bbox"]))
        bbox = fitz.Rect(
            max(page.rect.x0, bbox.x0 - 3.0),
            max(page.rect.y0, bbox.y0 - 3.0),
            min(page.rect.x1, bbox.x1 + 3.0),
            min(page.rect.y1, bbox.y1 + 3.0),
        )
        evidence.append(
            {
                "baseline_index": index,
                "baseline_y": round(float(baseline.y0), 3),
                "baseline_bbox": _rect(baseline),
                "bbox": _rect(bbox),
                "ink_bboxes": [_rect(ink["bbox"]) for ink in inks],
                "ink_count": len(inks),
            }
        )
    return evidence


def _identity_recognition(
    page: fitz.Page,
    field: str,
    bbox: Sequence[float],
    output_dir: Path | None,
) -> dict[str, Any]:
    if output_dir is None:
        return {
            "attempted": False,
            "apple_vision": {"status": "not_run", "candidates": []},
            "tesseract": {"status": "not_run", "candidates": []},
            "text_ocr": {"status": "not_run", "lines": []},
        }
    image_path = output_dir / "record61-ocr" / f"record61_identity_{field}.png"
    render_cell_for_ocr(page, fitz.Rect(bbox), image_path)
    candidates = local_ocr_candidates(image_path)
    text_candidates = local_text_ocr(image_path)
    return {
        "attempted": True,
        "ocr_image": str(image_path),
        "apple_vision": {
            "status": "available" if "apple_vision_error" not in candidates else "unavailable",
            "candidates": list(candidates.get("apple_vision", [])),
            "error": candidates.get("apple_vision_error"),
        },
        "tesseract": {
            "status": "available" if candidates.get("tesseract") else "no_candidate",
            "candidates": list(candidates.get("tesseract", [])),
        },
        "text_ocr": {
            "status": "available" if text_candidates.get("lines") else "unavailable",
            "lines": list(text_candidates.get("lines", [])),
            "error": text_candidates.get("error"),
        },
    }


def _identity_normalize(value: str) -> str:
    """Normalize OCR/report identity text without changing Chinese semantics."""

    text = display_text(value).upper()
    text = text.replace("Ｏ", "O").replace("Ｑ", "Q").replace("－", "-")
    # Report numbers are printed as QW2025-1234 while the Record may contain
    # only the handwritten suffix.  Keep alphanumeric content and Chinese
    # characters, dropping layout punctuation and spaces.
    return re.sub(r"[^0-9A-Z\u3400-\u9fff]+", "", text)


def _identity_report_number(value: str) -> tuple[str | None, str | None]:
    """Parse a Report number as (year, suffix), preserving a suffix-only OCR."""

    normalized = _identity_normalize(value)
    match = re.search(r"(?:QW)?(20\d{2})(\d{3,})$", normalized)
    if match:
        return match.group(1), match.group(2)
    suffix = re.search(r"\d{3,}$", normalized)
    return None, suffix.group(0) if suffix else None


def _identity_channel_values(
    recognition: Mapping[str, Any],
) -> tuple[list[str], list[str]]:
    """Return conservative, complete-field candidates from two local channels.

    Apple Vision lines are accepted only when confidence is explicit and at
    least 0.80. Tesseract's numeric candidates are retained as a second
    channel for report numbers; other handwritten Chinese fields remain manual
    unless two complete text channels independently agree.
    """

    apple: list[str] = []
    for item in recognition.get("text_ocr", {}).get("lines", []):
        text = str(item.get("text", "")).strip()
        confidence = item.get("confidence")
        if text and (confidence is None or float(confidence) >= 0.80):
            apple.append(text)
    tesseract: list[str] = []
    for item in recognition.get("tesseract", {}).get("candidates", []):
        text = str(item.get("text", "")).strip()
        if text:
            tesseract.append(text)
    return apple, tesseract


def _identity_decision(
    report_value: str,
    recognition: Mapping[str, Any],
    *,
    ink_count: int,
) -> tuple[str, str, str | None]:
    """Resolve a cover identity field when local text OCR has a clear value.

    The default path remains manual when OCR was not requested or did not yield
    a candidate.  A mismatch is emitted only when an OCR candidate is available
    and is clearly different from the printed Report value.
    """

    if ink_count == 0:
        return "manual", "record_identity_blank", None
    if not recognition.get("attempted"):
        return "manual", "record_handwritten_identity_not_dual_recognised", None
    apple, tesseract = _identity_channel_values(recognition)
    if not apple:
        return "manual", "record_handwritten_identity_not_dual_recognised", None
    target = _identity_normalize(report_value)
    if not target:
        return "manual", "record_handwritten_identity_not_dual_recognised", None

    # Report number comparison permits an explicitly located suffix, but only
    # after two independent OCR channels agree on that suffix (or on the full
    # QW+year+suffix value). A single low-confidence or conflicting candidate
    # cannot produce mismatch.
    target_year, target_suffix = _identity_report_number(report_value)
    if target_suffix:
        apple_parts = [_identity_report_number(value) for value in apple]
        tess_parts = [_identity_report_number(value) for value in tesseract]
        apple_suffixes = {suffix for _, suffix in apple_parts if suffix}
        tess_suffixes = {suffix for _, suffix in tess_parts if suffix}
        common_suffixes = apple_suffixes & tess_suffixes
        if len(common_suffixes) == 1:
            suffix = next(iter(common_suffixes))
            if suffix == target_suffix:
                return "matched", "record_identity_report_number_suffix_matches", suffix
            return "mismatch", "record_identity_report_number_suffix_differs", suffix

    # For Chinese/text identities, require exactly one complete candidate in
    # each independent channel and exact normalized equality. Partial text,
    # field labels, multiple candidates and one-channel OCR all remain manual.
    if len(apple) == 1 and len(tesseract) == 1:
        apple_value = _identity_normalize(apple[0])
        tesseract_value = _identity_normalize(tesseract[0])
        if apple_value and apple_value == tesseract_value:
            if apple_value == target:
                return "matched", "record_identity_dual_ocr_matches_report", apple_value
            return "mismatch", "record_identity_dual_ocr_differs_from_report", apple_value
    return "manual", "record_handwritten_identity_ocr_insufficient_evidence", None


def _identity_ledger(
    record_document: fitz.Document,
    report_document: fitz.Document,
    output_dir: Path | None = None,
) -> tuple[list[dict[str, Any]], list[CoverageEntry], list[str], list[str]]:
    _, values, table = _report_identity_cells(report_document)
    record_page = record_document[0]
    record_evidence = _record_identity_evidence(record_page)
    ledger: list[dict[str, Any]] = []
    coverage_entries: list[CoverageEntry] = []
    source_ids: list[str] = []
    target_ids: list[str] = []

    report_cover = report_document[0]
    cover_words = report_cover.get_text("words")
    anchor = next((word for word in cover_words if "报告编号" in str(word[4])), None)
    if anchor is None:
        raise ValueError("Report cover report-number line was not found")
    line_words = [
        word for word in cover_words
        if int(word[5]) == int(anchor[5]) and int(word[6]) == int(anchor[6])
    ]
    target_bbox = fitz.Rect(
        min(float(word[0]) for word in line_words),
        min(float(word[1]) for word in line_words),
        max(float(word[2]) for word in line_words),
        max(float(word[3]) for word in line_words),
    )
    target_value = " ".join(str(word[4]) for word in sorted(line_words, key=lambda item: item[0]))
    source_evidence = record_evidence[0]
    source_bbox = fitz.Rect(source_evidence["bbox"])
    recognition = _identity_recognition(record_page, "report_number", source_bbox, output_dir)
    decision, reason_code, recognized_value = _identity_decision(
        target_value,
        recognition,
        ink_count=int(source_evidence.get("ink_count", 0)),
    )
    source_id = "record61:identity:report_number"
    target_id = "report:identity:report_number"
    entry_id = "RECORD61-IDENTITY-REPORT_NUMBER"
    source_ids.append(source_id)
    target_ids.append(target_id)
    ledger.append(
        {
            "entry_id": entry_id,
            "id": entry_id,
            "rule_id": "RECORD61-IDENTITY",
            "scope_ids": ["S25", "S33"],
            "source_row_id": source_id,
            "target_row_id": target_id,
            "source_row_ids": [source_id],
            "target_row_ids": [target_id],
            "disposition": "matched" if decision == "matched" else "mismatch" if decision == "mismatch" else "manual",
            "reason_code": reason_code,
            "record_location": {"pdf_page": 1, "bbox": _rect(source_bbox)},
            "report_location": {"pdf_page": 1, "bbox": _rect(target_bbox)},
            "record": {
                "field": "report_number",
                "value": recognized_value,
                "extraction": "template_baseline_and_owned_ink",
                "baseline_y": source_evidence["baseline_y"],
                "baseline_bbox": source_evidence["baseline_bbox"],
                "ink_bboxes": source_evidence["ink_bboxes"],
                "recognition_channels": recognition,
            },
            "report": {"field": "report_number", "label": "报告编号", "value": target_value},
            "comparison": {
                "decision": decision,
                "reason_code": reason_code,
                "record_value": recognized_value,
                "report_value": target_value,
            },
        }
    )
    coverage_entries.append(
        CoverageEntry(
            source_id,
            target_id,
            "matched" if decision == "matched" else "mismatch" if decision == "mismatch" else "manual",
            reason_code,
            (entry_id,),
        )
    )

    for key, label, row_index, column_index, baseline_index in IDENTITY_FIELDS:
        source_id = f"record61:identity:{key}"
        target_id = f"report:identity:{key}"
        source_ids.append(source_id)
        target_ids.append(target_id)
        target_cell = table.rows[row_index].cells[column_index]
        if target_cell is None:
            raise ValueError(f"Report identity field {key} has no cell rectangle")
        target_value = display_text(values[row_index][column_index])
        source_evidence = record_evidence[baseline_index]
        source_bbox = fitz.Rect(source_evidence["bbox"])
        recognition = _identity_recognition(record_page, key, source_bbox, output_dir)
        decision, reason_code, recognized_value = _identity_decision(
            target_value,
            recognition,
            ink_count=int(source_evidence.get("ink_count", 0)),
        )
        entry_id = f"RECORD61-IDENTITY-{key.upper()}"
        ledger.append(
            {
                "entry_id": entry_id,
                "id": entry_id,
                "rule_id": "RECORD61-IDENTITY",
                "scope_ids": ["S25", "S33"],
                "source_row_id": source_id,
                "target_row_id": target_id,
                "source_row_ids": [source_id],
                "target_row_ids": [target_id],
                "disposition": "matched" if decision == "matched" else "mismatch" if decision == "mismatch" else "manual",
                "reason_code": reason_code,
                "record_location": {"pdf_page": 1, "bbox": _rect(source_bbox)},
                "report_location": {"pdf_page": 3, "bbox": _rect(target_cell)},
                "record": {
                    "field": key,
                    "value": recognized_value,
                    "extraction": "template_baseline_and_owned_ink",
                    "baseline_y": source_evidence["baseline_y"],
                    "baseline_bbox": source_evidence["baseline_bbox"],
                    "ink_bboxes": source_evidence["ink_bboxes"],
                    "recognition_channels": recognition,
                },
                "report": {"field": key, "label": label, "value": target_value},
                "comparison": {
                    "decision": decision,
                    "reason_code": reason_code,
                    "record_value": recognized_value,
                    "report_value": target_value,
                },
            }
        )
        coverage_entries.append(
            CoverageEntry(
                source_id,
                target_id,
                "matched" if decision == "matched" else "mismatch" if decision == "mismatch" else "manual",
                reason_code,
                (entry_id,),
            )
        )
    return ledger, coverage_entries, source_ids, target_ids


def _scope_118_entry(
    record_document: fitz.Document,
    report_row: ReportRow,
) -> tuple[dict[str, Any], CoverageEntry]:
    page = record_document[BODY_LAST_PDF_PAGE - 1]
    tables = page.find_tables().tables
    if not tables:
        raise ValueError("Record page 96 table is required as the end-of-template evidence")
    record_bbox = _rect(max(tables, key=lambda item: item.row_count * item.col_count).bbox)
    entry_id = "RECORD61-SCOPE-REPORT-118"
    reason = "record_template_ends_at_chapter_16_and_has_no_chapter_17_row"
    ledger = {
        "entry_id": entry_id,
        "id": entry_id,
        "rule_id": "RECORD61-SCOPE",
        "scope_ids": ["S32"],
        "source_row_id": None,
        "target_row_id": report_row.row_id,
        "source_row_ids": [],
        "target_row_ids": [report_row.row_id],
        "disposition": "not_applicable",
        "reason_code": reason,
        "record_location": {"pdf_page": BODY_LAST_PDF_PAGE, "bbox": record_bbox},
        "report_location": _report_location(report_row),
        "record": {"template_last_body_page": BODY_LAST_PDF_PAGE, "last_chapter": "16.9"},
            "report": _report_row_payload(report_row),
    }
    return ledger, CoverageEntry(None, report_row.row_id, "not_applicable", reason)


def _entry_evidence(entry: Mapping[str, Any]) -> list[dict[str, Any]]:
    def locations(value: Any, fallback: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        if isinstance(value, list) and value:
            return [item for item in value if isinstance(item, Mapping)]
        if isinstance(value, Mapping):
            return [value]
        return [fallback]

    record_context = entry.get("source_row_id") is None
    report_context = entry.get("target_row_id") is None
    evidence: list[dict[str, Any]] = []
    seen: set[tuple[str, int, tuple[float, ...]]] = set()
    for role, values, fallback, semantic_role in (
        (
            RECORD_ROLE,
            entry.get("record_evidence"),
            entry["record_location"],
            "sequence_context" if record_context else "source_observation",
        ),
        (
            "report",
            entry.get("report_evidence"),
            entry["report_location"],
            "sequence_context" if report_context else "comparison_target",
        ),
    ):
        for location in locations(values, fallback):
            key = (
                role,
                int(location["pdf_page"]),
                tuple(float(value) for value in location["bbox"]),
            )
            if key in seen:
                continue
            seen.add(key)
            evidence.append(
                {
                    "role": role,
                    "pdf_page": location["pdf_page"],
                    "bbox": location["bbox"],
                    "semantic_role": semantic_role,
                }
            )
    return evidence


def _findings(ledger: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for entry in ledger:
        disposition = entry["disposition"]
        status = {
            "matched": "pass",
            "mismatch": "error",
            "manual": "manual",
            "not_applicable": "pass",
            "excluded": "pass",
        }[disposition]
        findings.append(
            {
                "id": entry["entry_id"],
                "rule_id": entry["rule_id"],
                "status": status,
                "title": (
                    "9706.1 Record与Report不一致"
                    if status == "error"
                    else "9706.1 Record与Report待人工复核"
                    if status == "manual"
                    else "9706.1 Record与Report一致或范围外"
                ),
                "summary": str(entry.get("reason_code") or ""),
                "details": {
                    "scope_ids": entry.get("scope_ids", []),
                    "source_row_id": entry.get("source_row_id"),
                    "target_row_id": entry.get("target_row_id"),
                    "disposition": disposition,
                    "comparison": entry.get("comparison"),
                    "mapping": entry.get("mapping"),
                },
                "evidence_locations": _entry_evidence(entry),
            }
        )
        if entry.get("nonconforming_alert") and disposition == "matched":
            findings.append(
                {
                    "id": f"{entry['entry_id']}-NONCONFORMING",
                    "rule_id": "RECORD61-NONCONFORMING-ALERT",
                    "status": "warning",
                    "title": "Record明确记录不符合",
                    "summary": "Record的‘不符合’与Report已一致映射，但仍需显著警示",
                    "details": {"ledger_entry_id": entry["entry_id"]},
                    "evidence_locations": _entry_evidence(entry),
                }
            )
    return findings


def _overall_status(ledger: Sequence[Mapping[str, Any]], findings: Sequence[Mapping[str, Any]]) -> str:
    if any(entry["disposition"] == "mismatch" for entry in ledger):
        return "error"
    if any(entry["disposition"] == "manual" for entry in ledger):
        return "manual"
    if any(finding["status"] == "warning" for finding in findings):
        return "warning"
    return "pass"


def _record61_rule_execution_states(
    findings: Sequence[Mapping[str, Any]],
    *,
    coverage: Mapping[str, Any],
    ledger: Sequence[Mapping[str, Any]],
    record_status_rows: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Declare only proven empty conditional rules as not applicable.

    All ordinary rules remain succeeded, including a missing result.  The
    coordinator then rejects a missing ordinary Finding instead of silently
    treating it as a pass.  These three conditional rules can legitimately
    have no Finding after their target inventory has been inspected.
    """

    rule_ids = MODE_CATALOG[MODE]["rule_ids"]
    counts = Counter(str(finding.get("rule_id") or "") for finding in findings)
    states = {rule_id: {"state": "succeeded"} for rule_id in rule_ids}

    def declare_empty(rule_id: str, reason_code: str, reason_detail: Mapping[str, Any]) -> None:
        if counts[rule_id] == 0:
            states[rule_id] = {
                "state": "not_applicable",
                "disposition": "not_applicable",
                "reason_code": reason_code,
                "reason_detail": dict(reason_detail),
            }

    discovery = coverage.get("numeric_discovery", {})
    if (
        discovery.get("eligible") == 0
        and discovery.get("accounted") == 0
        and discovery.get("conserved") is True
    ):
        declare_empty(
            "RECORD61-NUMERIC-DISCOVERY",
            "RECORD61_NUMERIC_DISCOVERY_NO_TARGETS",
            {
                "target_count": 0,
                "search_basis": "Report sequences 1-117, actual numeric or percentage results outside validated numeric blocks",
                "inventory_conserved": True,
            },
        )

    numeric = coverage.get("numeric_targets", {})
    if (
        numeric.get("target_counts", {}).get("4.11") == 0
        and numeric.get("expected_target_counts", {}).get("4.11") == 0
    ):
        declare_empty(
            "RECORD61-BODY-PERCENT",
            "RECORD61_BODY_PERCENT_NO_TARGETS",
            {
                "clause": "4.11",
                "target_count": 0,
                "expected_target_count": 0,
                "search_basis": "Report actual final-percentage targets in clause 4.11",
            },
        )

    matched_nonconforming = sum(
        bool(entry.get("nonconforming_alert")) and entry.get("disposition") == "matched"
        for entry in ledger
    )
    if matched_nonconforming == 0:
        declare_empty(
            "RECORD61-NONCONFORMING-ALERT",
            "RECORD61_NONCONFORMING_ALERT_NO_MATCHED_ROWS",
            {
                "scanned_status_row_count": len(record_status_rows),
                "record_nonconforming_row_count": sum(
                    row.get("status") == "不符合" for row in record_status_rows
                ),
                "matched_nonconforming_row_count": 0,
                "search_basis": "All extracted Record status rows and matched body-status ledger entries",
            },
        )
    return states


def _structure_finding(
    extraction: Mapping[str, Any],
    report_rows: Sequence[ReportRow],
) -> dict[str, Any]:
    structure = extraction.get("template_structure", {})
    report_row = report_rows[0] if report_rows else None
    report_location = _report_location(report_row) if report_row is not None else {"pdf_page": 1, "bbox": [1.0, 1.0, 2.0, 2.0]}
    validated = bool(structure.get("validated"))
    return {
        "id": "RECORD61-STRUCTURE",
        "rule_id": "RECORD61-STRUCTURE",
        "scope_ids": ["S32"],
        "status": "pass" if validated else "manual",
        "title": "9706.1页码、表头和模板结构",
        "summary": "Record 6-96页模板结构完整" if validated else "Record页码或表格结构需要人工复核",
        "details": dict(structure),
        "evidence_locations": [
            {"role": RECORD_ROLE, "pdf_page": BODY_FIRST_PDF_PAGE, "bbox": [1.0, 1.0, 2.0, 2.0], "semantic_role": "template_structure"},
            {"role": "report", "pdf_page": report_location["pdf_page"], "bbox": report_location["bbox"], "semantic_role": "comparison_context"},
        ],
    }


def run_record_61_full(
    report_path: str | Path,
    record_path: str | Path,
    output_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Run the complete Report + GB 9706.1 Record coverage ledger.

    ``output_dir`` is accepted for the shared runner contract; this scanner does
    not write derived files.  Both source PDFs are hash-checked before return.
    """

    report_path = Path(report_path)
    record_path = Path(record_path)
    derived_output_dir = Path(output_dir) if output_dir is not None else None
    report_hash_before = sha256_file(report_path)
    record_hash_before = sha256_file(record_path)

    report_rows = scan_report_rows(report_path, (1, 118))
    report_body_rows = [row for row in report_rows if 1 <= row.sequence <= 117]
    report_118 = [row for row in report_rows if row.sequence == 118]
    if len(report_118) != 1:
        raise ValueError(f"Report sequence 118 must have one physical row, got {len(report_118)}")

    with fitz.open(record_path) as record_document, fitz.open(report_path) as report_document:
        record_status_rows, extraction = extract_record_61_status_rows(record_document)
        known_numeric_target_ids = _numeric_report_target_row_ids(report_body_rows)
        numeric_discovery_targets = _numeric_discovery_targets(report_body_rows, known_numeric_target_ids)
        numeric_discovery_ledger, numeric_discovery_coverage, numeric_discovery_source_ids = _numeric_discovery_ledger(
            numeric_discovery_targets, record_status_rows
        )
        identity_ledger, identity_coverage, identity_source_ids, identity_target_ids = _identity_ledger(
            record_document, report_document, derived_output_dir
        )
        (
            status_ledger,
            conclusion_ledger,
            status_coverage,
            conclusion_source_ids,
            conclusion_target_ids,
        ) = _status_ledger(
            record_status_rows,
            report_body_rows,
            excluded_target_row_ids=known_numeric_target_ids | {row.row_id for row in numeric_discovery_targets},
        )
        report_number = extract_report_number(report_document[0].get_text("text"))
        numeric_ledger, numeric_coverage, numeric_summary, numeric_source_ids = _numeric_ledger(
            record_document,
            report_rows,
            report_number,
            derived_output_dir,
        )
        metadata_ledger, metadata_coverage, metadata_source_ids, metadata_target_ids = _metadata_ledger(
            record_document, report_document
        )
        scope_ledger, scope_coverage = _scope_118_entry(record_document, report_118[0])

    ledger = [
        *identity_ledger,
        *status_ledger,
        *conclusion_ledger,
        *numeric_ledger,
        *numeric_discovery_ledger,
        *metadata_ledger,
        scope_ledger,
    ]
    coverage_entries = [
        *identity_coverage,
        *status_coverage,
        *numeric_coverage,
        *numeric_discovery_coverage,
        *metadata_coverage,
        scope_coverage,
    ]
    source_row_ids = [
        *identity_source_ids,
        *(row["row_id"] for row in record_status_rows),
        *conclusion_source_ids,
        *numeric_source_ids,
        *numeric_discovery_source_ids,
        *metadata_source_ids,
    ]
    target_row_ids = [
        *identity_target_ids,
        *(row.row_id for row in report_rows),
        *conclusion_target_ids,
        *metadata_target_ids,
    ]
    coverage = validate_coverage(source_row_ids, target_row_ids, coverage_entries)
    coverage["source_rows"]["row_ids"] = source_row_ids
    coverage["report_rows"]["row_ids"] = target_row_ids
    coverage["status_record_rows"] = {
        "eligible": len(record_status_rows),
        "accounted": len(
            {
                entry.source_row_id
                for entry in status_coverage
                if entry.source_row_id and entry.source_row_id.startswith("record61:status:")
            }
        ),
        "conserved": len(
            {
                entry.source_row_id
                for entry in status_coverage
                if entry.source_row_id and entry.source_row_id.startswith("record61:status:")
            }
        ) == len(record_status_rows),
    }
    coverage["sequence_conclusions"] = {
        "eligible": 117,
        "accounted": len(conclusion_ledger),
        "conserved": len(conclusion_ledger) == 117,
    }
    coverage["report_sequences"] = {
        "declared": 118,
        "body_compared": 117,
        "sequence_118_disposition": "not_applicable",
        "conserved": {row.sequence for row in report_rows} == set(range(1, 119)),
    }
    coverage["numeric_targets"] = numeric_summary
    coverage["numeric_discovery"] = {
        "eligible": len(numeric_discovery_targets),
        "accounted": len(numeric_discovery_ledger),
        "manual": len(numeric_discovery_ledger),
        "conserved": len(numeric_discovery_targets) == len(numeric_discovery_ledger),
        "target_row_ids": [row.row_id for row in numeric_discovery_targets],
    }

    report_hash_after = sha256_file(report_path)
    record_hash_after = sha256_file(record_path)
    unchanged = report_hash_before == report_hash_after and record_hash_before == record_hash_after
    if not unchanged:
        raise RuntimeError("source PDF changed during GB 9706.1 comparison")

    findings = _findings(ledger)
    findings.append(_structure_finding(extraction, report_rows))
    if any(not finding["rule_id"].startswith("RECORD61-") for finding in findings):
        raise RuntimeError("GB 9706.1 run leaked a non-RECORD61 finding")
    for finding in findings:
        if finding["status"] not in {"error", "manual"}:
            continue
        locations = finding["evidence_locations"]
        if {item["role"] for item in locations} != {"report", RECORD_ROLE}:
            raise RuntimeError(f"{finding['id']} lacks two-sided evidence roles")
        if any(not _valid_rect(item["bbox"]) for item in locations):
            raise RuntimeError(f"{finding['id']} has an invalid evidence rectangle")

    return {
        "schema_version": "record61-full-0.3",
        "mode": MODE,
        "overall_status": _overall_status(ledger, findings),
        "source_files": {
            "report": {"path": str(report_path), "sha256": report_hash_before},
            "record": {"path": str(record_path), "sha256": record_hash_before},
        },
        "source_integrity": {
            "report_sha256_before": report_hash_before,
            "report_sha256_after": report_hash_after,
            "record_sha256_before": record_hash_before,
            "record_sha256_after": record_hash_after,
            "unchanged": unchanged,
        },
        "scope": {
            "included": [
                "Record page-1 identity fields",
                "Record physical pages 6-96 / all 851 status rows",
                "Record/Report page, table-header and template-structure validation",
                "Report sequences 1-117 with bidirectional row coverage",
                "117 independent sequence-conclusion comparisons",
                "4.11, 8.6, 8.7, 9.6 and 16.6 numeric or final-percentage targets",
                "all Report numeric/percentage results outside validated blocks as explicit manual discovery targets",
                "object-level dates, instruments, personnel, signatures and remarks",
            ],
            "not_applicable": [
                "Report sequence 118 / chapter 17: no corresponding row in this Record template"
            ],
        },
        "extraction": extraction,
        "coverage": coverage,
        "ledger": ledger,
        "findings": findings,
        "rule_execution_states": _record61_rule_execution_states(
            findings,
            coverage=coverage,
            ledger=ledger,
            record_status_rows=record_status_rows,
        ),
    }


__all__ = [
    "EXPECTED_ALTERNATE_BOX_TRIPLETS",
    "EXPECTED_NATIVE_BOX_TRIPLETS",
    "EXPECTED_STATUS_ROWS",
    "Record61StatusInventoryError",
    "NUMERIC_TARGETS_BY_SAMPLE",
    "extract_record_61_status_rows",
    "run_record_61_full",
]
