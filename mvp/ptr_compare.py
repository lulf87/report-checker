"""Deterministic PTR ↔ Report comparison engine.

This module is the rule-first comparison layer used by both the offline
review package and the enabled formal runner.  Direct calls remain read-only
and non-publishable; :mod:`mvp.run_ptr_report` is the only layer that maps a
validated comparison into a formal WorkerResult.

The important safety rule is that an absent or ambiguous fact is ``manual``;
only evidence-backed, deterministic matches are ``matched``.  A future
``rules+llm`` caller may use the ``manual`` rows as a small review queue, but
this module never calls an LLM and never lets one replace a hard rule.
"""

from __future__ import annotations

import argparse
from difflib import SequenceMatcher
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
from mvp.ptr_report_contract import PTR_P01_RULE_VERSION


PTR_COMPARE_SCHEMA_VERSION = "ptr-compare-0.5"
# Kept as a compatibility alias for callers of the comparison layer.  The
# contract module owns the canonical version so the comparator and capability
# catalog cannot silently drift apart.
PTR_COMPARE_RULE_VERSION = PTR_P01_RULE_VERSION
PTR_COMPARE_RULE_ID = "PTR-P01"

MATCHED = "matched"
MISMATCH = "mismatch"
MANUAL = "manual"
EXCLUDED = "excluded"

# A clause begins with a dotted number and a human-readable title/body.  The
# body guard prevents table cells such as ``2.2±20%`` from becoming clauses.
CLAUSE_LINE_RE = re.compile(
    r"^\s*(?P<clause>\d+(?:\s*\.\s*\d+){1,8})\s*"
    r"(?:[、，,:：)）]\s*|[.．](?!\s*\d)\s*|\s+|(?=[\u3400-\u9fff]))(?P<body>.*\S)?\s*$"
)
# A token must be followed by a clause separator or whitespace plus a human
# readable title.  This avoids treating decimals such as ``2.76 μs`` and
# standard years such as ``9706.202-2021`` as clause boundaries.
CLAUSE_TOKEN_RE = re.compile(
    r"(?<![\d.])(?P<clause>\d+(?:\s*\.\s*\d+){1,8})"
    r"(?=(?:\s+[A-Za-z\u3400-\u9fff]|[.．、:：)）]\s*[A-Za-z\u3400-\u9fff]))"
)
STANDARD_RE = re.compile(r"(?:GB|YY)\s*\d+(?:\.\d+)+\s*-\s*\d{4}", re.IGNORECASE)
TABLE_RE = re.compile(
    r"(?:表|附录)\s*(?:[A-Za-z]?\d+|[A-Za-z]+)(?:[.\-－]\d+)*",
    re.IGNORECASE,
)
FIGURE_RE = re.compile(
    r"(?:图|Fig(?:ure)?\.?)[ \t]*[A-Za-z]?\d+(?:[.\-－]\d+)*",
    re.IGNORECASE,
)
PROTOCOL_IDENTIFIER_RE = re.compile(
    r"\b(?:IEEE|RS|USB|CAN)\s*[-/]?\s*\d+(?:[.\-]\d+)*(?:-[A-Za-z0-9]+)?\b"
    r"|\bISO\s*[-/]?\s*\d+(?:[.\-]\d+)*(?:-[A-Za-z0-9]+)?\b",
    re.IGNORECASE,
)
NUMBER_RE = re.compile(
    r"(?:(?P<operator>[<>≤≥=≈~～±+\-])\s*)?"
    r"(?<![A-Za-zμµΩΩ])"
    r"(?P<number>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>%|[A-Za-zμµΩΩ°℃·]+(?:\^?\d+|[⁰¹²³⁴⁵⁶⁷⁸⁹]+)?"
    r"(?:\s*/\s*(?:[A-Za-zμµΩΩ°℃·]+(?:\^?\d+|[⁰¹²³⁴⁵⁶⁷⁸⁹]+)?|件|器械))?)?"
)
UNIT_LABEL_RE = re.compile(
    r"单位\s*[:：]\s*(%|[A-Za-zμµΩΩ°℃·]+(?:\^?\d+|[⁰¹²³⁴⁵⁶⁷⁸⁹]+)?"
    r"(?:\s*/\s*(?:[A-Za-zμµΩΩ°℃·]+(?:\^?\d+|[⁰¹²³⁴⁵⁶⁷⁸⁹]+)?|件|器械))?)",
    re.IGNORECASE,
)

# Report 的标准要求单元格经常同时携带“接受标准”和执行测试时的附加
# 参数。下面这些行不是接受标准本身：它们在 Report 中作为测试模式、缩放
# 档位或适用型号出现。只按明确的标签识别，避免把普通正文中的数字泛化
# 地删掉。原文仍保留在 evidence 中，并通过 auxiliary_lines 输出供审计。
REPORT_AUXILIARY_LINE_RE = re.compile(
    r"^\s*(?:"
    r"运动距离缩放|"
    r"(?:产品)?型号(?:规格)?|适用型号|"
    r"测试条件|试验条件|检验条件|检测条件|测量条件|"
    r"测试参数|试验参数|检验参数|检测参数|"
    r"测试模式|试验模式|检验模式|检测模式|"
    r"模式参数|工作模式|"
    r"设置|参数"
    r")\s*(?:[:：]|(?=\d|[A-Za-z]))",
    re.IGNORECASE,
)

_SUPERSCRIPT_DIGITS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")

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
    if len(parts) <= 1:
        return None
    last = re.fullmatch(r"(\d+)([a-z])", parts[-1], re.IGNORECASE)
    if last:
        return ".".join([*parts[:-1], last.group(1)])
    return ".".join(parts[:-1])


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
        "−": "-",
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


def _split_report_auxiliary_lines(value: Any) -> tuple[str, list[str]]:
    """Separate clearly labelled Report test metadata from acceptance text.

    Report templates sometimes append model lists, motion scaling presets or
    test-mode parameters to the same ``标准要求`` cell.  Those lines are
    useful evidence, but they are not the PTR acceptance criterion.  The
    classifier is deliberately label-based and conservative; an unlabelled
    line remains part of the acceptance text.
    """

    lines = str(value or "").splitlines()
    acceptance: list[str] = []
    auxiliary: list[str] = []
    for line in lines:
        if REPORT_AUXILIARY_LINE_RE.match(line.strip()):
            auxiliary.append(line.strip())
        else:
            acceptance.append(line)
    return "\n".join(acceptance), auxiliary


def _normalization_for_numeric(value: Any) -> str:
    """Normalize numeric input without joining adjacent line values.

    ``normalize_comparison_text`` intentionally removes all whitespace for
    text equality.  Doing that before numeric parsing can join ``mm`` with
    the first digit on the next line (``mm0.96``), turning two facts into a
    bogus unit.  Numeric parsing keeps a single separator instead.
    """

    text = unicodedata.normalize("NFKC", str(value or ""))
    replacements = {
        "—": "-",
        "–": "-",
        "－": "-",
        "−": "-",
        "～": "~",
        "，": ",",
        "；": ";",
        "：": ":",
        "（": "(",
        "）": ")",
        "【": "[",
        "】": "]",
        "µ": "μ",
        "Ω": "Ω",
        "×": "x",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    text = re.sub(r"(?i)(?<=\d)\s*u\s*(?=[gGlLmMnNsSvVaApPΩ])", "μ", text)
    text = re.sub(r"(?i)usec", "μs", text)
    text = re.sub(r"(?i)msec", "ms", text)
    text = re.sub(r"(?<=[\u3400-\u9fff]):\s*(?=[\u3400-\u9fff])", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _numeric_signature(value: Any, *, role: str | None = None) -> list[tuple[str, str, str]]:
    """Return ordered numeric facts as ``(operator, value, unit)`` tuples."""

    text = _numeric_source_text(value, role=role)
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


def _raw_numeric_signature(value: Any, *, role: str | None = None) -> list[tuple[str, str, str]]:
    """Keep the written unit spelling for validating separate unit labels."""
    text = _numeric_source_text(value, role=role)
    return [
        (match.group("operator") or "", match.group("number"),
         _clean_numeric_unit(match.group("unit") or ""))
        for match in NUMBER_RE.finditer(text)
    ]


def _numeric_source_text(value: Any, *, role: str | None = None) -> str:
    """Remove references that look numeric but are not measured facts."""

    source = value
    if str(role or "").lower() == "report":
        source, _ = _split_report_auxiliary_lines(value)
    text = _strip_document_noise(_normalization_for_numeric(source))
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
    text = FIGURE_RE.sub("", text)
    # Printed page counters and cross-page markers are layout evidence, not
    # acceptance values (for example ``第 2/17 页``).
    text = re.sub(r"第\s*\d+\s*(?:/\s*\d+\s*)?页", "", text)
    text = re.sub(r"\b\d+\s*/\s*\d+\s*页", "", text)
    # Document numbers and protocol identifiers are metadata in the PTR
    # header/protocol matrix, not acceptance values.  Keep the surrounding
    # text so a wording difference can still enter the manual text queue.
    text = re.sub(
        r"(?:文件|文档|图纸|编号)\s*[:：]\s*[A-Za-z0-9][A-Za-z0-9._-]*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = PROTOCOL_IDENTIFIER_RE.sub("", text)
    # Deep clause references (2.10.1.2) cannot be a scalar measurement.
    text = re.sub(r"\d+\.\d+(?:\.\d+)+", "", text)
    # Short references such as “符合2.1.4的要求” need their local context.
    text = re.sub(r"(?:符合|满足|见|第|条款|章节|引用|参见|按照)\d+(?:\.\d+)?", "", text)
    # Product/document identifiers such as IEEE802.3, RS485 and R2 are not
    # requirement measurements. Keep a digit when it is a unit exponent
    # (m3/mm3) or a known reciprocal-unit suffix (min-1); those are physical
    # facts rather than identifiers.
    product_number_re = re.compile(r"(?<=[A-Za-z])\d+(?:\.\d+)?(?:-[A-Za-z0-9]+)?")

    def drop_product_number(match: re.Match[str]) -> str:
        start = match.start()
        prefix = text[max(0, start - 8) : start]
        if re.search(r"(?:mm|cm|μm|nm|m|V|A|W|Hz|s|Ω|N|Pa|J|C|g|L|min)$", prefix):
            return match.group(0)
        return ""

    text = product_number_re.sub(drop_product_number, text)
    return text


def _clean_numeric_unit(value: str) -> str:
    unit = value.replace("µ", "μ").replace("Ω", "Ω")
    unit = unit.translate(_SUPERSCRIPT_DIGITS)
    unit = re.sub(r"\s+", "", unit).replace("^", "")
    # PDF extraction can join a), b) list markers to the preceding unit:
    # ``10 nmb)`` means 10 nm, while ``12 Nmb)`` means 12 Nm.  Keep
    # uppercase base-unit letters (``mA``/``mV``) intact.  The same pattern
    # handles c) and d) markers, but only after a known unit token.
    unit = re.sub(r"(nm|μm|mm|cm|m|n|N|Ω|V|A|W|s)(?:[a-d])$", r"\1", unit)
    return unit


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def _parse_unit_product(value: str) -> tuple[str, Decimal] | None:
    """Return a base-unit product and scale for a controlled unit token."""

    raw = value.replace("µ", "μ").replace("Ω", "Ω")
    raw = raw.translate(_SUPERSCRIPT_DIGITS)
    raw = re.sub(r"\s+", "", raw).replace("^", "")
    if raw in _BASE_UNITS:
        return _BASE_UNITS[raw], Decimal(1)
    pieces = raw.split("·")
    canonical: list[str] = []
    scale = Decimal(1)
    for piece in pieces:
        exponent = 1
        exponent_match = re.fullmatch(r"(?P<body>.*?)(?P<exponent>\d+)", piece)
        if exponent_match:
            piece = exponent_match.group("body")
            exponent = int(exponent_match.group("exponent"))
            if exponent <= 0:
                return None
        if piece in _BASE_UNITS:
            canonical.append(f"{_BASE_UNITS[piece]}^{exponent}" if exponent != 1 else _BASE_UNITS[piece])
            continue
        match = re.fullmatch(r"(?P<prefix>[pnuμmckKMG])(?P<base>.+)", piece)
        if not match or match.group("base") not in _BASE_UNITS:
            return None
        canonical_base = _BASE_UNITS[match.group("base")]
        canonical.append(f"{canonical_base}^{exponent}" if exponent != 1 else canonical_base)
        scale *= _UNIT_PREFIX_SCALE[match.group("prefix")] ** exponent
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


def _canonical_unit_label(value: str) -> str:
    """Normalize a separately printed unit label without erasing scale."""

    label = str(value or "").replace("µ", "μ").replace("Ω", "Ω")
    label = label.translate(_SUPERSCRIPT_DIGITS)
    label = re.sub(r"\s+", "", label).replace("^", "")
    # Common PDF spellings of torque units omit the middle dot.  The alias is
    # intentionally narrow: MΩ/GΩ and m³/mm³ remain distinct.
    label = {
        "Nm": "N·m",
        "Ncm": "N·cm",
        "Nmm": "N·mm",
    }.get(label, label)
    return label


def _references(value: Any) -> dict[str, list[str]]:
    text = _strip_document_noise(normalize_comparison_text(value))
    standards = sorted({normalize_comparison_text(item) for item in STANDARD_RE.findall(text)})
    tables = sorted({normalize_comparison_text(item) for item in TABLE_RE.findall(text)})
    return {"standards": standards, "tables": tables}


def _unit_labels(value: Any) -> list[str]:
    labels = []
    for match in UNIT_LABEL_RE.finditer(unicodedata.normalize("NFKC", str(value or ""))):
        label = _canonical_unit_label(match.group(1))
        if label:
            labels.append(label)
    return sorted(set(labels))


def _text_for_comparison(value: Any, *, role: str | None = None) -> str:
    """Drop redundant Report ``单位：X`` labels from the text channel.

    The labels are compared separately by ``_compare_text``.  This keeps a
    repeated unit declaration from becoming a text mismatch while still
    exposing a contradictory declaration (for example ``5 MΩ`` plus
    ``单位：GΩ``) as a hard unit finding.
    """

    source = value
    if str(role or "").lower() == "report":
        source, _ = _split_report_auxiliary_lines(value)
    text = unicodedata.normalize("NFKC", str(source or ""))
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


def _text_diff_display(value: Any, *, role: str | None = None) -> str:
    """Return readable source text for the side-by-side difference view."""

    source = value
    if str(role or "").lower() == "report":
        source, _ = _split_report_auxiliary_lines(value)
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(source or ""))).strip()


_DIFF_TOKEN_RE = re.compile(
    # The final ``\\S`` keeps every other character (Ω, μ, °…) instead of
    # silently dropping it from the displayed text.
    r"[A-Za-z0-9]+(?:[._/\-][A-Za-z0-9]+)*|[\u3400-\u9fff\uf900-\ufaff]|[^\w\s]|\s+|\S",
    re.UNICODE,
)


def _text_diff_segments(report_value: Any, ptr_value: Any) -> dict[str, Any]:
    """Build safe, display-oriented character/token diff segments.

    Whitespace-only PDF layout changes are collapsed before diffing.  Chinese
    text is compared character by character while Latin words and identifiers
    stay together, so the UI can mark the smallest useful differing spans.
    """

    report_text = _text_diff_display(report_value, role="report")
    ptr_text = _text_diff_display(ptr_value, role="ptr")
    # The rule already treats layout whitespace and other presentation-only
    # differences as equal. Keep the source text available for context, but do
    # not paint those harmless differences as user-facing changes.
    if normalize_comparison_text(report_text) == normalize_comparison_text(ptr_text):
        return {
            "status": "same",
            "report_text": report_text,
            "ptr_text": ptr_text,
            "report_segments": [{"op": "equal", "text": report_text}] if report_text else [],
            "ptr_segments": [{"op": "equal", "text": ptr_text}] if ptr_text else [],
        }
    report_tokens = _DIFF_TOKEN_RE.findall(report_text)
    ptr_tokens = _DIFF_TOKEN_RE.findall(ptr_text)
    matcher = SequenceMatcher(None, report_tokens, ptr_tokens, autojunk=False)
    report_segments: list[dict[str, str]] = []
    ptr_segments: list[dict[str, str]] = []

    def append_segment(target: list[dict[str, str]], operation: str, tokens: Sequence[str]) -> None:
        if not tokens:
            return
        text = "".join(tokens)
        if target and target[-1]["op"] == operation:
            target[-1]["text"] += text
        else:
            target.append({"op": operation, "text": text})

    for operation, report_start, report_end, ptr_start, ptr_end in matcher.get_opcodes():
        if operation == "equal":
            append_segment(report_segments, operation, report_tokens[report_start:report_end])
            append_segment(ptr_segments, operation, ptr_tokens[ptr_start:ptr_end])
        elif operation == "delete":
            append_segment(report_segments, operation, report_tokens[report_start:report_end])
        elif operation == "insert":
            append_segment(ptr_segments, operation, ptr_tokens[ptr_start:ptr_end])
        else:  # replace
            append_segment(report_segments, operation, report_tokens[report_start:report_end])
            append_segment(ptr_segments, operation, ptr_tokens[ptr_start:ptr_end])
    return {
        "status": "same" if report_text == ptr_text else "different",
        "report_text": report_text,
        "ptr_text": ptr_text,
        "report_segments": report_segments,
        "ptr_segments": ptr_segments,
    }


# --------------------------------------------------------------------------
# Safe text equivalences (PTR-P01 0.5.1-draft)
#
# A strict normalized-text mismatch is first reported as TEXT_MISMATCH.  The
# rules below then try to explain the difference with *presentation-only*
# causes observed in real PTR/Report pairs.  Each rule removes one narrowly
# defined kind of material; the result is accepted only when the remaining
# PTR and Report strings are exactly equal.  Every applied rule is recorded so
# the reviewer can audit why a visible difference was not escalated.
# --------------------------------------------------------------------------

SAFE_EQUIVALENCE_RULES = {
    "PTR_RUNNING_HEADER": "PTR 页眉/页脚（多数页面重复出现的行）",
    "PTR_FIGURE_LABELS": "PTR 插图内文字及图注（图N…）",
    "REPORT_UNIT_LINE": "Report“单位：X”行（X 与 PTR 中的单位一致）",
    "REPORT_RESULT_LABELS": "Report 结果子行标签（如 X方向/Y方向、左路/右路）",
    "PUNCTUATION_ONLY": "仅标点或项目符号不同",
    "REPORT_REPEATED_TEXT": "Report 重复写了同一要求",
    "REPORT_EXPANDED_REFERENCE": "Report 展开了所引用条款的原文（已与 PTR 原条款核对）",
}

_FIGURE_CAPTION_RE = re.compile(r"^\s*图\s*\d+(?:[.\-－]\d+)*(?:\s|[㐀-鿿A-Za-z(（]|$)")
_REQUIREMENT_WORD_RE = re.compile(r"[应须需不无得≤≥<>±=＜＞]|大于|小于|至少|超过|符合|要求|允差|偏差|\d")
_SENTENCE_END_RE = re.compile(r"[。；;：:]\s*$")
_UNIT_LINE_RE = re.compile(r"^\s*单位\s*[:：]\s*(?P<unit>\S.*?)\s*$")
_EXPANDED_REFERENCE_RE = re.compile(r"^(?P<clause>\d+(?:\.\d+)+)\.?(?P<letter>[a-z])\)(?P<body>.+)$")
# Punctuation that never carries a technical value.  ``.`` and ``,`` between
# digits are kept (decimal point / thousands separator).
_PUNCT_RE = re.compile(r"[。；;：:、●•·▪\"'“”‘’]|(?<!\d)[.,]|[.,](?!\d)")


def ptr_running_header_lines(path: str | Path) -> set[str]:
    """Return normalized lines repeated on most PTR pages (headers/footers)."""

    counts: defaultdict[str, int] = defaultdict(int)
    try:
        document = fitz.open(path)
    except Exception:  # pragma: no cover - unreadable PDFs are handled upstream
        return set()
    with document:
        page_count = len(document)
        for page in document:
            seen = {
                normalize_comparison_text(line)
                for line in page.get_text().splitlines()
                if line.strip()
            }
            for line in seen:
                counts[line] += 1
    threshold = max(3, int(page_count * 0.4 + 0.999))
    return {
        line
        for line, count in counts.items()
        if count >= threshold and len(line) >= 4 and not re.match(r"^\d+(?:\.\d+)+", line)
    }


def _drop_running_header_lines(lines: list[str], headers: set[str]) -> tuple[list[str], bool]:
    """Drop lines made up only of running-header pieces.

    Page text often splits ``力反馈…控制系统`` and ``文件：1098908-01`` into two
    lines while the clause extractor joins them; a line counts as header when
    nothing is left after removing the known header strings.
    """

    ordered = sorted(headers, key=len, reverse=True)

    def is_header(line: str) -> bool:
        rest = normalize_comparison_text(line)
        if not rest:
            return False
        for header in ordered:
            rest = rest.replace(header, "")
        return not rest

    kept = [line for line in lines if not is_header(line)]
    return kept, len(kept) != len(lines)


def _drop_figure_label_lines(lines: list[str]) -> tuple[list[str], bool]:
    """Drop ``图N`` captions and the short label lines drawn inside the figure."""

    result: list[str] = []
    changed = False
    for line in lines:
        if not _FIGURE_CAPTION_RE.match(line):
            result.append(line)
            continue
        changed = True
        # Walk back over short, unpunctuated fragments: those are labels
        # inside the drawing (``肩部左右伸展θ1``), not requirement sentences.
        while result:
            previous = result[-1]
            normalized = normalize_comparison_text(previous)
            if (
                not normalized
                or len(normalized) > 20
                or _SENTENCE_END_RE.search(previous)
                or CLAUSE_LINE_RE.match(previous)
                or re.search(r"[应须]|表\s*\d", previous)
            ):
                break
            result.pop()
    return result, changed


def _drop_report_unit_lines(lines: list[str], ptr_normalized: str) -> tuple[list[str], bool]:
    ptr_plain = _strip_punctuation(ptr_normalized)
    kept: list[str] = []
    changed = False
    for line in lines:
        match = _UNIT_LINE_RE.match(unicodedata.normalize("NFKC", line))
        if match:
            unit = _strip_punctuation(normalize_comparison_text(match.group("unit")))
            if unit and unit in ptr_plain:
                changed = True
                continue
        kept.append(line)
    return kept, changed


def _parallel_labels(labels: Sequence[str]) -> bool:
    if len(labels) < 2:
        return False
    if all(re.fullmatch(r"[A-Za-z+\-]+", label) for label in labels):
        return True
    suffixes = {label[-2:] for label in labels}
    prefixes = {label[:2] for label in labels}
    return len(suffixes) == 1 or len(prefixes) == 1


def _is_label_line(line: str) -> bool:
    normalized = normalize_comparison_text(line)
    return bool(
        normalized
        and len(normalized) <= 10
        and not _REQUIREMENT_WORD_RE.search(normalized)
        and not _UNIT_LINE_RE.match(unicodedata.normalize("NFKC", line))
    )


def _drop_report_result_labels(lines: list[str]) -> tuple[list[str], bool]:
    """Drop blocks of parallel result sub-row labels from a Report cell.

    A block qualifies when it consists of at least two short, parallel labels
    (``X方向/Y方向``, ``非Firefly 模式/Firefly 模式``, ``Yaw/Pitch``) without
    digits or requirement words, and sits either at the end of the cell or
    directly after a ``单位：X`` line (the Report puts per-row labels under the
    unit of the requirement they belong to).
    """

    result: list[str] = []
    changed = False
    index = 0
    while index < len(lines):
        end = index
        while end < len(lines) and _is_label_line(lines[end]):
            end += 1
        if end > index:
            labels = [normalize_comparison_text(line) for line in lines[index:end]]
            previous = result[-1] if result else ""
            after_unit = bool(_UNIT_LINE_RE.match(unicodedata.normalize("NFKC", previous)))
            trailing = end == len(lines) and bool(result)
            if _parallel_labels(labels) and (after_unit or trailing):
                changed = True
            else:
                result.extend(lines[index:end])
            index = end
            continue
        result.append(lines[index])
        index += 1
    return result, changed


def _strip_punctuation(value: str) -> str:
    return _PUNCT_RE.sub("", value)


def _reduce_report_repetitions(
    report_text: str,
    ptr_plain: str,
    ptr_text_by_clause: Mapping[str, str],
) -> tuple[str, list[str]]:
    """Drop repeated sentences and verified expanded references from Report."""

    applied: list[str] = []
    kept: list[str] = []
    for piece in re.split(r"[。；;]", report_text):
        plain = _strip_punctuation(piece)
        if not plain:
            continue
        if kept and plain in "".join(kept):
            if "REPORT_REPEATED_TEXT" not in applied:
                applied.append("REPORT_REPEATED_TEXT")
            continue
        expanded = _EXPANDED_REFERENCE_RE.match(piece.strip())
        if expanded and plain not in ptr_plain:
            target = _strip_punctuation(
                ptr_text_by_clause.get(expanded.group("clause") + expanded.group("letter"), "")
            )
            body = _strip_punctuation(expanded.group("body"))
            if target and body and body in target:
                if "REPORT_EXPANDED_REFERENCE" not in applied:
                    applied.append("REPORT_EXPANDED_REFERENCE")
                continue
        kept.append(plain)
    return "".join(kept), applied


def safe_text_equivalence(
    ptr_source: Any,
    report_source: Any,
    *,
    ptr_headers: set[str] | None = None,
    ptr_text_by_clause: Mapping[str, str] | None = None,
) -> list[str] | None:
    """Return the applied rule ids when the texts are equivalent, else ``None``."""

    applied: list[str] = []
    ptr_lines = str(ptr_source or "").splitlines()
    report_body, _ = _split_report_auxiliary_lines(report_source)
    report_lines = report_body.splitlines()

    ptr_lines, changed = _drop_running_header_lines(ptr_lines, ptr_headers or set())
    if changed:
        applied.append("PTR_RUNNING_HEADER")
    ptr_lines, changed = _drop_figure_label_lines(ptr_lines)
    if changed:
        applied.append("PTR_FIGURE_LABELS")
    ptr_text = _text_for_comparison("\n".join(ptr_lines), role="ptr")
    # Labels are recognised by the ``单位：`` line above them, so detect them
    # before the unit lines are removed.
    report_lines, labels_changed = _drop_report_result_labels(report_lines)
    report_lines, changed = _drop_report_unit_lines(report_lines, normalize_comparison_text("\n".join(ptr_lines)))
    if changed:
        applied.append("REPORT_UNIT_LINE")
    if labels_changed:
        applied.append("REPORT_RESULT_LABELS")
    report_text = _text_for_comparison("\n".join(report_lines), role="report")
    if ptr_text == report_text:
        return applied or None
    ptr_plain = _strip_punctuation(ptr_text)
    report_plain = _strip_punctuation(report_text)
    if ptr_plain == report_plain:
        return [*applied, "PUNCTUATION_ONLY"]
    reduced, extra = _reduce_report_repetitions(report_text, ptr_plain, ptr_text_by_clause or {})
    if extra and reduced == ptr_plain:
        return [*applied, *extra]
    return None


_INLINE_NUMERIC_MARKER_RE = re.compile(
    r"(?m)^\s*(?P<clause>2(?:\s*\.\s*\d+){1,8})"
    r"(?=(?:\s+[A-Za-z\u3400-\u9fff]|[、，,:：;；)）]\s*[A-Za-z\u3400-\u9fff]|$))"
)
_INLINE_LETTER_MARKER_RE = re.compile(r"(?m)^\s*(?P<letter>[a-z])\s*[)）](?=\s*)", re.IGNORECASE)


def _inline_observation_text(value: Mapping[str, Any]) -> str:
    """Return searchable text for a clause evidence or table observation."""

    text = value.get("text") or value.get("requirement_text") or value.get("caption") or ""
    if not text and value.get("rows"):
        text = "\n".join(" | ".join(str(cell or "") for cell in row) for row in value.get("rows", []))
    return str(text or "")


def _inline_text_key(value: Any) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value or "")))


def _split_inline_observations(
    observations: Sequence[Mapping[str, Any]],
    segments: Sequence[Mapping[str, Any]],
    base_clause: str,
) -> dict[str, list[Mapping[str, Any]]]:
    """Assign physical observations to the inline clause that owns them.

    Inline PTR/Report clauses are often extracted as one parent node.  The
    parser may then create ``2.2.2e`` or ``2.3.6.6a`` nodes from that text, but
    their source evidence must be split at the same boundaries.  Observations
    without a new marker (continuation lines and referenced tables) stay with
    the most recently seen child marker.
    """

    segment_ids = [str(item.get("clause_id") or "") for item in segments if item.get("clause_id")]
    if not segment_ids:
        return {}
    canonical_ids = {_canonical_clause_id(value): value for value in segment_ids}
    base = canonical_ids.get(_canonical_clause_id(base_clause), segment_ids[0])
    letter_ids: dict[str, str] = {}
    for clause_id in segment_ids:
        match = re.fullmatch(r"(.+?)([a-z])", clause_id, re.IGNORECASE)
        if match and _canonical_clause_id(match.group(1)) == _canonical_clause_id(base):
            letter_ids[match.group(2).lower()] = clause_id

    def markers(value: Mapping[str, Any]) -> list[str]:
        text = _inline_observation_text(value)
        found: list[str] = []
        for match in _INLINE_NUMERIC_MARKER_RE.finditer(text):
            clause_id = _canonical_clause_id(match.group("clause"))
            if clause_id in canonical_ids and clause_id not in found:
                found.append(clause_id)
        for match in _INLINE_LETTER_MARKER_RE.finditer(text):
            clause_id = letter_ids.get(match.group("letter").lower())
            if clause_id and clause_id not in found:
                found.append(clause_id)
        return found

    def sort_key(item: tuple[int, Mapping[str, Any]]) -> tuple[float, float, float, int]:
        index, value = item
        bbox = value.get("bbox") or value.get("rect") or [0, 0, 0, 0]
        if not value.get("bbox") and not value.get("rect"):
            boxes = [
                cell.get("bbox")
                for cell in value.get("cells", []) or []
                if isinstance(cell, Mapping) and cell.get("bbox")
            ]
            if boxes:
                bbox = [
                    min(float(box[0]) for box in boxes),
                    min(float(box[1]) for box in boxes),
                    max(float(box[2]) for box in boxes),
                    max(float(box[3]) for box in boxes),
                ]
        try:
            page = float(value.get("pdf_page") or 0)
            top = float(bbox[1])
            left = float(bbox[0])
        except (TypeError, ValueError, IndexError):
            page, top, left = 0.0, 0.0, 0.0
        return page, top, left, index

    assigned: dict[str, list[Mapping[str, Any]]] = {clause_id: [] for clause_id in segment_ids}
    current = base
    for _, observation in sorted(enumerate(observations), key=sort_key):
        found = markers(observation)
        if found:
            for clause_id in found:
                assigned[clause_id].append(observation)
            current = found[-1]
        else:
            assigned.setdefault(current, []).append(observation)

    # A malformed or merged source cell can hide the marker.  Use the first
    # distinctive body text as a conservative fallback instead of returning a
    # child with no physical location at all.
    for segment in segments:
        clause_id = str(segment.get("clause_id") or "")
        if clause_id == base or assigned.get(clause_id):
            continue
        body = segment.get("text") or "\n".join(segment.get("parts", []))
        needle = _inline_text_key(body)[:32]
        if not needle:
            continue
        for observation in observations:
            if needle in _inline_text_key(_inline_observation_text(observation)):
                assigned[clause_id].append(observation)
                break
    return assigned


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

    # OCR fallback uses the same page-counter filter as the native layout
    # parser. Import lazily to avoid a module cycle during parser startup.
    from mvp.ptr_layout import is_page_counter_line

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
        if is_page_counter_line(line):
            continue
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
        node["normalized_text"] = _text_for_comparison(node["text"], role=role)
        node["numeric_signature"] = _numeric_signature(node["text"], role=role)
        node["raw_numeric_signature"] = _raw_numeric_signature(node["text"], role=role)
        node["references"] = _references(node["text"])
        node["unit_labels"] = _unit_labels(node["text"])
        node["auxiliary_lines"] = (
            _split_report_auxiliary_lines(node["text"])[1]
            if role == "report"
            else []
        )
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


def _table_cell_items(value: Any) -> tuple[str, ...]:
    """Normalize a cell into bullet items without losing wrapped text."""

    raw = unicodedata.normalize("NFKC", str(value or ""))
    parts = re.split(r"[\uf09f●•]\s*", raw)
    result = []
    for part in parts:
        normalized = _text_for_comparison(part)
        if normalized:
            result.append(normalized)
    return tuple(result)


def _reference_cycle_candidates(nodes: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Find table-to-table reference cycles from extracted table evidence.

    This is intentionally a candidate detector.  A cycle is never resolved by
    guessing an expansion order; callers keep the affected comparison rows in
    manual review.
    """

    labels_to_ids: defaultdict[str, set[str]] = defaultdict(set)
    table_text: dict[str, str] = {}

    def referenced_labels(value: str) -> list[str]:
        text = normalize_comparison_text(value)
        return sorted(
            {
                normalize_comparison_text(match.group(1))
                for match in re.finditer(
                    r"(?:见|参见|详见|参考|依据|按照|符合|参照)\s*"
                    r"(表\s*[0-9A-Za-z一二三四五六七八九十]+|附录\s*[A-Za-z一二三四五六七八九十]+)",
                    text,
                )
            }
        )

    for node in nodes:
        role = str(node.get("role") or "document")
        node_id = str(node.get("node_id") or "node")
        for index, table in enumerate(node.get("table_rows") or [], start=1):
            if not isinstance(table, Mapping):
                continue
            label = compact(str(table.get("label") or table.get("caption") or ""))
            table_id = str(table.get("table_id") or f"{role}:{node_id}:table:{index}")
            if not label:
                continue
            labels_to_ids[label].add(table_id)
            rows = table.get("rows") or []
            row_text = "\n".join(
                " ".join(str(cell or "") for cell in row)
                for row in rows
                if isinstance(row, Sequence) and not isinstance(row, (str, bytes))
            )
            # The caption identifies the current table; it is not a reference
            # to another table and must not create a self-cycle.
            table_text[table_id] = row_text
    edges: defaultdict[str, set[str]] = defaultdict(set)
    for source_id, text in table_text.items():
        source_label = next((label for label, ids in labels_to_ids.items() if source_id in ids), None)
        if not source_label:
            continue
        for target_label in referenced_labels(text):
            target_key = compact(target_label)
            if target_key in labels_to_ids:
                edges[source_label].add(target_key)

    cycles: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()

    def visit(start: str, current: str, path: list[str], active: set[str]) -> None:
        for target in sorted(edges.get(current, ())):
            if target == start:
                cycle = path[:]
                rotations = [tuple(cycle[index:] + cycle[:index]) for index in range(len(cycle))]
                key = min(rotations)
                if key in seen:
                    continue
                seen.add(key)
                cycles.append(
                    {
                        "labels": list(key),
                        "table_ids": sorted(
                            {table_id for label in key for table_id in labels_to_ids.get(label, set())}
                        ),
                        "status": "manual",
                        "reason_code": "REFERENCE_CYCLE_CANDIDATE",
                    }
                )
            elif target not in active:
                visit(start, target, [*path, target], active | {target})

    for label in sorted(edges):
        visit(label, label, [label], {label})
    return cycles


def _semantic_table_fingerprint(rows: Any) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Group vertically continued rows by their first physical column."""

    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return ()
    grouped: dict[str, list[str]] = {}
    order: list[str] = []
    current = ""
    for row in rows:
        if not isinstance(row, Sequence) or isinstance(row, (str, bytes)):
            continue
        cells = [str(value or "") for value in row]
        if len(cells) < 2:
            continue
        key = _text_for_comparison(cells[0])
        if key:
            current = key
            if key not in grouped:
                grouped[key] = []
                order.append(key)
        if not current:
            continue
        grouped[current].extend(_table_cell_items(cells[1]))
    return tuple((key, tuple(grouped[key])) for key in order if grouped[key])


def _table_comparison(ptr: Mapping[str, Any], report: Mapping[str, Any]) -> dict[str, Any]:
    """Compare only physically extracted table matrices; otherwise stay manual."""

    references = (ptr.get("references") or {}).get("tables", []) or (report.get("references") or {}).get("tables", [])
    if not references:
        return {"status": MATCHED, "reason_code": None, "ptr_tables": 0, "report_tables": 0}
    ptr_tables = list(ptr.get("table_rows") or [])
    report_tables = list(report.get("table_rows") or [])
    ptr_fingerprints = {_table_fingerprint(table.get("rows")) for table in ptr_tables}
    ptr_semantic_fingerprints = {_semantic_table_fingerprint(table.get("rows")) for table in ptr_tables}
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
    report_semantic_fingerprints = {_semantic_table_fingerprint(rows) for rows in grouped_report.values()}
    ptr_fingerprints.discard(())
    report_fingerprints.discard(())
    ptr_semantic_fingerprints.discard(())
    report_semantic_fingerprints.discard(())
    if ptr_fingerprints and report_fingerprints and ptr_fingerprints & report_fingerprints:
        return {
            "status": MATCHED,
            "reason_code": "TABLE_NORMALIZED_MATCH",
            "ptr_tables": len(ptr_fingerprints),
            "report_tables": len(report_fingerprints),
        }
    if ptr_semantic_fingerprints and report_semantic_fingerprints and ptr_semantic_fingerprints & report_semantic_fingerprints:
        return {
            "status": MATCHED,
            "reason_code": "TABLE_SEMANTIC_NORMALIZED_MATCH",
            "ptr_tables": len(ptr_semantic_fingerprints),
            "report_tables": len(report_semantic_fingerprints),
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
    report_acceptance_text, report_auxiliary_lines = _split_report_auxiliary_lines(requirement)
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
                "normalized_text": _text_for_comparison(parent_text, role="report"),
                "numeric_signature": _numeric_signature(parent_text, role="report"),
                "references": _references(parent_text),
                "unit_labels": _unit_labels(parent_text),
                "auxiliary_lines": report_auxiliary_lines,
                "is_heading_only": not _numeric_signature(parent_text, role="report"),
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
                "normalized_text": _text_for_comparison(body, role="report"),
                "numeric_signature": _numeric_signature(body, role="report"),
                "references": _references(body),
                "unit_labels": _unit_labels(body),
                "auxiliary_lines": _split_report_auxiliary_lines(body)[1],
                "is_heading_only": len(body.splitlines()) == 1 and not _numeric_signature(body, role="report"),
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


def _scope_clause_ids(scope: Mapping[str, Any]) -> list[str]:
    """Return the normalized, declared scope items in stable order."""

    if scope.get("status") != "parsed":
        return []
    values = {str(value) for value in scope.get("included_clause_ids", []) if str(value).strip()}
    return sorted(values, key=_clause_sort_key)


def _entry_covers_scope(entry: Mapping[str, Any], scope_id: str) -> bool:
    clause_id = str(entry.get("clause_id") or "")
    return bool(clause_id and _is_descendant(clause_id, scope_id))


def _ocr_derived(node: Mapping[str, Any]) -> bool:
    """Return true when any evidence for a node came from local OCR."""

    return any(str(item.get("extraction_method", "")).startswith("ocr:") for item in node.get("evidence", []))


def _reference_binding_status(node: Mapping[str, Any]) -> tuple[str, str | None]:
    """Return whether every printed table/appendix token has one target."""

    labels = list((node.get("references") or {}).get("tables", []) or [])
    if not labels:
        return MATCHED, None
    bindings = list(node.get("reference_bindings") or [])
    if len(bindings) != len(labels):
        return MANUAL, "REFERENCE_BINDING_UNRESOLVED"
    statuses = {str(item.get("status")) for item in bindings if isinstance(item, Mapping)}
    if statuses & {"ambiguous", "unresolved"}:
        return MANUAL, "REFERENCE_BINDING_UNRESOLVED"
    if not statuses or not statuses.issubset({"bound", "bound_physical_rows"}):
        return MANUAL, "REFERENCE_BINDING_UNRESOLVED"
    return MATCHED, None


def _unit_peak_context(*values: Any) -> bool:
    text = " ".join(str(value or "") for value in values)
    return bool(re.search(r"(?:峰值|peak(?:\s*[- ]?to[- ]?peak)?|peak\s*value)", text, re.IGNORECASE))


def _unit_equivalent(left: str, right: str, *, peak_context: bool = False) -> tuple[bool, str]:
    """Compare two printed units while retaining scale and peak semantics."""

    left_label = _canonical_unit_label(left)
    right_label = _canonical_unit_label(right)
    if left_label == right_label:
        return True, "EXACT_UNIT_MATCH"
    left_peak = left_label.endswith("p") and len(left_label) > 1
    right_peak = right_label.endswith("p") and len(right_label) > 1
    left_base = left_label[:-1] if left_peak else left_label
    right_base = right_label[:-1] if right_peak else right_label
    if left_peak != right_peak and not peak_context:
        return False, "UNIT_PEAK_SEMANTICS_UNRESOLVED"
    left_parsed = _parse_unit_product(left_base)
    right_parsed = _parse_unit_product(right_base)
    if left_parsed is None or right_parsed is None:
        return False, "UNIT_SPELLING_DIFFERENCE"
    if left_parsed == right_parsed:
        if left_peak != right_peak:
            return True, "UNIT_PEAK_SUFFIX_EQUIVALENT"
        return True, "UNIT_SI_EQUIVALENT"
    # Same physical dimension with different prefix scales is deliberately a
    # risk, not an equivalence.  This keeps MΩ/GΩ and m³/mm³ visible.
    if left_parsed[0] == right_parsed[0]:
        return False, "UNIT_SCALE_MISMATCH"
    return False, "UNIT_DIMENSION_MISMATCH"


def _unit_labels_match_numeric(
    labels: Sequence[str],
    raw_units: Sequence[str],
    *,
    peak_context: bool,
) -> tuple[bool, str | None]:
    if not labels:
        return True, None
    if not raw_units:
        return True, None
    reasons: list[str] = []
    for label in labels:
        matches: list[str] = []
        for raw_unit in raw_units:
            equivalent, reason = _unit_equivalent(label, raw_unit, peak_context=peak_context)
            if equivalent:
                matches.append(reason)
        if not matches:
            # Prefer a scale-specific explanation when dimensions agree.
            for raw_unit in raw_units:
                _, reason = _unit_equivalent(label, raw_unit, peak_context=peak_context)
                if reason == "UNIT_SCALE_MISMATCH":
                    return False, reason
            return False, "UNIT_LABEL_CONTRADICTS_NUMERIC"
        reasons.extend(matches)
    if "UNIT_PEAK_SUFFIX_EQUIVALENT" in reasons:
        return True, "UNIT_PEAK_SUFFIX_EQUIVALENT"
    if "UNIT_SI_EQUIVALENT" in reasons:
        return True, "UNIT_SI_EQUIVALENT"
    return True, "EXACT_UNIT_MATCH"


def _compare_text(
    ptr: Mapping[str, Any],
    report: Mapping[str, Any],
    *,
    has_children: bool,
    context: Mapping[str, Any] | None = None,
    row_expansion: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    ptr_text = str(
        ptr.get("normalized_text")
        or _text_for_comparison(ptr.get("text"), role=str(ptr.get("role") or ""))
    )
    report_text = str(
        report.get("normalized_text")
        or _text_for_comparison(report.get("text"), role=str(report.get("role") or ""))
    )
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
    equivalence_rules: list[str] = []
    if text_status == MISMATCH:
        context = context or {}
        rules = safe_text_equivalence(
            ptr.get("text"),
            report.get("text"),
            ptr_headers=context.get("ptr_headers"),
            ptr_text_by_clause=context.get("ptr_text_by_clause"),
        )
        if rules:
            text_status = MATCHED
            reason = "SAFE_TEXT_EQUIVALENCE"
            equivalence_rules = rules
    ptr_numbers = list(
        ptr.get("numeric_signature")
        or _numeric_signature(ptr.get("text"), role=str(ptr.get("role") or ""))
    )
    report_numbers = list(
        report.get("numeric_signature")
        or _numeric_signature(report.get("text"), role=str(report.get("role") or ""))
    )
    numeric_status = MATCHED if _numeric_facts_equal(ptr_numbers, report_numbers) else MISMATCH
    ptr_refs = ptr.get("references") or _references(ptr.get("text"))
    report_refs = report.get("references") or _references(report.get("text"))
    reference_status = MATCHED if ptr_refs == report_refs else MISMATCH
    ptr_binding_status, ptr_binding_reason = _reference_binding_status(ptr)
    report_binding_status, report_binding_reason = _reference_binding_status(report)
    binding_status = (
        MATCHED
        if ptr_binding_status == MATCHED and report_binding_status == MATCHED
        else MANUAL
    )
    if row_expansion is not None and ptr_binding_status == MATCHED and report_binding_status != MATCHED:
        # The Report expansion was matched to a row of the bound PTR table:
        # that is the binding of the Report's “表N”.
        report_binding_status, report_binding_reason = MATCHED, None
        binding_status = MATCHED
    binding_reason = ptr_binding_reason or report_binding_reason
    ptr_units = list(ptr.get("unit_labels") or _unit_labels(ptr.get("text")))
    report_units = list(report.get("unit_labels") or _unit_labels(report.get("text")))
    ptr_raw_units = {
        unit
        for _, _, unit in (
            ptr.get("raw_numeric_signature")
            or _raw_numeric_signature(ptr.get("text"), role=str(ptr.get("role") or ""))
        )
        if unit
    }
    report_raw_units = {
        unit
        for _, _, unit in (
            report.get("raw_numeric_signature")
            or _raw_numeric_signature(report.get("text"), role=str(report.get("role") or ""))
        )
        if unit
    }
    peak_context = _unit_peak_context(ptr.get("text"), report.get("text"))
    ptr_label_ok, ptr_label_reason = _unit_labels_match_numeric(
        ptr_units, sorted(ptr_raw_units), peak_context=peak_context
    )
    report_label_ok, report_label_reason = _unit_labels_match_numeric(
        report_units, sorted(report_raw_units), peak_context=peak_context
    )
    if not ptr_label_ok or not report_label_ok:
        unit_status = MISMATCH
        unit_reason = ptr_label_reason or report_label_reason or "UNIT_LABEL_CONTRADICTS_NUMERIC"
    elif ptr_units and report_units:
        pair_reasons: list[str] = []
        all_left_match = True
        for left in ptr_units:
            left_matches = []
            for right in report_units:
                equivalent, reason = _unit_equivalent(left, right, peak_context=peak_context)
                if equivalent:
                    left_matches.append(reason)
            if not left_matches:
                all_left_match = False
            pair_reasons.extend(left_matches)
        all_right_match = all(
            any(_unit_equivalent(right, left, peak_context=peak_context)[0] for left in ptr_units)
            for right in report_units
        )
        if pair_reasons and all_left_match and all_right_match:
            unit_status = MATCHED
            unit_reason = (
                "UNIT_PEAK_SUFFIX_EQUIVALENT"
                if "UNIT_PEAK_SUFFIX_EQUIVALENT" in pair_reasons
                else "UNIT_SI_EQUIVALENT"
                if "UNIT_SI_EQUIVALENT" in pair_reasons
                else "EXACT_UNIT_MATCH"
            )
        else:
            unit_status = MISMATCH
            mismatch_reasons = [
                _unit_equivalent(left, right, peak_context=peak_context)[1]
                for left in ptr_units
                for right in report_units
            ]
            unit_reason = (
                "UNIT_SCALE_MISMATCH"
                if "UNIT_SCALE_MISMATCH" in mismatch_reasons
                else "UNIT_DIMENSION_MISMATCH"
                if "UNIT_DIMENSION_MISMATCH" in mismatch_reasons
                else "UNIT_PEAK_SEMANTICS_UNRESOLVED"
                if "UNIT_PEAK_SEMANTICS_UNRESOLVED" in mismatch_reasons
                else "UNIT_SPELLING_DIFFERENCE"
            )
    else:
        unit_status = MATCHED
        unit_reason = ptr_label_reason or report_label_reason or "NO_CONTRADICTORY_UNIT_LABEL"
    table_comparison = _table_comparison(ptr, report)
    if row_expansion is None and table_comparison["status"] == MANUAL:
        # The Report may expand the referenced PTR table row as labelled
        # values.  Verify that expansion cell by cell, then compare the rest
        # of the Report text with the ordinary rules.
        from mvp.ptr_table_rows import compare_row_expansion, report_text_without_expansion

        expansion = compare_row_expansion(ptr, report)
        if expansion is not None:
            reduced = {
                key: value
                for key, value in report.items()
                if key not in {
                    "normalized_text", "numeric_signature", "raw_numeric_signature",
                    "unit_labels", "auxiliary_lines", "table_rows",
                }
            }
            reduced["text"] = report_text_without_expansion(report, expansion)
            result = _compare_text(
                ptr, reduced, has_children=has_children, context=context, row_expansion=expansion
            )
            # The diff view shows the Report text *without* the verified
            # expansion; the expansion itself is listed cell by cell.
            result["text_comparison"]["report_source"] = str(report.get("text") or "")
            result["text_comparison"]["report_expansion_text"] = "\n".join(
                line for index, line in enumerate(
                    [item for item in str(report.get("text") or "").splitlines() if item.strip()]
                )
                if index in set(expansion.get("consumed_report_lines") or [])
            )
            return result
    if row_expansion is not None:
        expansion_status = str(row_expansion.get("status"))
        table_comparison = {
            "status": expansion_status,
            "reason_code": {
                MATCHED: "TABLE_ROW_EXPANSION_MATCH",
                MISMATCH: "TABLE_CELL_MISMATCH",
            }.get(expansion_status, "TABLE_CELL_REVIEW_REQUIRED"),
            "ptr_tables": len(ptr.get("table_rows") or []),
            "report_tables": 0,
            "row_expansion": dict(row_expansion),
        }
    if numeric_status == MISMATCH and (
        row_expansion is not None
        or not (
            table_comparison["status"] == MANUAL
            and ((ptr_refs.get("tables") or report_refs.get("tables")))
        )
    ):
        decision = MISMATCH
        decision_reason = "NUMERIC_FACT_MISMATCH"
    elif unit_status == MISMATCH:
        decision = MISMATCH
        decision_reason = "UNIT_MISMATCH"
    elif reference_status == MISMATCH:
        decision = MISMATCH
        decision_reason = "REFERENCE_MISMATCH"
    elif binding_status != MATCHED:
        decision = MANUAL
        decision_reason = binding_reason or "REFERENCE_BINDING_UNRESOLVED"
    else:
        decision = text_status
        decision_reason = reason
        if text_status == MISMATCH:
            decision = MANUAL
            decision_reason = "TEXT_REVIEW_REQUIRED"
    if (
        table_comparison["status"] == MANUAL
        and (ptr_refs.get("tables") or report_refs.get("tables"))
        and (numeric_status == MISMATCH or text_status == MISMATCH)
        and not (row_expansion is not None and decision == MISMATCH)
    ):
        decision = MANUAL
        decision_reason = str(table_comparison.get("reason_code") or "TABLE_CONTENT_NOT_EXTRACTED")
    if table_comparison["status"] == MISMATCH and decision != MISMATCH:
        decision = MISMATCH
        decision_reason = str(table_comparison.get("reason_code") or "TABLE_CELL_MISMATCH")
    return {
        "title_comparison": {"status": text_status, "reason_code": reason},
        "text_comparison": {
            "status": text_status,
            "ptr_normalized": ptr_text,
            "report_normalized": report_text,
            "ptr_source": str(ptr.get("text") or ""),
            "report_source": str(report.get("text") or ""),
            "text_diff": _text_diff_segments(report.get("text"), ptr.get("text")),
            "equivalence_rules": [
                {"rule": rule, "label": SAFE_EQUIVALENCE_RULES[rule]} for rule in equivalence_rules
            ],
            "report_auxiliary_lines": list(
                report.get("auxiliary_lines")
                or _split_report_auxiliary_lines(report.get("text"))[1]
            ),
        },
        "numeric_comparison": {
            "status": numeric_status,
            "ptr": ptr_numbers,
            "report": report_numbers,
        },
        "reference_comparison": {
            "status": reference_status,
            "binding_status": binding_status,
            "binding_reason_code": binding_reason,
            "ptr": ptr_refs,
            "report": report_refs,
            "ptr_bindings": list(ptr.get("reference_bindings") or []),
            "report_bindings": list(report.get("reference_bindings") or []),
        },
        "unit_comparison": {
            "status": unit_status,
            "ptr": ptr_units,
            "report": report_units,
            "reason_code": unit_reason,
            "risk": "safe_equivalent"
            if unit_status == MATCHED and unit_reason in {
                "UNIT_SI_EQUIVALENT",
                "UNIT_PEAK_SUFFIX_EQUIVALENT",
                "EXACT_UNIT_MATCH",
                "NO_CONTRADICTORY_UNIT_LABEL",
            }
            else "hard_mismatch"
            if unit_status == MISMATCH
            else "unresolved",
        },
        "table_comparison": table_comparison,
        "decision": decision,
        "reason_code": decision_reason,
    }


def _ledger_entry(
    *,
    ptr: Mapping[str, Any] | None,
    reports: Sequence[Mapping[str, Any]],
    clause_id: str | None = None,
    scope_decision: str,
    scope_reason: str,
    comparison: Mapping[str, Any] | None,
    decision: str,
    reason_code: str,
) -> dict[str, Any]:
    clause_id = str(clause_id or (ptr or reports[0]).get("clause_id"))
    report_ids = [str(item["node_id"]) for item in reports]
    identity = str(ptr["node_id"]) if ptr else "|".join(report_ids) or f"scope:{clause_id}"
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


def _clause_title_key(node: Mapping[str, Any] | None) -> str:
    """Comparable clause title: first line, before a colon, normalized."""

    if not node:
        return ""
    title = str(node.get("title") or str(node.get("text") or "").split("\n", 1)[0])
    title = re.split(r"[:：]", title, maxsplit=1)[0]
    return _strip_punctuation(normalize_comparison_text(title))


def _flag_clause_number_shifts(
    ledger: list[dict[str, Any]],
    ptr_by_clause: Mapping[str, Mapping[str, Any]],
    report_by_clause: Mapping[str, Sequence[Mapping[str, Any]]],
) -> None:
    """Report rows whose title belongs to a *different* PTR clause number.

    When a Report drops a clause and renumbers the rest (0596: PTR 2.1.8
    输入阻抗 missing, Report 2.1.8～2.1.12 = PTR 2.1.9～2.1.13) every row
    looks like a text difference.  Name the real cause instead.
    """

    titles_by_parent: defaultdict[str | None, dict[str, str]] = defaultdict(dict)
    for clause_id, node in ptr_by_clause.items():
        key = _clause_title_key(node)
        if len(key) >= 2:
            titles_by_parent[_clause_parent(clause_id)][key] = clause_id
    shifted: list[tuple[str, str]] = []
    for entry in ledger:
        if entry.get("decision") not in {MANUAL, MISMATCH}:
            continue
        clause_id = str(entry.get("clause_id") or "")
        ptr_node = ptr_by_clause.get(clause_id)
        reports = report_by_clause.get(clause_id) or []
        if not ptr_node or len(reports) != 1:
            continue
        report_key = _clause_title_key(reports[0])
        ptr_key = _clause_title_key(ptr_node)
        if not report_key or report_key == ptr_key or ptr_key.startswith(report_key) or report_key.startswith(ptr_key):
            continue
        siblings = titles_by_parent.get(_clause_parent(clause_id), {})
        other = siblings.get(report_key) or next(
            (cid for key, cid in siblings.items() if len(report_key) >= 4 and (key.startswith(report_key) or report_key.startswith(key))),
            None,
        )
        if not other or other == clause_id:
            continue
        shifted.append((clause_id, other))
        entry["decision"] = MISMATCH
        entry["reason_code"] = "CLAUSE_NUMBER_SHIFTED"
        entry["clause_shift"] = {
            "report_clause": clause_id,
            "report_title": str(reports[0].get("title") or "").strip(),
            "ptr_title": str(ptr_node.get("title") or "").strip(),
            "ptr_clause_with_report_title": other,
            "message": (
                f"报告 {clause_id} 的标题是“{str(reports[0].get('title') or '').strip()}”，"
                f"对应的是 PTR {other}；PTR {clause_id} 是“{str(ptr_node.get('title') or '').strip()}”。"
                "报告条款号与 PTR 错位。"
            ),
        }
    _summarise_shift_runs(ledger, shifted, ptr_by_clause, report_by_clause)


def _summarise_shift_runs(
    ledger: list[dict[str, Any]],
    shifted: Sequence[tuple[str, str]],
    ptr_by_clause: Mapping[str, Mapping[str, Any]],
    report_by_clause: Mapping[str, Sequence[Mapping[str, Any]]],
) -> None:
    """Group consecutive shifted clauses into one sentence for the reviewer."""

    def split(clause: str) -> tuple[str, int] | None:
        match = re.fullmatch(r"(.+)\.(\d+)", clause)
        return (match.group(1), int(match.group(2))) if match else None

    runs: list[list[tuple[str, str]]] = []
    for report_clause, ptr_clause in sorted(shifted, key=lambda pair: _clause_sort_key(pair[0])):
        a, b = split(report_clause), split(ptr_clause)
        if not a or not b or a[0] != b[0]:
            runs.append([(report_clause, ptr_clause)])
            continue
        offset = b[1] - a[1]
        last = runs[-1][-1] if runs else None
        if last:
            la, lb = split(last[0]), split(last[1])
            if la and lb and la[0] == a[0] and lb[1] - la[1] == offset and a[1] == la[1] + 1:
                runs[-1].append((report_clause, ptr_clause))
                continue
        runs.append([(report_clause, ptr_clause)])
    report_titles = {
        _clause_title_key(node) for nodes in report_by_clause.values() for node in nodes
    }
    by_clause = {str(entry.get("clause_id")): entry for entry in ledger if entry.get("clause_shift")}
    for run in runs:
        first_report, first_ptr = run[0]
        last_report, last_ptr = run[-1]
        a, b = split(first_report), split(first_ptr)
        missing: list[str] = []
        if a and b and b[1] > a[1]:
            for number in range(a[1], b[1]):
                candidate = f"{a[0]}.{number}"
                node = ptr_by_clause.get(candidate)
                if node and _clause_title_key(node) not in report_titles:
                    missing.append(f"{candidate} {str(node.get('title') or '').strip()}")
        span_report = first_report if len(run) == 1 else f"{first_report}～{last_report}"
        span_ptr = first_ptr if len(run) == 1 else f"{first_ptr}～{last_ptr}"
        message = f"报告 {span_report} 实际对应 PTR {span_ptr}（条款号与 PTR 错位）"
        if missing:
            message += f"；报告中缺少 PTR {'、'.join(missing)}"
        message += "。"
        run_info = {
            "report_clauses": [pair[0] for pair in run],
            "ptr_clauses": [pair[1] for pair in run],
            "missing_ptr_clauses": missing,
            "message": message,
        }
        # A PTR clause whose content moved to another Report number is not
        # missing from the Report; it is part of the same shift.
        moved = {pair[1]: pair[0] for pair in run}
        for entry in ledger:
            clause = str(entry.get("clause_id") or "")
            if clause in moved and entry.get("reason_code") == "REPORT_CLAUSE_MISSING":
                entry["reason_code"] = "CLAUSE_NUMBER_SHIFTED"
                entry["clause_shift"] = {
                    "report_clause": moved[clause],
                    "ptr_clause_with_report_title": clause,
                    "message": f"PTR {clause} 的内容在报告 {moved[clause]} 中（报告条款号与 PTR 错位）。",
                    "run": run_info,
                }
        for report_clause, _ in run:
            entry = by_clause.get(report_clause)
            if entry:
                entry["clause_shift"]["run"] = run_info


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
    ptr_reference_cycles = _reference_cycle_candidates(ptr["nodes"])
    report_reference_cycles = _reference_cycle_candidates(report["nodes"])
    ptr_cycle_labels = {label for cycle in ptr_reference_cycles for label in cycle.get("labels", [])}
    report_cycle_labels = {label for cycle in report_reference_cycles for label in cycle.get("labels", [])}
    ptr_by_clause = {str(node["clause_id"]): node for node in ptr["nodes"]}
    report_by_clause = report["nodes_by_clause"]
    children: defaultdict[str, set[str]] = defaultdict(set)
    for node in ptr["nodes"]:
        parent = node.get("parent_clause_id")
        if parent:
            children[str(parent)].add(str(node["clause_id"]))
    equivalence_context = {
        "ptr_headers": ptr_running_header_lines(ptr_path),
        "ptr_text_by_clause": {
            clause: _text_for_comparison(node.get("text"), role="ptr") for clause, node in ptr_by_clause.items()
        },
    }
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
        comparison = _compare_text(
            ptr_node,
            report_node,
            has_children=bool(children.get(clause_id)),
            context=equivalence_context,
        )
        decision = comparison["decision"]
        if comparison["table_comparison"]["status"] == MANUAL and decision == MATCHED:
            decision = MANUAL
            reason_code = str(comparison["table_comparison"].get("reason_code") or "TABLE_CONTENT_NOT_EXTRACTED")
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
    # A declared scope item can be absent from both extracted sides.  Keep an
    # explicit, evidence-free ledger row for that hole instead of letting the
    # summary count the already-observed rows as if the whole ScopeSet were
    # covered.  This row is deliberately ineligible for automatic success.
    declared_scope_ids = _scope_clause_ids(scope)
    accounted_scope_ids = {
        scope_id
        for scope_id in declared_scope_ids
        if any(
            entry.get("decision") != EXCLUDED and _entry_covers_scope(entry, scope_id)
            for entry in ledger
        )
    }
    missing_scope_ids = [scope_id for scope_id in declared_scope_ids if scope_id not in accounted_scope_ids]
    for scope_id in missing_scope_ids:
        if ptr["diagnostics"].get("status") != "parsed" or report["diagnostics"].get("status") != "parsed":
            decision, reason = MANUAL, "SCOPE_ITEM_UNACCOUNTED"
        else:
            decision, reason = MISMATCH, "SCOPE_ITEM_UNACCOUNTED"
        ledger.append(
            _ledger_entry(
                ptr=None,
                reports=[],
                clause_id=scope_id,
                scope_decision="eligible",
                scope_reason="SCOPE_INCLUDED",
                comparison=None,
                decision=decision,
                reason_code=reason,
            )
        )
    _flag_clause_number_shifts(ledger, ptr_by_clause, report_by_clause)
    # A table-to-table reference cycle has no safe deterministic expansion
    # order.  Keep affected rows manual even when the surrounding text and
    # numeric facts happen to look identical.
    for entry in ledger:
        if entry.get("decision") == EXCLUDED:
            continue
        ptr_node = next((node for node in ptr["nodes"] if str(node.get("node_id")) == str(entry.get("ptr_node_id"))), None)
        report_nodes = [
            node
            for node in report["nodes"]
            if str(node.get("node_id")) in {str(value) for value in entry.get("report_item_ids", [])}
        ]
        ptr_labels = set((ptr_node or {}).get("references", {}).get("tables", []) or [])
        report_labels = {
            label
            for node in report_nodes
            for label in (node.get("references", {}).get("tables", []) or [])
        }
        if ptr_labels & ptr_cycle_labels or report_labels & report_cycle_labels:
            entry["decision"] = MANUAL
            entry["reason_code"] = "REFERENCE_CYCLE_CANDIDATE"
            comparison = entry.get("reference_comparison")
            if isinstance(comparison, dict):
                comparison["cycle_status"] = MANUAL
                comparison["cycle_reason_code"] = "REFERENCE_CYCLE_CANDIDATE"
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
    accounted_count = sum(
        1
        for entry in ledger
        if entry["decision"] != EXCLUDED and (entry.get("ptr_node_id") or entry.get("report_item_ids"))
    )
    unaccounted_ledger_ids = [
        str(entry["id"])
        for entry in ledger
        if entry["decision"] != EXCLUDED
        and not (entry.get("ptr_node_id") or entry.get("report_item_ids"))
    ]
    llm_queue: list[dict[str, Any]] = []
    if comparison_mode == "rules+llm":
        ptr_by_id = {str(node["node_id"]): node for node in ptr["nodes"]}
        report_by_id = {str(node["node_id"]): node for node in report["nodes"]}
        text_review_reasons = {"TEXT_MISMATCH", "TEXT_REVIEW_REQUIRED", "LLM_REVIEW_PENDING"}
        for entry in ledger:
            if entry["decision"] == EXCLUDED:
                continue
            if entry.get("reason_code") not in text_review_reasons:
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
            "accounted": accounted_count,
            "conserved": (
                len(ledger) == len({entry["id"] for entry in ledger})
                and eligible_count == accounted_count
                and not missing_scope_ids
            ),
        },
        "coverage_audit": {
            "status": "conserved" if not missing_scope_ids and eligible_count == accounted_count else "incomplete",
            "declared_scope_ids": declared_scope_ids,
            "accounted_scope_ids": sorted(accounted_scope_ids, key=_clause_sort_key),
            "missing_scope_items": missing_scope_ids,
            "unaccounted_ledger_ids": unaccounted_ledger_ids,
        },
        "reference_audit": {
            "ptr_cycles": ptr_reference_cycles,
            "report_cycles": report_reference_cycles,
            "status": "manual" if ptr_reference_cycles or report_reference_cycles else "resolved",
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
    audit_log: list[dict[str, Any]] = []
    for task in list((updated.get("llm") or {}).get("review_queue") or []):
        payload = review(task)
        if not isinstance(payload, Mapping):
            raise PtrCompareError("LLM provider 返回值必须是 JSON 对象")
        updated = apply_llm_review(updated, str(task.get("clause_id")), payload)
        ledger_entry = next(
            item for item in updated.get("coverage_ledger", [])
            if str(item.get("clause_id")) == str(task.get("clause_id"))
        )
        validated_review = dict(ledger_entry.get("llm_review") or {})
        responses.append(
            {
                "clause_id": task.get("clause_id"),
                "input_sha256": task.get("input_sha256"),
                "validated": True,
            }
        )
        audit_log.append(
            {
                "clause_id": task.get("clause_id"),
                "provider_id": provider_id,
                "model_id": model_id,
                "prompt_version": task.get("prompt_version"),
                "input_sha256": task.get("input_sha256"),
                "output": validated_review,
                "validated": bool(validated_review.get("validated")),
                "action": "cleared_text_manual"
                if ledger_entry.get("reason_code") == "LLM_VERIFIED_TEXT_EQUIVALENCE"
                else "retained_manual_or_hard_difference",
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
            "audit_log": audit_log,
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
