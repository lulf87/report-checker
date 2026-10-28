"""Cell-level comparison of a PTR parameter-table row and its Report expansion.

Pacemaker-type PTRs keep their parameters in one table (``参数 | 设置 |
标称值 | 允差`` …, or one column per product model) and every clause only
says “应符合表 N 的要求”.  The Report copies the clause sentence and then
*expands* the relevant table row as labelled values::

    基本频率（min−1）
    设置：30-130，步幅为5；140-170，步幅为10
    标称值：60min−1
    允差：±15ms
    @240Ω  单位：ms

This module turns both sides into ``(column, value)`` cells and compares them
cell by cell.  It never guesses:

* a Report value is accepted only when its label is a PTR column header (or
  the parameter name itself, for tables whose columns are product models)
  and its normalized value equals the PTR cell of that row;
* when the Report splits one PTR cell into several pieces (one per load,
  ``240 Ω：…`` / ``500 Ω：…``), the pieces are delimited by the PTR text and
  must reassemble the PTR cell exactly;
* test-point lines (``@240Ω``, ``0.25V、0.5V：±0.25 V``) are accepted only
  when every number in them is printed in the PTR clause or table, or lies
  inside the parameter's own PTR range.

The result lists every compared cell so the UI can point at the exact cell
that differs instead of the whole requirement text.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any, Mapping, Sequence

RULE_ID = "TABLE_ROW_EXPANSION"

_FOOTNOTE_RE = re.compile(r"[①-⑳]|\*\d*")
_PAREN_RE = re.compile(r"[(（][^()（）]*[)）]")
_LABELLED_RE = re.compile(r"^\s*(?P<label>[^:：\d@\s①-⑳*][^:：]{0,24}?)\s*[:：]\s*(?P<value>.*)$")
_OHM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[kK]?\s*Ω")
_UNIT_IN_LABEL_RE = re.compile(r"[(（]\s*([A-Za-zμµ°%/·\-−0-9]+)\s*[)）]")
_TEST_POINT_RE = re.compile(r"^\s*(?:@\s*\d|\d[^:：]{0,30}[:：])")
_UNIT_LINE_RE = re.compile(r"^\s*单位\s*[:：]")
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
_HEADER_WORDS = {"参数", "型号", "项目", "名称", "参数名称"}


def _norm(value: Any) -> str:
    from mvp.ptr_compare import normalize_comparison_text

    text = unicodedata.normalize("NFKC", str(value or "")).replace("−", "-").replace("–", "-")
    text = normalize_comparison_text(text)
    # ``+20/-25%`` and ``+20%/-25%`` state the same asymmetric tolerance.
    text = re.sub(r"([+\-±]?\d+(?:\.\d+)?)/(-\d+(?:\.\d+)?)%", r"\1%/\2%", text)
    return re.sub(r"[。；;，,、]+$", "", text)


def _key(value: Any) -> str:
    """Parameter name without units, footnote marks or qualifiers."""

    # Footnote marks first: NFKC would turn ``③`` into a plain ``3``.
    text = unicodedata.normalize("NFKC", _FOOTNOTE_RE.sub("", str(value or "")))
    previous = None
    while previous != text:
        previous = text
        text = _PAREN_RE.sub("", text)
    return re.sub(r"[\s:：]+", "", text)


def _numbers(value: Any) -> list[float]:
    text = unicodedata.normalize("NFKC", _FOOTNOTE_RE.sub("", str(value or "")))
    return [float(n) for n in _NUMBER_RE.findall(text)]


def _is_header(row: Sequence[str], *, first_row: bool) -> bool:
    if not row or not str(row[0] or "").strip():
        return False
    cells = [c for c in row if str(c or "").strip()]
    if len(cells) < 3:
        return False
    if _key(cells[0]) in _HEADER_WORDS:
        return True
    return first_row and not any(_NUMBER_RE.search(_FOOTNOTE_RE.sub("", c)) for c in cells[1:])


def _clause_parameter(ptr_node: Mapping[str, Any]) -> str:
    title = str(ptr_node.get("title") or str(ptr_node.get("text") or "").split("\n", 1)[0])
    # ``脉冲幅度(V)：脉冲幅度应符合表1 中的数值。`` – heading and body on one line.
    title = re.split(r"[:：]", title, maxsplit=1)[0]
    return _key(title)


def ptr_row_cells(ptr_node: Mapping[str, Any]) -> dict[str, Any] | None:
    """Find the PTR table row for this clause and return its labelled cells."""

    parameter = _clause_parameter(ptr_node)
    if not parameter:
        return None
    header: list[str] | None = None
    groups: list[dict[str, Any]] = []
    table_numbers: set[float] = set()
    for table in ptr_node.get("table_rows") or []:
        for row_index, row in enumerate(table.get("rows") or []):
            row = [str(cell or "") for cell in row]
            table_numbers.update(_numbers(" ".join(row)))
            if _is_header(row, first_row=row_index == 0 and not table.get("continuation_of")):
                header = row
                continue
            if row and row[0].strip():
                groups.append({"key": _key(row[0]), "rows": [row], "header": header, "table": table})
            elif groups:
                groups[-1]["rows"].append(row)
    matches = [
        g for g in groups
        if g["key"] and g["header"]
        and (g["key"] == parameter or g["key"].startswith(parameter) or g["key"].endswith(parameter))
    ]
    if len(matches) != 1:
        return None
    group = matches[0]
    header = group["header"]
    cells: list[dict[str, str]] = []
    row_numbers: list[float] = []
    for row in group["rows"]:
        for index, value in enumerate(row):
            if index == 0 or index >= len(header) or not value.strip():
                continue
            row_numbers.extend(_numbers(value))
            column = _key(header[index])
            # A cell may itself carry labelled values (``…\n出厂值：VVI``,
            # ``双腔起搏模式：93；125；…``); each label owns the wrapped lines
            # that follow it.
            segments: list[tuple[str, list[str]]] = [(column, [])]
            for line in value.splitlines():
                labelled = _LABELLED_RE.match(line)
                if labelled and _key(labelled.group("label")):
                    segments.append((_key(labelled.group("label")), [labelled.group("value")]))
                else:
                    segments[-1][1].append(line)
            for name, parts in segments:
                text = "\n".join(parts)
                if not text.strip():
                    continue
                cells.append({
                    "column": name,
                    "header": header[index].strip(),
                    "value": _norm(text),
                    "raw": re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)),
                    "text": text,
                    "plain": name == column,
                })
    # A cell continued on the next physical row (empty first column) is one
    # value: offer the concatenation per column as well.
    by_header: dict[str, list[str]] = {}
    for cell in cells:
        if cell["plain"]:
            by_header.setdefault(cell["header"], []).append(cell["text"])
    for header_name, texts in by_header.items():
        if len(texts) > 1:
            joined = "".join(texts)
            cells.append({
                "column": _key(header_name),
                "header": header_name,
                "value": _norm(joined),
                "raw": re.sub(r"\s+", "", unicodedata.normalize("NFKC", joined)),
                "text": joined,
                "plain": True,
            })
    unit_match = _UNIT_IN_LABEL_RE.findall(unicodedata.normalize("NFKC", group["rows"][0][0]))
    row_unit = unit_match[-1].replace("−", "-") if unit_match else ""
    return {
        "row_key": group["key"],
        "row_unit": row_unit,
        "row_label": group["rows"][0][0],
        "pdf_page": group["table"].get("pdf_page"),
        "bbox": group["table"].get("bbox"),
        "columns": sorted({c["column"] for c in cells}),
        "cells": cells,
        "table_numbers": table_numbers,
        "row_range": (min(row_numbers), max(row_numbers)) if row_numbers else None,
    }


def _report_lines(report_node: Mapping[str, Any]) -> list[str]:
    lines = str(report_node.get("text") or "").splitlines()
    for table in report_node.get("table_rows") or []:
        row = table.get("matrix_row")
        if row is None:
            continue
        for cell in row:
            lines.extend(str(cell or "").splitlines())
    return [line for line in lines if line.strip()]


def _is_heading(line: str, row_key: str) -> bool:
    key = _key(line)
    if not key or _NUMBER_RE.search(key) or re.search(r"应(?!期)|须", line) or len(key) > 20:
        return False
    if not re.search(r"[\u3400-\u9fff]", key):
        return False
    return bool(row_key and row_key in key) or bool(re.search(r"[:：]\s*$", line)) or len(key) <= 4


def _column_for(label: str, ptr_row: Mapping[str, Any]) -> str | None | bool:
    """PTR column for a Report label; ``None`` = any cell of the row; ``False`` = not a table value."""

    key = _key(label)
    if key in ptr_row["columns"]:
        return key
    row_key = ptr_row["row_key"]
    if key and (row_key in key or key.endswith(row_key) or row_key.endswith(key)):
        return None
    return False


def compare_row_expansion(
    ptr_node: Mapping[str, Any],
    report_node: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Compare a Report's expansion of the PTR table row, cell by cell.

    Returns ``None`` when the clause does not have that structure (no unique
    PTR row, or no Report value labelled with a PTR column).
    """

    ptr_row = ptr_row_cells(ptr_node)
    if ptr_row is None:
        return None
    lines = _report_lines(report_node)
    # The Report expansion must name the PTR row it copies; otherwise the
    # clause numbers line up but the content does not (e.g. PVARP vs AV delay).
    if not any(ptr_row["row_key"] in _key(line) for line in lines):
        return None
    clause_numbers = set(_numbers(ptr_node.get("text"))) | ptr_row["table_numbers"]
    low_high = ptr_row["row_range"]
    cells: list[dict[str, Any]] = []
    pieces: dict[str | None, list[tuple[str, dict[str, Any]]]] = {}
    consumed: set[int] = set()
    used_column = False
    index = 0

    def is_start(line: str) -> bool:
        return bool(
            _UNIT_LINE_RE.match(line)
            or _TEST_POINT_RE.match(line)
            or _LABELLED_RE.match(line)
            or _is_heading(line, ptr_row["row_key"])
        )

    while index < len(lines):
        line = lines[index]
        labelled = _LABELLED_RE.match(line)
        column = _column_for(labelled.group("label"), ptr_row) if labelled else False
        if labelled:
            # ``设置：双腔起搏模式：93；…`` – the inner label names the PTR cell.
            inner = _LABELLED_RE.match(labelled.group("value"))
            if inner and _column_for(inner.group("label"), ptr_row) not in (False, None):
                labelled, column = inner, _column_for(inner.group("label"), ptr_row)
        if _UNIT_LINE_RE.match(line):
            consumed.add(index)
            index += 1
            continue
        if labelled and column is not False:
            candidates = [c["value"] for c in ptr_row["cells"] if column is None or c["column"] == column]
            raw_candidates = [c.get("raw", c["value"]) for c in ptr_row["cells"] if column is None or c["column"] == column]
            if ptr_row["row_unit"]:
                # ``± 20`` under ``逸搏间期(ms)`` is ``±20ms``.
                candidates += [
                    c + ptr_row["row_unit"] for c in list(candidates)
                    if c and re.search(r"\d$", c)
                ]
            value = labelled.group("value")
            span = [index]
            index += 1
            if not _norm(value):
                # ``允差：`` on its own line: the value follows.
                if index < len(lines):
                    value = lines[index]
                    span.append(index)
                    index += 1
            # Absorb wrapped lines while the text stays a contiguous part of
            # the PTR cell (the PTR text decides where the value ends).
            while index < len(lines) and not _UNIT_LINE_RE.match(lines[index]):
                joined = _norm(value + lines[index])
                if any(joined in candidate for candidate in candidates) or (
                    not is_start(lines[index]) and _norm(value) not in candidates
                ):
                    value += lines[index]
                    span.append(index)
                    index += 1
                    continue
                break
            consumed.update(span)
            if column is not None:
                used_column = True
            normalized = _norm(value)
            cell = {
                "column": labelled.group("label").strip(),
                "report": re.sub(r"\s+", " ", value).strip(),
                "ptr": candidates[0] if len(candidates) == 1 else candidates,
                "report_lines": span,
            }
            if normalized in candidates:
                cell.update(
                    status="matched",
                    reason_code="CELL_EQUAL",
                    ptr=normalized,
                    ptr_columns=sorted({
                        c["header"] for c in ptr_row["cells"]
                        if (column is None or c["column"] == column)
                        and normalized in {c["value"], c["value"] + ptr_row["row_unit"]}
                    }),
                )
                used_column = True
            elif any(normalized in c for c in candidates):
                cell.update(status="pending", reason_code="CELL_PART")
                pieces.setdefault(column, []).append((normalized, cell))
            elif any(
                c and re.search(r"[;；,，、]$", c) and normalized.startswith(c.rstrip(";；,，、"))
                for c in raw_candidates
            ):
                # The PTR cell ends with a separator: the row continues on
                # the next page and the remainder was not attached.
                cell.update(status="manual", reason_code="CELL_PTR_CONTINUES")
            elif column is not None and set(_numbers(normalized)) - {
                n for c in candidates for n in _numbers(c)
            }:
                cell.update(status="mismatch", reason_code="CELL_VALUE_MISMATCH")
            else:
                cell.update(status="manual", reason_code="CELL_TEXT_DIFFERENT")
            cells.append(cell)
            continue
        if _TEST_POINT_RE.match(line):
            span = [index]
            text = line
            index += 1
            while index < len(lines) and not is_start(lines[index]):
                text += lines[index]
                span.append(index)
                index += 1
            loads = [float(v) for v in _OHM_RE.findall(unicodedata.normalize("NFKC", text))]
            ptr_loads = {float(v) for v in _OHM_RE.findall(unicodedata.normalize("NFKC", str(ptr_node.get("text") or "")))}
            missing = []
            for n in _numbers(text):
                if n in loads:
                    # A test load is a condition of the test method.  It must
                    # match the PTR only when the clause itself names loads.
                    if ptr_loads and n not in ptr_loads:
                        missing.append(n)
                    continue
                if n not in clause_numbers and not (low_high and low_high[0] <= n <= low_high[1]):
                    missing.append(n)
            cells.append({
                "column": "测试点",
                "report": re.sub(r"\s+", " ", text).strip(),
                "ptr": None,
                "status": "matched" if not missing else "manual",
                "reason_code": "TEST_POINT_IN_PTR" if not missing else "TEST_POINT_NOT_IN_PTR",
                "missing_numbers": [f"{n:g}" for n in missing],
                "report_lines": span,
            })
            consumed.update(span)
            continue
        index += 1

    # Pieces of one PTR cell must reassemble it exactly (in Report order,
    # repeated blocks for several chambers/loads counted once).
    for column, items in pieces.items():
        candidates = [c["value"] for c in ptr_row["cells"] if column is None or c["column"] == column]
        ordered: list[str] = []
        for text, _ in items:
            if text not in ordered:
                ordered.append(text)
        whole = "".join(ordered)
        ok = whole in candidates
        for _, cell in items:
            cell["status"] = "matched" if ok else "manual"
            cell["reason_code"] = "CELL_PIECES_REASSEMBLE" if ok else "CELL_PIECES_INCOMPLETE"
        if ok:
            used_column = True
    if not used_column:
        return None
    for cell in cells:
        if cell["status"] == "matched" or not isinstance(cell.get("ptr"), (list, str)) or not cell.get("ptr"):
            continue
        options = cell["ptr"] if isinstance(cell["ptr"], list) else [cell["ptr"]]
        report_value = _norm(cell["report"])
        closest = max(options, key=lambda v: SequenceMatcher(None, v, report_value, autojunk=False).ratio())
        cell["ptr_closest"] = closest
        from mvp.ptr_compare import _text_diff_segments

        segments = _text_diff_segments(cell["report"], closest)
        cell["report_segments"] = segments.get("report_segments")
        cell["ptr_segments"] = segments.get("ptr_segments")
        cell["diff"] = [
            {"op": op, "ptr": closest[a:b], "report": report_value[c:d]}
            for op, a, b, c, d in SequenceMatcher(None, closest, report_value, autojunk=False).get_opcodes()
            if op != "equal"
        ]
    statuses = {cell["status"] for cell in cells}
    status = "mismatch" if "mismatch" in statuses else "manual" if "manual" in statuses else "matched"
    return {
        "rule": RULE_ID,
        "status": status,
        "row_label": ptr_row["row_label"],
        "row_key": ptr_row["row_key"],
        "ptr_location": {"pdf_page": ptr_row["pdf_page"], "bbox": ptr_row["bbox"]},
        "cells": cells,
        "consumed_report_lines": sorted(consumed),
    }


def report_text_without_expansion(report_node: Mapping[str, Any], result: Mapping[str, Any]) -> str:
    """The Report requirement text with the verified expansion removed.

    Only lines consumed by the cell comparison are removed, plus the
    digit-free heading lines of the expansion block that follow the clause
    sentence (``脉冲幅度(V) ③（心房）``, ``计算公式：``, ``心房``).  Everything
    else, including a changed sentence, stays for the text and numeric rules.
    """

    text_lines = [line for line in str(report_node.get("text") or "").splitlines() if line.strip()]
    consumed = set(result.get("consumed_report_lines") or [])
    first = min(consumed) if consumed else len(text_lines)
    # The expansion starts after the last line that still belongs to the
    # requirement sentence (the one ending with 。 before the first value).
    sentence_end = max(
        (i for i, line in enumerate(text_lines[:first]) if re.search(r"应(?!期)|符合|表\s*[A-Za-z]?\d", line)),
        default=-1,
    )
    kept: list[str] = []
    for index, line in enumerate(text_lines):
        if index in consumed:
            continue
        if index > sentence_end and _is_heading(line, str(result.get("row_key") or "")):
            continue
        kept.append(line)
    return "\n".join(kept)
