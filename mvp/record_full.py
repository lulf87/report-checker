from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable, Literal, Sequence

try:
    import pymupdf as fitz
except ImportError:
    import fitz

from mvp.checker import compact, display_text, header_map, is_formal_report_table


RectTuple = tuple[float, float, float, float]
Decision = Literal["match", "mismatch", "manual", "not_applicable", "excluded"]
Disposition = Literal["matched", "mismatch", "manual", "not_applicable", "excluded"]
ObservationKind = Literal["status", "numeric", "percent", "text", "table3_reference"]


@dataclass(frozen=True, slots=True)
class ReportRow:
    """One physical data row from a formal Report result table."""

    row_id: str
    sequence: int
    row_ordinal: int
    pdf_page: int
    project_raw: str
    clause_raw: str
    requirement_raw: str
    result_raw: str
    conclusion_raw: str
    unit_context: str | None
    condition_tokens: tuple[str, ...]
    requirement_rect: RectTuple | None
    result_rect: RectTuple | None
    conclusion_rect: RectTuple | None
    requirement_parts: tuple[str, ...] = ()
    requirement_path: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


@dataclass(frozen=True, slots=True)
class Observation:
    """A coordinate-bearing observation extracted from a Record cell."""

    observation_id: str
    kind: ObservationKind
    raw: str
    candidates: tuple[str, ...] = ()
    value: Decimal | None = None
    unit: str | None = None
    bbox: RectTuple | None = None
    extraction_method: str = "native_pdf"
    confidence_state: str = "resolved"

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


@dataclass(frozen=True, slots=True)
class RecordRow:
    """A physical source row from either supported Record family."""

    row_id: str
    record_item: str
    row_ordinal: int
    pdf_page: int
    clause_raw: str
    requirement_raw: str
    observations: tuple[Observation, ...] = ()
    status_candidates: tuple[str, ...] = ()
    cell_rects: tuple[RectTuple, ...] = ()
    exclusion_reason: str | None = None
    project_raw: str = ""
    suggestion_raw: str = ""
    printed_page: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


@dataclass(frozen=True, slots=True)
class CoverageEntry:
    """Disposition for one source/target mapping edge.

    Either side may be absent for an explicitly unpaired source or Report row,
    but an entry with both sides absent is invalid.
    """

    source_row_id: str | None
    target_row_id: str | None
    disposition: Disposition
    reason_code: str
    finding_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


@dataclass(frozen=True, slots=True)
class ComparisonResult:
    decision: Decision
    reason_code: str
    expected: str | None
    observed: str | None
    normalized_record_value: Decimal | None = None

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


@dataclass(frozen=True, slots=True)
class ParsedNumeric:
    comparator: str | None
    value: Decimal
    unit: str | None
    decimal_places: int


class CoverageInvariantError(ValueError):
    pass


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    return value


def _rect_tuple(cell: Any) -> RectTuple | None:
    if cell is None:
        return None
    rect = fitz.Rect(cell)
    if rect.is_empty or rect.is_infinite:
        return None
    return tuple(round(float(value), 3) for value in rect)  # type: ignore[return-value]


def _sequence_selector(sequence_range: Any) -> Any:
    if sequence_range is None:
        return lambda _: True
    if isinstance(sequence_range, int):
        return lambda value: value == sequence_range
    if isinstance(sequence_range, range):
        return lambda value: value in sequence_range
    if (
        isinstance(sequence_range, tuple)
        and len(sequence_range) == 2
        and all(isinstance(value, int) for value in sequence_range)
    ):
        lower, upper = sequence_range
        if lower > upper:
            lower, upper = upper, lower
        return lambda value: lower <= value <= upper
    selected = frozenset(int(value) for value in sequence_range)
    return lambda value: value in selected


_UNIT_CONTEXT_RE = re.compile(
    r"单位\s*[:：]\s*([A-Za-zµμΩΩ℃°%]+(?:\([^)]*\))?)",
    flags=re.IGNORECASE,
)


def extract_unit_context(text: str) -> str | None:
    match = _UNIT_CONTEXT_RE.search(text or "")
    return match.group(1).strip() if match else None


_CONDITION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("潮湿预处理前", re.compile(r"潮湿预处理前")),
    ("潮湿预处理后", re.compile(r"潮湿预处理后")),
    ("正常状态", re.compile(r"正常状态|\bNC\b", flags=re.IGNORECASE)),
    ("单一故障状态", re.compile(r"单一故障状态|\bSFC\b", flags=re.IGNORECASE)),
    ("a.c.", re.compile(r"(?<![A-Za-z])a\.?\s*c\.?(?![A-Za-z])", flags=re.IGNORECASE)),
    ("d.c.", re.compile(r"(?<![A-Za-z])d\.?\s*c\.?(?![A-Za-z])", flags=re.IGNORECASE)),
)


def extract_condition_tokens(text: str) -> tuple[str, ...]:
    return tuple(label for label, pattern in _CONDITION_PATTERNS if pattern.search(text or ""))


def scan_report_rows(
    report_path: str | Path,
    sequence_range: int | range | tuple[int, int] | Sequence[int] | None = None,
) -> list[ReportRow]:
    """Extract formal Report rows without inferring or normalizing their content.

    A two-integer tuple is an inclusive range. A ``range`` retains normal Python
    semantics. The returned coordinates are PDF points and use one-based PDF
    page numbers.
    """

    selected = _sequence_selector(sequence_range)
    rows_out: list[ReportRow] = []
    active_sequence: int | None = None
    row_ordinals: dict[int, int] = {}
    project_context: dict[int, str] = {}
    clause_context: dict[int, str] = {}
    unit_context: dict[int, str] = {}
    requirement_context: dict[int, list[str]] = {}

    with fitz.open(Path(report_path)) as document:
        for page in document:
            for table_index, table in enumerate(page.find_tables().tables):
                if not is_formal_report_table(table):
                    continue
                extracted = table.extract()
                if not extracted:
                    continue
                mapping = header_map(extracted[0])
                for row_index, row in enumerate(extracted[1:], start=1):
                    sequence_text = (
                        compact(row[mapping["序号"]])
                        if mapping["序号"] < len(row)
                        else ""
                    )
                    sequence_match = re.fullmatch(r"(?:续)?(\d+)", sequence_text)
                    if sequence_match:
                        active_sequence = int(sequence_match.group(1))
                        row_ordinals.setdefault(active_sequence, 0)

                    if active_sequence is None:
                        continue

                    def value(column: str) -> str:
                        index = mapping[column]
                        return display_text(row[index]) if index < len(row) else ""

                    project_cell = value("检验项目")
                    clause_cell = value("标准条款")
                    requirement_start = mapping["标准要求"]
                    requirement_end = mapping["检验结果"]
                    requirement_parts = tuple(
                        display_text(row[index]) if index < len(row) else ""
                        for index in range(requirement_start, requirement_end)
                    )
                    requirement = requirement_parts[0] if requirement_parts else ""
                    result = value("检验结果")
                    conclusion = value("单项结论")
                    if project_cell:
                        project_context[active_sequence] = project_cell
                    if clause_cell:
                        clause_context[active_sequence] = clause_cell
                    contexts = requirement_context.setdefault(
                        active_sequence,
                        [""] * len(requirement_parts),
                    )
                    if len(contexts) < len(requirement_parts):
                        contexts.extend([""] * (len(requirement_parts) - len(contexts)))
                    for index, part in enumerate(requirement_parts):
                        if not part:
                            continue
                        contexts[index] = part
                        for child_index in range(index + 1, len(contexts)):
                            contexts[child_index] = ""
                    requirement_path = tuple(value for value in contexts if value)
                    requirement_text = " ".join(requirement_path or requirement_parts)
                    found_unit = extract_unit_context(requirement_text)
                    if found_unit:
                        unit_context[active_sequence] = found_unit

                    # Ignore structural blank rows, while retaining every physical
                    # row that carries user-visible Report content.
                    if not any((sequence_text, project_cell, clause_cell, requirement, result, conclusion)):
                        continue
                    if not selected(active_sequence):
                        continue

                    row_ordinals[active_sequence] += 1
                    table_row = table.rows[row_index]

                    def cell_rect(column: str) -> RectTuple | None:
                        index = mapping[column]
                        return _rect_tuple(table_row.cells[index]) if index < len(table_row.cells) else None

                    rows_out.append(
                        ReportRow(
                            row_id=(
                                f"report:p{page.number + 1:03d}:t{table_index + 1:02d}:"
                                f"r{row_index:03d}:s{active_sequence:03d}"
                            ),
                            sequence=active_sequence,
                            row_ordinal=row_ordinals[active_sequence],
                            pdf_page=page.number + 1,
                            project_raw=project_cell or project_context.get(active_sequence, ""),
                            clause_raw=clause_cell or clause_context.get(active_sequence, ""),
                            requirement_raw=requirement,
                            result_raw=result,
                            conclusion_raw=conclusion,
                            unit_context=found_unit or unit_context.get(active_sequence),
                            condition_tokens=extract_condition_tokens(requirement_text),
                            requirement_rect=cell_rect("标准要求"),
                            result_rect=cell_rect("检验结果"),
                            conclusion_rect=cell_rect("单项结论"),
                            requirement_parts=requirement_parts,
                            requirement_path=requirement_path,
                        )
                    )
    return rows_out


_PLACEHOLDERS = {"——", "/"}
_INVALID_SINGLE_DASHES = {"-", "–", "—", "−"}


def is_actual_report_result(value: Any) -> bool:
    normalized = compact(value)
    if not normalized or normalized in _PLACEHOLDERS | _INVALID_SINGLE_DASHES:
        return False
    if normalized in {"符合要求", "不符合要求"}:
        return False
    return re.search(r"[0-9A-Za-z]", normalized) is not None


def _compare_strict_status(
    source_status: Any,
    report_result: Any,
    mapping: dict[str, str],
) -> ComparisonResult:
    source = compact(source_status)
    observed = compact(report_result)
    expected = mapping.get(source)
    if expected is None:
        return ComparisonResult("manual", "source_status_unresolved", None, observed or None)
    if not observed:
        return ComparisonResult("mismatch", "report_result_missing", expected, None)
    if expected == "<actual-or-conforming>":
        matched = observed == "符合要求" or is_actual_report_result(observed)
    else:
        matched = observed == expected
    return ComparisonResult(
        "match" if matched else "mismatch",
        "status_result_matched" if matched else "status_result_mismatch",
        expected,
        observed,
    )


RECORD61_STATUS_MAPPING = {
    "符合": "<actual-or-conforming>",
    "不符合": "不符合要求",
    "不适用": "——",
}

RECORD202_STATUS_MAPPING = {
    "√": "<actual-or-conforming>",
    "×": "不符合要求",
    "△": "——",
    "/": "/",
}


def compare_record61_status_result(record_status: Any, report_result: Any) -> ComparisonResult:
    return _compare_strict_status(record_status, report_result, RECORD61_STATUS_MAPPING)


def compare_record202_status_result(record_status: Any, report_result: Any) -> ComparisonResult:
    return _compare_strict_status(record_status, report_result, RECORD202_STATUS_MAPPING)


def expected_report_conclusion_from_record_statuses(
    statuses: Iterable[Any],
    *,
    mode: Literal["9706.1", "9706.202"],
) -> str | None:
    values = [compact(value) for value in statuses]
    allowed = set(RECORD61_STATUS_MAPPING if mode == "9706.1" else RECORD202_STATUS_MAPPING)
    if not values or any(value not in allowed for value in values):
        return None
    if mode == "9706.1":
        if "不符合" in values:
            return "不符合"
        if "符合" in values:
            return "符合"
        return "/"
    if "×" in values:
        return "不符合"
    if "√" in values:
        return "符合"
    return "/"


_UNIT_DEFINITIONS: dict[str, tuple[str, Decimal]] = {
    "A": ("current", Decimal("1")),
    "mA": ("current", Decimal("0.001")),
    "uA": ("current", Decimal("0.000001")),
    "V": ("voltage", Decimal("1")),
    "mV": ("voltage", Decimal("0.001")),
    "kV": ("voltage", Decimal("1000")),
    "Ω": ("resistance", Decimal("1")),
    "mΩ": ("resistance", Decimal("0.001")),
    "kΩ": ("resistance", Decimal("1000")),
    "MΩ": ("resistance", Decimal("1000000")),
    "W": ("power", Decimal("1")),
    "mW": ("power", Decimal("0.001")),
    "kW": ("power", Decimal("1000")),
    "Hz": ("frequency", Decimal("1")),
    "kHz": ("frequency", Decimal("1000")),
    "MHz": ("frequency", Decimal("1000000")),
    "F": ("capacitance", Decimal("1")),
    "uF": ("capacitance", Decimal("0.000001")),
    "nF": ("capacitance", Decimal("0.000000001")),
    "s": ("time", Decimal("1")),
    "ms": ("time", Decimal("0.001")),
    "%": ("percent", Decimal("1")),
    "°C": ("temperature", Decimal("1")),
    "dB": ("sound", Decimal("1")),
    "dB(A)": ("sound_a", Decimal("1")),
    "dB(C)": ("sound_c", Decimal("1")),
}


_UNIT_ALIASES = {
    "µA": "uA",
    "μA": "uA",
    "UA": "uA",
    "ua": "uA",
    "Ω": "Ω",
    "ohm": "Ω",
    "Ohm": "Ω",
    "℃": "°C",
}


def normalize_unit(unit: str | None) -> str | None:
    if unit is None:
        return None
    value = re.sub(r"\s+", "", unit)
    return _UNIT_ALIASES.get(value, value) or None


def decimal_value(value: Decimal | int | str | float) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise TypeError("boolean is not a numeric observation")
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid decimal value: {value!r}") from exc


def convert_decimal(
    value: Decimal | int | str | float,
    from_unit: str | None,
    to_unit: str | None,
) -> Decimal:
    number = decimal_value(value)
    source = normalize_unit(from_unit)
    target = normalize_unit(to_unit)
    if source == target:
        return number
    if source is None or target is None:
        raise ValueError(f"unit is missing: {from_unit!r} -> {to_unit!r}")
    source_definition = _UNIT_DEFINITIONS.get(source)
    target_definition = _UNIT_DEFINITIONS.get(target)
    if source_definition is None or target_definition is None:
        raise ValueError(f"unsupported unit conversion: {from_unit!r} -> {to_unit!r}")
    if source_definition[0] != target_definition[0]:
        raise ValueError(f"incompatible units: {from_unit!r} -> {to_unit!r}")
    return number * source_definition[1] / target_definition[1]


_NUMERIC_RE = re.compile(
    r"^\s*(?P<comparator><=|>=|[<>≤≥＜＞])?\s*"
    r"(?P<number>[+＋\-－−]?(?:\d+(?:\.\d*)?|\.\d+))\s*"
    r"(?P<unit>[A-Za-zµμΩΩ℃°%]+(?:\([^)]*\))?)?\s*$"
)


def parse_report_numeric(text: Any, unit_context: str | None = None) -> ParsedNumeric | None:
    source = display_text(text)
    match = _NUMERIC_RE.fullmatch(source)
    if not match:
        return None
    number_token = (
        match.group("number")
        .replace("＋", "+")
        .replace("－", "-")
        .replace("−", "-")
    )
    comparator = match.group("comparator")
    if comparator:
        comparator = {
            "＜": "<",
            "＞": ">",
            "≤": "<=",
            "≥": ">=",
        }.get(comparator, comparator)
    decimals = len(number_token.partition(".")[2]) if "." in number_token else 0
    return ParsedNumeric(
        comparator=comparator,
        value=Decimal(number_token),
        unit=normalize_unit(match.group("unit") or unit_context),
        decimal_places=decimals,
    )


def quantize_for_report(value: Decimal | int | str | float, decimal_places: int) -> Decimal:
    if decimal_places < 0:
        raise ValueError("decimal_places must not be negative")
    quantum = Decimal("1").scaleb(-decimal_places)
    return decimal_value(value).quantize(quantum, rounding=ROUND_HALF_UP)


def compare_numeric_observation(
    record_value: Decimal | int | str | float,
    record_unit: str | None,
    report_text: Any,
    report_unit_context: str | None = None,
) -> ComparisonResult:
    parsed = parse_report_numeric(report_text, report_unit_context)
    if parsed is None:
        return ComparisonResult(
            "manual",
            "report_numeric_not_parsed",
            None,
            compact(report_text) or None,
        )
    try:
        converted = convert_decimal(record_value, record_unit, parsed.unit)
    except (TypeError, ValueError) as exc:
        return ComparisonResult(
            "manual",
            "unit_conversion_unresolved",
            str(parsed.value),
            str(exc),
        )

    if parsed.comparator is not None:
        comparisons = {
            "<": converted < parsed.value,
            "<=": converted <= parsed.value,
            ">": converted > parsed.value,
            ">=": converted >= parsed.value,
        }
        matched = comparisons[parsed.comparator]
        return ComparisonResult(
            "match" if matched else "mismatch",
            "numeric_threshold_matched" if matched else "numeric_threshold_mismatch",
            f"{parsed.comparator}{parsed.value}",
            str(converted),
            converted,
        )

    rounded = quantize_for_report(converted, parsed.decimal_places)
    matched = rounded == parsed.value
    return ComparisonResult(
        "match" if matched else "mismatch",
        "numeric_value_matched" if matched else "numeric_value_mismatch",
        str(parsed.value),
        str(rounded),
        converted,
    )


_PERCENT_NUMBER = r"[+＋\-－−]?(?:\d+(?:\.\d*)?|\.\d+)"
_PERCENT_RE = re.compile(rf"(?P<number>{_PERCENT_NUMBER})\s*[%％]")
_PERCENT_RANGE_RE = re.compile(
    rf"(?P<lower>{_PERCENT_NUMBER})\s*[%％]?\s*"
    rf"(?:~|～|〜|∼|至|-)\s*"
    rf"(?P<upper>{_PERCENT_NUMBER})\s*[%％]"
)


def _normalized_percentage_decimal(token: str) -> Decimal:
    return Decimal(token.replace("＋", "+").replace("－", "-").replace("−", "-"))


def extract_percentage_values(text: Any) -> tuple[Decimal, ...]:
    """Return the percentage values that must be copied to the Report.

    A written range keeps both ordered endpoints.  When the source explicitly
    marks a correction/final value, only the last percentage is authoritative;
    this never recalculates a percentage from other measurements.
    """

    source = display_text(text)
    range_match = _PERCENT_RANGE_RE.search(source)
    if range_match:
        return (
            _normalized_percentage_decimal(range_match.group("lower")),
            _normalized_percentage_decimal(range_match.group("upper")),
        )
    matches = list(_PERCENT_RE.finditer(source))
    if not matches:
        return ()
    values = tuple(_normalized_percentage_decimal(match.group("number")) for match in matches)
    if len(values) > 1 and re.search(r"最终|改为|更正|→|->", source):
        return (values[-1],)
    return values


def extract_final_percentage(text: Any) -> tuple[str, Decimal] | None:
    matches = list(_PERCENT_RE.finditer(display_text(text)))
    if not matches:
        return None
    token = matches[-1].group("number")
    return f"{token}%", _normalized_percentage_decimal(token)


def compare_final_percentage(record_text: Any, report_text: Any) -> ComparisonResult:
    record_values = extract_percentage_values(record_text)
    report_values = extract_percentage_values(report_text)
    if not record_values and not report_values:
        return ComparisonResult("manual", "percentage_missing_both_sides", None, None)
    if not record_values:
        return ComparisonResult(
            "mismatch",
            "record_percentage_missing",
            None,
            ",".join(f"{value}%" for value in report_values) or None,
        )
    if not report_values:
        return ComparisonResult(
            "mismatch",
            "report_percentage_missing",
            ",".join(f"{value}%" for value in record_values),
            None,
        )
    matched = record_values == report_values
    return ComparisonResult(
        "match" if matched else "mismatch",
        "percentage_copied" if matched else "percentage_mismatch",
        ",".join(f"{value}%" for value in record_values),
        ",".join(f"{value}%" for value in report_values),
    )


_DISPOSITION_RANK: dict[Disposition, int] = {
    "excluded": 0,
    "not_applicable": 1,
    "matched": 2,
    "manual": 3,
    "mismatch": 4,
}


def _aggregate_disposition(values: Iterable[Disposition]) -> Disposition:
    return max(values, key=_DISPOSITION_RANK.__getitem__)


def validate_coverage(
    eligible_source_row_ids: Iterable[str],
    eligible_report_row_ids: Iterable[str],
    entries: Iterable[CoverageEntry],
) -> dict[str, Any]:
    """Enforce row-level coverage conservation on both document sides."""

    source_ids = tuple(eligible_source_row_ids)
    report_ids = tuple(eligible_report_row_ids)
    if len(source_ids) != len(set(source_ids)):
        raise CoverageInvariantError("eligible source row ids are not unique")
    if len(report_ids) != len(set(report_ids)):
        raise CoverageInvariantError("eligible Report row ids are not unique")

    source_set = set(source_ids)
    report_set = set(report_ids)
    entry_list = list(entries)
    seen_edges: set[tuple[str | None, str | None]] = set()
    source_dispositions: dict[str, list[Disposition]] = {row_id: [] for row_id in source_ids}
    report_dispositions: dict[str, list[Disposition]] = {row_id: [] for row_id in report_ids}

    for entry in entry_list:
        if entry.source_row_id is None and entry.target_row_id is None:
            raise CoverageInvariantError("coverage entry has neither source nor target")
        edge = (entry.source_row_id, entry.target_row_id)
        if edge in seen_edges:
            raise CoverageInvariantError(f"duplicate coverage edge: {edge}")
        seen_edges.add(edge)
        if entry.source_row_id is not None:
            if entry.source_row_id not in source_set:
                raise CoverageInvariantError(f"unknown source row: {entry.source_row_id}")
            source_dispositions[entry.source_row_id].append(entry.disposition)
        if entry.target_row_id is not None:
            if entry.target_row_id not in report_set:
                raise CoverageInvariantError(f"unknown Report row: {entry.target_row_id}")
            report_dispositions[entry.target_row_id].append(entry.disposition)

    missing_source = [row_id for row_id, values in source_dispositions.items() if not values]
    missing_report = [row_id for row_id, values in report_dispositions.items() if not values]
    if missing_source or missing_report:
        raise CoverageInvariantError(
            f"coverage is incomplete: source={missing_source}, report={missing_report}"
        )

    source_final = {
        row_id: _aggregate_disposition(values)
        for row_id, values in source_dispositions.items()
    }
    report_final = {
        row_id: _aggregate_disposition(values)
        for row_id, values in report_dispositions.items()
    }

    def summary(values: dict[str, Disposition]) -> dict[str, Any]:
        counts = {
            disposition: sum(value == disposition for value in values.values())
            for disposition in _DISPOSITION_RANK
        }
        total = len(values)
        accounted = sum(counts.values())
        if accounted != total:
            raise CoverageInvariantError(
                f"coverage conservation failed: total={total}, accounted={accounted}"
            )
        return {
            "eligible": total,
            "accounted": accounted,
            "row_ids": list(values),
            "dispositions": counts,
            "conserved": True,
        }

    return {
        "source_rows": summary(source_final),
        "report_rows": summary(report_final),
        "entries": len(entry_list),
    }
