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

PARSER_VERSION = "ptr-layout-1.4"
CLAUSE = re.compile(
    r"^\s*(2(?:\s*\.\s*\d+){1,8})"
    r"(?:\s+|[、，,:：)）]\s*|[.．](?!\s*\d)\s*|(?=[\u3400-\u9fff]))(\S.*)$"
)
STOP = re.compile(r"^\s*3(?:[.．、:]|\s)\s*(?:检验|试验|测试|检测|Test|Inspection)", re.I)
CAPTION = re.compile(r"^\s*(表\s*[A-Za-z]?\d+(?:[.\-－]\d+)*|附录\s*[A-Za-z])(?:\s|[:：]|[\u3400-\u9fff]|$)")
# Sample/model sub-row labels printed in the Report requirement cell
# (``470066 左路``).  A model followed by a requirement (``470066：0°±3°。``)
# is requirement text and must be kept.
RESULT_QUALIFIER = re.compile(r"^\s*\d{5,8}\s*(?:[:：]\s*)?(?:左路|右路)?\s*$|^\s*(?:左路|右路)\s*$")
# A dotted number followed by a measure word is a value (``2.85 个周期/°``),
# not a clause heading.
VALUE_NOT_CLAUSE = re.compile(r"^(?:个|倍|周期|像素|%|°)(?![\u3400-\u9fff]{0,2}(?:要求|性能|功能|防护))")
PAGE_COUNTER = re.compile(
    r"^(?:"
    r"第\s*\d+\s*页(?:[，,]?\s*(?:共\s*\d+\s*页?|/\s*(?:共\s*)?\d+\s*页?))"
    r"|第\s*\d+\s*/\s*\d+\s*页"
    r"|\d+\s*/\s*\d+\s*页?"
    r"|page\s*\d+\s*of\s*\d+"
    r")$",
    re.IGNORECASE,
)


def is_page_counter_line(value: Any) -> bool:
    """Return whether a line is a printed page counter, not requirement text."""

    text = str(value or "").strip().replace("，", ",")
    text = re.sub(r"\s+", "", text)
    return bool(PAGE_COUNTER.fullmatch(text))


def _clean_requirement_text(value: str) -> str:
    """Remove sample-result qualifiers that were printed in the requirement cell."""
    lines = []
    for line in str(value or "").splitlines():
        line = line.strip()
        if not line or RESULT_QUALIFIER.match(line):
            continue
        lines.append(line)
    return "\n".join(lines)


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
    from mvp.ptr_compare import (
        _numeric_signature,
        _raw_numeric_signature,
        _references,
        _split_report_auxiliary_lines,
        _text_for_comparison,
        _unit_labels,
    )
    role = str(node.get("role") or "")
    node["text"] = "\n".join(node.pop("parts", [node.get("text", "")]))
    node["normalized_text"] = _text_for_comparison(node["text"], role=role)
    node["numeric_signature"] = _numeric_signature(node["text"], role=role)
    node["raw_numeric_signature"] = _raw_numeric_signature(node["text"], role=role)
    node["references"] = _references(node["text"])
    node["reference_bindings"] = _reference_bindings(node)
    node["unit_labels"] = _unit_labels(node["text"])
    node["auxiliary_lines"] = (
        _split_report_auxiliary_lines(node["text"])[1] if role == "report" else []
    )
    node["is_heading_only"] = node["normalized_text"] == _text_for_comparison(node["title"], role=role)
    node["pages"] = sorted({v["pdf_page"] for v in node["evidence"]})
    return node


def _reference_bindings(node: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Describe whether each table/appendix token has physical evidence."""

    references = (node.get("references") or {}).get("tables", [])
    tables = list(node.get("table_rows") or [])
    bindings: list[dict[str, Any]] = []
    for label in references:
        wanted = compact(label)
        candidates = []
        for table in tables:
            observed = compact(str(table.get("label") or table.get("caption") or ""))
            if wanted and wanted in observed:
                candidates.append(table)
        # A table continued over several pages is one target.
        groups = {str(t.get("table_group") or t.get("table_id")) for t in candidates}
        if len(candidates) == 1 or (candidates and len(groups) == 1):
            status = "bound"
        elif len(candidates) > 1:
            status = "ambiguous"
        elif tables and str(node.get("role") or "") == "report" and wanted in compact(str(node.get("text") or "")):
            # Report requirement rows may not carry a caption, but their
            # physical matrix is still evidence for a token printed in the
            # same requirement cell.
            status = "bound_physical_rows"
            candidates = tables
        else:
            status = "unresolved"
        bindings.append({
            "label": label,
            "status": status,
            "table_ids": [str(table.get("table_id")) for table in candidates if table.get("table_id")],
        })
    return bindings


def _new_node(role: str, clause: str, title: str, digest: str, suffix: str = "") -> dict[str, Any]:
    return {"node_id": f"{role}:{suffix}clause:{clause}", "role": role, "clause_id": clause,
            "parent_clause_id": clause.rsplit(".", 1)[0], "title": title, "parts": [title] if title else [],
            "source_sha256": digest, "evidence": [], "table_rows": [], "figures": [],
            "extraction_issues": []}


def _split_inline_clauses(nodes: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Split clause headings that a PDF text block placed on one line."""
    from mvp.ptr_compare import _canonical_clause_id, _clause_parent, _split_inline_observations

    def segment_sources(node: Mapping[str, Any], segments: Sequence[dict[str, Any]], base_clause: str) -> None:
        """Give each inline child only the evidence and tables it owns."""

        if len(segments) <= 1:
            return
        observations: list[dict[str, Any]] = []
        for value in node.get("evidence", []) or []:
            observations.append({"_kind": "evidence", "_value": value, **dict(value)})
        for value in node.get("table_rows", []) or []:
            observations.append({"_kind": "table", "_value": value, **dict(value)})
        assigned = _split_inline_observations(observations, segments, base_clause)
        for segment in segments:
            clause_id = str(segment.get("clause_id") or "")
            if clause_id == _canonical_clause_id(base_clause):
                # Parent nodes remain the complete auditable source.  Only
                # independently addressable child nodes are narrowed.
                continue
            values = assigned.get(clause_id, [])
            segment["evidence"] = [item["_value"] for item in values if item.get("_kind") == "evidence"]
            segment["table_rows"] = [item["_value"] for item in values if item.get("_kind") == "table"]

    marker = re.compile(
        r"(?<![\w.])(?P<clause>2(?:\s*\.\s*\d+){1,8})"
        r"(?=(?:\s+|[.．、，,:：;；)）]\s*)?[\u3400-\u9fffA-Za-z])"
    )
    existing_ids = {str(node.get("clause_id")) for node in nodes}
    result: list[dict[str, Any]] = []
    for node in nodes:
        raw = "\n".join(node.get("parts", [node.get("text", "")]))
        # Keep indexes aligned with the match spans.  Noise is removed later
        # by the comparison normalizer; segmentation must use source text.
        raw_for_context = raw
        current_clause = _canonical_clause_id(str(node.get("clause_id") or ""))

        def is_heading_boundary(match: re.Match[str]) -> bool:
            if match.start() <= 0:
                return False
            previous = raw_for_context[match.start() - 1]
            if previous not in "\r\n 、,，;；。.:：)）":
                return False
            # A reference can be placed after punctuation or on its own line:
            # ``符合：2.1.4 的要求`` must remain part of the current body.
            prefix = raw_for_context[:match.start()].rstrip()
            if re.search(r"(?:符合|满足|见|第|条款|章节|引用|参见|按照)\s*[:：,，;；。．、.)]?\s*$", prefix[-32:]):
                return False
            candidate = _canonical_clause_id(match.group("clause"))
            if candidate == current_clause or candidate in existing_ids:
                return False
            # A newly discovered inline item should be a child or a sibling
            # of the node whose text swallowed it.  This rejects unrelated
            # cross-references that happen to start a new extracted line.
            current_parent = _clause_parent(current_clause)
            return candidate.startswith(current_clause + ".") or _clause_parent(candidate) == current_parent

        matches = [
            m for m in marker.finditer(raw)
            if is_heading_boundary(m)
        ]
        if not matches:
            result.append(node)
            continue
        boundaries = [0] + [match.start() for match in matches]
        clauses = [node.get("clause_id", "")] + [_canonical_clause_id(m.group("clause")) for m in matches]
        segments: list[dict[str, Any]] = []
        for index, (start, clause) in enumerate(zip(boundaries, clauses)):
            end = boundaries[index + 1] if index + 1 < len(boundaries) else len(raw)
            if index == 0:
                body = raw[start:end].strip()
                child = dict(node)
                child["parts"] = [body]
            else:
                marker_match = matches[index - 1]
                body = raw[marker_match.end():end]
                body = re.sub(r"^\s*", "", body)
                body = re.sub(r"^[.．、，,:：;；)）]\s*", "", body)
                body = body.strip()
                child = dict(node)
                role = str(node.get("role") or "ptr")
                child["clause_id"] = clause
                child["parent_clause_id"] = _clause_parent(clause)
                sequence = child.get("sequence")
                child["node_id"] = (
                    f"{role}:item:{sequence}:clause:{clause}"
                    if role == "report" and sequence is not None
                    else f"{role}:clause:{clause}"
                )
                child["title"] = body.splitlines()[0] if body else clause
                child["parts"] = [body]
                child["table_rows"] = []
                child["figures"] = []
                child["extraction_issues"] = ["INLINE_CLAUSE_SPLIT"]
            segments.append(child)
        segment_sources(node, segments, current_clause)
        result.extend(segments)
    # Many PTRs write lettered sub-items as ``a) ...`` on separate lines
    # inside one numeric clause.  The Report scope uses the stable form
    # ``2.3.6.6a``; split those visual sub-items into the same identity while
    # retaining the numeric parent as an auditable heading node.
    letter_marker = re.compile(r"(?m)^[ \t]*(?P<letter>[a-z])\s*[)）](?=\s*)", re.IGNORECASE)
    lettered: list[dict[str, Any]] = []
    for node in result:
        raw = "\n".join(node.get("parts", [node.get("text", "")]))
        current_clause = _canonical_clause_id(str(node.get("clause_id") or ""))
        if not current_clause or re.search(r"[a-z]$", current_clause, re.IGNORECASE):
            lettered.append(node)
            continue
        matches = [match for match in letter_marker.finditer(raw) if match.group("letter")]
        # Report requirement cells can be promoted to table observations.  In
        # that layout d)–i) are absent from ``parts`` even though their
        # evidence text has an exact marker and bbox.  Include those markers
        # when constructing child nodes instead of leaving them as
        # REPORT_CLAUSE_MISSING rows.
        evidence_bodies: dict[str, str] = {}
        evidence_order: list[str] = []
        for observation in sorted(
            node.get("evidence", []) or [],
            key=lambda value: (
                int(value.get("pdf_page") or 0),
                float((value.get("bbox") or [0, 0, 0, 0])[1]),
                float((value.get("bbox") or [0, 0, 0, 0])[0]),
            ),
        ):
            text = str(observation.get("text") or "")
            for marker_match in letter_marker.finditer(text):
                letter = str(marker_match.group("letter") or "").lower()
                if not letter or letter in evidence_bodies:
                    continue
                body = text[marker_match.end():]
                body = re.sub(r"^\s*", "", body)
                body = re.sub(r"^[.．、，,:：;；)）]\s*", "", body).strip()
                if body:
                    evidence_bodies[letter] = body
                    evidence_order.append(letter)
        raw_order = [str(match.group("letter") or "").lower() for match in matches]
        letter_order: list[str] = []
        for letter in [*raw_order, *evidence_order]:
            if letter and letter not in letter_order:
                letter_order.append(letter)
        if not letter_order:
            lettered.append(node)
            continue
        segments: list[dict[str, Any]] = []
        prefix = raw[: matches[0].start()].strip() if matches else raw.strip()
        base = dict(node)
        # Keep the parent's complete source text for backwards-compatible
        # evidence and parent-level comparisons; children provide the
        # independently addressable lettered identities below.
        base["parts"] = [raw]
        base["title"] = (prefix.splitlines()[0] if prefix else str(node.get("title") or current_clause)).strip()
        base["extraction_issues"] = [*node.get("extraction_issues", []), "LETTERED_CLAUSE_PARENT"]
        segments.append(base)
        raw_by_letter = {str(match.group("letter") or "").lower(): (index, match) for index, match in enumerate(matches)}
        for letter in letter_order:
            raw_entry = raw_by_letter.get(letter)
            if raw_entry is not None:
                index, match = raw_entry
                end = matches[index + 1].start() if index + 1 < len(matches) else len(raw)
                body = raw[match.end():end].strip()
            else:
                body = evidence_bodies.get(letter, "")
            if not body or body.startswith("～") or body.startswith("~"):
                continue
            clause = f"{current_clause}{letter}"
            child = dict(node)
            role = str(node.get("role") or "ptr")
            sequence = child.get("sequence")
            child["clause_id"] = clause
            child["parent_clause_id"] = current_clause
            child["node_id"] = (
                f"{role}:item:{sequence}:clause:{clause}"
                if role == "report" and sequence is not None
                else f"{role}:clause:{clause}"
            )
            child["title"] = body.splitlines()[0] if body else clause
            child["parts"] = [body]
            child["table_rows"] = []
            child["figures"] = []
            child["extraction_issues"] = [*node.get("extraction_issues", []), "INLINE_LETTERED_CLAUSE_SPLIT"]
            segments.append(child)
        if len(segments) == 1:
            lettered.append(node)
        else:
            segment_sources(node, segments, current_clause)
            lettered.extend(segments)
    # A single Report sequence can legitimately contain repeated lettered
    # clauses from separate physical rows.  Keep the clause key for mapping,
    # but make the evidence identity unique so Coverage Ledger conservation
    # can distinguish the two observations instead of collapsing them.
    occurrences: dict[str, int] = {}
    for node in lettered:
        node_id = str(node.get("node_id") or "")
        occurrences[node_id] = occurrences.get(node_id, 0) + 1
        if occurrences[node_id] > 1:
            node["node_id"] = f"{node_id}:occurrence{occurrences[node_id]}"
    return lettered


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


def _label_continuation_tables(tables: list[dict[str, Any]]) -> None:
    """Give an uncaptioned table at the top of the next page its predecessor's label.

    A long PTR table continues across pages without repeating the caption.
    The continuation keeps its own physical evidence; only ``label`` and
    ``table_group`` are inherited so references can bind the whole table.
    """

    previous: dict[str, Any] | None = None
    for table in tables:
        if table.get("label"):
            table.setdefault("table_group", table.get("table_id"))
        elif (
            previous is not None
            and previous.get("label")
            and int(table.get("pdf_page", 0)) == int(previous.get("pdf_page", 0)) + 1
            and float(table["bbox"][1]) < 130
            and table.get("rows")
            and previous.get("rows")
            and len(table["rows"][0]) == len(previous["rows"][0])
        ):
            table["label"] = previous["label"]
            table["table_group"] = previous.get("table_group") or previous.get("table_id")
            table["continuation_of"] = previous.get("table_id")
        previous = table


def _ancestor_table_groups(
    node: Mapping[str, Any],
    by_clause: Mapping[str, Mapping[str, Any]],
    group_ids: set[str],
) -> list[str]:
    clause = str(node.get("clause_id") or "")
    while "." in clause:
        clause = clause.rsplit(".", 1)[0]
        ancestor = by_clause.get(clause)
        if not ancestor:
            continue
        found = sorted({
            str(t.get("table_group") or t.get("table_id"))
            for t in ancestor.get("table_rows", [])
            if str(t.get("table_group") or t.get("table_id")) in group_ids
        })
        if found:
            return found
    return []


def _cluster_starts(values: Sequence[float], tolerance: float = 14.0) -> list[float]:
    starts: list[float] = []
    for value in sorted(values):
        if not starts or value - starts[-1] > tolerance:
            starts.append(value)
    return starts


def _text_table(page: fitz.Page, caption: Mapping[str, Any], lines: Sequence[Mapping[str, Any]],
                digest: str, index: int) -> dict[str, Any] | None:
    """Recover a no-border table from text span x/y positions.

    The result is deliberately marked ``text_table``. It is evidence for
    review and exact fingerprinting only; a low-confidence or irregular table
    stays manual in the comparison layer.
    """
    caption_y = float(caption["bbox"][3])
    after = [line for line in lines if float(line["bbox"][1]) > caption_y + 2]
    end = float(page.rect.height) - 24
    for line in after:
        text = str(line.get("text") or "")
        if STOP.match(text) or CLAUSE.match(text):
            end = float(line["bbox"][1]) - 2
            break
    candidates = [line for line in after if float(line["bbox"][1]) < end and line.get("fragments")]
    if len(candidates) < 2:
        return None
    fragments = [fragment for line in candidates for fragment in line["fragments"]]
    starts = _cluster_starts([float(fragment["bbox"][0]) for fragment in fragments])
    if len(starts) < 2 or len(starts) > 8:
        return None
    grouped: list[list[dict[str, Any]]] = []
    for line in candidates:
        center = (float(line["bbox"][1]) + float(line["bbox"][3])) / 2
        if grouped:
            previous = grouped[-1]
            previous_center = sum((float(item["bbox"][1]) + float(item["bbox"][3])) / 2 for item in previous) / len(previous)
            if abs(center - previous_center) <= max(4.0, float(line.get("size", 10)) * .55):
                grouped[-1].extend(line["fragments"])
                continue
        grouped.append(list(line["fragments"]))
    rows: list[list[str]] = []
    row_boxes: list[list[float]] = []
    for row in grouped:
        cells = [""] * len(starts)
        boxes: list[Any] = []
        for fragment in sorted(row, key=lambda item: float(item["bbox"][0])):
            x = float(fragment["bbox"][0])
            column = min(range(len(starts)), key=lambda idx: abs(starts[idx] - x))
            cells[column] = (cells[column] + " " + str(fragment.get("text") or "")).strip()
            boxes.append(fragment["bbox"])
        if any(cells):
            rows.append(cells)
            row_boxes.append([min(float(box[0]) for box in boxes), min(float(box[1]) for box in boxes),
                              max(float(box[2]) for box in boxes), max(float(box[3]) for box in boxes)])
    if len(rows) < 2:
        return None
    return {
        "table_id": f"ptr:text-table:p{page.number+1}:{index}",
        "pdf_page": page.number + 1,
        "caption": str(caption.get("text") or ""),
        "label": compact(CAPTION.match(str(caption.get("text") or "")).group(1)) if CAPTION.match(str(caption.get("text") or "")) else None,
        "bbox": [min(float(caption["bbox"][0]), min(box[0] for box in row_boxes)),
                 float(caption["bbox"][1]), max(box[2] for box in row_boxes), max(box[3] for box in row_boxes)],
        "rows": rows,
        "cell_boxes": [],
        "source_sha256": digest,
        "extraction_method": "pymupdf_text_table",
        "binding": "physical",
        "confidence": "manual_review_required",
    }


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
            captions = [line for line in lines if CAPTION.match(str(line.get("text") or ""))]
            for caption_index, caption in enumerate(captions, start=len(page_tables)):
                if any(
                    abs(float(table["bbox"][1]) - float(caption["bbox"][3])) < 35
                    and float(table["bbox"][2]) >= float(caption["bbox"][0])
                    for table in page_tables
                ):
                    continue
                recovered = _text_table(page, caption, lines, digest, caption_index)
                if recovered is not None:
                    page_tables.append(recovered)
            # Do not mistake tables in test methods for inline requirements.
            events = []
            for line in lines:
                if is_page_counter_line(line["text"]) or (
                    re.fullmatch(r"\d+(?:\s*/\s*\d+)?", line["text"]) and (
                    line["bbox"][1] > page.rect.height * .90 or line["bbox"][3] < page.rect.height * .07
                    )
                ):
                    continue
                fragments = [v for v in line["fragments"] if not any(_inside(v["bbox"], t["bbox"]) for t in page_tables)]
                if not fragments:
                    continue
                events.append({**line, "text": " ".join(v["text"] for v in fragments), "kind": "text"})
            events.extend({**table, "kind": "table", "text": table["caption"]} for table in page_tables)
            events.sort(key=lambda v: (v["bbox"][1], v["bbox"][0]))
            clause_only = re.compile(r"^\s*(2(?:\s*\.\s*\d+){1,8})\s*$")
            merged_events: list[dict[str, Any]] = []
            pending_clause: dict[str, Any] | None = None
            for event in events:
                if event["kind"] != "text":
                    if pending_clause is not None:
                        merged_events.append(pending_clause)
                        pending_clause = None
                    merged_events.append(event)
                    continue
                if pending_clause is not None:
                    previous = pending_clause["bbox"]
                    current_box = event["bbox"]
                    same_visual_line = (
                        current_box[1] <= previous[3] + 3.0
                        and current_box[3] >= previous[1] - 3.0
                        and current_box[0] >= previous[2] - 2.0
                    )
                    if same_visual_line:
                        merged_events.append({
                            **event,
                            "text": f"{pending_clause['text']} {event['text']}",
                            "bbox": [
                                min(previous[0], current_box[0]),
                                min(previous[1], current_box[1]),
                                max(previous[2], current_box[2]),
                                max(previous[3], current_box[3]),
                            ],
                            "fragments": [*pending_clause.get("fragments", []), *event.get("fragments", [])],
                        })
                        pending_clause = None
                        continue
                    merged_events.append(pending_clause)
                    pending_clause = None
                if clause_only.match(str(event.get("text") or "")) and event["bbox"][0] < page.rect.width * .32:
                    pending_clause = event
                else:
                    merged_events.append(event)
            if pending_clause is not None:
                merged_events.append(pending_clause)
            events = merged_events
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
                    if not re.fullmatch(r"(?:[munpμkMG]?)(?:V|A|s|sec|Hz|Ω|W)\b.*", title) and not VALUE_NOT_CLAUSE.match(title):
                        current = _new_node("ptr", clause, title, digest)
                        nodes.append(current)
                        current["evidence"].append(evidence("ptr", page.number+1, event["bbox"], text, digest))
                        continue
                standalone = clause_only.match(text)
                if standalone and event["bbox"][0] < page.rect.width * .32:
                    clause = compact(standalone.group(1))
                    current = _new_node("ptr", clause, "", digest)
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
    nodes = _split_inline_clauses(nodes)
    _label_continuation_tables(tables)
    by_clause = {node["clause_id"]: node for node in nodes}
    for node in nodes:
        if "parts" in node:
            _enrich(node)
        # Resolve a reference to a previously printed table using the caption,
        # keeping duplicate captions ambiguous instead of picking arbitrarily.
        for label in node["references"].get("tables", []):
            key = compact(label)
            local = [t for t in node["table_rows"] if t.get("label") == key]
            if local:
                continue
            groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for table in tables:
                if compact(table.get("label") or "") == key:
                    groups[str(table.get("table_group") or table.get("table_id"))].append(table)
            binding = "reference"
            if len(groups) > 1:
                # Duplicate captions (e.g. two different “表1”): use the one
                # printed inside the referencing clause's own chapter.
                scoped = _ancestor_table_groups(node, by_clause, set(groups))
                groups = {gid: groups[gid] for gid in scoped}
                binding = "reference_scoped"
            if len(groups) == 1:
                for table in next(iter(groups.values())):
                    node["table_rows"].append({**table, "binding": binding})
        node["reference_bindings"] = _reference_bindings(node)
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


def _requirement_column_starts(table: Any, left: float, right: float) -> list[float]:
    """Return physical x starts for the merged Report requirement columns."""

    starts = {
        round(float(cell[0]), 3)
        for row in table.rows
        for cell in row.cells
        if cell is not None and float(cell[0]) >= left - 1.5 and float(cell[0]) < right - 1.5
    }
    return sorted(starts)


def _requirement_cells(
    page: fitz.Page,
    table: Any,
    row_index: int,
    left: float,
    right: float,
    starts: Sequence[float] = (),
) -> list[dict[str, Any]]:
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
        raw_text = page.get_text("text", clip=rect).strip()
        column_index = None
        if starts:
            nearest = min(range(len(starts)), key=lambda index: abs(float(starts[index]) - rect.x0))
            if abs(float(starts[nearest]) - rect.x0) <= 3.0:
                column_index = nearest
        result.append({
            "text": _clean_requirement_text(raw_text),
            "raw_text": raw_text,
            "bbox": _box(rect),
            "column_index": column_index,
        })
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
            for table_index, table in enumerate(report_page_tables(page), start=1):
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
                        groups[key] = {
                            "parent": node,
                            "current": node,
                            "nodes": {active_clause: node},
                            "table": [],
                            "table_states": {},
                        }
                        nodes.append(node)
                    group = groups[key]
                    current_node = group["current"]
                    state_key = str(current_node.get("node_id") or "")
                    table_state = group.setdefault("table_states", {}).setdefault(
                        state_key, {"mode": False, "starts": []}
                    )
                    starts = _requirement_column_starts(table, left, right)
                    if len(starts) > len(table_state.get("starts", [])):
                        table_state["starts"] = starts
                    cells = _requirement_cells(page, table, row_index, left, right, table_state.get("starts", starts))
                    if not cells or not any(cell.get("text") for cell in cells):
                        continue
                    text = "\n".join(cell["text"] for cell in cells if cell["text"])
                    has_clause_heading = any(CLAUSE.match(line.strip()) for line in text.splitlines())
                    physical_columns = [
                        int(cell["column_index"])
                        for cell in cells
                        if cell.get("column_index") is not None
                    ]
                    has_split_requirement_columns = len(physical_columns) > 1 or any(
                        index > 0 for index in physical_columns
                    )
                    if has_split_requirement_columns and not has_clause_heading:
                        table_state["mode"] = True
                    if table_state.get("mode") and not has_clause_heading:
                        width = max(
                            len(table_state.get("starts", [])),
                            max(physical_columns, default=-1) + 1,
                        )
                        matrix_row = [""] * width
                        for cell in cells:
                            index = cell.get("column_index")
                            if index is None:
                                index = 0
                            value = str(cell.get("text") or "")
                            matrix_row[index] = (
                                f"{matrix_row[index]}\n{value}" if matrix_row[index] and value else matrix_row[index] or value
                            ).strip()
                        table_id = f"report:table:item:{active_sequence}:clause:{current_node.get('clause_id', active_clause)}"
                        group["current"]["table_rows"].append({
                            "pdf_page": page.number + 1,
                            "table_id": table_id,
                            "cells": cells,
                            "matrix_row": matrix_row,
                            "rows": [matrix_row],
                            "source_sha256": digest,
                            "extraction_method": "report_requirement_columns",
                            "binding": "physical",
                        })
                        group["current"]["evidence"].extend(
                            evidence(
                                "report",
                                page.number + 1,
                                cell["bbox"],
                                cell.get("raw_text", cell["text"]),
                                digest,
                                "report_requirement_cell",
                            )
                            for cell in cells
                        )
                        continue
                    # A multi-column row belongs to the active requirement
                    # table. All physical columns, including blank values,
                    # survive; merged keys are inherited only by table logic.
                    if len(cells) > 1 and not has_clause_heading:
                        width = max(len(table_state.get("starts", [])), max(physical_columns, default=-1) + 1)
                        matrix_row = [""] * width
                        for cell in cells:
                            index = cell.get("column_index")
                            if index is None:
                                index = 0
                            matrix_row[index] = str(cell.get("text") or "")
                        group["current"]["table_rows"].append({"pdf_page": page.number+1,
                            "table_id": f"report:table:item:{active_sequence}:clause:{current_node.get('clause_id', active_clause)}",
                            "cells": cells, "matrix_row": matrix_row,
                            "rows": [matrix_row], "source_sha256": digest,
                            "extraction_method": "report_requirement_columns", "binding": "physical"})
                        group["current"]["evidence"].extend(evidence("report", page.number+1, c["bbox"], c.get("raw_text", c["text"]), digest,
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
                            group["current"]["evidence"].append(evidence("report", page.number+1, cell["bbox"], cell.get("raw_text", line), digest,
                                    "report_requirement_cell"))
                    if not group["parent"]["evidence"]:
                        group["parent"]["evidence"].append(evidence("report", page.number+1,
                            [left, cells[0]["bbox"][1], right, cells[-1]["bbox"][3]], group["parent"]["title"], digest))
    nodes = _split_inline_clauses(nodes)
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
