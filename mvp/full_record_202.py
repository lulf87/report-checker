from __future__ import annotations

"""Full-table comparison for GB 9706.202 table-2 records.

The existing MVP deliberately sampled one late-document project.  This module
instead emits one ledger entry for every comparable table-2 row.  It remains
conservative: a row whose Ink cannot be assigned and classified uniquely is a
manual-review row, never an inferred pass.
"""

import hashlib
import math
import re
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:
    import pymupdf as fitz
except ImportError:  # PyMuPDF < 1.24 exposes the same API as ``fitz``.
    import fitz

from mvp.checker import (
    SAMPLE_CONFIGS,
    classify_report_number_field,
    compact,
    display_text,
    extract_report_number,
    find_exact_rect,
    header_map,
    is_formal_report_table,
    local_text_ocr,
    number_evidence_rects,
    page_three_fields,
    sha256_file,
    verified_202_legend_mapping,
)
from mvp.record_full import (
    CoverageEntry,
    compare_final_percentage,
    compare_numeric_observation,
    compare_record202_status_result,
    convert_decimal,
    extract_percentage_values,
    extract_unit_context,
    parse_report_numeric,
    validate_coverage,
)


EXPECTED_ITEM_ROW_COUNTS: dict[int, int] = {
    1: 1,
    2: 2,
    3: 4,
    4: 1,
    5: 1,
    6: 4,
    7: 2,
    8: 5,
    9: 1,
    10: 24,
    11: 15,
    12: 1,
    13: 9,
    14: 16,
    15: 1,
    16: 2,
    17: 1,
    18: 1,
    19: 1,
    20: 11,
    21: 1,
    22: 8,
    23: 1,
    24: 14,
    25: 1,
    26: 1,
    27: 1,
    28: 1,
    29: 2,
    30: 1,
    31: 2,
    32: 3,
    33: 8,
    34: 10,
    35: 1,
    36: 5,
    37: 10,
    38: 2,
}

EXPECTED_COMPARISON_UNITS = sum(EXPECTED_ITEM_ROW_COUNTS.values())
EXPECTED_REPORT_PHYSICAL_ROWS = EXPECTED_COMPARISON_UNITS + 1
EXPECTED_NUMBER_ROWS = 24

# Table 2 has no dedicated product/metadata cover.  These fields are still
# emitted as explicit scope objects so a missing Record-side value is visible
# as ``not_applicable`` (or ``manual`` when extraction is ambiguous), rather
# than silently disappearing from the comparison plan.
RECORD202_IDENTITY_FIELDS = (
    "样品名称",
    "型号规格",
    "产品编号/批号",
    "生产单位",
    "受检单位",
)
RECORD202_METADATA_FIELDS = (
    "生产日期",
    "到样日期",
    "检验日期",
    "检验地点",
    "签发日期",
    "检测仪器",
    "检验",
    "审核",
    "批准",
    "备注",
)
RECORD202_SCOPE_FIELDS = RECORD202_IDENTITY_FIELDS + RECORD202_METADATA_FIELDS

# The identities are frozen from the three approved GB 9706.202 templates.  Each
# digest covers the ordered (parent clause, Chinese requirement text) sequence
# for one Record item.  Keeping the digest independent of filled-in numbers and
# PDF math-font artefacts lets all three known templates pass while still
# detecting inserted, removed, reordered, or replaced requirements.
FROZEN_RECORD_ITEM_IDENTITIES: dict[int, str] = {
    1: "0e3da5ec4e3919d9", 2: "5c9b1c4368e90d28", 3: "41bb0a0c10dfcb74",
    4: "3f96c46efb58ebd3", 5: "160f2454e712d14a", 6: "f3825239828f24e0",
    7: "aabccb51343a8094", 8: "d1972a8f673dc622", 9: "c63c0a714d64103c",
    10: "da3f4a26c947d394", 11: "b45dae67f6664729", 12: "9ffe1c4ba0696c46",
    13: "573820c76cbc7235", 14: "78a16f0a46baad51", 15: "8485cba0a2e20c84",
    16: "dba73fe8aaa38d32", 17: "6757c949116edae9", 18: "9d69c6bda36174fd",
    19: "e1b63def8ba65d4f", 20: "d8fd7b53dae8a97f", 21: "6a7b51ed921cc25d",
    22: "03eb2ce4e4da33e5", 23: "413b18b36a7f863f", 24: "18bdb8242d7c0c80",
    25: "3d920f0506a1015e", 26: "7632bdecac5b118d", 27: "79f489340895511e",
    28: "0bae90caa1968343", 29: "2f2b8c55582ae1da", 30: "b118bd254cc2e916",
    31: "afaddab15b13f317", 32: "580dcb5f11b55302", 33: "cc6ae0ffe807014b",
    34: "4bd7c48ba50d8d1b", 35: "63083d588cc5120b", 36: "1f183e33695ac36a",
    37: "18427768e8093bff", 38: "5353fac2e8e1689f",
}

FROZEN_REPORT_ITEM_IDENTITIES: dict[int, str] = {
    1: "0e3da5ec4e3919d9", 2: "5c9b1c4368e90d28", 3: "41bb0a0c10dfcb74",
    4: "3f96c46efb58ebd3", 5: "160f2454e712d14a", 6: "f3825239828f24e0",
    7: "aabccb51343a8094", 8: "d74f08770acda0fc", 9: "c63c0a714d64103c",
    10: "da3f4a26c947d394", 11: "b45dae67f6664729", 12: "9ffe1c4ba0696c46",
    13: "573820c76cbc7235", 14: "78a16f0a46baad51", 15: "83172244a6ecadda",
    16: "4a82e0c5c52acc14", 17: "6757c949116edae9", 18: "9d69c6bda36174fd",
    19: "e1b63def8ba65d4f", 20: "d8fd7b53dae8a97f", 21: "6a7b51ed921cc25d",
    22: "efe462125b3c0bbd", 23: "413b18b36a7f863f", 24: "18bdb8242d7c0c80",
    25: "3d920f0506a1015e", 26: "7632bdecac5b118d", 27: "79f489340895511e",
    28: "0bae90caa1968343", 29: "2f2b8c55582ae1da", 30: "b118bd254cc2e916",
    31: "afaddab15b13f317", 32: "580dcb5f11b55302", 33: "cc6ae0ffe807014b",
    34: "4bd7c48ba50d8d1b", 35: "63083d588cc5120b", 36: "1f183e33695ac36a",
    37: "f61ffb4b751fbd75", 38: "5353fac2e8e1689f",
}

@dataclass(frozen=True)
class ColumnBands:
    sequence: fitz.Rect
    project: fitz.Rect
    clause: fitz.Rect
    requirement: fitz.Rect
    result: fitz.Rect


def _rect(value: Sequence[float] | fitz.Rect) -> list[float]:
    rect = fitz.Rect(value)
    return [round(rect.x0, 3), round(rect.y0, 3), round(rect.x1, 3), round(rect.y1, 3)]


def _union(rectangles: Iterable[fitz.Rect]) -> fitz.Rect | None:
    values = [fitz.Rect(value) for value in rectangles]
    if not values:
        return None
    result = fitz.Rect(values[0])
    for value in values[1:]:
        result |= value
    return result


def _same_x(cell: Sequence[float], band: fitz.Rect, tolerance: float = 1.1) -> bool:
    rect = fitz.Rect(cell)
    return abs(rect.x0 - band.x0) <= tolerance and abs(rect.x1 - band.x1) <= tolerance


def _table_value_for_band(
    extracted_row: Sequence[Any],
    cells: Sequence[Sequence[float] | None],
    band: fitz.Rect,
) -> tuple[str, fitz.Rect | None]:
    for index, cell in enumerate(cells):
        if cell is None or not _same_x(cell, band):
            continue
        value = display_text(extracted_row[index]) if index < len(extracted_row) else ""
        return value, fitz.Rect(cell)
    return "", None


def _header_cell(
    table: fitz.table.Table,
    rows: Sequence[Sequence[Any]],
    row_index: int,
    label: str,
) -> fitz.Rect:
    normalized_label = compact(label)
    for index, value in enumerate(rows[row_index]):
        if compact(value) != normalized_label:
            continue
        cell = table.rows[row_index].cells[index]
        if cell is not None:
            return fitz.Rect(cell)
    raise ValueError(f"record table header {label!r} not found")


def _record_column_bands(page: fitz.Page, table: fitz.table.Table) -> ColumnBands:
    """Resolve columns from header text and geometry, including 6/7-column pages."""

    rows = table.extract()
    sequence = _header_cell(table, rows, 0, "序号")
    project = _header_cell(table, rows, 1, "检验项目")
    clause = _header_cell(table, rows, 1, "条款号")
    requirement = _header_cell(table, rows, 1, "标准要求")

    header_fragments = [fitz.Rect(value) for value in page.search_for("实测数据")]
    if not header_fragments:
        raise ValueError(f"PDF page {page.number + 1}: 实测数据 header not found")
    header_x = sum((rect.x0 + rect.x1) / 2 for rect in header_fragments) / len(header_fragments)
    candidates = [
        fitz.Rect(cell)
        for cell in table.rows[0].cells
        if cell is not None and fitz.Rect(cell).x0 <= header_x <= fitz.Rect(cell).x1
    ]
    if len(candidates) != 1:
        raise ValueError(
            f"PDF page {page.number + 1}: 实测数据 column is not unique ({len(candidates)})"
        )
    result = candidates[0]
    return ColumnBands(sequence, project, clause, requirement, result)


def _annotation_strokes(annotation: fitz.Annot) -> list[list[tuple[float, float]]]:
    return [
        [(float(point[0]), float(point[1])) for point in stroke]
        for stroke in (annotation.vertices or [])
        if stroke
    ]


def _path_length(stroke: Sequence[tuple[float, float]]) -> float:
    return sum(math.dist(stroke[index - 1], stroke[index]) for index in range(1, len(stroke)))


def _geometry_from_strokes(
    strokes: Sequence[Sequence[tuple[float, float]]],
    bbox: Sequence[float] | fitz.Rect,
) -> dict[str, Any]:
    points = [point for stroke in strokes for point in stroke]
    rect = fitz.Rect(bbox)
    diagonal = math.hypot(rect.width, rect.height) or 1.0
    path_length = sum(_path_length(stroke) for stroke in strokes)
    if len(strokes) == 1 and len(strokes[0]) >= 2:
        end_distance = math.dist(strokes[0][0], strokes[0][-1])
    else:
        end_distance = 0.0
    return {
        "bbox": _rect(rect),
        "stroke_count": len(strokes),
        "point_count": len(points),
        "path_length": round(path_length, 4),
        "end_distance": round(end_distance, 4),
        "open_ratio": round(end_distance / diagonal, 4),
        "end_path_ratio": round(end_distance / path_length, 4) if path_length else 0.0,
        "points": points,
        "strokes": strokes,
    }


def _ink_geometry(annotation: fitz.Annot) -> dict[str, Any]:
    return _geometry_from_strokes(_annotation_strokes(annotation), annotation.rect)


def _stroke_is_linear(stroke: Sequence[tuple[float, float]]) -> bool:
    if len(stroke) < 2:
        return False
    length = _path_length(stroke)
    return bool(length) and math.dist(stroke[0], stroke[-1]) / length >= 0.9


def _two_strokes_cross(strokes: Sequence[Sequence[tuple[float, float]]]) -> bool:
    if len(strokes) != 2 or not all(_stroke_is_linear(stroke) for stroke in strokes):
        return False
    first_start, first_end = strokes[0][0], strokes[0][-1]
    second_start, second_end = strokes[1][0], strokes[1][-1]
    first_vector = (
        first_end[0] - first_start[0],
        first_end[1] - first_start[1],
    )
    second_vector = (
        second_end[0] - second_start[0],
        second_end[1] - second_start[1],
    )
    determinant = (
        first_vector[0] * second_vector[1]
        - first_vector[1] * second_vector[0]
    )
    first_length = math.hypot(*first_vector)
    second_length = math.hypot(*second_vector)
    if not first_length or not second_length:
        return False
    # A shallow overlap between two handwriting strokes is not an ×.  The
    # strokes must meet at a material angle and cross through both interiors.
    if abs(determinant) / (first_length * second_length) < 0.45:
        return False
    offset = (
        second_start[0] - first_start[0],
        second_start[1] - first_start[1],
    )
    first_position = (
        offset[0] * second_vector[1] - offset[1] * second_vector[0]
    ) / determinant
    second_position = (
        offset[0] * first_vector[1] - offset[1] * first_vector[0]
    ) / determinant
    return bool(
        0.10 <= first_position <= 0.90
        and 0.10 <= second_position <= 0.90
    )


def classify_202_ink_geometry(geometry: Mapping[str, Any]) -> str:
    """Classify only high-confidence legal marks; everything else stays unknown."""

    stroke_count = int(geometry["stroke_count"])
    open_ratio = float(geometry["open_ratio"])
    end_path_ratio = float(geometry["end_path_ratio"])
    if _two_strokes_cross(geometry["strokes"]):
        return "×"
    if stroke_count == 1 and end_path_ratio >= 0.92 and open_ratio >= 0.85:
        return "/"
    if stroke_count == 1 and open_ratio <= 0.40 and end_path_ratio <= 0.18:
        return "△"
    if (
        stroke_count == 1
        and open_ratio >= 0.60
        and 0.45 <= end_path_ratio < 0.92
    ):
        return "√"
    return "其他"


def _public_ink(annotation: fitz.Annot) -> dict[str, Any]:
    geometry = _ink_geometry(annotation)
    symbol = classify_202_ink_geometry(geometry)
    return {
        "bbox": geometry["bbox"],
        "stroke_count": geometry["stroke_count"],
        "point_count": geometry["point_count"],
        "path_length": geometry["path_length"],
        "open_ratio": geometry["open_ratio"],
        "end_path_ratio": geometry["end_path_ratio"],
        "classified_symbol": symbol,
        "_points": geometry["points"],
        "_strokes": geometry["strokes"],
    }


def _assign_page_inks(page: fitz.Page, rows: list[dict[str, Any]]) -> None:
    """Assign each Ink to at most one row by point ownership, not centroid."""

    for annotation in page.annots() or []:
        if len(annotation.type) < 2 or annotation.type[1] != "Ink":
            continue
        ink = _public_ink(annotation)
        points = ink["_points"]
        scores: list[tuple[int, float, int]] = []
        annotation_rect = fitz.Rect(annotation.rect)
        annotation_center_y = (annotation_rect.y0 + annotation_rect.y1) / 2
        for index, row in enumerate(rows):
            cell = fitz.Rect(row["result_bbox"])
            expanded = fitz.Rect(cell.x0 - 1.5, cell.y0 - 1.5, cell.x1 + 1.5, cell.y1 + 1.5)
            inside = sum(expanded.contains(fitz.Point(*point)) for point in points)
            if inside:
                scores.append((inside, -abs(annotation_center_y - (cell.y0 + cell.y1) / 2), index))
        if not scores:
            continue
        scores.sort(reverse=True)
        selected_count, _, selected_index = scores[0]
        runner_up_count = scores[1][0] if len(scores) > 1 else 0
        ink["assigned_point_count"] = selected_count
        ink["runner_up_point_count"] = runner_up_count
        ink["cross_row_ambiguous"] = bool(
            runner_up_count and runner_up_count / selected_count >= 0.75
        )
        rows[selected_index]["inks"].append(ink)


def _requirement_fingerprint(value: str) -> str:
    return hashlib.sha256(compact(value).encode("utf-8")).hexdigest()


def _requirement_identity_text(value: str) -> str:
    """Return the stable semantic spine used only for template identity.

    Filled measurements and PDF-specific math glyphs vary among the approved
    reports, while the Chinese requirement wording remains stable.
    """

    return "".join(re.findall(r"[\u3400-\u9fff]+", value or ""))


def _item_template_fingerprint(rows: Sequence[Mapping[str, Any]]) -> str:
    identity = "\x1e".join(
        f"{compact(row.get('parent_clause'))}\x1f"
        f"{_requirement_identity_text(str(row.get('requirement', '')))}"
        for row in rows
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def extract_record_202_rows(document: fitz.Document) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows_out: list[dict[str, Any]] = []
    active_item: int | None = None
    active_project = ""
    active_clause = ""
    occurrence: Counter[int] = Counter()
    table_shapes: dict[int, list[int]] = {}
    result_columns: dict[int, list[float]] = {}

    for page in document:
        tables = [
            (table_index, table)
            for table_index, table in enumerate(page.find_tables().tables, start=1)
            if table.row_count >= 3
        ]
        if not tables:
            continue
        table_index, table = max(
            tables,
            key=lambda candidate: candidate[1].row_count * candidate[1].col_count,
        )
        extracted = table.extract()
        bands = _record_column_bands(page, table)
        table_shapes[page.number + 1] = [table.row_count, table.col_count]
        result_columns[page.number + 1] = [round(bands.result.x0, 3), round(bands.result.x1, 3)]
        page_rows: list[dict[str, Any]] = []

        for row_index in range(2, len(extracted)):
            values = extracted[row_index]
            cells = table.rows[row_index].cells
            sequence_text, _ = _table_value_for_band(values, cells, bands.sequence)
            sequence_match = re.fullmatch(r"(?:续)?(\d+)", compact(sequence_text))
            if sequence_match:
                active_item = int(sequence_match.group(1))
                project, _ = _table_value_for_band(values, cells, bands.project)
                clause, _ = _table_value_for_band(values, cells, bands.clause)
                if project:
                    active_project = project
                if clause:
                    active_clause = clause
            else:
                project, _ = _table_value_for_band(values, cells, bands.project)
                clause, _ = _table_value_for_band(values, cells, bands.clause)
                if project:
                    active_project = project
                if clause:
                    active_clause = clause

            if active_item is None:
                continue
            result_text, result_cell = _table_value_for_band(values, cells, bands.result)
            if result_cell is None:
                continue

            requirement_parts: list[str] = []
            requirement_rects: list[fitz.Rect] = []
            for index, cell_value in enumerate(cells):
                if cell_value is None:
                    continue
                cell = fitz.Rect(cell_value)
                if cell.x0 < bands.requirement.x0 - 1 or cell.x1 > bands.requirement.x1 + 1:
                    continue
                if cell.x0 < bands.requirement.x0 - 1:
                    continue
                value = display_text(values[index]) if index < len(values) else ""
                if value:
                    requirement_parts.append(value)
                    requirement_rects.append(cell)
            requirement = " ".join(requirement_parts)
            if not requirement:
                continue
            if active_item == 8 and compact(requirement) == "颜色含义":
                continue

            occurrence[active_item] += 1
            requirement_bbox = _union(requirement_rects)
            row = {
                "row_id": (
                    f"record202:p{page.number + 1:03d}:t{table_index:02d}:"
                    f"r{row_index:03d}:i{active_item:02d}"
                ),
                "item": active_item,
                "project": active_project,
                "parent_clause": active_clause,
                "logical_row": occurrence[active_item],
                "requirement": requirement,
                "requirement_fingerprint": _requirement_fingerprint(requirement),
                "pdf_page": page.number + 1,
                "requirement_bbox": _rect(requirement_bbox) if requirement_bbox else None,
                "result_bbox": _rect(result_cell),
                "native_result_text": result_text,
                "inks": [],
            }
            page_rows.append(row)
            rows_out.append(row)

        _assign_page_inks(page, page_rows)

    counts = Counter(row["item"] for row in rows_out)
    return rows_out, {
        "page_count": document.page_count,
        "item_count": len(counts),
        "row_count": len(rows_out),
        "item_row_counts": dict(sorted(counts.items())),
        "table_shapes": table_shapes,
        "result_columns": result_columns,
        "column_detection": "实测数据_header_text_and_table_geometry",
    }


def extract_report_202_rows(document: fitz.Document) -> dict[int, list[dict[str, Any]]]:
    by_item: dict[int, list[dict[str, Any]]] = defaultdict(list)
    active_item: int | None = None
    active_project = ""
    active_clause = ""

    for page in document:
        for table_index, table in enumerate(page.find_tables().tables, start=1):
            if not is_formal_report_table(table):
                continue
            extracted = table.extract()
            mapping = header_map(extracted[0])
            for row_index, row in enumerate(extracted[1:], start=1):
                sequence_text = compact(row[mapping["序号"]])
                match = re.fullmatch(r"(?:续)?(\d+)", sequence_text)
                if match:
                    active_item = int(match.group(1))
                if active_item is None or not 119 <= active_item <= 156:
                    continue
                project = display_text(row[mapping["检验项目"]])
                clause = display_text(row[mapping["标准条款"]])
                if project:
                    active_project = project
                if clause:
                    active_clause = clause
                requirement = display_text(row[mapping["标准要求"]])
                if not requirement:
                    continue
                result = display_text(row[mapping["检验结果"]])
                requirement_cell = table.rows[row_index].cells[mapping["标准要求"]]
                result_cell = table.rows[row_index].cells[mapping["检验结果"]]
                by_item[active_item].append(
                    {
                        "row_id": (
                            f"report:p{page.number + 1:03d}:t{table_index:02d}:"
                            f"r{row_index:03d}:s{active_item:03d}"
                        ),
                        "item": active_item,
                        "project": active_project,
                        "parent_clause": active_clause,
                        "physical_row": len(by_item[active_item]) + 1,
                        "requirement": requirement,
                        "requirement_fingerprint": _requirement_fingerprint(requirement),
                        "result": result,
                        "pdf_page": page.number + 1,
                        "requirement_bbox": _rect(requirement_cell) if requirement_cell else None,
                        "result_bbox": _rect(result_cell) if result_cell else None,
                    }
                )
    return dict(by_item)


def _record_202_structure(document: fitz.Document) -> dict[str, Any]:
    """Capture page/table/header structure as an explicit scope check."""

    pages: dict[str, Any] = {}
    missing: list[int] = []
    required_header_names = ("序号", "检验项目", "条款号", "标准要求", "实测数据")
    for page_number in range(1, EXPECTED_NUMBER_ROWS + 1):
        page = document[page_number - 1] if page_number <= document.page_count else None
        tables = page.find_tables().tables if page is not None else []
        if not tables:
            missing.append(page_number)
        page_text = compact(page.get_text("text")) if page is not None else ""
        pages[str(page_number)] = {
            "table_count": len(tables),
            "table_shapes": [[int(table.row_count), int(table.col_count)] for table in tables],
            "required_headers": {
                name: name in page_text for name in required_header_names
            },
        }
    header_complete_pages = [
        page_number
        for page_number, value in pages.items()
        if all(value["required_headers"].values())
    ]
    page_sequence = [int(page_number) for page_number, value in pages.items() if value["table_count"]]
    return {
        "expected_page_count": EXPECTED_NUMBER_ROWS,
        "observed_page_count": document.page_count,
        "missing_pages": missing,
        "pages": pages,
        "required_headers": list(required_header_names),
        "header_complete_pages": header_complete_pages,
        "table_page_sequence": page_sequence,
        "page_sequence_contiguous": page_sequence == list(range(1, EXPECTED_NUMBER_ROWS + 1)),
        "validated": (
            document.page_count == EXPECTED_NUMBER_ROWS
            and not missing
            and len(header_complete_pages) == EXPECTED_NUMBER_ROWS
            and page_sequence == list(range(1, EXPECTED_NUMBER_ROWS + 1))
        ),
        "header_policy": "headers are resolved by text plus table geometry; continuation headers permitted",
    }


def _report_202_structure(document: fitz.Document) -> dict[str, Any]:
    pages: dict[str, Any] = {}
    formal_pages: list[int] = []
    formal_table_shapes: dict[str, list[list[int]]] = {}
    for page in document:
        tables = [table for table in page.find_tables().tables if is_formal_report_table(table)]
        if tables:
            formal_pages.append(page.number + 1)
        formal_table_shapes[str(page.number + 1)] = [
            [int(table.row_count), int(table.col_count)] for table in tables
        ]
        pages[str(page.number + 1)] = {
            "formal_table_count": len(tables),
            "table_shapes": [[int(table.row_count), int(table.col_count)] for table in tables],
        }
    page_sequence_contiguous = bool(formal_pages) and formal_pages == list(range(formal_pages[0], formal_pages[-1] + 1))
    return {
        "observed_page_count": document.page_count,
        "formal_table_pages": formal_pages,
        "formal_table_count": sum(item["formal_table_count"] for item in pages.values()),
        "formal_table_shapes": formal_table_shapes,
        "formal_page_sequence_contiguous": page_sequence_contiguous,
        "pages": pages,
        "validated": bool(formal_pages),
    }


def _line_value_after_label(page: fitz.Page, label: str) -> tuple[str, fitz.Rect | None]:
    """Extract a label/value pair from non-table cover text conservatively."""

    lines = [line.strip() for line in page.get_text("text").splitlines() if line.strip()]
    target = compact(label)
    for index, line in enumerate(lines):
        normalized = compact(line)
        if normalized != target and not normalized.startswith(target + ":") and not normalized.startswith(target + "："):
            continue
        suffix = normalized[len(target):].lstrip(":：")
        if suffix:
            return suffix, find_exact_rect(page, suffix)
        if index + 1 < len(lines):
            value = lines[index + 1]
            next_normalized = compact(value)
            if (
                next_normalized not in set(RECORD202_SCOPE_FIELDS)
                and next_normalized not in {target}
                and ":" not in next_normalized
                and "：" not in next_normalized
            ):
                return value, find_exact_rect(page, value)
    return "", None


def _report_202_fixed_fields(document: fitz.Document) -> dict[str, dict[str, Any]]:
    page = document[2] if document.page_count >= 3 else document[0]
    fields = page_three_fields(page)
    result: dict[str, dict[str, Any]] = {}
    for field in RECORD202_SCOPE_FIELDS:
        value, bbox = fields.get(field, ("", None))
        if not value:
            value, bbox = _line_value_after_label(page, field)
        result[field] = {
            "value": value,
            "pdf_page": page.number + 1,
            "bbox": _rect(bbox) if bbox is not None else _rect(page.rect),
        }
    return result


def _record_202_fixed_fields(document: fitz.Document) -> dict[str, dict[str, Any]]:
    """Find fixed fields if a future Table 2 template exposes them.

    Current approved templates expose only the per-page report number.  Keeping
    this extractor explicit makes that absence an auditable not_applicable
    disposition instead of silently omitting identity/metadata scope objects.
    """

    result: dict[str, dict[str, Any]] = {}
    page_lines = [
        (page, [line.strip() for line in page.get_text("text").splitlines() if line.strip()])
        for page in document
    ]
    for field in RECORD202_SCOPE_FIELDS:
        value = ""
        page_number = 1
        bbox = _rect(document[0].rect)
        target = compact(field)
        for page, lines in page_lines:
            for index, line in enumerate(lines):
                normalized = compact(line)
                if normalized != target and not normalized.startswith(target + ":") and not normalized.startswith(target + "："):
                    continue
                candidate = normalized[len(target):].lstrip(":：")
                if candidate:
                    value = candidate
                    page_number = page.number + 1
                    candidate_bbox = find_exact_rect(page, candidate)
                    bbox = _rect(candidate_bbox) if candidate_bbox is not None else _rect(page.rect)
                    break
            if value:
                break
        result[field] = {"value": value, "pdf_page": page_number, "bbox": bbox}
    return result


def _scope_disposition(decision: str) -> str:
    return {
        "match": "matched",
        "mismatch": "mismatch",
        "manual": "manual",
        "not_applicable": "not_applicable",
        "excluded": "excluded",
    }[decision]


def _expanded_scope_ledger(
    record_doc: fitz.Document,
    report_doc: fitz.Document,
    *,
    record_rows: Sequence[Mapping[str, Any]],
    report_groups: Mapping[int, Sequence[Sequence[Mapping[str, Any]]]],
    body_ledger: Sequence[Mapping[str, Any]],
    record_structure: Mapping[str, Any],
    report_structure: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build object-level S38/S39/S42/S43/S44/S45/S46 scope coverage."""

    scope_ledger: list[dict[str, Any]] = []
    coverage_entries: list[CoverageEntry] = []
    source_ids: list[str] = []
    report_ids: list[str] = []
    categories: Counter[str] = Counter()

    def add_entry(entry: dict[str, Any], source_id: str, target_ids: Sequence[str]) -> None:
        scope_ledger.append(entry)
        source_ids.append(source_id)
        if not target_ids:
            target_ids = [f"{source_id}:target"]
        for target_id in target_ids:
            report_ids.append(target_id)
            coverage_entries.append(
                CoverageEntry(
                    source_id,
                    target_id,
                    entry["disposition"],
                    entry["reason_code"],
                    (entry["id"],),
                )
            )

    body_by_source = {str(row["source_row_id"]): row for row in body_ledger}
    record_by_item: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for row in record_rows:
        record_by_item[int(row["item"])].append(row)
    # Mapping fields and actual values are retained for every Record row.
    for record_row in record_rows:
        item = int(record_row["item"])
        ordinal = int(record_row["logical_row"])
        groups = list(report_groups.get(item, ()))
        report_group = groups[ordinal - 1] if ordinal <= len(groups) else []
        fields = _mapping_field_comparisons(record_row, report_group)
        legacy = body_by_source.get(str(record_row["row_id"]), {})
        field_decisions = [str(item.get("decision", "manual")) for item in fields.values()]
        if "mismatch" in field_decisions:
            decision = "mismatch"
            field_reason = next(
                (str(item.get("reason_code")) for item in fields.values() if item.get("decision") == "mismatch"),
                "scope_field_difference",
            )
        elif "manual" in field_decisions or not report_group:
            decision = "manual"
            field_reason = next(
                (str(item.get("reason_code")) for item in fields.values() if item.get("decision") == "manual"),
                "scope_field_manual",
            )
        else:
            decision = "match"
            field_reason = "scope_fields_match"
        disposition = _scope_disposition(decision)
        source_id = f"{record_row['row_id']}:scope"
        target_ids = [f"{row['row_id']}:scope" for row in report_group]
        entry_id = f"RECORD202-SCOPE-I{item:02d}-R{ordinal:02d}"
        add_entry(
            {
                "entry_type": "scope",
                "scope_category": "mapping",
                "id": entry_id,
                "entry_id": entry_id,
                "rule_id": "RECORD202-SCOPE",
                "source_row_id": str(record_row["row_id"]),
                "target_row_ids": target_ids,
                "disposition": disposition,
                "decision": decision,
                "reason_code": field_reason,
                "scope_ids": ["S38", "S39"],
                "record_location": {"pdf_page": record_row["pdf_page"], "bbox": record_row["result_bbox"]},
                "report_locations": [
                    {"pdf_page": row["pdf_page"], "bbox": row["result_bbox"]}
                    for row in report_group
                ] or [{"pdf_page": 1, "bbox": _rect(report_doc[0].rect)}],
                "record": {
                    "item": item,
                    "logical_row": ordinal,
                    "project": record_row.get("project", ""),
                    "parent_clause": record_row.get("parent_clause", ""),
                    "requirement": record_row.get("requirement", ""),
                },
                "report": [
                    {
                        "item": row.get("item"),
                        "physical_row": row.get("physical_row"),
                        "project": row.get("project", ""),
                        "parent_clause": row.get("parent_clause", ""),
                        "requirement": row.get("requirement", ""),
                    }
                    for row in report_group
                ],
                "field_comparisons": fields,
            },
            source_id,
            target_ids,
        )
        categories["mapping"] += 1

        # Make every report-side numeric or acceptance token a first-class
        # object. Multiple tokens remain manual unless a unique Record value
        # can be assigned; this avoids silently comparing the wrong number.
        for target_ordinal, report_row in enumerate(report_group, start=1):
            result_text = str(report_row.get("result", ""))
            requirement_text = str(report_row.get("requirement", ""))
            result_unit_context = extract_unit_context(requirement_text)
            result_tokens = _extract_numeric_tokens(result_text, unit_context=result_unit_context)
            requirement_tokens = [
                _numeric_token_dict(match, result_unit_context)
                for match in _NATIVE_MEASUREMENT_RE.finditer(compact(requirement_text))
            ]
            if not result_tokens and not requirement_tokens:
                continue
            record_text = str(record_row.get("native_result_text", ""))
            record_tokens = _extract_numeric_tokens(record_text, unit_context=result_unit_context)
            acceptance = _acceptance_constraints(requirement_text)
            if "表3" in compact(record_text):
                numeric_decision, numeric_disposition, reason = "excluded", "excluded", "TABLE3_OUT_OF_SCOPE"
            elif len(result_tokens) == 1:
                candidate = _native_measurement_candidate(record_text)
                if candidate is None:
                    numeric_decision, numeric_disposition, reason = "manual", "manual", "record_numeric_not_uniquely_resolved"
                else:
                    parsed_candidate = parse_report_numeric(candidate, result_unit_context)
                    if parsed_candidate is None:
                        numeric_decision, numeric_disposition, reason = "manual", "manual", "record_numeric_not_parsed"
                    else:
                        comparison = compare_numeric_observation(
                            parsed_candidate.value,
                            parsed_candidate.unit,
                            result_text,
                            result_unit_context,
                        )
                        numeric_decision = comparison.decision
                        numeric_disposition = _scope_disposition(numeric_decision)
                        reason = comparison.reason_code
                        acceptance_comparison = _compare_record_to_acceptance(record_text, requirement_text)
                        if acceptance_comparison["decision"] == "mismatch":
                            numeric_decision, numeric_disposition, reason = "mismatch", "mismatch", acceptance_comparison["reason_code"]
                        elif acceptance_comparison["decision"] == "manual" and numeric_decision == "match":
                            numeric_decision, numeric_disposition, reason = "manual", "manual", acceptance_comparison["reason_code"]
            elif not result_tokens and requirement_tokens:
                numeric_decision, numeric_disposition, reason = "manual", "manual", "report_numeric_result_missing"
            elif result_tokens or requirement_tokens:
                numeric_decision, numeric_disposition, reason = "manual", "manual", "numeric_target_not_uniquely_resolved"
            entry_id = f"RECORD202-NUMERIC-I{item:02d}-R{ordinal:02d}-T{target_ordinal:02d}"
            source_id = f"{record_row['row_id']}:numeric:{target_ordinal}"
            target_id = f"{report_row['row_id']}:numeric:{target_ordinal}"
            add_entry(
                {
                    "entry_type": "scope",
                    "scope_category": "numeric_target",
                    "id": entry_id,
                    "entry_id": entry_id,
                    "rule_id": "RECORD202-BODY-PERCENT" if ("%" in result_text or "％" in result_text or "%" in requirement_text or "％" in requirement_text) else "RECORD202-BODY-NUMERIC",
                    "source_row_id": str(record_row["row_id"]),
                    "target_row_id": str(report_row["row_id"]),
                    "target_row_ids": [str(report_row["row_id"])],
                    "disposition": numeric_disposition,
                    "decision": numeric_decision,
                    "reason_code": reason,
                    "scope_ids": ["S48"] if numeric_decision == "excluded" else ["S42", "S43"],
                    "record_location": {"pdf_page": record_row["pdf_page"], "bbox": record_row["result_bbox"]},
                    "report_locations": [{"pdf_page": report_row["pdf_page"], "bbox": report_row["result_bbox"]}],
                    "record": {"raw": record_text, "numeric_tokens": record_tokens},
                    "report": {
                        "raw_result": result_text,
                        "result_tokens": result_tokens,
                        "acceptance_text": requirement_text,
                        "acceptance_tokens": requirement_tokens,
                        "acceptance": acceptance,
                    },
                },
                source_id,
                [target_id],
            )
            categories["numeric_target"] += 1

    # Structure has two sides and must remain explicit even when a table is
    # malformed.  It is kept separate from the row mapping ledger.
    structure_decision = "match" if record_structure.get("validated") and report_structure.get("validated") else "manual"
    structure_disposition = _scope_disposition(structure_decision)
    structure_reason = "record_report_structure_match" if structure_decision == "match" else "record_report_structure_requires_review"
    add_entry(
        {
            "entry_type": "scope",
            "scope_category": "structure",
            "id": "RECORD202-STRUCTURE-SCOPE",
            "entry_id": "RECORD202-STRUCTURE-SCOPE",
            "rule_id": "RECORD202-STRUCTURE",
            "source_row_id": "record202:structure",
            "target_row_id": "report:structure",
            "target_row_ids": ["report:structure"],
            "disposition": structure_disposition,
            "decision": structure_decision,
            "reason_code": structure_reason,
            "scope_ids": ["S44"],
            "record_location": {"pdf_page": 1, "bbox": _rect(record_doc[0].rect)},
            "report_locations": [{"pdf_page": 1, "bbox": _rect(report_doc[0].rect)}],
            "record": dict(record_structure),
            "report": dict(report_structure),
        },
        "record202:structure",
        ["report:structure"],
    )
    categories["structure"] += 1

    # Fixed identity and metadata are object-level scope entries even though
    # current Table 2 PDFs do not expose those fields on the Record side.
    record_fixed = _record_202_fixed_fields(record_doc)
    report_fixed = _report_202_fixed_fields(report_doc)
    for category, fields, rule_id in (
        ("identity", RECORD202_IDENTITY_FIELDS, "RECORD202-IDENTITY"),
        ("metadata", RECORD202_METADATA_FIELDS, "RECORD202-METADATA"),
    ):
        for index, field in enumerate(fields, start=1):
            source = record_fixed[field]
            target = report_fixed[field]
            record_value = display_text(source.get("value", ""))
            report_value = display_text(target.get("value", ""))
            if record_value and report_value:
                comparison = _field_disposition(record_value, [report_value], field=field)
                decision = comparison["decision"]
                disposition = comparison["disposition"]
                reason = comparison["reason_code"]
            elif not record_value and not report_value:
                decision, disposition, reason = "not_applicable", "not_applicable", "field_not_present_in_either_document"
            elif not record_value:
                decision, disposition, reason = "not_applicable", "not_applicable", "record_202_table2_field_not_present"
            else:
                decision, disposition, reason = "manual", "manual", "report_fixed_field_missing"
            entry_id = f"{rule_id}-{index:02d}"
            source_id = f"record202:fixed:{category}:{field}"
            target_id = f"report:fixed:{category}:{field}"
            add_entry(
                {
                    "entry_type": "scope",
                    "scope_category": category,
                    "id": entry_id,
                    "entry_id": entry_id,
                    "rule_id": rule_id,
                    "source_row_id": source_id,
                    "target_row_id": target_id,
                    "target_row_ids": [target_id],
                    "disposition": disposition,
                    "decision": decision,
                    "reason_code": reason,
                    "scope_ids": ["S45" if category == "identity" else "S46"],
                    "record_location": {"pdf_page": source["pdf_page"], "bbox": source["bbox"]},
                    "report_locations": [{"pdf_page": target["pdf_page"], "bbox": target["bbox"]}],
                    "record": {"field": field, "value": record_value},
                    "report": {"field": field, "value": report_value},
                },
                source_id,
                [target_id],
            )
            categories[category] += 1

    coverage = validate_coverage(source_ids, report_ids, coverage_entries)
    decisions = Counter(entry["decision"] for entry in scope_ledger)
    coverage["categories"] = dict(categories)
    coverage["decision_counts"] = {
        "match": decisions["match"],
        "mismatch": decisions["mismatch"],
        "manual": decisions["manual"],
        "not_applicable": decisions["not_applicable"],
        "excluded": decisions["excluded"],
    }
    coverage["ledger_entries"] = len(scope_ledger)
    coverage["conserved"] = bool(
        coverage.get("source_rows", {}).get("conserved")
        and coverage.get("report_rows", {}).get("conserved")
    )
    return scope_ledger, coverage


def _legend_check(record_doc: fitz.Document) -> dict[str, Any]:
    """Verify the four 9706.202 symbols before using any status comparison."""

    page = record_doc[-1]
    native_lines = page.get_text("text").splitlines()
    mapping = verified_202_legend_mapping(native_lines)
    source = "native_pdf_text" if mapping is not None else "unresolved"
    ocr: dict[str, Any] | None = None
    if mapping is None:
        # OCR is intentionally best-effort; unresolved legend keeps status rows
        # manual through the existing symbol classifier.
        rects = [fitz.Rect(value) for value in page.search_for("注：")]
        rect = rects[0] if rects else fitz.Rect(0, max(0, page.rect.height - 100), page.rect.width, page.rect.height)
        try:
            from mvp.checker import render_cell_for_ocr
            # Keep OCR scratch files isolated per invocation. A fixed /tmp path
            # allowed concurrent runs to overwrite one another's legend image.
            with tempfile.TemporaryDirectory(prefix="record202-legend-") as scratch_dir:
                image_path = Path(scratch_dir) / "legend.png"
                render_cell_for_ocr(page, rect, image_path)
                ocr = local_text_ocr(image_path)
            mapping = verified_202_legend_mapping(
                line.get("text", "") if isinstance(line, Mapping) else str(line)
                for line in ocr.get("lines", [])
            )
            source = "apple_vision_local_ocr" if mapping is not None else "unresolved"
        except Exception as exc:
            ocr = {"error": str(exc), "lines": []}
    return {
        "validated": mapping is not None,
        "source": source,
        "mapping": mapping,
        "ocr": ocr,
        "pdf_page": page.number + 1,
        "expected_mapping": {"√": "符合要求", "×": "不符合要求", "△": "不适用", "/": "此项空白"},
        "bbox": _rect(fitz.Rect(0, max(0, page.rect.height - 100), page.rect.width, page.rect.height)),
    }


def build_report_202_groups(
    report_rows: Mapping[int, list[dict[str, Any]]],
) -> dict[int, list[list[dict[str, Any]]]]:
    groups: dict[int, list[list[dict[str, Any]]]] = {}
    for record_item in range(1, 39):
        report_item = 118 + record_item
        physical_rows = list(report_rows.get(report_item, []))
        if record_item == 8:
            # Report preserves the introductory row and nested "颜色" header.  They
            # have no result and are structural, not comparison units.
            physical_rows = physical_rows[2:]
            groups[record_item] = [[row] for row in physical_rows]
        elif record_item == 16:
            # The Record combines the first two Report requirements in one row.
            groups[record_item] = [physical_rows[:2], physical_rows[2:3]]
        else:
            groups[record_item] = [[row] for row in physical_rows]
    return groups


_LEGAL_RECORD_SYMBOLS = {"√", "×", "△", "/"}


def _ink_bbox(ink: Mapping[str, Any]) -> fitz.Rect | None:
    bbox = ink.get("bbox")
    if not isinstance(bbox, Sequence) or isinstance(bbox, (str, bytes)) or len(bbox) != 4:
        return None
    return fitz.Rect(bbox)


def _ink_strokes(ink: Mapping[str, Any]) -> list[list[tuple[float, float]]]:
    raw_strokes = ink.get("_strokes")
    if not isinstance(raw_strokes, Sequence) or isinstance(raw_strokes, (str, bytes)):
        return []
    strokes: list[list[tuple[float, float]]] = []
    for raw_stroke in raw_strokes:
        if not isinstance(raw_stroke, Sequence) or isinstance(raw_stroke, (str, bytes)):
            continue
        stroke: list[tuple[float, float]] = []
        for raw_point in raw_stroke:
            if not isinstance(raw_point, Sequence) or len(raw_point) < 2:
                continue
            stroke.append((float(raw_point[0]), float(raw_point[1])))
        if stroke:
            strokes.append(stroke)
    return strokes


def _ink_pair_forms_cross(first: Mapping[str, Any], second: Mapping[str, Any]) -> bool:
    strokes = _ink_strokes(first) + _ink_strokes(second)
    return _two_strokes_cross(strokes)


def _status_ink_clusters(
    inks: Sequence[Mapping[str, Any]],
) -> list[list[Mapping[str, Any]]]:
    """Combine separate crossed strokes; leave unrelated handwriting isolated.

    Broad bounding-box clustering would merge a large check mark with text that
    merely lies inside its diagonal bounding rectangle.  The only multi-Ink
    legal mark accepted here is therefore an actual two-stroke cross.  Every
    other annotation remains an independent spatial cluster and must pass the
    conservative size gate below.
    """

    parents = list(range(len(inks)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(first: int, second: int) -> None:
        first_root = find(first)
        second_root = find(second)
        if first_root != second_root:
            parents[second_root] = first_root

    for first in range(len(inks)):
        for second in range(first + 1, len(inks)):
            if _ink_pair_forms_cross(inks[first], inks[second]):
                union(first, second)

    grouped: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for index, ink in enumerate(inks):
        grouped[find(index)].append(ink)
    return list(grouped.values())


def _status_cluster_geometry(cluster: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    rectangles = [rect for ink in cluster if (rect := _ink_bbox(ink)) is not None]
    bbox = _union(rectangles)
    strokes = [stroke for ink in cluster for stroke in _ink_strokes(ink)]
    if len(cluster) == 1:
        symbol = str(cluster[0].get("classified_symbol", "其他"))
    elif bbox is not None and strokes:
        symbol = classify_202_ink_geometry(_geometry_from_strokes(strokes, bbox))
    else:
        symbol = "其他"
    return {
        "symbol": symbol,
        "bbox": bbox,
        "cross_row_ambiguous": any(
            bool(ink.get("cross_row_ambiguous")) for ink in cluster
        ),
    }


def _is_high_confidence_status_cluster(cluster: Sequence[Mapping[str, Any]]) -> bool:
    geometry = _status_cluster_geometry(cluster)
    symbol = geometry["symbol"]
    if symbol not in _LEGAL_RECORD_SYMBOLS:
        return False
    bbox = geometry["bbox"]
    if bbox is None:
        # Unit-level callers historically supplied only a classified symbol.
        return True
    width = bbox.width
    height = bbox.height
    area = bbox.get_area()
    if symbol == "√":
        return width >= 25 and height >= 20 and area >= 900
    if symbol == "△":
        return width >= 14 and height >= 10 and area >= 180
    if symbol == "×":
        return width >= 12 and height >= 12 and area >= 180
    return width >= 10 and height >= 10 and max(width, height) >= 24


def _is_single_open_triangle(ink: Mapping[str, Any]) -> bool:
    """Accept the bounded open-triangle geometry seen in five frozen rows."""

    if str(ink.get("classified_symbol", "其他")) != "其他":
        return False
    if int(ink.get("stroke_count", 0)) != 1:
        return False
    bbox = _ink_bbox(ink)
    if bbox is None or bbox.width < 20 or bbox.height < 20 or bbox.get_area() < 450:
        return False
    aspect_ratio = bbox.width / bbox.height if bbox.height else 0.0
    return bool(
        0.55 <= aspect_ratio <= 1.80
        and 0.35 <= float(ink.get("open_ratio", 0.0)) <= 0.55
        and 0.18 <= float(ink.get("end_path_ratio", 0.0)) <= 0.30
    )


def _resolved_record_symbol(inks: Sequence[Mapping[str, Any]]) -> tuple[str | None, str | None]:
    if not inks:
        return None, "record_ink_count_not_one"
    if len(inks) == 1:
        symbol = str(inks[0].get("classified_symbol", "其他"))
        if symbol in _LEGAL_RECORD_SYMBOLS:
            return symbol, None
        if _is_single_open_triangle(inks[0]):
            return "△", None
        return None, "record_ink_geometry_unresolved"

    candidates = [
        cluster
        for cluster in _status_ink_clusters(inks)
        if _is_high_confidence_status_cluster(cluster)
    ]
    if not candidates:
        return None, "record_status_symbol_not_found"
    if len(candidates) != 1:
        return None, "record_status_symbol_conflict"
    geometry = _status_cluster_geometry(candidates[0])
    if geometry["cross_row_ambiguous"]:
        return None, "record_ink_cross_row_ambiguous"
    return str(geometry["symbol"]), None


def _has_nonconforming_candidate(inks: Sequence[Mapping[str, Any]]) -> bool:
    if any(str(ink.get("classified_symbol", "")) == "×" for ink in inks):
        return True
    return any(
        _ink_pair_forms_cross(inks[first], inks[second])
        for first in range(len(inks))
        for second in range(first + 1, len(inks))
    )


def _compare_symbol_to_report(symbol: str, report_results: Sequence[str]) -> dict[str, Any]:
    if not report_results:
        return {"decision": "manual", "expected": None, "reason_code": "report_mapping_missing"}
    components = [compare_record202_status_result(symbol, value).to_dict() for value in report_results]
    decisions = [component["decision"] for component in components]
    if "mismatch" in decisions:
        decision = "mismatch"
    elif "manual" in decisions:
        decision = "manual"
    else:
        decision = "match"
    return {
        "decision": decision,
        "expected": components[0].get("expected") if components else None,
        "reason_code": (
            "record_report_status_match"
            if decision == "match"
            else "record_report_status_mismatch"
            if decision == "mismatch"
            else "record_report_status_manual"
        ),
        "components": components,
    }


def _field_disposition(
    record_value: Any,
    report_values: Sequence[Any],
    *,
    field: str,
    allow_one_to_many: bool = False,
) -> dict[str, Any]:
    """Compare one mapping field while retaining both sides' actual values."""

    observed = [display_text(value) for value in report_values]
    record_text = display_text(record_value)
    normalized_record = compact(record_text)
    normalized_observed = [compact(value) for value in observed if compact(value)]
    if not normalized_record or not normalized_observed:
        return {
            "decision": "manual",
            "disposition": "manual",
            "reason_code": f"{field}_value_not_uniquely_resolved",
            "record_value": record_text,
            "report_values": observed,
        }
    if allow_one_to_many and normalized_record in normalized_observed:
        return {
            "decision": "match",
            "disposition": "matched",
            "reason_code": f"{field}_matched_one_to_many",
            "record_value": record_text,
            "report_values": observed,
        }
    if len(normalized_observed) == 1 and normalized_record == normalized_observed[0]:
        return {
            "decision": "match",
            "disposition": "matched",
            "reason_code": f"{field}_match",
            "record_value": record_text,
            "report_values": observed,
        }
    if len(normalized_observed) > 1 and normalized_record == "".join(normalized_observed):
        return {
            "decision": "match",
            "disposition": "matched",
            "reason_code": f"{field}_match_concatenated",
            "record_value": record_text,
            "report_values": observed,
        }
    return {
        "decision": "mismatch",
        "disposition": "mismatch",
        "reason_code": f"{field}_difference",
        "record_value": record_text,
        "report_values": observed,
    }


def _mapping_field_comparisons(
    record_row: Mapping[str, Any],
    report_group: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Return field-level mapping decisions without changing legacy row status."""

    report_projects = [str(row.get("project", "")) for row in report_group]
    report_clauses = [str(row.get("parent_clause", "")) for row in report_group]
    report_requirements = [str(row.get("requirement", "")) for row in report_group]
    record_requirement = str(record_row.get("requirement", ""))
    # Item 16 intentionally maps one Record requirement to two Report rows;
    # retain both actual values and classify this as matched only when one side
    # is an exact concatenation, otherwise manual rather than a fabricated pass.
    requirement_allow_many = len(report_requirements) > 1
    field_comparisons = {
        "project": _field_disposition(
            record_row.get("project", ""), report_projects, field="project", allow_one_to_many=False
        ),
        "parent_clause": _field_disposition(
            record_row.get("parent_clause", ""), report_clauses, field="parent_clause", allow_one_to_many=False
        ),
        "requirement": _field_disposition(
            record_requirement,
            report_requirements,
            field="requirement",
            allow_one_to_many=requirement_allow_many,
        ),
        "occurrence": {
            "decision": "match" if report_group else "manual",
            "disposition": "matched" if report_group else "manual",
            "reason_code": (
                "occurrence_order_match"
                if report_group
                else "occurrence_target_missing"
            ),
            "record_value": int(record_row.get("logical_row", 0)),
            "report_values": [int(row.get("physical_row", 0)) for row in report_group],
        },
    }
    return field_comparisons


_NUMBER_TOKEN = r"[+＋\-－−]?(?:\d+(?:\.\d*)?|\.\d+)"
_UNIT_TOKEN = (
    r"dB\(A\)|dB\(C\)|dB|MHz|KHz|kHz|Hz|MΩ|kΩ|mΩ|Ω|Ω|"
    r"mm/kV|mm2|mm|cm|kg|g|kV|mV|V|kW|mW|W|mA|uA|µA|μA|A|"
    r"uF|µF|μF|pF|nF|F|ms|s|N|Pa|kPa|m|°C|℃|%|％"
)
_NATIVE_MEASUREMENT_RE = re.compile(
    rf"(?<![\d.])(?P<number>{_NUMBER_TOKEN})\s*(?P<unit>{_UNIT_TOKEN})"
)
_BARE_NUMERIC_RE = re.compile(
    rf"^(?P<comparator><=|>=|≤|≥|＜|＞|<|>)?\s*(?P<number>{_NUMBER_TOKEN})\s*$"
)
_ACCEPTANCE_RANGE_RE = re.compile(
    rf"(?P<lower>{_NUMBER_TOKEN})\s*(?P<lower_unit>{_UNIT_TOKEN})?\s*"
    rf"(?:~|～|〜|∼|至|到|—|－|-)\s*"
    rf"(?P<upper>{_NUMBER_TOKEN})\s*(?P<upper_unit>{_UNIT_TOKEN})?"
)
_ACCEPTANCE_TOLERANCE_RE = re.compile(
    rf"(?:±|\+/-)\s*(?P<number>{_NUMBER_TOKEN})\s*(?P<unit>{_UNIT_TOKEN})?"
)
_BOUND_WORDS = (
    ("不超过", "<="),
    ("不得超过", "<="),
    ("不大于", "<="),
    ("不应超过", "<="),
    ("不小于", ">="),
    ("不得低于", ">="),
    ("不低于", ">="),
    ("至少", ">="),
    ("大于", ">"),
    ("小于", "<"),
)


def _numeric_token_dict(match: re.Match[str], unit_context: str | None = None) -> dict[str, Any]:
    number_text = match.group("number").replace("＋", "+").replace("－", "-").replace("−", "-")
    unit = match.groupdict().get("unit") or unit_context
    try:
        value = Decimal(number_text)
    except InvalidOperation:
        value = None
    return {
        "raw": match.group(0),
        "value": str(value) if value is not None else None,
        "unit": unit,
        "decimal_places": len(number_text.partition(".")[2]) if "." in number_text else 0,
        "start": match.start(),
        "end": match.end(),
    }


def _extract_numeric_tokens(text: str, *, unit_context: str | None = None) -> list[dict[str, Any]]:
    """Extract explicit measurements and a single bare result with context.

    Bare numbers are accepted only when the whole result cell is numeric. This
    prevents clause numbers, dates and page references in requirement prose
    from being treated as measurements.
    """

    source = compact(text)
    tokens = [_numeric_token_dict(match, unit_context) for match in _NATIVE_MEASUREMENT_RE.finditer(source)]
    if tokens:
        return tokens
    bare = _BARE_NUMERIC_RE.fullmatch(source)
    if bare and unit_context:
        token = _numeric_token_dict(bare, unit_context)
        token["raw"] = source
        return [token]
    return []


def _acceptance_constraints(requirement: str) -> dict[str, Any]:
    """Extract explicit limits/ranges without inventing semantics.

    The returned object is always published. Unrecognised prose is marked
    ``unresolved`` so S43 remains visible for manual review.
    """

    source = compact(requirement)
    unit_context = extract_unit_context(source)
    ranges: list[dict[str, Any]] = []
    occupied: list[tuple[int, int]] = []
    for match in _ACCEPTANCE_RANGE_RE.finditer(source):
        lower_unit = match.group("lower_unit") or unit_context
        upper_unit = match.group("upper_unit") or lower_unit or unit_context
        ranges.append({
            "lower": match.group("lower"),
            "upper": match.group("upper"),
            "lower_unit": lower_unit,
            "upper_unit": upper_unit,
            "raw": match.group(0),
        })
        occupied.append(match.span())

    tolerances = [
        {"value": match.group("number"), "unit": match.group("unit") or unit_context, "raw": match.group(0)}
        for match in _ACCEPTANCE_TOLERANCE_RE.finditer(source)
    ]
    bounds: list[dict[str, Any]] = []
    for match in _NATIVE_MEASUREMENT_RE.finditer(source):
        if any(start <= match.start() < end for start, end in occupied):
            continue
        before = source[max(0, match.start() - 16):match.start()]
        after = source[match.end():match.end() + 8]
        operator = None
        for word, candidate in _BOUND_WORDS:
            if word in before:
                operator = candidate
                break
        if operator is None:
            immediate = before[-2:].replace(" ", "")
            operator = {"≤": "<=", "＜": "<", "≥": ">=", "＞": ">"}.get(immediate)
        if operator is None and re.match(r"(?:以上|以下|以内)", after):
            operator = ">=" if after.startswith("以上") else "<="
        if operator is not None:
            token = _numeric_token_dict(match, unit_context)
            token["operator"] = operator
            bounds.append(token)
    explicit = bool(ranges or bounds or tolerances)
    return {
        "raw": requirement,
        "unit_context": unit_context,
        "ranges": ranges,
        "bounds": bounds,
        "tolerances": tolerances,
        "status": "resolved" if explicit else "unresolved",
        "reason_code": "acceptance_constraints_resolved" if explicit else "acceptance_relation_unresolved",
    }


def _compare_record_to_acceptance(record_text: str, requirement: str) -> dict[str, Any]:
    acceptance = _acceptance_constraints(requirement)
    if not acceptance["ranges"] and not acceptance["bounds"] and not acceptance["tolerances"]:
        return {"decision": "not_applicable", "reason_code": acceptance["reason_code"], "acceptance": acceptance}
    candidate = _extract_numeric_tokens(record_text, unit_context=acceptance["unit_context"])
    if len(candidate) != 1:
        return {"decision": "manual", "reason_code": "record_acceptance_value_not_uniquely_resolved", "acceptance": acceptance}
    parsed = parse_report_numeric(candidate[0]["raw"], acceptance["unit_context"])
    if parsed is None or parsed.comparator is not None or parsed.value is None:
        return {"decision": "manual", "reason_code": "record_acceptance_value_not_parsed", "acceptance": acceptance}
    try:
        for bound in acceptance["bounds"]:
            converted = convert_decimal(parsed.value, parsed.unit, bound["unit"])
            limit = Decimal(bound["value"])
            ok = {"<": converted < limit, "<=": converted <= limit, ">": converted > limit, ">=": converted >= limit}[bound["operator"]]
            if not ok:
                return {"decision": "mismatch", "reason_code": "acceptance_limit_mismatch", "acceptance": acceptance, "observed": str(converted)}
        for interval in acceptance["ranges"]:
            low = convert_decimal(parsed.value, parsed.unit, interval["lower_unit"])
            high = convert_decimal(parsed.value, parsed.unit, interval["upper_unit"])
            if not (low >= Decimal(interval["lower"]) and high <= Decimal(interval["upper"])):
                return {"decision": "mismatch", "reason_code": "acceptance_range_mismatch", "acceptance": acceptance, "observed": str(parsed.value)}
        if acceptance["tolerances"]:
            return {"decision": "manual", "reason_code": "acceptance_tolerance_reference_unresolved", "acceptance": acceptance}
    except (TypeError, ValueError, InvalidOperation):
        return {"decision": "manual", "reason_code": "acceptance_unit_conversion_unresolved", "acceptance": acceptance}
    return {"decision": "match", "reason_code": "acceptance_limit_matched", "acceptance": acceptance, "observed": str(parsed.value)}


def _report_numeric(value: str, requirement: str = "") -> Any:
    return parse_report_numeric(value, extract_unit_context(requirement))


def _is_report_measurement(value: str, requirement: str = "") -> bool:
    return _report_numeric(value, requirement) is not None and "%" not in compact(value) and "％" not in compact(value)


def parse_measurement(value: str) -> dict[str, Any] | None:
    parsed = parse_report_numeric(value)
    if parsed is None:
        return None
    return {
        "operator": parsed.comparator or "",
        "value": str(parsed.value),
        "unit": parsed.unit,
        "decimals": parsed.decimal_places,
    }


def compare_measurements(record_value: str, report_value: str) -> dict[str, Any]:
    record = parse_report_numeric(record_value)
    report = parse_report_numeric(report_value)
    if record is None or report is None or record.comparator is not None:
        return {"decision": "manual", "reason_code": "measurement_syntax_unresolved"}
    comparison = compare_numeric_observation(record.value, record.unit, report_value)
    return {
        **comparison.to_dict(),
        "record": parse_measurement(record_value),
        "report": parse_measurement(report_value),
        "record_in_report_unit": (
            str(comparison.normalized_record_value)
            if comparison.normalized_record_value is not None
            else None
        ),
        "rounded_to_report_precision": comparison.observed,
    }


def _native_measurement_candidate(record_text: str) -> str | None:
    normalized = compact(record_text)
    if not normalized or re.search(r"表3|P_+|见GB|环境温湿度|检测仪器|日期", normalized):
        return None
    matches = list(_NATIVE_MEASUREMENT_RE.finditer(normalized))
    if len(matches) == 1:
        return matches[0].group(0)
    bare = _BARE_NUMERIC_RE.fullmatch(normalized)
    return normalized if bare else None


def _compare_one(record_row: Mapping[str, Any], report_group: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    report_results = [str(row.get("result", "")) for row in report_group]
    symbol, symbol_error = _resolved_record_symbol(record_row.get("inks", []))
    if symbol is None:
        status_comparison = {
            "decision": "manual",
            "expected": None,
            "reason_code": symbol_error,
        }
    else:
        status_comparison = _compare_symbol_to_report(symbol, report_results)

    record_project = compact(str(record_row.get("project", "")))
    report_projects = sorted({compact(str(row.get("project", ""))) for row in report_group if row.get("project")})
    if not report_projects or not record_project:
        project_comparison = {
            "decision": "manual",
            "reason_code": "project_name_not_uniquely_resolved",
            "record_project": record_project,
            "report_projects": report_projects,
        }
    elif report_projects == [record_project]:
        project_comparison = {
            "decision": "match",
            "reason_code": "project_name_match",
            "record_project": record_project,
            "report_projects": report_projects,
        }
    else:
        project_comparison = {
            "decision": "mismatch",
            "reason_code": "project_name_difference",
            "record_project": record_project,
            "report_projects": report_projects,
        }

    measurement_rows = [
        (str(row.get("result", "")), str(row.get("requirement", "")))
        for row in report_group
        if _is_report_measurement(str(row.get("result", "")), str(row.get("requirement", "")))
    ]
    percentage_rows = [value for value in report_results if extract_percentage_values(value)]
    numeric_comparison: dict[str, Any] | None = None
    record_text = str(record_row.get("native_result_text", ""))
    if "表3" in compact(record_text):
        numeric_comparison = {
            "decision": "excluded",
            "reason_code": "TABLE3_OUT_OF_SCOPE",
            "report_values": report_results,
        }
    elif percentage_rows:
        if len(percentage_rows) != 1 or not extract_percentage_values(record_text):
            numeric_comparison = {
                "decision": "manual",
                "reason_code": "record_percentage_not_uniquely_resolved",
                "report_values": percentage_rows,
            }
        else:
            numeric_comparison = compare_final_percentage(
                record_text,
                percentage_rows[0],
            ).to_dict()
            numeric_comparison["acceptance"] = _acceptance_constraints(
                str(report_group[0].get("requirement", ""))
            )
    elif measurement_rows:
        record_candidate = _native_measurement_candidate(str(record_row.get("native_result_text", "")))
        if record_candidate is None or len(measurement_rows) != 1:
            numeric_comparison = {
                "decision": "manual",
                "reason_code": "record_measurement_not_uniquely_resolved",
                "report_values": [value for value, _ in measurement_rows],
            }
        else:
            report_value, report_requirement = measurement_rows[0]
            parsed_record = parse_report_numeric(record_candidate, extract_unit_context(report_requirement))
            if parsed_record is None or parsed_record.comparator is not None:
                numeric_comparison = {
                    "decision": "manual",
                    "reason_code": "record_measurement_not_uniquely_resolved",
                    "report_values": [report_value],
                }
            else:
                numeric_comparison = compare_numeric_observation(
                    parsed_record.value,
                    parsed_record.unit,
                    report_value,
                    extract_unit_context(report_requirement),
                ).to_dict()
                acceptance = _compare_record_to_acceptance(
                    str(record_row.get("native_result_text", "")),
                    report_requirement,
                )
                numeric_comparison["acceptance"] = acceptance
                if acceptance["decision"] == "mismatch":
                    numeric_comparison["decision"] = "mismatch"
                    numeric_comparison["reason_code"] = acceptance["reason_code"]
                elif acceptance["decision"] == "manual" and numeric_comparison["decision"] == "match":
                    numeric_comparison["decision"] = "manual"
                    numeric_comparison["reason_code"] = acceptance["reason_code"]

    component_decisions = [status_comparison["decision"], project_comparison["decision"]]
    if numeric_comparison is not None and numeric_comparison["decision"] != "excluded":
        component_decisions.append(numeric_comparison["decision"])
    if "mismatch" in component_decisions:
        decision = "mismatch"
    elif "manual" in component_decisions:
        decision = "manual"
    else:
        decision = "match"
    return {
        "decision": decision,
        "record_symbol": symbol,
        "status": status_comparison,
        "project": project_comparison,
        "numeric": numeric_comparison,
        "nonconforming_alert": symbol == "×" or _has_nonconforming_candidate(
            record_row.get("inks", [])
        ),
    }


def _number_check(record_doc: fitz.Document, report_doc: fitz.Document) -> dict[str, Any]:
    report_number = extract_report_number(report_doc[0].get_text("text"))
    report_number_text = (
        f"QW{report_number[0]}-{report_number[1]}" if report_number is not None else None
    )
    page_states: list[dict[str, Any]] = []
    for page in record_doc:
        state = classify_report_number_field(page.get_text("text"))
        parsed = state["number"]
        page_states.append(
            {
                "source_row_id": f"record202:number:p{page.number + 1:03d}",
                "pdf_page": page.number + 1,
                "state": state["state"],
                "parsed_number": f"QW{parsed[0]}-{parsed[1]}" if parsed else None,
                "bbox": _rect(number_evidence_rects(page, report_number)[0]),
            }
        )
    missing = [row["pdf_page"] for row in page_states if row["state"] == "explicit_missing"]
    malformed = [row["pdf_page"] for row in page_states if row["state"] == "malformed"]
    unreadable = [row["pdf_page"] for row in page_states if row["state"] == "unreadable_or_unsupported"]
    mismatched = [
        row["pdf_page"]
        for row in page_states
        if row["state"] == "parsed" and row["parsed_number"] != report_number_text
    ]
    if report_number is None or unreadable:
        status = "manual"
    elif missing or malformed or mismatched:
        status = "error"
    else:
        status = "pass"
    return {
        "status": status,
        "report_row_id": "report:number:p001",
        "report_number": report_number_text,
        "report_pdf_page": 1,
        "report_bbox": _rect(number_evidence_rects(report_doc[0], report_number)[0]),
        "record_page_count": record_doc.page_count,
        "missing_pages": missing,
        "malformed_pages": malformed,
        "unreadable_pages": unreadable,
        "mismatched_pages": mismatched,
        "page_states": page_states,
    }


def _number_ledger(number_check: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[CoverageEntry]]:
    ledger: list[dict[str, Any]] = []
    coverage_entries: list[CoverageEntry] = []
    target_id = str(number_check["report_row_id"])
    report_number = number_check.get("report_number")
    for state in number_check["page_states"]:
        if report_number is None or state["state"] == "unreadable_or_unsupported":
            decision = "manual"
            disposition = "manual"
            reason_code = "report_or_record_number_unresolved"
        elif state["state"] == "parsed" and state["parsed_number"] == report_number:
            decision = "match"
            disposition = "matched"
            reason_code = "page_report_number_matched"
        else:
            decision = "mismatch"
            disposition = "mismatch"
            reason_code = f"page_report_number_{state['state']}"
        source_id = str(state["source_row_id"])
        entry_id = f"RECORD202-NUMBER-P{state['pdf_page']:02d}"
        entry = {
            "entry_type": "number",
            "id": entry_id,
            "entry_id": entry_id,
            "rule_id": "RECORD202-NUMBER",
            "source_row_id": source_id,
            "source_row_ids": [source_id],
            "target_row_id": target_id,
            "target_row_ids": [target_id],
            "disposition": disposition,
            "reason_code": reason_code,
            "record_location": {
                "pdf_page": state["pdf_page"],
                "bbox": state["bbox"],
            },
            "report_location": {
                "pdf_page": number_check["report_pdf_page"],
                "bbox": number_check["report_bbox"],
            },
            "record": {
                "pdf_page": state["pdf_page"],
                "state": state["state"],
                "number": state["parsed_number"],
            },
            "report": {
                "pdf_page": number_check["report_pdf_page"],
                "number": report_number,
            },
            "comparison": {
                "decision": decision,
                "reason_code": reason_code,
                "expected": report_number,
                "observed": state["parsed_number"],
            },
            "decision": decision,
        }
        ledger.append(entry)
        coverage_entries.append(
            CoverageEntry(source_id, target_id, disposition, reason_code, (entry_id,))
        )
    return ledger, coverage_entries


def compare_record_202_documents(
    record_path: str | Path,
    report_path: str | Path,
    *,
    sample: str | None = None,
) -> dict[str, Any]:
    record_path = Path(record_path)
    report_path = Path(report_path)
    record_hash_before = sha256_file(record_path)
    report_hash_before = sha256_file(report_path)

    with fitz.open(record_path) as record_doc, fitz.open(report_path) as report_doc:
        record_rows, extraction = extract_record_202_rows(record_doc)
        report_rows = extract_report_202_rows(report_doc)
        report_groups = build_report_202_groups(report_rows)
        number_check = _number_check(record_doc, report_doc)
        legend_check = _legend_check(record_doc)
        record_structure = _record_202_structure(record_doc)
        report_structure = _report_202_structure(report_doc)

        actual_counts = extraction["item_row_counts"]
        if actual_counts != EXPECTED_ITEM_ROW_COUNTS:
            raise ValueError(f"unexpected Record row inventory: {actual_counts}")
        if extraction["row_count"] != EXPECTED_COMPARISON_UNITS:
            raise ValueError(f"expected 175 Record rows, got {extraction['row_count']}")

        report_comparable_rows = [
            row
            for item_groups in report_groups.values()
            for group in item_groups
            for row in group
        ]
        if len(report_comparable_rows) != EXPECTED_REPORT_PHYSICAL_ROWS:
            raise ValueError(
                f"expected {EXPECTED_REPORT_PHYSICAL_ROWS} comparable Report rows, "
                f"got {len(report_comparable_rows)}"
            )

        record_by_item: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in record_rows:
            record_by_item[int(row["item"])].append(row)
        item_mapping_state: dict[int, dict[str, Any]] = {}
        for item in range(1, 39):
            flat_report_rows = [row for group in report_groups.get(item, []) for row in group]
            record_identity = _item_template_fingerprint(record_by_item[item])
            report_identity = _item_template_fingerprint(flat_report_rows)
            record_identity_ok = record_identity == FROZEN_RECORD_ITEM_IDENTITIES[item]
            report_identity_ok = report_identity == FROZEN_REPORT_ITEM_IDENTITIES[item]
            expected_report_rows = EXPECTED_ITEM_ROW_COUNTS[item] + (1 if item == 16 else 0)
            count_ok = (
                len(record_by_item[item]) == EXPECTED_ITEM_ROW_COUNTS[item]
                and len(report_groups.get(item, [])) == EXPECTED_ITEM_ROW_COUNTS[item]
                and len(flat_report_rows) == expected_report_rows
            )
            item_mapping_state[item] = {
                "valid": record_identity_ok and report_identity_ok and count_ok,
                "record_requirement_identity": record_identity,
                "expected_record_requirement_identity": FROZEN_RECORD_ITEM_IDENTITIES[item],
                "record_requirement_identity_ok": record_identity_ok,
                "report_requirement_identity": report_identity,
                "expected_report_requirement_identity": FROZEN_REPORT_ITEM_IDENTITIES[item],
                "report_requirement_identity_ok": report_identity_ok,
                "occurrence_counts_ok": count_ok,
            }

        body_ledger: list[dict[str, Any]] = []
        body_coverage_entries: list[CoverageEntry] = []
        group_offsets: Counter[int] = Counter()
        for record_row in record_rows:
            item = int(record_row["item"])
            group_index = group_offsets[item]
            groups = report_groups.get(item, [])
            report_group = groups[group_index] if group_index < len(groups) else []
            group_offsets[item] += 1
            report_item = 118 + item
            parent_clause_match = bool(report_group) and all(
                compact(row.get("parent_clause")) == compact(record_row.get("parent_clause"))
                for row in report_group
            )
            project_name_match = bool(report_group) and all(
                compact(row.get("project")) == compact(record_row.get("project"))
                for row in report_group
            )
            mapping_ok = (
                bool(report_group)
                and parent_clause_match
                and item_mapping_state[item]["valid"]
            )
            if mapping_ok:
                comparison = _compare_one(record_row, report_group)
                if not legend_check["validated"]:
                    # A symbol cannot be interpreted before the template legend
                    # is verified.  Numeric/project checks remain visible, but
                    # the status component is deliberately downgraded to manual.
                    comparison["status"] = {
                        "decision": "manual",
                        "expected": None,
                        "reason_code": "record_202_legend_unverified",
                    }
                    components = [
                        comparison["status"],
                        comparison.get("project") or {},
                        comparison.get("numeric") or {},
                    ]
                    if any(item.get("decision") == "mismatch" for item in components):
                        comparison["decision"] = "mismatch"
                    elif any(item.get("decision") == "manual" for item in components):
                        comparison["decision"] = "manual"
                    else:
                        comparison["decision"] = "match"
            else:
                symbol, symbol_error = _resolved_record_symbol(record_row.get("inks", []))
                comparison = {
                    "decision": "manual",
                    "record_symbol": symbol,
                    "status": {
                        "decision": "manual",
                        "reason_code": (
                            "report_mapping_missing"
                            if not report_group
                            else "parent_clause_mismatch"
                            if not parent_clause_match
                            else "requirement_identity_abnormal"
                        ),
                        "symbol_reason_code": symbol_error,
                    },
                    "numeric": None,
                    "nonconforming_alert": symbol == "×" or _has_nonconforming_candidate(
                        record_row.get("inks", [])
                    ),
                }

            public_record = {key: value for key, value in record_row.items()}
            public_record["inks"] = [
                {
                    key: value
                    for key, value in ink.items()
                    if not str(key).startswith("_")
                }
                for ink in record_row.get("inks", [])
            ]
            report_results = [row.get("result", "") for row in report_group]
            rule_id = (
                "RECORD202-BODY-PERCENT"
                if any("%" in compact(value) for value in report_results)
                else "RECORD202-BODY-NUMERIC"
                if any(
                    _is_report_measurement(
                        str(row.get("result", "")),
                        str(row.get("requirement", "")),
                    )
                    for row in report_group
                )
                else "RECORD202-BODY-STATUS"
            )
            disposition = {
                "match": "matched",
                "mismatch": "mismatch",
                "manual": "manual",
            }[comparison["decision"]]
            report_evidence = [
                {
                    "pdf_page": row["pdf_page"],
                    "bbox": row["result_bbox"],
                }
                for row in report_group
            ]
            target_row_ids = [str(row["row_id"]) for row in report_group]
            # The published reason must explain the top-level disposition.  A
            # table-3 reference is an excluded numeric sub-comparison; it must
            # not hide an independently matched or unresolved status mark.
            components = [
                comparison["status"],
                comparison.get("project") or {},
                comparison.get("numeric") or {},
            ]
            reason_code = next(
                (
                    component.get("reason_code")
                    for component in components
                    if component.get("decision") == comparison["decision"]
                    and component.get("reason_code")
                ),
                comparison.get("project", {}).get("reason_code")
                or comparison["status"].get("reason_code")
                or (comparison.get("numeric") or {}).get("reason_code"),
            )
            entry_id = f"RECORD202-I{item:02d}-R{record_row['logical_row']:02d}"
            body_ledger.append(
                {
                    "entry_type": "body",
                    "id": entry_id,
                    "entry_id": entry_id,
                    "rule_id": rule_id,
                    "source_row_id": record_row["row_id"],
                    "source_row_ids": [record_row["row_id"]],
                    "target_row_id": target_row_ids[0] if len(target_row_ids) == 1 else None,
                    "target_row_ids": target_row_ids,
                    "disposition": disposition,
                    "reason_code": reason_code,
                    "record_location": {
                        "pdf_page": record_row["pdf_page"],
                        "bbox": record_row["result_bbox"],
                    },
                    "report_location": report_evidence[0] if report_evidence else None,
                    "report_locations": report_evidence,
                    "record_evidence": {
                        "pdf_page": record_row["pdf_page"],
                        "rects": [record_row["result_bbox"]],
                    },
                    "report_evidence": report_evidence,
                    "record": public_record,
                    "report": {
                        "item": report_item,
                        "rows": list(report_group),
                        "results": report_results,
                    },
                    "mapping": {
                        "method": (
                            "frozen_item_parent_clause_occurrence_with_item16_one_to_many"
                            if item == 16 and record_row["logical_row"] == 1
                            else "frozen_item_parent_clause_occurrence_with_item8_structural_exclusion"
                            if item == 8
                            else "frozen_item_parent_clause_occurrence"
                        ),
                        "valid": mapping_ok,
                        "record_item": item,
                        "report_item": report_item,
                        "occurrence": record_row["logical_row"],
                        "parent_clause_match": parent_clause_match,
                        "project_name_match": project_name_match,
                        "project_name_diagnostics": (
                            [] if project_name_match else ["project_name_difference"]
                        ),
                        **item_mapping_state[item],
                        "record_requirement_fingerprint": record_row["requirement_fingerprint"],
                        "report_requirement_fingerprints": [
                            row["requirement_fingerprint"] for row in report_group
                        ],
                        "field_comparisons": _mapping_field_comparisons(record_row, report_group),
                    },
                    "comparison": comparison,
                    "decision": comparison["decision"],
                }
            )
            for target_row_id in target_row_ids:
                body_coverage_entries.append(
                    CoverageEntry(
                        str(record_row["row_id"]),
                        target_row_id,
                        disposition,
                        str(reason_code),
                        (entry_id,),
                    )
                )

        number_ledger, number_coverage_entries = _number_ledger(number_check)
        ledger = body_ledger + number_ledger
        coverage = validate_coverage(
            [str(row["row_id"]) for row in record_rows]
            + [str(row["source_row_id"]) for row in number_check["page_states"]],
            [str(row["row_id"]) for row in report_comparable_rows]
            + [str(number_check["report_row_id"])],
            body_coverage_entries + number_coverage_entries,
        )

        scope_ledger, scope_coverage = _expanded_scope_ledger(
            record_doc,
            report_doc,
            record_rows=record_rows,
            report_groups=report_groups,
            body_ledger=body_ledger,
            record_structure=record_structure,
            report_structure=report_structure,
        )

    if sha256_file(record_path) != record_hash_before or sha256_file(report_path) != report_hash_before:
        raise RuntimeError("source PDF changed during comparison")

    decisions = Counter(row["decision"] for row in ledger)
    body_decisions = Counter(row["decision"] for row in body_ledger)
    if decisions["mismatch"]:
        overall_status = "error"
    elif decisions["manual"]:
        overall_status = "manual"
    else:
        overall_status = "pass"
    return {
        "schema_version": "record202-full-0.3",
        "sample": sample,
        "mode": "report_record_9706_202",
        "overall_status": overall_status,
        "source_files": {
            "record": {"path": str(record_path), "sha256": record_hash_before},
            "report": {"path": str(report_path), "sha256": report_hash_before},
        },
        "scope": {
            "included": [
                "GB 9706.202 table-2 pages 1-24",
                "38 Record projects / 175 comparison units",
                "per-page report number",
                "four-symbol legend verification before status comparison",
                "status marks and Report results",
                "project name, parent clause, requirement text and occurrence order",
                "field-level project, parent clause, requirement and occurrence objects",
                "all machine-readable numeric/percentage result and acceptance targets",
                "Record/Report page, header and template structure details",
                "fixed identity and metadata fields as object-level scope entries",
            ],
            "excluded": [
                "GB 9706.202 table-3 documents and pages",
                "recalculation from table-3 raw measurements",
            ],
            "table3_reference_policy": (
                "A table-2 row containing a table-3 page reference remains in the ledger; "
                "the page reference itself is not treated as a measurement."
            ),
        },
        "number_check": number_check,
        "legend_check": legend_check,
        "structure": {"record": record_structure, "report": report_structure},
        "extraction": extraction,
        "coverage": {
            **coverage,
            "expected_items": 38,
            "emitted_items": len({row["record"]["item"] for row in body_ledger}),
            "expected_comparison_units": EXPECTED_COMPARISON_UNITS + EXPECTED_NUMBER_ROWS,
            "emitted_comparison_units": len(ledger),
            "decision_counts": {
                "match": decisions["match"],
                "mismatch": decisions["mismatch"],
                "manual": decisions["manual"],
            },
            "body_decision_counts": {
                "match": body_decisions["match"],
                "mismatch": body_decisions["mismatch"],
                "manual": body_decisions["manual"],
            },
            "ledger_entries": len(ledger),
            "conserved": (
                coverage["source_rows"]["conserved"]
                and coverage["report_rows"]["conserved"]
            ),
        },
        "ledger": ledger,
        "scope_ledger": scope_ledger,
        "scope_coverage": scope_coverage,
    }


def compare_record_202_sample(root: str | Path, sample: str) -> dict[str, Any]:
    root = Path(root)
    config = SAMPLE_CONFIGS[sample]
    if config.record202 is None:
        raise ValueError(f"sample {sample} has no GB 9706.202 Record")
    return compare_record_202_documents(
        root / config.record202,
        root / config.report,
        sample=sample,
    )


def _ledger_evidence_locations(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    record = row["record"]
    locations = [
        {
            "role": "record_9706_202",
            "pdf_page": record["pdf_page"],
            "bbox": record["result_bbox"],
            "semantic_role": "source_observation",
        }
    ]
    for report_row in row["report"]["rows"]:
        locations.append(
            {
                "role": "report",
                "pdf_page": report_row["pdf_page"],
                "bbox": report_row["result_bbox"],
                "semantic_role": "comparison_target",
            }
        )
    return locations


def _findings_from_result(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    number = result["number_check"]
    number_locations = [
        {
            "role": "report",
            "pdf_page": number["report_pdf_page"],
            "bbox": number["report_bbox"],
            "semantic_role": "comparison_target",
        }
    ]
    relevant_record_pages = (
        number["missing_pages"]
        + number["malformed_pages"]
        + number["unreadable_pages"]
        + number["mismatched_pages"]
    )
    if not relevant_record_pages:
        relevant_record_pages = [1, number["record_page_count"]]
    states_by_page = {row["pdf_page"]: row for row in number["page_states"]}
    number_locations.extend(
        {
            "role": "record_9706_202",
            "pdf_page": page_number,
            "bbox": states_by_page[page_number]["bbox"],
            "semantic_role": "source_observation",
        }
        for page_number in sorted(set(relevant_record_pages))
    )
    findings: list[dict[str, Any]] = [
        {
            "id": "RECORD202-NUMBER",
            "rule_id": "RECORD202-NUMBER",
            "status": number["status"],
            "title": "9706.202逐页报告编号",
            "summary": (
                "Record全部页的报告编号与Report一致"
                if number["status"] == "pass"
                else f"编号空白{len(number['missing_pages'])}页、不一致{len(number['mismatched_pages'])}页"
                if number["status"] == "error"
                else "Record或Report编号无法唯一解析"
            ),
            "details": number,
            "evidence_locations": number_locations,
        },
        {
            "id": "RECORD202-SYMBOLS",
            "rule_id": "RECORD202-SYMBOLS",
            "status": "pass" if result.get("legend_check", {}).get("validated") else "manual",
            "title": "9706.202状态符号图例核验",
            "summary": (
                "√、×、△、/图例已核验"
                if result.get("legend_check", {}).get("validated")
                else "状态符号图例无法可靠核验，正文状态保持人工复核"
            ),
            "details": result.get("legend_check", {}),
            "evidence_locations": [
                {
                    "role": "record_9706_202",
                    "pdf_page": int(result.get("legend_check", {}).get("pdf_page", 1)),
                    "bbox": result.get("legend_check", {}).get("bbox", [0.0, 0.0, 1.0, 1.0]),
                    "semantic_role": "legend_source",
                },
                {
                    "role": "report",
                    "pdf_page": int(number["report_pdf_page"]),
                    "bbox": number["report_bbox"],
                    "semantic_role": "comparison_context",
                },
            ],
        },
    ]
    structure = result.get("structure", {})
    structure_valid = bool(structure.get("record", {}).get("validated")) and bool(structure.get("report", {}).get("validated"))
    findings.append(
        {
            "id": "RECORD202-STRUCTURE",
            "rule_id": "RECORD202-STRUCTURE",
            "status": "pass" if structure_valid else "manual",
            "title": "9706.202页码、表头和模板结构",
            "summary": "Record 24页和Report正式表格结构完整" if structure_valid else "Record/Report页码或表格结构需人工复核",
            "details": structure,
            "evidence_locations": number_locations,
        }
    )
    for row in result["ledger"]:
        if row.get("entry_type") != "body":
            continue
        status = {"match": "pass", "mismatch": "error", "manual": "manual"}[row["decision"]]
        record = row["record"]
        report_results = row["report"]["results"]
        findings.append(
            {
                "id": row["id"],
                "rule_id": (
                    "RECORD202-BODY-PERCENT"
                    if any("%" in compact(value) for value in report_results)
                    else "RECORD202-BODY-NUMERIC"
                    if any(
                        _is_report_measurement(
                            str(report_row.get("result", "")),
                            str(report_row.get("requirement", "")),
                        )
                        for report_row in row["report"]["rows"]
                    )
                    else "RECORD202-BODY-STATUS"
                ),
                "status": status,
                "title": f"Record项目{record['item']}第{record['logical_row']}行",
                "summary": (
                    "Record与Report结果一致"
                    if status == "pass"
                    else "Record与Report结果明确不一致"
                    if status == "error"
                    else "Record符号、实测值或映射需人工复核"
                ),
                "details": {
                    "record_item": record["item"],
                    "record_logical_row": record["logical_row"],
                    "report_item": row["report"]["item"],
                    "report_results": report_results,
                    "comparison": row["comparison"],
                    "mapping": row["mapping"],
                },
                "evidence_locations": _ledger_evidence_locations(row),
            }
        )
        if row["comparison"].get("nonconforming_alert"):
            findings.append(
                {
                    "id": f"{row['id']}-NONCONFORMING",
                    "rule_id": "RECORD202-NONCONFORMING",
                    "status": "warning",
                    "title": "9706.202 Record明确记录不符合",
                    "summary": "Record的‘×’要求单独警示；一致性判断仍保留在正文Finding中",
                    "details": {"ledger_entry_id": row["id"]},
                    "evidence_locations": _ledger_evidence_locations(row),
                }
            )
    return findings


def run_record_202_full(
    report_path: str | Path,
    record_path: str | Path,
    output_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Stable entry point for the full Report + GB 9706.202 Record run."""

    result = compare_record_202_documents(record_path, report_path)
    record_hash = result["source_files"]["record"]["sha256"]
    report_hash = result["source_files"]["report"]["sha256"]
    del output_dir  # The coordinating runner owns all output publication.
    record_after = sha256_file(Path(record_path))
    report_after = sha256_file(Path(report_path))
    result["source_integrity"] = [
        {
            "role": "report",
            "sha256_before": report_hash,
            "sha256_after": report_after,
            "unchanged": report_hash == report_after,
        },
        {
            "role": "record_9706_202",
            "sha256_before": record_hash,
            "sha256_after": record_after,
            "unchanged": record_hash == record_after,
        },
    ]
    result["findings"] = _findings_from_result(result)
    if any(not finding["rule_id"].startswith("RECORD202-") for finding in result["findings"]):
        raise RuntimeError("9706.202 run leaked a non-RECORD202 finding")
    for finding in result["findings"]:
        if finding["status"] in {"error", "manual"}:
            roles = {location["role"] for location in finding["evidence_locations"]}
            if roles != {"report", "record_9706_202"}:
                raise RuntimeError(f"{finding['id']} lacks two-sided evidence locations")
    return result
