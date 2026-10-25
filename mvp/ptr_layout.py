"""Geometry-based PTR requirements and Report requirement-column extraction.

Every observation retains its physical source. Reading order is computed from
line positions rather than PDF object insertion order. Report results are
never read into the requirements channel.
"""
from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover - compatibility with older environments
    import fitz

from mvp.checker import compact, header_map, is_formal_report_table, report_page_tables, sha256_file

PARSER_VERSION = "ptr-layout-1.0"
CLAUSE = re.compile(r"^\s*(2(?:\s*\.\s*\d+){1,8})(?:\s+|[、:：．]\s*)(\S.*)$")
STOP = re.compile(r"^\s*3(?:[.．、:]|\s)\s*(?:检验|试验|测试|检测|Test|Inspection)", re.I)
CAPTION = re.compile(r"^\s*(表\s*[A-Za-z]?\d+(?:[.\-－]\d+)*|附录\s*[A-Za-z])(?:\s|[\u3400-\u9fff]|$)")


def _box(rect: Any) -> list[float]:
    return [round(float(value), 3) for value in rect]


def evidence(role: str, page: int, bbox: Any, text: str, digest: str, method: str = "pdf_layout") -> dict[str, Any]:
    return {"role": role, "pdf_page": page, "page_index": page - 1, "bbox": _box(bbox),
            "source_sha256": digest, "coordinate_space": "pdf_points", "text": text,
            "semantic_role": f"{role}_requirement", "extraction_method": method}


def page_lines(page: fitz.Page) -> list[dict[str, Any]]:
    """Join fragments of one visual line, preserving table-cell geometry."""
    lines = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            text = "".join(span["text"] for span in line["spans"]).strip()
            if text:
                lines.append({"text": text, "bbox": _box(line["bbox"]),
                              "size": max(span["size"] for span in line["spans"])})
    rows: list[list[dict[str, Any]]] = []
    for line in sorted(lines, key=lambda v: (v["bbox"][1], v["bbox"][0])):
        center = (line["bbox"][1] + line["bbox"][3]) / 2
        if rows:
            other = rows[-1][0]
            other_center = (other["bbox"][1] + other["bbox"][3]) / 2
            if abs(center - other_center) <= min(3.0, line["size"] * .24):
                rows[-1].append(line)
                continue
        rows.append([line])
    result = []
    for row in rows:
        row.sort(key=lambda v: v["bbox"][0])
        result.append({"text": " ".join(v["text"] for v in row),
                       "bbox": [min(v["bbox"][0] for v in row), min(v["bbox"][1] for v in row),
                                max(v["bbox"][2] for v in row), max(v["bbox"][3] for v in row)],
                       "size": max(v["size"] for v in row), "fragments": row})
    return result


def _inside(a: Any, b: Any) -> bool:
    box = fitz.Rect(a)
    return fitz.Rect(b).contains(fitz.Point((box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2))


def _enrich(node: dict[str, Any]) -> dict[str, Any]:
    # Imported at call time so the comparison facade can expose the legacy API.
    from mvp.ptr_compare import _numeric_signature, _raw_numeric_signature, _references, _text_for_comparison, _unit_labels
    node["text"] = "\n".join(node.pop("parts", [node.get("text", "")]))
    node["normalized_text"] = _text_for_comparison(node["text"])
    node["numeric_signature"] = _numeric_signature(node["text"])
    node["raw_numeric_signature"] = _raw_numeric_signature(node["text"])
    node["references"] = _references(node["text"])
    node["unit_labels"] = _unit_labels(node["text"])
    node["is_heading_only"] = node["normalized_text"] == _text_for_comparison(node["title"])
    node["pages"] = sorted({v["pdf_page"] for v in node["evidence"]})
    return node


def _new_node(role: str, clause: str, title: str, digest: str, suffix: str = "") -> dict[str, Any]:
    return {"node_id": f"{role}:{suffix}clause:{clause}", "role": role, "clause_id": clause,
            "parent_clause_id": clause.rsplit(".", 1)[0], "title": title, "parts": [title] if title else [],
            "source_sha256": digest, "evidence": [], "table_rows": [], "figures": [],
            "extraction_issues": []}


def _table(page: fitz.Page, table: Any, digest: str, lines: Sequence[Mapping[str, Any]], index: int) -> dict[str, Any]:
    rows = table.extract()
    preceding = [line for line in lines if line["bbox"][3] <= table.bbox[1] + 2
                 and table.bbox[1] - line["bbox"][3] < 65 and CAPTION.match(line["text"])]
    caption = preceding[-1]["text"] if preceding else ""
    label = CAPTION.match(caption)
    return {"table_id": f"ptr:table:p{page.number+1}:{index}", "pdf_page": page.number+1,
            "caption": caption, "label": compact(label.group(1)) if label else None,
            "bbox": _box(table.bbox), "rows": [[str(v or "") for v in row] for row in rows],
            "cell_boxes": [[_box(cell) if cell is not None else None for cell in row.cells] for row in table.rows],
            "source_sha256": digest, "extraction_method": "pymupdf_grid", "binding": "physical"}


def extract_ptr_layout(path: Path, *, ocr_mode: str = "auto") -> dict[str, Any]:
    from mvp.ptr_ocr import iter_ptr_ocr_lines
    digest = sha256_file(path)
    nodes: list[dict[str, Any]] = []
    tables: list[dict[str, Any]] = []
    current = None
    stopped = False
    total_text = 0
    with fitz.open(path) as doc:
        page_count = len(doc)
        for page in doc:
            total_text += len(page.get_text())
            lines = page_lines(page)
            raw_tables = list(page.find_tables(strategy="lines_strict").tables)
            page_tables = [_table(page, t, digest, lines, i) for i, t in enumerate(raw_tables)]
            # Do not mistake tables in test methods for inline requirements.
            events = []
            for line in lines:
                if re.fullmatch(r"\d+(?:\s*/\s*\d+)?", line["text"]) and (
                    line["bbox"][1] > page.rect.height * .90 or line["bbox"][3] < page.rect.height * .07
                ):
                    continue
                fragments = [v for v in line["fragments"] if not any(_inside(v["bbox"], t["bbox"]) for t in page_tables)]
                if not fragments:
                    continue
                events.append({**line, "text": " ".join(v["text"] for v in fragments), "kind": "text"})
            events.extend({**table, "kind": "table", "text": table["caption"]} for table in page_tables)
            events.sort(key=lambda v: (v["bbox"][1], v["bbox"][0]))
            for event in events:
                text = event["text"]
                if event["kind"] == "table":
                    tables.append(event)
                    if current is not None and not stopped:
                        current["table_rows"].append(event)
                        current["evidence"].append(evidence("ptr", page.number+1, event["bbox"],
                            "\n".join(" | ".join(r) for r in event["rows"]), digest, "pymupdf_grid"))
                    continue
                if STOP.match(text):
                    stopped = True
                    current = None
                if stopped:
                    continue
                match = CLAUSE.match(text)
                if match and event["bbox"][0] < page.rect.width * .32:
                    clause = compact(match.group(1))
                    title = match.group(2).strip()
                    # Table decimals and unit tokens are not clause headings.
                    if not re.fullmatch(r"(?:[munpμkMG]?)(?:V|A|s|sec|Hz|Ω|W)\b.*", title):
                        current = _new_node("ptr", clause, title, digest)
                        nodes.append(current)
                        current["evidence"].append(evidence("ptr", page.number+1, event["bbox"], text, digest))
                        continue
                if current is not None:
                    current["parts"].append(text)
                    current["evidence"].append(evidence("ptr", page.number+1, event["bbox"], text, digest))
                    if re.match(r"^图\s*[0-9]+", text):
                        current["figures"].append({"caption": text, "pdf_page": page.number+1, "bbox": event["bbox"]})
    ocr = {"status": "not_requested", "used_backends": [], "reason_code": None}
    if (not nodes or ocr_mode == "required") and ocr_mode != "never":
        # OCR channels are alternative observations, never concatenated as a
        # single source. Keep the primary channel for segmentation and retain
        # all observations for audit/review.
        observations, ocr = iter_ptr_ocr_lines(path)
        from mvp.ptr_compare import _parse_clause_lines
        channels = ocr.get("used_backends", [])
        primary = next((name for name in channels if any(v[3] == f"ocr:{name}" for v in observations)), None)
        selected = [v for v in observations if v[3] == f"ocr:{primary}"]
        if selected:
            ocr_nodes, _ = _parse_clause_lines(selected, role="ptr", source_sha256=digest)
            if ocr_nodes:
                nodes = ocr_nodes
                ocr["primary_backend"] = primary
                ocr["observations"] = [list(v) for v in observations]
                for node in nodes:
                    node.update(table_rows=[], figures=[], extraction_issues=["OCR_REVIEW_REQUIRED"])
    for node in nodes:
        if "parts" in node:
            _enrich(node)
        # Resolve a reference to a previously printed table using the caption,
        # keeping duplicate captions ambiguous instead of picking arbitrarily.
        for label in node["references"].get("tables", []):
            candidates = [t for t in tables if compact(t.get("label") or "") == compact(label)]
            local = [t for t in node["table_rows"] if t.get("label") == compact(label)]
            if not local and len(candidates) == 1:
                node["table_rows"].append({**candidates[0], "binding": "reference"})
    duplicates = [c for c in {n["clause_id"] for n in nodes} if sum(n["clause_id"] == c for n in nodes) > 1]
    for node in nodes:
        if node["clause_id"] in duplicates:
            node["extraction_issues"].append("DUPLICATE_PTR_CLAUSE")
    ocr_used = bool(nodes) and any(str(e.get("extraction_method", "")).startswith("ocr:") for n in nodes for e in n["evidence"])
    return {"role": "ptr", "path": str(path), "source_sha256": digest, "nodes": nodes,
            "diagnostics": {"parser_version": PARSER_VERSION, "page_count": page_count, "text_length": total_text,
                "candidate_count": len(nodes), "table_count": len(tables), "text_layer": bool(total_text),
                "status": "ocr_manual" if ocr_used else "parsed" if nodes else "manual", "ocr_used": ocr_used,
                "reason_code": "PTR_OCR_CANDIDATES_MANUAL" if ocr_used else None if nodes else "PTR_TEXT_LAYER_MISSING",
                "duplicates": duplicates, "ocr": ocr}}


def _requirement_cells(page: fitz.Page, table: Any, row_index: int, left: float, right: float) -> list[dict[str, Any]]:
    result = []
    seen = set()
    for cell in table.rows[row_index].cells:
        if cell is None or tuple(cell) in seen:
            continue
        seen.add(tuple(cell))
        rect = fitz.Rect(cell)
        # The Report requirement is sometimes a merged cell spanning the
        # hidden second requirement column. Use the result column's left edge
        # as the ownership boundary; testing x1 would discard that cell.
        if rect.x0 < left - 1.5 or rect.x0 >= right - 1.5:
            continue
        text = page.get_text("text", clip=rect).strip()
        result.append({"text": text, "bbox": _box(rect)})
    return sorted(result, key=lambda c: c["bbox"][0])


def extract_report_layout(path: Path) -> dict[str, Any]:
    from mvp.ptr_scope import extract_report_scope
    from mvp.checker import align_report_table_row
    digest = sha256_file(path)
    groups: dict[tuple[int, str], dict[str, Any]] = {}
    nodes = []
    active_sequence = None
    active_clause = ""
    product_count = 0
    with fitz.open(path) as doc:
        for page in doc:
            for table in report_page_tables(page):
                if not is_formal_report_table(table):
                    continue
                extracted = table.extract()
                mapping = header_map(extracted[0])
                header = table.rows[0].cells
                left = float(header[mapping["标准要求"]][0])
                right = float(header[mapping["检验结果"]][0])
                for row_index, raw in enumerate(extracted[1:], 1):
                    row = align_report_table_row(table, row_index, raw)
                    seq = re.fullmatch(r"(?:续)?(\d+)", compact(row[mapping["序号"]]))
                    if seq:
                        active_sequence = int(seq.group(1))
                        active_clause = compact(row[mapping["标准条款"]])
                    if active_sequence is None or not re.fullmatch(r"2(?:\.\d+)+[a-z]?", active_clause, re.I):
                        continue
                    key = active_sequence, active_clause
                    if key not in groups:
                        product_count += 1
                        title = str(row[mapping["检验项目"]] or "").strip()
                        node = _new_node("report", active_clause, title, digest, f"item:{active_sequence}:")
                        node["sequence"] = active_sequence
                        groups[key] = {"parent": node, "current": node, "nodes": {active_clause: node}, "table": []}
                        nodes.append(node)
                    group = groups[key]
                    cells = _requirement_cells(page, table, row_index, left, right)
                    if not cells:
                        continue
                    text = "\n".join(cell["text"] for cell in cells if cell["text"])
                    # A multi-column row belongs to the active requirement
                    # table. All physical columns, including blank values,
                    # survive; merged keys are inherited only by table logic.
                    if len(cells) > 1 and not any(CLAUSE.match(t.strip()) for t in text.splitlines()):
                        group["current"]["table_rows"].append({"pdf_page": page.number+1, "cells": cells,
                            "rows": [[cell["text"] for cell in cells]], "source_sha256": digest,
                            "extraction_method": "report_requirement_columns", "binding": "physical"})
                        group["current"]["evidence"].extend(evidence("report", page.number+1, c["bbox"], c["text"], digest,
                                "report_requirement_cell") for c in cells)
                        continue
                    for cell in cells:
                        for line in cell["text"].splitlines():
                            line = line.strip()
                            if not line:
                                continue
                            match = CLAUSE.match(line)
                            if match and (compact(match.group(1)) == active_clause or compact(match.group(1)).startswith(active_clause + ".")):
                                clause, title = compact(match.group(1)), match.group(2).strip()
                                if clause not in group["nodes"]:
                                    node = _new_node("report", clause, title, digest, f"item:{active_sequence}:")
                                    node["sequence"] = active_sequence
                                    group["nodes"][clause] = node
                                    nodes.append(node)
                                else:
                                    node = group["nodes"][clause]
                                    if compact(title) != compact(node["title"]):
                                        node["extraction_issues"].append("CONFLICTING_REPORT_HEADING")
                                group["current"] = node
                            else:
                                group["current"]["parts"].append(line)
                            group["current"]["evidence"].append(evidence("report", page.number+1, cell["bbox"], line, digest,
                                    "report_requirement_cell"))
                    if not group["parent"]["evidence"]:
                        group["parent"]["evidence"].append(evidence("report", page.number+1,
                            [left, cells[0]["bbox"][1], right, cells[-1]["bbox"][3]], group["parent"]["title"], digest))
    by_clause: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for node in nodes:
        _enrich(node)
        by_clause[node["clause_id"]].append(node)
    scope = extract_report_scope(path)
    return {"role": "report", "path": str(path), "source_sha256": digest, "nodes": nodes,
            "nodes_by_clause": dict(by_clause), "scope": scope,
            "diagnostics": {"parser_version": PARSER_VERSION, "product_item_count": product_count,
                "product_node_count": len(nodes), "status": "parsed" if nodes else "manual",
                "reason_code": None if nodes else "REPORT_PRODUCT_ITEMS_MISSING", "scope_status": scope["status"]}}
