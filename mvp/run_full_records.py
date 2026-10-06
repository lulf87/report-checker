from __future__ import annotations

import argparse
import hashlib
import html
import importlib
import json
import platform
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

try:
    import pymupdf as fitz
except ImportError:
    import fitz

from mvp.capabilities import MODE_CATALOG
from mvp.input_variants import Record61StatusInventoryError


MODE_CONFIG = {
    "report_record_9706_1": {
        "module": "mvp.full_record_61",
        "callable": "run_record_61_full",
        "record_role": "record_9706_1",
        "id_prefix": "RECORD61-",
        "label": "Report + GB 9706.1 Record",
    },
    "report_record_9706_202": {
        "module": "mvp.full_record_202",
        "callable": "run_record_202_full",
        "record_role": "record_9706_202",
        "id_prefix": "RECORD202-",
        "label": "Report + GB 9706.202 Record",
    },
}

STATUS_LABELS = {
    "pass": "通过",
    "warning": "警示",
    "manual": "待人工复核",
    "error": "不通过",
}

DISPOSITION_LABELS = {
    "matched": "一致",
    "mismatch": "不一致",
    "manual": "待人工复核",
    "not_applicable": "无可检内容",
    "excluded": "已排除",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_pdf(path: Path) -> list[fitz.Rect]:
    if not path.is_file():
        raise FileNotFoundError(path)
    with fitz.open(path) as document:
        if document.needs_pass:
            raise ValueError(f"encrypted PDF is not supported: {path}")
        return [fitz.Rect(page.rect) for page in document]


def _load_scanner(mode: str) -> Callable[..., Mapping[str, Any]]:
    config = MODE_CONFIG[mode]
    module = importlib.import_module(config["module"])
    scanner = getattr(module, config["callable"], None)
    if not callable(scanner):
        raise RuntimeError(
            f"scanner callable is unavailable: {config['module']}.{config['callable']}"
        )
    return scanner


def _rect_is_valid(value: Any) -> bool:
    return (
        isinstance(value, (list, tuple))
        and len(value) == 4
        and all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in value)
        and value[0] < value[2]
        and value[1] < value[3]
    )


def _canonical_location(
    value: Any,
    page_rects: Sequence[fitz.Rect] | None = None,
) -> dict[str, Any] | None:
    if isinstance(value, list):
        for item in value:
            location = _canonical_location(item, page_rects)
            if location is not None:
                return location
        return None
    if not isinstance(value, Mapping):
        return None
    page = value.get("pdf_page", value.get("page"))
    bbox = value.get("bbox", value.get("rect"))
    if bbox is None:
        rects = value.get("rects")
        if isinstance(rects, list) and rects:
            bbox = rects[0]
    if (
        not isinstance(page, int)
        or isinstance(page, bool)
        or page < 1
        or not _rect_is_valid(bbox)
    ):
        return None
    if page_rects is not None:
        if page > len(page_rects):
            return None
        location_rect = fitz.Rect(bbox)
        page_rect = page_rects[page - 1]
        tolerance = 0.01
        if (
            location_rect.x0 < page_rect.x0 - tolerance
            or location_rect.y0 < page_rect.y0 - tolerance
            or location_rect.x1 > page_rect.x1 + tolerance
            or location_rect.y1 > page_rect.y1 + tolerance
        ):
            return None
    return {
        "pdf_page": page,
        "bbox": [round(float(item), 3) for item in bbox],
    }


def _validate_location_value(
    value: Any,
    page_rects: Sequence[fitz.Rect],
    *,
    label: str,
) -> None:
    """Validate every advertised page/bbox, not just the first fallback."""

    if isinstance(value, list):
        if not value:
            raise ValueError(f"{label} is empty")
        for index, item in enumerate(value):
            _validate_location_value(item, page_rects, label=f"{label}[{index}]")
        return
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} is not a location mapping")
    page = value.get("pdf_page", value.get("page"))
    bbox = value.get("bbox", value.get("rect"))
    rects = value.get("rects")
    if bbox is not None and rects is not None:
        raise ValueError(f"{label} supplies both bbox and rects")
    if rects is not None:
        if not isinstance(rects, list) or not rects:
            raise ValueError(f"{label}.rects is invalid")
        for index, rect in enumerate(rects):
            candidate = {"pdf_page": page, "bbox": rect}
            if _canonical_location(candidate, page_rects) is None:
                raise ValueError(f"{label}.rects[{index}] is out of bounds")
        return
    if _canonical_location(value, page_rects) is None:
        raise ValueError(f"{label} is out of bounds")


def _ledger_location(
    entry: Mapping[str, Any],
    role: str,
    page_rects: Sequence[fitz.Rect] | None = None,
) -> dict[str, Any] | None:
    if role == "record":
        keys = ("record_location", "source_location", "record_evidence")
        nested_keys = ("record", "source")
    else:
        keys = ("report_location", "target_location", "report_evidence")
        nested_keys = ("report", "target")
    for key in keys:
        location = _canonical_location(entry.get(key), page_rects)
        if location is not None:
            return location
    for key in nested_keys:
        nested = entry.get(key)
        if isinstance(nested, Mapping):
            location = _canonical_location(nested.get("location", nested), page_rects)
            if location is not None:
                return location
    return None


_DISPOSITION_RANK = {
    "excluded": 0,
    "not_applicable": 1,
    "matched": 2,
    "manual": 3,
    "mismatch": 4,
}


def _ledger_row_ids(entry: Mapping[str, Any], side: str) -> tuple[str, ...]:
    singular_key = "source_row_id" if side == "source" else "target_row_id"
    plural_key = "source_row_ids" if side == "source" else "target_row_ids"
    plural = entry.get(plural_key)
    singular = entry.get(singular_key)
    if plural is None:
        values = () if singular is None else (singular,)
    else:
        if not isinstance(plural, list):
            raise ValueError(f"ledger {plural_key} must be a list")
        values = tuple(plural)
        if singular is not None and singular not in values:
            raise ValueError(f"ledger {singular_key} is absent from {plural_key}")
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError(f"ledger {side} row ids must be non-empty strings")
    if len(values) != len(set(values)):
        raise ValueError(f"ledger {side} row ids contain duplicates")
    return values


def _validate_coverage(coverage: Any, ledger: Sequence[Mapping[str, Any]]) -> None:
    if not isinstance(coverage, Mapping):
        raise ValueError("scanner result is missing row-level coverage")
    sides = {
        "source_rows": "source",
        "report_rows": "target",
    }
    seen_edges: set[tuple[str | None, str | None]] = set()
    observed: dict[str, dict[str, list[str]]] = {
        key: {} for key in sides
    }
    for entry in ledger:
        source_ids = _ledger_row_ids(entry, "source")
        target_ids = _ledger_row_ids(entry, "target")
        if not source_ids and not target_ids:
            raise ValueError("ledger entry has neither source nor target row id")
        edge_sources: tuple[str | None, ...] = source_ids or (None,)
        edge_targets: tuple[str | None, ...] = target_ids or (None,)
        for source_id in edge_sources:
            for target_id in edge_targets:
                edge = (source_id, target_id)
                if edge in seen_edges:
                    raise ValueError(f"duplicate coverage edge: {edge}")
                seen_edges.add(edge)
        disposition = str(entry["disposition"])
        for key, ids in (("source_rows", source_ids), ("report_rows", target_ids)):
            for row_id in ids:
                observed[key].setdefault(row_id, []).append(disposition)

    for summary_key, _ in sides.items():
        summary = coverage.get(summary_key)
        if not isinstance(summary, Mapping):
            raise ValueError(f"coverage.{summary_key} is missing")
        row_ids = summary.get("row_ids")
        if not isinstance(row_ids, list):
            raise ValueError(f"coverage.{summary_key}.row_ids is missing")
        if any(not isinstance(row_id, str) or not row_id.strip() for row_id in row_ids):
            raise ValueError(f"coverage.{summary_key}.row_ids is invalid")
        if len(row_ids) != len(set(row_ids)):
            raise ValueError(f"coverage.{summary_key}.row_ids is not unique")
        eligible = summary.get("eligible")
        accounted = summary.get("accounted")
        if eligible != len(row_ids):
            raise ValueError(f"coverage.{summary_key}.eligible does not match row_ids")
        observed_ids = set(observed[summary_key])
        if observed_ids != set(row_ids):
            missing = sorted(set(row_ids) - observed_ids)
            unknown = sorted(observed_ids - set(row_ids))
            raise ValueError(
                f"coverage.{summary_key} ledger mismatch: missing={missing}, unknown={unknown}"
            )
        if accounted != len(observed_ids) or accounted != eligible:
            raise ValueError(f"coverage.{summary_key} is not conserved")
        if summary.get("conserved") is not True:
            raise ValueError(f"coverage.{summary_key}.conserved is not true")
        reported_dispositions = summary.get("dispositions")
        if not isinstance(reported_dispositions, Mapping):
            raise ValueError(f"coverage.{summary_key}.dispositions is missing")
        calculated = {name: 0 for name in _DISPOSITION_RANK}
        for values in observed[summary_key].values():
            final = max(values, key=_DISPOSITION_RANK.__getitem__)
            calculated[final] += 1
        if dict(reported_dispositions) != calculated:
            raise ValueError(
                f"coverage.{summary_key}.dispositions does not match ledger"
            )
    if (
        coverage["source_rows"]["eligible"] == 0
        and coverage["report_rows"]["eligible"] == 0
    ):
        raise ValueError("empty row coverage cannot be published as a completed run")


def _validate_payload(
    mode: str,
    payload: Any,
    *,
    report_page_rects: Sequence[fitz.Rect],
    record_page_rects: Sequence[fitz.Rect],
) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise TypeError("record scanner must return a mapping")
    result = dict(payload)
    findings = result.get("findings", [])
    ledger = result.get("ledger")
    if not isinstance(findings, list):
        raise ValueError("scanner findings must be a list")
    if not isinstance(ledger, list):
        raise ValueError("scanner ledger must be a list")
    prefix = MODE_CONFIG[mode]["id_prefix"]
    record_role = MODE_CONFIG[mode]["record_role"]
    finding_ids: set[str] = set()
    for finding in findings:
        if not isinstance(finding, Mapping):
            raise ValueError("finding must be a mapping")
        finding_id = str(finding.get("id") or "")
        rule_id = str(finding.get("rule_id") or finding_id)
        if not finding_id.startswith(prefix) or not rule_id.startswith(prefix):
            raise ValueError(f"finding is outside the selected mode: {finding_id or rule_id}")
        if finding_id in finding_ids:
            raise ValueError(f"duplicate finding id: {finding_id}")
        finding_ids.add(finding_id)
        if finding.get("status") not in STATUS_LABELS:
            raise ValueError(f"invalid finding status: {finding.get('status')!r}")
        evidence = finding.get("evidence_locations")
        evidence_roles: set[str] = set()
        if evidence is not None:
            if not isinstance(evidence, list):
                raise ValueError(f"{finding_id}.evidence_locations must be a list")
            for location in evidence:
                if not isinstance(location, Mapping):
                    raise ValueError(f"{finding_id} has invalid evidence location")
                role = location.get("role")
                if role == "report":
                    page_rects = report_page_rects
                elif role == record_role:
                    page_rects = record_page_rects
                else:
                    raise ValueError(f"{finding_id} has evidence for an invalid role: {role}")
                _validate_location_value(
                    location,
                    page_rects,
                    label=f"{finding_id}.evidence_locations",
                )
                evidence_roles.add(str(role))
        if finding.get("status") in {"error", "manual", "warning"}:
            if evidence is None:
                raise ValueError(f"{finding_id} lacks evidence_locations")
            if evidence_roles != {"report", record_role}:
                raise ValueError(f"{finding_id} lacks valid two-sided evidence locations")

    ledger_ids: set[str] = set()
    for index, entry in enumerate(ledger, start=1):
        if not isinstance(entry, Mapping):
            raise ValueError("ledger entry must be a mapping")
        entry_id = str(entry.get("entry_id") or entry.get("id") or f"ledger-{index}")
        if entry_id in ledger_ids:
            raise ValueError(f"duplicate ledger entry id: {entry_id}")
        ledger_ids.add(entry_id)
        rule_id = entry.get("rule_id")
        if not isinstance(rule_id, str) or not rule_id.startswith(prefix):
            raise ValueError(f"ledger entry is outside the selected mode: {rule_id}")
        disposition = entry.get("disposition")
        if disposition not in DISPOSITION_LABELS:
            raise ValueError(f"invalid ledger disposition: {disposition!r}")
        _ledger_row_ids(entry, "source")
        _ledger_row_ids(entry, "target")
        for key in ("record_location", "source_location", "record_evidence"):
            if key in entry and entry[key] is not None:
                _validate_location_value(
                    entry[key],
                    record_page_rects,
                    label=f"{entry_id}.{key}",
                )
        for key in ("report_location", "target_location", "report_evidence"):
            if key in entry and entry[key] is not None:
                _validate_location_value(
                    entry[key],
                    report_page_rects,
                    label=f"{entry_id}.{key}",
                )
        if disposition in {"mismatch", "manual"}:
            missing_roles = []
            if _ledger_location(entry, "record", record_page_rects) is None:
                missing_roles.append("record")
            if _ledger_location(entry, "report", report_page_rects) is None:
                missing_roles.append("report")
            if missing_roles:
                raise ValueError(
                    f"{entry_id} lacks page+bbox evidence for: {', '.join(missing_roles)}"
                )
    _validate_coverage(result.get("coverage"), ledger)
    return result


def _finding_status_counts(findings: list[Mapping[str, Any]]) -> dict[str, int]:
    return {
        status: sum(finding.get("status") == status for finding in findings)
        for status in STATUS_LABELS
    }


def _ledger_status_counts(ledger: list[Mapping[str, Any]]) -> dict[str, int]:
    return {
        "pass": sum(entry.get("disposition") == "matched" for entry in ledger),
        "warning": 0,
        "manual": sum(entry.get("disposition") == "manual" for entry in ledger),
        "error": sum(entry.get("disposition") == "mismatch" for entry in ledger),
        "not_applicable": sum(
            entry.get("disposition") == "not_applicable" for entry in ledger
        ),
        "excluded": sum(entry.get("disposition") == "excluded" for entry in ledger),
    }


def _overall_status(
    finding_counts: Mapping[str, int],
    ledger_counts: Mapping[str, int],
) -> str:
    if finding_counts["error"] or ledger_counts["error"]:
        return "error"
    if finding_counts["manual"] or ledger_counts["manual"]:
        return "manual"
    if finding_counts["warning"]:
        return "warning"
    return "pass"


def _inventory_evidence_location(page_rects: Sequence[fitz.Rect], page: int) -> dict[str, Any]:
    """Return a small, valid page anchor for an input-variant finding."""

    if not page_rects:
        raise ValueError("PDF has no pages for inventory evidence")
    page = max(1, min(page, len(page_rects)))
    rect = page_rects[page - 1]
    # Keep the anchor away from a page edge while remaining valid for small
    # synthetic fixtures used by the contract tests.
    x0 = min(max(1.0, float(rect.x0) + 1.0), max(float(rect.x0), float(rect.x1) - 2.0))
    y0 = min(max(1.0, float(rect.y0) + 1.0), max(float(rect.y0), float(rect.y1) - 2.0))
    x1 = min(float(rect.x1) - 1.0, x0 + 1.0)
    y1 = min(float(rect.y1) - 1.0, y0 + 1.0)
    if x1 <= x0 or y1 <= y0:
        x0, y0 = float(rect.x0), float(rect.y0)
        x1, y1 = float(rect.x1), float(rect.y1)
    return {"pdf_page": page, "bbox": [round(x0, 3), round(y0, 3), round(x1, 3), round(y1, 3)]}


def _unsupported_record61_inventory_result(
    error: Record61StatusInventoryError,
    *,
    report: Path,
    record: Path,
    report_page_rects: Sequence[fitz.Rect],
    record_page_rects: Sequence[fitz.Rect],
) -> dict[str, Any]:
    """Build an auditable result for a known but unsupported 9706.1 variant.

    The structure rule emits one manual Finding with both source locations.
    All downstream rules are explicitly marked unsupported, so the API keeps
    the difference between an input variant and a Worker crash.
    """

    report_location = _inventory_evidence_location(report_page_rects, 1)
    record_location = _inventory_evidence_location(record_page_rects, 6)
    details = {
        "reason_code": error.code,
        "inventory": error.inventory,
        "report_filename": report.name,
        "record_filename": record.name,
        "next_action": "确认Record模板版本、页面完整性或补充适配后重新运行",
    }
    rule_ids = MODE_CATALOG["report_record_9706_1"]["rule_ids"]
    structure_rule = "RECORD61-STRUCTURE"
    finding = {
        "id": "RECORD61-STRUCTURE:status-inventory",
        "rule_id": structure_rule,
        "status": "manual",
        "title": "9706.1 Record 状态库存不在已验证模板范围",
        "summary": (
            f"检测到 {error.inventory['observed']['total']} 条状态行 "
            f"（原生 {error.inventory['observed']['native']}、异体 {error.inventory['observed']['alternate']}），"
            f"当前模板要求 {error.inventory['expected']['total']} 条；需确认模板版本或页面完整性。"
        ),
        "details": details,
        "reason_code": error.code,
        "evidence_locations": [
            {**report_location, "role": "report", "semantic_role": "comparison_context"},
            {**record_location, "role": "record_9706_1", "semantic_role": "status_inventory"},
        ],
    }
    source_row_id = "record61:status_inventory"
    report_row_id = "report:status_inventory"
    ledger_entry = {
        "entry_id": source_row_id,
        "rule_id": structure_rule,
        "source_row_id": source_row_id,
        "target_row_id": report_row_id,
        "disposition": "manual",
        "reason_code": error.code,
        "reason_detail": details,
        "record_location": record_location,
        "report_location": report_location,
    }
    coverage = {
        "level": "input_validation",
        "comparison_complete": False,
        "source_rows": {
            "eligible": 1,
            "accounted": 1,
            "conserved": True,
            "row_ids": [source_row_id],
            "dispositions": {"matched": 0, "manual": 1, "mismatch": 0, "not_applicable": 0, "excluded": 0},
        },
        "report_rows": {
            "eligible": 1,
            "accounted": 1,
            "conserved": True,
            "row_ids": [report_row_id],
            "dispositions": {"matched": 0, "manual": 1, "mismatch": 0, "not_applicable": 0, "excluded": 0},
        },
        "input_variant": details,
    }
    variant_evidence = [
        {**report_location, "role": "report", "semantic_role": "comparison_context"},
        {**record_location, "role": "record_9706_1", "semantic_role": "status_inventory"},
    ]
    execution_states = {
        rule_id: (
            {
                "state": "succeeded",
                "disposition": "manual",
                "reason_code": error.code,
                "reason_detail": details,
                "evidence_locations": variant_evidence,
            }
            if rule_id == structure_rule
            else {
                "state": "unsupported",
                "disposition": "unsupported",
                "reason_code": error.code,
                "reason_detail": details,
                "evidence_locations": variant_evidence,
            }
        )
        for rule_id in rule_ids
    }
    return {
        "schema_version": "record-full-input-variant-1.0",
        "mode": "report_record_9706_1",
        "overall_status": "manual",
        "machine_overall_status": "manual",
        "comparison_complete": False,
        "input_variant": details,
        "rule_execution_states": execution_states,
        "findings": [finding],
        "ledger": [ledger_entry],
        "coverage": coverage,
    }


def _position_text(location: dict[str, Any] | None, role_label: str) -> str:
    if location is None:
        return "—"
    bbox = json.dumps(location["bbox"], ensure_ascii=False, separators=(",", ":"))
    return f"{role_label}第{location['pdf_page']}页 / bbox {bbox}"


def render_run_html(result: Mapping[str, Any]) -> str:
    counts = result["status_counts"]
    coverage = result["coverage"]
    findings = result.get("findings", [])
    ledger = result.get("ledger", [])

    metric_html = "".join(
        f'<div class="metric {status}"><strong>{counts[status]}</strong><span>{STATUS_LABELS[status]}</span></div>'
        for status in ("error", "manual", "warning", "pass")
    )
    coverage_html = "".join(
        f"<tr><td>{label}</td><td>{coverage[key]['eligible']}</td>"
        f"<td>{coverage[key]['accounted']}</td><td>已守恒</td></tr>"
        for key, label in (("source_rows", "Record行"), ("report_rows", "Report行"))
    )

    finding_html = "".join(
        "<article class=\"finding {status}\"><div><code>{identifier}</code>"
        "<span class=\"badge\">{label}</span></div><h3>{title}</h3><p>{summary}</p></article>".format(
            status=html.escape(str(item.get("status"))),
            identifier=html.escape(str(item.get("id", ""))),
            label=html.escape(STATUS_LABELS.get(str(item.get("status")), str(item.get("status")))),
            title=html.escape(str(item.get("title") or item.get("rule_id") or "比对结果")),
            summary=html.escape(str(item.get("summary") or item.get("reason_code") or "")),
        )
        for item in findings
    ) or "<p class=\"empty\">本次运行未生成独立问题Finding；请以下方逐行ledger为准。</p>"

    ledger_rows: list[str] = []
    for index, entry in enumerate(ledger, start=1):
        disposition = str(entry.get("disposition"))
        record_location = _ledger_location(entry, "record")
        report_location = _ledger_location(entry, "report")
        identifier = entry.get("entry_id") or entry.get("id") or f"L{index}"
        ledger_rows.append(
            f'<tr class="{html.escape(disposition)}">'
            f"<td>{html.escape(str(identifier))}</td>"
            f"<td>{html.escape(str(entry.get('rule_id') or ''))}</td>"
            f"<td>{html.escape(DISPOSITION_LABELS[disposition])}</td>"
            f"<td>{html.escape(str(entry.get('reason_code') or ''))}</td>"
            f"<td>{html.escape(_position_text(report_location, 'Report'))}</td>"
            f"<td>{html.escape(_position_text(record_location, 'Record'))}</td>"
            "</tr>"
        )

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{html.escape(str(result['mode_label']))}</title>
  <style>
    :root{{--ink:#17202a;--muted:#667085;--line:#dfe3e8;--bg:#f5f7fa;--error:#b42318;--manual:#b54708;--pass:#027a48}}
    *{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif}}
    main{{width:min(1440px,calc(100% - 40px));margin:30px auto 60px}} header{{background:#132238;color:white;border-radius:18px;padding:26px}}
    h1{{margin:4px 0 8px;font-size:28px}} header p{{margin:0;color:#d0d5dd}} .overall{{display:inline-block;margin-top:14px;padding:7px 11px;border:1px solid #ffffff40;border-radius:999px}}
    .metrics{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:16px 0}} .metric{{background:white;border:1px solid var(--line);border-radius:13px;padding:16px}}
    .metric strong{{display:block;font-size:25px}} .metric span,.empty{{color:var(--muted)}} section{{background:white;border:1px solid var(--line);border-radius:14px;padding:18px;margin-top:16px}}
    table{{width:100%;border-collapse:collapse;font-size:13px}} th,td{{border-bottom:1px solid var(--line);padding:10px;text-align:left;vertical-align:top}} th{{background:#f9fafb;position:sticky;top:0}}
    tr.mismatch td{{background:#fff1f0}} tr.manual td{{background:#fff8eb}} code{{overflow-wrap:anywhere}} .finding{{border-left:4px solid #98a2b3;padding:12px 14px;margin:10px 0;background:#fafafa}}
    .finding.error{{border-color:var(--error)}} .finding.manual{{border-color:var(--manual)}} .finding h3{{margin:8px 0 3px}} .finding p{{margin:0;color:var(--muted)}} .badge{{float:right;font-weight:700}}
    @media(max-width:800px){{.metrics{{grid-template-columns:repeat(2,1fr)}} section{{overflow:auto}}}}
  </style>
</head>
<body><main>
  <header><small>INDEPENDENT RECORD COMPARISON</small><h1>{html.escape(str(result['mode_label']))}</h1>
    <p>本结果仅包含当前Record比对模式，不聚合Report自检。</p>
    <span class="overall">总体：{html.escape(STATUS_LABELS[str(result['machine_overall_status'])])}</span></header>
  <div class="metrics">{metric_html}</div>
  <section><h2>Coverage守恒</h2><table><thead><tr><th>对象</th><th>应覆盖</th><th>已处置</th><th>状态</th></tr></thead><tbody>{coverage_html}</tbody></table></section>
  <section><h2>问题与待复核</h2>{finding_html}</section>
  <section><h2>逐行Coverage Ledger</h2><table><thead><tr><th>ID</th><th>规则</th><th>处置</th><th>原因</th><th>Report位置</th><th>Record位置</th></tr></thead><tbody>{''.join(ledger_rows)}</tbody></table></section>
</main></body></html>"""


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def run_full_records(
    *,
    mode: str,
    report_path: str | Path,
    record_path: str | Path,
    output_dir: str | Path,
) -> dict[str, Any]:
    if mode not in MODE_CONFIG:
        raise ValueError(f"unsupported mode: {mode}")
    report = Path(report_path).expanduser().resolve()
    record = Path(record_path).expanduser().resolve()
    output = Path(output_dir).expanduser().resolve()
    report_page_rects = _validate_pdf(report)
    record_page_rects = _validate_pdf(record)

    before = {"report": _sha256(report), "record": _sha256(record)}
    try:
        scanner = _load_scanner(mode)
        try:
            scanner_payload = scanner(
                report_path=report,
                record_path=record,
                output_dir=output,
            )
        except Record61StatusInventoryError as exc:
            if mode != "report_record_9706_1":
                raise
            scanner_payload = _unsupported_record61_inventory_result(
                exc,
                report=report,
                record=record,
                report_page_rects=report_page_rects,
                record_page_rects=record_page_rects,
            )
        payload = _validate_payload(
            mode,
            scanner_payload,
            report_page_rects=report_page_rects,
            record_page_rects=record_page_rects,
        )
    except Exception as exc:
        try:
            after_failure = {"report": _sha256(report), "record": _sha256(record)}
        except Exception as integrity_exc:
            raise RuntimeError(
                "a source PDF became unavailable during a failed comparison run"
            ) from integrity_exc
        if before != after_failure:
            raise RuntimeError("a source PDF changed during a failed comparison run") from exc
        raise
    after = {"report": _sha256(report), "record": _sha256(record)}
    if before != after:
        raise RuntimeError("a source PDF changed during the comparison run")

    findings = payload.get("findings", [])
    ledger = payload["ledger"]
    counts = _finding_status_counts(findings)
    ledger_counts = _ledger_status_counts(ledger)
    result = dict(payload)
    result.update(
        {
            "schema_version": "record-full-1.0",
            "mode": mode,
            "mode_label": MODE_CONFIG[mode]["label"],
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "engine": {
                "llm_used": False,
                "agent_used": False,
                "python_version": platform.python_version(),
                "pymupdf_version": fitz.VersionBind,
            },
            "files": [
                {"role": "report", "path": str(report), "sha256": before["report"]},
                {
                    "role": MODE_CONFIG[mode]["record_role"],
                    "path": str(record),
                    "sha256": before["record"],
                },
            ],
            "status_counts": counts,
            "ledger_status_counts": ledger_counts,
            "machine_overall_status": _overall_status(counts, ledger_counts),
            "source_integrity": [
                {"role": "report", "sha256_before": before["report"], "sha256_after": after["report"], "unchanged": True},
                {"role": MODE_CONFIG[mode]["record_role"], "sha256_before": before["record"], "sha256_after": after["record"], "unchanged": True},
            ],
        }
    )

    _write_atomic(
        output / "result.json",
        json.dumps(result, ensure_ascii=False, indent=2, default=str),
    )
    _write_atomic(output / "index.html", render_run_html(result))
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one independent Report/Record full comparison mode."
    )
    parser.add_argument("--mode", required=True, choices=tuple(MODE_CONFIG))
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--record", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = run_full_records(
        mode=args.mode,
        report_path=args.report,
        record_path=args.record,
        output_dir=args.output,
    )
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
