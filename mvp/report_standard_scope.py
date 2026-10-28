"""Extract and resolve standard-specific sequence ranges declared by a Report.

Reports may contain more than one section for the same standard.  The
``型号规格或其他说明`` paragraph usually states which Report sequence numbers
belong to each standard, for example ``序号119～序号156 是 ... 9706.202``.
This module keeps those declarations as first-class, auditable scope objects so
Record comparison never has to guess which repeated standard section is meant.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import re
import unicodedata
from pathlib import Path
from typing import Any

try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover - compatibility with older environments
    import fitz


REPORT_STANDARD_SCOPE_SCHEMA_VERSION = "report-standard-scope-0.1"

# Keep the standard token optional because real reports sometimes write
# ``射频消融9706.202-2021`` without the GB prefix.
_STANDARD_TOKEN = r"(?:(?:GB|YY(?:\s*/\s*T)?)\s*)?\d+(?:\.\d+)+\s*-\s*\d{4}"
_DECLARATION_RE = re.compile(
    r"(?P<sequence_expression>"
    r"序号\s*\d+\s*(?:(?:～|~|至|-)\s*序号?\s*\d+|(?:、|,)\s*序号?\s*\d+)*"
    r")"
    r"\s*(?:是|为|属于)\s*"
    # A descriptive product label can sit between the predicate and the
    # standard token (``是射频消融 9706.202-2021 的内容``).
    r"[^，。；;]{0,24}?"
    r"(?P<standard>" + _STANDARD_TOKEN + r")"
    r"\s*(?:的内容|内容)?",
    re.IGNORECASE,
)


class ReportStandardScopeError(ValueError):
    """Base error for an invalid or ambiguous Report scope selection."""

    code = "REPORT_SCOPE_INVALID"

    def __init__(self, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = dict(details or {})


class ReportScopeSelectionRequired(ReportStandardScopeError):
    code = "REPORT_SCOPE_SELECTION_REQUIRED"


class ReportScopeNotFound(ReportStandardScopeError):
    code = "REPORT_SCOPE_NOT_FOUND"


def _compact(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", "", text)


def _canonical_standard(value: str) -> str:
    compacted = _compact(value).upper().replace("／", "/")
    match = re.fullmatch(r"(?:(GB|YY(?:/T)?)?)(\d+(?:\.\d+)+-\d{4})", compacted)
    if not match:
        return str(value or "").strip()
    prefix = match.group(1)
    if prefix is None:
        prefix = "GB" if match.group(2).startswith("9706.") else ""
    return f"{prefix} {match.group(2)}".strip()


def _standard_key(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", _canonical_standard(value).upper())


def _sequence_ids(expression: str) -> list[int]:
    """Expand a ``序号`` expression without duplicating range endpoints."""

    text = _compact(expression)
    values: list[int] = []
    consumed: list[tuple[int, int]] = []
    for match in re.finditer(r"序号(\d+)(?:[～~至-]序号?(\d+))?", text):
        first = int(match.group(1))
        last = int(match.group(2) or first)
        if last < first or last - first > 2000:
            continue
        values.extend(range(first, last + 1))
        consumed.append(match.span())
    # The regular expression above already includes list members, but keep the
    # de-duplication explicit for malformed declarations such as 1～3、3～4.
    return sorted(set(values))


def _line_records(page: fitz.Page) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for block in page.get_text("dict").get("blocks", []):
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            text = "".join(str(span.get("text", "")) for span in spans).strip()
            if not text:
                continue
            lines.append({"text": text, "bbox": [float(v) for v in line["bbox"]]})
    return lines


def _line_bbox(lines: Sequence[Mapping[str, Any]], start: int, end: int) -> list[float]:
    """Map a compact-text match back to the physical lines that contain it."""

    cursor = 0
    selected: list[Sequence[float]] = []
    for line in lines:
        length = len(_compact(line.get("text", "")))
        line_start, line_end = cursor, cursor + length
        if line_end > start and line_start < end:
            selected.append(line["bbox"])
        cursor = line_end
    if not selected:
        return [0.0, 0.0, 1.0, 1.0]
    return [
        min(float(box[0]) for box in selected),
        min(float(box[1]) for box in selected),
        max(float(box[2]) for box in selected),
        max(float(box[3]) for box in selected),
    ]


def _scope_id(standard: str, sequence_start: int, sequence_end: int, ordinal: int) -> str:
    # The ordinal makes repeated declarations deterministic even if two
    # declarations happen to use the same range.
    return f"{_standard_key(standard)}:{sequence_start}-{sequence_end}#{ordinal}"


def _fallback_scope(
    standard: str,
    sequence_start: int,
    sequence_end: int,
) -> dict[str, Any]:
    sequence_ids = list(range(sequence_start, sequence_end + 1))
    return {
        "schema_version": REPORT_STANDARD_SCOPE_SCHEMA_VERSION,
        "scope_id": _scope_id(standard, sequence_start, sequence_end, 1),
        "standard": _canonical_standard(standard),
        "sequence_start": sequence_start,
        "sequence_end": sequence_end,
        "sequence_ids": sequence_ids,
        "sequence_count": len(sequence_ids),
        "raw_text": "",
        "source": {"pdf_page": None, "bbox": None, "extraction_method": "implicit_default"},
        "status": "implicit",
    }


def _default_range_for_mode(mode: str) -> tuple[str, int, int]:
    if mode in {"report_record_9706_1", "record61", "9706.1"}:
        return "GB 9706.1-2020", 1, 118
    if mode in {"report_record_9706_202", "record202", "9706.202"}:
        return "GB 9706.202-2021", 119, 156
    raise ReportScopeNotFound(f"不支持按模式解析 Report 标准范围: {mode}")


def extract_report_standard_scopes(report_path: str | Path) -> dict[str, Any]:
    """Extract all standard/sequence declarations from the Report front matter."""

    path = Path(report_path).expanduser().resolve()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    scopes: list[dict[str, Any]] = []
    pages_scanned: list[int] = []
    with fitz.open(path) as document:
        # The declaration normally appears on the page printed as page 3, but
        # a PDF cover can add a title page. Scan the first 12 physical pages.
        for page_index in range(min(document.page_count, 12)):
            page = document[page_index]
            lines = _line_records(page)
            compact_text = "".join(_compact(line["text"]) for line in lines)
            if "序号" not in compact_text or "9706" not in compact_text:
                continue
            pages_scanned.append(page_index + 1)
            for match in _DECLARATION_RE.finditer(compact_text):
                ids = _sequence_ids(match.group("sequence_expression"))
                if not ids:
                    continue
                standard = _canonical_standard(match.group("standard"))
                start, end = min(ids), max(ids)
                ordinal = sum(1 for item in scopes if _standard_key(item["standard"]) == _standard_key(standard)) + 1
                scopes.append(
                    {
                        "schema_version": REPORT_STANDARD_SCOPE_SCHEMA_VERSION,
                        "scope_id": _scope_id(standard, start, end, ordinal),
                        "standard": standard,
                        "sequence_start": start,
                        "sequence_end": end,
                        "sequence_ids": ids,
                        "sequence_count": len(ids),
                        "raw_text": compact_text[match.start() : match.end()],
                        "source": {
                            "pdf_page": page_index + 1,
                            "bbox": _line_bbox(lines, match.start(), match.end()),
                            "extraction_method": "report_front_matter_standard_declaration",
                        },
                        "status": "parsed",
                    }
                )
    # Preserve declaration order and remove an accidental duplicate emitted by
    # overlapping PDF text blocks.
    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, tuple[int, ...]]] = set()
    for item in scopes:
        key = (_standard_key(item["standard"]), tuple(item["sequence_ids"]))
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return {
        "schema_version": REPORT_STANDARD_SCOPE_SCHEMA_VERSION,
        "status": "parsed" if unique else "manual",
        "source": {"path": str(path), "source_sha256": digest, "pages_scanned": pages_scanned},
        "scopes": unique,
        "unresolved": [] if unique else [{"reason": "standard_sequence_declaration_not_found"}],
    }


def scopes_for_mode(scopes: Sequence[Mapping[str, Any]], mode: str) -> list[dict[str, Any]]:
    standard, _, _ = _default_range_for_mode(mode)
    wanted = _standard_key(standard)
    return [dict(item) for item in scopes if _standard_key(str(item.get("standard", ""))) == wanted]


def resolve_report_scope(
    report_path: str | Path,
    *,
    mode: str,
    selection: Mapping[str, Any] | str | None = None,
) -> dict[str, Any]:
    """Resolve one selected scope, refusing ambiguous repeated standards."""

    declared = extract_report_standard_scopes(report_path)
    candidates = scopes_for_mode(declared.get("scopes", []), mode)
    default_standard, default_start, default_end = _default_range_for_mode(mode)
    if not candidates:
        return _fallback_scope(default_standard, default_start, default_end)

    if selection is None:
        if len(candidates) == 1:
            return candidates[0]
        raise ReportScopeSelectionRequired(
            "Report 中同一标准存在多个序号区段，请先选择需要核对的区段",
            details={"mode": mode, "candidates": candidates},
        )

    selected_id = str(selection.get("scope_id") if isinstance(selection, Mapping) else selection)
    matches = [item for item in candidates if item.get("scope_id") == selected_id]
    if not matches and isinstance(selection, Mapping):
        wanted_standard = _standard_key(str(selection.get("standard", "")))
        try:
            wanted_start = int(selection.get("sequence_start"))
            wanted_end = int(selection.get("sequence_end"))
        except (TypeError, ValueError):
            wanted_start = wanted_end = -1
        matches = [
            item for item in candidates
            if _standard_key(str(item.get("standard", ""))) == wanted_standard
            and int(item.get("sequence_start", -1)) == wanted_start
            and int(item.get("sequence_end", -1)) == wanted_end
        ]
    if len(matches) != 1:
        raise ReportScopeNotFound(
            "选择的 Report 标准序号区段不存在或不唯一",
            details={"mode": mode, "selection": selection, "candidates": candidates},
        )
    return matches[0]


def validate_report_scope_for_mode(scope: Mapping[str, Any], mode: str) -> None:
    """Ensure a selected segment can map to the corresponding Record template."""

    _, expected_start, expected_end = _default_range_for_mode(mode)
    ids = [int(value) for value in scope.get("sequence_ids", [])]
    start = int(scope.get("sequence_start", -1))
    end = int(scope.get("sequence_end", -1))
    if start != min(ids or [0]) or end != max(ids or [0]) or ids != list(range(start, end + 1)):
        raise ReportScopeNotFound("Report 标准范围必须是连续的序号区段", details={"scope": dict(scope)})
    expected_count = expected_end - expected_start + 1
    if len(ids) != expected_count:
        raise ReportScopeNotFound(
            f"所选 Report 区段包含 {len(ids)} 个序号，但 {mode} 原始记录需要 {expected_count} 个序号",
            details={"scope": dict(scope), "expected_count": expected_count},
        )


__all__ = [
    "REPORT_STANDARD_SCOPE_SCHEMA_VERSION",
    "ReportScopeNotFound",
    "ReportScopeSelectionRequired",
    "extract_report_standard_scopes",
    "resolve_report_scope",
    "scopes_for_mode",
    "validate_report_scope_for_mode",
]
