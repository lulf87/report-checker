"""Report 首页写法、日期、版本、页眉与编号一致性规则（REPORT-R12～R16）。

这些规则只读取 Report 本身，不依赖 OCR、PTR 或 Record。规则口径来自报告审核
问题统计表及 2026-10-20 用户确认：

* R12 检验项目写法：区间用“～”，分隔用“、”；连续条款 ≥3 条写区间、<3 条逐条列举；
  除外条款按“生物相容性、可用性、电磁兼容性”顺序；不得写“全性能/全项目/2.性能”；
  标准编号年代号用连字符。
* R13 检验依据与检验结论：使用“产品技术要求”全称；变更文件写“及变更对比表”；
  标准名称后加“标准”；委托方与生产单位不一致时写“提供”；有除外/仅检时结论写
  “被检样品所检项目符合”；依据与结论引用的文件一致；公司名称与委托方一致（含全/半角括号）。
* R14 日期与封面版本：到样日期、检验日期、签发日期写作“YYYY年M月D日”（不补零）；
  日期先后合理；已签发报告的封面版本号与签发日期对应的生效版本一致。
* R15 页眉与编号：每页页眉报告编号与封面一致；样品编号与报告编号对应；总页数与实际页数一致。
* R16 首页其他字段：职务为“授权签字人”；单位名称括号全/半角一致；送样/抽样与抽样字段一致。

每条规则输出对象级 ``details.checks``，供 Coverage Ledger 与工作台逐条展示。
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Iterable, Mapping, Sequence

try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover - older PyMuPDF installs
    import fitz


# ---------------------------------------------------------------------------
# 可调整的口径常量
# ---------------------------------------------------------------------------

#: 封面“版本/修订”与生效日期。按签发日期取不晚于签发日期的最新版本。
COVER_VERSIONS: tuple[tuple[str, date], ...] = (
    ("5/2", date(2025, 5, 1)),
    ("5/3", date(2026, 4, 7)),
)
COVER_FORM_CODE = "CMTC-QR7.8-01-06"

#: 除外条款的固定先后顺序（只约束这三项之间的相对顺序）。
EXCLUSION_ORDER: tuple[str, ...] = ("生物相容性", "可用性", "电磁兼容性")
SIGNATORY_TITLE = "授权签字人"
FORBIDDEN_SCOPE_PHRASES: tuple[str, ...] = ("全性能", "全项目", "全部项目")
#: 封面与注意事项页不编打印页码。
UNNUMBERED_LEADING_PAGES = 2

PAGE3_FIELD_NAMES: tuple[str, ...] = (
    "样品名称", "样品编号", "商标", "型号规格", "委托方", "检验类别", "委托方地址",
    "产品编号/批号", "生产单位", "抽样单编号", "受检单位", "生产日期", "抽样单位",
    "样品数量", "抽样地点", "抽样基数", "抽样日期", "检验地点", "到样日期",
    "检验日期", "检验项目", "检验依据", "检验结论",
)
SAMPLING_FIELDS: tuple[str, ...] = ("抽样单编号", "抽样单位", "抽样地点", "抽样基数", "抽样日期")

RULE_TITLES: dict[str, str] = {
    "REPORT-R12": "首页检验项目写法",
    "REPORT-R13": "首页检验依据与检验结论写法",
    "REPORT-R14": "首页日期格式、日期先后与封面版本",
    "REPORT-R15": "页眉报告编号、样品编号与总页数",
    "REPORT-R16": "首页职务、单位名称与抽样字段",
}
RULE_TEXT: dict[str, str] = {
    "REPORT-R12": "区间用“～”、分隔用“、”；连续条款≥3条写区间、<3条逐条列举；除外条款按生物相容性、可用性、电磁兼容性顺序；不得写全性能/全项目/2.性能；标准编号年代号用连字符",
    "REPORT-R13": "使用“产品技术要求”全称；变更文件写“及变更对比表”；标准名称后加“标准”；委托方与生产单位不一致时写“提供”；有除外或仅检时结论写“被检样品所检项目符合”；依据与结论引用一致；公司名称与委托方一致",
    "REPORT-R14": "到样日期、检验日期、签发日期写作YYYY年M月D日且不补零；抽样≤到样≤检验开始≤检验结束≤签发；已签发报告的封面版本与签发日期对应的生效版本一致",
    "REPORT-R15": "每页页眉报告编号与封面一致；页眉和首页样品编号与报告编号对应；打印总页数等于PDF页数减去封面和注意事项页",
    "REPORT-R16": "职务为“授权签字人”；同一单位名称括号全/半角一致；送样时抽样字段为“/”，抽样时抽样字段已填写",
}

STATUS_RANK = {"not_applicable": -1, "pass": 0, "warning": 1, "manual": 2, "error": 3}

_CLAUSE = r"\d+(?:\.\d+)*"
_LETTER = r"[a-z]\)"
_GOOD_TILDES = "～〜"
_BAD_RANGE_SYMBOLS = "-－—–~"
_STANDARD_RE = re.compile(
    r"(?:GB|YY|IEC|ISO|EN|WS)(?:\s*/\s*[TZ])?\s*\d+(?:\.\d+)*(?:\s*[-－—–]\s*\d{2,4})?"
)
_STANDARD_MISSING_HYPHEN_RE = re.compile(
    r"(?<![A-Za-z])(?:GB|YY)(?:\s*/\s*[TZ])?\s*\d+(?:\.\d+)*\s+(?:19|20)\d{2}(?!\d)"
)
_REPORT_NUMBER_RE = re.compile(r"QW\s*(\d{4})\s*第\s*(\d+)\s*号")
_CN_DATE_RE = re.compile(r"^(\d{4})年(\d{1,2})月(\d{1,2})日$")


# ---------------------------------------------------------------------------
# 文本工具
# ---------------------------------------------------------------------------

def compact(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", "", str(value))


def _join_lines(raw: str) -> str:
    """Join wrapped PDF lines: Chinese wraps join directly, Latin words keep a space."""

    lines = [line.strip() for line in str(raw or "").splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return ""
    text = lines[0]
    for line in lines[1:]:
        if re.search(r"[A-Za-z0-9.,]$", text) and re.match(r"[A-Za-z]", line):
            text += " " + line
        else:
            text += line
    return re.sub(r"[ \t　]+", " ", text).strip()


def _bracket_normalized(value: str) -> str:
    return value.replace("(", "（").replace(")", "）")


def _name_relation(left: str, right: str) -> str:
    """Return ``same``, ``bracket_width`` (only bracket width differs) or ``different``."""

    a, b = compact(left), compact(right)
    if a == b:
        return "same"
    if _bracket_normalized(a) == _bracket_normalized(b):
        return "bracket_width"
    return "different"


def _has_mixed_brackets(value: str) -> bool:
    text = compact(value)
    return bool(re.search(r"[()]", text)) and bool(re.search(r"[（）]", text))


def _rect(value: Any) -> list[float] | None:
    if value is None:
        return None
    try:
        rect = fitz.Rect(value)
    except (TypeError, ValueError):
        return None
    if rect.is_empty or rect.is_infinite:
        return None
    return [round(float(rect.x0), 3), round(float(rect.y0), 3), round(float(rect.x1), 3), round(float(rect.y1), 3)]


def _location(page_number: int, rect: Any, source: str) -> dict[str, Any]:
    return {"document_role": "report", "pdf_page": page_number, "rect": _rect(rect), "source": source}


def _check(
    check_id: str,
    field: str,
    status: str,
    reason_code: str,
    message: str,
    *,
    observed: Any = None,
    expected: Any = None,
    location: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "check_id": check_id,
        "field": field,
        "status": status,
        "reason_code": reason_code,
        "message": message,
        "observed": observed,
        "expected": expected,
        "location": dict(location) if location else None,
    }


# ---------------------------------------------------------------------------
# 版面提取
# ---------------------------------------------------------------------------

def _clip_text(page: fitz.Page, bbox: Any) -> str:
    rect = fitz.Rect(bbox)
    inner = fitz.Rect(rect.x0 + 0.5, rect.y0 + 0.5, rect.x1 - 0.5, rect.y1 - 0.5)
    return _join_lines(page.get_text("text", clip=inner, sort=True))


def extract_page3_fields(page: fitz.Page) -> dict[str, dict[str, Any]]:
    """Return page-3 field values read from cell geometry.

    ``Table.extract()`` interleaves punctuation from a different font onto a
    separate line (for example ``2 2 2 5 ... \n. 、 .``), so values are read
    with ``get_text(clip=cell)`` instead.
    """

    fields: dict[str, dict[str, Any]] = {}
    tables = page.find_tables().tables
    if not tables:
        return fields
    table = tables[0]
    for row in table.rows:
        cells = [cell for cell in row.cells]
        for index, cell in enumerate(cells):
            if cell is None:
                continue
            label = compact(_clip_text(page, cell)).replace("／", "/")
            if label.startswith("送样") or label.startswith("抽样（") or label.startswith("抽样("):
                key = "送样标记" if label.startswith("送样") else "抽样标记"
                fields.setdefault(key, {"value": label, "rect": _rect(cell), "pdf_page": page.number + 1})
                continue
            if label not in PAGE3_FIELD_NAMES or label in fields:
                continue
            value_cell = next((item for item in cells[index + 1 :] if item is not None), None)
            if value_cell is None:
                continue
            value = _clip_text(page, value_cell)
            if label == "检验结论":
                # 盖章提示和签发日期印在检验结论单元格内，不属于结论文字。
                value = re.split(r"[（(]\s*检验报告专用章|签发日期", value)[0].strip()
            fields[label] = {
                "value": value,
                "rect": _rect(value_cell),
                "label_rect": _rect(cell),
                "pdf_page": page.number + 1,
            }
    return fields


def _find_line(page: fitz.Page, pattern: re.Pattern[str]) -> tuple[re.Match[str] | None, list[float] | None]:
    for block in page.get_text("dict").get("blocks", []):
        for line in block.get("lines", []):
            text = "".join(span.get("text", "") for span in line.get("spans", []))
            match = pattern.search(text)
            if match:
                return match, _rect(line.get("bbox"))
    return None, None


def _header_text(page: fitz.Page) -> tuple[str, list[list[float]]]:
    limit = page.rect.y0 + page.rect.height * 0.25
    texts: list[str] = []
    rects: list[list[float]] = []
    for block in page.get_text("dict").get("blocks", []):
        for line in block.get("lines", []):
            bbox = fitz.Rect(line.get("bbox"))
            if bbox.y0 > limit:
                continue
            text = "".join(span.get("text", "") for span in line.get("spans", []))
            if "报告编号" in text or "样品编号" in text or re.search(r"共\s*\d+\s*页", text):
                texts.append(text)
                rect = _rect(bbox)
                if rect:
                    rects.append(rect)
    return " ".join(texts), rects


def extract_front_matter(document: fitz.Document) -> dict[str, Any]:
    cover = document[0]
    cover_text = cover.get_text("text")
    version_match, version_rect = _find_line(
        cover, re.compile(r"版本/修订[:：]\s*(\d+/\d+)\s*生效日期[:：]\s*(\d{4}-\d{1,2}-\d{1,2})\s*(CMTC-QR[\w.\-]+)?")
    )
    number_match, number_rect = _find_line(cover, re.compile(r"报告编号[:：]\s*(.+?号)"))
    cover_client = ""
    lines = [line.strip() for line in cover_text.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        if compact(line) == "委托方" and index + 1 < len(lines):
            parts = []
            for following in lines[index + 1 :]:
                if compact(following).startswith(("样品名称", "型号规格", "检验类别")):
                    break
                parts.append(following)
            cover_client = _join_lines("\n".join(parts))
            break
    page3 = document[2] if document.page_count >= 3 else None
    fields = extract_page3_fields(page3) if page3 is not None else {}
    title_match, title_rect = (None, None)
    issue_match, issue_rect = (None, None)
    if page3 is not None:
        title_match, title_rect = _find_line(page3, re.compile(r"职\s*务\s*[:：]\s*(.*)$"))
        issue_match, issue_rect = _find_line(page3, re.compile(r"签发日期\s*(.*)$"))
    headers = []
    for page in document:
        if page.number < 2:
            continue
        text, rects = _header_text(page)
        number = re.search(r"报告编号[:：]\s*(.+?号)", text)
        sample = re.search(r"样品编号[:：]\s*([A-Za-z0-9\-－]+)", text)
        total = re.search(r"共\s*(\d+)\s*页\s*第\s*(\d+)\s*页", text)
        headers.append(
            {
                "pdf_page": page.number + 1,
                "report_number": number.group(1) if number else None,
                "sample_number": sample.group(1) if sample else None,
                "printed_total": int(total.group(1)) if total else None,
                "printed_page": int(total.group(2)) if total else None,
                "rects": rects,
            }
        )
    return {
        "page_count": document.page_count,
        "cover": {
            "version": version_match.group(1) if version_match else None,
            "effective_date": version_match.group(2) if version_match else None,
            "form_code": version_match.group(3) if version_match else None,
            "version_rect": version_rect,
            "report_number": number_match.group(1) if number_match else None,
            "report_number_rect": number_rect,
            "client": cover_client,
        },
        "page3_fields": fields,
        "signatory_title": title_match.group(1).strip() if title_match else None,
        "signatory_title_rect": title_rect,
        "issue_date": issue_match.group(1).strip() if issue_match else None,
        "issue_date_rect": issue_rect,
        "issue_date_found": issue_match is not None,
        "headers": headers,
    }


# ---------------------------------------------------------------------------
# R12 检验项目
# ---------------------------------------------------------------------------

def _mask_standards(text: str) -> str:
    return _STANDARD_RE.sub(lambda match: "Ⓢ" * len(match.group(0)), text)


def _split_top_level(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Split the clause list on 、 outside brackets.

    Returns tokens ``{"text", "qualified"}`` and the bracket group contents.
    A trailing group starting with “除” is the global exclusion and does not
    qualify the last token.
    """

    stripped = text.strip()
    trailing_global = None
    match = re.search(r"[（(]\s*(除[^（）()]*(?:[（(][^（）()]*[)）][^（）()]*)*)[)）]\s*$", stripped)
    if match:
        trailing_global = match.group(1)
        stripped = stripped[: match.start()].rstrip()
    tokens: list[dict[str, Any]] = []
    groups: list[str] = [trailing_global] if trailing_global else []
    depth = 0
    buffer: list[str] = []
    group_buffer: list[str] = []
    qualified = False
    for char in stripped:
        if char in "（(":
            depth += 1
            qualified = True
            if depth == 1:
                group_buffer = []
                continue
        if char in "）)":
            if depth > 0:
                depth -= 1
                if depth == 0:
                    groups.append("".join(group_buffer))
                    continue
            else:
                buffer.append(char)
                continue
        if depth > 0:
            group_buffer.append(char)
            continue
        if char in "、，,；;":
            tokens.append({"text": "".join(buffer).strip(), "qualified": qualified})
            buffer = []
            qualified = False
            continue
        buffer.append(char)
    tokens.append({"text": "".join(buffer).strip(), "qualified": qualified})
    return [token for token in tokens if token["text"]], groups


def _clause_parts(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


def _parse_unit(token: str, previous_clause: tuple[int, ...] | None) -> dict[str, Any]:
    text = compact(token).replace("〜", "～")
    for symbol in _BAD_RANGE_SYMBOLS:
        text = text.replace(symbol, "～")
    if match := re.fullmatch(rf"({_CLAUSE})", text):
        return {"kind": "clause", "clause": _clause_parts(match.group(1))}
    if match := re.fullmatch(rf"({_CLAUSE})({_LETTER})", text):
        return {"kind": "letter", "clause": _clause_parts(match.group(1)), "letter": match.group(2)[0]}
    if match := re.fullmatch(rf"({_CLAUSE})～({_CLAUSE})", text):
        return {"kind": "range", "start": _clause_parts(match.group(1)), "end": _clause_parts(match.group(2))}
    if match := re.fullmatch(rf"({_CLAUSE})({_LETTER})～({_LETTER})", text):
        return {
            "kind": "letter_range",
            "clause": _clause_parts(match.group(1)),
            "start": match.group(2)[0],
            "end": match.group(3)[0],
        }
    if previous_clause is not None and (match := re.fullmatch(rf"({_LETTER})", text)):
        return {"kind": "letter", "clause": previous_clause, "letter": match.group(1)[0]}
    if previous_clause is not None and (match := re.fullmatch(rf"({_LETTER})～({_LETTER})", text)):
        return {"kind": "letter_range", "clause": previous_clause, "start": match.group(1)[0], "end": match.group(2)[0]}
    return {"kind": "other"}


def _clause_text(parts: Sequence[int]) -> str:
    return ".".join(str(part) for part in parts)


def _is_next_clause(previous: Sequence[int], current: Sequence[int]) -> bool:
    return (
        len(previous) == len(current)
        and tuple(previous[:-1]) == tuple(current[:-1])
        and current[-1] == previous[-1] + 1
    )


def analyze_inspection_items(text: str) -> list[dict[str, Any]]:
    """Return R12 issues for the page-3 “检验项目” value."""

    issues: list[dict[str, Any]] = []
    raw = _join_lines(text)
    value = compact(raw)
    if not value:
        return [{"reason_code": "inspection_items_missing", "status": "error", "message": "检验项目为空"}]

    for phrase in FORBIDDEN_SCOPE_PHRASES:
        if phrase in value:
            issues.append({
                "reason_code": "inspection_items_forbidden_phrase",
                "status": "error",
                "message": f"检验项目不应写“{phrase}”，应写具体条款区间（如“2.1～2.N”）",
                "observed": phrase,
            })
    if match := re.search(r"(?<![\d.])\d\.性能", value):
        issues.append({
            "reason_code": "inspection_items_forbidden_phrase",
            "status": "error",
            "message": f"检验项目不应写“{match.group(0)}”，应写具体条款区间（如“2.1～2.N”）",
            "observed": match.group(0),
        })

    for match in _STANDARD_MISSING_HYPHEN_RE.finditer(raw):
        observed = match.group(0)
        expected = re.sub(r"\s+((?:19|20)\d{2})$", r"-\1", observed)
        issues.append({
            "reason_code": "standard_number_year_hyphen_missing",
            "status": "error",
            "message": f"标准编号“{observed}”缺少年代号连字符，应为“{expected}”",
            "observed": observed,
            "expected": expected,
        })

    masked = _mask_standards(value)
    for match in re.finditer(r"[，,；;]", masked):
        context = value[max(0, match.start() - 8) : match.end() + 8]
        issues.append({
            "reason_code": "inspection_items_separator",
            "status": "error",
            "message": f"检验项目中的“{match.group(0)}”应改为“、”：…{context}…",
            "observed": match.group(0),
            "expected": "、",
        })
    bad_range = re.compile(
        rf"({_CLAUSE}(?:{_LETTER})?|{_LETTER})([{re.escape(_BAD_RANGE_SYMBOLS)}]|至)({_CLAUSE}|{_LETTER})"
    )
    for match in bad_range.finditer(masked):
        if "." not in match.group(1) and not match.group(1).endswith(")"):
            continue
        observed = value[match.start() : match.end()]
        expected = f"{match.group(1)}～{match.group(3)}"
        issues.append({
            "reason_code": "inspection_items_range_symbol",
            "status": "error",
            "message": f"条款区间“{observed}”应使用“～”，写作“{expected}”",
            "observed": observed,
            "expected": expected,
        })

    tokens, groups = _split_top_level(raw)
    for group in groups:
        group_text = compact(group)
        if not group_text.startswith("除"):
            continue
        present = [(group_text.index(item), item) for item in EXCLUSION_ORDER if item in group_text]
        actual = [item for _position, item in sorted(present)]
        canonical = [item for item in EXCLUSION_ORDER if item in actual]
        if actual != canonical:
            issues.append({
                "reason_code": "exclusion_order",
                "status": "error",
                "message": f"除外条款顺序应为“除{'、'.join(canonical)}”，报告写作“除{'、'.join(actual)}”",
                "observed": "、".join(actual),
                "expected": "、".join(canonical),
            })

    units: list[dict[str, Any]] = []
    previous_clause: tuple[int, ...] | None = None
    for token in tokens:
        unit = _parse_unit(token["text"], previous_clause)
        unit["text"] = compact(token["text"])
        unit["qualified"] = token["qualified"]
        if unit["kind"] in {"clause", "letter", "letter_range"}:
            previous_clause = unit["clause"]
        elif unit["kind"] == "range":
            previous_clause = unit["end"]
        units.append(unit)

    for unit in units:
        if unit["kind"] == "range":
            start, end = unit["start"], unit["end"]
            if _is_next_clause(start, end):
                expected = f"{_clause_text(start)}、{_clause_text(end)}"
                issues.append({
                    "reason_code": "two_clause_range",
                    "status": "error",
                    "message": f"“{unit['text']}”只含2条连续条款，应逐条列举为“{expected}”",
                    "observed": unit["text"],
                    "expected": expected,
                })
            elif len(start) == len(end) and start[:-1] == end[:-1] and end[-1] <= start[-1]:
                issues.append({
                    "reason_code": "range_not_increasing",
                    "status": "error",
                    "message": f"条款区间“{unit['text']}”起止顺序不正确",
                    "observed": unit["text"],
                })
        if unit["kind"] == "letter_range" and ord(unit["end"]) == ord(unit["start"]) + 1:
            expected = f"{_clause_text(unit['clause'])}{unit['start']})、{unit['end']})"
            issues.append({
                "reason_code": "two_clause_range",
                "status": "error",
                "message": f"“{unit['text']}”只含2项，应逐项列举为“{unit['start']})、{unit['end']})”",
                "observed": unit["text"],
                "expected": expected,
            })

    def flush(run: list[dict[str, Any]], kind: str) -> None:
        if len(run) < 3:
            return
        texts = [item["text"] for item in run]
        if kind == "clause":
            expected = f"{_clause_text(run[0]['clause'])}～{_clause_text(run[-1]['clause'])}"
        else:
            expected = f"{_clause_text(run[0]['clause'])}{run[0]['letter']})～{run[-1]['letter']})"
        issues.append({
            "reason_code": "consecutive_clauses_not_ranged",
            "status": "error",
            "message": f"“{'、'.join(texts)}”为{len(run)}条连续条款，应写为区间“{expected}”",
            "observed": "、".join(texts),
            "expected": expected,
        })

    run: list[dict[str, Any]] = []
    run_kind = ""
    for unit in units:
        kind = unit["kind"]
        eligible = kind in {"clause", "letter"} and not unit["qualified"]
        if eligible and run and run_kind == kind:
            previous = run[-1]
            if kind == "clause" and _is_next_clause(previous["clause"], unit["clause"]):
                run.append(unit)
                continue
            if (
                kind == "letter"
                and previous["clause"] == unit["clause"]
                and ord(unit["letter"]) == ord(previous["letter"]) + 1
            ):
                run.append(unit)
                continue
        flush(run, run_kind)
        run, run_kind = ([unit], kind) if eligible else ([], "")
    flush(run, run_kind)
    return issues


def scope_has_exclusion_or_partial(text: str) -> bool:
    value = compact(text)
    return bool(re.search(r"[（(]除", value)) or "仅检" in value


# ---------------------------------------------------------------------------
# R13 检验依据与检验结论
# ---------------------------------------------------------------------------

_PTR_PHRASE_RE = re.compile(
    r"(?P<company>[^《》、]*?)(?P<provide>提供)?的《(?P<name>[^》]+)》(?P<doc>产品技术要求|技术要求)(?P<suffix>[^、。；;]*)"
)


def _parse_ptr_phrase(value: str) -> dict[str, Any] | None:
    match = _PTR_PHRASE_RE.search(value)
    if not match:
        return None
    company = match.group("company")
    company = re.sub(r"^(?:被检样品(?:所检项目)?符合)", "", company)
    return {
        "company": company,
        "provide": bool(match.group("provide")),
        "name": match.group("name"),
        "doc": match.group("doc"),
        "suffix": match.group("suffix"),
        "text": match.group(0),
    }


def _standard_citation_issues(value: str, field: str) -> list[dict[str, Any]]:
    issues = []
    for match in re.finditer(r"((?:GB|YY|IEC|ISO)(?:/[TZ])?\d+(?:\.\d+)*-\d{4})(《[^》]+》)(?!标准)", value):
        issues.append({
            "reason_code": "standard_title_without_suffix",
            "status": "error",
            "message": f"{field}中“{match.group(1)}{match.group(2)}”后应加“标准”",
            "observed": match.group(0),
            "expected": f"{match.group(0)}标准",
        })
    for match in re.finditer(r"((?:GB|YY|IEC|ISO)(?:/[TZ])?\d+(?:\.\d+)*-\d{4})的要求", value):
        issues.append({
            "reason_code": "standard_requirement_without_suffix",
            "status": "error",
            "message": f"{field}中“{match.group(0)}”应写为“{match.group(1)}标准的要求”",
            "observed": match.group(0),
            "expected": f"{match.group(1)}标准的要求",
        })
    return issues


def analyze_basis_and_conclusion(
    basis: str,
    conclusion: str,
    *,
    client: str,
    producer: str,
    inspection_items: str,
) -> list[dict[str, Any]]:
    """Return R13 issues; each issue carries the field (检验依据/检验结论) it belongs to."""

    issues: list[dict[str, Any]] = []
    basis_value = compact(basis)
    conclusion_value = compact(conclusion)
    relation = _name_relation(client, producer)
    provide_expected = relation == "different"

    parsed: dict[str, dict[str, Any] | None] = {}
    suffix_flagged: set[str] = set()
    for field, value in (("检验依据", basis_value), ("检验结论", conclusion_value)):
        if not value:
            issues.append({"field": field, "reason_code": "field_missing", "status": "error", "message": f"{field}为空"})
            parsed[field] = None
            continue
        issues.extend({"field": field, **issue} for issue in _standard_citation_issues(value, field))
        # “技术要求变更对比表”这类写法由下面的变更文件名称检查统一报告，避免同一处重复计数。
        for match in re.finditer(r"(?<!产品)技术要求(?!变更)", value):
            context = value[max(0, match.start() - 6) : match.end() + 6]
            issues.append({
                "field": field,
                "reason_code": "product_technical_requirement_name",
                "status": "error",
                "message": f"{field}中“技术要求”应写全称“产品技术要求”：…{context}…",
                "observed": context,
            })
        if "变更单" in value:
            issues.append({
                "field": field,
                "reason_code": "change_document_name",
                "status": "error",
                "message": f"{field}中“变更单”应写为“及变更对比表”",
                "observed": "变更单",
                "expected": "及变更对比表",
            })
        phrase = _parse_ptr_phrase(value)
        parsed[field] = phrase
        if phrase is None:
            continue
        suffix = phrase["suffix"]
        if "变更单" in suffix:
            suffix_flagged.add(field)
        if "变更" in suffix and suffix != "及变更对比表" and "变更单" not in suffix:
            suffix_flagged.add(field)
            issues.append({
                "field": field,
                "reason_code": "change_document_name",
                "status": "error",
                "message": f"{field}中产品技术要求之后应写“及变更对比表”，报告写作“{phrase['doc']}{suffix}”",
                "observed": f"{phrase['doc']}{suffix}",
                "expected": "产品技术要求及变更对比表",
            })
        if phrase["provide"] != provide_expected:
            if provide_expected:
                message = f"委托方与生产单位不一致，{field}应写“{compact(client)}提供的《{phrase['name']}》…”"
            else:
                message = f"委托方与生产单位一致，{field}不应写“提供”"
            issues.append({
                "field": field,
                "reason_code": "provide_wording",
                "status": "error",
                "message": message,
                "observed": phrase["text"],
                "expected": "含“提供”" if provide_expected else "不含“提供”",
            })
        company_relation = _name_relation(phrase["company"], client)
        if company_relation == "bracket_width":
            issues.append({
                "field": field,
                "reason_code": "name_bracket_width_inconsistent",
                "status": "error",
                "message": f"{field}中的公司名称“{phrase['company']}”与委托方“{compact(client)}”括号全/半角不一致",
                "observed": phrase["company"],
                "expected": compact(client),
            })
        elif company_relation == "different" and phrase["company"]:
            issues.append({
                "field": field,
                "reason_code": "company_differs_from_client",
                "status": "error",
                "message": f"{field}中的公司名称“{phrase['company']}”与委托方“{compact(client)}”不一致",
                "observed": phrase["company"],
                "expected": compact(client),
            })
        elif not phrase["company"]:
            issues.append({
                "field": field,
                "reason_code": "company_not_located",
                "status": "manual",
                "message": f"{field}中未能定位产品技术要求所属公司名称",
            })

    if conclusion_value:
        partial = scope_has_exclusion_or_partial(inspection_items)
        if not conclusion_value.startswith("被检样品"):
            issues.append({
                "field": "检验结论",
                "reason_code": "conclusion_prefix",
                "status": "error",
                "message": "检验结论应以“被检样品…符合”开头",
                "observed": conclusion_value[:12],
            })
        elif partial and not conclusion_value.startswith("被检样品所检项目符合"):
            issues.append({
                "field": "检验结论",
                "reason_code": "conclusion_scope_wording",
                "status": "error",
                "message": "检验项目含除外条款或“仅检”，检验结论应写“被检样品所检项目符合…”",
                "observed": conclusion_value[:12],
                "expected": "被检样品所检项目符合",
            })

    basis_phrase, conclusion_phrase = parsed.get("检验依据"), parsed.get("检验结论")
    if basis_value and conclusion_value:
        if basis_phrase is None or conclusion_phrase is None:
            issues.append({
                "field": "检验依据" if basis_phrase is None else "检验结论",
                "reason_code": "ptr_phrase_not_parsed",
                "status": "manual",
                "message": "未能按“XX公司[提供]的《XX》产品技术要求”解析检验依据或检验结论，请人工核对",
            })
        else:
            keys = ("company", "provide", "name", "doc", "suffix")
            different = [key for key in keys if compact(basis_phrase[key]) != compact(conclusion_phrase[key])]
            # 只因变更文件名称写法不同而不一致时，上面已逐字段报告，不再重复计数。
            if different and not (set(different) <= {"suffix"} and suffix_flagged):
                issues.append({
                    "field": "检验结论",
                    "reason_code": "basis_conclusion_inconsistent",
                    "status": "error",
                    "message": f"检验结论引用的文件“{conclusion_phrase['text']}”与检验依据“{basis_phrase['text']}”不一致",
                    "observed": conclusion_phrase["text"],
                    "expected": basis_phrase["text"],
                })
    return issues


# ---------------------------------------------------------------------------
# R14 日期与封面版本
# ---------------------------------------------------------------------------

def parse_cn_date(value: str) -> tuple[date | None, dict[str, Any] | None]:
    text = compact(value)
    match = _CN_DATE_RE.fullmatch(text)
    if not match:
        return None, {
            "reason_code": "date_format",
            "status": "error",
            "message": f"日期“{text}”应写作“YYYY年M月D日”",
            "observed": text,
        }
    year, month_text, day_text = match.groups()
    try:
        parsed = date(int(year), int(month_text), int(day_text))
    except ValueError:
        return None, {"reason_code": "date_invalid", "status": "error", "message": f"日期“{text}”不是有效日期", "observed": text}
    if month_text.startswith("0") or day_text.startswith("0"):
        expected = f"{parsed.year}年{parsed.month}月{parsed.day}日"
        return parsed, {
            "reason_code": "date_zero_padded",
            "status": "error",
            "message": f"日期“{text}”不应补零，应写作“{expected}”",
            "observed": text,
            "expected": expected,
        }
    return parsed, None


def expected_cover_version(issue_date: date) -> tuple[str, date] | None:
    eligible = [item for item in COVER_VERSIONS if item[1] <= issue_date]
    return eligible[-1] if eligible else None


# ---------------------------------------------------------------------------
# 规则组装
# ---------------------------------------------------------------------------

def _rule_status(checks: Sequence[Mapping[str, Any]]) -> str:
    worst = max((STATUS_RANK.get(str(item["status"]), 2) for item in checks), default=0)
    return {-1: "pass", 0: "pass", 1: "warning", 2: "manual", 3: "error"}[worst]


def _finding(rule_id: str, checks: list[dict[str, Any]], pass_summary: str) -> dict[str, Any]:
    status = _rule_status(checks)
    problems = [item for item in checks if item["status"] in {"error", "manual", "warning"}]
    counts = {
        key: sum(item["status"] == key for item in checks)
        for key in ("pass", "warning", "manual", "error", "not_applicable")
    }
    if problems:
        errors = counts["error"]
        manual = counts["manual"]
        head = "；".join(item["message"] for item in problems[:2])
        more = f"等{len(problems)}处" if len(problems) > 2 else ""
        prefix = f"发现{errors}处问题" if errors else f"{manual}处需人工复核"
        if errors and manual:
            prefix += f"、{manual}处需人工复核"
        summary = f"{prefix}：{head}{more}"
    else:
        summary = pass_summary
    evidence: dict[int, list[list[float]]] = {}
    for item in problems:
        location = item.get("location") or {}
        page = location.get("pdf_page")
        rect = location.get("rect")
        if isinstance(page, int) and rect:
            evidence.setdefault(page, [])
            if rect not in evidence[page]:
                evidence[page].append(rect)
    return {
        "id": rule_id,
        "title": RULE_TITLES[rule_id],
        "status": status,
        "summary": summary,
        "details": {"checks": checks, "counts": counts, "rule": RULE_TEXT[rule_id]},
        "evidence": [
            {"pdf_page": page, "rects": rects, "source": rule_id.lower()}
            for page, rects in sorted(evidence.items())
        ],
    }


def _field_location(fields: Mapping[str, Any], name: str, page_rect: fitz.Rect, page_number: int = 3) -> dict[str, Any]:
    field = fields.get(name) or {}
    rect = field.get("rect") or field.get("label_rect") or [page_rect.x0, page_rect.y0, page_rect.x1, page_rect.y1]
    return _location(page_number, rect, f"report_page3_{name}")


def _r12(front: Mapping[str, Any], page_rect: fitz.Rect) -> dict[str, Any]:
    fields = front["page3_fields"]
    location = _field_location(fields, "检验项目", page_rect)
    if "检验项目" not in fields:
        checks = [_check("R12-locate", "检验项目", "manual", "field_not_located", "未在第三页定位到“检验项目”", location=location)]
        return _finding("REPORT-R12", checks, "")
    value = fields["检验项目"]["value"]
    issues = analyze_inspection_items(value)
    checks = [
        _check(
            f"R12-{index:02d}",
            "检验项目",
            issue["status"],
            issue["reason_code"],
            issue["message"],
            observed=issue.get("observed", value),
            expected=issue.get("expected"),
            location=location,
        )
        for index, issue in enumerate(issues, start=1)
    ]
    if not checks:
        checks.append(_check("R12-00", "检验项目", "pass", "inspection_items_wording_ok", "检验项目写法符合要求", observed=value, location=location))
    return _finding("REPORT-R12", checks, f"检验项目写法符合要求：{compact(value)}")


def _r13(front: Mapping[str, Any], page_rect: fitz.Rect) -> dict[str, Any]:
    fields = front["page3_fields"]
    basis = (fields.get("检验依据") or {}).get("value", "")
    conclusion = (fields.get("检验结论") or {}).get("value", "")
    client = (fields.get("委托方") or {}).get("value", "")
    producer = (fields.get("生产单位") or {}).get("value", "")
    items = (fields.get("检验项目") or {}).get("value", "")
    if "检验依据" not in fields and "检验结论" not in fields:
        checks = [_check("R13-locate", "检验依据", "manual", "field_not_located", "未在第三页定位到检验依据和检验结论", location=_field_location(fields, "检验依据", page_rect))]
        return _finding("REPORT-R13", checks, "")
    issues = analyze_basis_and_conclusion(basis, conclusion, client=client, producer=producer, inspection_items=items)
    checks = [
        _check(
            f"R13-{index:02d}",
            issue["field"],
            issue["status"],
            issue["reason_code"],
            issue["message"],
            observed=issue.get("observed"),
            expected=issue.get("expected"),
            location=_field_location(fields, issue["field"], page_rect),
        )
        for index, issue in enumerate(issues, start=1)
    ]
    if not checks:
        checks = [
            _check("R13-00a", "检验依据", "pass", "basis_wording_ok", "检验依据写法符合要求", observed=compact(basis), location=_field_location(fields, "检验依据", page_rect)),
            _check("R13-00b", "检验结论", "pass", "conclusion_wording_ok", "检验结论写法符合要求且与检验依据一致", observed=compact(conclusion), location=_field_location(fields, "检验结论", page_rect)),
        ]
    provide = "委托方与生产单位不一致，已写“提供”" if _name_relation(client, producer) == "different" else "委托方与生产单位一致，未写“提供”"
    return _finding("REPORT-R13", checks, f"检验依据与检验结论写法一致且符合要求（{provide}）")


def _r14(front: Mapping[str, Any], page_rect: fitz.Rect, cover_rect: fitz.Rect) -> dict[str, Any]:
    fields = front["page3_fields"]
    checks: list[dict[str, Any]] = []
    parsed: dict[str, date | None] = {}

    def date_check(name: str, raw: str, check_id: str, location: Mapping[str, Any]) -> date | None:
        value, issue = parse_cn_date(raw)
        if issue:
            checks.append(_check(check_id, name, issue["status"], issue["reason_code"], f"{name}{issue['message']}", observed=issue.get("observed"), expected=issue.get("expected"), location=location))
        else:
            checks.append(_check(check_id, name, "pass", "date_format_ok", f"{name}格式正确", observed=compact(raw), location=location))
        return value

    arrival = (fields.get("到样日期") or {}).get("value")
    if arrival is None:
        checks.append(_check("R14-arrival", "到样日期", "manual", "field_not_located", "未定位到到样日期", location=_field_location(fields, "到样日期", page_rect)))
    else:
        parsed["到样日期"] = date_check("到样日期", arrival, "R14-arrival", _field_location(fields, "到样日期", page_rect))

    inspection = (fields.get("检验日期") or {}).get("value")
    if inspection is None:
        checks.append(_check("R14-inspection", "检验日期", "manual", "field_not_located", "未定位到检验日期", location=_field_location(fields, "检验日期", page_rect)))
    else:
        text = compact(inspection)
        location = _field_location(fields, "检验日期", page_rect)
        parts = re.split(r"[～〜]", text)
        if len(parts) == 1 and re.search(r"[-~－—至]", text) and "年" in text:
            checks.append(_check("R14-inspection-range", "检验日期", "error", "date_range_symbol", f"检验日期区间应使用“～”：{text}", observed=text, location=location))
            parts = re.split(r"[-~－—至]", text)
        if len(parts) == 1:
            parsed["检验开始"] = parsed["检验结束"] = date_check("检验日期", parts[0], "R14-inspection", location)
        elif len(parts) == 2:
            parsed["检验开始"] = date_check("检验日期（开始）", parts[0], "R14-inspection-start", location)
            parsed["检验结束"] = date_check("检验日期（结束）", parts[1], "R14-inspection-end", location)
        else:
            checks.append(_check("R14-inspection", "检验日期", "manual", "date_not_parsed", f"检验日期无法解析：{text}", observed=text, location=location))

    issue_rect = front.get("issue_date_rect") or [page_rect.x0, page_rect.y0, page_rect.x1, page_rect.y1]
    issue_location = _location(3, issue_rect, "report_page3_issue_date")
    issue_raw = compact(front.get("issue_date"))
    issue_date: date | None = None
    if not front.get("issue_date_found"):
        checks.append(_check("R14-issue", "签发日期", "manual", "field_not_located", "未定位到签发日期", location=issue_location))
    elif not issue_raw:
        checks.append(_check("R14-issue", "签发日期", "not_applicable", "draft_issue_date_blank", "签发日期为空（草稿），不判断签发日期格式", location=issue_location))
    else:
        issue_date = date_check("签发日期", issue_raw, "R14-issue", issue_location)
        parsed["签发日期"] = issue_date

    sampling = compact((fields.get("抽样日期") or {}).get("value"))
    if sampling and sampling != "/":
        sampling_date = None
        for pattern in (r"(\d{4})年(\d{1,2})月(\d{1,2})日", r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})"):
            if match := re.fullmatch(pattern, sampling):
                try:
                    sampling_date = date(*(int(part) for part in match.groups()))
                except ValueError:
                    sampling_date = None
                break
        parsed["抽样日期"] = sampling_date

    order = [("抽样日期", "到样日期"), ("到样日期", "检验开始"), ("检验开始", "检验结束"), ("检验结束", "签发日期")]
    order_ok = True
    for left, right in order:
        a, b = parsed.get(left), parsed.get(right)
        if a and b and a > b:
            order_ok = False
            checks.append(_check(
                f"R14-order-{left}-{right}",
                "日期先后",
                "error",
                "date_order",
                f"{left}（{a.year}年{a.month}月{a.day}日）晚于{right}（{b.year}年{b.month}月{b.day}日）",
                observed=f"{a.isoformat()} > {b.isoformat()}",
                location=_field_location(fields, "检验日期" if "检验" in right else "到样日期", page_rect),
            ))
    if order_ok and len([value for value in parsed.values() if value]) >= 2:
        checks.append(_check("R14-order", "日期先后", "pass", "date_order_ok", "抽样、到样、检验、签发日期先后合理", location=_field_location(fields, "检验日期", page_rect)))

    cover = front["cover"]
    version_rect = cover.get("version_rect") or [cover_rect.x0, cover_rect.y0, cover_rect.x1, cover_rect.y0 + 60]
    version_location = _location(1, version_rect, "report_cover_version")
    observed_version = f"版本/修订：{cover.get('version')} 生效日期:{cover.get('effective_date')} {cover.get('form_code') or ''}".strip()
    if not cover.get("version"):
        checks.append(_check("R14-version", "封面版本", "manual", "cover_version_not_located", "未在封面定位到“版本/修订”信息", location=version_location))
    elif issue_date is None:
        checks.append(_check("R14-version", "封面版本", "not_applicable", "cover_version_draft_not_judged", "报告未签发（签发日期为空），不判断封面版本", observed=observed_version, location=version_location))
    else:
        expected = expected_cover_version(issue_date)
        if expected is None:
            checks.append(_check("R14-version", "封面版本", "manual", "cover_version_table_not_covering", f"签发日期{issue_date.isoformat()}早于版本表最早生效日期，请人工确认", observed=observed_version, location=version_location))
        else:
            version, effective = expected
            expected_text = f"版本/修订：{version} 生效日期:{effective.isoformat()} {COVER_FORM_CODE}"
            problems = []
            if cover.get("version") != version:
                problems.append(f"版本应为{version}")
            if cover.get("effective_date") != effective.isoformat():
                problems.append(f"生效日期应为{effective.isoformat()}")
            if (cover.get("form_code") or "") != COVER_FORM_CODE:
                problems.append(f"表单编号应为{COVER_FORM_CODE}")
            if problems:
                checks.append(_check("R14-version", "封面版本", "error", "cover_version_outdated", f"签发日期{issue_date.year}年{issue_date.month}月{issue_date.day}日对应的封面应为“{expected_text}”，报告为“{observed_version}”（{'；'.join(problems)}）", observed=observed_version, expected=expected_text, location=version_location))
            else:
                checks.append(_check("R14-version", "封面版本", "pass", "cover_version_ok", "封面版本与签发日期对应的生效版本一致", observed=observed_version, location=version_location))
    return _finding("REPORT-R14", checks, "日期格式、日期先后与封面版本均符合要求")


def _r15(front: Mapping[str, Any], page_rect: fitz.Rect, page_rects: Sequence[fitz.Rect]) -> dict[str, Any]:
    fields = front["page3_fields"]
    cover = front["cover"]
    checks: list[dict[str, Any]] = []
    cover_number = compact(cover.get("report_number"))
    number_match = _REPORT_NUMBER_RE.search(cover_number)
    expected_sample = f"QW{number_match.group(1)}-{int(number_match.group(2)):04d}" if number_match else None
    cover_location = _location(1, cover.get("report_number_rect") or [page_rects[0].x0, page_rects[0].y0, page_rects[0].x1, page_rects[0].y1], "report_cover_number")
    if not cover_number:
        checks.append(_check("R15-cover", "报告编号", "manual", "cover_report_number_not_located", "未在封面定位到报告编号", location=cover_location))

    page3_sample = compact((fields.get("样品编号") or {}).get("value")).replace("－", "-")
    if expected_sample:
        location = _field_location(fields, "样品编号", page_rect)
        if not page3_sample:
            checks.append(_check("R15-sample-page3", "样品编号", "error", "sample_number_missing", "第三页样品编号为空", expected=expected_sample, location=location))
        elif page3_sample != expected_sample:
            checks.append(_check("R15-sample-page3", "样品编号", "error", "sample_number_mismatch", f"第三页样品编号“{page3_sample}”与报告编号对应的“{expected_sample}”不一致", observed=page3_sample, expected=expected_sample, location=location))
        else:
            checks.append(_check("R15-sample-page3", "样品编号", "pass", "sample_number_ok", "第三页样品编号与报告编号对应", observed=page3_sample, location=location))

    page_count = int(front["page_count"])
    totals = set()
    for header in front["headers"]:
        page = header["pdf_page"]
        rects = header.get("rects") or []
        rect = rects[0] if rects else [page_rects[page - 1].x0, page_rects[page - 1].y0, page_rects[page - 1].x1, page_rects[page - 1].y0 + 120]
        location = _location(page, rect, "report_page_header")
        observed_number = compact(header.get("report_number"))
        problems = []
        if not observed_number:
            problems.append(("header_report_number_missing", "页眉未找到报告编号"))
        elif cover_number and observed_number != cover_number:
            problems.append(("header_report_number_mismatch", f"页眉报告编号“{observed_number}”与封面“{cover_number}”不一致"))
        sample = compact(header.get("sample_number")).replace("－", "-")
        if page > 3 and expected_sample:
            if not sample:
                problems.append(("header_sample_number_missing", "页眉未找到样品编号"))
            elif sample != expected_sample:
                problems.append(("header_sample_number_mismatch", f"页眉样品编号“{sample}”与报告编号对应的“{expected_sample}”不一致"))
        if header.get("printed_total") is not None:
            totals.add(header["printed_total"])
        if problems:
            checks.append(_check(
                f"R15-header-p{page:03d}",
                "页眉",
                "error",
                problems[0][0],
                f"第{page}页（PDF页）" + "；".join(message for _code, message in problems),
                observed={"report_number": observed_number, "sample_number": sample},
                expected={"report_number": cover_number, "sample_number": expected_sample},
                location=location,
            ))
        else:
            checks.append(_check(f"R15-header-p{page:03d}", "页眉", "pass", "header_ok", f"第{page}页页眉报告编号/样品编号一致", location=location))

    expected_total = page_count - UNNUMBERED_LEADING_PAGES
    last_rect = page_rects[-1]
    last_location = _location(page_count, [last_rect.x0, last_rect.y0, last_rect.x1, last_rect.y0 + 120], "report_page_total")
    if not totals:
        checks.append(_check("R15-total", "总页数", "manual", "printed_total_not_located", "未找到“共N页”", location=last_location))
    elif totals != {expected_total}:
        checks.append(_check("R15-total", "总页数", "error", "printed_total_mismatch", f"页眉总页数为{'、'.join(str(value) for value in sorted(totals))}，PDF共{page_count}页（除封面和注意事项页后应为{expected_total}页）", observed=sorted(totals), expected=expected_total, location=last_location))
    else:
        checks.append(_check("R15-total", "总页数", "pass", "printed_total_ok", f"总页数{expected_total}与PDF实际页数一致", observed=expected_total, location=last_location))
    return _finding("REPORT-R15", checks, f"各页页眉报告编号、样品编号一致，总页数{expected_total}与实际页数一致")


def _r16(front: Mapping[str, Any], page_rect: fitz.Rect) -> dict[str, Any]:
    fields = front["page3_fields"]
    checks: list[dict[str, Any]] = []
    title = front.get("signatory_title")
    title_location = _location(3, front.get("signatory_title_rect") or [page_rect.x0, page_rect.y0, page_rect.x1, page_rect.y1], "report_page3_signatory_title")
    if title is None:
        checks.append(_check("R16-title", "职务", "manual", "field_not_located", "未在第三页定位到“职务”", location=title_location))
    elif compact(title) != SIGNATORY_TITLE:
        checks.append(_check("R16-title", "职务", "error", "signatory_title", f"职务应填写“{SIGNATORY_TITLE}”，报告为“{compact(title) or '空'}”", observed=compact(title), expected=SIGNATORY_TITLE, location=title_location))
    else:
        checks.append(_check("R16-title", "职务", "pass", "signatory_title_ok", "职务为“授权签字人”", observed=SIGNATORY_TITLE, location=title_location))

    client = (fields.get("委托方") or {}).get("value", "")
    for name in ("委托方", "受检单位", "生产单位", "委托方地址", "抽样单位"):
        value = (fields.get(name) or {}).get("value", "")
        if value and _has_mixed_brackets(value):
            checks.append(_check(f"R16-mixed-{name}", name, "error", "name_mixed_bracket_width", f"{name}“{compact(value)}”中同时使用了全角和半角括号", observed=compact(value), location=_field_location(fields, name, page_rect)))
    for name in ("受检单位", "生产单位"):
        value = (fields.get(name) or {}).get("value", "")
        if client and value and _name_relation(client, value) == "bracket_width":
            checks.append(_check(f"R16-bracket-{name}", name, "error", "name_bracket_width_inconsistent", f"{name}“{compact(value)}”与委托方“{compact(client)}”括号全/半角不一致", observed=compact(value), expected=compact(client), location=_field_location(fields, name, page_rect)))
    cover_client = front["cover"].get("client") or ""
    if cover_client and client and _name_relation(cover_client, client) == "bracket_width":
        checks.append(_check("R16-bracket-cover", "委托方", "error", "name_bracket_width_inconsistent", f"封面委托方“{compact(cover_client)}”与第三页委托方“{compact(client)}”括号全/半角不一致", observed=compact(cover_client), expected=compact(client), location=_field_location(fields, "委托方", page_rect)))
    if not any(item["check_id"].startswith(("R16-mixed", "R16-bracket")) for item in checks):
        checks.append(_check("R16-names", "单位名称", "pass", "name_bracket_width_ok", "委托方、受检单位、生产单位等名称括号全/半角一致", location=_field_location(fields, "委托方", page_rect)))

    delivered = compact((fields.get("送样标记") or {}).get("value"))
    sampled = compact((fields.get("抽样标记") or {}).get("value"))
    mark_location = _location(3, (fields.get("送样标记") or {}).get("rect") or [page_rect.x0, page_rect.y0, page_rect.x1, page_rect.y1], "report_page3_sampling_mark")
    delivered_checked = "√" in delivered
    sampled_checked = "√" in sampled
    if not delivered and not sampled:
        checks.append(_check("R16-sampling", "送样/抽样", "manual", "sampling_mark_not_located", "未定位到送样/抽样标记", location=mark_location))
    elif delivered_checked == sampled_checked:
        checks.append(_check("R16-sampling", "送样/抽样", "error", "sampling_mark_conflict", f"送样与抽样标记应二选一，报告为“{delivered}”“{sampled}”", observed=[delivered, sampled], location=mark_location))
    else:
        wrong = []
        for name in SAMPLING_FIELDS:
            value = compact((fields.get(name) or {}).get("value"))
            if name not in fields:
                continue
            if delivered_checked and value not in {"/", ""}:
                wrong.append(f"{name}应为“/”（报告为“{value}”）")
            if sampled_checked and value in {"/", ""}:
                wrong.append(f"{name}未填写")
        if wrong:
            checks.append(_check("R16-sampling", "抽样字段", "error", "sampling_fields_inconsistent", ("送样" if delivered_checked else "抽样") + "：" + "；".join(wrong), observed=wrong, location=mark_location))
        else:
            checks.append(_check("R16-sampling", "抽样字段", "pass", "sampling_fields_ok", ("送样，抽样相关字段均为“/”" if delivered_checked else "抽样，抽样相关字段均已填写"), location=mark_location))
    return _finding("REPORT-R16", checks, "职务、单位名称括号与抽样字段均符合要求")


FRONT_RULE_IDS: tuple[str, ...] = ("REPORT-R12", "REPORT-R13", "REPORT-R14", "REPORT-R15", "REPORT-R16")


def front_matter_findings(document: fitz.Document) -> dict[str, dict[str, Any]]:
    """Run REPORT-R12～R16 on an open Report document."""

    page_rects = [fitz.Rect(page.rect) for page in document]
    if document.page_count < 3:
        location = _location(1, page_rects[0], "report_document")
        return {
            rule_id: _finding(rule_id, [_check(f"{rule_id}-doc", "文档", "manual", "report_too_short", "Report 少于3页，无法定位首页", location=location)], "")
            for rule_id in FRONT_RULE_IDS
        }
    front = extract_front_matter(document)
    page3_rect = page_rects[2]
    findings = {
        "REPORT-R12": _r12(front, page3_rect),
        "REPORT-R13": _r13(front, page3_rect),
        "REPORT-R14": _r14(front, page3_rect, page_rects[0]),
        "REPORT-R15": _r15(front, page3_rect, page_rects),
        "REPORT-R16": _r16(front, page3_rect),
    }
    for finding in findings.values():
        finding["details"]["page3_values"] = {
            key: value.get("value") for key, value in front["page3_fields"].items()
        }
    return findings
