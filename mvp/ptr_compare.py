"""Deterministic PTR ↔ Report comparison draft.

The public PTR capability is still disabled.  This module is deliberately a
read-only, non-publishable comparison draft that turns the existing Report
table scanner and :mod:`mvp.ptr_scope` into an auditable comparison ledger.

The important safety rule is that an absent or ambiguous fact is ``manual``;
only evidence-backed, deterministic matches are ``matched``.  A future
``rules+llm`` caller may use the ``manual`` rows as a small review queue, but
this module never calls an LLM and never lets one replace a hard rule.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover - compatibility with older environments
    import fitz

from mvp.checker import (
    align_report_table_row,
    compact,
    display_text,
    header_map,
    is_formal_report_table,
    rect_list,
    report_page_tables,
    report_table_cell,
    scan_report_items,
    sha256_file,
)
from mvp.ptr_scope import extract_report_scope
from mvp.ptr_ocr import iter_ptr_ocr_lines
from mvp.ptr_llm import build_review_task, validate_llm_review


PTR_COMPARE_SCHEMA_VERSION = "ptr-compare-0.2"
PTR_COMPARE_RULE_VERSION = "0.2.0-draft"
PTR_COMPARE_RULE_ID = "PTR-P01"

MATCHED = "matched"
MISMATCH = "mismatch"
MANUAL = "manual"
EXCLUDED = "excluded"

# A clause begins with a dotted number and a human-readable title/body.  The
# body guard prevents table cells such as ``2.2±20%`` from becoming clauses.
CLAUSE_LINE_RE = re.compile(
    r"^\s*(?P<clause>\d+(?:\s*\.\s*\d+){1,8})\s*"
    r"(?:[.．、，,:：)）]\s*|\s+|(?=[\u3400-\u9fff]))(?P<body>.*\S)?\s*$"
)
# A token must be followed by a clause separator or whitespace plus a human
# readable title.  This avoids treating decimals such as ``2.76 μs`` and
# standard years such as ``9706.202-2021`` as clause boundaries.
CLAUSE_TOKEN_RE = re.compile(
    r"(?<![\d.])(?P<clause>\d+(?:\s*\.\s*\d+){1,8})"
    r"(?=(?:\s+[A-Za-z\u3400-\u9fff]|[.．、:：)）]\s*[A-Za-z\u3400-\u9fff]))"
)
STANDARD_RE = re.compile(r"(?:GB|YY)\s*\d+(?:\.\d+)+\s*-\s*\d{4}", re.IGNORECASE)
TABLE_RE = re.compile(r"(?:表|附录)\s*[0-9A-Za-z一二三四五六七八九十]+", re.IGNORECASE)
NUMBER_RE = re.compile(
    r"(?P<operator>[<>≤≥=≈~～±+\-])?\s*"
    r"(?P<number>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>%|[A-Za-zμµΩΩ°℃·]+(?:\s*/\s*(?:[A-Za-zμµΩΩ°℃·]+|件|器械))?)?"
)
UNIT_LABEL_RE = re.compile(
    r"单位\s*[:：]\s*([A-Za-zμµΩΩ°℃·]+(?:\s*/\s*(?:[A-Za-zμµΩΩ°℃·]+|件|器械))?)(?:\s*[pP])?",
    re.IGNORECASE,
)

_UNIT_PREFIX_SCALE = {
    "p": Decimal("1e-12"),
    "n": Decimal("1e-9"),
    "μ": Decimal("1e-6"),
    "u": Decimal("1e-6"),
    "m": Decimal("1e-3"),
    "c": Decimal("1e-2"),
    "k": Decimal("1e3"),
    "K": Decimal("1e3"),
    "M": Decimal("1e6"),
    "G": Decimal("1e9"),
}
_BASE_UNITS = {
    "A": "A",
    "V": "V",
    "W": "W",
    "Ω": "Ω",
    "Ω": "Ω",
    "ohm": "Ω",
    "Ohm": "Ω",
    "Hz": "Hz",
    "s": "s",
    "g": "g",
    "N": "N",
    "m": "m",
    "J": "J",
    "C": "C",
    "Pa": "Pa",
    "EU": "EU",
    "件": "件",
    "器械": "器械",
    "%": "%",
    "°C": "°C",
    "℃": "°C",
}


class PtrCompareError(ValueError):
    """Raised when a comparison draft cannot satisfy its input contract."""


def _canonical_clause_id(value: str) -> str:
    """Normalize dotted clause IDs such as ``201.7.2. 8.2``."""

    text = unicodedata.normalize("NFKC", str(value or ""))
    text = re.sub(r"\s+", "", text).rstrip(".．:：、)）")
    match = re.fullmatch(r"\d+(?:\.\d+){1,8}[A-Za-z]?", text)
    if not match:
        return text
    return text.lower()


def _clause_parent(clause_id: str) -> str | None:
    parts = clause_id.split(".")
    return ".".join(parts[:-1]) if len(parts) > 1 else None


def _clause_sort_key(clause_id: str) -> tuple[tuple[int, Any], ...]:
    parts = re.split(r"[.]", clause_id)
    key: list[tuple[int, Any]] = []
    for part in parts:
        match = re.fullmatch(r"(\d+)([a-z]?)", part)
        if match:
            key.append((0, int(match.group(1))))
            if match.group(2):
                key.append((1, match.group(2)))
        else:
            key.append((1, part))
    return tuple(key)


def _is_descendant(clause_id: str, scope_id: str) -> bool:
    """Return true for a selected clause and all of its children."""

    if clause_id == scope_id:
        return True
    if clause_id.startswith(scope_id + "."):
        return True
    # Lettered sub-items are represented as ``2.3.1a`` by ptr_scope.
    return bool(re.fullmatch(re.escape(scope_id) + r"[a-z]", clause_id))


def normalize_comparison_text(value: Any) -> str:
    """Remove presentation-only differences while keeping technical words.

    This intentionally does not perform synonym replacement or fuzzy matching.
    In particular, numbers, comparison signs, conditions and object names stay
    in the normalized string so a semantic change cannot be hidden by a score.
    """

    text = unicodedata.normalize("NFKC", str(value or ""))
    replacements = {
        "—": "-",
        "–": "-",
        "－": "-",
        "～": "~",
        "，": ",",
        "；": ";",
        "：": ":",
        "（": "(",
        "）": ")",
        "【": "[",
        "】": "]",
        "“": '"',
        "”": '"',
        "‘": "'",
        "’": "'",
        "µ": "μ",
        "Ω": "Ω",
        "×": "x",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    text = re.sub(r"(?i)(?<=\d)\s*u\s*(?=[gGlLmMnNsSvVaApPΩ])", "μ", text)
    text = re.sub(r"(?i)usec", "μs", text)
    text = re.sub(r"(?i)msec", "ms", text)
    # A colon directly joining Chinese label and Chinese value is a layout
    # choice (``细菌内毒素：应不超过`` versus ``细菌内毒素 应不超过``).
    text = re.sub(r"(?<=[\u3400-\u9fff]):\s*(?=[\u3400-\u9fff])", "", text)
    # PDF extraction frequently inserts spaces between every table column and
    # between a number and its unit.  Whitespace is a presentation detail for
    # the text rule; the numeric rule below still retains value/unit tokens.
    text = re.sub(r"\s+", "", text)
    return text.strip(".;。；")


def _strip_document_noise(value: str) -> str:
    """Remove repeating document watermarks before comparing requirements."""
    value = re.sub(
        r"intuitivesurgicalinc\.?专有机密文件.*?产品",
        "",
        value,
        flags=re.IGNORECASE,
    )
    return re.sub(r"技术要求修订版[:：]?[A-Z](?=[\u3400-\u9fffA-Z0-9]|$)", "", value, flags=re.IGNORECASE)


def _numeric_signature(value: Any) -> list[tuple[str, str, str]]:
    """Return ordered numeric facts as ``(operator, value, unit)`` tuples."""

    text = _numeric_source_text(value)
    result: list[tuple[str, str, str]] = []
    for match in NUMBER_RE.finditer(text):
        number = match.group("number")
        # Clause IDs have already been removed from individual node text.  Keep
        # all other numbers because a changed threshold or standard year must
        # remain a hard difference.
        unit = _clean_numeric_unit(match.group("unit") or "")
        operator = match.group("operator") or ""
        number, unit = _canonical_numeric_fact(number, unit)
        result.append((operator, number, unit))
    return result


def _raw_numeric_signature(value: Any) -> list[tuple[str, str, str]]:
    """Keep the written unit spelling for validating separate unit labels."""
    text = _numeric_source_text(value)
    return [
        (match.group("operator") or "", match.group("number"),
         _clean_numeric_unit(match.group("unit") or ""))
        for match in NUMBER_RE.finditer(text)
    ]


def _numeric_source_text(value: Any) -> str:
    """Remove references that look numeric but are not measured facts."""

    text = _strip_document_noise(normalize_comparison_text(value))
    # Chinese unit words in the source PDF are equivalent to their controlled
    # Latin spellings when they directly follow a number.
    text = re.sub(r"(?<=\d)(?:微米|μ米)", "μm", text)
    text = re.sub(r"(?<=\d)毫米", "mm", text)
    text = re.sub(r"(?<=\d)厘米", "cm", text)
    text = re.sub(r"(?<=\d)米", "m", text)
    text = re.sub(r"(?<=\d)秒", "s", text)
    text = re.sub(r"(?<!\d)(?:470066|470067)(?=(?:左路|右路|:))", "", text)
    text = STANDARD_RE.sub("", text)
    text = TABLE_RE.sub("", text)
    # Deep clause references (2.10.1.2) cannot be a scalar measurement.
    text = re.sub(r"\d+\.\d+(?:\.\d+)+", "", text)
    # Short references such as “符合2.1.4的要求” need their local context.
    text = re.sub(r"(?:符合|满足|见|第|条款|章节|引用|参见|按照)\d+(?:\.\d+)?", "", text)
    # Product/document identifiers such as IEEE802.3, RS485 and R2 are not
    # requirement measurements. Numeric values after a letter remain in the
    # text channel, but do not enter the numeric fact signature.
    text = re.sub(r"(?<=[A-Za-z])\d+(?:\.\d+)?(?:-[A-Za-z0-9]+)?", "", text)
    return text


def _clean_numeric_unit(value: str) -> str:
    unit = value.replace("µ", "μ").replace("Ω", "Ω")
    # PDF extraction can join a), b) list markers to the preceding unit:
    # ``10 nmb)`` means 10 nm, while ``12 Nmb)`` means 12 Nm.  Keep
    # uppercase base-unit letters (``mA``/``mV``) intact.
    return re.sub(r"(nm|μm|mm|cm|m|n|N|Ω|V|A|W|s)[ab]$", r"\1", unit)


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def _parse_unit_product(value: str) -> tuple[str, Decimal] | None:
    """Return a base-unit product and scale for a controlled unit token."""

    raw = value.replace("µ", "μ").replace("Ω", "Ω")
    if raw in _BASE_UNITS:
        return _BASE_UNITS[raw], Decimal(1)
    pieces = raw.split("·")
    canonical: list[str] = []
    scale = Decimal(1)
    for piece in pieces:
        if piece in _BASE_UNITS:
            canonical.append(_BASE_UNITS[piece])
            continue
        match = re.fullmatch(r"(?P<prefix>[pnuμmckKMG])(?P<base>.+)", piece)
        if not match or match.group("base") not in _BASE_UNITS:
            return None
        canonical.append(_BASE_UNITS[match.group("base")])
        scale *= _UNIT_PREFIX_SCALE[match.group("prefix")]
    return "·".join(canonical), scale


def _canonical_numeric_fact(number: str, unit: str) -> tuple[str, str]:
    """Convert safe SI-prefixed scalar/product units to base units.

    Unknown or semantic units such as ``EU/件`` remain textual.  This is a
    conservative conversion: it never guesses a dimension or converts a
    unit merely because its spelling looks similar.
    """

    if not unit or unit == "%":
        return number, unit
    try:
        numeric = Decimal(number)
    except (InvalidOperation, ValueError):
        return number, unit
    numerator, denominator = (unit.split("/", 1) + [""])[:2] if "/" in unit else (unit, "")
    numerator_parsed = _parse_unit_product(numerator)
    denominator_parsed = _parse_unit_product(denominator) if denominator else ("", Decimal(1))
    if numerator_parsed is None or denominator_parsed is None:
        return number, unit
    numerator_unit, numerator_scale = numerator_parsed
    denominator_unit, denominator_scale = denominator_parsed
    scaled = numeric * numerator_scale / denominator_scale
    canonical_unit = numerator_unit if not denominator else f"{numerator_unit}/{denominator_unit}"
    return _decimal_text(scaled), canonical_unit


def _references(value: Any) -> dict[str, list[str]]:
    text = _strip_document_noise(normalize_comparison_text(value))
    standards = sorted({normalize_comparison_text(item) for item in STANDARD_RE.findall(text)})
    tables = sorted({normalize_comparison_text(item) for item in TABLE_RE.findall(text)})
    return {"standards": standards, "tables": tables}


def _unit_labels(value: Any) -> list[str]:
    labels = []
    for match in UNIT_LABEL_RE.finditer(unicodedata.normalize("NFKC", str(value or ""))):
        label = normalize_comparison_text(match.group(1)).replace("µ", "μ").replace("Ω", "Ω")
        # ``N·m`` and ``Nm`` are two common PDF spellings of the same
        # controlled product unit.  Keep the original numeric facts intact;
        # this only canonicalizes a separately printed unit label.
        label = label.replace("·", "")
        if label:
            labels.append(label)
    return sorted(set(labels))


def _text_for_comparison(value: Any) -> str:
    """Drop redundant Report ``单位：X`` labels from the text channel.

    The labels are compared separately by ``_compare_text``.  This keeps a
    repeated unit declaration from becoming a text mismatch while still
    exposing a contradictory declaration (for example ``5 MΩ`` plus
    ``单位：GΩ``) as a hard unit finding.
    """

    text = unicodedata.normalize("NFKC", str(value or ""))
    text = UNIT_LABEL_RE.sub("", text)
    text = re.sub(r"(?<!\d)(?:470066|470067)(?=(?:左路|右路|:))", "", text)
    text = re.sub(r"(?:左路|右路)", "", text)
    return _strip_document_noise(normalize_comparison_text(text))


def _report_clause_matches(combined: str) -> list[re.Match[str]]:
    """Return clause markers, excluding decimal/product-number fragments."""

    result: list[re.Match[str]] = []
    for match in CLAUSE_TOKEN_RE.finditer(combined):
        clause_id = _canonical_clause_id(match.group("clause"))
        if not clause_id.startswith("2."):
            continue
        if clause_id == "2.0":
            continue
        prefix_raw = combined[: match.start()]
        prefix = prefix_raw.rstrip()
        immediate_newline = bool(prefix_raw and prefix_raw[-1] in "\r\n")
        if prefix and not immediate_newline and prefix[-1].isascii() and prefix[-1].isalnum():
            # ``USB 2.0 协议`` and similar product/version strings are not
            # clause boundaries.  Real nested clauses are normally introduced
            # by punctuation or start after a Chinese title.
            continue
        result.append(match)
    return result


def _looks_like_clause_body(body: str) -> bool:
    stripped = body.strip()
    if not stripped:
        return False
    # Numeric values and table fragments are not requirement headings.
    if stripped[0] in "±+-≤≥=<>~～/\\":
        return False
    if stripped[0].isdigit():
        return False
    return True


def _block_bbox(block: Sequence[Any]) -> list[float]:
    if len(block) < 4:
        return [0.0, 0.0, 1.0, 1.0]
    return [round(float(value), 3) for value in block[:4]]


def _evidence(
    *,
    role: str,
    page: int,
    bbox: Sequence[float],
    source_sha256: str,
    text: str,
    semantic_role: str,
    extraction_method: str,
    confidence: float | None = None,
) -> dict[str, Any]:
    evidence = {
        "role": role,
        "pdf_page": int(page),
        "bbox": [round(float(value), 3) for value in bbox],
        "source_sha256": source_sha256,
        "coordinate_space": "pdf_points",
        "text": str(text),
        "semantic_role": semantic_role,
        "extraction_method": extraction_method,
    }
    if confidence is not None:
        evidence["confidence"] = round(float(confidence), 4)
    return evidence


def _iter_text_lines(path: Path) -> Iterable[tuple[int, list[float], str, str]]:
    with fitz.open(path) as document:
        for page_number, page in enumerate(document, start=1):
            for block in page.get_text("blocks"):
                if len(block) < 5:
                    continue
                block_text = str(block[4] or "")
                for line in block_text.splitlines():
                    text = " ".join(line.split())
                    if text:
                        yield page_number, _block_bbox(block), text, "pymupdf_text_blocks"


def _parse_clause_lines(
    lines: Iterable[Sequence[Any]],
    *,
    role: str,
    source_sha256: str,
    stop_at_top_level: str | None = "3",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Parse a product requirement section from text blocks.

    A node is started only by a clause-looking line.  Continuation lines are
    retained until the next clause.  The first top-level ``3`` clause ends the
    product requirements section, preventing test-method references such as
    ``2.1.1 的要求`` from being mistaken for requirements.
    """

    nodes: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    seen: set[str] = set()
    stopped = False
    def flush_current() -> None:
        nonlocal current
        if current is None:
            return
        current["text"] = "\n".join(current.pop("_parts"))
        nodes.append(current)
        current = None

    for record in lines:
        if len(record) < 3:
            continue
        page, bbox, line = record[:3]
        extraction_method = str(record[3]) if len(record) >= 4 else "pymupdf_text_blocks"
        confidence = float(record[4]) if len(record) >= 5 and record[4] is not None else None
        if stop_at_top_level and re.match(
            rf"^\s*{re.escape(stop_at_top_level)}[.．]\s*(?=[A-Za-z\u3400-\u9fff])",
            line,
        ):
            flush_current()
            stopped = True
            continue
        match = CLAUSE_LINE_RE.match(line)
        candidate: tuple[str, str] | None = None
        if match:
            clause_id = _canonical_clause_id(match.group("clause"))
            body = (match.group("body") or "").strip()
            if _looks_like_clause_body(body):
                candidate = (clause_id, body)
        if candidate is not None:
            clause_id, body = candidate
            if stop_at_top_level and (clause_id == stop_at_top_level or clause_id.startswith(stop_at_top_level + ".")):
                flush_current()
                stopped = True
                continue
            if stopped:
                continue
            if not clause_id.startswith("2."):
                # Product sections are currently identified by the 2.x tree.
                # Other top-level clauses (1.x, 3.x) are not comparison nodes.
                continue
            if clause_id in seen:
                # The same clause can occur in a generated table or repeated
                # header.  Keep the first occurrence as the stable source.
                flush_current()
                continue
            flush_current()
            current = {
                "node_id": f"{role}:clause:{clause_id}",
                "role": role,
                "clause_id": clause_id,
                "parent_clause_id": _clause_parent(clause_id),
                "title": body,
                "text": body,
                "_parts": [body],
                "pages": [page],
                "evidence": [
                    _evidence(
                        role=role,
                        page=page,
                        bbox=bbox,
                        source_sha256=source_sha256,
                        text=line,
                        semantic_role="ptr_clause" if role == "ptr" else "report_clause",
                        extraction_method=extraction_method,
                        confidence=confidence,
                    )
                ],
            }
            seen.add(clause_id)
            continue
        if current is not None and not stopped:
            # Standalone PDF page labels (``3`` or ``3 / 40``) are not part of
            # the preceding requirement body.
            if re.fullmatch(r"\d+(?:\s*/\s*\d+)?", line):
                continue
            current["_parts"].append(line)
            if page not in current["pages"]:
                current["pages"].append(page)
            current["evidence"].append(
                _evidence(
                    role=role,
                    page=page,
                    bbox=bbox,
                    source_sha256=source_sha256,
                    text=line,
                    semantic_role="ptr_clause_continuation" if role == "ptr" else "report_clause_continuation",
                    extraction_method=extraction_method,
                    confidence=confidence,
                )
            )
    flush_current()
    for node in nodes:
        node["normalized_text"] = _text_for_comparison(node["text"])
        node["numeric_signature"] = _numeric_signature(node["text"])
        node["raw_numeric_signature"] = _raw_numeric_signature(node["text"])
        node["references"] = _references(node["text"])
        node["unit_labels"] = _unit_labels(node["text"])
        node["is_heading_only"] = len(node["text"].splitlines()) == 1 and not node["numeric_signature"]
    return nodes, {"candidate_count": len(nodes), "text_layer": bool(nodes)}


def extract_ptr_clauses(ptr_path: str | Path, *, ocr_mode: str = "auto") -> dict[str, Any]:
    """Extract product clauses and tables in physical reading order."""
    from mvp.ptr_layout import extract_ptr_layout
    if ocr_mode not in {"auto", "never", "required"}:
        raise PtrCompareError("ocr_mode 必须是 auto、never 或 required")
    result = extract_ptr_layout(Path(ptr_path).expanduser().resolve(), ocr_mode=ocr_mode)
    result["schema_version"] = PTR_COMPARE_SCHEMA_VERSION
    return result


def _report_cell_evidence_all(
    document: fitz.Document,
    source_sha256: str,
) -> dict[int, dict[str, list[dict[str, Any]]]]:
    """Collect physical Report cells for all inspection items in one pass."""

    values: dict[int, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    active_sequence: int | None = None
    for page in document:
        for table_index, table in enumerate(report_page_tables(page), start=1):
            if not is_formal_report_table(table):
                continue
            rows = table.extract()
            if not rows:
                continue
            mapping = header_map(rows[0])
            for row_index, raw_row in enumerate(rows[1:], start=1):
                row = align_report_table_row(table, row_index, raw_row)
                sequence_text = compact(row[mapping["序号"]]) if mapping["序号"] < len(row) else ""
                match = re.fullmatch(r"(续)?(\d+)", sequence_text)
                if match:
                    active_sequence = int(match.group(2))
                if active_sequence is None:
                    continue
                table_row = {
                    "pdf_page": page.number + 1,
                    "row_index": row_index,
                    "table_id": f"report:table:p{page.number + 1}:{table_index}",
                    "cells": [],
                    "all_cells": [
                        {
                            "column_index": index,
                            "text": display_text(value),
                            "bbox": rect_list(table.rows[row_index].cells[index])
                            if row_index < len(table.rows) and index < len(table.rows[row_index].cells) and table.rows[row_index].cells[index] is not None
                            else None,
                        }
                        for index, value in enumerate(row)
                    ],
                    "requirement_text": " ".join(
                        display_text(value)
                        for value in row[mapping["标准要求"] : mapping["检验结果"]]
                        if display_text(value)
                    ),
                    "matrix_row": [
                        display_text(value)
                        for value in row[mapping["标准要求"] : mapping["检验结果"]]
                    ],
                    "extraction_method": "pymupdf_report_table_cell",
                }
                for column, semantic_role in (
                    ("检验项目", "report_project"),
                    ("标准条款", "report_clause"),
                    ("标准要求", "report_requirement"),
                ):
                    value = display_text(row[mapping[column]]) if mapping[column] < len(row) else ""
                    cell = report_table_cell(table, row_index, mapping[column])
                    table_row["cells"].append(
                        {
                            "column": column,
                            "text": value,
                            "bbox": rect_list(cell) if cell is not None else None,
                        }
                    )
                    if not value or cell is None:
                        continue
                    values[active_sequence][column].append(
                        _evidence(
                            role="report",
                            page=page.number + 1,
                            bbox=rect_list(cell),
                            source_sha256=source_sha256,
                            text=value,
                            semantic_role=semantic_role,
                            extraction_method="pymupdf_report_table_cell",
                        )
                    )
                values[active_sequence]["_table_rows"].append(table_row)
    return {sequence: dict(columns) for sequence, columns in values.items()}


def _extract_ptr_tables(
    path: Path,
    source_sha256: str,
    *,
    pages: set[int] | None = None,
) -> list[dict[str, Any]]:
    """Extract deterministic vector tables where PyMuPDF can see a grid."""

    tables: list[dict[str, Any]] = []
    with fitz.open(path) as document:
        for page_number, page in enumerate(document, start=1):
            if pages is not None and page_number not in pages:
                continue
            try:
                found = page.find_tables().tables
            except Exception:
                found = []
            for table_index, table in enumerate(found, start=1):
                rows = table.extract()
                if not rows:
                    continue
                tables.append(
                    {
                        "pdf_page": page_number,
                        "table_index": table_index,
                        "bbox": [round(float(value), 3) for value in table.bbox],
                        "rows": [[display_text(cell) for cell in row] for row in rows],
                        "source_sha256": source_sha256,
                        "extraction_method": "pymupdf_find_tables",
                    }
                )
    return tables


def _attach_ptr_tables(nodes: Sequence[dict[str, Any]], tables: Sequence[Mapping[str, Any]]) -> None:
    """Attach page-local vector table observations without inferring semantics."""

    for node in nodes:
        pages = {int(page) for page in node.get("pages", [])}
        attached = [dict(table) for table in tables if int(table.get("pdf_page", 0)) in pages]
        node["table_rows"] = [
            {
                "pdf_page": table["pdf_page"],
                "bbox": table["bbox"],
                "rows": table["rows"],
                "source_sha256": table["source_sha256"],
                "extraction_method": table["extraction_method"],
            }
            for table in attached
        ]


def _table_fingerprint(rows: Any) -> tuple[tuple[str, ...], ...]:
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return ()
    normalized: list[tuple[str, ...]] = []
    for row in rows:
        if not isinstance(row, Sequence) or isinstance(row, (str, bytes)):
            continue
        cells = tuple(_text_for_comparison(cell) for cell in row)
        while cells and not cells[-1]:
            cells = cells[:-1]
        if cells and any(cells):
            normalized.append(cells)
    return tuple(normalized)


def _table_comparison(ptr: Mapping[str, Any], report: Mapping[str, Any]) -> dict[str, Any]:
    """Compare only physically extracted table matrices; otherwise stay manual."""

    references = (ptr.get("references") or {}).get("tables", []) or (report.get("references") or {}).get("tables", [])
    if not references:
        return {"status": MATCHED, "reason_code": None, "ptr_tables": 0, "report_tables": 0}
    ptr_tables = list(ptr.get("table_rows") or [])
    report_tables = list(report.get("table_rows") or [])
    ptr_fingerprints = {_table_fingerprint(table.get("rows")) for table in ptr_tables}
    grouped_report: dict[str, list[list[str]]] = defaultdict(list)
    for index, table in enumerate(report_tables):
        table_id = str(table.get("table_id") or f"row:{index}")
        row = table.get("matrix_row")
        if row is None:
            whole_rows = table.get("rows")
            if isinstance(whole_rows, Sequence) and whole_rows and all(
                isinstance(item, Sequence) and not isinstance(item, (str, bytes)) for item in whole_rows
            ):
                grouped_report[table_id].extend([[str(value or "") for value in item] for item in whole_rows])
                continue
            row = whole_rows or [cell.get("text", "") for cell in (table.get("all_cells") or table.get("cells", []))]
        if isinstance(row, Sequence) and not isinstance(row, (str, bytes)):
            grouped_report[table_id].append([str(value or "") for value in row])
    report_fingerprints = {_table_fingerprint(rows) for rows in grouped_report.values()}
    ptr_fingerprints.discard(())
    report_fingerprints.discard(())
    if ptr_fingerprints and report_fingerprints and ptr_fingerprints & report_fingerprints:
        return {
            "status": MATCHED,
            "reason_code": "TABLE_NORMALIZED_MATCH",
            "ptr_tables": len(ptr_fingerprints),
            "report_tables": len(report_fingerprints),
        }
    return {
        "status": MANUAL,
        "reason_code": "TABLE_CONTENT_NOT_EXTRACTED",
        "ptr_tables": len(ptr_fingerprints),
        "report_tables": len(report_fingerprints),
    }


def _numeric_facts_equal(left: Sequence[Any], right: Sequence[Any]) -> bool:
    """Treat a repeated Report table row as the same fact set."""
    left_list, right_list = list(left), list(right)
    if left_list == right_list:
        return True
    if left_list and len(right_list) % len(left_list) == 0:
        repeat = len(right_list) // len(left_list)
        if right_list == left_list * repeat:
            return True
    if right_list and len(left_list) % len(right_list) == 0:
        repeat = len(left_list) // len(right_list)
        if left_list == right_list * repeat:
            return True
    return False


def _node_table_evidence(node: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Turn attached table observations into ordinary auditable evidence."""

    source_sha256 = str(node.get("source_sha256") or "")
    if not source_sha256:
        source_sha256 = str((node.get("evidence") or [{}])[0].get("source_sha256") or "")
    result: list[dict[str, Any]] = []
    for index, table in enumerate(node.get("table_rows") or [], start=1):
        cells = table.get("cells") or table.get("all_cells") or []
        boxes = [cell.get("bbox") for cell in cells if isinstance(cell, Mapping) and cell.get("bbox")]
        bbox = table.get("bbox")
        if not bbox and boxes:
            bbox = [
                min(float(box[0]) for box in boxes),
                min(float(box[1]) for box in boxes),
                max(float(box[2]) for box in boxes),
                max(float(box[3]) for box in boxes),
            ]
        if not bbox:
            continue
        text = str(table.get("requirement_text") or "")
        if not text:
            rows = table.get("rows") or []
            text = " | ".join(" / ".join(str(cell or "") for cell in row) for row in rows[:20])
        result.append(
            _evidence(
                role=str(node.get("role") or ""),
                page=int(table.get("pdf_page", 0)),
                bbox=bbox,
                source_sha256=source_sha256,
                text=text,
                semantic_role=f"{node.get('role', 'document')}_table_{index}",
                extraction_method=str(table.get("extraction_method") or "table_observation"),
            )
        )
    return result


def _report_nodes_from_item(
    item: Mapping[str, Any],
    *,
    evidence: Mapping[str, Sequence[Mapping[str, Any]]],
    source_sha256: str,
) -> list[dict[str, Any]]:
    """Split one Report item into its parent and embedded product clauses."""

    sequence = int(item["sequence"])
    parent_clause = _canonical_clause_id(str(item.get("clause") or ""))
    project = display_text(item.get("project") or "")
    requirement_parts = [display_text(value) for value in item.get("requirements", []) if display_text(value)]
    for table_row in evidence.get("_table_rows", []):
        wide_requirement = display_text(table_row.get("requirement_text") or "")
        if wide_requirement and normalize_comparison_text(wide_requirement) not in {
            normalize_comparison_text(value) for value in requirement_parts
        }:
            requirement_parts.append(wide_requirement)
    requirement = "\n".join(requirement_parts)
    combined = requirement
    matches = _report_clause_matches(combined) if combined else []
    segments: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        clause_id = _canonical_clause_id(match.group("clause"))
        if not clause_id.startswith("2."):
            continue
        start = match.end()
        while start < len(combined) and combined[start] in " .．、:：)）\n\t":
            start += 1
        end = matches[index + 1].start() if index + 1 < len(matches) else len(combined)
        body = combined[start:end].strip(" \n")
        if _looks_like_clause_body(body):
            segments.append((clause_id, body))
    nodes: list[dict[str, Any]] = []
    table_rows = [dict(value) for value in evidence.get("_table_rows", [])]
    physical_evidence = [
        dict(value)
        for key, values in evidence.items()
        if not str(key).startswith("_")
        for value in values
    ]
    if parent_clause.startswith("2."):
        prefix = combined[: matches[0].start()].strip() if matches else combined.strip()
        parent_text = " ".join(item for item in (project, prefix) if item)
        if not parent_text:
            parent_text = project or requirement
        nodes.append(
            {
                "node_id": f"report:item:{sequence}:clause:{parent_clause}",
                "role": "report",
                "clause_id": parent_clause,
                "parent_clause_id": _clause_parent(parent_clause),
                "sequence": sequence,
                "title": project,
                "text": parent_text,
                "normalized_text": _text_for_comparison(parent_text),
                "numeric_signature": _numeric_signature(parent_text),
                "references": _references(parent_text),
                "unit_labels": _unit_labels(parent_text),
                "is_heading_only": not _numeric_signature(parent_text),
                "pages": sorted({int(page) for page in item.get("pages", [])}),
                "evidence": physical_evidence,
                "table_rows": table_rows,
                "source_sha256": source_sha256,
            }
        )
    for clause_id, body in segments:
        nodes.append(
            {
                "node_id": f"report:item:{sequence}:clause:{clause_id}",
                "role": "report",
                "clause_id": clause_id,
                "parent_clause_id": _clause_parent(clause_id),
                "sequence": sequence,
                "title": body.splitlines()[0] if body else "",
                "text": body,
                "normalized_text": _text_for_comparison(body),
                "numeric_signature": _numeric_signature(body),
                "references": _references(body),
                "unit_labels": _unit_labels(body),
                "is_heading_only": len(body.splitlines()) == 1 and not _numeric_signature(body),
                "pages": sorted({int(page) for page in item.get("pages", [])}),
                "evidence": physical_evidence,
                "table_rows": table_rows,
                "source_sha256": source_sha256,
            }
        )
    return nodes


def extract_report_clauses(report_path: str | Path) -> dict[str, Any]:
    """Extract all physical requirement columns, excluding Report results."""
    from mvp.ptr_layout import extract_report_layout
    result = extract_report_layout(Path(report_path).expanduser().resolve())
    result["schema_version"] = PTR_COMPARE_SCHEMA_VERSION
    return result


def _scope_decision(scope: Mapping[str, Any], clause_id: str) -> tuple[str, str]:
    if scope.get("status") != "parsed":
        return MANUAL, "SCOPE_UNRESOLVED"
    included = [str(value) for value in scope.get("included_clause_ids", [])]
    excluded = [str(value) for value in scope.get("excluded_clause_ids", [])]
    if any(_is_descendant(clause_id, value) for value in excluded):
        return EXCLUDED, "SCOPE_EXCLUDED"
    if any(_is_descendant(clause_id, value) for value in included):
        return "eligible", "SCOPE_INCLUDED"
    return EXCLUDED, "OUT_OF_SCOPE"


def _ocr_derived(node: Mapping[str, Any]) -> bool:
    """Return true when any evidence for a node came from local OCR."""

    return any(str(item.get("extraction_method", "")).startswith("ocr:") for item in node.get("evidence", []))


def _compare_text(ptr: Mapping[str, Any], report: Mapping[str, Any], *, has_children: bool) -> dict[str, Any]:
    ptr_text = str(ptr.get("normalized_text") or _text_for_comparison(ptr.get("text")))
    report_text = str(report.get("normalized_text") or _text_for_comparison(report.get("text")))
    if ptr_text == report_text:
        text_status = MATCHED
        reason = "EXACT_NORMALIZED_MATCH"
    elif ptr.get("is_heading_only") and report_text.startswith(ptr_text) and has_children:
        text_status = MATCHED
        reason = "HEADING_WITH_CHILDREN"
    elif report.get("is_heading_only") and ptr_text.startswith(report_text) and has_children:
        text_status = MATCHED
        reason = "REPORT_HEADING_WITH_CHILDREN"
    else:
        text_status = MISMATCH
        reason = "TEXT_MISMATCH"
    ptr_numbers = list(ptr.get("numeric_signature") or _numeric_signature(ptr.get("text")))
    report_numbers = list(report.get("numeric_signature") or _numeric_signature(report.get("text")))
    numeric_status = MATCHED if _numeric_facts_equal(ptr_numbers, report_numbers) else MISMATCH
    ptr_refs = ptr.get("references") or _references(ptr.get("text"))
    report_refs = report.get("references") or _references(report.get("text"))
    reference_status = MATCHED if ptr_refs == report_refs else MISMATCH
    ptr_units = list(ptr.get("unit_labels") or _unit_labels(ptr.get("text")))
    report_units = list(report.get("unit_labels") or _unit_labels(report.get("text")))
    ptr_raw_units = {unit for _, _, unit in (ptr.get("raw_numeric_signature") or _raw_numeric_signature(ptr.get("text"))) if unit}
    report_raw_units = {unit for _, _, unit in (report.get("raw_numeric_signature") or _raw_numeric_signature(report.get("text"))) if unit}
    if ptr_units and report_units:
        unit_status = MATCHED if ptr_units == report_units else MISMATCH
    elif report_units:
        # A Report unit column is acceptable when it repeats a unit already
        # attached to a numeric fact.  A contradictory label is a hard issue.
        if not report_numbers:
            unit_status = MATCHED
        elif ptr_raw_units:
            unit_status = MATCHED if set(report_units).issubset(ptr_raw_units) else MISMATCH
        elif report_raw_units:
            unit_status = MATCHED if set(report_units).issubset(report_raw_units) else MISMATCH
        else:
            report_fact_units = {unit for _, _, unit in report.get("numeric_signature", []) if unit}
            unit_status = MATCHED if set(report_units).issubset(report_fact_units) else MISMATCH
    else:
        unit_status = MATCHED
    table_comparison = _table_comparison(ptr, report)
    if numeric_status == MISMATCH and not (
        table_comparison["status"] == MANUAL
        and ((ptr_refs.get("tables") or report_refs.get("tables")))
    ):
        decision = MISMATCH
        decision_reason = "NUMERIC_FACT_MISMATCH"
    elif unit_status == MISMATCH:
        decision = MISMATCH
        decision_reason = "UNIT_MISMATCH"
    elif reference_status == MISMATCH:
        decision = MISMATCH
        decision_reason = "REFERENCE_MISMATCH"
    else:
        decision = text_status
        decision_reason = reason
    if (
        table_comparison["status"] == MANUAL
        and (ptr_refs.get("tables") or report_refs.get("tables"))
        and numeric_status == MISMATCH
    ):
        decision = MANUAL
        decision_reason = "TABLE_CONTENT_NOT_EXTRACTED"
    return {
        "title_comparison": {"status": text_status, "reason_code": reason},
        "text_comparison": {
            "status": text_status,
            "ptr_normalized": ptr_text,
            "report_normalized": report_text,
        },
        "numeric_comparison": {
            "status": numeric_status,
            "ptr": ptr_numbers,
            "report": report_numbers,
        },
        "reference_comparison": {
            "status": reference_status,
            "ptr": ptr_refs,
            "report": report_refs,
        },
        "unit_comparison": {
            "status": unit_status,
            "ptr": ptr_units,
            "report": report_units,
        },
        "table_comparison": table_comparison,
        "decision": decision,
        "reason_code": decision_reason,
    }


def _ledger_entry(
    *,
    ptr: Mapping[str, Any] | None,
    reports: Sequence[Mapping[str, Any]],
    scope_decision: str,
    scope_reason: str,
    comparison: Mapping[str, Any] | None,
    decision: str,
    reason_code: str,
) -> dict[str, Any]:
    clause_id = str((ptr or reports[0]).get("clause_id"))
    report_ids = [str(item["node_id"]) for item in reports]
    identity = str(ptr["node_id"]) if ptr else "|".join(report_ids)
    return {
        "id": f"comparison:{clause_id}:{identity}",
        "ptr_node_id": str(ptr["node_id"]) if ptr else None,
        "report_item_ids": report_ids,
        "clause_id": clause_id,
        "scope_decision": scope_decision,
        "scope_reason_code": scope_reason,
        "title_comparison": (comparison or {}).get("title_comparison"),
        "text_comparison": (comparison or {}).get("text_comparison"),
        "numeric_comparison": (comparison or {}).get("numeric_comparison"),
        "unit_comparison": (comparison or {}).get("unit_comparison"),
        "table_comparison": (comparison or {}).get("table_comparison"),
        "reference_comparison": (comparison or {}).get("reference_comparison"),
        "decision": decision,
        "reason_code": reason_code,
        "evidence": [
            *[dict(value) for value in (ptr or {}).get("evidence", [])],
            *[dict(value) for item in reports for value in item.get("evidence", [])],
            *(_node_table_evidence(ptr) if ptr else []),
            *[value for item in reports for value in _node_table_evidence(item)],
        ],
    }


def compare_ptr_report(
    report_path: str | Path,
    ptr_path: str | Path,
    *,
    root: str | Path | None = None,
    comparison_mode: str = "rules",
    ocr_mode: str = "auto",
) -> dict[str, Any]:
    """Build a non-publishable PTR ↔ Report comparison draft.

    ``comparison_mode`` accepts ``rules`` and ``rules+llm``.  The latter only
    labels the manual queue for a future adapter; it intentionally performs no
    model call, so an LLM can never be the sole comparison mechanism.
    """

    if comparison_mode not in {"rules", "rules+llm"}:
        raise PtrCompareError("comparison_mode 必须是 rules 或 rules+llm")
    report = extract_report_clauses(report_path)
    ptr = extract_ptr_clauses(ptr_path, ocr_mode=ocr_mode)
    scope = report["scope"]
    ptr_by_clause = {str(node["clause_id"]): node for node in ptr["nodes"]}
    report_by_clause = report["nodes_by_clause"]
    children: defaultdict[str, set[str]] = defaultdict(set)
    for node in ptr["nodes"]:
        parent = node.get("parent_clause_id")
        if parent:
            children[str(parent)].add(str(node["clause_id"]))
    ledger: list[dict[str, Any]] = []
    accounted_report_ids: set[str] = set()
    for clause_id, ptr_node in sorted(ptr_by_clause.items(), key=lambda item: _clause_sort_key(item[0])):
        scope_decision, scope_reason = _scope_decision(scope, clause_id)
        candidates = list(report_by_clause.get(clause_id, []))
        if scope_decision == EXCLUDED:
            ledger.append(
                _ledger_entry(
                    ptr=ptr_node,
                    reports=[],
                    scope_decision=scope_decision,
                    scope_reason=scope_reason,
                    comparison=None,
                    decision=EXCLUDED,
                    reason_code=scope_reason,
                )
            )
            continue
        if scope_decision == MANUAL:
            ledger.append(
                _ledger_entry(
                    ptr=ptr_node,
                    reports=candidates,
                    scope_decision=scope_decision,
                    scope_reason=scope_reason,
                    comparison=None,
                    decision=MANUAL,
                    reason_code=scope_reason,
                )
            )
            accounted_report_ids.update(str(item["node_id"]) for item in candidates)
            continue
        if not candidates:
            if ptr["diagnostics"].get("status") != "parsed":
                decision, reason = MANUAL, str(
                    ptr["diagnostics"].get("reason_code") or "PTR_UNREADABLE"
                )
            elif report["diagnostics"].get("status") != "parsed":
                decision, reason = MANUAL, str(
                    report["diagnostics"].get("reason_code") or "REPORT_UNREADABLE"
                )
            else:
                decision, reason = MISMATCH, "REPORT_CLAUSE_MISSING"
            ledger.append(
                _ledger_entry(
                    ptr=ptr_node,
                    reports=[],
                    scope_decision=scope_decision,
                    scope_reason=scope_reason,
                    comparison=None,
                    decision=decision,
                    reason_code=reason,
                )
            )
            continue
        if len(candidates) != 1:
            ledger.append(
                _ledger_entry(
                    ptr=ptr_node,
                    reports=candidates,
                    scope_decision=scope_decision,
                    scope_reason=scope_reason,
                    comparison=None,
                    decision=MANUAL,
                    reason_code="AMBIGUOUS_REPORT_MAPPING",
                )
            )
            accounted_report_ids.update(str(item["node_id"]) for item in candidates)
            continue
        report_node = candidates[0]
        accounted_report_ids.add(str(report_node["node_id"]))
        comparison = _compare_text(ptr_node, report_node, has_children=bool(children.get(clause_id)))
        decision = comparison["decision"]
        if comparison["table_comparison"]["status"] == MANUAL and decision == MATCHED:
            decision = MANUAL
            reason_code = "TABLE_CONTENT_NOT_EXTRACTED"
        elif _ocr_derived(ptr_node) or ptr.get("diagnostics", {}).get("ocr_used"):
            decision = MANUAL
            reason_code = "PTR_OCR_CANDIDATES_MANUAL"
        else:
            reason_code = comparison["reason_code"]
        if comparison_mode == "rules+llm" and decision == MISMATCH and comparison["reason_code"] == "TEXT_MISMATCH":
            # An optional LLM may later explain this row, but it cannot change
            # the hard-rule result in this draft.
            decision = MANUAL
            reason_code = "LLM_REVIEW_PENDING"
        ledger.append(
            _ledger_entry(
                ptr=ptr_node,
                reports=[report_node],
                scope_decision=scope_decision,
                scope_reason=scope_reason,
                comparison=comparison,
                decision=decision,
                reason_code=reason_code,
            )
        )
    # A Report product node without a PTR counterpart is an explicit coverage
    # mismatch, never silently ignored.
    for clause_id, candidates in sorted(report_by_clause.items()):
        scope_decision, scope_reason = _scope_decision(scope, clause_id)
        for report_node in candidates:
            if str(report_node["node_id"]) in accounted_report_ids:
                continue
            if scope_decision == EXCLUDED:
                decision, reason = EXCLUDED, scope_reason
            elif scope_decision == MANUAL:
                decision, reason = MANUAL, scope_reason
            elif ptr["diagnostics"].get("status") != "parsed":
                decision, reason = MANUAL, str(ptr["diagnostics"].get("reason_code") or "PTR_UNREADABLE")
            else:
                decision, reason = MISMATCH, "PTR_CLAUSE_MISSING"
            ledger.append(
                _ledger_entry(
                    ptr=None,
                    reports=[report_node],
                    scope_decision=scope_decision,
                    scope_reason=scope_reason,
                    comparison=None,
                    decision=decision,
                    reason_code=reason,
                )
            )
    counts = {MATCHED: 0, MISMATCH: 0, MANUAL: 0, EXCLUDED: 0}
    for entry in ledger:
        counts[str(entry["decision"])] = counts.get(str(entry["decision"]), 0) + 1
    if counts[MISMATCH]:
        overall = MISMATCH
    elif counts[MANUAL]:
        overall = MANUAL
    elif ledger:
        overall = MATCHED
    else:
        overall = MANUAL
    eligible_count = sum(1 for entry in ledger if entry["decision"] != EXCLUDED)
    llm_queue: list[dict[str, Any]] = []
    if comparison_mode == "rules+llm":
        ptr_by_id = {str(node["node_id"]): node for node in ptr["nodes"]}
        report_by_id = {str(node["node_id"]): node for node in report["nodes"]}
        for entry in ledger:
            if entry["decision"] == EXCLUDED:
                continue
            if entry["decision"] != MANUAL and entry.get("reason_code") not in {
                "TEXT_MISMATCH",
                "LLM_REVIEW_PENDING",
            }:
                continue
            ptr_node = ptr_by_id.get(str(entry.get("ptr_node_id")))
            report_nodes = [report_by_id[item] for item in entry.get("report_item_ids", []) if item in report_by_id]
            report_node = report_nodes[0] if report_nodes else None
            llm_queue.append(
                build_review_task(
                    clause_id=str(entry["clause_id"]),
                    reason_code=str(entry.get("reason_code") or "MANUAL_REVIEW"),
                    ptr_text=str((ptr_node or {}).get("text") or ""),
                    report_text=str((report_node or {}).get("text") or ""),
                    ptr_pages=(ptr_node or {}).get("pages", []),
                    report_pages=(report_node or {}).get("pages", []),
                )
            )
    return {
        "schema_version": PTR_COMPARE_SCHEMA_VERSION,
        "result_kind": "ptr_report_comparison_draft",
        "publishable": False,
        "comparison_mode": comparison_mode,
        "rule": {
            "id": PTR_COMPARE_RULE_ID,
            "version": PTR_COMPARE_RULE_VERSION,
            "status": "draft_disabled",
            "automatic_pass_forbidden": True,
        },
        "inputs": {
            "report": {"path": str(report_path), "source_sha256": report["source_sha256"]},
            "ptr": {"path": str(ptr_path), "source_sha256": ptr["source_sha256"]},
        },
        "scope": scope,
        "ptr": {"diagnostics": ptr["diagnostics"], "node_count": len(ptr["nodes"])},
        "report": {"diagnostics": report["diagnostics"], "node_count": len(report["nodes"])},
        "coverage_ledger": ledger,
        "summary": {
            "decision": overall,
            "counts": counts,
            "eligible": eligible_count,
            "accounted": sum(1 for entry in ledger if entry["decision"] != EXCLUDED),
            "conserved": len(ledger) == len({entry["id"] for entry in ledger}),
        },
        "llm": {
            "used": False,
            "policy": "仅处理规则 manual 队列；不能覆盖数值、范围、引用或硬差异。",
            "status": "not_configured" if comparison_mode == "rules+llm" else "disabled",
            "queue_count": len(llm_queue),
            "review_queue": llm_queue,
        },
    }


def write_ptr_report_comparison(
    report_path: str | Path,
    ptr_path: str | Path,
    output_path: str | Path,
    *,
    root: str | Path | None = None,
    comparison_mode: str = "rules",
    ocr_mode: str = "auto",
) -> dict[str, Any]:
    result = compare_ptr_report(
        report_path,
        ptr_path,
        root=root,
        comparison_mode=comparison_mode,
        ocr_mode=ocr_mode,
    )
    destination = Path(output_path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(destination)
    return result


def apply_llm_review(
    result: Mapping[str, Any],
    clause_id: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Apply one *validated* optional LLM review to a draft result.

    This function is deliberately narrower than a runner: it only consumes a
    queued text review, requires all numeric/unit/reference/table rules to be
    already matched, and keeps the overall result non-publishable.  A hard
    difference or an uncertain response remains unchanged.
    """

    if result.get("comparison_mode") != "rules+llm":
        raise PtrCompareError("只有 rules+llm 草案可以接收 LLM 复核")
    queue = list((result.get("llm") or {}).get("review_queue") or [])
    task = next((item for item in queue if str(item.get("clause_id")) == str(clause_id)), None)
    if task is None:
        raise PtrCompareError(f"条款 {clause_id} 不在 LLM 复核队列中")
    review = validate_llm_review(
        payload,
        ptr_text=str((task.get("ptr") or {}).get("text") or ""),
        report_text=str((task.get("report") or {}).get("text") or ""),
    )
    review["task_input_sha256"] = str(task.get("input_sha256") or "")
    import copy

    updated = copy.deepcopy(dict(result))
    entry = next(
        (item for item in updated.get("coverage_ledger", []) if str(item.get("clause_id")) == str(clause_id)),
        None,
    )
    if entry is None:
        raise PtrCompareError(f"找不到条款 {clause_id} 的 Coverage Ledger")
    entry["llm_review"] = review
    comparison = entry.get("numeric_comparison"), entry.get("unit_comparison"), entry.get("reference_comparison"), entry.get("table_comparison")
    hard_fields = [item for item in comparison if isinstance(item, Mapping)]
    hard_statuses = [item.get("status") for item in hard_fields]
    can_clear_text = (
        entry.get("decision") == MANUAL
        and review["decision"] == "equivalent"
        and bool(review.get("text_evidence"))
        and len(hard_fields) == 4
        and all(status == MATCHED for status in hard_statuses)
    )
    if can_clear_text:
        entry["decision"] = MATCHED
        entry["reason_code"] = "LLM_VERIFIED_TEXT_EQUIVALENCE"
    else:
        entry["reason_code"] = entry.get("reason_code") or "LLM_REVIEW_RETAINED_MANUAL"
    counts = {MATCHED: 0, MISMATCH: 0, MANUAL: 0, EXCLUDED: 0}
    for item in updated.get("coverage_ledger", []):
        counts[str(item.get("decision"))] = counts.get(str(item.get("decision")), 0) + 1
    if counts[MISMATCH]:
        overall = MISMATCH
    elif counts[MANUAL]:
        overall = MANUAL
    elif counts[MATCHED]:
        overall = MATCHED
    else:
        overall = MANUAL
    updated.setdefault("summary", {})["counts"] = counts
    updated["summary"]["decision"] = overall
    updated.setdefault("llm", {})["validated_reviews"] = int(updated.get("llm", {}).get("validated_reviews", 0)) + 1
    updated["publishable"] = False
    return updated


def run_llm_reviews(result: Mapping[str, Any], provider: Any) -> dict[str, Any]:
    """Run an explicitly supplied provider over the bounded manual queue.

    The provider is an injected object with ``review(task)``; this function is
    never called by :func:`compare_ptr_report` or the Draft Runner. Every
    response passes through :func:`apply_llm_review`, so a provider cannot
    change a numeric, unit, reference, range, or table hard finding.
    """

    if result.get("comparison_mode") != "rules+llm":
        raise PtrCompareError("只有 rules+llm 草案可以调用 LLM provider")
    review = getattr(provider, "review", None)
    if not callable(review):
        raise PtrCompareError("LLM provider 必须提供 review(task) 方法")
    updated = dict(result)
    provider_id = str(getattr(provider, "provider_id", provider.__class__.__name__))
    model_id = str(getattr(provider, "model_id", "unknown"))
    responses: list[dict[str, Any]] = []
    for task in list((updated.get("llm") or {}).get("review_queue") or []):
        payload = review(task)
        if not isinstance(payload, Mapping):
            raise PtrCompareError("LLM provider 返回值必须是 JSON 对象")
        updated = apply_llm_review(updated, str(task.get("clause_id")), payload)
        responses.append(
            {
                "clause_id": task.get("clause_id"),
                "input_sha256": task.get("input_sha256"),
                "validated": True,
            }
        )
    llm = dict(updated.get("llm") or {})
    llm.update(
        {
            "used": bool(responses),
            "status": "validated",
            "provider_id": provider_id,
            "model_id": model_id,
            "validated_responses": responses,
        }
    )
    updated["llm"] = llm
    updated["publishable"] = False
    return updated


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="生成不可发布的 PTR 与 Report 规则核对草案")
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--ptr", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--mode", choices=("rules", "rules+llm"), default="rules")
    parser.add_argument("--ocr-mode", choices=("auto", "never", "required"), default="auto")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = write_ptr_report_comparison(
        args.report,
        args.ptr,
        args.output,
        root=args.root,
        comparison_mode=args.mode,
        ocr_mode=args.ocr_mode,
    )
    print(
        json.dumps(
            {
                "result_kind": result["result_kind"],
                "publishable": result["publishable"],
                "decision": result["summary"]["decision"],
                "counts": result["summary"]["counts"],
                "output": str(args.output.resolve()),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "EXCLUDED",
    "MANUAL",
    "MATCHED",
    "MISMATCH",
    "PTR_COMPARE_RULE_ID",
    "PTR_COMPARE_RULE_VERSION",
    "PTR_COMPARE_SCHEMA_VERSION",
    "PtrCompareError",
    "apply_llm_review",
    "compare_ptr_report",
    "extract_ptr_clauses",
    "extract_report_clauses",
    "normalize_comparison_text",
    "run_llm_reviews",
    "write_ptr_report_comparison",
]
