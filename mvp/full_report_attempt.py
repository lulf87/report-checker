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


def _report_page_tables(page: Any) -> list[Any]:
    from mvp.checker import report_page_tables

    return report_page_tables(page)


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


def _align_table_row(table: fitz.table.Table, row_index: int, row: list[Any]) -> list[Any]:
    header_cells = list(table.rows[0].cells) if table.rows else []
    if not header_cells or len(row) == len(header_cells):
        return list(row)
    row_cells = [cell for cell in table.rows[row_index].cells if cell is not None]
    if len(row_cells) != len(row):
        return list(row) + [""] * max(0, len(header_cells) - len(row))
    header_starts = [float(fitz.Rect(cell).x0) for cell in header_cells]
    aligned: list[Any] = [""] * len(header_cells)
    for value, cell in zip(row, row_cells):
        start = float(fitz.Rect(cell).x0)
        column = min(range(len(header_starts)), key=lambda index: abs(start - header_starts[index]))
        aligned[column] = value
    return aligned


def _table_cell(table: fitz.table.Table, row_index: int, column: int) -> Any:
    if not table.rows or not (0 <= column < len(table.rows[0].cells)):
        return None
    header = table.rows[0].cells[column]
    if header is None:
        return None
    target_x = float(fitz.Rect(header).x0)
    for cell in table.rows[row_index].cells:
        if cell is not None and abs(float(fitz.Rect(cell).x0) - target_x) <= 1.5:
            return cell
    return None


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



# ---------------------------------------------------------------------------
# 2026-10-20 扩充：偏差写法、SI 词头换算和更多中文限值措辞。
# 只在语义唯一时自动判定；有歧义的写法仍转人工。
# ---------------------------------------------------------------------------

_SI_PREFIX = {
    "n": Decimal("1e-9"), "μ": Decimal("1e-6"), "µ": Decimal("1e-6"), "u": Decimal("1e-6"),
    "m": Decimal("1e-3"), "": Decimal(1), "k": Decimal(1000), "K": Decimal(1000),
    "M": Decimal("1e6"), "G": Decimal("1e9"),
}
_SI_BASES = ("Hz", "Ω", "V", "A", "s", "J", "W")
# 结果后面带一个括号符号（如“0.05 (−)”）时，只在上限判定中按绝对值比较。
_ANNOTATION_SUFFIXES = {"(-)", "(+)", "(&)", "(－)", "(＋)", "(−)"}


def _unit_factor(unit: str | None) -> tuple[str, Decimal] | None:
    if unit is None:
        return None
    value = unit.replace("sec", "s").replace("（", "(").replace("）", ")")
    value = re.sub(r"\(.*\)$", "", value)
    for base in sorted(_SI_BASES, key=len, reverse=True):
        if value.endswith(base):
            prefix = value[: -len(base)]
            if prefix in _SI_PREFIX:
                return base, _SI_PREFIX[prefix]
    return None


def _convert(value: Decimal, unit: str | None, target: str | None) -> Decimal | None:
    """Convert ``value`` from ``unit`` to ``target`` within one SI base unit."""

    if not unit or not target or unit == target:
        return value
    source, goal = _unit_factor(unit), _unit_factor(target)
    if source is None or goal is None or source[0] != goal[0]:
        return None
    return value * source[1] / goal[1]


def _result_values(parsed: dict[str, Any]) -> list[Decimal]:
    return [parsed["low"], parsed["high"]] if parsed["kind"] == "range" else [parsed["value"]]


def _interval_decision(parsed: dict[str, Any], low: Decimal, high: Decimal, reason: str, criterion: dict[str, Any]) -> dict[str, Any] | None:
    if parsed["kind"] == "scalar" and parsed.get("operator") != "=":
        return None
    values = _result_values(parsed)
    passed = all(low <= value <= high for value in values)
    return {
        "status": "pass" if passed else "error",
        "reason": f"{reason}_within_tolerance" if passed else f"{reason}_exceeds_tolerance",
        "criterion": _public_criterion(criterion),
    }


def _has_bound_words(text: str) -> bool:
    if any(word in text for word in (*UPPER_WORDS, *LOWER_WORDS, "低于", "小于", "大于", "高于")):
        return True
    # “以下要求”不是限值；“40dB 以下”才是。
    return bool(re.search(r"\d+(?:\.\d+)?[A-Za-zµμΩ%°()（）]*以(?:下|上)", text))


_TOLERANCE_WORDS = ("允差", "允许误差", "误差", "精度", "偏差")


def _deviation_decision(text: str, result_text: str, parsed: dict[str, Any]) -> dict[str, Any] | None:
    """Results written as deviations from a nominal value with a ± tolerance."""

    if _has_bound_words(text):
        return None
    if parsed.get("unit") == "%":
        percent_tolerances = {Decimal(value) for value in re.findall(r"±(\d+(?:\.\d+)?)%", text)}
        asymmetric = re.search(r"\+(\d+(?:\.\d+)?)%/[-－](\d+(?:\.\d+)?)%|[-－](\d+(?:\.\d+)?)%/\+(\d+(?:\.\d+)?)%", text)
        if asymmetric and not percent_tolerances:
            if asymmetric.group(1) is not None:
                high, low = Decimal(asymmetric.group(1)), -Decimal(asymmetric.group(2))
            else:
                low, high = -Decimal(asymmetric.group(3)), Decimal(asymmetric.group(4))
            criterion = {"kind": "interval", "low": low, "high": high, "unit": "%", "source": asymmetric.group(0)}
            return _interval_decision(parsed, low, high, "percent_deviation", criterion)
        if not percent_tolerances:
            return None
        if len(percent_tolerances) > 1 and not re.search(r"取(?:最)?大值?", text):
            return None
        tolerance = max(percent_tolerances)
        criterion = {"kind": "interval", "low": -tolerance, "high": tolerance, "unit": "%", "source": f"±{tolerance}%"}
        return _interval_decision(parsed, -tolerance, tolerance, "percent_deviation", criterion)

    if parsed.get("unit") is not None:
        return None
    signed = re.fullmatch(r"[+-]\d+(?:\.\d+)?(?:～[+-]\d+(?:\.\d+)?)?", _normalized_math(result_text))
    if not signed:
        return None
    tolerances = list(re.finditer(r"±(?P<t>\d+(?:\.\d+)?)(?![\d.])(?P<tu>[A-Za-zµμΩ]*)(?![%\d])", text))
    if len({(match.group("t"), match.group("tu")) for match in tolerances}) != 1:
        return None
    match = tolerances[0]
    nominal = re.search(
        r"(?<![\d.])(?P<c>\d+(?:\.\d+)?)(?P<cu>[A-Za-zµμΩ]*)[:：]?$",
        text[: match.start()],
    )
    tolerance_unit = match.group("tu") or (nominal.group("cu") if nominal else "") or None
    target = _declared_unit(text) or tolerance_unit
    tolerance = _convert(_parse_number(match.group("t")), tolerance_unit, target)
    if tolerance is None:
        return None
    values = _result_values(parsed)
    if nominal is not None:
        center_unit = nominal.group("cu") or match.group("tu") or None
        center = _convert(_parse_number(nominal.group("c")), center_unit, target)
        if center is None:
            return None
        # 带正负号的结果只有在“按绝对值理解不可能合格”时才按偏差处理，避免两种理解都说得通。
        if any(abs(value) >= center - tolerance for value in values):
            return None
    elif not any(word in text for word in _TOLERANCE_WORDS):
        return None
    criterion = {"kind": "interval", "low": -tolerance, "high": tolerance, "unit": target, "source": match.group(0)}
    return _interval_decision(parsed, -tolerance, tolerance, "signed_deviation", criterion)


def _extra_criterion(text: str) -> dict[str, Any] | None:
    units = rf"(?:{_NUMERIC_UNITS}|mJ|J|A|W|dB)"
    patterns = (
        (rf"(?:不应|应不|不得|不)低于[^0-9。；;]*?(\d+(?:\.\d+)?)({units})?", "lower", True),
        (rf"不应[^。；;]*?降到?(\d+(?:\.\d+)?)({units})?[^。；;]*?以下", "lower", True),
        (rf"(?<!不)(?<!不应)(?<!应不)小于(\d+(?:\.\d+)?)({units})?", "upper", False),
        (rf"(?<!不)(?<!不应)(?<!应不)低于(\d+(?:\.\d+)?)({units})?", "upper", False),
    )
    for pattern, kind, inclusive in patterns:
        if match := re.search(pattern, text):
            return {
                "kind": kind,
                "value": _parse_number(match.group(1)),
                "inclusive": inclusive,
                "unit": match.group(2) or _declared_unit(text),
                "source": match.group(0),
            }
    if match := re.search(r"应?在(\d+(?:\.\d+)?)%(?:以)?内", text):
        bound = _parse_number(match.group(1))
        return {"kind": "interval", "low": -bound, "high": bound, "unit": "%", "source": match.group(0)}
    if match := re.search(
        r"(?:包含在|在)(\d+(?:\.\d+)?)([A-Za-zµμΩ]+)～(\d+(?:\.\d+)?)([A-Za-zµμΩ]+)的?(?:频段|范围)",
        text,
    ):
        target = _declared_unit(text) or match.group(4)
        low = _convert(_parse_number(match.group(1)), match.group(2), target)
        high = _convert(_parse_number(match.group(3)), match.group(4), target)
        if low is not None and high is not None:
            return {"kind": "interval", "low": low, "high": high, "unit": target, "source": match.group(0)}
    return None


def _criterion_in_result_unit(criterion: dict[str, Any], text: str, parsed: dict[str, Any]) -> dict[str, Any] | None:
    """Return the criterion expressed in the result unit, or ``None`` if units cannot be reconciled."""

    result_unit = "%" if parsed.get("unit") == "%" else _declared_unit(text)
    criterion_unit = criterion.get("unit")
    if result_unit == "%" or criterion_unit == "%":
        return criterion if result_unit == criterion_unit else None
    if not criterion_unit or not result_unit or criterion_unit == result_unit:
        return criterion
    if criterion_unit.startswith("dB") and result_unit.startswith("dB"):
        return criterion
    converted = dict(criterion)
    for key in ("value", "low", "high"):
        if key in converted:
            value = _convert(converted[key], criterion_unit, result_unit)
            if value is None:
                return None
            converted[key] = value
    converted["unit"] = result_unit
    converted["converted_from"] = criterion_unit
    return converted


def evaluate_numeric_measurement(requirement: str, result_text: str) -> dict[str, Any]:
    """Evaluate one Report result against its acceptance text.

    The return value always contains ``status``, ``reason``, and ``criterion``.
    ``status`` is one of ``pass``, ``error``, or ``manual``.
    """

    if numeric_result_kind(result_text) is None:
        return {"status": "manual", "reason": "unsupported_numeric_result_syntax", "criterion": None}
    parsed = _parse_result(result_text)

    text = _normalized_math(requirement)
    if parsed.get("suffix"):
        criterion = _criterion_from_requirement(requirement)
        if (
            _compact(parsed["suffix"]) in _ANNOTATION_SUFFIXES
            and criterion is not None
            and criterion["kind"] == "upper"
            and parsed["operator"] == "="
            and (criterion := _criterion_in_result_unit(criterion, text, parsed)) is not None
        ):
            value = abs(parsed["value"])
            bound = criterion["value"]
            passed = value <= bound if criterion["inclusive"] else value < bound
            return {
                "status": "pass" if passed else "error",
                "reason": "annotated_value_within_bound" if passed else "annotated_value_exceeds_bound",
                "criterion": _public_criterion(criterion),
            }
        return {"status": "manual", "reason": "numeric_result_has_unconfirmed_suffix", "criterion": None}

    deviation = _deviation_decision(text, result_text, parsed)
    if deviation is not None:
        return deviation

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

    criterion = _criterion_from_requirement(requirement)
    if criterion is None:
        criterion = _extra_criterion(text)
    if _unit_scale_conflict(requirement) or (criterion is not None and criterion.get("unit") != _declared_unit(text)):
        # 限值和结果单位不同（如 MΩ 与 GΩ、Hz 与 kHz）时按 SI 词头换算；无法换算才转人工。
        converted = _criterion_in_result_unit(criterion, text, parsed) if criterion is not None else None
        if converted is None:
            if _unit_scale_conflict(requirement):
                return {
                    "status": "manual",
                    "reason": "unit_prefix_conversion_required_but_disabled",
                    "criterion": None,
                }
        else:
            criterion = converted
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
        for table in _report_page_tables(page):
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

            for row_index, raw_row in enumerate(rows[1:], start=1):
                row = _align_table_row(table, row_index, raw_row)
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
                cell = _table_cell(table, row_index, result_index)
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
