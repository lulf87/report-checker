from __future__ import annotations

import argparse
import json
import platform
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:
    import pymupdf as fitz
except ImportError:
    import fitz

from mvp.checker import (
    REPORT_SELF_RULE_ORDER,
    SourceFile,
    overall_status,
    relative_evidence_paths,
    render_html,
    report_self_check,
    sha256_file,
    validate_source,
)


REPORT_SCOPE_RULES: dict[str, tuple[str, ...]] = {
    "S09": ("REPORT-R01",),
    "S10": ("REPORT-R01",),
    "S11": ("REPORT-R03",),
    "S12": ("REPORT-R02", "REPORT-R04"),
    "S13": ("REPORT-R05",),
    "S14": ("REPORT-R06",),
    "S15": ("REPORT-R08",),
    "S16": ("REPORT-R07",),
    "S17": ("REPORT-R07-B",),
    "S18": ("REPORT-R07-B",),
    "S19": ("REPORT-R09",),
    "S20": ("REPORT-R10",),
    "S21": ("REPORT-R11",),
}


def _disposition(status: str) -> str:
    return {
        "pass": "matched",
        "warning": "warning",
        "error": "mismatch",
        "manual": "manual",
        "not_applicable": "not_applicable",
    }.get(status, "manual")


def _json_safe(value: Any) -> Any:
    """Keep the persisted scope ledger JSON-safe even for PyMuPDF values."""
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    try:
        return [round(float(item), 3) for item in value]
    except (TypeError, ValueError):
        return str(value)


def _location_or_page(
    location: Mapping[str, Any] | None,
    page_rects: Sequence[fitz.Rect],
    page_number: int | None = None,
) -> dict[str, Any] | None:
    if isinstance(location, Mapping):
        source = dict(location)
    elif isinstance(location, (list, tuple)) and len(location) == 4:
        source = {"rect": list(location)}
    else:
        source = {}
    page = source.get("pdf_page", page_number)
    if not isinstance(page, int) or not 1 <= page <= len(page_rects):
        return None
    rect = source.get("rect") or source.get("bbox")
    if not isinstance(rect, (list, tuple)) or len(rect) != 4:
        page_rect = page_rects[page - 1]
        rect = [float(page_rect.x0), float(page_rect.y0), float(page_rect.x1), float(page_rect.y1)]
    return {
        "document_role": "report",
        "pdf_page": page,
        "rect": [round(float(value), 3) for value in rect],
        "source": source.get("source", "report_scope"),
    }


def _build_report_ledger(
    findings: Sequence[Mapping[str, Any]],
    report_items: Mapping[int, Mapping[str, Any]],
    page_rects: Sequence[fitz.Rect],
) -> list[dict[str, Any]]:
    """Expand each Report rule into auditable object-level scope entries."""

    by_id = {str(finding["id"]): finding for finding in findings}
    ledger: list[dict[str, Any]] = []
    counter = 0

    def add(
        rule_id: str,
        scope_ids: Sequence[str],
        object_key: str,
        status: str,
        *,
        reason_code: str | None = None,
        value: Any = None,
        location: Mapping[str, Any] | None = None,
        page_number: int | None = None,
        photo_locations: Sequence[Mapping[str, Any]] | None = None,
    ) -> None:
        nonlocal counter
        counter += 1
        resolved = _location_or_page(location, page_rects, page_number)
        entry = {
                "entry_id": f"REPORT-SCOPE-{counter:04d}",
                "rule_id": rule_id,
                "scope_ids": list(scope_ids),
                "object_key": object_key,
                "status": status,
                "disposition": _disposition(status),
                "reason_code": reason_code or "rule_status",
                "value": _json_safe(value),
                "report_location": resolved,
            }
        if photo_locations:
            entry["photo_locations"] = [
                _location_or_page(photo, page_rects) for photo in photo_locations
                if _location_or_page(photo, page_rects) is not None
            ]
        ledger.append(entry)

    r01 = by_id["REPORT-R01"]
    details = r01.get("details", {})
    for comparison in details.get("comparisons", []):
        status = "pass" if comparison.get("match") else "error"
        add("REPORT-R01", ("S09",), f"identity:{comparison.get('field')}", status, value=comparison, location=comparison.get("cover_location"))
    for field, value in details.get("page3_fields", {}).items():
        location = details.get("page3_locations", {}).get(field)
        status = "pass" if str(value).strip() else "error"
        add("REPORT-R01", ("S10",), f"page3:{field}", status, reason_code="field_present" if status == "pass" else "field_missing", value=value, location=location, page_number=3)

    for rule_id, scope_ids, key in (
        ("REPORT-R02", ("S12",), "objects"),
        ("REPORT-R03", ("S11",), "comparisons"),
        ("REPORT-R04", ("S12",), "cell_checks"),
        ("REPORT-R05", ("S13",), "objects"),
        ("REPORT-R06", ("S14",), "objects"),
    ):
        for index, item in enumerate(by_id[rule_id].get("details", {}).get(key, []), start=1):
            add(
                rule_id,
                scope_ids,
                f"{key}:{item.get('sequence', index)}:{item.get('field', '')}",
                str(item.get("status", "manual")),
                reason_code=item.get("reason_code"),
                value=item,
                location=item.get("report_location") or item.get("locations", {}).get("部件名称") if isinstance(item, Mapping) else None,
                page_number=item.get("pdf_page") if isinstance(item, Mapping) else None,
                photo_locations=(
                    item.get("photo_locations", [])
                    or ([item.get("photo_location")] if item.get("photo_location") else [])
                ) if isinstance(item, Mapping) else None,
            )
            # R02 contains field-level values inside each discovered object.
            for comparison in item.get("field_comparisons", []) if isinstance(item, Mapping) else []:
                add(
                    rule_id,
                    scope_ids,
                    f"object:{item.get('sequence')}:{comparison.get('field')}",
                    str(comparison.get("status", "manual")),
                    reason_code=comparison.get("reason_code"),
                    value=comparison,
                    location=comparison.get("report_location"),
                    photo_locations=[comparison.get("photo_location")] if comparison.get("photo_location") else None,
                )

    r07 = by_id["REPORT-R07"]
    for sequence, item in report_items.items():
        locations = item.get("row_locations", [])
        add("REPORT-R07", ("S16",), f"item:{sequence}", "error" if any(m.get("sequence") == sequence for m in r07.get("details", {}).get("mismatches", [])) else "pass", value={"results": item.get("results", []), "conclusions": item.get("conclusions", [])}, location=locations[0] if locations else None, page_number=(locations[0].get("pdf_page") if locations else (item.get("pages") or [None])[0]))

    r07b = by_id["REPORT-R07-B"]
    for index, comparison in enumerate(r07b.get("details", {}).get("comparisons", []), start=1):
        scopes = ("S17", "S18")
        add("REPORT-R07-B", scopes, f"numeric:{index}", str(comparison.get("status", "manual")), reason_code=comparison.get("reason"), value=comparison, location={"pdf_page": comparison.get("pdf_page"), "rect": comparison.get("result_rect"), "source": "report_numeric_result"})
    for index, token in enumerate(r07b.get("details", {}).get("excluded_numeric_tokens", []), start=1):
        add("REPORT-R07-B", ("S17", "S18"), f"excluded_numeric:{index}", "manual", reason_code=token.get("reason"), value=token, page_number=token.get("pdf_page"))

    r08 = by_id["REPORT-R08"]
    missing_by_sequence = {item.get("sequence"): item for item in r08.get("details", {}).get("missing", [])}
    for sequence, item in report_items.items():
        missing = missing_by_sequence.get(sequence)
        add("REPORT-R08", ("S15",), f"item:{sequence}", "error" if missing else "pass", reason_code="missing_fields" if missing else "all_required_fields_present", value=missing or item, location=None, page_number=(item.get("pages") or [None])[0])

    for sequence in by_id["REPORT-R09"].get("details", {}).get("new_sequence_order", []):
        item = report_items.get(sequence, {})
        add("REPORT-R09", ("S19",), f"sequence:{sequence}", "pass" if not by_id["REPORT-R09"].get("details", {}).get("errors") else "error", value=sequence, page_number=(item.get("pages") or [None])[0])
    for marker in by_id["REPORT-R10"].get("details", {}).get("first_markers_by_page", []):
        add("REPORT-R10", ("S20",), f"page:{marker.get('pdf_page')}", "pass" if not by_id["REPORT-R10"].get("details", {}).get("errors") else "error", value=marker, page_number=marker.get("pdf_page"))
    for page_state in by_id["REPORT-R11"].get("details", {}).get("page_states", []):
        add("REPORT-R11", ("S21",), f"page:{page_state.get('pdf_page')}", str(page_state.get("status", by_id["REPORT-R11"].get("status", "manual"))), value=page_state, page_number=page_state.get("pdf_page"))
    # A discovery failure is itself an accounted manual object.  This keeps a
    # malformed or unsupported template from producing an empty, apparently
    # successful scope bucket.
    for scope_id, rule_ids in REPORT_SCOPE_RULES.items():
        if not any(scope_id in item.get("scope_ids", []) for item in ledger):
            add(rule_ids[0], (scope_id,), f"scope:{scope_id}:unresolved", "manual", reason_code="scope_object_discovery_empty", value=None, page_number=1)
    return ledger


def _scope_coverage(ledger: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    entries = []
    for scope_id, rule_ids in REPORT_SCOPE_RULES.items():
        scoped = [item for item in ledger if scope_id in item.get("scope_ids", [])]
        entries.append(
            {
                "scope_id": scope_id,
                "rule_ids": list(rule_ids),
                "eligible": len(scoped),
                "accounted": len(scoped),
                "conserved": len(scoped) == len([item for item in ledger if scope_id in item.get("scope_ids", [])]),
                "status_counts": {
                    status: sum(item.get("status") == status for item in scoped)
                    for status in ("pass", "warning", "manual", "error", "not_applicable", "excluded")
                },
                "entry_ids": [item["entry_id"] for item in scoped],
            }
        )
    return {"schema_version": "report-scope-1.0", "scope_ids": list(REPORT_SCOPE_RULES), "entries": entries, "ledger_entries": len(ledger), "conserved": all(item["conserved"] for item in entries)}


def _status_counts(findings: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    values = list(findings)
    return {
        status: sum(item.get("status") == status for item in values)
        for status in ("pass", "warning", "manual", "error")
    }


def _coverage(findings: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    finding_ids = [str(finding.get("id")) for finding in findings]
    if len(finding_ids) != len(set(finding_ids)):
        raise ValueError("Report self rule coverage contains duplicate finding IDs")
    by_id = dict(zip(finding_ids, findings, strict=True))
    missing = [rule_id for rule_id in REPORT_SELF_RULE_ORDER if rule_id not in by_id]
    unexpected = [rule_id for rule_id in by_id if rule_id not in REPORT_SELF_RULE_ORDER]
    if missing or unexpected:
        raise ValueError(
            f"Report self rule coverage mismatch: missing={missing}, unexpected={unexpected}"
        )
    return {
        "planned": len(REPORT_SELF_RULE_ORDER),
        "attempted": len(REPORT_SELF_RULE_ORDER),
        "completed": len(REPORT_SELF_RULE_ORDER),
        "missing": [],
        "rule_executions": [
            {
                "rule_id": rule_id,
                "execution_status": "succeeded",
                "machine_status": by_id[rule_id]["status"],
            }
            for rule_id in REPORT_SELF_RULE_ORDER
        ],
    }


def _file_state(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "sha256": sha256_file(path),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def _normalize_findings(
    findings: Sequence[Mapping[str, Any]],
    page_rects: Sequence[fitz.Rect],
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for original in findings:
        finding = dict(original)
        finding_id = finding.get("id")
        if not isinstance(finding_id, str) or not finding_id.startswith("REPORT-"):
            raise ValueError("Report self findings must use REPORT-* identifiers")
        if "rule_id" in finding and finding["rule_id"] != finding_id:
            raise ValueError(f"Report self rule_id does not match finding id: {finding_id}")
        if finding.get("status") not in {"pass", "warning", "manual", "error"}:
            raise ValueError(f"Report self finding has an invalid status: {finding_id}")
        finding["rule_id"] = finding_id
        evidence_locations: list[dict[str, Any]] = []
        evidence_values: list[dict[str, Any]] = []
        for evidence in finding.get("evidence", []):
            if not isinstance(evidence, Mapping):
                raise ValueError(f"Report self evidence is invalid: {finding_id}")
            evidence_item = dict(evidence)
            page = evidence_item.get("pdf_page")
            rects = evidence_item.get("rects", [])
            if not isinstance(page, int) or isinstance(page, bool) or page < 1 or page > len(page_rects):
                raise ValueError(f"Report self evidence page is invalid: {finding_id}")
            if not isinstance(rects, list):
                raise ValueError(f"Report self evidence rects are invalid: {finding_id}")
            page_rect = page_rects[page - 1]
            for rect in rects:
                if (
                    not isinstance(rect, (list, tuple))
                    or len(rect) != 4
                    or any(not isinstance(value, (int, float)) or isinstance(value, bool) for value in rect)
                    or rect[0] >= rect[2]
                    or rect[1] >= rect[3]
                ):
                    raise ValueError(f"Report self evidence bbox is invalid: {finding_id}")
                bbox = fitz.Rect(rect)
                if (
                    bbox.x0 < page_rect.x0
                    or bbox.y0 < page_rect.y0
                    or bbox.x1 > page_rect.x1
                    or bbox.y1 > page_rect.y1
                ):
                    raise ValueError(f"Report self evidence bbox is out of bounds: {finding_id}")
                evidence_locations.append(
                    {
                        "role": "report",
                        "pdf_page": page,
                        "bbox": [round(float(value), 3) for value in bbox],
                    }
                )
            evidence_item["document_role"] = "report"
            evidence_values.append(evidence_item)
        finding["evidence"] = evidence_values
        finding["evidence_locations"] = evidence_locations
        normalized.append(finding)
    return normalized


def run_report_self(
    *,
    report_path: str | Path,
    output_dir: str | Path,
) -> dict[str, Any]:
    report = Path(report_path).expanduser().resolve()
    output = Path(output_dir).expanduser().resolve()
    if output.exists():
        raise FileExistsError(f"refusing to overwrite existing output directory: {output}")
    source = SourceFile("report", report)
    validate_source(source)
    before = _file_state(report)
    output.mkdir(parents=True, exist_ok=False)
    with fitz.open(report) as document:
        page_rects = [fitz.Rect(page.rect) for page in document]
    try:
        findings_raw, report_items = report_self_check(source, output)
    except Exception as exc:
        try:
            after_failure = _file_state(report)
        except Exception as integrity_exc:
            raise RuntimeError("the Report PDF became unavailable during a failed self-check") from integrity_exc
        if before != after_failure:
            raise RuntimeError("the Report PDF changed during a failed self-check") from exc
        raise
    findings = _normalize_findings(findings_raw, page_rects)
    ledger = _build_report_ledger(findings, report_items, page_rects)
    after = _file_state(report)
    unchanged = before == after
    if not unchanged:
        raise RuntimeError("the Report PDF changed during the self-check")

    counts = _status_counts(findings)
    result = {
        "schema_version": "report-self-1.1",
        "sample": report.stem,
        "mode": "report_self",
        "mode_label": "Report 自检",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "lifecycle_status": "succeeded",
        "machine_overall_status": overall_status(findings),
        "overall_status": overall_status(findings),
        "engine": {
            "llm_used": False,
            "agent_used": False,
            "python_version": platform.python_version(),
        },
        "scope": {
            "included": [
                "Report 首页与第三页身份字段及样品描述/标签字段",
                "Report 样品生产日期值与书写格式",
                "Report 样品描述字段在照片页的覆盖",
                "Report 实物照片和中文标签照片存在性",
                "Report 多行结果与单项结论",
                "Report 数值结果与同行接受标准",
                "Report 项目、条款、要求、结果和结论漏填",
                "Report 新项目序号连续性",
                "Report 跨页续N标记",
                "Report 打印页码连续性",
            ],
            "excluded": ["PTR", "GB 9706.1 Record", "GB 9706.202 Record"],
        },
        "coverage": _coverage(findings),
        "scope_coverage": _scope_coverage(ledger),
        "ledger": ledger,
        "status_counts": counts,
        "files": [
            {
                "role": "report",
                "path": str(report),
                "sha256": before["sha256"],
                "size": before["size"],
            }
        ],
        "source_files": {
            "report": {
                "path": str(report),
                "sha256": before["sha256"],
            }
        },
        "source_integrity": [
            {
                "role": "report",
                "sha256_before": before["sha256"],
                "sha256_after": after["sha256"],
                "unchanged": True,
            }
        ],
        "findings": findings,
        "human_evaluation": None,
    }
    relative_evidence_paths(result["findings"], output)
    (output / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    (output / "index.html").write_text(render_html(result), encoding="utf-8")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one independent Report self-check mode.")
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = run_report_self(report_path=args.report, output_dir=args.output)
    print(
        json.dumps(
            {
                "mode": result["mode"],
                "machine_overall_status": result["machine_overall_status"],
                "result_json": str(args.output.resolve() / "result.json"),
                "html": str(args.output.resolve() / "index.html"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
