"""Locate a review-only 2.x requirements body in one OCR observation channel.

Selection uses section transitions and actual requirement sentences. A heading
outline is excluded only with TOC evidence or repeated headings in the later
body. Multiple supported bodies are ambiguous; no last/longest fallback exists.
Indices always point to unchanged OCR records, not corrected text.
"""
from __future__ import annotations

import re
from typing import Any, Sequence

_CLAUSE = re.compile(
    r"^\s*(?P<clause>[23](?:\s*\.\s*\d+){1,8})"
    r"(?=\s|[、，,:：)）\u3400-\u9fffA-Za-z])(?P<body>.*\S)?\s*$"
)
_TOP = re.compile(r"^\s*(?P<chapter>[23])\s*[.．、:：]?\s*(?P<body>[A-Za-z\u3400-\u9fff].*)$")
_REQUIREMENTS = re.compile(r"^(?:性能|技术要求|要求|technical\s+requirements\b|requirements\b)", re.I)
_METHODS = re.compile(r"^(?:试验|检验|测试|检测|test\b|inspection\b|methods?\b)", re.I)
_TOC_TITLE = re.compile(r"^\s*(?:目\s*录|table\s+of\s+contents|contents)\s*$", re.I)
_LEADERS = re.compile(r"(?:\.{4,}|…{2,}|·{4,}|_{4,})\s*\d*\s*$")
# The 应 in 感应 is part of a noun, not a modal requirement. A later
# standalone 应 in '压力感应精度应符合…' still supports a requirement.
_REQUIREMENT_SENTENCE = re.compile(
    r"(?<!感)应|须|不得|不少于|不大于|不小于|不超过|≥|≤|±|\b(?:shall|must)\b", re.I
)


def _heading(text: str):
    match = _CLAUSE.match(text)
    if match:
        return re.sub(r"\s", "", match["clause"]), str(match["body"] or "").lstrip(" 、，,:：)）")
    return None


def _title_key(text: str) -> str:
    # Only used to compare repeated outline titles, never acceptance limits.
    return re.sub(r"\s", "", text).rstrip("0123456789.．…·_ ")


def select_ocr_requirement_section(lines: Sequence[Sequence[Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "version": "ocr-section-1.1", "status": "body_not_detected",
        "reason_code": "PTR_OCR_BODY_NOT_DETECTED", "body_indices": [],
        "toc_indices": [], "candidates": [], "automatic_pass_authorized": False,
        "observation_count": len(lines),
    }
    if any(len(v) < 4 or not isinstance(v[2], str) for v in lines):
        result.update(status="invalid_observations", reason_code="PTR_OCR_OBSERVATIONS_INVALID")
        return result
    channels = {str(v[3]) for v in lines}
    if len(channels) > 1 or any(not channel.startswith("ocr:") for channel in channels):
        result.update(status="invalid_observations", reason_code="PTR_OCR_CHANNEL_AMBIGUOUS")
        return result
    current: dict[str, Any] | None = None
    in_methods = False
    toc_pages = {v[0] for v in lines if _TOC_TITLE.fullmatch(v[2])}

    def close(end: int, boundary: str):
        nonlocal current
        if current is not None:
            current.update(end_index_exclusive=end, end_boundary=boundary)
            result["candidates"].append(current)
            current = None

    for i, record in enumerate(lines):
        text = record[2].strip()
        heading = _heading(text)
        top = _TOP.match(text) if heading is None else None
        starts_requirements = bool(top and top["chapter"] == "2" and _REQUIREMENTS.match(top["body"]))
        starts_methods = bool(
            (heading and heading[0].startswith("3."))
            or (top and top["chapter"] == "3" and _METHODS.match(top["body"]))
        )
        if starts_requirements:
            close(i, "next_requirements_section")
            current = {"start_index": i, "start_kind": "requirements_title"}
            in_methods = False
        elif starts_methods:
            close(i, "methods_section")
            in_methods = True
        elif heading and heading[0].startswith("2.") and current is None and not in_methods:
            current = {"start_index": i, "start_kind": "first_requirement_clause"}
    close(len(lines), "end_of_observations")

    for candidate in result["candidates"]:
        headings = []
        signals = []
        leaders = []
        after_clause = False
        start, end = candidate["start_index"], candidate["end_index_exclusive"]
        for i in range(start, end):
            text = lines[i][2].strip()
            heading = _heading(text)
            if _LEADERS.search(text):
                leaders.append(i)
            if heading and heading[0].startswith("2."):
                headings.append({"clause_id": heading[0], "title": heading[1], "index": i})
                after_clause = True
            body = heading[1] if heading else text
            # Neither page numbers nor an outline title alone support a body.
            if after_clause and len(re.sub(r"\s", "", body)) >= 5 and not _LEADERS.search(text):
                if _REQUIREMENT_SENTENCE.search(body) or (
                    heading is None and len(body) >= 12 and body.endswith(("。", "."))
                    and re.search(r"[,，;；]", body)
                ):
                    signals.append(i)
        candidate.update(
            headings=headings, requirement_signal_indices=signals, leader_indices=leaders,
            toc_title_present=any(lines[i][0] in toc_pages for i in range(start, end)),
            start_evidence={"index": start, "pdf_page": lines[start][0], "bbox": lines[start][1], "text": lines[start][2]},
            end_evidence=({"index": end, "pdf_page": lines[end][0], "bbox": lines[end][1], "text": lines[end][2]} if end < len(lines) else None),
            classification="body_candidate" if headings and signals else "heading_outline",
        )
    supported = [c for c in result["candidates"] if c["classification"] == "body_candidate"]
    if len(supported) != 1:
        if len(supported) > 1:
            result.update(status="body_ambiguous", reason_code="PTR_OCR_BODY_AMBIGUOUS")
        return result
    body = supported[0]
    # A candidate carrying both TOC signals and substantive conditions must not
    # be discarded or selected merely because its text is longer.
    if body["toc_title_present"] or body["leader_indices"]:
        result.update(status="body_ambiguous", reason_code="PTR_OCR_BODY_AMBIGUOUS")
        return result
    titles = {(h["clause_id"], _title_key(h["title"])) for h in body["headings"]}
    for candidate in result["candidates"]:
        if candidate is body:
            continue
        repeated = [h["index"] for h in candidate["headings"] if (h["clause_id"], _title_key(h["title"])) in titles]
        earlier = candidate["end_index_exclusive"] <= body["start_index"]
        toc_evidence = candidate["toc_title_present"] or len(candidate["leader_indices"]) >= 2 or (earlier and len(repeated) >= 2)
        candidate["repeated_heading_indices"] = repeated
        if candidate["requirement_signal_indices"] or not earlier or not toc_evidence:
            result.update(status="body_ambiguous", reason_code="PTR_OCR_BODY_AMBIGUOUS")
            return result
        candidate["classification"] = "toc_outline"
    result.update(
        status="selected_body", reason_code="PTR_OCR_BODY_SELECTED_REVIEW_REQUIRED",
        body_indices=list(range(body["start_index"], body["end_index_exclusive"])),
        toc_indices=[i for c in result["candidates"] if c["classification"] == "toc_outline" for i in range(c["start_index"], c["end_index_exclusive"])],
    )
    return result
