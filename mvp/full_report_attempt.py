"""Reusable, deterministic probes for a fuller Report self-check attempt.

Only the R07-B numeric scan is implemented here.  The scanner reads native
PDF tables, preserves evidence coordinates, and returns ``manual`` whenever a
decision would require an unconfirmed result convention, a unit conversion,
or interpretation of an unsupported suffix.
"""

from __future__ import annotations

import hashlib
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
# “最大输出电压4800V”这类接受标准没有显式比较符，但“最大”本身
# 已经给出了上限语义。把 A/J/W 也放进来，覆盖同一组常见的输出量。
_MAXIMUM_OUTPUT_UNITS = rf"(?:{_NUMERIC_UNITS}|mJ|J|A|W)"


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


def _additional_numeric_result_kind(text: str) -> str | None:
    """Recover two previously excluded formats without expanding auto-pass.

    Existing evaluator behavior is unchanged. Each newly covered cell gets
    its own manual result with its full raw text and source coordinates.
    """
    import unicodedata

    raw = str(text).translate(str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺", "0123456789-+"))
    value = _normalized_math(unicodedata.normalize("NFKC", raw))
    number = r"[+-]?(?:\d+(?:\.\d+)?|\.\d+)"
    if re.fullmatch(rf"{number}(?:[eE][+-]?\d+|[×·]10\^?[+-]?\d+)%?", value):
        return "scientific"
    if "％" in str(text) and re.fullmatch(rf"{number}%?～{number}%?", value):
        return "range_fullwidth_percent"
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
    match = re.search(r"单位[:：]([A-Za-zµμΩ%()∙·⋅]+)", _normalized_math(requirement), re.IGNORECASE)
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

    # 接受标准常把“最大输出值”直接写成“最大输出电压4800V”，不再重复写
    # “≤”。在没有“为/应为/额定/标称/设定值”等精确目标语义时，按“实测值
    # 不超过该最大值”处理；显式比较符仍由上面的规则优先解析。
    maximum_output = re.search(
        rf"(?P<label>(?:单个脉冲)?(?:最大|最高)输出(?:电压|电流|能量|功率|幅度))"
        rf"(?P<separator>[:：])?(?P<n>\d+(?:\.\d+)?)(?P<u>{_MAXIMUM_OUTPUT_UNITS})?"
        rf"(?![A-Za-zµμΩ])",
        text,
    )
    if maximum_output:
        number = _parse_number(maximum_output.group("n"))
        return {
            "kind": "upper",
            "value": number,
            "inclusive": True,
            "unit": maximum_output.group("u") or _declared_unit(text),
            "source": maximum_output.group(0),
            "rule": "bare_maximum_as_upper_bound",
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


def _alternative_tolerance(text: str) -> bool:
    """A requirement row that is the second half of a tolerance.

    e.g. “负载阻抗显示误差不超过±5%” followed by its own row
    “或±5Ω（取较大值）。单位：Ω” with the Ω result “+2”.
    """

    return bool(re.match(r"或±\d", text) or re.search(r"取较?大值", text))


# “在压力值≤20g 时，误差不超过±3g”：“≤20g”是适用条件，不是接受标准。
_CONDITION_CLAUSE = re.compile(r"(?:在|当)[^，,。；;:：]{0,40}?时[，,]?")


def _strip_condition_clauses(requirement: str) -> str:
    stripped = _CONDITION_CLAUSE.sub("", requirement)
    return stripped if re.search(r"\d", stripped) else requirement


def _deviation_decision(text: str, result_text: str, parsed: dict[str, Any]) -> dict[str, Any] | None:
    """Results written as deviations from a nominal value with a ± tolerance."""

    if _has_bound_words(text):
        return None
    if parsed.get("unit") == "%":
        percent_tolerances = {Decimal(value) for value in re.findall(r"±(\d+(?:\.\d+)?)%", text)}
        # “+5%/-10%”，也有写成“-10%，+5%”的。
        asymmetric = re.search(r"\+(\d+(?:\.\d+)?)%[/，,、][-－](\d+(?:\.\d+)?)%|[-－](\d+(?:\.\d+)?)%[/，,、]\+(\d+(?:\.\d+)?)%", text)
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
    tolerances = list(re.finditer(r"±(?P<t>\d+(?:\.\d+)?)(?![\d.])(?P<tu>[A-Za-zµμΩ∙·⋅]*)(?![%\d])", text))
    if len({(match.group("t"), match.group("tu")) for match in tolerances}) != 1:
        return None
    match = tolerances[0]
    nominal = re.search(
        r"(?<![\d.])(?P<c>\d+(?:\.\d+)?)(?P<cu>[A-Za-zµμΩ∙·⋅]*)[:：]?$",
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
        # 带正负号的结果可以理解为偏差（±tolerance 以内），也可以理解为测得值本身
        # （center±tolerance 以内）。两种理解结论相同才自动判定（如 0.2mV±0.2mV、结果 +0.0：
        # 两种都合格）；结论不同才交人工。
        if any(abs(value) >= center - tolerance for value in values):
            as_deviation = all(-tolerance <= value <= tolerance for value in values)
            as_measured = all(center - tolerance <= value <= center + tolerance for value in values)
            if as_deviation != as_measured:
                return None
    elif not any(word in text for word in _TOLERANCE_WORDS) and not _alternative_tolerance(text):
        return None
    criterion = {"kind": "interval", "low": -tolerance, "high": tolerance, "unit": target, "source": match.group(0)}
    return _interval_decision(parsed, -tolerance, tolerance, "signed_deviation", criterion)


def _extra_criterion(text: str) -> dict[str, Any] | None:
    units = rf"(?:{_NUMERIC_UNITS}|mJ|J|A|W|dB)"
    # A limit computed in the requirement itself, e.g. "应限制……漏电流小于I漏；
    # I漏[mA]=2.0×10−5×d×L×f×U试=135.7[mA]" or "相应限值为：I漏[mA]=…=201.6[mA]".
    formula_values = re.findall(rf"=(\d+(?:\.\d+)?)\[?({_SIMPLE_UNITS})\]?", text)
    if formula_values and (re.search(r"小于I", text) or re.search(r"限值为", text)):
        number, unit = formula_values[-1]
        strict = bool(re.search(r"小于I", text)) and not re.search(r"限值为", text)
        return {
            "kind": "upper",
            "value": _parse_number(number),
            "inclusive": not strict,
            "unit": unit,
            "source": f"={number}{unit}",
        }
    # "……的限值改变为100mA" / "限值改为…"
    if match := re.search(rf"限值(?:改变为|改为|变为|调整为)(\d+(?:\.\d+)?)({units})?", text):
        return {
            "kind": "upper",
            "value": _parse_number(match.group(1)),
            "inclusive": True,
            "unit": match.group(2) or _declared_unit(text),
            "source": match.group(0),
        }
    # "不应产生150mA以上高频电流" / "不得产生50mA以上…"
    if match := re.search(rf"(?:不应|不得|不能|不可|应不)[^。；;0-9]{{0,12}}?(\d+(?:\.\d+)?)({units})?以上", text):
        return {
            "kind": "upper",
            "value": _parse_number(match.group(1)),
            "inclusive": True,
            "unit": match.group(2) or _declared_unit(text),
            "source": match.group(0),
        }
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
    # “扭矩为0.08～0.12N∙m 单位：N∙m”：要求本身给出的取值范围，单位须与声明的单位一致。
    declared = _declared_unit(text)
    if declared and (match := re.search(
        r"(?:应为|为|应在)(\d+(?:\.\d+)?)～(\d+(?:\.\d+)?)([A-Za-zµμΩ%∙·⋅]+)?(?:之间|范围内|以内)?",
        text,
    )):
        unit = match.group(3)
        low, high = _parse_number(match.group(1)), _parse_number(match.group(2))
        if (unit is None or unit == declared) and low < high:
            return {"kind": "interval", "low": low, "high": high, "unit": declared, "source": match.group(0)}
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
    requirement = _strip_condition_clauses(requirement)

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


# A deliberately bounded table grammar.  A bare pair of numbers is not a
# criterion: the native table must also own a model column, a Grip-angle
# heading and an explicit degree declaration on the same page.
_GRIP_INTRO = re.compile(r"Grip开口动作最大角度应(?:该)?符合下表(?:中)?的(?:要求|规定)[。:：]?")
_GRIP_HEADERS = {"Grip最大开口角度", "Grip开口动作角度"}
_ANGLE_RANGE = re.compile(r"(?P<low>\d+(?:\.\d+)?)°[~～](?P<high>\d+(?:\.\d+)?)°")
_MODEL_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


def _same_page_angular_bindings(table: Any, rows: list[list[Any]], mapping: dict[str, int]) -> dict[int, dict[str, Any]]:
    """Retain physical row ownership; never inherit a heading across pages."""
    start, stop = mapping["标准要求"], mapping["检验结果"]
    pending: dict[str, Any] | None = None
    active: dict[str, Any] | None = None
    blocks: list[dict[str, Any]] = []
    for row_index, raw in enumerate(rows[1:], 1):
        row = _align_table_row(table, row_index, raw)
        atoms = [(i, _compact(row[i])) for i in range(start, stop) if i < len(row) and _compact(row[i])]
        values = [text for _, text in atoms]
        if len(values) == 1 and values[0].endswith("单位：°") and _GRIP_INTRO.fullmatch(values[0][:-4]):
            pending = {"intro_row": row_index, "intro_column": atoms[0][0], "intro_text": values[0]}
            active = None
            continue
        if len(values) == 2 and values[0] == "型号" and values[1] in _GRIP_HEADERS:
            active = None
            if pending and pending["intro_row"] == row_index - 1:
                active = {**pending, "header_row": row_index, "model_column": atoms[0][0],
                          "limit_column": atoms[1][0], "header_text": values[1], "rows": [], "models": []}
                blocks.append(active)
            pending = None
            continue
        pending = None
        if active is None:
            continue
        # A different project/clause/sequence or any other table paragraph
        # ends this subtable.  Do not let a previous heading govern it.
        if any(_compact(row[mapping[name]]) for name in ("序号", "检验项目", "标准条款")):
            active = None
            continue
        if len(atoms) != 2 or [i for i, _ in atoms] != [active["model_column"], active["limit_column"]] or not _MODEL_TOKEN.fullmatch(values[0]):
            active = None
            continue
        active["models"].append(values[0])
        interval = _ANGLE_RANGE.fullmatch(values[1])
        if interval and Decimal(interval["low"]) <= Decimal(interval["high"]):
            active["rows"].append({"row_index": row_index, "model": values[0], "limit_text": values[1],
                                   "low": interval["low"], "high": interval["high"]})
        elif values[1] not in {"/", "——", "—"}:
            # A different constraint syntax is not promoted by this rule.
            active = None
    bindings = {}
    for block in blocks:
        if len(block["models"]) != len(set(block["models"])):
            continue
        for row in block["rows"]:
            bindings[row["row_index"]] = {**{k: v for k, v in block.items() if k not in {"rows", "models"}}, **row}
    return bindings


def _source_table_snapshot(table_index: int, table: Any, rows: list[list[Any]],
                           mapping: dict[str, int]) -> dict[str, Any]:
    # Materialized data only: do not retain PyMuPDF Table/Page objects after
    # moving to another page. The snapshot lives within one read-only scan.
    return {"table_index":table_index, "table_bbox":list(table.bbox), "mapping":dict(mapping),
            "rows":[list(rows[0]), *[_align_table_row(table,i,row) for i,row in enumerate(rows[1:],1)]],
            "cells":[[list(cell) if cell else None for cell in row.cells] for row in table.rows]}


def _angular_native_sources(document: fitz.Document, *,
                             table_snapshots: dict[int, list[dict[str, Any]]] | None = None) -> list[dict[str, Any]]:
    # Keep the complete native-text ledger.  A reference-only footnote may
    # inherit its parameter and unit from a table without repeating either.
    sources = []
    snapshots = table_snapshots if table_snapshots is not None else {}
    active_sequence, active_clause, parameter_clause = None, "", ""
    for page in document:
        owners = []
        if page.number not in snapshots:
            page_snapshots = []
            for table_index, table in enumerate(_report_page_tables(page)):
                if _is_formal_report_table(table):
                    rows = table.extract()
                    page_snapshots.append(_source_table_snapshot(table_index,table,rows,_header_map(rows[0])))
            snapshots[page.number] = page_snapshots
        for snapshot in snapshots[page.number]:
            table_index, mapping = snapshot["table_index"], snapshot["mapping"]
            for row_index, row in enumerate(snapshot["rows"][1:], 1):
                sequence = re.fullmatch(r"(?:续)?(\d+)", _compact(row[mapping["序号"]]))
                if sequence and int(sequence[1]) != active_sequence:
                    active_sequence, active_clause = int(sequence[1]), ""
                    parameter_clause = ""
                if _compact(row[mapping["标准条款"]]):
                    active_clause = _compact(row[mapping["标准条款"]])
                for value in row[mapping["标准要求"]:mapping["检验结果"]]:
                    headings = re.findall(r"(?:^|\n)\s*(\d+(?:\.\d+){2,5})(?![\d.])", str(value or ""))
                    if headings:
                        parameter_clause = headings[-1]
                cells = snapshot["cells"][row_index]
                content_cells = [cells[c] for c in range(mapping["标准要求"], mapping["检验结果"] + 1)
                                 if c < len(cells) and cells[c]]
                if content_cells:
                    y0 = min(c[1] for c in content_cells)
                    y1 = max(c[3] for c in content_cells)
                    owners.append({"table_index": table_index, "row_index": row_index,
                        "clause": active_clause, "sequence": active_sequence, "parameter_clause": parameter_clause,
                        "row_bbox": [snapshot["table_bbox"][0], y0, snapshot["table_bbox"][2], y1],
                        "table_bbox": snapshot["table_bbox"]})
        for index, block in enumerate(page.get_text("blocks")):
            if len(block) < 5 or not isinstance(block[4], str):
                continue
            if block[4].strip():
                rect = fitz.Rect(block[:4])
                owning = [o for o in owners if (rect & fitz.Rect(o["row_bbox"])).get_area() > 0.01]
                complete = bool(owning) and all(o["clause"] for o in owning) and any(
                    (rect & fitz.Rect(o["table_bbox"])).get_area() >= 0.98 * rect.get_area() for o in owning)
                sources.append({"id": f"angle-source:p{page.number + 1}:b{index}", "pdf_page": page.number + 1,
                                "bbox": list(block[:4]), "text": block[4],
                                "owner_clauses": sorted({o["clause"] for o in owning if o["clause"]}),
                                "owner_parameter_clauses": sorted({o["parameter_clause"] for o in owning if o["parameter_clause"]}),
                                "owner_rows": [{k:o[k] for k in ("table_index", "row_index", "clause", "sequence")} for o in owning],
                                "table_ownership_complete": complete})
    return sources


_CLAUSE_REFERENCE = re.compile(r"(?<![\d.])\d+(?:\.\d+){2,5}(?![\d.])")
_TABLE_PAGE_REFERENCE = re.compile(r"(?:上|下|前|后|原|本|该)(?:页|表)|上述|前述|后续|表\d+")
_LIMIT_REVISION = re.compile(r"上限|下限|限值|最大允许值|最小允许值|改为|改成|调整|修订|修改|变更|例外|另有规定")
_AMENDMENT = re.compile(r"改为|改成|调整|修订|修改|变更|例外|另有规定")
_BOUND_LANGUAGE = re.compile(r"[<>≤≥]|不大于|不小于|不得超过|不超过|大于|小于|至少|至多")


def _angular_applicable_clauses(clause: str, target: dict[str, Any]) -> set[str]:
    # A test may depend on several clauses (e.g. reprocessing repeats
    # 2.1.5--2.1.9).  A native subsection heading identifies which of those
    # owns this parameter; sibling clauses are not all angle constraints.
    parameter_clause = target.get("parameter_clause")
    referred = [parameter_clause] if parameter_clause else target.get("referenced_clauses", [])
    return {_compact(clause), *referred} - {""}


def _requirement_clause_references(text: str) -> list[str]:
    """References declared by the current test, not guessed from nearby pages.

    This only creates possible dependencies for condition review.  It does
    not import a referenced clause's threshold or assert it was fulfilled.
    """
    compact = _compact(text)
    if not re.search(r"符合|满足|参照|参见|依据|按照|按第", compact):
        return []
    clauses = set(_CLAUSE_REFERENCE.findall(compact))
    for match in re.finditer(r"(\d+(?:\.\d+){2,5})(?:至|到|[~～—-])(\d+(?:\.\d+){2,5})", compact):
        low, high = match[1].split("."), match[2].split(".")
        if low[:-1] == high[:-1] and 0 <= int(high[-1]) - int(low[-1]) <= 30:
            clauses.update(".".join([*low[:-1], str(n)]) for n in range(int(low[-1]), int(high[-1]) + 1))
    return sorted(clauses)


def _angular_reference_condition(source: dict[str, Any], sentence: str, model: str,
                                 clause: str, page_number: int, target: dict[str, Any],
                                 *, grip_parameter: bool = True) -> dict[str, Any] | None:
    """Bind an inherited-unit condition to a target, or retain ambiguity.

    A match is never used to rewrite the local limit automatically.  The
    record identifies why the source may govern this table/row, so an
    unresolved reference cannot silently become a successful comparison.
    """
    reference = bool(_TABLE_PAGE_REFERENCE.search(sentence))
    annotation = bool(source.get("annotation_context")) or bool(re.search(
        r"(?:^|\n)(?:注\d*[:：]?|备注[:：]?|注意[:：]?|[*※])", sentence))
    revision = bool(_LIMIT_REVISION.search(sentence))
    bound = bool(_BOUND_LANGUAGE.search(sentence) and re.search(r"\d", sentence))
    range_statement = bool((annotation or reference) and re.search(
        r"(?:范围|区间)(?:为|改为|调整为)", sentence))
    models = set(re.findall(r"(?<!\d)\d{6}(?!\d)", sentence))
    applicable_clauses = _angular_applicable_clauses(clause, target)
    matched_clauses = {c for c in applicable_clauses if re.search(
        rf"(?<![\d.]){re.escape(c)}(?![\d.])", sentence)}
    if not (revision or bound or range_statement) or not (reference or annotation or models or matched_clauses
                                      or "型号" in sentence or _CLAUSE_REFERENCE.search(sentence)):
        return None
    # A complete statement explicitly about another axis is not a Grip
    # amendment.  Mixed/reference statements remain ambiguous instead.
    if (grip_parameter and re.search(r"Roll|Pitch|Yaw", sentence, re.IGNORECASE)
            and not re.search(r"Grip|开口|但|另外|同时", sentence, re.IGNORECASE)):
        return None
    if _native_angular_row_models(source["text"]):
        # This is a completely parsed data block (possibly followed by one
        # separately typed length statement), not an inherited-unit note.
        return None
    scope = _angular_model_scope(sentence, model)  # No whole-block row shortcut.
    if scope["applies"] is False:
        return None
    mentioned_clauses = set(_requirement_clause_references("参见" + sentence)) | matched_clauses
    unresolved_clause_range = False
    for match in re.finditer(r"(\d+(?:\.\d+){1,5})(?:至|到|[~～—-])(\d+(?:\.\d+){1,5})", sentence):
        low, high = tuple(map(int, match[1].split('.'))), tuple(map(int, match[2].split('.')))
        if low[:-1] != high[:-1] or low > high:
            unresolved_clause_range = True
        else:
            for candidate_clause in applicable_clauses:
                value = tuple(map(int, candidate_clause.split('.'))) if re.fullmatch(r"\d+(?:\.\d+)+", candidate_clause) else ()
                if value[:-1] == low[:-1] and low <= value <= high:
                    mentioned_clauses.add(candidate_clause)
    owner_clauses = set(source.get("owner_clauses", []))
    explicit_target = model in models and (annotation or revision or scope["applies"] is True)
    cross_owner_reference = bool(re.search(r"(?:上|下|前|后|原)(?:页|表)|(?:上述|前述)(?:表格|表)", sentence))
    same_physical_table = source["pdf_page"] == page_number and any(
        o["table_index"] == target.get("table_index") for o in source.get("owner_rows", []))
    if (source.get("table_ownership_complete") and owner_clauses.isdisjoint(applicable_clauses)
            and not (mentioned_clauses & applicable_clauses) and not explicit_target
            and not cross_owner_reference and not (reference and same_physical_table)
            and not unresolved_clause_range and not _AMENDMENT.search(sentence)
            and not re.search(r"Grip|开口", sentence, re.IGNORECASE)):
        # A normal criterion/annotation physically owned by a different
        # clause is not a condition on this table.  Explicit amendments or
        # links back to the target must still be reviewed across clauses.
        return None
    links = []
    if unresolved_clause_range:
        links.append("clause_range_scope_unresolved")
    if model in models:
        links.append("model")
    if scope.get("relation") == "all_except_listed_models" and scope.get("applies"):
        links.append("model_complement")
    if mentioned_clauses & applicable_clauses:
        links.append("clause_or_declared_dependency")
    if reference:
        links.append("table_or_page_reference")
        if ("上页" in sentence or "前页" in sentence) and source["pdf_page"] == page_number + 1:
            links.append("referenced_previous_page")
        if ("下页" in sentence or "后页" in sentence) and source["pdf_page"] == page_number - 1:
            links.append("referenced_next_page")
    if annotation and source["pdf_page"] == page_number:
        links.append("same_page_annotation_scope_unresolved")
    if not links:
        # An explicit different clause with no target/model/table link does
        # not own this row.  An unowned amendment/footnote is retained: page
        # distance by itself is not proof that its scope is unrelated.
        if mentioned_clauses and not target.get("clause_reference_uncertain"):
            return None
        if annotation or (models and scope["applies"] is None):
            links.append("unresolved_annotation_or_model_scope")
        else:
            return None
    return {"status": "requires_review", "links": links,
            "annotation_inherited_from_block": bool(source.get("annotation_context")),
            "source_id": source["id"], "mentioned_clauses": sorted(mentioned_clauses),
            "source_owner_clauses": sorted(owner_clauses),
            "model_scope": scope,
            "target": {"pdf_page": page_number, "table_index": target.get("table_index"),
                       "intro_row": target.get("intro_row"), "row_index": target.get("row_index"),
                       "model": model, "clause": clause,
                       "parameter_clause": target.get("parameter_clause"),
                       "referenced_clauses": target.get("referenced_clauses", [])}}


def _native_angular_row_models(text: str) -> list[str] | None:
    """Recognize complete native model/limit/result rows before a new field.

    A PDF text block can contain several angle rows followed by a separate
    head-length requirement.  Preserve native whitespace: compacting it
    joins a measurement to the next model and loses the row boundaries.
    """
    number = r"\d+(?:\.\d+)?"
    limit = rf"[<>≤≥]?{number}\s*°(?:\s*[~～]\s*{number}\s*°)?"
    row = re.compile(rf"\s*(?P<model>\d{{6}})\s+{limit}\s+[+-]?{number}\s+(?=\S|$)")
    models, offset = [], 0
    while match := row.match(text, offset):
        models.append(match["model"])
        offset = match.end()
    tail = _compact(text[offset:])
    if not models or len(models) != len(set(models)):
        return None
    length_only = (rf"器械的头端长度(?:应符合(?:该|本|下)表的规定[,，])?误差为±?{number}mm"
                   rf"(?:[（(]除\d{{6}}[）)])?(?:[,，]\d{{6}}的误差为{number}%)?[。.]?/?")
    if tail and not re.fullmatch(length_only, tail):
        return None
    return models


def _angular_model_scope(sentence: str, model: str, native_text: str = "") -> dict[str, Any]:
    """Mentioned models are not necessarily the models a condition governs.

    Exclude a condition only after resolving its positive or complement
    scope.  Unknown/collective wording remains evidence requiring review.
    """
    token = r"\d{6}"
    model_list = rf"{token}(?:[/、,，和及]{token})*"
    prefix = r"(?:(?:注\d*|备注|注意)[:：])?"
    complement = re.fullmatch(
        rf"{prefix}除(?:型号)?(?P<models>{model_list})(?:以|之)?外[,，]?"
        r"(?:其余|其他|其它)(?:所有)?型号(?:的)?(?P<requirement>.+)", sentence)
    if complement:
        if re.search(r"\d{6}|但|然而|其他|其余|所有|型号", complement["requirement"]):
            return {"relation": "multiple_model_scopes_unresolved", "applies": None}
        excluded = re.findall(token, complement["models"])
        return {"relation": "all_except_listed_models", "excluded_models": excluded,
                "applies": model not in excluded}
    native_models = _native_angular_row_models(native_text) if native_text else None
    if native_models:
        return {"relation": "native_model_limit_result_rows", "included_models": native_models,
                "applies": model in native_models, "separate_parameter": "head_length"}
    # Negation, a complement or a collective subject must never be reduced
    # to the incidental numeric IDs appearing somewhere in the sentence.
    if re.search(r"除|其余|其他|其它|以外|之外|不含|不包括|排除|例外|所有|全部|均|参照|参见", sentence):
        return {"relation": "collective_or_negative_scope_unresolved", "applies": None}
    positive = re.fullmatch(
        rf"{prefix}(?:仅(?:适用于)?)?型号(?P<models>{model_list})(?:的)?"
        r"(?P<requirement>(?:Grip|开口|角度|上限|下限|限值|最大允许值|最小允许值).+)", sentence, re.IGNORECASE)
    if positive:
        if re.search(r"\d{6}|但|然而|型号", positive["requirement"]):
            return {"relation": "multiple_model_scopes_unresolved", "applies": None}
        included = re.findall(token, positive["models"])
        return {"relation": "listed_models_only", "included_models": included,
                "applies": model in included}
    return {"relation": "model_scope_unresolved", "applies": None}


def _angular_condition_conflicts(sources: list[dict[str, Any]], model: str, clause: str, page_number: int,
                                 target: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    unresolved = []
    target = target or {}
    for source in sources:
        # Native line boundaries distinguish a header from a requirement
        # beginning with the same words.  Do not delete a substring prefix.
        lines = [line for line in source["text"].splitlines() if _compact(line) not in _GRIP_HEADERS]
        # Retain a sentence boundary when removing a recognized table intro.
        text = _GRIP_INTRO.sub("。", _compact("\n".join(lines)))
        annotation_context = bool(re.match(r"(?:注\d*[:：]?|备注[:：]?|注意[:：]?|[*※])", text))
        for sentence in re.split(r"[。；;]", text):
            inherited = _angular_reference_condition({**source, "annotation_context": annotation_context},
                                                      sentence, model, clause, page_number, target)
            if inherited:
                unresolved.append({**source, "unresolved_text": sentence,
                                   "model_scope": inherited["model_scope"], "reference_binding": inherited})
                continue
            grip = bool(re.search(r"Grip", sentence, re.IGNORECASE)) or (
                bool(re.search(r"开口.*角度", sentence)) and
                bool(re.search(r"[<>≤≥]|调整|例外|须|应|要求|限值|改为|\d+(?:\.\d+)?°", sentence)))
            generic = ("角度" in sentence and bool(re.search(r"[<>≤≥]|不小于|不大于|至少|应|须|调整|例外|限值", sentence))) or bool(
                re.search(r"(?:[<>≤≥]|不小于|不大于|至少|大于|小于)\d+(?:\.\d+)?°", sentence)) or bool(
                re.search(r"(?:限值|调整|例外|改为).*°", sentence))
            if not grip and not generic:
                continue
            # Explicitly named other axes do not change the Grip parameter.
            if not grip and re.search(r"Roll|Pitch|Yaw", sentence, re.IGNORECASE):
                continue
            models = set(re.findall(r"(?<!\d)\d{6}(?!\d)", sentence))
            model_scope = _angular_model_scope(sentence, model, source["text"])
            if model_scope["applies"] is False:
                continue
            same_clause = any(re.search(rf"(?<![\d.]){re.escape(c)}(?![\d.])", sentence)
                              for c in _angular_applicable_clauses(clause, target))
            reference = bool(_TABLE_PAGE_REFERENCE.search(sentence))
            if grip or models or same_clause or source["pdf_page"] == page_number or reference:
                unresolved.append({**source, "unresolved_text": sentence, "model_scope": model_scope})
    return unresolved


def _structured_angular_decision(page: fitz.Page, table: Any, table_index: int, mapping: dict[str, int],
                                 binding: dict[str, Any], result_text: str, clause: str,
                                 sources: list[dict[str, Any]], source_sha256: str,
                                 clause_references: list[dict[str, Any]] | None = None,
                                 reference_section: dict[str, Any] | None = None) -> dict[str, Any] | None:
    row_index = binding["row_index"]
    roles = [
        ("intro", binding["intro_row"], binding["intro_column"], binding["intro_text"]),
        ("model_header", binding["header_row"], binding["model_column"], "型号"),
        ("parameter_header", binding["header_row"], binding["limit_column"], binding["header_text"]),
        ("model", row_index, binding["model_column"], binding["model"]),
        ("limits", row_index, binding["limit_column"], binding["limit_text"]),
        ("result", row_index, mapping["检验结果"], _compact(result_text)),
    ]
    evidence = []
    for role, ri, column, expected in roles:
        # The formal "标准要求" header spans the nested columns.  Its
        # placeholders are None, so locate this subtable's actual row cell,
        # not a column inferred from that merged outer header.
        cells = table.rows[ri].cells
        cell = cells[column] if len(cells) == len(table.rows[0].cells) and column < len(cells) else None
        if not cell:
            return None
        native = page.get_textbox(fitz.Rect(cell))
        if _compact(native) != expected:
            return None
        evidence.append({"role": role, "document_role": "report", "pdf_page": page.number + 1,
                         "table_index": table_index, "row_index": ri, "bbox": list(cell),
                         "text": native, "source_sha256": source_sha256})
    # These three values must be disjoint cells of the same physical row.
    for i, left in enumerate(evidence[3:]):
        for right in evidence[3:][i + 1:]:
            if (fitz.Rect(left["bbox"]) & fitz.Rect(right["bbox"])).get_area() > 0.01:
                return None
    if not re.fullmatch(r"\d+(?:\.\d+)?", _compact(result_text)):
        return None
    low, high, value = Decimal(binding["low"]), Decimal(binding["high"]), Decimal(_compact(result_text))
    local_status = "pass" if low <= value <= high else "error"
    criterion = {"kind": "interval", "low": binding["low"], "high": binding["high"],
                 "low_inclusive": True, "high_inclusive": True, "unit": "°", "source": binding["limit_text"],
                 "rule": "same_page_model_Grip_angle_interval"}
    references = [dict(entry) for entry in (clause_references or [])]
    section = dict(reference_section) if reference_section else None
    target = {"table_index": table_index, "intro_row": binding["intro_row"], "row_index": row_index,
              "referenced_clauses": sorted({c for entry in references for c in entry["clauses"]}),
              "parameter_clause": section["clause"] if section and section["native_projection_matches"] else None,
              "clause_reference_uncertain": any(not entry["native_projection_matches"] for entry in references)}
    conflicts = _angular_condition_conflicts(sources, binding["model"], clause, page.number + 1, target)
    for entry in references:
        if not entry["native_projection_matches"]:
            conflicts.append({**entry, "unresolved_text": entry["projection"],
                              "reason": "clause_reference_native_projection_mismatch"})
    if section and not section["native_projection_matches"]:
        conflicts.append({**section, "unresolved_text": section["projection"],
                          "reason": "reference_section_native_projection_mismatch"})
    return {"status": "manual" if conflicts else local_status,
            "reason": "angular_condition_scope_unresolved" if conflicts else
                      "model_angle_in_interval" if local_status == "pass" else "model_angle_outside_interval",
            "criterion": criterion,
            "angular_binding": {"model": binding["model"], "parameter": "Grip开口动作角度", "unit": "°",
                                "evidence": evidence, "local_interval_status": local_status,
                                "unresolved_conditions": conflicts,
                                "target_context": {**target, "clause": clause, "clause_reference_evidence": references,
                                                   "parameter_clause_evidence": section},
                                "condition_source_coverage": {"native_text_blocks": len(sources),
                                                              "selection": "all_native_text_blocks", "OCR_used": False}}}


def _simple_parent_requirement(text: str) -> str | None:
    """Only one literal bound; no derived/reference/tolerance interpretation."""
    normalized = _normalized_math(text)
    headings = re.findall(r"(?:^|\n)\s*(\d+(?:\.\d+){2,5})(?![\d.])", text)
    if not headings or not _CLAUSE_REFERENCE.match(_compact(text)):
        return None
    if re.search(r"绝对值|标称|允差|偏差|实测值|模式|如果|除|仅|另|否则|型号|适用|对于|针对|±", normalized):
        return None
    if len(re.findall(r"[<>≤≥]\d+(?:\.\d+)?", normalized)) != 1:
        return None
    criterion = _criterion_from_requirement(text)
    return headings[-1] if criterion and criterion["kind"] in {"upper", "lower"} else None


def _centered_cell_text(page: fitz.Page, bbox: list[float] | tuple[float, ...]) -> str:
    # Superscript/subscript glyph boxes may overlap the following cell.
    # Attribute words by their centre, preserving the full cell text too.
    x0,y0,x1,y1 = bbox
    words = [w for w in page.get_text("words", clip=fitz.Rect(bbox) + (-2,-2,2,2))
             if x0 <= (w[0]+w[2])/2 < x1 and y0 <= (w[1]+w[3])/2 < y1]
    return " ".join(w[4] for w in words)


def _same_page_parent_bindings(page: fitz.Page, table: Any, rows: list[list[Any]],
                               mapping: dict[str, int]) -> dict[int, dict[str, Any]]:
    """A parent statement owns only contiguous model/dimension child cells.

    Inherited labels must physically span the measured row.  A new heading,
    non-label paragraph or table/page boundary ends the parent, and duplicate
    model/dimension tuples invalidate that whole block.
    """
    start, stop = mapping["标准要求"], mapping["检验结果"]
    active, blocks = None, []
    for ri, raw in enumerate(rows[1:], 1):
        row = _align_table_row(table, ri, raw)
        atoms = [(c, _compact(row[c])) for c in range(start, stop) if c < len(row) and _compact(row[c])]
        if any(_compact(row[mapping[n]]) for n in ("序号", "检验项目", "标准条款")):
            active = None
        if len(atoms) == 1 and atoms[0][0] == start and _CLAUSE_REFERENCE.match(atoms[0][1]):
            active = None
            cell = table.rows[ri].cells[start]
            text = page.get_textbox(fitz.Rect(cell)) if cell else ""
            clause = _simple_parent_requirement(text)
            if clause and not _compact(row[stop]):
                active = {"clause": clause, "parent_row": ri, "parent_bbox": list(cell),
                          "parent_text": text, "labels": {}, "children": []}
                blocks.append(active)
            continue
        if active is None:
            continue
        if not atoms:
            active = None
            continue
        valid = True
        for column, text in atoms:
            model = column == start and bool(re.fullmatch(r"(?:\d{5,8}|[A-Za-z][A-Za-z0-9_.-]*\d[A-Za-z0-9_.-]*)", text))
            dimension = column > start and bool(re.fullmatch(r"[左右]路|[RGB]", text))
            cell = table.rows[ri].cells[column]
            native = page.get_textbox(fitz.Rect(cell)) if cell else ""
            label_text = _centered_cell_text(page, cell) if cell else ""
            if not (model or dimension) or _compact(label_text) != text:
                valid = False; break
            for deeper in [c for c in active["labels"] if c > column]:
                del active["labels"][deeper]
            active["labels"][column] = {"text": label_text, "native_cell_text":native,
                                       "row_index": ri, "column":column, "bbox": list(cell)}
        if not valid or start not in active["labels"]:
            active = None; continue
        result_cell = table.rows[ri].cells[stop]
        if not result_cell or numeric_result_kind(_display(row[stop])) is None:
            active = None; continue
        if not all(label["bbox"][1] <= result_cell[1] + .02 and label["bbox"][3] >= result_cell[3] - .02
                   for label in active["labels"].values()):
            active = None; continue
        active["children"].append({"row_index": ri, "labels": [dict(active["labels"][c]) for c in sorted(active["labels"])]})
    bindings = {}
    for block in blocks:
        keys = [tuple(_compact(l["text"]) for l in child["labels"]) for child in block["children"]]
        if len(keys) != len(set(keys)):
            continue
        for child in block["children"]:
            bindings[child["row_index"]] = {k:v for k,v in block.items() if k not in {"labels", "children"}} | child
    return bindings


def _parent_bound_decision(page: fitz.Page, table: Any, table_index: int, mapping: dict[str, int],
                           binding: dict[str, Any], result_text: str, sources: list[dict[str, Any]],
                           source_sha256: str) -> dict[str, Any] | None:
    parent = page.get_textbox(fitz.Rect(binding["parent_bbox"]))
    if list(table.rows[binding["parent_row"]].cells[mapping["标准要求"]] or []) != binding["parent_bbox"]:
        return None
    if parent != binding["parent_text"] or _simple_parent_requirement(parent) != binding["clause"]:
        return None
    result_cell = table.rows[binding["row_index"]].cells[mapping["检验结果"]]
    native_result = page.get_textbox(fitz.Rect(result_cell)) if result_cell else ""
    if _compact(native_result) != _compact(result_text):
        return None
    parsed = _parse_result(result_text)
    if parsed.get("unit") and not (_criterion_from_requirement(parent) or {}).get("unit"):
        return None
    evidence = [{"role":"parent_requirement", "row_index":binding["parent_row"],
                 "bbox":binding["parent_bbox"], "text":parent}]
    for i, label in enumerate(binding["labels"]):
        cell = table.rows[label["row_index"]].cells[label["column"]]
        if (list(cell or []) != label["bbox"] or label["bbox"][1] > result_cell[1]+.02
                or label["bbox"][3] < result_cell[3]-.02):
            return None
        if (page.get_textbox(fitz.Rect(label["bbox"])) != label["native_cell_text"]
                or _compact(_centered_cell_text(page,label["bbox"])) != _compact(label["text"])):
            return None
        evidence.append({"role":"model" if i == 0 else "dimension", **label})
    evidence.append({"role":"result", "row_index":binding["row_index"], "bbox":list(result_cell), "text":native_result})
    for e in evidence:
        e.update(document_role="report", pdf_page=page.number+1, table_index=table_index, source_sha256=source_sha256)
    model = _compact(binding["labels"][0]["text"])
    conflicts = []
    for source in sources:
        rect = fitz.Rect(source["bbox"])
        if source["pdf_page"] == page.number+1 and (rect & fitz.Rect(binding["parent_bbox"])).get_area() >= .98 * rect.get_area():
            continue  # This is the validated parent statement itself.
        annotation = bool(re.match(r"(?:注\d*[:：]?|备注[:：]?|注意[:：]?|[*※])", _compact(source["text"])))
        parts = re.split(r"(?m)(?=^\s*\d+(?:\.\d+){2,5}(?![\d.]))", source["text"])
        for part in parts:
            text = _compact(part)
            heading = _CLAUSE_REFERENCE.match(text)
            parent_context = False
            if (heading and heading[0] == binding["clause"] and source["pdf_page"] == page.number+1
                    and (rect & fitz.Rect(binding["parent_bbox"])).get_area() > 0):
                parent_tail = _compact(parent)[_compact(parent).index(binding["clause"]):]
                # Remove only text already verified in the parent cell. A
                # native block may append child rows after it, or split the
                # trailing unit into a different block. Any residual limit
                # retains this parameter's review context.
                if text.startswith(parent_tail):
                    text = text[len(parent_tail):]; parent_context = True
                elif parent_tail.startswith(text):
                    text = ""; parent_context = True
            owners = {heading[0]} if heading else set(source.get("owner_parameter_clauses", []))
            target_mention = bool(re.search(rf"(?<![\d.]){re.escape(binding['clause'])}(?![\d.])", text))
            explicit_model = bool(re.search(rf"(?:型号)?{re.escape(model)}(?:的)?(?:上限|下限|限值|应|不得|不小于|不大于)", text))
            # Ordinary source statements owned by another parameter are not
            # amendments to this one.  Explicit changes/notes/references back
            # to the target are retained even inside another subsection.
            if (owners and binding["clause"] not in owners and not target_mention
                    and not annotation and not explicit_model and not _AMENDMENT.search(text)
                    and not re.search(r"(?:上|前|原)(?:页|表)|下页|后页", text)):
                continue
            native = {**source,"owner_clauses":sorted(owners | set(source.get("owner_clauses", [])))}
            for sentence in re.split(r"[。；;]", text):
                linked = _angular_reference_condition({**native,"annotation_context":annotation or parent_context}, sentence, model,
                    binding["clause"], page.number+1, {"table_index":table_index,"intro_row":binding["parent_row"],
                    "row_index":binding["row_index"],"parameter_clause":binding["clause"]}, grip_parameter=False)
                if linked:
                    conflicts.append({**source,"unresolved_text":sentence,"reference_binding":linked})
    local = evaluate_numeric_measurement(parent, result_text)
    if local["status"] == "manual":
        return None
    return {**local, "status":"manual" if conflicts else local["status"],
            "reason":"parent_requirement_condition_unresolved" if conflicts else
                     "parent_requirement_bound_pass" if local["status"] == "pass" else "parent_requirement_bound_error",
            "requirement_binding":{"kind":"same_page_parent_requirement", "parameter_clause":binding["clause"],
                "model":model,"dimensions":[_compact(l["text"]) for l in binding["labels"][1:]],
                "evidence":evidence,"local_status":local["status"],"unresolved_conditions":conflicts}}


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
    angular_sources: list[dict[str, Any]] | None = None
    angular_source_hash: str | None = None
    active_clause_references: list[dict[str, Any]] = []
    active_reference_section: dict[str, Any] | None = None
    source_table_snapshots: dict[int, list[dict[str, Any]]] = {}

    for page in document:
        printed_page = _printed_page_number(page)
        page_tables = []
        for table_index, table in enumerate(_report_page_tables(page)):
            if not _is_formal_report_table(table):
                continue
            rows = table.extract()
            mapping = _header_map(rows[0])
            page_tables.append((table_index,table,rows,mapping))
        source_table_snapshots[page.number] = [_source_table_snapshot(ti,t,rows,mapping)
                                               for ti,t,rows,mapping in page_tables]
        for table_index, table, rows, mapping in page_tables:
            formal_table_pages.add(page.number + 1)
            standard_columns = tuple(range(mapping["标准要求"], mapping["检验结果"]))
            result_index = mapping["检验结果"]
            angular_bindings = _same_page_angular_bindings(table, rows, mapping)
            parent_bindings = _same_page_parent_bindings(page, table, rows, mapping)
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
                        active_clause_references = []
                        active_reference_section = None
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
                for column, component in zip(standard_columns, row_components):
                    referred = _requirement_clause_references(component)
                    if referred:
                        cells = table.rows[row_index].cells
                        cell = cells[column] if column < len(cells) else None
                        native = page.get_textbox(fitz.Rect(cell)) if cell else ""
                        active_clause_references.append({"clauses": referred, "pdf_page": page.number + 1,
                            "table_index": table_index, "row_index": row_index, "bbox": list(cell) if cell else None,
                            "text": native, "projection": component,
                            "native_projection_matches": _compact(native) == _compact(component)})
                    heading = _CLAUSE_REFERENCE.match(_compact(component))
                    if heading:
                        active_reference_section = None
                        declared = {c for entry in active_clause_references for c in entry["clauses"]}
                        if heading[0] in declared:
                            cells = table.rows[row_index].cells
                            cell = cells[column] if column < len(cells) else None
                            native = page.get_textbox(fitz.Rect(cell)) if cell else ""
                            active_reference_section = {"clause": heading[0], "pdf_page": page.number + 1,
                                "table_index": table_index, "row_index": row_index,
                                "bbox": list(cell) if cell else None, "text": native, "projection": component,
                                "native_projection_matches": _compact(native) == _compact(component)}
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
                additional_kind = _additional_numeric_result_kind(result_text) if result_kind is None else None
                if additional_kind:
                    result_kind = additional_kind
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

                decision = ({"status": "manual", "reason": "newly_covered_numeric_format_requires_review",
                             "criterion": None, "coverage_recovery": additional_kind}
                            if additional_kind else evaluate_numeric_measurement(requirement, result_text))
                if decision.get("reason") == "acceptance_criterion_not_resolved" and row_index in angular_bindings:
                    if angular_sources is None:
                        angular_sources = _angular_native_sources(document, table_snapshots=source_table_snapshots)
                        path = Path(document.name) if document.name else None
                        angular_source_hash = hashlib.sha256(path.read_bytes() if path and path.is_file() else document.tobytes()).hexdigest()
                    structured = _structured_angular_decision(page, table, table_index, mapping,
                        angular_bindings[row_index], result_text, active_clause, angular_sources, angular_source_hash or "",
                        active_clause_references, active_reference_section)
                    if structured is not None:
                        decision = structured
                if decision.get("reason") == "acceptance_criterion_not_resolved" and row_index in parent_bindings:
                    if angular_sources is None:
                        angular_sources = _angular_native_sources(document, table_snapshots=source_table_snapshots)
                        path = Path(document.name) if document.name else None
                        angular_source_hash = hashlib.sha256(path.read_bytes() if path and path.is_file() else document.tobytes()).hexdigest()
                    structured = _parent_bound_decision(page, table, table_index, mapping, parent_bindings[row_index],
                                                        result_text, angular_sources, angular_source_hash or "")
                    if structured is not None:
                        decision = structured
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
