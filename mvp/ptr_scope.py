"""Deterministic parsing of the Report ``检验项目`` declaration.

The third-page inspection-items field is the boundary for PTR ↔ Report
comparison.  This module deliberately parses only what can be supported by
the field itself: clause identifiers, numeric/letter ranges, exclusions and
standard-specific ``仅检`` selectors.  It does not inspect the PTR or infer
missing clauses from the Report body.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Any, Iterable

try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover - compatibility with older environments
    import fitz

from mvp.checker import compact, display_text, find_exact_rect, page_three_fields, rect_list, sha256_file


PTR_SCOPE_SCHEMA_VERSION = "ptr-scope-0.1"
_RANGE_MARKERS = ("～", "~", "至")
_CLAUSE_RE = re.compile(r"^(?P<numeric>\d+(?:\.\d+)*)(?P<letter>[A-Za-z])?\)?$")
_STANDARD_RE = re.compile(r"(?P<standard>(?:GB|YY)\s*\d+(?:\.\d+)+\s*-\s*\d{4})", re.IGNORECASE)
_QUALIFIER_RE = re.compile(r"[（(](?P<body>[^（）()]*)[）)]")


class PtrScopeParseError(ValueError):
    """Raised when a scope expression cannot be represented safely."""


def _normal_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    replacements = {
        "—": "～",
        "–": "～",
        "－": "～",
        "，": "、",
        ",": "、",
        "；": "、",
        ";": "、",
        "／": "/",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return re.sub(r"\s+", " ", text).strip()


def normalize_standard(value: str) -> str:
    """Return a stable spelling such as ``GB 9706.1-2020``."""

    compacted = re.sub(r"\s+", "", unicodedata.normalize("NFKC", value)).upper()
    match = re.fullmatch(r"(GB|YY)(\d+(?:\.\d+)+-\d{4})", compacted)
    return f"{match.group(1)} {match.group(2)}" if match else _normal_text(value)


def _clause_parts(value: str) -> tuple[tuple[int, ...], str | None] | None:
    compacted = re.sub(r"\s+", "", value).rstrip("。．:")
    match = _CLAUSE_RE.fullmatch(compacted)
    if not match:
        return None
    return tuple(int(part) for part in match.group("numeric").split(".")), (match.group("letter") or "").lower() or None


def _clause_text(parts: tuple[tuple[int, ...], str | None]) -> str:
    numeric, letter = parts
    return ".".join(str(item) for item in numeric) + (letter or "")


def _expand_clause_range(start_text: str, end_text: str) -> list[str] | None:
    start = _clause_parts(start_text)
    if start is None:
        return None
    end = _clause_parts(end_text)
    if end is None and re.fullmatch(r"\s*[A-Za-z]\)?\s*", end_text):
        end = (start[0], end_text.strip().rstrip(")").lower())
    if end is None:
        return None
    start_numeric, start_letter = start
    end_numeric, end_letter = end
    if start_letter or end_letter:
        if start_numeric != end_numeric or not start_letter or not end_letter:
            return None
        first, last = ord(start_letter), ord(end_letter)
        if last < first or last - first > 26:
            return None
        return [_clause_text((start_numeric, chr(value))) for value in range(first, last + 1)]
    if len(start_numeric) != len(end_numeric) or start_numeric[:-1] != end_numeric[:-1]:
        return None
    first, last = start_numeric[-1], end_numeric[-1]
    if last < first or last - first > 500:
        return None
    prefix = start_numeric[:-1]
    return [_clause_text((prefix + (value,), None)) for value in range(first, last + 1)]


def _scope_piece(piece: str, inherited_numeric: tuple[int, ...] | None = None) -> dict[str, Any] | None:
    raw = piece.strip()
    if not raw:
        return None
    if inherited_numeric is not None and re.fullmatch(r"[A-Za-z]\)?", raw):
        letter = raw.rstrip(")").lower()
        clause_id = _clause_text((inherited_numeric, letter))
        return {"raw": raw, "kind": "clause", "clause_ids": [clause_id], "inherited": True}
    for marker in _RANGE_MARKERS:
        if marker in raw:
            left, right = raw.split(marker, 1)
            left_parts = _clause_parts(left.strip())
            right_parts = _clause_parts(right.strip())
            if right_parts is None and left_parts and re.fullmatch(r"[A-Za-z]\)?", right.strip()):
                right_parts = (left_parts[0], right.strip().rstrip(")").lower())
            expanded = _expand_clause_range(left, right)
            if expanded is None:
                return None
            return {
                "raw": raw,
                "kind": "range",
                "start": _clause_text(left_parts) if left_parts else left.strip(),
                "end": _clause_text(right_parts) if right_parts else right.strip(),
                "clause_ids": expanded,
            }
    parsed = _clause_parts(raw)
    if parsed is None:
        return None
    clause_id = _clause_text(parsed)
    return {"raw": raw, "kind": "clause", "clause_ids": [clause_id]}


def _split_scope_list(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"、", value) if part.strip()]


def _unique(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _parse_only_selector(
    body: str,
    *,
    applies_to_clause_ids: list[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    selectors: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    matches = list(_STANDARD_RE.finditer(body))
    if not matches:
        if body.strip():
            return [
                {
                    "raw": body.strip(),
                    "kind": "text",
                    "standard": None,
                    "groups": [],
                    "clause_ids": [],
                    "applies_to_clause_ids": list(applies_to_clause_ids or []),
                }
            ], []
        return [], [{"section": "only", "raw": body.strip(), "reason": "standard_reference_not_found"}]
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        segment = body[match.end() : end].strip(" 、")
        segment = re.sub(r"^(?:中|第)\s*", "", segment)
        groups: list[dict[str, Any]] = []
        for piece in _split_scope_list(segment):
            parsed = _scope_piece(piece)
            if parsed is None:
                unresolved.append({"section": "only", "raw": piece, "reason": "clause_token_unparsed"})
            else:
                groups.append(parsed)
        clause_ids = _unique(item for group in groups for item in group["clause_ids"])
        if not clause_ids:
            unresolved.append({"section": "only", "raw": body[match.start() : end].strip(), "reason": "selector_has_no_clause"})
        selectors.append(
            {
                "raw": body[match.start() : end].strip(" 、"),
                "standard": normalize_standard(match.group("standard")),
                "groups": groups,
                "clause_ids": clause_ids,
                "applies_to_clause_ids": list(applies_to_clause_ids or []),
            }
        )
    return selectors, unresolved


def _parse_exclusion(body: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    entries: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for piece in _split_scope_list(body):
        parsed = _scope_piece(piece)
        if parsed is not None:
            parsed["selection"] = "excluded"
            entries.append(parsed)
        else:
            # Exclusions are often descriptive Chinese names rather than
            # clause IDs. They remain valid scope atoms and are compared as
            # text later; they are not treated as parser failures.
            entries.append({"raw": piece, "kind": "text", "selection": "excluded", "clause_ids": []})
    return entries, unresolved


def _qualifier_target(raw_text: str, start: int) -> list[str]:
    """Return the immediately preceding clause for an inline qualifier."""

    prefix = _QUALIFIER_RE.sub("", raw_text[:start]).strip(" 、")
    pieces = _split_scope_list(prefix)
    if not pieces:
        return []
    parsed = _scope_piece(pieces[-1])
    return list(parsed.get("clause_ids", [])) if parsed else []


def parse_scope_expression(expression: str) -> dict[str, Any]:
    """Parse one Report ``检验项目`` expression into a deterministic ScopeSet."""

    raw_text = _normal_text(expression)
    qualifier_matches = list(_QUALIFIER_RE.finditer(raw_text))
    main_text = _QUALIFIER_RE.sub("", raw_text).strip(" 、")
    groups: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    inherited_numeric: tuple[int, ...] | None = None
    for piece in _split_scope_list(main_text):
        parsed = _scope_piece(piece, inherited_numeric)
        if parsed is None:
            unresolved.append({"section": "included", "raw": piece, "reason": "clause_token_unparsed"})
        else:
            parsed["selection"] = "included"
            groups.append(parsed)
            last_parts = _clause_parts(parsed["clause_ids"][-1])
            inherited_numeric = last_parts[0] if last_parts and last_parts[1] else None

    excluded: list[dict[str, Any]] = []
    only: list[dict[str, Any]] = []
    for match in qualifier_matches:
        qualifier = match.group("body").strip()
        normalized = qualifier.strip()
        compacted = compact(normalized)
        if compacted.startswith("除"):
            items, errors = _parse_exclusion(normalized[normalized.find("除") + 1 :])
            excluded.extend(items)
            unresolved.extend(errors)
        elif compacted.startswith("仅检"):
            selectors, errors = _parse_only_selector(
                normalized[normalized.find("仅检") + 2 :],
                applies_to_clause_ids=_qualifier_target(raw_text, match.start()),
            )
            only.extend(selectors)
            unresolved.extend(errors)
        else:
            unresolved.append({"section": "qualifier", "raw": normalized, "reason": "unknown_qualifier"})

    included_clause_ids = _unique(item for group in groups for item in group["clause_ids"])
    excluded_clause_ids = _unique(item for group in excluded for item in group.get("clause_ids", []))
    only_clause_ids = _unique(item for selector in only for item in selector.get("clause_ids", []))
    return {
        "schema_version": PTR_SCOPE_SCHEMA_VERSION,
        "status": "parsed" if not unresolved and groups else "manual",
        "raw_text": raw_text,
        "main_text": main_text,
        "groups": groups,
        "included_clause_ids": included_clause_ids,
        "excluded": excluded,
        "excluded_clause_ids": excluded_clause_ids,
        "only": only,
        "only_clause_ids": only_clause_ids,
        "unresolved": unresolved,
    }


def _inspection_items_cell(page: fitz.Page) -> tuple[str, fitz.Rect | None, str]:
    """Locate the value cell and return ``(value, bbox, extraction_method)``."""

    for table_index, table in enumerate(page.find_tables().tables):
        rows = table.extract()
        for row_index, row in enumerate(rows):
            for column_index, value in enumerate(row):
                if compact(value).replace("／", "/") != "检验项目":
                    continue
                value_index = next(
                    (index for index in range(column_index + 1, len(row)) if display_text(row[index])),
                    None,
                )
                if value_index is None:
                    continue
                text = display_text(row[value_index])
                rect: fitz.Rect | None = None
                cells = list(table.rows[row_index].cells) if row_index < len(table.rows) else []
                if value_index < len(cells) and cells[value_index] is not None:
                    rect = fitz.Rect(cells[value_index])
                if rect is None:
                    rect = find_exact_rect(page, text)
                return text, rect, f"pymupdf_table_cell:t{table_index + 1:02d}:r{row_index + 1:02d}:c{value_index + 1:02d}"
    fields = page_three_fields(page)
    value, rect = fields.get("检验项目", ("", None))
    return display_text(value), rect, "pymupdf_page_three_fields"


def extract_report_scope(report_path: str | Path, *, page_number: int = 3) -> dict[str, Any]:
    """Extract and parse the third-page Report inspection-items field."""

    path = Path(report_path).expanduser().resolve()
    source_sha256 = sha256_file(path)
    with fitz.open(path) as document:
        if page_number < 1 or page_number > document.page_count:
            return {
                "schema_version": PTR_SCOPE_SCHEMA_VERSION,
                "status": "manual",
                "raw_text": "",
                "source": {
                    "role": "report",
                    "pdf_page": page_number,
                    "bbox": rect_list(document[0].rect) if document.page_count else [0.0, 0.0, 1.0, 1.0],
                    "source_sha256": source_sha256,
                    "extraction_method": "page_missing",
                },
                "unresolved": [{"section": "source", "raw": "", "reason": "report_page_missing"}],
            }
        page = document[page_number - 1]
        text, rect, method = _inspection_items_cell(page)
        parsed = parse_scope_expression(text) if text else {
            "schema_version": PTR_SCOPE_SCHEMA_VERSION,
            "status": "manual",
            "raw_text": "",
            "main_text": "",
            "groups": [],
            "included_clause_ids": [],
            "excluded": [],
            "excluded_clause_ids": [],
            "only": [],
            "only_clause_ids": [],
            "unresolved": [{"section": "source", "raw": "", "reason": "inspection_items_field_missing"}],
        }
        parsed["source"] = {
            "role": "report",
            "pdf_page": page_number,
            "bbox": rect_list(rect or page.rect),
            "source_sha256": source_sha256,
            "coordinate_space": "pdf_points",
            "extraction_method": method,
            "text": text,
        }
        return parsed


__all__ = [
    "PTR_SCOPE_SCHEMA_VERSION",
    "PtrScopeParseError",
    "extract_report_scope",
    "normalize_standard",
    "parse_scope_expression",
]
