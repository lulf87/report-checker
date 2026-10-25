"""Review-only PTR ↔ Report Draft Runner.

This runner materializes the offline comparison into the same broad shape as
the project's other result artifacts, while keeping a separate mode name and
``publishable=false``. It deliberately does not register with ``run_modes`` or
the Worker, so a draft cannot create a formal ``report_ptr`` Run accidentally.
"""

from __future__ import annotations

import argparse
import html
import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from mvp.ptr_compare import EXCLUDED, MANUAL, MATCHED, MISMATCH, compare_ptr_report


DRAFT_RUN_SCHEMA_VERSION = "ptr-draft-run-0.1"
DRAFT_MODE = "report_ptr_draft"
RULE_ID = "PTR-P01"


class PtrDraftRunnerError(ValueError):
    """Raised when a draft run cannot preserve its read-only contract."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _status(decision: str) -> str:
    return {MATCHED: "pass", MISMATCH: "error", MANUAL: "manual", EXCLUDED: "not_applicable"}.get(decision, "manual")


def _overall(summary: Mapping[str, Any]) -> str:
    if int(summary.get("counts", {}).get(MISMATCH, 0)):
        return "error"
    if int(summary.get("counts", {}).get(MANUAL, 0)):
        return "manual"
    if int(summary.get("counts", {}).get(MATCHED, 0)):
        return "pass"
    return "manual"


def _finding(entry: Mapping[str, Any], *, rule_version: str) -> dict[str, Any]:
    decision = str(entry.get("decision") or MANUAL)
    evidence = []
    for item in entry.get("evidence", []):
        if not isinstance(item, Mapping):
            continue
        evidence.append(
            {
                "document_role": item.get("role"),
                "pdf_page": item.get("pdf_page"),
                "bbox": item.get("bbox"),
                "source_sha256": item.get("source_sha256"),
                "coordinate_space": item.get("coordinate_space", "pdf_points"),
                "semantic_role": item.get("semantic_role"),
                "extraction_method": item.get("extraction_method"),
                "text": item.get("text"),
                "confidence": item.get("confidence"),
            }
        )
    return {
        "id": str(entry.get("id")),
        "rule_id": RULE_ID,
        "rule_version": rule_version,
        "status": _status(decision),
        "decision": decision,
        "title": f"PTR 条款 {entry.get('clause_id')}",
        "summary": f"{decision}: {entry.get('reason_code') or 'no_reason'}",
        "details": {
            "clause_id": entry.get("clause_id"),
            "scope_decision": entry.get("scope_decision"),
            "scope_reason_code": entry.get("scope_reason_code"),
            "reason_code": entry.get("reason_code"),
            "title_comparison": entry.get("title_comparison"),
            "text_comparison": entry.get("text_comparison"),
            "numeric_comparison": entry.get("numeric_comparison"),
            "unit_comparison": entry.get("unit_comparison"),
            "table_comparison": entry.get("table_comparison"),
            "reference_comparison": entry.get("reference_comparison"),
        },
        "evidence": evidence,
        "evidence_locations": evidence,
        "contributes_to_overall": decision != EXCLUDED,
    }


def _write_html(path: Path, result: Mapping[str, Any]) -> None:
    rows = []
    for finding in result.get("findings", []):
        details = json.dumps(finding.get("details", {}), ensure_ascii=False, indent=2)
        rows.append(
            "<tr><td><code>{}</code></td><td>{}</td><td>{}</td><td><pre>{}</pre></td></tr>".format(
                html.escape(str(finding.get("details", {}).get("clause_id", ""))),
                html.escape(str(finding.get("status", ""))),
                html.escape(str(finding.get("summary", ""))),
                html.escape(details),
            )
        )
    body = "".join(rows)
    rule_version = str((result.get("rule") or {}).get("version", ""))
    content = f"""<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><title>PTR Draft Runner</title>
<style>body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;margin:32px;color:#101828}}table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #d0d5dd;padding:8px;text-align:left;vertical-align:top}}pre{{white-space:pre-wrap;max-width:720px}}.notice{{padding:12px;background:#fffaeb;color:#7a2e0e}}</style>
<h1>PTR 与 Report 核对草案</h1>
<p class="notice">这是离线 Draft Runner 结果；publishable=false，不能作为正式 PTR Run 通过结论。</p>
<p>总体状态：{html.escape(str(result.get('machine_overall_status')))}；规则：{RULE_ID}@{html.escape(rule_version)}</p>
<table><thead><tr><th>条款</th><th>状态</th><th>原因</th><th>详情</th></tr></thead><tbody>{body}</tbody></table>
</html>"""
    path.write_text(content, encoding="utf-8")


def run_ptr_draft(
    *,
    report_path: str | Path,
    ptr_path: str | Path,
    output_dir: str | Path,
    comparison_mode: str = "rules",
    ocr_mode: str = "auto",
) -> dict[str, Any]:
    report = Path(report_path).expanduser().resolve()
    ptr = Path(ptr_path).expanduser().resolve()
    output = Path(output_dir).expanduser().resolve()
    if not report.is_file() or not ptr.is_file():
        raise FileNotFoundError("Report 和 PTR 都必须是存在的 PDF 文件")
    if output.exists():
        raise PtrDraftRunnerError(f"拒绝覆盖已有 Draft 输出目录：{output}")
    before = {"report": _sha256(report), "ptr": _sha256(ptr)}
    comparison = compare_ptr_report(report, ptr, comparison_mode=comparison_mode, ocr_mode=ocr_mode)
    after = {"report": _sha256(report), "ptr": _sha256(ptr)}
    if before != after:
        raise PtrDraftRunnerError("输入 PDF 在 Draft Runner 执行期间发生变化")
    rule_version = str(comparison["rule"]["version"])
    findings = [_finding(entry, rule_version=rule_version) for entry in comparison["coverage_ledger"]]
    result = {
        "schema_version": DRAFT_RUN_SCHEMA_VERSION,
        "mode": DRAFT_MODE,
        "comparison_mode": comparison_mode,
        "lifecycle_status": "succeeded",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "publishable": False,
        "machine_overall_status": _overall(comparison["summary"]),
        "overall_status": _overall(comparison["summary"]),
        "rule": comparison["rule"],
        "rule_execution_states": {
            RULE_ID: {
                "state": "succeeded",
                "reason_code": "DRAFT_NOT_PUBLISHABLE",
                "finding_count": len(findings),
            }
        },
        "engine": {
            "llm_used": False,
            "agent_used": False,
            "meaning": "Draft Runner 只执行本机规则；rules+llm 只生成待接入队列，不调用模型。",
        },
        "files": [
            {"role": "report", "path": str(report), "sha256": before["report"]},
            {"role": "ptr", "path": str(ptr), "sha256": before["ptr"]},
        ],
        "source_integrity": [
            {"role": role, "sha256_before": before[role], "sha256_after": after[role], "unchanged": True}
            for role in ("report", "ptr")
        ],
        "scope": comparison["scope"],
        "coverage_audit": comparison.get("coverage_audit"),
        "reference_audit": comparison.get("reference_audit"),
        "coverage_ledger": comparison["coverage_ledger"],
        "coverage": comparison["summary"],
        "status_counts": {
            "pass": comparison["summary"]["counts"].get(MATCHED, 0),
            "warning": 0,
            "manual": comparison["summary"]["counts"].get(MANUAL, 0),
            "error": comparison["summary"]["counts"].get(MISMATCH, 0),
            "not_applicable": comparison["summary"]["counts"].get(EXCLUDED, 0),
        },
        "llm": comparison["llm"],
        "findings": findings,
    }
    output.mkdir(parents=True, exist_ok=False)
    temporary = output / ".result.json.tmp"
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(output / "result.json")
    _write_html(output / "index.html", result)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="生成不可发布的 PTR ↔ Report Draft Runner 结果")
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--ptr", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--mode", choices=("rules", "rules+llm"), default="rules")
    parser.add_argument("--ocr-mode", choices=("auto", "never", "required"), default="auto")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = run_ptr_draft(
        report_path=args.report,
        ptr_path=args.ptr,
        output_dir=args.output,
        comparison_mode=args.mode,
        ocr_mode=args.ocr_mode,
    )
    print(json.dumps({"mode": result["mode"], "machine_overall_status": result["machine_overall_status"],
                      "publishable": result["publishable"], "result_json": str((args.output / "result.json").resolve())}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["DRAFT_MODE", "DRAFT_RUN_SCHEMA_VERSION", "PtrDraftRunnerError", "run_ptr_draft"]
