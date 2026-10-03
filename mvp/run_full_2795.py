from __future__ import annotations

import argparse
import hashlib
import html
import json
import platform
import re
import shutil
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

try:
    import pymupdf as fitz
except ImportError:
    import fitz

from mvp.checker import (
    SAMPLE_CONFIGS,
    STATUS_LABEL,
    SourceFile,
    compact,
    header_map,
    is_formal_report_table,
    number_evidence_rects,
    overall_status,
    record_202_check,
    relative_evidence_paths,
    render_evidence,
    render_html,
    report_self_check,
    rect_list,
    scan_report_items,
    sha256_file,
    validate_source,
)
from mvp.full_report_attempt import scan_report_numeric_file
from mvp.full_report_photo import analyze_report_photo_rules


REPORT_RULE_ORDER = (
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
RECORD_202_RULE_ORDER = ("RECORD202-NUMBER", "RECORD202-SYMBOLS")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_write(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


def _file_state(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def _relative_source(root: Path, source: SourceFile) -> dict[str, Any]:
    return {
        "role": source.role,
        "path": str(source.path.relative_to(root)),
        "sha256": sha256_file(source.path),
        "size": source.path.stat().st_size,
    }


def _status_counts(findings: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    values = list(findings)
    return {
        status: sum(item.get("status") == status for item in values)
        for status in ("pass", "warning", "manual", "error")
    }


def _coverage(findings: list[dict[str, Any]], planned: Iterable[str]) -> dict[str, Any]:
    planned_ids = list(planned)
    by_id = {finding["id"]: finding for finding in findings}
    missing = [rule_id for rule_id in planned_ids if rule_id not in by_id]
    unexpected = [finding_id for finding_id in by_id if finding_id not in planned_ids]
    if missing or unexpected:
        raise RuntimeError(
            f"rule coverage mismatch: missing={missing}, unexpected={unexpected}"
        )
    executions = [
        {
            "rule_id": rule_id,
            "execution_status": "succeeded",
            "machine_status": by_id[rule_id]["status"],
        }
        for rule_id in planned_ids
    ]
    return {
        "planned": len(planned_ids),
        "attempted": len(executions),
        "completed": len(executions),
        "missing": [],
        "rule_executions": executions,
    }


def _image_index(record: Mapping[str, Any]) -> int | None:
    value = record.get("image_index")
    if isinstance(value, int):
        return value
    match = re.search(r"image-(\d+)", str(record.get("path") or ""))
    return int(match.group(1)) if match else None


def verify_ocr_derivatives(
    root: Path,
    report_path: Path,
    ocr_paths: Iterable[Path],
) -> dict[str, Any]:
    """Bind cached OCR JSON and extracted image bytes to the current Report PDF."""

    checked: list[dict[str, Any]] = []
    with fitz.open(report_path) as document:
        for ocr_path in ocr_paths:
            payload = json.loads(ocr_path.read_text(encoding="utf-8"))
            if not isinstance(payload, list):
                raise ValueError(f"OCR derivative is not a list: {ocr_path}")
            for record_index, record in enumerate(payload, start=1):
                if not isinstance(record, dict) or not isinstance(record.get("pdf_page"), int):
                    raise ValueError(f"invalid OCR derivative row {record_index}: {ocr_path}")
                image_index = _image_index(record)
                if image_index is None:
                    raise ValueError(f"OCR image index missing at row {record_index}: {ocr_path}")
                page = document[record["pdf_page"] - 1]
                image_info = sorted(
                    page.get_image_info(xrefs=True),
                    key=lambda item: (float(item["bbox"][1]), float(item["bbox"][0])),
                )
                if image_index < 1 or image_index > len(image_info):
                    raise ValueError(
                        f"OCR image index {image_index} is outside PDF page {record['pdf_page']}"
                    )
                xref = image_info[image_index - 1].get("xref")
                if not isinstance(xref, int) or xref <= 0:
                    raise ValueError(
                        f"PDF image xref is unavailable on page {record['pdf_page']} image {image_index}"
                    )
                embedded = document.extract_image(xref)["image"]
                image_path = Path(str(record.get("path") or ""))
                if not image_path.is_absolute():
                    image_path = root / image_path
                if not image_path.is_file():
                    raise FileNotFoundError(image_path)
                embedded_hash = hashlib.sha256(embedded).hexdigest()
                derivative_hash = sha256_file(image_path)
                if embedded_hash != derivative_hash:
                    raise RuntimeError(
                        f"OCR derivative image is not the current Report image: {image_path}"
                    )
                checked.append(
                    {
                        "ocr_file": str(ocr_path.relative_to(root)),
                        "pdf_page": record["pdf_page"],
                        "image_index": image_index,
                        "image_sha256": derivative_hash,
                        "matches_embedded_report_image": True,
                    }
                )
    return {
        "status": "pass",
        "ocr_files": [
            {
                "path": str(path.relative_to(root)),
                "sha256": sha256_file(path),
                "records": sum(
                    1
                    for item in json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(item, dict)
                ),
            }
            for path in ocr_paths
        ],
        "image_bindings_checked": len(checked),
        "image_bindings": checked,
    }


def _render_photo_rule_evidence(
    report_path: Path,
    finding: dict[str, Any],
    output_dir: Path,
) -> None:
    locators = list(finding.get("evidence", []))
    finding.setdefault("details", {})["evidence_locations"] = locators
    grouped: dict[int, list[fitz.Rect]] = defaultdict(list)
    seen: set[tuple[int, tuple[float, float, float, float]]] = set()
    for locator in locators:
        if locator.get("document_role") != "report":
            continue
        page_number = locator.get("pdf_page")
        rect_values = locator.get("rects") or []
        if not isinstance(page_number, int):
            continue
        for rect_value in rect_values:
            rect = fitz.Rect(rect_value)
            key = (page_number, tuple(round(value, 3) for value in rect))
            if rect.is_empty or key in seen:
                continue
            seen.add(key)
            grouped[page_number].append(rect)

    rendered: list[dict[str, Any]] = []
    with fitz.open(report_path) as document:
        for page_number, rects in sorted(grouped.items()):
            rendered_item = render_evidence(
                document[page_number - 1],
                rects,
                output_dir
                / "evidence"
                / f"{finding['id'].lower()}-page{page_number}.png",
                padding=20,
                full_width=True,
            )
            rendered_item["document_role"] = "report"
            rendered.append(rendered_item)
    finding["evidence"] = rendered


def _numeric_row_rects(
    document: fitz.Document,
    comparison: Mapping[str, Any],
) -> list[fitz.Rect]:
    page_number = comparison.get("pdf_page")
    row_index = comparison.get("row_index")
    if not isinstance(page_number, int) or not isinstance(row_index, int):
        return []
    page = document[page_number - 1]
    for table in page.find_tables().tables:
        if not is_formal_report_table(table):
            continue
        rows = table.extract()
        mapping = header_map(rows[0])
        if row_index >= len(rows):
            continue
        row = rows[row_index]
        result_index = mapping["检验结果"]
        observed = compact(row[result_index]) if result_index < len(row) else ""
        if observed != compact(comparison.get("result")):
            continue
        rects: list[fitz.Rect] = []
        for column_index in [
            *comparison.get("standard_columns", []),
            result_index,
        ]:
            if not isinstance(column_index, int):
                continue
            cell = table.rows[row_index].cells[column_index]
            if cell is not None:
                rects.append(fitz.Rect(cell))
        return rects
    fallback = comparison.get("result_rect")
    return [fitz.Rect(fallback)] if fallback else []


def _r07b_finding(report_path: Path, output_dir: Path) -> dict[str, Any]:
    scan = scan_report_numeric_file(report_path)
    if scan["counts"]["error"]:
        status = "error"
    elif scan["counts"]["manual"]:
        status = "manual"
    else:
        status = "pass"

    issue_rects: dict[int, list[fitz.Rect]] = defaultdict(list)
    with fitz.open(report_path) as document:
        for comparison in scan["comparisons"]:
            if comparison["status"] == "pass":
                continue
            rects = _numeric_row_rects(document, comparison)
            comparison["evidence_rects"] = [
                [round(value, 3) for value in rect] for rect in rects
            ]
            issue_rects[comparison["pdf_page"]].extend(rects)

        evidence = []
        for page_number, rects in sorted(issue_rects.items()):
            rendered = render_evidence(
                document[page_number - 1],
                rects,
                output_dir / "evidence" / f"report-r07b-page{page_number}.png",
                padding=22,
                full_width=True,
            )
            rendered["document_role"] = "report"
            evidence.append(rendered)

    counts = scan["counts"]
    return {
        "id": "REPORT-R07-B",
        "title": "实测结果与接受标准",
        "status": status,
        "summary": (
            f"识别到{scan['numeric_measurements']}项数值测量："
            f"{counts['pass']}项通过、{counts['error']}项不通过、"
            f"{counts['manual']}项待人工复核"
        ),
        "details": {
            "scope": "只比较Report中的数值型实测结果与同行接受标准；不使用LLM或Agent裁决",
            "counts": counts,
            "digit_containing_result_cells": scan["digit_containing_result_cells"],
            "numeric_measurements": scan["numeric_measurements"],
            "excluded_numeric_tokens": scan["excluded_numeric_tokens"],
            "comparisons": scan["comparisons"],
        },
        "evidence": evidence,
    }


def _r08_finding(items: Mapping[int, Mapping[str, Any]]) -> dict[str, Any]:
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


def _r09_r10_findings(diagnostics: Mapping[str, Any]) -> list[dict[str, Any]]:
    new_sequence_order = [
        marker[2]
        for page in diagnostics["page_sequences"]
        for marker in page["explicit"]
        if marker[1] == "new"
    ]
    expected = (
        list(range(1, max(new_sequence_order) + 1))
        if new_sequence_order
        else []
    )
    sequence_ok = bool(new_sequence_order) and new_sequence_order == expected
    sequence_errors = list(diagnostics["sequence_gaps"])
    if not new_sequence_order:
        sequence_errors.append("未识别到新检验项目序号")
    elif new_sequence_order[0] != 1:
        sequence_errors.append(f"首个新项目序号为{new_sequence_order[0]}，不是1")
    if len(new_sequence_order) != len(set(new_sequence_order)):
        duplicates = sorted(
            number for number in set(new_sequence_order) if new_sequence_order.count(number) > 1
        )
        sequence_errors.append(f"新项目序号重复：{duplicates}")
    sequence_ok = sequence_ok and not sequence_errors

    continuation_errors = list(diagnostics["continuation_errors"])
    continuation_ok = not continuation_errors
    first_markers = [
        {
            "pdf_page": page["pdf_page"],
            "first_marker": page["explicit"][0][1:] if page["explicit"] else None,
        }
        for page in diagnostics["page_sequences"]
    ]
    return [
        {
            "id": "REPORT-R09",
            "title": "新检验项目序号连续性",
            "status": "pass" if sequence_ok else "error",
            "summary": (
                f"新检验项目序号从1连续至{new_sequence_order[-1]}"
                if sequence_ok
                else "发现新检验项目序号缺失、重复、跳号或倒序"
            ),
            "details": {
                "new_sequence_order": new_sequence_order,
                "expected_sequence_order": expected,
                "errors": sequence_errors,
            },
            "evidence": [],
        },
        {
            "id": "REPORT-R10",
            "title": "跨页首项“续N”检查",
            "status": "pass" if continuation_ok else "error",
            "summary": (
                f"检查{len(first_markers)}个正式表格页，跨页首项“续N”位置及序号正确"
                if continuation_ok
                else f"发现{len(continuation_errors)}个跨页标记错误"
            ),
            "details": {
                "formal_table_pages_checked": len(first_markers),
                "first_markers_by_page": first_markers,
                "errors": continuation_errors,
            },
            "evidence": [],
        },
    ]


def _engine() -> dict[str, Any]:
    return {
        "llm_used": False,
        "agent_used": False,
        "meaning": "本次自动解析与业务判定未调用LLM或Agent；OCR为已绑定到当前PDF内嵌图像的本地派生结果",
        "python_version": platform.python_version(),
        "pymupdf_version": fitz.VersionBind,
        "components": [
            "PyMuPDF",
            "PDF原生文字",
            "矢量表格",
            "Ink几何",
            "Apple Vision OCR派生结果",
            "确定性规则",
        ],
    }


def _write_run(output_dir: Path, result: dict[str, Any]) -> dict[str, Any]:
    relative_evidence_paths(result["findings"], output_dir)
    _json_write(output_dir / "result.json", result)
    rendered_html = render_html(result)
    role_labels = {
        "report": "Report",
        "record_9706_1": "GB 9706.1 Record",
        "record_9706_202": "GB 9706.202 Record",
    }
    for finding in result["findings"]:
        for evidence in finding.get("evidence", []):
            role_label = role_labels.get(evidence.get("document_role"))
            if not role_label:
                continue
            old_caption = (
                f'<figcaption>PDF第{evidence["pdf_page"]}页；区域 '
                f'{html.escape(str(evidence["rects"]))}'
            )
            new_caption = (
                f'<figcaption>{html.escape(role_label)} PDF第{evidence["pdf_page"]}页；区域 '
                f'{html.escape(str(evidence["rects"]))}'
            )
            rendered_html = rendered_html.replace(old_caption, new_caption, 1)
    (output_dir / "index.html").write_text(rendered_html, encoding="utf-8")
    return result


def _build_report_run(
    root: Path,
    report: SourceFile,
    output_dir: Path,
    label_ocr_json: Path,
    object_ocr_json: Path,
    ocr_verification: dict[str, Any],
) -> tuple[dict[str, Any], dict[int, dict[str, Any]]]:
    output_dir.mkdir(parents=True, exist_ok=False)
    baseline_findings, report_items = report_self_check(report, output_dir)
    baseline = {finding["id"]: finding for finding in baseline_findings}

    with fitz.open(report.path) as document:
        independently_scanned_items, diagnostics = scan_report_items(document)
    if list(report_items) != list(independently_scanned_items):
        raise RuntimeError("Report item scan changed between self-check passes")

    photo_analysis = analyze_report_photo_rules(
        report.path,
        label_ocr_json,
        object_ocr_json,
    )
    photo_findings = photo_analysis["findings"]
    for finding in photo_findings:
        _render_photo_rule_evidence(report.path, finding, output_dir)

    numeric_finding = _r07b_finding(report.path, output_dir)
    r08 = _r08_finding(report_items)
    r09, r10 = _r09_r10_findings(diagnostics)

    by_id = {
        baseline["REPORT-R01"]["id"]: baseline["REPORT-R01"],
        **{finding["id"]: finding for finding in photo_findings},
        baseline["REPORT-R07"]["id"]: baseline["REPORT-R07"],
        numeric_finding["id"]: numeric_finding,
        r08["id"]: r08,
        r09["id"]: r09,
        r10["id"]: r10,
        baseline["REPORT-R11"]["id"]: baseline["REPORT-R11"],
    }
    findings = [by_id[rule_id] for rule_id in REPORT_RULE_ORDER]
    for finding in findings:
        for evidence in finding.get("evidence", []):
            evidence.setdefault("document_role", "report")
    coverage = _coverage(findings, REPORT_RULE_ORDER)

    artifacts_dir = output_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    copied_ocr = []
    for source in (label_ocr_json, object_ocr_json):
        destination = artifacts_dir / source.name
        shutil.copy2(source, destination)
        copied_ocr.append(
            {
                "path": str(destination.relative_to(output_dir)),
                "sha256": sha256_file(destination),
            }
        )

    result = {
        "schema_version": "full-attempt-0.1",
        "sample": "2795 / Report自检完整试跑",
        "run_id": "2795-report-self-full-attempt",
        "mode": "report_self",
        "created_at": _utc_now(),
        "lifecycle_status": "succeeded",
        "machine_overall_status": overall_status(findings),
        "overall_status": overall_status(findings),
        "engine": _engine(),
        "scope": {
            "included": ["Report R01–R11（含独立R07-B，共12个逻辑规则）"],
            "excluded": ["PTR", "GB 9706.1 Record", "GB 9706.202 Record"],
        },
        "coverage": coverage,
        "status_counts": _status_counts(findings),
        "files": [_relative_source(root, report)],
        "artifacts": {
            "ocr_derivatives": copied_ocr,
            "ocr_source_binding": ocr_verification,
            "photo_rule_diagnostics": photo_analysis["diagnostics"],
        },
        "findings": findings,
        "human_evaluation": None,
    }
    return _write_run(output_dir, result), report_items


def _build_record_202_run(
    root: Path,
    report: SourceFile,
    record: SourceFile,
    report_items: dict[int, dict[str, Any]],
    output_dir: Path,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=False)
    findings = record_202_check(record, report, output_dir, report_items)
    if any(finding["id"].startswith("REPORT-") for finding in findings):
        raise RuntimeError("9706.202 run unexpectedly contains Report self-check findings")
    for finding in findings:
        for evidence in finding.get("evidence", []):
            filename = Path(evidence["image"]).name
            evidence["document_role"] = (
                "report" if filename.startswith("report") else "record_9706_202"
            )
    number_finding = next(
        finding for finding in findings if finding["id"] == "RECORD202-NUMBER"
    )
    report_number_value = number_finding["details"].get("report_number")
    report_number = (
        tuple(report_number_value)
        if isinstance(report_number_value, (list, tuple)) and len(report_number_value) == 2
        else None
    )
    with fitz.open(record.path) as record_document:
        page_states = number_finding["details"]["page_states"]
        for page_index, state in enumerate(page_states):
            state["pdf_page"] = page_index + 1
            state["document_role"] = "record_9706_202"
            state["evidence_rects"] = [
                rect_list(rect)
                for rect in number_evidence_rects(
                    record_document[page_index],
                    report_number,
                )
            ]
    ordered = {finding["id"]: finding for finding in findings}
    findings = [ordered[rule_id] for rule_id in RECORD_202_RULE_ORDER]
    coverage = _coverage(findings, RECORD_202_RULE_ORDER)
    result = {
        "schema_version": "full-attempt-0.1",
        "sample": "2795 / Report+GB 9706.202 Record",
        "run_id": "2795-report-record-9706-202-attempt",
        "mode": "report_record_9706_202",
        "created_at": _utc_now(),
        "lifecycle_status": "succeeded",
        "machine_overall_status": overall_status(findings),
        "overall_status": overall_status(findings),
        "engine": _engine(),
        "scope": {
            "included": ["9706.202逐页报告编号", "9706.202图例、Ink符号与Report结果链"],
            "excluded": ["Report自检", "PTR", "GB 9706.1 Record", "9706.202表3"],
        },
        "coverage": coverage,
        "status_counts": _status_counts(findings),
        "files": [_relative_source(root, report), _relative_source(root, record)],
        "findings": findings,
        "human_evaluation": None,
    }
    return _write_run(output_dir, result)


def _render_attempt_index(manifest: Mapping[str, Any]) -> str:
    cards = []
    for run in manifest["runs"]:
        counts = run["status_counts"]
        cards.append(
            f"""
            <article>
              <div class="head"><div><span>{html.escape(run['mode'])}</span><h2>{html.escape(run['title'])}</h2></div><b class="{html.escape(run['machine_overall_status'])}">{html.escape(STATUS_LABEL[run['machine_overall_status']])}</b></div>
              <p>覆盖 {run['coverage']['completed']} / {run['coverage']['planned']}；通过 {counts['pass']}，警示 {counts['warning']}，待复核 {counts['manual']}，错误 {counts['error']}。</p>
              <p><a href="{html.escape(run['html'])}">打开HTML结果</a> · <a href="{html.escape(run['json'])}">打开JSON</a></p>
            </article>
            """
        )
    source_rows = "".join(
        f"<tr><td>{html.escape(item['role'])}</td><td>{html.escape(item['path'])}</td><td><code>{html.escape(item['sha256'])}</code></td><td>{'未变化' if item['unchanged'] else '已变化'}</td></tr>"
        for item in manifest["source_integrity"]
    )
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>2795完整内容试跑</title>
<style>
:root{{color-scheme:light;--ink:#101828;--muted:#667085;--line:#e4e7ec;--bg:#f6f7fb}}*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif}}main{{width:min(1180px,calc(100% - 32px));margin:32px auto 64px}}header{{padding:28px;border-radius:20px;background:linear-gradient(135deg,#101828,#1849a9);color:#fff}}header h1{{margin:6px 0 10px}}header p{{margin:0;color:#d0d5dd;line-height:1.7}}section{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px;margin-top:18px}}article,.sources{{background:#fff;border:1px solid var(--line);border-radius:16px;padding:20px}}.head{{display:flex;justify-content:space-between;gap:18px;align-items:flex-start}}.head span{{font-size:12px;color:var(--muted)}}h2{{margin:4px 0 0}}b{{padding:6px 10px;border-radius:999px;white-space:nowrap}}b.error{{background:#fee4e2;color:#b42318}}b.manual{{background:#f4ebff;color:#6938ef}}b.pass{{background:#dcfae6;color:#067647}}a{{color:#175cd3;font-weight:700;text-decoration:none}}.sources{{margin-top:18px;overflow:auto}}table{{width:100%;border-collapse:collapse}}th,td{{padding:10px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}}code{{font-size:11px;word-break:break-all}}@media(max-width:760px){{section{{grid-template-columns:1fr}}}}
</style></head><body><main><header><span>DETERMINISTIC FULL ATTEMPT</span><h1>2795 完整内容试跑</h1><p>Report自检与Report+GB 9706.202 Record为两次独立运行。自动解析和业务判定未调用LLM或Agent；不确定证据保持待人工复核。</p></header><section>{''.join(cards)}</section><div class="sources"><h2>原始文件完整性</h2><table><thead><tr><th>角色</th><th>路径</th><th>SHA-256</th><th>运行后</th></tr></thead><tbody>{source_rows}</tbody></table></div></main></body></html>"""


def run_full_2795_attempt(
    root: Path,
    output_dir: Path,
    *,
    label_ocr_json: Path | None = None,
    object_ocr_json: Path | None = None,
) -> dict[str, Any]:
    root = root.resolve()
    output_dir = output_dir.resolve()
    if output_dir.exists():
        raise FileExistsError(f"refusing to overwrite existing attempt directory: {output_dir}")

    config = SAMPLE_CONFIGS["2795"]
    report = SourceFile("report", root / config.report)
    if config.record202 is None:
        raise RuntimeError("sample 2795 has no GB 9706.202 Record")
    record = SourceFile("record_9706_202", root / config.record202)
    for source in (report, record):
        validate_source(source)

    label_ocr_json = label_ocr_json or root / "tmp/pdfs/2795/full-attempt/label-ocr.json"
    object_ocr_json = object_ocr_json or root / "tmp/pdfs/2795/full-attempt/object-ocr.json"
    label_ocr_json = label_ocr_json.resolve()
    object_ocr_json = object_ocr_json.resolve()
    ocr_verification = verify_ocr_derivatives(
        root,
        report.path,
        (label_ocr_json, object_ocr_json),
    )

    source_before = {
        source.role: _file_state(source.path)
        for source in (report, record)
    }
    output_dir.mkdir(parents=True, exist_ok=False)
    report_result, report_items = _build_report_run(
        root,
        report,
        output_dir / "report-self",
        label_ocr_json,
        object_ocr_json,
        ocr_verification,
    )
    record_result = _build_record_202_run(
        root,
        report,
        record,
        report_items,
        output_dir / "report-record-9706-202",
    )

    source_after = {
        source.role: _file_state(source.path)
        for source in (report, record)
    }
    integrity = []
    for source in (report, record):
        before = source_before[source.role]
        after = source_after[source.role]
        unchanged = before == after
        integrity.append(
            {
                "role": source.role,
                "path": str(source.path.relative_to(root)),
                "sha256": after["sha256"],
                "size": after["size"],
                "mtime_ns": after["mtime_ns"],
                "unchanged": unchanged,
            }
        )
        if not unchanged:
            raise RuntimeError(f"source file changed during run: {source.path}")

    manifest = {
        "schema_version": "full-attempt-manifest-0.1",
        "sample": "2795",
        "created_at": _utc_now(),
        "runtime_llm_used": False,
        "runtime_agent_used": False,
        "runs": [
            {
                "mode": report_result["mode"],
                "title": "Report自检（R01–R11，含R07-B）",
                "machine_overall_status": report_result["machine_overall_status"],
                "coverage": report_result["coverage"],
                "status_counts": report_result["status_counts"],
                "html": "report-self/index.html",
                "json": "report-self/result.json",
            },
            {
                "mode": record_result["mode"],
                "title": "Report + GB 9706.202 Record",
                "machine_overall_status": record_result["machine_overall_status"],
                "coverage": record_result["coverage"],
                "status_counts": record_result["status_counts"],
                "html": "report-record-9706-202/index.html",
                "json": "report-record-9706-202/result.json",
            },
        ],
        "source_integrity": integrity,
    }
    _json_write(output_dir / "manifest.json", manifest)
    (output_dir / "index.html").write_text(
        _render_attempt_index(manifest),
        encoding="utf-8",
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the isolated full 2795 attempt")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="new output directory; existing paths are never overwritten",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output_dir = args.output or root / "output/full-report-2795-attempt-20260929"
    manifest = run_full_2795_attempt(root, output_dir)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
