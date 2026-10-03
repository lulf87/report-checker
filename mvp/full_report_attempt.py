"""Reusable, deterministic probes for a fuller Report self-check attempt.

Only the R07-B numeric scan is implemented here.  The scanner reads native
PDF tables, preserves evidence coordinates, and returns ``manual`` whenever a
decision would require an unconfirmed result convention, a unit conversion,
or interpretation of an unsupported suffix.
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from typing import Any

try:
    import pymupdf as fitz
except ImportError:
    import fitz


HEADER_NAMES = ("序号", "检验项目", "标准条款", "标准要求", "检验结果", "单项结论", "备注")
UPPER_WORDS = ("不应大于", "不应超过", "不得超过", "不超过")
LOWER_WORDS = ("不应小于", "应不小于", "不小于", "至少")

_NUMERIC_UNITS = r"%|mA|µA|μA|Ω|kΩ|MΩ|GΩ|Hz|KHz|kHz|s|ns|V|Vp|mm|dB(?:\(A\))?|pF|PF"
_SIMPLE_UNITS = r"mA|µA|μA|Ω|kΩ|MΩ|GΩ|Hz|KHz|kHz|s|ns|V|Vp|mm|dB\(A\)|pF|PF|%"


def _compact(value: Any) -> str:
    return re.sub(r"\s+", "", "" if value is None else str(value))


def _display(value: Any) -> str:
    return re.sub(r"\s+", " ", "" if value is None else str(value)).strip()


def _header_map(row: list[Any]) -> dict[str, int]:
    normalized = [_compact(cell) for cell in row]
    mapping: dict[str, int] = {}
    for name in HEADER_NAMES:
        for index, value in enumerate(normalized):
            if value == name:
                mapping[name] = index
                break
    return mapping


def _is_formal_report_table(table: fitz.table.Table) -> bool:
    rows = table.extract()
    if not rows:
        return False
    mapping = _header_map(rows[0])
    required = {"序号", "检验项目", "标准条款", "标准要求", "检验结果", "单项结论"}
    return required.issubset(mapping)


def _normalized_math(text: str) -> str:
    return (
        _compact(text)
        .replace("＜", "<")
        .replace("＞", ">")
        .replace("≦", "≤")
        .replace("≧", "≥")
        .replace("~", "～")
        .replace("−", "-")
    )


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _printed_page_number(page: fitz.Page) -> int | None:
    match = re.search(r"共\s*\d+\s*页\s*第\s*(\d+)\s*页", page.get_text("text"))
    return int(match.group(1)) if match else None


def numeric_result_kind(text: str) -> str | None:
    """Return the supported measurement syntax, excluding IDs and references."""

    value = _normalized_math(text)
    if not value or not re.search(r"\d", value):
        return None
    if re.match(r"见序号", value) or re.match(r"[A-Za-z]+\d", value):
        return None
    if re.fullmatch(r"[<>≤≥]?[+-]?\d+(?:\.\d+)?%?(?:\([^)]*\))?", value):
        return "scalar"
    if re.fullmatch(r"[+-]?\d+(?:\.\d+)?%?～[+-]?\d+(?:\.\d+)?%?", value):
        return "range"
    return None


def _parse_number(token: str) -> Decimal:
    return Decimal(token.replace("%", ""))


def _parse_result(text: str) -> dict[str, Any]:
    value = _normalized_math(text)
    range_match = re.fullmatch(
        r"(?P<a>[+-]?\d+(?:\.\d+)?)(?P<pa>%)?～(?P<b>[+-]?\d+(?:\.\d+)?)(?P<pb>%)?",
        value,
    )
    if range_match:
        first = _parse_number(range_match.group("a"))
        second = _parse_number(range_match.group("b"))
        return {
            "kind": "range",
            "low": min(first, second),
            "high": max(first, second),
            "unit": "%" if range_match.group("pa") or range_match.group("pb") else None,
        }

    scalar_match = re.fullmatch(
        r"(?P<op><|>|≤|≥)?(?P<number>[+-]?\d+(?:\.\d+)?)(?P<percent>%)?(?P<suffix>\([^)]*\))?",
        value,
    )
    if not scalar_match:
        raise ValueError(f"unsupported numeric result: {text!r}")
    number_text = scalar_match.group("number")
    return {
        "kind": "scalar",
        "operator": scalar_match.group("op") or "=",
        "value": _parse_number(number_text),
        "unit": "%" if scalar_match.group("percent") else None,
        "suffix": scalar_match.group("suffix"),
        "explicit_sign": number_text.startswith(("+", "-")),
    }


def _declared_unit(requirement: str) -> str | None:
    match = re.search(r"单位[:：]([A-Za-zµμΩ%()]+)", _normalized_math(requirement), re.IGNORECASE)
    return match.group(1) if match else None


def _unit_scale_conflict(requirement: str) -> bool:
    """Detect comparisons that need a disabled SI-prefix conversion."""

    text = _normalized_math(requirement)
    declared = _declared_unit(text)
    if not declared:
        return False
    numeric_units = re.findall(r"\d+(?:\.\d+)?\s*(KHz|kHz|Hz|GΩ|MΩ|kΩ|Ω)", text)
    families = (
        {"Hz", "KHz", "kHz"},
        {"Ω", "kΩ", "MΩ", "GΩ"},
    )
    return any(
        declared in family and any(unit in family and unit != declared for unit in numeric_units)
        for family in families
    )


def _criterion_from_requirement(requirement: str) -> dict[str, Any] | None:
    text = _normalized_math(requirement)

    deviation = re.search(r"偏差[^。；;]*?±(?P<t>\d+(?:\.\d+)?)%", text)
    if deviation:
        tolerance = _parse_number(deviation.group("t"))
        return {
            "kind": "interval",
            "low": -tolerance,
            "high": tolerance,
            "unit": "%",
            "source": deviation.group(0),
        }

    resolved = re.search(rf"取(?P<n>\d+(?:\.\d+)?)(?P<u>{_SIMPLE_UNITS})", text)
    if resolved:
        number = _parse_number(resolved.group("n"))
        if any(word in text for word in LOWER_WORDS):
            return {
                "kind": "lower",
                "value": number,
                "inclusive": True,
                "unit": resolved.group("u"),
                "source": resolved.group(0),
            }
        if any(word in text for word in UPPER_WORDS):
            return {
                "kind": "upper",
                "value": number,
                "inclusive": True,
                "unit": resolved.group("u"),
                "source": resolved.group(0),
            }

    formula_values = re.findall(rf"=(\d+(?:\.\d+)?)\[?({_SIMPLE_UNITS})\]?", text)
    if formula_values and any(word in text for word in UPPER_WORDS):
        number, unit = formula_values[-1]
        return {
            "kind": "upper",
            "value": _parse_number(number),
            "inclusive": True,
            "unit": unit,
            "source": f"={number}{unit}",
        }

    symbolic = re.findall(rf"(≤|≥|<|>)(\d+(?:\.\d+)?)({_NUMERIC_UNITS})?", text)
    if symbolic:
        operator, number, unit = symbolic[-1]
        return {
            "kind": "upper" if operator in {"≤", "<"} else "lower",
            "value": _parse_number(number),
            "inclusive": operator in {"≤", "≥"},
            "unit": unit or _declared_unit(text),
            "source": f"{operator}{number}{unit}",
        }

    limit_value = re.search(rf"限值[:：]?(\d+(?:\.\d+)?)({_NUMERIC_UNITS})?", text)
    if limit_value:
        number, unit = limit_value.groups()
        kind = "lower" if any(word in text for word in LOWER_WORDS) else "upper"
        return {
            "kind": kind,
            "value": _parse_number(number),
            "inclusive": True,
            "unit": unit or _declared_unit(text),
            "source": limit_value.group(0),
        }

    for words, kind in ((UPPER_WORDS, "upper"), (LOWER_WORDS, "lower")):
        alternation = "|".join(map(re.escape, words))
        phrase = re.search(
            rf"(?:{alternation})[^。；;]*?(\d+(?:\.\d+)?)({_NUMERIC_UNITS})?",
            text,
        )
        if phrase:
            number, unit = phrase.groups()
            return {
                "kind": kind,
                "value": _parse_number(number),
                "inclusive": True,
                "unit": unit or _declared_unit(text),
                "source": phrase.group(0),
            }

    unchanged = re.search(r"(\d+(?:\.\d+)?)%(?:的)?限制不变", text)
    if unchanged:
        return {
            "kind": "upper",
            "value": _parse_number(unchanged.group(1)),
            "inclusive": True,
            "unit": "%",
            "source": unchanged.group(0),
        }
    return None


def _public_criterion(criterion: dict[str, Any] | None) -> dict[str, Any] | None:
    if criterion is None:
        return None
    return {
        key: _decimal_text(value) if isinstance(value, Decimal) else value
        for key, value in criterion.items()
    }


def evaluate_numeric_measurement(requirement: str, result_text: str) -> dict[str, Any]:
    """Evaluate one Report result against its acceptance text.

    The return value always contains ``status``, ``reason``, and ``criterion``.
    ``status`` is one of ``pass``, ``error``, or ``manual``.
    """

    if numeric_result_kind(result_text) is None:
        return {"status": "manual", "reason": "unsupported_numeric_result_syntax", "criterion": None}
    parsed = _parse_result(result_text)

    if parsed.get("suffix"):
        return {"status": "manual", "reason": "numeric_result_has_unconfirmed_suffix", "criterion": None}

    nominal_tolerance = re.search(
        r"(?P<center>\d+(?:\.\d+)?)(?P<u>[A-Za-zµμ]+)±(?P<t>\d+(?:\.\d+)?)(?P=u)",
        _normalized_math(requirement),
    )
    if (
        nominal_tolerance
        and parsed.get("kind") == "scalar"
        and parsed.get("explicit_sign")
        and "偏差" not in _normalized_math(requirement)
    ):
        return {
            "status": "manual",
            "reason": "signed_result_may_be_deviation_or_absolute_value",
            "criterion": None,
        }

    if _unit_scale_conflict(requirement):
        return {
            "status": "manual",
            "reason": "unit_prefix_conversion_required_but_disabled",
            "criterion": None,
        }

    criterion = _criterion_from_requirement(requirement)
    public_criterion = _public_criterion(criterion)
    if criterion is None:
        return {"status": "manual", "reason": "acceptance_criterion_not_resolved", "criterion": None}

    if criterion["kind"] == "interval":
        if parsed["kind"] == "range":
            passed = parsed["low"] >= criterion["low"] and parsed["high"] <= criterion["high"]
            return {
                "status": "pass" if passed else "error",
                "reason": "range_contained" if passed else "range_exceeds_tolerance",
                "criterion": public_criterion,
            }
        if parsed["kind"] == "scalar" and parsed["operator"] == "=":
            passed = criterion["low"] <= parsed["value"] <= criterion["high"]
            return {
                "status": "pass" if passed else "error",
                "reason": "value_in_tolerance" if passed else "value_outside_tolerance",
                "criterion": public_criterion,
            }
        return {
            "status": "manual",
            "reason": "result_interval_not_bounded_for_tolerance",
            "criterion": public_criterion,
        }

    bound = criterion["value"]
    if parsed["kind"] == "range":
        passed = parsed["high"] <= bound if criterion["kind"] == "upper" else parsed["low"] >= bound
        return {
            "status": "pass" if passed else "error",
            "reason": "range_within_bound" if passed else "range_exceeds_bound",
            "criterion": public_criterion,
        }

    operator = parsed["operator"]
    value = parsed["value"]
    if operator == "=":
        if criterion["kind"] == "upper":
            passed = value <= bound if criterion["inclusive"] else value < bound
        else:
            passed = value >= bound if criterion["inclusive"] else value > bound
        return {
            "status": "pass" if passed else "error",
            "reason": "value_within_bound" if passed else "value_exceeds_bound",
            "criterion": public_criterion,
        }

    if operator in {"<", "≤"} and criterion["kind"] == "upper" and value <= bound:
        return {
            "status": "pass",
            "reason": "censored_upper_interval_within_bound",
            "criterion": public_criterion,
        }
    if operator in {">", "≥"} and criterion["kind"] == "lower" and value >= bound:
        return {
            "status": "pass",
            "reason": "censored_lower_interval_within_bound",
            "criterion": public_criterion,
        }
    return {"status": "manual", "reason": "censored_result_not_decisive", "criterion": public_criterion}


def scan_report_numeric_measurements(document: fitz.Document) -> dict[str, Any]:
    """Scan all formal Report tables and return traceable R07-B decisions.

    Nested acceptance tables are handled structurally: every physical column
    from the first ``标准要求`` header up to, but excluding, ``检验结果`` is
    part of the acceptance side.  No page number, item number, or threshold is
    hard-coded.
    """

    active_sequence: int | None = None
    active_project = ""
    active_clause = ""
    inherited_requirement = ""
    active_standard_context: list[str] = []
    active_standard_columns: tuple[int, ...] = ()
    digit_cells = 0
    formal_table_pages: set[int] = set()
    excluded_numeric_tokens: list[dict[str, Any]] = []
    comparisons: list[dict[str, Any]] = []

    for page in document:
        printed_page = _printed_page_number(page)
        for table in page.find_tables().tables:
            if not _is_formal_report_table(table):
                continue
            formal_table_pages.add(page.number + 1)
            rows = table.extract()
            mapping = _header_map(rows[0])
            standard_columns = tuple(range(mapping["标准要求"], mapping["检验结果"]))
            result_index = mapping["检验结果"]
            if standard_columns != active_standard_columns:
                active_standard_columns = standard_columns
                active_standard_context = [""] * len(standard_columns)

            for row_index, row in enumerate(rows[1:], start=1):
                sequence_text = _compact(row[mapping["序号"]]) if mapping["序号"] < len(row) else ""
                sequence_match = re.fullmatch(r"(续)?(\d+)", sequence_text)
                if sequence_match:
                    sequence = int(sequence_match.group(2))
                    if active_sequence != sequence:
                        active_project = ""
                        active_clause = ""
                        inherited_requirement = ""
                        active_standard_context = [""] * len(standard_columns)
                    active_sequence = sequence

                project = _display(row[mapping["检验项目"]]) if mapping["检验项目"] < len(row) else ""
                clause = _display(row[mapping["标准条款"]]) if mapping["标准条款"] < len(row) else ""
                if project:
                    active_project = project
                if clause:
                    active_clause = clause

                row_components = [
                    _display(row[index]) if index < len(row) else ""
                    for index in standard_columns
                ]
                for level, component in enumerate(row_components):
                    if not component:
                        continue
                    active_standard_context[level] = component
                    for deeper in range(level + 1, len(active_standard_context)):
                        active_standard_context[deeper] = ""
                if any(row_components):
                    requirement = " | ".join(value for value in active_standard_context if value)
                    inherited_requirement = requirement
                else:
                    requirement = inherited_requirement

                result_text = _display(row[result_index]) if result_index < len(row) else ""
                if re.search(r"\d", result_text):
                    digit_cells += 1
                result_kind = numeric_result_kind(result_text)
                if result_kind is None:
                    if re.search(r"\d", result_text):
                        excluded_numeric_tokens.append(
                            {
                                "pdf_page": page.number + 1,
                                "printed_page": printed_page,
                                "row_index": row_index,
                                "sequence": active_sequence,
                                "result": result_text,
                                "reason": "digit_containing_non_measurement_token",
                            }
                        )
                    continue

                decision = evaluate_numeric_measurement(requirement, result_text)
                cell = table.rows[row_index].cells[result_index]
                comparisons.append(
                    {
                        "pdf_page": page.number + 1,
                        "printed_page": printed_page,
                        "row_index": row_index,
                        "sequence": active_sequence,
                        "project": active_project,
                        "clause": active_clause,
                        "standard_columns": list(standard_columns),
                        "requirement_components": [value for value in active_standard_context if value],
                        "requirement": requirement,
                        "result": result_text,
                        "result_kind": result_kind,
                        **decision,
                        "result_rect": [round(value, 3) for value in cell] if cell else None,
                    }
                )

    counts = {
        status: sum(comparison["status"] == status for comparison in comparisons)
        for status in ("pass", "error", "manual")
    }
    return {
        "source": document.name,
        "pdf_pages": document.page_count,
        "formal_table_pages": sorted(formal_table_pages),
        "digit_containing_result_cells": digit_cells,
        "excluded_numeric_tokens": excluded_numeric_tokens,
        "numeric_measurements": len(comparisons),
        "counts": counts,
        "comparisons": comparisons,
    }


def scan_report_numeric_file(path: str | Path) -> dict[str, Any]:
    """Open ``path`` read-only and run :func:`scan_report_numeric_measurements`."""

    with fitz.open(Path(path)) as document:
        return scan_report_numeric_measurements(document)
