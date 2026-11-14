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


def _clause_aliases(ptr_node: Mapping[str, Any]) -> set[str]:
    """Names the clause title gives in brackets: ``心室后心房不应期（PVARP）``.

    An alias only ever selects a table row whose name equals it exactly, so a
    unit in brackets (``(ms)``) selects nothing.
    """

    title = str(ptr_node.get("title") or str(ptr_node.get("text") or "").split("\n", 1)[0])
    title = re.split(r"[:：]", unicodedata.normalize("NFKC", title), maxsplit=1)[0]
    return {_key(name) for name in re.findall(r"\(([^()]+)\)", title) if _key(name)}


def _continues_previous_page(table: Mapping[str, Any], row_index: int, row: Sequence[str]) -> bool:
    """A table row cut by a page break: the next page's table starts with the
    rest of it, and its first cell holds only the rest of the row name – a
    bracketed unit such as ``（ms）`` – not a new name."""

    return bool(
        table.get("continuation_of") and row_index == 0 and row
        and str(row[0]).strip() and not _key(row[0])
        and any(str(cell or "").strip() for cell in row[1:])
    )


def ptr_row_cells(ptr_node: Mapping[str, Any]) -> dict[str, Any] | None:
    """Find the PTR table row for this clause and return its labelled cells."""

    parameter = _clause_parameter(ptr_node)
    if not parameter:
        return None
    aliases = _clause_aliases(ptr_node)
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
            if _continues_previous_page(table, row_index, row) and groups:
                row = ["", *row[1:]]
            if row and row[0].strip():
                groups.append({"key": _key(row[0]), "rows": [row], "header": header, "table": table})
            elif groups:
                groups[-1]["rows"].append(row)
    matches = [
        g for g in groups
        if g["key"] and g["header"]
        and (g["key"] == parameter or g["key"].startswith(parameter) or g["key"].endswith(parameter)
             or g["key"] in aliases)
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
    models: Sequence[str] | None = None,
) -> dict[str, Any] | None:
    """Compare a Report's expansion of the PTR table row, cell by cell.

    Returns ``None`` when the clause does not have that structure (no unique
    PTR row, or no Report value labelled with a PTR column).

    ``models`` are the models registered on the Report's page 3.  When it is
    given (an empty list means “not stated”) and the PTR row has different
    values for different product models, a Report value is accepted only if
    it is the value of a registered model; see ``_require_submitted_model``.
    """

    ptr_row = ptr_row_cells(ptr_node)
    if ptr_row is None:
        return None
    ptr_is_ocr = any(
        str(item.get("extraction_method", "")).startswith("ocr:") for item in ptr_node.get("evidence", [])
    ) or any(str(t.get("extraction_method", "")).startswith("ocr") for t in ptr_node.get("table_rows") or [])
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
                    # ``磁频率（min−1）:`` then ``BOS：96min−1`` – the value
                    # line carries the label of the PTR cell it copies.
                    inner = _LABELLED_RE.match(value)
                    if inner and _column_for(inner.group("label"), ptr_row) not in (False, None):
                        labelled, column = inner, _column_for(inner.group("label"), ptr_row)
                        value = inner.group("value")
                        candidates = [c["value"] for c in ptr_row["cells"] if c["column"] == column]
                        raw_candidates = [c.get("raw", c["value"]) for c in ptr_row["cells"] if c["column"] == column]
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
        if (
            cell.get("reason_code") == "CELL_TEXT_DIFFERENT"
            and not ptr_is_ocr
            and _few_characters_different(closest, report_value, cell["diff"])
        ):
            # The Report copies this PTR cell except for a character or two
            # (PTR ``0Dplus/DDIR``, Report ``Dplus/DDIR``): it is that cell,
            # and it is not copied word for word.
            cell.update(status="mismatch", reason_code="CELL_FEW_CHARACTERS_DIFFERENT")
    if models is not None:
        _require_submitted_model(ptr_node, ptr_row, cells, models)
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


FEW_CHARACTERS_MIN_LENGTH = 12
FEW_CHARACTERS_MAX_DIFFERENT = 2


def _few_characters_different(ptr_value: str, report_value: str, diff: Sequence[Mapping[str, Any]]) -> bool:
    """A long cell copied with at most two characters added, dropped or
    replaced.  Short cells (a value, a unit) are left out: there a character
    or two may be the whole content, and which PTR cell is meant is unknown."""

    if min(len(ptr_value), len(report_value)) < FEW_CHARACTERS_MIN_LENGTH or not diff:
        return False
    return (
        sum(len(str(item.get("ptr") or "")) for item in diff) <= FEW_CHARACTERS_MAX_DIFFERENT
        and sum(len(str(item.get("report") or "")) for item in diff) <= FEW_CHARACTERS_MAX_DIFFERENT
    )


# ---------------------------------------------------------------------------
# A clause that covers several PTR table rows
# ---------------------------------------------------------------------------

_MODEL_HEADERS = {"型号", "规格型号", "型号规格", "适用型号", "产品型号"}
_ALL_MODELS = {"全部型号", "所有型号", "全部", "通用"}


def _spaced(value: Any) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value or ""))).strip()


def _model_applies(model_cell: str, models: Sequence[str]) -> bool:
    """Is one of the submitted ``models`` printed in this PTR model cell?

    The name must stand on its own (``DR-T`` is not found inside ``SR-T`` and
    ``6232`` not inside ``62320``); a wrapped name (``Alpha 8⏎DR-T``) counts.
    """

    cell = _spaced(model_cell)
    if _key(cell) in _ALL_MODELS:
        return True
    for model in models:
        name = _spaced(model)
        start = cell.find(name) if name else -1
        while start >= 0:
            before = cell[start - 1] if start else " "
            after = cell[start + len(name)] if start + len(name) < len(cell) else " "
            if not (before.isalnum() or before == "-") and not (after.isalnum() or after == "-"):
                return True
            start = cell.find(name, start + 1)
    return False


def _rows_with_merged_cells(table: Mapping[str, Any]) -> list[list[str]]:
    """Table rows with vertically merged cells repeated in every row they span.

    PyMuPDF reports a merged cell once, in its first row, with a rectangle
    that covers all its rows; the rows below have *no* cell there (box
    ``None``), unlike a real empty cell, which has its own rectangle.  A
    missing cell takes the text of the cell above only when that cell's
    rectangle reaches down to this row.  The first column is left alone: an
    empty first cell is what marks a continuation row everywhere else.
    """

    rows = [[str(cell or "") for cell in row] for row in table.get("rows") or []]
    boxes = table.get("cell_boxes") or []
    if len(boxes) != len(rows):
        return rows
    for r, row in enumerate(rows):
        own = [box for box in boxes[r] if box is not None]
        if not own:
            continue
        bottom = max(float(box[3]) for box in own)
        for c in range(1, min(len(row), len(boxes[r]))):
            if boxes[r][c] is not None or row[c].strip():
                continue
            for q in range(r - 1, -1, -1):
                if c < len(boxes[q]) and boxes[q][c] is not None:
                    if float(boxes[q][c][3]) >= bottom - 1.5:
                        row[c] = rows[q][c]
                    break
    return rows


def _row_names(label: str) -> list[str]:
    """Parameter names in a first cell; one cell may serve two parameters
    (``房室间期(ms)⏎感知后的房室间期(ms)``)."""

    text = re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(label or "")))
    names = [part for part in re.split(r"(?<=\))(?=[^(①-⑳*])", text) if _key(part)]
    return names or [text]


def _clause_row_records(ptr_node: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every PTR table row that belongs to the clause's parameter.

    One record per physical row (merged cells repeated).  Only a row with an
    empty first cell at the top of a continuation table (the table went on to
    the next page) is joined to the record before it.
    """

    parameter = _clause_parameter(ptr_node)
    if not parameter:
        return []
    aliases = _clause_aliases(ptr_node)

    def related(key: str) -> bool:
        return bool(key) and (key == parameter or key.startswith(parameter) or key.endswith(parameter)
                              or key in aliases)

    header: list[str] | None = None
    group: dict[str, Any] | None = None
    records: list[dict[str, Any]] = []
    for table in ptr_node.get("table_rows") or []:
        for row_index, row in enumerate(_rows_with_merged_cells(table)):
            if _is_header(row, first_row=row_index == 0 and not table.get("continuation_of")):
                header = row
                continue
            if _continues_previous_page(table, row_index, row) and records:
                row = ["", *row[1:]]
            if row and row[0].strip():
                names = [name for name in _row_names(row[0]) if related(_key(name))]
                if not names and related(_key(row[0])):
                    names = [re.sub(r"\s+", "", row[0])]
                group = {"names": names, "header": header, "table": table} if names and header else None
            elif group is not None and group["table"] is not table and records and records[-1]["group"] is group:
                # The row continues on the next page.  A model cell cut by the
                # page break (``Alpha 8 DR`` / ``Alpha 8⏎DR-T``) is one cell.
                record = records[-1]
                index = record["model_index"]
                if index is not None and index < len(row) and row[index].strip():
                    record["model"] = "\n".join(part for part in (record["model"], row[index].strip()) if part)
                record["rows"].append(row)
                continue
            if group is None:
                continue
            head = group["header"]
            model_index = next((i for i, name in enumerate(head) if i and _key(name) in _MODEL_HEADERS), None)
            model = row[model_index].strip() if model_index is not None and model_index < len(row) else ""
            records.append({
                "group": group, "names": group["names"], "keys": {_key(name) for name in group["names"]},
                "header": head, "model_index": model_index, "rows": [row], "model": model or None,
                "pdf_page": table.get("pdf_page"), "bbox": table.get("bbox"),
            })
    return records


def compare_multi_row_expansion(
    ptr_node: Mapping[str, Any],
    report_node: Mapping[str, Any],
    models: Sequence[str] | None = None,
) -> dict[str, Any] | None:
    """Row expansion for a clause whose parameter has several PTR table rows.

    ``灵敏度`` covers the rows ``心房感知灵敏度`` and ``右心室感知灵敏度``;
    ``房室间期`` is printed as three rows; a row may exist once per product
    model.  The Report copies each row as its own block (the row name, then
    ``常规数值：…`` / ``标准设置：…`` / ``允许误差：…``).

    Every Report block is compared with every candidate PTR row using
    ``compare_row_expansion`` on that single row, and must equal one row in
    full: each labelled value equals that row's cell, and every non-empty
    cell of the row is present.  The blocks must then be exactly the rows the
    Report has to copy:

    * no model column: all rows of the parameter;
    * a model column: the rows whose model cell names a model registered on
      the Report's page 3 (``models``) or says 全部型号.  Unknown models
      (``见“样品描述”栏``) leave the result manual unless all rows are copied.

    Returns ``None`` when the clause has fewer than two such rows or the
    Report has no block that names one of them.
    """

    records = _clause_row_records(ptr_node)
    if len(records) < 2:
        return None
    lines = [line for line in str(report_node.get("text") or "").splitlines() if line.strip()]
    keys = {key for record in records for key in record["keys"]}
    starts = [index for index, line in enumerate(lines) if _key(line) in keys]
    if not starts:
        return None
    models = [str(model) for model in (models or []) if str(model).strip()]

    # Values the Report prints in the narrow label column of its result
    # table instead of the requirement text (``允许误⏎差：±⏎20%``).
    side_cells = [
        re.sub(r"\s+", "", str(cell or ""))
        for table in report_node.get("table_rows") or []
        for cell in (table.get("matrix_row") or [])
        if str(cell or "").strip()
    ]

    def attempt(record: Mapping[str, Any], block: Sequence[str]) -> dict[str, Any] | None:
        head = list(record["header"])
        rows = [list(row) for row in record["rows"]]
        label = next(name for name in record["names"] if _key(name) == _key(block[0]))
        rows[0][0] = label
        result = compare_row_expansion(
            {"title": label, "text": str(ptr_node.get("text") or ""),
             "table_rows": [{"rows": [head, *rows], "pdf_page": record["pdf_page"], "bbox": record["bbox"]}]},
            {"text": "\n".join(block)},
        )
        if result is None:
            return None
        unit = _UNIT_IN_LABEL_RE.findall(unicodedata.normalize("NFKC", label))
        unit = unit[-1].replace("−", "-") if unit else ""
        required: dict[str, set[str]] = {}
        for row in rows:
            for index, value in enumerate(row):
                if index and index != record["model_index"] and index < len(head) and value.strip():
                    required.setdefault(_key(head[index]), set()).update({_norm(value), _norm(value) + unit})
        copied: set[str] = set()
        for cell in result["cells"]:
            if cell["status"] == "matched":
                copied.add(_key(cell["column"]))
                copied.update(_key(name) for name in cell.get("ptr_columns") or [])
        result["side_cells"] = {}
        for column in sorted(set(required) - copied):
            for position, text in enumerate(side_cells):
                labelled = _LABELLED_RE.match(text)
                if labelled and _key(labelled.group("label")) == column and _norm(labelled.group("value")) in required[column]:
                    result["side_cells"][column] = position
                    break
        result["missing_columns"] = sorted(set(required) - copied - set(result["side_cells"]))
        result["complete"] = result["status"] == "matched" and not result["missing_columns"]
        return result

    blocks: list[dict[str, Any]] = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        key = _key(lines[start])
        candidates = [index for index, record in enumerate(records) if key in record["keys"]]
        attempts = {index: attempt(records[index], lines[start:end]) for index in candidates}
        if all(result is None for result in attempts.values()):
            if not blocks:
                continue  # the clause title repeats the row name; not a block
            blocks.append({"start": start, "end": end, "key": key, "attempts": {}, "complete": []})
            continue
        blocks.append({
            "start": start, "end": end, "key": key,
            "attempts": {index: result for index, result in attempts.items() if result is not None},
            "complete": [index for index, result in attempts.items() if result is not None and result["complete"]],
        })
    if not blocks:
        return None

    has_models = any(record["model_index"] is not None for record in records)
    if not has_models:
        expected, coverage_reason = list(range(len(records))), "ALL_ROWS_REQUIRED"
    elif models:
        expected = [
            index for index, record in enumerate(records)
            if record["model"] is None or _model_applies(record["model"], models)
        ]
        coverage_reason = "ROWS_OF_SUBMITTED_MODELS"
    else:
        expected, coverage_reason = list(range(len(records))), "SUBMITTED_MODELS_UNKNOWN_ALL_ROWS_REQUIRED"

    # Each block must be a different expected row (rows with equal values are
    # interchangeable, so try the assignments).
    # A value found in the result table's label column may support one block only.
    def assign(position: int, used: frozenset[int], side: frozenset[int]) -> list[int] | None:
        if position == len(blocks):
            return []
        for index in blocks[position]["complete"]:
            needs = frozenset(blocks[position]["attempts"][index]["side_cells"].values())
            if index in expected and index not in used and not (needs & side):
                rest = assign(position + 1, used | {index}, side | needs)
                if rest is not None:
                    return [index, *rest]
        return None

    assignment = assign(0, frozenset(), frozenset()) if len(blocks) == len(expected) else None
    cells: list[dict[str, Any]] = []
    consumed: set[int] = set()
    statuses: set[str] = set()
    for position, block in enumerate(blocks):
        if assignment is not None:
            chosen = assignment[position]
        elif block["complete"]:
            chosen = block["complete"][0]
        elif block["attempts"]:
            chosen = max(block["attempts"], key=lambda index: sum(
                cell["status"] == "matched" for cell in block["attempts"][index]["cells"]
            ))
        else:
            statuses.add("manual")
            continue
        result = block["attempts"][chosen]
        consumed.add(block["start"])
        consumed.update(block["start"] + index for index in result["consumed_report_lines"])
        only_candidate = len([i for i in block["attempts"] if i in expected]) == 1 and chosen in expected
        status = result["status"]
        if status == "mismatch" and not only_candidate:
            status = "manual"  # another row might be the one that was copied
        if status == "matched" and not result["complete"]:
            status = "manual"
        statuses.add(status)
        row_name = _spaced(lines[block["start"]])
        model_name = _spaced(records[chosen]["model"]) if records[chosen]["model"] else None
        for cell in result["cells"]:
            cells.append({
                **cell, "row": row_name, "model": model_name,
                "report_lines": [block["start"] + index for index in cell.get("report_lines") or []],
            })
        for column, position in result["side_cells"].items():
            labelled = _LABELLED_RE.match(side_cells[position])
            cells.append({
                "column": column, "row": row_name, "model": model_name,
                "report": labelled.group("value"), "ptr": _norm(labelled.group("value")),
                "status": "matched", "reason_code": "CELL_EQUAL_IN_RESULT_TABLE", "report_lines": [],
            })
        if result["missing_columns"]:
            cells.append({
                "column": "、".join(result["missing_columns"]), "row": row_name, "model": model_name,
                "report": "", "ptr": None, "status": "manual", "reason_code": "CELL_NOT_COPIED", "report_lines": [],
            })
    if assignment is None:
        statuses.add("manual")
    status = "mismatch" if "mismatch" in statuses else "manual" if "manual" in statuses else "matched"
    first = records[0]
    return {
        "rule": RULE_ID,
        "status": status,
        "row_label": "、".join(dict.fromkeys(name for record in records for name in record["names"])),
        "row_key": _clause_parameter(ptr_node),
        "ptr_location": {"pdf_page": first["pdf_page"], "bbox": first["bbox"]},
        "cells": cells,
        "consumed_report_lines": sorted(consumed),
        "row_coverage": {
            "status": "matched" if assignment is not None else "manual",
            "reason_code": coverage_reason if assignment is not None else "REPORT_BLOCKS_NOT_THE_REQUIRED_ROWS",
            "requirement": coverage_reason,
            "ptr_rows": len(records),
            "required_rows": len(expected),
            "report_blocks": len(blocks),
            "submitted_models": models,
        },
    }


def compare_parameter_column(
    ptr_node: Mapping[str, Any],
    report_node: Mapping[str, Any],
) -> dict[str, Any] | None:
    """A PTR table with a column named after the clause's parameter.

    ``腔室 | 模式 | 感知不应期`` with one row per chamber; the Report copies
    the parameter column as ``心室：95 ms ±10 ms``.  Every PTR row must have
    exactly one Report line labelled with its row name whose value equals the
    row's cell in the parameter column.

    The other columns (``模式``) are context of the PTR table and are not
    required in the Report (confirmed by the user for 0596 2.1.6.1); they are
    listed in ``unchecked_ptr_columns``.  Tables with a model column are not
    handled here.
    """

    parameter = _clause_parameter(ptr_node)
    labels = {compact_label(label) for label in (ptr_node.get("references") or {}).get("tables", [])}
    tables = [
        table for table in ptr_node.get("table_rows") or []
        if not labels or compact_label(table.get("label") or "") in labels
    ]
    if not parameter or not tables:
        return None
    header: list[str] | None = None
    rows: list[tuple[str, str, Mapping[str, Any]]] = []
    for table in tables:
        for row_index, row in enumerate(_rows_with_merged_cells(table)):
            if _is_header(row, first_row=row_index == 0 and not table.get("continuation_of")):
                if header is not None and [_key(c) for c in row] != [_key(c) for c in header]:
                    return None
                header = row
                continue
            if header is None:
                return None
            column = next((i for i, name in enumerate(header) if i and _key(name) == parameter), None)
            if column is None or any(_key(name) in _MODEL_HEADERS for name in header):
                return None
            if not row or not _key(row[0]) or column >= len(row) or not row[column].strip():
                return None
            rows.append((_key(row[0]), row[column], table))
    if not rows or header is None or len({name for name, _, _ in rows}) != len(rows):
        return None
    names = {name for name, _, _ in rows}
    lines = [line for line in str(report_node.get("text") or "").splitlines() if line.strip()]
    by_name: dict[str, list[tuple[int, str]]] = {}
    for index, line in enumerate(lines):
        labelled = _LABELLED_RE.match(line)
        if labelled and _key(labelled.group("label")) in names and labelled.group("value").strip():
            by_name.setdefault(_key(labelled.group("label")), []).append((index, labelled.group("value")))
    if not by_name:
        return None
    cells: list[dict[str, Any]] = []
    consumed: set[int] = set()
    for name, value, _ in rows:
        found = by_name.get(name) or []
        cell = {"column": header[0].strip() + "：" + name, "ptr": _norm(value), "report": "", "report_lines": []}
        if len(found) != 1:
            cell.update(status="manual", reason_code="CELL_NOT_COPIED" if not found else "ROW_COPIED_TWICE")
        else:
            index, text = found[0]
            cell.update(report=re.sub(r"\s+", " ", text).strip(), report_lines=[index])
            consumed.add(index)
            if _norm(text) == _norm(value):
                cell.update(status="matched", reason_code="CELL_EQUAL")
            elif _numbers(text) != _numbers(value):
                cell.update(status="mismatch", reason_code="CELL_VALUE_MISMATCH")
            else:
                cell.update(status="manual", reason_code="CELL_TEXT_DIFFERENT")
        cells.append(cell)
    first = min(consumed) if consumed else len(lines)
    for index, line in enumerate(lines):
        labelled = _LABELLED_RE.match(line)
        if index > first and _UNIT_LINE_RE.match(line):
            consumed.add(index)
        elif labelled and _key(labelled.group("label")) == parameter and not labelled.group("value").strip():
            consumed.add(index)  # ``感知不应期：`` heading of the copied column
    statuses = {cell["status"] for cell in cells}
    return {
        "rule": RULE_ID,
        "status": "mismatch" if "mismatch" in statuses else "manual" if "manual" in statuses else "matched",
        "row_label": next(name.strip() for i, name in enumerate(header) if i and _key(name) == parameter),
        "row_key": parameter,
        "ptr_location": {"pdf_page": rows[0][2].get("pdf_page"), "bbox": rows[0][2].get("bbox")},
        "cells": cells,
        "consumed_report_lines": sorted(consumed),
        "unchecked_ptr_columns": [name.strip() for i, name in enumerate(header) if i and _key(name) != parameter],
    }


def _require_submitted_model(
    ptr_node: Mapping[str, Any],
    ptr_row: Mapping[str, Any],
    cells: list[dict[str, Any]],
    models: Sequence[str],
) -> None:
    """Downgrade a matched cell that equals another model's value.

    A parameter row may hold one value per product model, either as one
    table column per model (header ``型号 | 6131 | 6232``) or as one row per
    model (a ``型号`` column).  When those values differ, a Report value that
    equals *some* model's value proves nothing: it must be the value of a
    model registered on the Report's page 3.  Otherwise the cell is left for
    review (``CELL_OF_ANOTHER_MODEL``, or ``SUBMITTED_MODEL_UNKNOWN`` when
    page 3 names no model).  Rows whose values are the same for every model
    are not affected.
    """

    records = _clause_row_records(ptr_node)
    if not records:
        return
    head = records[0]["header"]
    unit = ptr_row["row_unit"]
    by_model: dict[str, set[str]] = {}
    if _key(head[0]) in _MODEL_HEADERS:
        # One column per model: the header cell is the model name.
        for cell in ptr_row["cells"]:
            by_model.setdefault(cell["header"], set()).update({cell["value"], cell["value"] + unit})
    elif len(records) > 1 and any(record["model"] for record in records):
        for record in records:
            values = by_model.setdefault(_spaced(record["model"] or "全部型号"), set())
            for row in record["rows"]:
                for index, value in enumerate(row):
                    if index and index != record["model_index"] and value.strip():
                        values.update({_norm(value), _norm(value) + unit})
    if len({frozenset(values) for values in by_model.values()}) < 2:
        return
    for cell in cells:
        if cell["status"] != "matched" or cell.get("reason_code") != "CELL_EQUAL":
            continue
        value = _norm(cell["report"])
        owners = [model for model, values in by_model.items() if value in values]
        if len(owners) == len(by_model):
            continue  # every model has this value
        if models and any(_model_applies(owner, models) for owner in owners):
            continue
        cell.update(
            status="manual",
            reason_code="CELL_OF_ANOTHER_MODEL" if models else "SUBMITTED_MODEL_UNKNOWN",
            ptr_models=owners,
            submitted_models=list(models),
        )


def report_text_without_expansion(report_node: Mapping[str, Any], result: Mapping[str, Any]) -> str:
    """The Report requirement text with the verified expansion removed.

    Only lines consumed by the cell comparison are removed, plus the
    digit-free heading lines of the expansion block that follow the clause
    sentence (``脉冲幅度(V) ③（心房）``, ``计算公式：``, ``心房``).  Everything
    else, including a changed sentence, stays for the text and numeric rules.
    """

    if result.get("rule") == "TABLE_ROW_KEYS":
        return str(report_node.get("text") or "")
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
        if index > sentence_end and result.get("row_key") and _key(line) == result["row_key"]:
            continue  # the row's own name without Chinese characters: ``PVARP(ms)``
        kept.append(line)
    return "\n".join(kept)


# ---------------------------------------------------------------------------
# Whole-table comparison by row name (PTR table vs Report table)
# ---------------------------------------------------------------------------

TABLE_RULE_ID = "TABLE_ROW_KEYS"
_DECIMAL_RE = re.compile(r"\d+(?:\.\d+)?")


def _plain(value: Any) -> str:
    from mvp.ptr_compare import _strip_punctuation

    text = "\n".join(
        line for line in str(value or "").splitlines() if not _UNIT_LINE_RE.match(unicodedata.normalize("NFKC", line))
    )
    return _strip_punctuation(_norm(text))


def _numeric_equal(left: str, right: str) -> bool:
    """``0.70`` and ``0.7``: same value, different writing (reported as a difference)."""

    from decimal import Decimal

    if _DECIMAL_RE.sub("#", left) != _DECIMAL_RE.sub("#", right):
        return False
    a, b = _DECIMAL_RE.findall(left), _DECIMAL_RE.findall(right)
    return len(a) == len(b) and all(Decimal(x) == Decimal(y) for x, y in zip(a, b))


def _ptr_table_rows(
    ptr_node: Mapping[str, Any],
    labels: set[str],
    noise: set[str] | None = None,
) -> tuple[list[tuple[str, str, dict]], str]:
    """PTR data rows as ``(key, value, table)`` plus the header text."""

    rows: list[tuple[str, str, dict]] = []
    header = ""
    noise = noise or set()
    for table in ptr_node.get("table_rows") or []:
        if labels is not None and labels and str(table.get("label") or "") not in labels:
            continue
        if noise and all(
            not str(c or "").strip() or _plain(c) in noise or re.fullmatch(r"第?\d+/\d+页?", _plain(c))
            for row in table.get("rows") or [] for c in row
        ):
            continue  # running header/footer mis-detected as a table
        # A vertically merged value cell (``≥15`` for four rows) belongs to
        # every row it spans.
        for index, row in enumerate(_rows_with_merged_cells(table)):
            cells = [str(c or "").strip() for c in row if str(c or "").strip()]
            if not cells:
                continue
            if index == 0 and not table.get("continuation_of") and not re.search(r"\d", "".join(cells)):
                header += _plain(" ".join(cells))
                continue
            if len(cells) == 1:
                # Text-recovered table: ``肩部左右伸展 116° 116°`` in one
                # cell; a cell with several lines holds several sub-rows.
                for line in cells[0].splitlines():
                    parts = re.split(r"\s+", line.strip(), maxsplit=1)
                    # A leading row number (``1 3D观察窗：倾角 …``) is not the
                    # row name; the Report often drops that column.
                    if len(parts) == 2 and re.fullmatch(r"\d{1,3}", parts[0]):
                        parts = re.split(r"\s+", parts[1], maxsplit=1)
                    if len(parts) == 2:
                        rows.append((_plain(parts[0]), parts[1], table))
                continue
            if len(cells) >= 3 and re.fullmatch(r"\d{1,3}", cells[0]):
                cells = cells[1:]
            rows.append((_plain(cells[0]), " ".join(cells[1:]), table))
    return rows, header


def compare_table_cells(
    ptr_node: Mapping[str, Any],
    report_node: Mapping[str, Any],
    headers: set[str] | None = None,
) -> dict[str, Any] | None:
    """Compare a PTR table with the Report's table by row name.

    Column layout may differ (the Report may split one PTR table into a left
    and a right table, or add a ``关节编号`` column); what must hold is that
    every PTR row name appears in the Report with the same values (in
    order), and every Report data row belongs to a PTR row.
    """

    from mvp.ptr_compare import _text_diff_segments

    labels = {compact_label(label) for label in (ptr_node.get("references") or {}).get("tables", [])}
    noise = {_plain(h) for h in (headers or set())}
    ptr_rows, header = _ptr_table_rows(ptr_node, labels, noise)
    if len(ptr_rows) < 2:
        # Captions that did not attach to their grid (continued tables,
        # captions above a page break): use the tables printed on the
        # clause's own pages.
        ptr_rows, header = _ptr_table_rows(
            {**ptr_node, "table_rows": [t for t in ptr_node.get("table_rows") or [] if t.get("binding") == "physical"]},
            set(),
            noise,
        )
    if len(ptr_rows) < 2:
        return None
    keys = {key for key, _, _ in ptr_rows}
    # Preserve punctuation in the actual printed row names. Normalized keys
    # alone must not make distinct names (e.g. A/B and AB) interchangeable.
    printed_keys: dict[str, set[str]] = {}
    printed_values: dict[str, set[tuple[str, ...]]] = {}
    for key, _, ptr_table in ptr_rows:
        for raw_row in _rows_with_merged_cells(ptr_table):
            printed_cells = [str(c) for c in raw_row if str(c or "").strip()]
            if len(printed_cells) >= 3 and re.fullmatch(r"\d{1,3}", printed_cells[0]):
                printed_cells = printed_cells[1:]
            if printed_cells and _plain(printed_cells[0]) == key:
                printed_keys.setdefault(key, set()).add(compact_label(printed_cells[0]))
                printed_values.setdefault(key, set()).add(tuple(_plain(c) for c in printed_cells[1:]))
    report_values: dict[str, list[str]] = {}
    report_raw: dict[str, list[str]] = {}
    identity_evidence: dict[str, list[dict[str, Any]]] = {}
    identity_column_checks: dict[str, list[bool]] = {}
    unmatched_report: list[str] = []
    data_rows = 0
    for table in report_node.get("table_rows") or []:
        row = table.get("matrix_row")
        if row is None:
            continue
        cells = [str(c or "") for c in row]
        plain_cells = [_plain(c) for c in cells]
        # A remarks cell may supply a missing row name, never a requirement
        # value. Require the same physical row, page and exact source hash.
        raw_external_key = compact_label(table.get("row_key"))
        external_keys = [key for key, labels in printed_keys.items()
                         if labels == {raw_external_key}]
        external_key = external_keys[0] if len(external_keys) == 1 else ""
        identity = table.get("row_key_evidence") or {}
        source_hash = table.get("source_sha256")
        source_matches_node = report_node.get("source_sha256") == source_hash
        identity_valid = (
            external_key in keys and source_matches_node
            and isinstance(source_hash, str) and bool(re.fullmatch(r"[a-f0-9]{64}", source_hash))
            and identity.get("role") == "report" and identity.get("column") == "备注"
            and identity.get("extraction_method") == "report_table_row_identity"
            and identity.get("source_sha256") == source_hash
            and identity.get("pdf_page") == table.get("pdf_page")
            and identity.get("physical_table_id") == table.get("physical_table_id")
            and bool(identity.get("physical_table_id"))
            and identity.get("row_index") == table.get("row_index")
            and isinstance(identity.get("row_index"), int)
            and bool(identity.get("bbox"))
            and compact_label(identity.get("text")) == raw_external_key
        )
        if identity_valid and not any(value in keys for value in plain_cells):
            # Binding a row name must not join distinct columns into one
            # string that happens to equal the PTR. Ambiguous layouts remain
            # manual for this row only; a proven mismatch stays a mismatch.
            expected_columns = printed_values.get(external_key, set())
            identity_column_checks.setdefault(external_key, []).append(
                len(expected_columns) == 1 and tuple(plain_cells) in expected_columns)
            cells.insert(0, str(table["row_key"]))
            plain_cells.insert(0, external_key)
            identity_evidence.setdefault(external_key, []).append(identity)
        if not any(plain_cells):
            continue
        key_index = next((i for i, value in enumerate(plain_cells) if value in keys), None)
        if key_index is None:
            first = next(c for c in cells if c.strip())
            non_empty = [c for c in cells if c.strip()]
            if len(non_empty) == 1 and re.search(r"[:：]\s*$", non_empty[0]):
                continue  # block heading such as ``2 号器械臂：``
            header_chars = set(header)
            if re.match(r"^\s*表\s*[A-Za-z]?\d", first) or all(
                not value or set(value) <= header_chars for value in plain_cells
            ):
                continue  # caption or (re-printed) header row
            unmatched_report.append((" | ".join(c.strip() for c in cells if c.strip()), [p for p in plain_cells if p]))
            continue
        data_rows += 1
        key = plain_cells[key_index]
        value = "".join(plain_cells[key_index + 1:])
        report_values.setdefault(key, []).append(value)
        report_raw.setdefault(key, []).append(" ".join(c.strip() for c in cells[key_index + 1:] if c.strip()))
    if data_rows == 0:
        return None
    grouped: dict[str, list[tuple[str, dict]]] = {}
    for key, value, table in ptr_rows:
        grouped.setdefault(key, []).append((value, table))
    cells_out: list[dict[str, Any]] = []
    for key, items in grouped.items():
        value = " / ".join(item[0] for item in items)
        table = items[0][1]
        ptr_value = "".join(_plain(item[0]) for item in items)
        if len(key) < 2:
            cells_out.append({"column": key, "report": " / ".join(report_raw.get(key, [])), "ptr": value,
                              "status": "manual", "reason_code": "PTR_ROW_NOT_PARSED", "ptr_page": table.get("pdf_page")})
            continue
        got = report_values.get(key)
        report_text = " / ".join(report_raw.get(key, []))
        if not got:
            cells_out.append({"column": key, "report": "", "ptr": value, "status": "mismatch",
                              "reason_code": "TABLE_ROW_MISSING_IN_REPORT", "ptr_page": table.get("pdf_page")})
            continue
        joined = "".join(got)
        if re.search(r"[±+\-~～≤≥<>至或]\s*$", value.strip()) and joined != ptr_value:
            # The PTR cell ends with an operator: its value continues on a
            # line the table recovery did not attach.
            cells_out.append({"column": key, "report": report_text, "ptr": value, "status": "manual",
                              "reason_code": "CELL_PTR_CONTINUES", "ptr_page": table.get("pdf_page")})
            continue
        if joined == ptr_value or all(item == ptr_value for item in got):
            status, reason = "matched", "CELL_EQUAL"
        elif _numeric_equal(joined, ptr_value) or all(_numeric_equal(item, ptr_value) for item in got):
            # Same value written differently (``0.70``/``0.7``): the Report
            # must copy the PTR figure exactly.
            status, reason = "mismatch", "CELL_NUMBER_WRITTEN_DIFFERENTLY"
        elif _DECIMAL_RE.findall(joined) != _DECIMAL_RE.findall(ptr_value):
            status, reason = "mismatch", "CELL_VALUE_MISMATCH"
        else:
            status, reason = "manual", "CELL_TEXT_DIFFERENT"
        if status == "matched" and key in identity_column_checks and not all(identity_column_checks[key]):
            status, reason = "manual", "REMARK_BOUND_COLUMN_VALUES_NOT_VERIFIED"
        cell = {"column": key, "report": report_text, "ptr": value, "status": status, "reason_code": reason,
                "ptr_page": table.get("pdf_page")}
        if key in identity_evidence:
            cell["report_row_identity_evidence"] = identity_evidence[key]
        if status != "matched":
            segments = _text_diff_segments(report_text, value)
            cell["report_segments"] = segments.get("report_segments")
            cell["ptr_segments"] = segments.get("ptr_segments")
        cells_out.append(cell)
    # Rows the PTR prints as running text rather than as a grid (merged
    # cells such as ``柔性旋转 / 1号臂 100° / 2号臂 105°``): accept a Report row
    # only when every one of its cells is printed in the PTR clause.
    from mvp.ptr_compare import _strip_punctuation, normalize_comparison_text

    ptr_plain = _strip_punctuation(normalize_comparison_text(
        str(ptr_node.get("text") or "") + "".join(
            str(c or "") for t in ptr_node.get("table_rows") or [] for row in t.get("rows") or [] for c in row
        )
    ))
    for text, parts in unmatched_report:
        if len(parts) > 2 and len(parts[0]) <= 3:
            parts = parts[1:]  # leading joint/row code such as ``θ1`` or ``d3``
        found = bool(parts) and all(part in ptr_plain for part in parts) and any(_DECIMAL_RE.search(part) for part in parts)
        cells_out.append({
            "column": "Report 表格行",
            "report": text,
            "ptr": "（PTR 中以正文形式出现）" if found else "",
            "status": "matched" if found else "manual",
            "reason_code": "ROW_CELLS_IN_PTR_TEXT" if found else "TABLE_ROW_NOT_IN_PTR",
        })
    statuses = {cell["status"] for cell in cells_out}
    status = "mismatch" if "mismatch" in statuses else "manual" if "manual" in statuses else "matched"
    first = ptr_rows[0][2]
    return {
        "rule": TABLE_RULE_ID,
        "status": status,
        "row_label": "、".join(sorted(labels)) or str(first.get("label") or "表格"),
        "row_key": "",
        "ptr_location": {"pdf_page": first.get("pdf_page"), "bbox": first.get("bbox")},
        "cells": cells_out,
        "consumed_report_lines": [],
    }


def compact_label(value: Any) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value or "")))
