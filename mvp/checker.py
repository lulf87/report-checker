from __future__ import annotations

import hashlib
import html
import json
import math
import platform
import re
import shutil
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable, Mapping

try:
    import pymupdf as fitz
except ImportError:
    import fitz
from PIL import Image, ImageDraw

# Report-self extensions are intentionally imported here (rather than through
# ``run_full_2795``) so the production Report-only runner can use the same
# deterministic rule implementations without creating an import cycle.
from mvp.full_report_attempt import scan_report_numeric_file
from mvp.full_report_photo import analyze_report_photo_rules


STATUS_RANK = {"pass": 0, "warning": 1, "manual": 2, "error": 3}
STATUS_LABEL = {
    "pass": "通过",
    "warning": "通过（有警示）",
    "manual": "待人工复核",
    "error": "不通过",
}

# Formal Report self-check scope.  Keep the legacy ``R07-B`` numeric rule as
# its own execution so a Report result can be traced independently from the
# aggregation/conclusion rule ``R07``.
REPORT_SELF_RULE_ORDER = (
    "REPORT-R01",
    "REPORT-R02",
    "REPORT-R03",
    "REPORT-R04",
    "REPORT-R05",
    "REPORT-R06",
    "REPORT-R07",
    "REPORT-R07-B",
    "REPORT-R08",
    "REPORT-R09",
    "REPORT-R10",
    "REPORT-R11",
)


@dataclass(frozen=True)
class SourceFile:
    role: str
    path: Path


@dataclass(frozen=True)
class SampleConfig:
    sample: str
    report: Path
    record61: Path | None = None
    record202: Path | None = None


@dataclass(frozen=True)
class NumericProbeSpec:
    probe_id: str
    record_anchor: str
    switch_column_count: int
    result_columns: tuple[int, ...]
    report_metric: str
    report_requirement_tokens: tuple[str, ...]
    description: str


SAMPLE_CONFIGS: dict[str, SampleConfig] = {
    "1539": SampleConfig(
        sample="1539",
        report=Path("素材/report/1539/QW2025-1539 Draft.pdf"),
        record61=Path("素材/record/1539/GB 9706.1/Copy of GB9706.1-2020原始记录-2023-0915 3.pdf"),
        record202=Path("素材/record/1539/GB 9706.202/GB9706.202-2021表2主机版250312（纸质表3版） 5.pdf"),
    ),
    "2948": SampleConfig(
        sample="2948",
        report=Path("素材/report/2948/QW2025-2948 Draft.pdf"),
        record61=Path("素材/record/2948/GB 9706.1-2020/Copy of GB9706.1-2020原始记录-2023-0915 2.pdf"),
        record202=Path("素材/record/2948/GB 9706.202-2021/GB9706.202-2021表2主机版250312（纸质表3版） 3.pdf"),
    ),
    "2795": SampleConfig(
        sample="2795",
        report=Path("素材/report/2795/QW2025-2795 Draft.pdf"),
        record202=Path("素材/record/2795/GB 9706.202/Copy of GB9706.202-2021表2主机版250312（纸质表3版） 2.pdf"),
    ),
    "1347": SampleConfig(
        sample="1347",
        report=Path("素材/report/1347/QW2026-1347 Draft.pdf"),
        record61=Path("素材/record/1347/Copy of GB9706.1-2020原始记录-2023-0915 2.pdf"),
    ),
}


NUMERIC_PROBE_SPECS: tuple[NumericProbeSpec, ...] = (
    NumericProbeSpec(
        probe_id="ground-leakage-after-sfc",
        record_anchor="图.13对地漏电流",
        switch_column_count=3,
        result_columns=(6,),
        report_metric="对地漏电流",
        report_requirement_tokens=("单一故障状态下≤10mA",),
        description="8.7 / 图13 / 对地漏电流 / 潮湿预处理后 / SFC / 全部有效开关行取最大值",
    ),
    NumericProbeSpec(
        probe_id="patient-leakage-after-sfc-cf-ac",
        record_anchor="图15.患者漏电流的测量",
        switch_column_count=4,
        result_columns=(11, 15),
        report_metric="患者漏电流",
        report_requirement_tokens=("单一故障状态下", "CF≤0.05mA"),
        description="8.7 / 图15 / 患者漏电流 / 潮湿预处理后 / 两组SFC / a.c. / 全部有效开关行取最大值",
    ),
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compact(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", "", str(value))


def display_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def expected_report_conclusion(results: Iterable[Any]) -> str:
    values = [compact(value) for value in results if compact(value)]
    if not values:
        return "<检验结果缺失>"
    if any("不符合要求" in value for value in values):
        return "不符合"
    if any(value not in {"——", "/"} for value in values):
        return "符合"
    return "/"


def deterministic_comparison_status(
    comparisons: Iterable[dict[str, Any]],
    *,
    has_additional_unresolved: bool = False,
) -> str:
    decisions = [item.get("decision") for item in comparisons]
    if "mismatch" in decisions:
        return "error"
    if not decisions or "manual" in decisions or has_additional_unresolved:
        return "manual"
    return "pass"


def compare_record_61_statuses(
    selected_by_row: Iterable[Iterable[str]],
    report_conclusions: Iterable[Any],
) -> dict[str, Any]:
    rows = [list(values) for values in selected_by_row]
    allowed_record_statuses = {"符合", "不符合", "不适用"}
    if len(rows) != 2 or any(len(values) != 1 or values[0] not in allowed_record_statuses for values in rows):
        return {
            "status": "manual",
            "expected_report_conclusion": None,
            "report_conclusions": [compact(value) for value in report_conclusions if compact(value)],
            "reason_code": "record_status_not_uniquely_resolved",
        }

    record_statuses = [values[0] for values in rows]
    if "不符合" in record_statuses:
        expected = "不符合"
    elif "符合" in record_statuses:
        expected = "符合"
    else:
        expected = "/"

    conclusions = [compact(value) for value in report_conclusions if compact(value)]
    if not conclusions:
        return {
            "status": "manual",
            "expected_report_conclusion": expected,
            "report_conclusions": [],
            "reason_code": "report_conclusion_not_resolved",
        }
    if any(value not in {"符合", "不符合", "/"} for value in conclusions):
        return {
            "status": "error",
            "expected_report_conclusion": expected,
            "report_conclusions": conclusions,
            "reason_code": "invalid_report_conclusion",
        }
    if len(set(conclusions)) != 1:
        return {
            "status": "error",
            "expected_report_conclusion": expected,
            "report_conclusions": conclusions,
            "reason_code": "conflicting_report_conclusions",
        }
    return {
        "status": "pass" if conclusions[0] == expected else "error",
        "expected_report_conclusion": expected,
        "report_conclusions": conclusions,
        "reason_code": "matched" if conclusions[0] == expected else "record_report_status_mismatch",
    }


EXPECTED_202_LEGEND_MAPPING = {
    "√": "符合要求",
    "×": "不符合要求",
    "△": "不适用",
    "/": "此项空白",
}


def verified_202_legend_mapping(lines: Iterable[str] | str) -> dict[str, str] | None:
    source_lines = lines.splitlines() if isinstance(lines, str) else list(lines)
    normalized_lines = [
        compact(line)
        .replace("“", "")
        .replace("”", "")
        .replace('"', "")
        .replace("∆", "△")
        .replace("Δ", "△")
        for line in source_lines
    ]
    meanings = "不符合要求|符合要求|不适用|此项空白"
    for symbol, expected_meaning in EXPECTED_202_LEGEND_MAPPING.items():
        observed: set[str] = set()
        pattern = rf"{re.escape(symbol)}为({meanings})"
        for line in normalized_lines:
            observed.update(re.findall(pattern, line))
        if observed != {expected_meaning}:
            return None
    return dict(EXPECTED_202_LEGEND_MAPPING)


def rect_list(rect: fitz.Rect | tuple[float, float, float, float]) -> list[float]:
    r = fitz.Rect(rect)
    return [round(r.x0, 3), round(r.y0, 3), round(r.x1, 3), round(r.y1, 3)]


def union_rect(rects: Iterable[fitz.Rect]) -> fitz.Rect:
    rects = list(rects)
    if not rects:
        raise ValueError("at least one rectangle is required")
    result = fitz.Rect(rects[0])
    for rect in rects[1:]:
        result |= fitz.Rect(rect)
    return result


def render_evidence(
    page: fitz.Page,
    rects: list[fitz.Rect],
    output_path: Path,
    *,
    padding: float = 18,
    scale: float = 2.5,
    full_width: bool = False,
    context_rects: list[fitz.Rect] | None = None,
) -> dict[str, Any]:
    page_rect = page.rect
    focus = union_rect([*rects, *(context_rects or [])])
    if full_width:
        clip = fitz.Rect(page_rect.x0, max(page_rect.y0, focus.y0 - padding), page_rect.x1, min(page_rect.y1, focus.y1 + padding))
    else:
        clip = fitz.Rect(
            max(page_rect.x0, focus.x0 - padding),
            max(page_rect.y0, focus.y0 - padding),
            min(page_rect.x1, focus.x1 + padding),
            min(page_rect.y1, focus.y1 + padding),
        )
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=clip, annots=True, alpha=False)
    image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    draw = ImageDraw.Draw(image)
    colors = ["#f04438", "#2970ff", "#f79009", "#12b76a"]
    for index, rect in enumerate(rects):
        box = [
            (rect.x0 - clip.x0) * scale,
            (rect.y0 - clip.y0) * scale,
            (rect.x1 - clip.x0) * scale,
            (rect.y1 - clip.y0) * scale,
        ]
        draw.rectangle(box, outline=colors[index % len(colors)], width=max(3, int(scale)))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path)
    return {
        "pdf_page": page.number + 1,
        "rects": [rect_list(rect) for rect in rects],
        "image": str(output_path),
    }


def find_exact_rect(page: fitz.Page, text: str) -> fitz.Rect | None:
    matches = page.search_for(text)
    if matches:
        return fitz.Rect(matches[0])
    return None


def extract_report_number(text: str) -> tuple[int, int] | None:
    normalized = text.replace("（", "(").replace("）", ")")
    match = re.search(r"QW\s*(\d{4})\s*第?\s*(\d+)\s*号", normalized, re.I)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def classify_report_number_field(text: str) -> dict[str, Any]:
    number = extract_report_number(text)
    if number is not None:
        return {"state": "parsed", "number": number}
    normalized = compact(text).replace("（", "(").replace("）", ")")
    if "编号" not in normalized or "QW" not in normalized:
        return {"state": "unreadable_or_unsupported", "number": None}
    if re.search(r"QW(?:第)?号", normalized, re.I):
        return {"state": "explicit_missing", "number": None}
    return {"state": "malformed", "number": None}


def cover_value_after_label(page: fitz.Page, label: str) -> tuple[str, fitz.Rect | None]:
    lines = [line.strip() for line in page.get_text("text").splitlines() if line.strip()]
    target = compact(label)
    for index, line in enumerate(lines):
        normalized = compact(line)
        if normalized == target and index + 1 < len(lines):
            value = lines[index + 1]
            return value, find_exact_rect(page, value)
        if normalized.startswith(target) and len(normalized) > len(target):
            value = normalized[len(target) :]
            return value, find_exact_rect(page, value)
    return "", None


def page_three_fields(page: fitz.Page) -> dict[str, tuple[str, fitz.Rect | None]]:
    tables = page.find_tables().tables
    if not tables:
        return {}
    fields: dict[str, tuple[str, fitz.Rect | None]] = {}
    field_names = {
        "样品名称", "样品编号", "商标", "型号规格", "委托方", "检验类别", "委托方地址",
        "产品编号/批号", "生产单位", "抽样单编号", "受检单位", "生产日期", "抽样单位",
        "样品数量", "抽样地点", "抽样基数", "抽样日期", "检验地点", "到样日期",
        "检验日期", "检验项目", "检验依据", "检验结论", "备注",
    }
    for row in tables[0].extract():
        for idx, cell in enumerate(row):
            key = compact(cell).replace("／", "/")
            # Merged cells can shift the second label away from index 2, so
            # recognize the finite field vocabulary instead of fixed columns.
            if key in field_names and idx + 1 < len(row):
                value = display_text(row[idx + 1])
                fields[key] = (value, find_exact_rect(page, value))
    return fields


def header_map(row: list[Any]) -> dict[str, int]:
    normalized = [compact(cell) for cell in row]
    mapping: dict[str, int] = {}
    for expected in ["序号", "检验项目", "标准条款", "标准要求", "检验结果", "单项结论", "备注"]:
        for idx, value in enumerate(normalized):
            if value == expected:
                mapping[expected] = idx
                break
    return mapping


def is_formal_report_table(table: fitz.table.Table) -> bool:
    rows = table.extract()
    if not rows:
        return False
    mapping = header_map(rows[0])
    return {"序号", "检验项目", "标准条款", "标准要求", "检验结果", "单项结论"}.issubset(mapping)


def scan_report_items(doc: fitz.Document) -> tuple[dict[int, dict[str, Any]], dict[str, Any]]:
    items: dict[int, dict[str, Any]] = {}
    page_sequences: list[dict[str, Any]] = []
    active_sequence: int | None = None
    continuation_errors: list[str] = []
    new_sequence_order: list[int] = []

    for page in doc:
        selected: fitz.table.Table | None = None
        for table in page.find_tables().tables:
            if is_formal_report_table(table):
                selected = table
                break
        if selected is None:
            continue

        rows = selected.extract()
        mapping = header_map(rows[0])
        explicit: list[tuple[int, str, int]] = []
        current_on_page = active_sequence
        for row_index, row in enumerate(rows[1:], start=1):
            sequence_text = compact(row[mapping["序号"]]) if mapping["序号"] < len(row) else ""
            match = re.fullmatch(r"(续)?(\d+)", sequence_text)
            if row_index == 1 and not match and active_sequence is not None:
                continuation_errors.append(
                    f"PDF第{page.number + 1}页跨页项目{active_sequence}缺少“续{active_sequence}”"
                )
            if match:
                is_continuation = bool(match.group(1))
                sequence = int(match.group(2))
                explicit.append((row_index, "continued" if is_continuation else "new", sequence))
                if is_continuation:
                    if row_index != 1:
                        continuation_errors.append(f"PDF第{page.number + 1}页的续{sequence}出现在页面中部")
                    if active_sequence is not None and sequence != active_sequence:
                        continuation_errors.append(
                            f"PDF第{page.number + 1}页以续{sequence}开始，但上一页最后项目为{active_sequence}"
                        )
                else:
                    if row_index == 1 and active_sequence == sequence:
                        continuation_errors.append(f"PDF第{page.number + 1}页跨页项目{sequence}缺少“续”")
                    new_sequence_order.append(sequence)
                    items.setdefault(
                        sequence,
                        {
                            "sequence": sequence,
                            "project": "",
                            "clause": "",
                            "requirements": [],
                            "results": [],
                            "conclusions": [],
                            "pages": [],
                            "row_locations": [],
                        },
                    )
                current_on_page = sequence
                active_sequence = sequence

            if current_on_page is None or current_on_page not in items:
                continue
            item = items[current_on_page]
            item["pages"].append(page.number + 1)
            for key, destination in [
                ("检验项目", "project"),
                ("标准条款", "clause"),
            ]:
                idx = mapping[key]
                value = display_text(row[idx]) if idx < len(row) else ""
                if value and not item[destination]:
                    item[destination] = value
            requirement = display_text(row[mapping["标准要求"]])
            result = display_text(row[mapping["检验结果"]])
            conclusion = display_text(row[mapping["单项结论"]])
            if requirement:
                item["requirements"].append(requirement)
            if result:
                item["results"].append(result)
            if conclusion:
                item["conclusions"].append(conclusion)
            if result or conclusion:
                result_cell = selected.rows[row_index].cells[mapping["检验结果"]]
                conclusion_cell = selected.rows[row_index].cells[mapping["单项结论"]]
                item["row_locations"].append(
                    {
                        "pdf_page": page.number + 1,
                        "result": result,
                        "conclusion": conclusion,
                        "result_rect": rect_list(result_cell) if result_cell is not None else None,
                        "conclusion_rect": rect_list(conclusion_cell) if conclusion_cell is not None else None,
                    }
                )

        page_sequences.append({"pdf_page": page.number + 1, "explicit": explicit})

    unique_new: list[int] = []
    for number in new_sequence_order:
        if not unique_new or unique_new[-1] != number:
            unique_new.append(number)
    gaps: list[str] = []
    if unique_new:
        expected = list(range(unique_new[0], unique_new[-1] + 1))
        if unique_new != expected:
            missing = sorted(set(expected) - set(unique_new))
            duplicates = sorted({x for x in unique_new if unique_new.count(x) > 1})
            if missing:
                gaps.append(f"缺少新项目序号：{missing}")
            if duplicates:
                gaps.append(f"新项目序号重复：{duplicates}")

    result_mismatches: list[dict[str, Any]] = []
    nonconforming: list[int] = []
    expected_conclusion_counts: dict[str, int] = {"符合": 0, "/": 0, "不符合": 0, "缺失": 0}
    for number, item in items.items():
        values = [compact(value) for value in item["results"] if compact(value)]
        conclusions = [compact(value) for value in item["conclusions"] if compact(value)]
        actual = conclusions[0] if conclusions else ""
        expected = expected_report_conclusion(values)
        if expected == "不符合":
            nonconforming.append(number)
        if expected == "<检验结果缺失>":
            expected_conclusion_counts["缺失"] += 1
        else:
            expected_conclusion_counts[expected] += 1
        conclusion_conflict = len(set(conclusions)) > 1
        if actual != expected or conclusion_conflict:
            result_mismatches.append(
                {
                    "sequence": number,
                    "expected": expected,
                    "actual": actual or "<空>",
                    "results": item["results"],
                    "all_conclusions": item["conclusions"],
                    "conclusion_conflict": conclusion_conflict,
                    "pdf_pages": sorted(set(item["pages"])),
                }
            )

    diagnostics = {
        "page_sequences": page_sequences,
        "new_sequence_order": unique_new,
        "sequence_gaps": gaps,
        "continuation_errors": continuation_errors,
        "result_mismatches": result_mismatches,
        "nonconforming_items": nonconforming,
        "expected_conclusion_counts": expected_conclusion_counts,
    }
    return items, diagnostics


def find_report_item_value_cell(
    doc: fitz.Document,
    sequence: int,
    column: str,
    expected_value: str | None = None,
) -> tuple[int, str, fitz.Rect]:
    current_sequence: int | None = None
    for page in doc:
        for table in page.find_tables().tables:
            if not is_formal_report_table(table):
                continue
            rows = table.extract()
            mapping = header_map(rows[0])
            for row_index, row in enumerate(rows[1:], start=1):
                sequence_text = compact(row[mapping["序号"]])
                sequence_match = re.fullmatch(r"(续)?(\d+)", sequence_text)
                if sequence_match:
                    current_sequence = int(sequence_match.group(2))
                if current_sequence != sequence:
                    continue
                value = display_text(row[mapping[column]])
                if not value or (expected_value is not None and compact(value) != compact(expected_value)):
                    continue
                cell = table.rows[row_index].cells[mapping[column]]
                if cell is not None:
                    return page.number + 1, value, fitz.Rect(cell)
    raise RuntimeError(f"report item {sequence} column {column} was not located")


def _render_finding_locations(
    document: fitz.Document,
    finding: dict[str, Any],
    output_dir: Path,
    *,
    slug: str | None = None,
) -> None:
    """Turn reusable rule locations into the runner's rendered evidence contract."""

    raw_locations = finding.get("evidence", [])
    rendered: list[dict[str, Any]] = []
    seen: set[tuple[int, tuple[float, float, float, float]]] = set()
    for index, location in enumerate(raw_locations):
        if not isinstance(location, Mapping):
            continue
        page_number = location.get("pdf_page")
        rect_values = location.get("rects") or []
        if not isinstance(page_number, int) or not (1 <= page_number <= document.page_count):
            continue
        rects: list[fitz.Rect] = []
        for value in rect_values:
            try:
                rect = fitz.Rect(value)
            except (TypeError, ValueError):
                continue
            if rect.is_empty:
                continue
            key = (page_number, tuple(round(float(item), 3) for item in rect))
            if key in seen:
                continue
            seen.add(key)
            rects.append(rect)
        if not rects:
            continue
        stem = slug or finding["id"].lower()
        rendered_item = render_evidence(
            document[page_number - 1],
            rects,
            output_dir / "evidence" / f"{stem}-{index + 1}-page{page_number}.png",
            padding=20,
            full_width=True,
        )
        rendered_item["document_role"] = "report"
        rendered_item["source"] = location.get("source", "report")
        rendered_item["pdf_page"] = page_number
        rendered.append(rendered_item)
    finding["evidence"] = rendered


def _report_r08_finding(items: Mapping[int, Mapping[str, Any]]) -> dict[str, Any]:
    required_fields = {
        "project": lambda item: bool(compact(item.get("project"))),
        "clause": lambda item: bool(compact(item.get("clause"))),
        "requirements": lambda item: any(compact(value) for value in item.get("requirements", [])),
        "results": lambda item: any(compact(value) for value in item.get("results", [])),
        "conclusions": lambda item: any(compact(value) for value in item.get("conclusions", [])),
    }
    missing: list[dict[str, Any]] = []
    for sequence, item in items.items():
        absent = [name for name, predicate in required_fields.items() if not predicate(item)]
        if absent:
            missing.append(
                {
                    "sequence": sequence,
                    "project": item.get("project"),
                    "missing_fields": absent,
                    "pdf_pages": sorted(set(item.get("pages", []))),
                }
            )
    return {
        "id": "REPORT-R08",
        "title": "完整检验项目漏填检查",
        "status": "error" if missing else "pass",
        "summary": (
            f"{len(items)}个完整检验项目的项目、条款、要求、结果和单项结论均已填写"
            if not missing
            else f"发现{len(missing)}个完整检验项目存在漏填"
        ),
        "details": {
            "items_checked": len(items),
            "required_fields": list(required_fields),
            "placeholders_count_as_filled": ["/", "——"],
            "missing": missing,
        },
        "evidence": [],
    }


def _report_r09_r10_findings(diagnostics: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    new_sequence_order = [
        marker[2]
        for page in diagnostics["page_sequences"]
        for marker in page["explicit"]
        if marker[1] == "new"
    ]
    expected = list(range(1, max(new_sequence_order) + 1)) if new_sequence_order else []
    sequence_errors = list(diagnostics["sequence_gaps"])
    if not new_sequence_order:
        sequence_errors.append("未识别到新检验项目序号")
    elif new_sequence_order[0] != 1:
        sequence_errors.append(f"首个新项目序号为{new_sequence_order[0]}，不是1")
    if len(new_sequence_order) != len(set(new_sequence_order)):
        duplicates = sorted(number for number in set(new_sequence_order) if new_sequence_order.count(number) > 1)
        sequence_errors.append(f"新项目序号重复：{duplicates}")
    sequence_ok = bool(new_sequence_order) and new_sequence_order == expected and not sequence_errors
    continuation_errors = list(diagnostics["continuation_errors"])
    first_markers = [
        {"pdf_page": page["pdf_page"], "first_marker": page["explicit"][0][1:] if page["explicit"] else None}
        for page in diagnostics["page_sequences"]
    ]
    r09 = {
        "id": "REPORT-R09",
        "title": "新检验项目序号连续性",
        "status": "pass" if sequence_ok else "error",
        "summary": f"新检验项目序号从1连续至{new_sequence_order[-1]}" if sequence_ok else "发现新检验项目序号缺失、重复、跳号或倒序",
        "details": {"new_sequence_order": new_sequence_order, "expected_sequence_order": expected, "errors": sequence_errors},
        "evidence": [],
    }
    r10 = {
        "id": "REPORT-R10",
        "title": "跨页首项“续N”检查",
        "status": "pass" if not continuation_errors else "error",
        "summary": f"检查{len(first_markers)}个正式表格页，跨页首项“续N”位置及序号正确" if not continuation_errors else f"发现{len(continuation_errors)}个跨页标记错误",
        "details": {"formal_table_pages_checked": len(first_markers), "first_markers_by_page": first_markers, "errors": continuation_errors},
        "evidence": [],
    }
    return r09, r10


def _report_r07b_finding(report_path: Path, output_dir: Path, document: fitz.Document) -> dict[str, Any]:
    scan = scan_report_numeric_file(report_path)
    counts = scan["counts"]
    # A zero-discovery scan is unresolved coverage, never a successful
    # numerical check.  The scope ledger will retain a manual object for it.
    status = "manual" if scan["numeric_measurements"] == 0 else "error" if counts["error"] else "manual" if counts["manual"] else "pass"
    locations: list[dict[str, Any]] = []
    for comparison in scan["comparisons"]:
        if comparison["status"] == "pass":
            continue
        rects: list[list[float]] = []
        result_rect = comparison.get("result_rect")
        if result_rect:
            rects.append(result_rect)
        locations.append({"pdf_page": comparison["pdf_page"], "rects": rects})
    if not locations and status == "manual" and document.page_count:
        locations.append({"pdf_page": 1, "rects": [[0, 0, document[0].rect.width, document[0].rect.height]]})
    finding = {
        "id": "REPORT-R07-B",
        "title": "实测结果与接受标准",
        "status": status,
        "summary": f"识别到{scan['numeric_measurements']}项数值测量：{counts['pass']}项通过、{counts['error']}项不通过、{counts['manual']}项待人工复核",
        "details": {
            "scope": "比较Report中的数值型实测结果与同行接受标准；无法确认单位、转换或非标准后缀时转人工复核",
            "counts": counts,
            "digit_containing_result_cells": scan["digit_containing_result_cells"],
            "numeric_measurements": scan["numeric_measurements"],
            "zero_discovery": scan["numeric_measurements"] == 0,
            "excluded_numeric_tokens": scan["excluded_numeric_tokens"],
            "comparisons": scan["comparisons"],
        },
        "evidence": locations,
    }
    _render_finding_locations(document, finding, output_dir)
    return finding


def report_self_check(report: SourceFile, output_dir: Path) -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]]]:
    doc = fitz.open(report.path)
    findings: list[dict[str, Any]] = []

    cover = doc[0]
    first = {
        key: cover_value_after_label(cover, label)
        for key, label in {"委托方": "委 托 方", "样品名称": "样品名称", "型号规格": "型号规格"}.items()
    }
    third = page_three_fields(doc[2])
    comparisons = []
    evidence_rects: list[fitz.Rect] = []
    cover_number = extract_report_number(cover.get_text("text"))
    page3_number = extract_report_number(doc[2].get_text("text"))
    cover_number_rects = cover.search_for("QW")
    page3_number_rects = doc[2].search_for("QW")
    comparisons.append(
        {
            "field": "报告编号",
            "cover": cover_number,
            "page3": page3_number,
            "match": cover_number is not None and cover_number == page3_number,
            "cover_location": {"document_role": "report", "pdf_page": 1, "rect": rect_list(cover_number_rects[0]) if cover_number_rects else None, "source": "report_cover_number"},
            "page3_location": {"document_role": "report", "pdf_page": 3, "rect": rect_list(page3_number_rects[0]) if page3_number_rects else None, "source": "report_page3_number"},
        }
    )
    if cover_number_rects:
        evidence_rects.append(fitz.Rect(cover_number_rects[0]))
    for key in ["委托方", "样品名称", "型号规格"]:
        a, rect_a = first.get(key, ("", None))
        b, rect_b = third.get(key, ("", None))
        field_match = bool(compact(a)) and bool(compact(b)) and compact(a) == compact(b)
        comparisons.append(
            {
                "field": key,
                "cover": a,
                "page3": b,
                "match": field_match,
                "cover_location": {"document_role": "report", "pdf_page": 1, "rect": rect_list(rect_a), "source": "report_cover_identity"} if rect_a else None,
                "page3_location": {"document_role": "report", "pdf_page": 3, "rect": rect_list(rect_b), "source": "report_identity_cell"} if rect_b else None,
            }
        )
        if rect_a:
            evidence_rects.append(rect_a)
    cover_evidence = render_evidence(
        cover,
        evidence_rects,
        output_dir / "evidence" / "report-cover-identity.png",
        padding=24,
        full_width=True,
    )
    page3_rects = [value[1] for value in third.values() if value[1] is not None]
    if page3_number_rects:
        page3_rects.append(fitz.Rect(page3_number_rects[0]))
    page3_evidence = render_evidence(
        doc[2],
        page3_rects,
        output_dir / "evidence" / "report-page3-identity.png",
        padding=24,
        full_width=True,
    )
    required_page3_fields = (
        "样品名称",
        "样品编号",
        "型号规格",
        "委托方",
        "委托方地址",
        "产品编号/批号",
        "生产单位",
        "受检单位",
        "样品数量",
        "检验地点",
        "到样日期",
        "检验日期",
        "检验项目",
        "检验依据",
        "检验结论",
    )
    extension_missing = [
        field for field in required_page3_fields
        if field not in third or not compact(third[field][0])
    ]
    identity_ok = all(item["match"] for item in comparisons) and not extension_missing
    findings.append(
        {
            "id": "REPORT-R01",
            "title": "首页与第三页身份字段",
            "status": "pass" if identity_ok else "error",
            "summary": "首页/第三页身份字段及第三页扩展字段完整" if identity_ok else "身份字段不一致或第三页扩展字段缺失",
            "details": {
                "comparisons": comparisons,
                "page3_fields": {key: value for key, (value, _rect) in third.items()},
                "page3_locations": {key: location for key, (_value, location) in third.items()},
                "required_page3_fields": list(required_page3_fields),
                "missing_page3_fields": extension_missing,
                "rule": "首页三项身份字段必须与第三页一致；第三页样品编号、委托方地址、产品编号/批号、生产日期等扩展字段必须可定位且有值",
            },
            "evidence": [cover_evidence, page3_evidence],
        }
    )

    body_pages: list[tuple[int, int, int]] = []
    missing_footers: list[int] = []
    for page in doc:
        match = re.search(r"共\s*(\d+)\s*页\s*第\s*(\d+)\s*页", page.get_text("text"))
        if match:
            body_pages.append((page.number + 1, int(match.group(1)), int(match.group(2))))
        elif page.number >= 2:
            missing_footers.append(page.number + 1)
    totals = {item[1] for item in body_pages}
    printed = [item[2] for item in body_pages]
    page_ok = (
        not missing_footers
        and len(totals) == 1
        and printed == list(range(1, len(printed) + 1))
        and printed
        and printed[-1] == next(iter(totals))
    )
    last_page = doc[-1]
    footer_query = (
        f"共{next(iter(totals))} 页 第{printed[-1]} 页"
        if printed and len(totals) == 1
        else ""
    )
    footer_matches = [fitz.Rect(rect) for rect in last_page.search_for(footer_query)] if footer_query else []
    footer_rect = union_rect(footer_matches) if footer_matches else fitz.Rect(350, 35, 550, 80)
    footer_evidence = render_evidence(
        last_page,
        [footer_rect],
        output_dir / "evidence" / "report-final-footer.png",
        padding=18,
        full_width=True,
    )
    findings.append(
        {
            "id": "REPORT-R11",
            "title": "打印页码连续性",
            "status": "pass" if page_ok else "error",
            "summary": (
                f"正文打印页码1–{printed[-1]}连续，总页数恒为{next(iter(totals))}"
                if page_ok
                else "打印页码或总页数存在异常"
            ),
            "details": {
                "pdf_pages": doc.page_count,
                "printed_pages_found": len(printed),
                "totals": sorted(totals),
                "missing_footer_pdf_pages": missing_footers,
                "page_states": [
                    {
                        "pdf_page": pdf_page,
                        "printed_total": total,
                        "printed_page": printed_page,
                        "status": "pass" if page_ok else "manual",
                    }
                    for pdf_page, total, printed_page in body_pages
                ],
            },
            "evidence": [footer_evidence],
        }
    )

    items, diagnostics = scan_report_items(doc)
    structure_ok = not diagnostics["sequence_gaps"] and not diagnostics["continuation_errors"]
    # R09 and R10 are separate scope objects.  The former checks the sequence
    # inventory, while the latter checks the continuation marker at page
    # boundaries; combining them hid which part of a report failed.

    result_ok = not diagnostics["result_mismatches"]
    status = "pass" if result_ok and not diagnostics["nonconforming_items"] else "warning" if result_ok else "error"
    r07_evidence: list[dict[str, Any]] = []
    for mismatch in diagnostics["result_mismatches"]:
        item = items[mismatch["sequence"]]
        locations_by_page: dict[int, list[fitz.Rect]] = defaultdict(list)
        for location in item["row_locations"]:
            for key in ("result_rect", "conclusion_rect"):
                if location.get(key):
                    locations_by_page[location["pdf_page"]].append(fitz.Rect(location[key]))
        for page_number, rects in sorted(locations_by_page.items()):
            r07_evidence.append(
                render_evidence(
                    doc[page_number - 1],
                    rects,
                    output_dir / "evidence" / f"report-r07-item{mismatch['sequence']}-page{page_number}.png",
                    padding=24,
                    full_width=True,
                )
            )
    findings.append(
        {
            "id": "REPORT-R07",
            "title": "多行结果与单项结论",
            "status": status,
            "summary": (
                f"{len(items)}个项目的多行结果聚合与单项结论一致"
                if result_ok
                else f"发现{len(diagnostics['result_mismatches'])}个聚合结论不一致"
            ),
            "details": {
                "mismatches": diagnostics["result_mismatches"],
                "nonconforming_items": diagnostics["nonconforming_items"],
                "expected_conclusion_counts": diagnostics["expected_conclusion_counts"],
                "rule": "不符合优先；否则有实际结果即符合；全为——或/则单项结论应为/",
            },
            "evidence": r07_evidence,
        }
    )
    # Add the expanded Report-only scope to the same production entry point.
    # OCR derivatives are deliberately not accepted by this runner: photo and
    # label content therefore remains ``manual`` (or a concrete missing-photo
    # ``error``), with the reason retained by the reusable photo-rule module.
    photo_analysis = analyze_report_photo_rules(report.path, None, None)
    photo_findings = photo_analysis["findings"]
    for finding in photo_findings:
        _render_finding_locations(doc, finding, output_dir)

    r07b = _report_r07b_finding(report.path, output_dir, doc)
    r08 = _report_r08_finding(items)
    r09, r10 = _report_r09_r10_findings(diagnostics)

    # Give missing-field findings a page-level location so every concrete
    # omission remains reviewable in the HTML evidence panel.
    if r08["details"]["missing"]:
        r08["evidence"] = [
            {
                "pdf_page": page_number,
                "rects": [[0, 0, doc[page_number - 1].rect.width, doc[page_number - 1].rect.height]],
            }
            for page_number in sorted({page for item in r08["details"]["missing"] for page in item["pdf_pages"]})
            if 1 <= page_number <= doc.page_count
        ]
        _render_finding_locations(doc, r08, output_dir)

    baseline = {finding["id"]: finding for finding in findings}
    by_id = {
        "REPORT-R01": baseline["REPORT-R01"],
        **{finding["id"]: finding for finding in photo_findings},
        "REPORT-R07": baseline["REPORT-R07"],
        "REPORT-R07-B": r07b,
        "REPORT-R08": r08,
        "REPORT-R09": r09,
        "REPORT-R10": r10,
        "REPORT-R11": baseline["REPORT-R11"],
    }
    findings = [by_id[rule_id] for rule_id in REPORT_SELF_RULE_ORDER]
    doc.close()
    return findings, items


def annotation_points(annotation: fitz.Annot) -> list[tuple[float, float]]:
    vertices = annotation.vertices or []
    return [(float(point[0]), float(point[1])) for stroke in vertices for point in stroke]


def annotation_centroid(annotation: fitz.Annot) -> tuple[float, float]:
    points = annotation_points(annotation)
    if not points:
        rect = annotation.rect
        return (rect.x0 + rect.x1) / 2, (rect.y0 + rect.y1) / 2
    return sum(x for x, _ in points) / len(points), sum(y for _, y in points) / len(points)


def ink_shape_features(annotation: fitz.Annot) -> dict[str, float]:
    points = annotation_points(annotation)
    if len(points) < 2:
        return {
            "stroke_count": float(len(annotation.vertices or [])),
            "path_length": 0.0,
            "end_distance": 0.0,
            "open_ratio": 0.0,
            "end_path_ratio": 0.0,
        }
    path_length = sum(math.dist(points[index - 1], points[index]) for index in range(1, len(points)))
    end_distance = math.dist(points[0], points[-1])
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    diagonal = math.hypot(max(xs) - min(xs), max(ys) - min(ys)) or 1.0
    return {
        "stroke_count": float(len(annotation.vertices or [])),
        "path_length": path_length,
        "end_distance": end_distance,
        "open_ratio": end_distance / diagonal,
        "end_path_ratio": end_distance / path_length if path_length else 0.0,
    }


def classify_202_ink(annotation: fitz.Annot) -> tuple[str, dict[str, float]]:
    features = ink_shape_features(annotation)
    if (
        features["stroke_count"] == 1
        and features["open_ratio"] >= 0.60
        and features["end_path_ratio"] >= 0.45
    ):
        return "√", features
    if (
        features["stroke_count"] == 1
        and features["open_ratio"] <= 0.40
        and features["end_path_ratio"] <= 0.18
    ):
        return "△", features
    return "其他", features


def ink_for_cell(page: fitz.Page, cell: tuple[float, float, float, float], tolerance: float = 3.0) -> list[fitz.Annot]:
    rect = fitz.Rect(cell)
    expanded = fitz.Rect(rect.x0 - tolerance, rect.y0 - tolerance, rect.x1 + tolerance, rect.y1 + tolerance)
    selected: list[fitz.Annot] = []
    for annotation in page.annots() or []:
        annotation_type = annotation.type
        if len(annotation_type) < 2 or annotation_type[1] != "Ink":
            continue
        cx, cy = annotation_centroid(annotation)
        if not rect.contains(fitz.Point(cx, cy)):
            continue
        points = annotation_points(annotation)
        if points:
            inside = sum(1 for point in points if expanded.contains(fitz.Point(*point)))
            if inside / len(points) >= 0.5:
                selected.append(annotation)
        else:
            if expanded.contains(fitz.Point(cx, cy)):
                selected.append(annotation)
    return selected


def render_cell_for_ocr(page: fitz.Page, rect: fitz.Rect, output_path: Path) -> None:
    clip = fitz.Rect(rect.x0 + 0.5, rect.y0 + 0.2, rect.x1 - 0.5, rect.y1 - 0.2)
    pix = page.get_pixmap(matrix=fitz.Matrix(8, 8), clip=clip, annots=True, alpha=False, colorspace=fitz.csGRAY)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    pix.save(output_path)


def local_ocr_candidates(image_path: Path) -> dict[str, Any]:
    results: dict[str, Any] = {"apple_vision": [], "tesseract": []}
    try:
        from ocrmac.ocrmac import text_from_image

        vision = text_from_image(
            str(image_path),
            recognition_level="accurate",
            language_preference=["en-US"],
            confidence_threshold=0.0,
        )
        results["apple_vision"] = [{"text": item[0], "confidence": float(item[1])} for item in vision]
    except Exception as exc:  # optional local backend
        results["apple_vision_error"] = str(exc)

    if shutil.which("tesseract"):
        for psm in (7, 8, 13):
            process = subprocess.run(
                [
                    "tesseract",
                    str(image_path),
                    "stdout",
                    "--psm",
                    str(psm),
                    "-l",
                    "eng",
                    "-c",
                    "tessedit_char_whitelist=0123456789.%+-<>",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            text = compact(process.stdout)
            if text:
                results["tesseract"].append({"psm": psm, "text": text})
    return results


def local_text_ocr(image_path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {"backend": "apple_vision", "lines": []}
    try:
        from ocrmac.ocrmac import text_from_image

        recognized = text_from_image(
            str(image_path),
            recognition_level="accurate",
            language_preference=["zh-Hans", "en-US"],
            confidence_threshold=0.0,
        )
        result["lines"] = [
            {"text": item[0], "confidence": float(item[1])}
            for item in recognized
        ]
    except Exception as exc:  # optional local backend
        result["error"] = str(exc)
    return result


def accepted_numeric_candidate(candidates: dict[str, Any]) -> str | None:
    vision_values: set[str] = set()
    for item in candidates.get("apple_vision", []):
        value = item.get("text", "").strip()
        if re.fullmatch(r"\d{1,4}\.\d{1,2}", value):
            vision_values.add(value)
    tesseract_values: set[str] = set()
    for item in candidates.get("tesseract", []):
        value = item.get("text", "").strip()
        if re.fullmatch(r"\d{1,4}\.\d{1,2}", value):
            tesseract_values.add(value)
    agreement = vision_values & tesseract_values
    if len(agreement) == 1:
        return next(iter(agreement))
    return None


def find_report_numeric_value(
    doc: fitz.Document,
    spec: NumericProbeSpec,
) -> tuple[int, str, fitz.Rect, str]:
    for page in doc:
        text = page.get_text("text")
        if "漏电流和患者辅助电流的测量（潮湿预处理后）" not in text:
            continue
        for table in page.find_tables().tables:
            if not is_formal_report_table(table):
                continue
            rows = table.extract()
            mapping = header_map(rows[0])
            in_after = False
            current_metric = ""
            for row_index, row in enumerate(rows[1:], start=1):
                joined = compact("".join(str(value or "") for value in row))
                if "潮湿预处理后" in joined:
                    in_after = True
                for metric in ("对地漏电流", "接触电流", "患者漏电流", "患者辅助电流"):
                    if metric in joined:
                        current_metric = metric
                        break
                if (
                    in_after
                    and current_metric == spec.report_metric
                    and all(token in joined for token in spec.report_requirement_tokens)
                ):
                    value = display_text(row[mapping["检验结果"]])
                    cell = table.rows[row_index].cells[mapping["检验结果"]]
                    if cell is None:
                        raise RuntimeError("report numeric value was located structurally but its coordinate was unavailable")
                    requirement = " / ".join(spec.report_requirement_tokens)
                    return page.number + 1, value, fitz.Rect(cell), requirement
    raise RuntimeError(f"target report numeric result was not found for {spec.probe_id}")


def find_record_numeric_probe(
    doc: fitz.Document,
    spec: NumericProbeSpec,
) -> tuple[fitz.Page, fitz.table.Table, list[dict[str, Any]], list[dict[str, Any]]]:
    attempts: list[dict[str, Any]] = []
    anchor = compact(spec.record_anchor)
    for page in doc:
        if anchor not in compact(page.get_text("text")):
            continue
        tables = page.find_tables().tables
        if not tables:
            attempts.append({"probe_id": spec.probe_id, "pdf_page": page.number + 1, "reason": "未找到表格"})
            continue
        for table in tables:
            if table.col_count <= max(spec.result_columns):
                continue
            measurement_cells: list[dict[str, Any]] = []
            for row_index, row in enumerate(table.extract()):
                switches = tuple(compact(value) for value in row[: spec.switch_column_count])
                if len(switches) != spec.switch_column_count or any(value not in {"0", "1"} for value in switches):
                    continue
                for column in spec.result_columns:
                    cell_tuple = table.rows[row_index].cells[column]
                    if cell_tuple is None:
                        continue
                    cell = fitz.Rect(cell_tuple)
                    annotations = ink_for_cell(page, cell, tolerance=4)
                    if not annotations:
                        continue
                    measurement_cells.append(
                        {
                            "row_index": row_index,
                            "switches": switches,
                            "column": column,
                            "cell": cell,
                            "annotations": annotations,
                        }
                    )
            if measurement_cells:
                return page, table, measurement_cells, attempts
        attempts.append(
            {
                "probe_id": spec.probe_id,
                "pdf_page": page.number + 1,
                "reason": "受支持的结果列中没有可归属数字Ink",
            }
        )
    raise RuntimeError(f"record probe {spec.probe_id} has no attributable measurement cells: {attempts}")


def record_61_check(record: SourceFile, report: SourceFile, report_items: dict[int, dict[str, Any]], output_dir: Path) -> list[dict[str, Any]]:
    record_doc = fitz.open(record.path)
    report_doc = fitz.open(report.path)
    findings: list[dict[str, Any]] = []

    status_page: fitz.Page | None = None
    for page in record_doc:
        text = page.get_text("text")
        if "8.7.1" in text and "见附表8.7" in text:
            status_page = page
            break
    if status_page is None:
        raise RuntimeError("record 8.7.1 status page not found")
    words = status_page.get_text("words")
    header_centers: dict[str, float] = {}
    header_rects: list[fitz.Rect] = []
    for word in words:
        if word[4] in {"符合", "不符合", "不适用"}:
            header_centers[word[4]] = (word[0] + word[2]) / 2
            header_rects.append(fitz.Rect(word[:4]))
    table = status_page.find_tables().tables[0]
    status_rows: list[tuple[str, int]] = []
    inside_871 = False
    for row_index, row in enumerate(table.extract()):
        first = compact(row[0]) if row else ""
        second = compact(row[1]) if len(row) > 1 else ""
        if first == "8.7.1":
            inside_871 = True
            continue
        status_match = re.match(r"^([ab])[)）]", second) if inside_871 else None
        if status_match:
            status_rows.append((status_match.group(1), row_index))
        if len(status_rows) == 2:
            break
    parsed_statuses: list[dict[str, Any]] = []
    status_rects: list[fitz.Rect] = []
    headers_complete = set(header_centers) == {"符合", "不符合", "不适用"}
    for row_label, row_index in status_rows:
        row = table.rows[row_index]
        cell = row.cells[3]
        if cell is None:
            continue
        annotations = ink_for_cell(status_page, cell, tolerance=5)
        status_rects.append(fitz.Rect(cell))
        selected: list[str] = []
        for annotation in annotations:
            status_rects.append(fitz.Rect(annotation.rect))
            if headers_complete:
                x, _ = annotation_centroid(annotation)
                nearest = min(header_centers, key=lambda label: abs(header_centers[label] - x))
                selected.append(nearest)
        parsed_statuses.append({"label": row_label, "row": row_index, "selected": sorted(set(selected))})
    matching_report_items = [
        item
        for item in report_items.values()
        if compact(item.get("clause")) == "8.7" and "漏电流" in compact(item.get("project"))
    ]
    if len(matching_report_items) != 1:
        raise RuntimeError("report 8.7 item was not located uniquely")
    report_item = matching_report_items[0]
    report_item_number = report_item["sequence"]
    report_conclusions = [compact(value) for value in report_item.get("conclusions", []) if compact(value)]
    status_decision = compare_record_61_statuses(
        [item["selected"] for item in parsed_statuses]
        if headers_complete and sorted(item["label"] for item in parsed_statuses) == ["a", "b"]
        else [],
        report_conclusions,
    )
    record_status_header_evidence = render_evidence(
        status_page,
        header_rects or [fitz.Rect(table.bbox)],
        output_dir / "evidence" / "record61-status-column-headings.png",
        padding=18,
        full_width=True,
    )
    record_status_evidence = render_evidence(
        status_page,
        status_rects or [fitz.Rect(table.bbox)],
        output_dir / "evidence" / "record61-status-8.7.1.png",
        padding=28,
        full_width=True,
    )
    report_conclusion_rects: dict[int, list[fitz.Rect]] = defaultdict(list)
    for location in report_item.get("row_locations", []):
        if compact(location.get("conclusion")) and location.get("conclusion_rect"):
            report_conclusion_rects[location["pdf_page"]].append(fitz.Rect(location["conclusion_rect"]))
    if not report_conclusion_rects:
        for location in report_item.get("row_locations", []):
            if location.get("conclusion_rect"):
                report_conclusion_rects[location["pdf_page"]].append(fitz.Rect(location["conclusion_rect"]))
                break
    report_status_evidence = [
        render_evidence(
            report_doc[pdf_page - 1],
            rects,
            output_dir / "evidence" / f"report-item{report_item_number}-conclusion-page{pdf_page}.png",
            padding=30,
            full_width=True,
        )
        for pdf_page, rects in sorted(report_conclusion_rects.items())
    ]
    record_status_values = [item["selected"][0] for item in parsed_statuses if len(item["selected"]) == 1]
    if status_decision["status"] == "pass":
        status_summary = (
            f"Record 8.7.1 a)、b)聚合为“{status_decision['expected_report_conclusion']}”，"
            f"Report第{report_item_number}项全部单项结论一致"
        )
    elif status_decision["status"] == "manual":
        status_summary = "Record勾选或Report单项结论无法唯一可靠归类"
    else:
        status_summary = (
            f"Record聚合要求“{status_decision['expected_report_conclusion']}”，"
            f"Report第{report_item_number}项单项结论为{status_decision['report_conclusions']}"
        )
    findings.append(
        {
            "id": "RECORD61-STATUS",
            "title": "9706.1正文状态与Report结论",
            "status": status_decision["status"],
            "summary": status_summary,
            "details": {
                "record_headers_complete": headers_complete,
                "record_rows": parsed_statuses,
                "record_statuses": record_status_values,
                "report_item": report_item_number,
                "expected_report_conclusion": status_decision["expected_report_conclusion"],
                "report_conclusions": status_decision["report_conclusions"],
                "reason_code": status_decision["reason_code"],
            },
            "evidence": [record_status_header_evidence, record_status_evidence, *report_status_evidence],
        }
    )

    probe_attempts: list[dict[str, Any]] = []
    selected_probe: tuple[
        NumericProbeSpec,
        fitz.Page,
        fitz.table.Table,
        list[dict[str, Any]],
        int,
        str,
        fitz.Rect,
        str,
    ] | None = None
    for candidate_spec in NUMERIC_PROBE_SPECS:
        try:
            report_numeric = find_report_numeric_value(report_doc, candidate_spec)
        except RuntimeError as exc:
            probe_attempts.append({"probe_id": candidate_spec.probe_id, "reason": str(exc)})
            continue
        report_pdf_page, report_value, report_value_rect, requirement = report_numeric
        if compact(report_value) in {"", "——", "/"}:
            probe_attempts.append(
                {
                    "probe_id": candidate_spec.probe_id,
                    "report_pdf_page": report_pdf_page,
                    "report_value": report_value,
                    "reason": "Report该测量为占位值，不启动数字OCR",
                }
            )
            continue
        try:
            numeric_page, numeric_table, measurement_cells, record_attempts = find_record_numeric_probe(
                record_doc,
                candidate_spec,
            )
        except RuntimeError as exc:
            probe_attempts.append({"probe_id": candidate_spec.probe_id, "reason": str(exc)})
            continue
        probe_attempts.extend(record_attempts)
        selected_probe = (
            candidate_spec,
            numeric_page,
            numeric_table,
            measurement_cells,
            report_pdf_page,
            report_value,
            report_value_rect,
            requirement,
        )
        break
    if selected_probe is None:
        findings.append(
            {
                "id": "RECORD61-NUMERIC",
                "title": "9706.1实测值、单位换算与Report",
                "status": "manual",
                "summary": "未找到同时具有Report数值和可归属Record数字Ink的受支持测量链",
                "details": {"probe_attempts": probe_attempts, "automatic_comparison": None},
                "evidence": [],
            }
        )
        record_doc.close()
        report_doc.close()
        return findings

    (
        spec,
        numeric_page,
        numeric_table,
        measurement_cells,
        report_pdf_page,
        report_value,
        report_value_rect,
        requirement,
    ) = selected_probe
    cell_details: list[dict[str, Any]] = []
    crop_evidence: list[dict[str, Any]] = []
    all_annotation_rects: list[fitz.Rect] = []
    for index, measurement in enumerate(measurement_cells, start=1):
        cell = measurement["cell"]
        annotations = measurement["annotations"]
        annotation_rects = [fitz.Rect(annotation.rect) for annotation in annotations]
        all_annotation_rects.extend(annotation_rects)
        ink_bbox = union_rect(annotation_rects)
        crop_path = output_dir / "evidence" / f"record61-numeric-cell-{index:02d}.png"
        render_cell_for_ocr(numeric_page, cell, crop_path)
        ocr = local_ocr_candidates(crop_path)
        accepted_value = accepted_numeric_candidate(ocr)
        cell_details.append(
            {
                "record_row_index": measurement["row_index"],
                "switch_values": list(measurement["switches"]),
                "result_column": measurement["column"],
                "record_cell": rect_list(cell),
                "ink_count": len(annotations),
                "ink_bbox": rect_list(ink_bbox),
                "local_ocr_candidates": ocr,
                "accepted_value": accepted_value,
            }
        )
        crop_evidence.append(
            {
                "pdf_page": numeric_page.number + 1,
                "rects": [rect_list(cell), rect_list(ink_bbox)],
                "image": str(crop_path),
            }
        )
    context_evidence = render_evidence(
        numeric_page,
        [measurement["cell"] for measurement in measurement_cells],
        output_dir / "evidence" / "record61-leakage-context.png",
        padding=22,
        full_width=True,
        context_rects=[fitz.Rect(numeric_table.bbox)],
    )
    accepted_values = [detail["accepted_value"] for detail in cell_details]
    raw_value = max(accepted_values, key=Decimal) if accepted_values and all(accepted_values) else None
    automatic_comparison: dict[str, Any] | None = None
    if raw_value is not None and re.fullmatch(r"\d+(?:\.\d+)?", report_value):
        normalized = Decimal(raw_value) / Decimal(1000)
        decimals = len(report_value.partition(".")[2]) if "." in report_value else 0
        quantum = Decimal(1).scaleb(-decimals)
        rounded = normalized.quantize(quantum, rounding=ROUND_HALF_UP)
        automatic_comparison = {
            "record_raw": raw_value,
            "record_values": accepted_values,
            "aggregation": "maximum",
            "record_unit": "μA",
            "normalized": str(normalized),
            "report_unit": "mA",
            "rounded_to_report_precision": f"{rounded:.{decimals}f}",
            "report_value": report_value,
            "match": f"{rounded:.{decimals}f}" == report_value,
        }

    report_numeric_page = report_doc[report_pdf_page - 1]
    after_heading_rects = [fitz.Rect(rect) for rect in report_numeric_page.search_for("潮湿预处理后")]
    report_numeric_evidence = render_evidence(
        report_numeric_page,
        [report_value_rect],
        output_dir / "evidence" / "report61-numeric-result.png",
        padding=24,
        full_width=True,
        context_rects=after_heading_rects,
    )
    numeric_status = "manual" if automatic_comparison is None else "pass" if automatic_comparison["match"] else "error"
    numeric_summary = (
        "表格、Ink和Report值均已定位，但两个本地OCR未对手写数字形成一致可信结果"
        if automatic_comparison is None
        else "Record数值经单位换算及Report精度处理后与Report一致"
        if automatic_comparison["match"]
        else "Record数值换算后与Report不一致"
    )
    findings.append(
        {
            "id": "RECORD61-NUMERIC",
            "title": "9706.1实测值、单位换算与Report",
            "status": numeric_status,
            "summary": numeric_summary,
            "details": {
                "probe_id": spec.probe_id,
                "record_pdf_page": numeric_page.number + 1,
                "anchor": spec.description,
                "switch_column_count": spec.switch_column_count,
                "result_columns": list(spec.result_columns),
                "measurement_cells": cell_details,
                "record_cells": [detail["record_cell"] for detail in cell_details],
                "ink_count": sum(detail["ink_count"] for detail in cell_details),
                "ink_bbox": rect_list(union_rect(all_annotation_rects)),
                "skipped_supported_probes": probe_attempts,
                "accepted_record_value": raw_value,
                "report_pdf_page": report_pdf_page,
                "report_requirement": requirement,
                "report_value": report_value,
                "automatic_comparison": automatic_comparison,
                "decision_rule": "全部相关单元格均须由Apple Vision与Tesseract给出同一合法数值，之后取最大值、换算单位并按Report精度比较；任一格不可靠即待人工复核",
            },
            "evidence": [context_evidence, *crop_evidence, report_numeric_evidence],
        }
    )
    record_doc.close()
    report_doc.close()
    return findings


def leading_202_clause(requirement: str) -> str | None:
    match = re.match(r"^201\.15\.101\.(\d+)(?!\d)", compact(requirement))
    return f"201.15.101.{match.group(1)}" if match else None


def report_202_rows(doc: fitz.Document, target_sequence: int = 155) -> dict[str, list[dict[str, Any]]]:
    index: dict[str, list[dict[str, Any]]] = defaultdict(list)
    current_sequence: int | None = None
    current_clause: str | None = None
    for page in doc:
        text = page.get_text("text")
        if "201.15.101" not in text:
            continue
        for table in page.find_tables().tables:
            if not is_formal_report_table(table):
                continue
            rows = table.extract()
            mapping = header_map(rows[0])
            for row_index, row in enumerate(rows[1:], start=1):
                sequence_text = compact(row[mapping["序号"]])
                sequence_match = re.fullmatch(r"(续)?(\d+)", sequence_text)
                if sequence_match:
                    current_sequence = int(sequence_match.group(2))
                    if current_sequence != target_sequence:
                        current_clause = None
                if current_sequence != target_sequence:
                    continue
                requirement = display_text(row[mapping["标准要求"]])
                result = display_text(row[mapping["检验结果"]])
                clause = leading_202_clause(requirement)
                if clause:
                    current_clause = clause
                if current_clause and requirement:
                    result_cell = table.rows[row_index].cells[mapping["检验结果"]]
                    result_rect = fitz.Rect(result_cell) if result_cell is not None else None
                    index[current_clause].append(
                        {
                            "pdf_page": page.number + 1,
                            "requirement": requirement,
                            "result": result,
                            "result_rect": rect_list(result_rect) if result_rect else None,
                        }
                    )
    return index


def number_evidence_rects(page: fitz.Page, report_number: tuple[int, int] | None) -> list[fitz.Rect]:
    if report_number is not None:
        query = f"QW{report_number[0]} 第{report_number[1]} 号"
        matches = [fitz.Rect(rect) for rect in page.search_for(query)]
        if matches:
            return matches
    labels = [fitz.Rect(rect) for rect in page.search_for("编号")]
    if labels:
        label = union_rect(labels)
        return [
            fitz.Rect(
                max(page.rect.x0, label.x0 - 8),
                max(page.rect.y0, label.y0 - 8),
                min(page.rect.x1, page.rect.width - 24),
                min(page.rect.y1, label.y1 + 10),
            )
        ]
    return [fitz.Rect(24, 82, page.rect.width - 24, 145)]


def record_202_target_rows(
    doc: fitz.Document,
) -> tuple[int, list[dict[str, Any]], dict[int, list[int]]]:
    target_sequence: int | None = None
    active_sequence: int | None = None
    current_clause = ""
    rows_found: list[dict[str, Any]] = []
    table_shapes: dict[int, list[int]] = {}
    completed = False

    for page in doc:
        for table in page.find_tables().tables:
            if table.col_count < 5 or table.row_count < 3:
                continue
            rows = table.extract()
            for row_index in range(2, len(rows)):
                row = rows[row_index]
                sequence_text = compact(row[0]) if row else ""
                sequence_match = re.fullmatch(r"(续)?(\d+)", sequence_text)
                if sequence_match:
                    active_sequence = int(sequence_match.group(2))
                    project = compact(row[1]) if len(row) > 1 else ""
                    if target_sequence is None and "中性电极" in project:
                        target_sequence = active_sequence
                        current_clause = ""
                    elif target_sequence is not None and active_sequence != target_sequence and rows_found:
                        completed = True
                        break

                if target_sequence is None or active_sequence != target_sequence:
                    continue
                requirement = display_text(row[3]) if len(row) > 3 else ""
                clause = leading_202_clause(requirement)
                if clause:
                    current_clause = clause
                if not requirement or not current_clause:
                    continue
                cell_tuple = table.rows[row_index].cells[4]
                if cell_tuple is None:
                    rows_found.append(
                        {
                            "record_pdf_page": page.number + 1,
                            "clause": current_clause,
                            "requirement": requirement,
                            "record_cell": None,
                            "annotation_count": 0,
                            "record_symbol": "其他",
                            "shape_features": {},
                            "unresolved_reason": "实测数据单元格坐标缺失",
                            "evidence_rects": [],
                        }
                    )
                    continue
                cell = fitz.Rect(cell_tuple)
                annotations = ink_for_cell(page, cell, tolerance=6)
                if len(annotations) == 1:
                    symbol, features = classify_202_ink(annotations[0])
                    unresolved_reason = None if symbol != "其他" else "Ink几何无法归入已验证类别"
                else:
                    symbol = "其他"
                    features = {}
                    unresolved_reason = f"目标单元格归属到{len(annotations)}个Ink，无法唯一分类"
                evidence_rects = [fitz.Rect(annotation.rect) for annotation in annotations] or [cell]
                rows_found.append(
                    {
                        "record_pdf_page": page.number + 1,
                        "clause": current_clause,
                        "requirement": requirement,
                        "record_cell": rect_list(cell),
                        "annotation_count": len(annotations),
                        "record_symbol": symbol,
                        "shape_features": {key: round(value, 4) for key, value in features.items()},
                        "unresolved_reason": unresolved_reason,
                        "evidence_rects": [rect_list(rect) for rect in evidence_rects],
                    }
                )
                table_shapes[page.number + 1] = [table.row_count, table.col_count]
            if completed:
                break
        if completed:
            break

    if target_sequence is None or not rows_found:
        raise RuntimeError("9706.202 neutral-electrode rows were not located")
    return target_sequence, rows_found, table_shapes


def record_202_check(
    record: SourceFile,
    report: SourceFile,
    output_dir: Path,
    report_items: dict[int, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    record_doc = fitz.open(record.path)
    report_doc = fitz.open(report.path)
    findings: list[dict[str, Any]] = []

    report_number = extract_report_number(report_doc[0].get_text("text"))
    number_states = [classify_report_number_field(page.get_text("text")) for page in record_doc]
    numbers = [state["number"] for state in number_states]
    missing_pages = [idx + 1 for idx, state in enumerate(number_states) if state["state"] == "explicit_missing"]
    malformed_pages = [idx + 1 for idx, state in enumerate(number_states) if state["state"] == "malformed"]
    unreadable_pages = [
        idx + 1 for idx, state in enumerate(number_states) if state["state"] == "unreadable_or_unsupported"
    ]
    mismatched_pages = [
        idx + 1
        for idx, state in enumerate(number_states)
        if state["state"] == "parsed" and state["number"] != report_number
    ]
    matched_pages = sum(state["state"] == "parsed" and state["number"] == report_number for state in number_states)
    if report_number is None:
        number_status = "manual"
    elif missing_pages or malformed_pages or mismatched_pages:
        number_status = "error"
    elif unreadable_pages:
        number_status = "manual"
    else:
        number_status = "pass"
    number_evidence: list[dict[str, Any]] = []
    number_sources = [
        (report_doc[0], "report-number.png"),
        (record_doc[0], "record202-number-first-page.png"),
        (record_doc[-1], "record202-number-last-page.png"),
    ]
    for page, filename in number_sources:
        number_evidence.append(
            render_evidence(
                page,
                number_evidence_rects(page, report_number),
                output_dir / "evidence" / filename,
                padding=12,
                full_width=True,
            )
        )
    findings.append(
        {
            "id": "RECORD202-NUMBER",
            "title": "9706.202逐页报告编号",
            "status": number_status,
            "summary": (
                f"{record_doc.page_count}/{record_doc.page_count}页均解析为QW{report_number[0]}-{report_number[1]}，与Report一致"
                if number_status == "pass" and report_number
                else f"{len(missing_pages)}页编号明确空白，{len(mismatched_pages)}页编号不一致"
                if number_status == "error"
                else "Report或Record编号栏无法可靠解析"
            ),
            "details": {
                "report_number": report_number,
                "record_pages": record_doc.page_count,
                "matched_pages": matched_pages,
                "unmatched_pages": [idx + 1 for idx, number in enumerate(numbers) if number != report_number],
                "missing_pages": missing_pages,
                "mismatched_pages": mismatched_pages,
                "malformed_pages": malformed_pages,
                "unreadable_pages": unreadable_pages,
                "page_states": number_states,
                "parsed_record_numbers": [number for number in numbers],
            },
            "evidence": number_evidence,
        }
    )

    record_target_sequence, record_rows, record_table_shapes = record_202_target_rows(record_doc)

    legend_page = record_doc[-1]
    legend_rects = [fitz.Rect(rect) for rect in legend_page.search_for("注：")]
    if not legend_rects:
        legend_rects = [fitz.Rect(45, legend_page.rect.height - 72, legend_page.rect.width - 45, legend_page.rect.height - 28)]
    legend_evidence = render_evidence(
        legend_page,
        legend_rects,
        output_dir / "evidence" / "record202-symbol-legend.png",
        padding=20,
        full_width=True,
    )
    legend_mapping = verified_202_legend_mapping(legend_page.get_text("text").splitlines())
    legend_ocr: dict[str, Any] | None = None
    legend_source = "native_pdf_text" if legend_mapping is not None else "unresolved"
    legend_ok = legend_mapping is not None
    if not legend_ok:
        legend_ocr = local_text_ocr(Path(legend_evidence["image"]))
        legend_mapping = verified_202_legend_mapping(
            item["text"] for item in legend_ocr.get("lines", [])
        )
        legend_ok = legend_mapping is not None
        legend_source = "apple_vision_local_ocr" if legend_ok else "unresolved"

    if report_items is None:
        report_items, _ = scan_report_items(report_doc)
    target_sequences = [
        sequence
        for sequence, item in report_items.items()
        if "中性电极" in compact(item.get("project"))
        and (
            compact(item.get("clause")) == "201.15.101"
            or any(leading_202_clause(requirement) == "201.15.101.1" for requirement in item.get("requirements", []))
        )
    ]
    if len(target_sequences) != 1:
        raise RuntimeError(f"report neutral-electrode item was not located uniquely: {target_sequences}")
    target_sequence = target_sequences[0]
    report_index = report_202_rows(report_doc, target_sequence=target_sequence)
    comparisons: list[dict[str, Any]] = []
    record_rects_by_page: dict[int, list[fitz.Rect]] = defaultdict(list)
    report_rects_by_page: dict[int, list[fitz.Rect]] = defaultdict(list)
    offsets: dict[str, int] = defaultdict(int)
    for record_row in record_rows:
        clause = record_row["clause"]
        for rect in record_row["evidence_rects"]:
            record_rects_by_page[record_row["record_pdf_page"]].append(fitz.Rect(rect))
        report_rows = report_index.get(clause, [])
        offset = offsets[clause]
        report_row = report_rows[offset] if offset < len(report_rows) else None
        offsets[clause] += 1
        report_result = compact(report_row["result"]) if report_row else ""
        symbol = record_row["record_symbol"]
        unresolved_reason = record_row["unresolved_reason"]
        if not legend_ok:
            expected = "图例语义待人工复核"
            match: bool | None = None
            unresolved_reason = "图例未可靠核实"
        elif unresolved_reason:
            expected = "待人工复核"
            match = None
        elif report_row is None:
            expected = (legend_mapping or {}).get(symbol, "待人工复核")
            match = None
            unresolved_reason = "Report中未定位到同条款同序位结果"
        elif symbol == "√":
            match = bool(report_result and report_result not in {"——", "/", "不符合要求"})
            expected = "符合要求或合格实测值"
        elif symbol == "△":
            match = report_result == "——"
            expected = "——"
        elif symbol == "/":
            match = report_result == "/"
            expected = "/"
        elif symbol == "×":
            match = report_result == "不符合要求"
            expected = "不符合要求"
        else:
            match = None
            expected = "待人工复核"
            unresolved_reason = "符号类别尚未得到真实样本验证"
        if report_row and report_row.get("result_rect"):
            report_rects_by_page[report_row["pdf_page"]].append(fitz.Rect(report_row["result_rect"]))
        comparisons.append(
            {
                "clause": clause,
                "clause_occurrence": offset + 1,
                "record_pdf_page": record_row["record_pdf_page"],
                "record_cell": record_row["record_cell"],
                "annotation_count": record_row["annotation_count"],
                "record_symbol": symbol,
                "shape_features": record_row["shape_features"],
                "expected_report": expected,
                "report_result": report_row["result"] if report_row else "<未定位>",
                "report_pdf_page": report_row["pdf_page"] if report_row else None,
                "match": match,
                "decision": "manual" if match is None else "match" if match else "mismatch",
                "unresolved_reason": unresolved_reason,
            }
        )

    evidence = [legend_evidence]
    for page_number, rects in sorted(record_rects_by_page.items()):
        evidence.append(
            render_evidence(
                record_doc[page_number - 1],
                rects,
                output_dir / "evidence" / f"record202-page{page_number}-symbols.png",
                padding=24,
                full_width=True,
            )
        )
    for page_number, rects in sorted(report_rects_by_page.items()):
        evidence.append(
            render_evidence(
                report_doc[page_number - 1],
                rects,
                output_dir / "evidence" / f"report202-page{page_number}-results.png",
                padding=24,
                full_width=True,
            )
        )
    mismatches = [item for item in comparisons if item["decision"] == "mismatch"]
    unresolved = [item for item in comparisons if item["decision"] == "manual"]
    missing_report_rows = [
        {"clause": clause, "clause_occurrence": index + 1, "report_pdf_page": row["pdf_page"]}
        for clause, rows in report_index.items()
        for index, row in enumerate(rows)
        if index >= offsets[clause]
    ]
    if missing_report_rows:
        unresolved.append({"reason": "Report存在未与Record配对的目标行", "rows": missing_report_rows})
    symbol_status = deterministic_comparison_status(
        comparisons,
        has_additional_unresolved=bool(missing_report_rows),
    )
    if symbol_status == "error":
        symbol_summary = (
            f"目标项目跨PDF第{min(record_rects_by_page)}–{max(record_rects_by_page)}页；"
            f"{len(comparisons) - len(mismatches) - len([x for x in comparisons if x['decision'] == 'manual'])}项一致，"
            f"{len(mismatches)}项明确不一致，{len([x for x in comparisons if x['decision'] == 'manual'])}项待复核"
        )
    elif not comparisons:
        symbol_summary = "未获得可比较的目标符号"
    elif symbol_status == "manual":
        symbol_summary = f"目标项目已跨页解析，但有{len(unresolved)}项结构、图例或符号证据不足"
    elif comparisons:
        symbol_summary = (
            f"第{legend_page.number + 1}页图例已核实；目标项目跨PDF第{min(record_rects_by_page)}–"
            f"{max(record_rects_by_page)}页的{len(comparisons)}个Ink符号均独立分类并与Report一致"
        )
    findings.append(
        {
            "id": "RECORD202-SYMBOLS",
            "title": "9706.202 Ink符号与Report结果",
            "status": symbol_status,
            "summary": symbol_summary,
            "details": {
                "record_item": record_target_sequence,
                "record_pdf_pages": sorted(record_rects_by_page),
                "table_shapes": record_table_shapes,
                "legend_pdf_page": legend_page.number + 1,
                "legend_validated": legend_ok,
                "legend_source": legend_source,
                "legend_ocr": legend_ocr,
                "legend_mapping": legend_mapping,
                "report_item": target_sequence,
                "comparisons": comparisons,
                "matched_count": sum(item["decision"] == "match" for item in comparisons),
                "mismatch_count": len(mismatches),
                "unresolved_count": len([item for item in comparisons if item["decision"] == "manual"]),
                "mismatches": mismatches,
                "unresolved": unresolved,
                "unpaired_report_rows": missing_report_rows,
                "validated_classes": sorted({item["record_symbol"] for item in comparisons}) if legend_ok else [],
                "observed_symbol_classes": sorted({item["record_symbol"] for item in comparisons}),
                "unvalidated_classes": sorted(
                    {"√", "×", "△", "/"} - {item["record_symbol"] for item in comparisons}
                ) if legend_ok else sorted({"√", "×", "△", "/"}),
            },
            "evidence": evidence,
        }
    )
    record_doc.close()
    report_doc.close()
    return findings


def relative_evidence_paths(findings: list[dict[str, Any]], output_dir: Path) -> None:
    for finding in findings:
        for evidence in finding.get("evidence", []):
            path = Path(evidence["image"])
            evidence["image"] = str(path.relative_to(output_dir))


def overall_status(findings: list[dict[str, Any]]) -> str:
    return max((finding["status"] for finding in findings), key=lambda status: STATUS_RANK[status])


def load_human_evaluation(root: Path, sample: str) -> dict[str, Any] | None:
    review_path = root / "mvp" / "reviews" / f"{sample}.json"
    if not review_path.exists():
        return None
    review = json.loads(review_path.read_text(encoding="utf-8"))
    if "observed_value" not in review or "report_value" not in review:
        return review
    raw = Decimal(review["observed_value"])
    divisor = Decimal(str(review.get("conversion_divisor", "1000")))
    normalized = raw / divisor
    report_value = Decimal(review["report_value"])
    decimals = len(review["report_value"].partition(".")[2])
    quantum = Decimal(1).scaleb(-decimals)
    rounded = normalized.quantize(quantum, rounding=ROUND_HALF_UP)
    review["normalized_value"] = str(normalized)
    review["normalized_unit"] = review.get("normalized_unit", "mA")
    review["rounded_to_report_precision"] = f"{rounded:.{decimals}f}"
    review["matches_report"] = rounded == report_value
    return review


def validate_source(source: SourceFile) -> None:
    if not source.path.exists():
        raise FileNotFoundError(source.path)
    with fitz.open(source.path) as document:
        if document.needs_pass:
            raise ValueError(f"encrypted PDF is not supported: {source.path}")


def run_sample(root: Path, sample: str, output_dir: Path) -> dict[str, Any]:
    if sample not in SAMPLE_CONFIGS:
        raise KeyError(f"unsupported sample: {sample}")
    config = SAMPLE_CONFIGS[sample]
    evidence_dir = output_dir / "evidence"
    if evidence_dir.exists():
        shutil.rmtree(evidence_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    report = SourceFile("report", root / config.report)
    record61 = SourceFile("record_9706_1", root / config.record61) if config.record61 else None
    record202 = SourceFile("record_9706_202", root / config.record202) if config.record202 else None
    sources = [source for source in (report, record61, record202) if source is not None]
    for source in sources:
        validate_source(source)

    report_findings, report_items = report_self_check(report, output_dir)
    record61_findings = record_61_check(record61, report, report_items, output_dir) if record61 else []
    record202_findings = record_202_check(record202, report, output_dir, report_items) if record202 else []
    findings = report_findings + record61_findings + record202_findings
    relative_evidence_paths(findings, output_dir)
    human_evaluation = load_human_evaluation(root, sample)
    included = ["Report R01/R07/R09/R10/R11"]
    excluded = ["PTR", "Report照片语义/中文标签完整核对"]
    if record61:
        included.extend(["9706.1状态链", "9706.1数值链试跑"])
        excluded.append("9706.1全部数值页")
    else:
        excluded.append("本样本未提供9706.1 record")
    if record202:
        included.append("9706.202报告编号、图例与符号链")
        excluded.extend(["9706.202表3", "未在本样本出现或未可靠分类的符号类别"])
    else:
        excluded.append("本样本未提供9706.202 record")
    result = {
        "schema_version": "mvp-0.3",
        "sample": sample,
        "engine": {
            "llm_used": False,
            "agent_used": False,
            "meaning": "自动解析运行时未调用LLM或Agent；人工评判单独列示且不改变自动状态",
            "python_version": platform.python_version(),
            "pymupdf_version": fitz.VersionBind,
            "components": ["PyMuPDF", "PDF原生文字", "矢量表格", "Ink几何", "Apple Vision OCR候选", "Tesseract候选", "确定性规则"],
        },
        "scope": {
            "included": included,
            "excluded": excluded,
        },
        "files": [
            {"role": source.role, "path": str(source.path.relative_to(root)), "sha256": sha256_file(source.path)}
            for source in sources
        ],
        "overall_status": overall_status(findings),
        "findings": findings,
        "human_evaluation": human_evaluation,
    }
    json_path = output_dir / "result.json"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    html_path = output_dir / "index.html"
    html_path.write_text(render_html(result), encoding="utf-8")
    return result


def run_1539(root: Path, output_dir: Path) -> dict[str, Any]:
    return run_sample(root, "1539", output_dir)


def render_html(result: dict[str, Any]) -> str:
    counts = {status: 0 for status in STATUS_RANK}
    for finding in result["findings"]:
        counts[finding["status"]] += 1

    def render_detail(value: Any) -> str:
        return html.escape(json.dumps(value, ensure_ascii=False, indent=2, default=str))

    cards: list[str] = []
    for finding in result["findings"]:
        evidence_html = "".join(
            f'<figure><a href="{html.escape(item["image"])}" target="_blank" rel="noopener"><img src="{html.escape(item["image"])}" alt="{html.escape(finding["title"])}证据"></a><figcaption>PDF第{item["pdf_page"]}页；区域 {html.escape(str(item["rects"]))}；点击查看原图</figcaption></figure>'
            for item in finding.get("evidence", [])
        )
        cards.append(
            f"""
            <article class="finding {finding['status']}">
              <div class="finding-head">
                <div><span class="rule-id">{html.escape(finding['id'])}</span><h2>{html.escape(finding['title'])}</h2></div>
                <span class="badge">{STATUS_LABEL[finding['status']]}</span>
              </div>
              <p class="summary">{html.escape(finding['summary'])}</p>
              <details><summary>结构化详情</summary><pre>{render_detail(finding.get('details'))}</pre></details>
              <div class="evidence-grid">{evidence_html}</div>
            </article>
            """
        )

    source_rows = "".join(
        f"<tr><td>{html.escape(item['role'])}</td><td>{html.escape(item['path'])}</td><td><code>{html.escape(item['sha256'][:16])}…</code></td></tr>"
        for item in result["files"]
    )
    review = result.get("human_evaluation")
    review_html = ""
    if review:
        review_status = "一致" if review.get("matches_report") else "不一致"
        divisor = html.escape(str(review.get("conversion_divisor", "1000")))
        review_html = f"""
        <section class="assessment">
          <div class="assessment-head"><div><span class="eyebrow dark">POST-RUN REVIEW</span><h2>我的人工视觉评判</h2></div><span class="review-badge">{review_status}</span></div>
          <p>{html.escape(review['statement'])}</p>
          <div class="calculation"><code>{html.escape(review['observed_value'])} {html.escape(review['observed_unit'])}</code><span>÷ {divisor}</span><code>{html.escape(review['normalized_value'])} {html.escape(review['normalized_unit'])}</code><span>按Report显示精度</span><code>{html.escape(review['rounded_to_report_precision'])} {html.escape(review['normalized_unit'])}</code><span>对比</span><code>{html.escape(review['report_value'])} {html.escape(review['normalized_unit'])}</code></div>
          <p class="review-note">该人工读数不写回自动识别结果，也不改变任何自动判定；数值项仍保持“待人工复核”，其他已发现错误仍保留。</p>
        </section>
        """
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(result['sample'])} 无LLM核对MVP</title>
  <style>
    :root {{ color-scheme: light; --ink:#101828; --muted:#667085; --line:#e4e7ec; --bg:#f6f7fb; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif; color:var(--ink); background:var(--bg); }}
    main {{ width:min(1180px,calc(100% - 40px)); margin:36px auto 72px; }}
    .hero {{ padding:30px; color:white; border-radius:22px; background:linear-gradient(135deg,#0b1220,#173b66); box-shadow:0 20px 45px #10182820; }}
    .hero h1 {{ margin:8px 0 10px; font-size:32px; }}
    .hero p {{ margin:0; color:#d0d5dd; }}
    .eyebrow {{ font-size:13px; letter-spacing:.12em; text-transform:uppercase; color:#84caff; }}
    .overall {{ display:inline-flex; margin-top:18px; padding:8px 12px; border-radius:999px; background:#ffffff1c; font-weight:700; }}
    .metrics {{ display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin:18px 0; }}
    .metric {{ padding:18px; background:white; border:1px solid var(--line); border-radius:16px; }}
    .metric strong {{ display:block; font-size:26px; }} .metric span {{ color:var(--muted); font-size:13px; }}
    .scope,.sources {{ background:white; border:1px solid var(--line); border-radius:16px; padding:20px; margin:18px 0; }}
    .assessment {{ background:#f4f3ff; border:1px solid #d9d6fe; border-radius:16px; padding:22px; margin:18px 0; }}
    .assessment-head {{ display:flex; justify-content:space-between; gap:16px; align-items:flex-start; }}
    .assessment h2 {{ margin:4px 0 8px; }} .eyebrow.dark {{ color:#5925dc; }}
    .review-badge {{ white-space:nowrap; padding:7px 11px; border-radius:999px; background:#12b76a; color:white; font-weight:700; }}
    .calculation {{ display:flex; flex-wrap:wrap; align-items:center; gap:9px; padding:14px; border-radius:12px; background:white; border:1px solid #d9d6fe; }}
    .calculation code {{ font-size:15px; font-weight:700; color:#42307d; }} .calculation span {{ color:var(--muted); }}
    .review-note {{ margin-bottom:0; color:#53389e; font-size:13px; }}
    .scope-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:20px; }}
    ul {{ margin:8px 0 0; padding-left:20px; }}
    table {{ width:100%; border-collapse:collapse; }} th,td {{ padding:10px; text-align:left; border-bottom:1px solid var(--line); vertical-align:top; }}
    .finding {{ margin:16px 0; padding:22px; background:white; border:1px solid var(--line); border-left-width:6px; border-radius:16px; }}
    .finding.pass {{ border-left-color:#12b76a; }} .finding.warning {{ border-left-color:#f79009; }} .finding.manual {{ border-left-color:#7f56d9; }} .finding.error {{ border-left-color:#f04438; }}
    .finding-head {{ display:flex; justify-content:space-between; gap:16px; align-items:flex-start; }}
    .finding h2 {{ margin:4px 0 0; font-size:20px; }} .rule-id {{ font-size:12px; color:var(--muted); }}
    .badge {{ white-space:nowrap; padding:6px 10px; border-radius:999px; background:#f2f4f7; font-weight:700; font-size:13px; }}
    .summary {{ font-size:16px; line-height:1.7; }}
    details {{ margin-top:12px; }} summary {{ cursor:pointer; color:#175cd3; }}
    pre {{ overflow:auto; padding:14px; border-radius:12px; background:#101828; color:#e4e7ec; font-size:12px; }}
    .evidence-grid {{ display:grid; grid-template-columns:1fr; gap:14px; margin-top:16px; }}
    figure {{ margin:0; border:1px solid var(--line); border-radius:12px; overflow:hidden; background:#f9fafb; }}
    figure a {{ display:block; }} figure img {{ display:block; width:100%; max-height:640px; object-fit:contain; background:white; }}
    figcaption {{ padding:9px 12px; color:var(--muted); font-size:12px; }}
    .notice {{ margin-top:20px; padding:14px 16px; border-radius:12px; background:#fffaeb; color:#7a2e0e; }}
    @media(max-width:760px) {{ .metrics,.scope-grid {{ grid-template-columns:1fr; }} main {{ width:min(100% - 20px,1180px); }} }}
  </style>
</head>
<body>
<main>
  <section class="hero">
    <div class="eyebrow">Deterministic PDF proof of concept</div>
    <h1>{html.escape(result['sample'])} 无LLM核对MVP</h1>
    <p>自动解析运行时未调用大模型或Agent；原始PDF只读，证据来自原生文字、矢量表格和Ink坐标。</p>
    <div class="overall">自动总体状态：{STATUS_LABEL[result['overall_status']]}</div>
  </section>
  <section class="metrics">
    <div class="metric"><strong>{counts['pass']}</strong><span>通过</span></div>
    <div class="metric"><strong>{counts['warning']}</strong><span>有警示</span></div>
    <div class="metric"><strong>{counts['manual']}</strong><span>待人工复核</span></div>
    <div class="metric"><strong>{counts['error']}</strong><span>不通过</span></div>
  </section>
  <section class="scope">
    <h2>MVP边界</h2>
    <div class="scope-grid"><div><strong>本次覆盖</strong><ul>{''.join(f'<li>{html.escape(x)}</li>' for x in result['scope']['included'])}</ul></div><div><strong>本次未覆盖</strong><ul>{''.join(f'<li>{html.escape(x)}</li>' for x in result['scope']['excluded'])}</ul></div></div>
    <div class="notice">“待人工复核”不是失败：它表示结构定位已经完成，但现有本地OCR不足以安全确认手写数字，因此程序拒绝猜测或反向套用Report结果。</div>
  </section>
  {review_html}
  <section class="sources"><h2>输入文件与哈希</h2><table><thead><tr><th>角色</th><th>只读源文件</th><th>SHA-256</th></tr></thead><tbody>{source_rows}</tbody></table></section>
  {''.join(cards)}
</main>
</body>
</html>"""


def render_comparison_html(results: list[dict[str, Any]]) -> str:
    finding_order = [
        "REPORT-R01",
        "REPORT-R11",
        "REPORT-R09-R10",
        "REPORT-R07",
        "RECORD61-STATUS",
        "RECORD61-NUMERIC",
        "RECORD202-NUMBER",
        "RECORD202-SYMBOLS",
    ]
    finding_titles = {
        finding["id"]: finding["title"]
        for result in results
        for finding in result["findings"]
    }
    result_maps = [{finding["id"]: finding for finding in result["findings"]} for result in results]
    header_cells = "".join(
        f'<th><a href="../mvp-{html.escape(result["sample"])}/index.html">{html.escape(result["sample"])}</a>'
        f'<span class="overall {html.escape(result["overall_status"])}">{STATUS_LABEL[result["overall_status"]]}</span></th>'
        for result in results
    )
    matrix_rows: list[str] = []
    for finding_id in finding_order:
        cells: list[str] = []
        for mapping in result_maps:
            finding = mapping.get(finding_id)
            if finding is None:
                cells.append('<td class="not-run"><span class="pill">未运行</span><p>本样本未提供对应Record。</p></td>')
                continue
            cells.append(
                f'<td class="{html.escape(finding["status"])}"><span class="pill">{STATUS_LABEL[finding["status"]]}</span>'
                f'<p>{html.escape(finding["summary"])}</p></td>'
            )
        matrix_rows.append(
            f'<tr><th scope="row"><code>{html.escape(finding_id)}</code><span>{html.escape(finding_titles.get(finding_id, ""))}</span></th>{"".join(cells)}</tr>'
        )

    sample_cards: list[str] = []
    for result in results:
        counts = {status: 0 for status in STATUS_RANK}
        for finding in result["findings"]:
            counts[finding["status"]] += 1
        important = [
            finding for finding in result["findings"] if finding["status"] in {"error", "manual", "warning"}
        ]
        review = result.get("human_evaluation")
        review_line = ""
        if review:
            review_line = (
                f'<p class="review">人工视觉：{html.escape(review["observed_value"])} {html.escape(review["observed_unit"])}'
                f' → {html.escape(review["rounded_to_report_precision"])} {html.escape(review["normalized_unit"])}，'
                f'{"与Report一致" if review.get("matches_report") else "与Report不一致"}；不改变自动状态。</p>'
            )
        sample_cards.append(
            f"""
            <article class="sample-card">
              <div class="sample-head"><div><span class="kicker">SAMPLE</span><h2>{html.escape(result['sample'])}</h2></div><span class="overall {html.escape(result['overall_status'])}">{STATUS_LABEL[result['overall_status']]}</span></div>
              <div class="counts"><span>通过 {counts['pass']}</span><span>警示 {counts['warning']}</span><span>复核 {counts['manual']}</span><span>错误 {counts['error']}</span></div>
              <ul>{''.join(f'<li><strong>{html.escape(item["id"])}</strong>：{html.escape(item["summary"])}</li>' for item in important) or '<li>没有警示、待复核或不通过项。</li>'}</ul>
              {review_line}
              <a class="detail-link" href="../mvp-{html.escape(result['sample'])}/index.html">打开完整结果与证据 →</a>
            </article>
            """
        )

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>无LLM三样本横向比较</title>
  <style>
    :root {{ color-scheme:light; --ink:#101828; --muted:#667085; --line:#d0d5dd; --bg:#f6f7fb; }}
    * {{ box-sizing:border-box; }} body {{ margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif; color:var(--ink); background:var(--bg); }}
    main {{ width:min(1440px,calc(100% - 36px)); margin:32px auto 64px; }}
    .hero {{ padding:30px; border-radius:22px; color:white; background:linear-gradient(135deg,#101828,#1849a9); box-shadow:0 18px 42px #10182822; }}
    .hero h1 {{ margin:7px 0 10px; font-size:32px; }} .hero p {{ margin:0; max-width:920px; color:#d0d5dd; line-height:1.7; }}
    .kicker {{ color:#84caff; font-size:12px; letter-spacing:.14em; }}
    .cards {{ display:grid; grid-template-columns:repeat(3,1fr); gap:14px; margin:18px 0; }}
    .sample-card,.matrix-wrap {{ background:white; border:1px solid #e4e7ec; border-radius:16px; padding:20px; }}
    .sample-head {{ display:flex; justify-content:space-between; gap:14px; align-items:flex-start; }} .sample-head h2 {{ margin:3px 0 0; }}
    .overall,.pill {{ display:inline-flex; border-radius:999px; padding:6px 10px; font-size:13px; font-weight:700; white-space:nowrap; background:#f2f4f7; }}
    .overall.pass,.td.pass .pill {{ background:#dcfae6; color:#067647; }} .overall.manual {{ background:#f4ebff; color:#6938ef; }} .overall.error {{ background:#fee4e2; color:#b42318; }}
    .counts {{ display:flex; flex-wrap:wrap; gap:7px; margin:14px 0; }} .counts span {{ background:#f2f4f7; border-radius:8px; padding:5px 8px; font-size:12px; }}
    ul {{ padding-left:20px; color:#344054; line-height:1.55; }} .review {{ color:#53389e; background:#f4f3ff; border-radius:10px; padding:10px; font-size:13px; }}
    .detail-link {{ color:#175cd3; font-weight:700; text-decoration:none; }}
    .matrix-wrap {{ overflow-x:auto; }} table {{ width:100%; min-width:1000px; border-collapse:collapse; }} th,td {{ border-bottom:1px solid #e4e7ec; padding:13px; text-align:left; vertical-align:top; }}
    thead th {{ background:#f9fafb; }} thead a {{ display:block; color:#1849a9; font-size:18px; margin-bottom:8px; }} tbody th {{ width:210px; }} tbody th code,tbody th span {{ display:block; }} tbody th span {{ color:var(--muted); margin-top:5px; font-size:12px; }} td p {{ margin:8px 0 0; line-height:1.5; }}
    td.pass .pill {{ background:#dcfae6; color:#067647; }} td.warning .pill {{ background:#fef0c7; color:#b54708; }} td.manual .pill {{ background:#f4ebff; color:#6938ef; }} td.error .pill {{ background:#fee4e2; color:#b42318; }} td.not-run .pill {{ color:#475467; }}
    .boundary {{ margin-top:18px; padding:14px 16px; border-radius:12px; background:#fffaeb; color:#7a2e0e; line-height:1.6; }}
    @media(max-width:900px) {{ .cards {{ grid-template-columns:1fr; }} main {{ width:min(100% - 20px,1440px); }} }}
  </style>
</head>
<body><main>
  <section class="hero"><span class="kicker">DETERMINISTIC PDF MVP</span><h1>三份真实样本横向比较</h1><p>自动解析运行时未调用LLM或Agent。比较范围是Report R01/R07/R09/R10/R11，以及样本具备的9706.1与9706.202目标链；人工视觉读数单独列示，不改变自动判定。</p></section>
  <section class="cards">{''.join(sample_cards)}</section>
  <section class="matrix-wrap"><h2>检查项矩阵</h2><table><thead><tr><th>检查项</th>{header_cells}</tr></thead><tbody>{''.join(matrix_rows)}</tbody></table></section>
  <div class="boundary">这是可行性MVP的真实PDF回归，不是Report、9706.1 record或9706.202 record的全项目审核；PTR、照片语义和未验证符号类别仍不在本轮范围。</div>
</main></body></html>"""


def write_comparison(results: list[dict[str, Any]], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        "schema_version": "mvp-comparison-0.1",
        "samples": [
            {
                "sample": result["sample"],
                "overall_status": result["overall_status"],
                "findings": [
                    {"id": finding["id"], "status": finding["status"], "summary": finding["summary"]}
                    for finding in result["findings"]
                ],
            }
            for result in results
        ],
    }
    (output_dir / "comparison.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    html_path = output_dir / "index.html"
    html_path.write_text(render_comparison_html(results), encoding="utf-8")
    return html_path
