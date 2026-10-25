"""Formal PTR ↔ Report runner behind the explicit capability gate.

The default entry point refuses to produce a publishable result.  The unified
dispatcher may pass ``allow_formal_publish=True`` only after the capability
catalog has been explicitly enabled following business sign-off.  This keeps
direct draft calls from bypassing the formal Run/Worker publication contract.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from mvp.capabilities import validate_mode
from mvp.ptr_compare import EXCLUDED, MANUAL, MATCHED, MISMATCH, compare_ptr_report
from mvp.ptr_draft_runner import _finding


FORMAL_MODE = "report_ptr"
FORMAL_RESULT_SCHEMA_VERSION = "ptr-report-result-0.1"


class PtrFormalRunnerError(ValueError):
    """Raised when formal PTR execution is attempted outside the gate."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _overall(counts: dict[str, int]) -> str:
    if counts["error"]:
        return "error"
    if counts["manual"]:
        return "manual"
    if counts["warning"]:
        return "warning"
    return "pass"


def _write_html(path: Path, result: dict[str, Any]) -> None:
    rows = []
    for finding in result.get("findings", []):
        details = json.dumps(finding.get("details", {}), ensure_ascii=False, indent=2)
        rows.append(
            "<tr><td>{}</td><td>{}</td><td>{}</td><td><pre>{}</pre></td></tr>".format(
                html.escape(str(finding.get("details", {}).get("clause_id", ""))),
                html.escape(str(finding.get("status", ""))),
                html.escape(str(finding.get("summary", ""))),
                html.escape(details),
            )
        )
    content = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<title>PTR 与 Report 正式核对</title>
<style>body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;margin:32px;color:#101828}}table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #d0d5dd;padding:8px;text-align:left;vertical-align:top}}pre{{white-space:pre-wrap;max-width:720px}}.notice{{padding:12px;background:#ecfdf3;color:#05603a}}</style>
<h1>PTR 与 Report 正式核对</h1>
<p class="notice">这是经过能力目录启用闸门的正式结果；证据仍来自规则比较，LLM 不能覆盖硬差异。</p>
<p>总体状态：{html.escape(str(result.get("machine_overall_status")))}；规则：{html.escape(str((result.get("rule") or {}).get("version")))}</p>
<table><thead><tr><th>条款</th><th>状态</th><th>原因</th><th>详情</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
</html>"""
    path.write_text(content, encoding="utf-8")


def run_ptr_report(
    *,
    report_path: str | Path,
    ptr_path: str | Path,
    output_dir: str | Path,
    comparison_mode: str = "rules",
    ocr_mode: str = "auto",
    allow_formal_publish: bool = False,
) -> dict[str, Any]:
    """Run the formal PTR result contract after an explicit capability gate."""

    if not allow_formal_publish:
        raise PtrFormalRunnerError("PTR 正式能力尚未启用；只能运行 report_ptr_draft")
    decision = validate_mode(FORMAL_MODE, operation="formal_runner")
    if not decision["enabled"]:
        raise PtrFormalRunnerError(str(decision.get("message") or "PTR 正式能力尚未启用"))
    report = Path(report_path).expanduser().resolve()
    ptr = Path(ptr_path).expanduser().resolve()
    output = Path(output_dir).expanduser().resolve()
    if not report.is_file() or not ptr.is_file():
        raise FileNotFoundError("Report 和 PTR 都必须是存在的 PDF 文件")
    if output.exists():
        raise PtrFormalRunnerError(f"拒绝覆盖已有 PTR 正式输出目录：{output}")
    before = {"report": _sha256(report), "ptr": _sha256(ptr)}
    comparison = compare_ptr_report(report, ptr, comparison_mode=comparison_mode, ocr_mode=ocr_mode)
    after = {"report": _sha256(report), "ptr": _sha256(ptr)}
    if before != after:
        raise PtrFormalRunnerError("输入 PDF 在 PTR 正式 Runner 执行期间发生变化")
    eligible_entries = [
        entry for entry in comparison["coverage_ledger"] if entry.get("decision") != EXCLUDED
    ]
    if not eligible_entries or not comparison["summary"].get("conserved"):
        raise PtrFormalRunnerError("PTR Coverage Ledger 未守恒，拒绝生成正式结果")
    rule_version = str((comparison.get("rule") or {}).get("version", ""))
    findings = [_finding(entry, rule_version=rule_version) for entry in eligible_entries]
    status_counts = {"pass": 0, "warning": 0, "manual": 0, "error": 0}
    for finding in findings:
        status_counts[str(finding["status"])] += 1
    result = {
        "schema_version": FORMAL_RESULT_SCHEMA_VERSION,
        "mode": FORMAL_MODE,
        "comparison_mode": comparison_mode,
        "lifecycle_status": "succeeded",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "publishable": True,
        "machine_overall_status": _overall(status_counts),
        "overall_status": _overall(status_counts),
        "rule": {
            "id": comparison["rule"]["id"],
            "version": rule_version,
            "status": "validated",
            "automatic_pass_forbidden": False,
        },
        "rule_execution_states": {
            "PTR-P01": {
                "state": "succeeded",
                "reason_code": None,
                "finding_count": len(findings),
            }
        },
        "engine": {
            "llm_used": False,
            "agent_used": False,
            "meaning": "正式 PTR 结果先执行规则；rules+llm 只能处理文本人工队列。",
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
        "status_counts": status_counts,
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
    parser = argparse.ArgumentParser(description="生成经启用闸门的 PTR 与 Report 正式结果")
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--ptr", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--mode", choices=("rules", "rules+llm"), default="rules")
    parser.add_argument("--ocr-mode", choices=("auto", "never", "required"), default="auto")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = run_ptr_report(
        report_path=args.report,
        ptr_path=args.ptr,
        output_dir=args.output,
        comparison_mode=args.mode,
        ocr_mode=args.ocr_mode,
        allow_formal_publish=False,
    )
    print(json.dumps({"mode": result["mode"], "publishable": result["publishable"]}, ensure_ascii=False, indent=2))
    return 0


__all__ = ["FORMAL_MODE", "FORMAL_RESULT_SCHEMA_VERSION", "PtrFormalRunnerError", "run_ptr_report"]
