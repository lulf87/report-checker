"""Materialize a human-review package for the disabled PTR draft.

The package is an export of machine observations, not an acceptance switch.
It keeps source hashes, both sides' evidence coordinates, hard comparison
fields, and a blank reviewer disposition for every eligible clause.  Editing
the CSV/Markdown cannot change a Coverage Ledger or create a formal Run.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from mvp.ptr_compare import EXCLUDED, compare_ptr_report


PTR_REVIEW_PACKAGE_SCHEMA_VERSION = "ptr-review-package-0.1"
REVIEW_DECISIONS = {
    "confirm_match",
    "confirm_mismatch",
    "confirm_manual",
    "confirm_excluded",
}


def _pages(entry: Mapping[str, Any], role: str) -> list[int]:
    return sorted(
        {
            int(item["pdf_page"])
            for item in entry.get("evidence", [])
            if isinstance(item, Mapping) and item.get("role") == role and item.get("pdf_page")
        }
    )


def _review_row(entry: Mapping[str, Any]) -> dict[str, Any]:
    decision = str(entry.get("decision") or "manual")
    return {
        "ledger_id": str(entry.get("id") or ""),
        "clause_id": str(entry.get("clause_id") or ""),
        "decision": decision,
        "reason_code": str(entry.get("reason_code") or ""),
        "scope_decision": str(entry.get("scope_decision") or ""),
        "scope_reason_code": str(entry.get("scope_reason_code") or ""),
        "ptr_node_id": entry.get("ptr_node_id"),
        "report_item_ids": list(entry.get("report_item_ids") or []),
        "ptr_pages": _pages(entry, "ptr"),
        "report_pages": _pages(entry, "report"),
        "evidence_count": len(entry.get("evidence") or []),
        "numeric_status": (entry.get("numeric_comparison") or {}).get("status"),
        "unit_status": (entry.get("unit_comparison") or {}).get("status"),
        "reference_status": (entry.get("reference_comparison") or {}).get("status"),
        "table_status": (entry.get("table_comparison") or {}).get("status"),
        "review_status": "excluded" if decision == EXCLUDED else "pending",
        "review_decision": "",
        "review_note": "",
    }


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    fields = [
        "ledger_id",
        "clause_id",
        "decision",
        "reason_code",
        "scope_decision",
        "scope_reason_code",
        "ptr_node_id",
        "report_item_ids",
        "ptr_pages",
        "report_pages",
        "evidence_count",
        "numeric_status",
        "unit_status",
        "reference_status",
        "table_status",
        "review_status",
        "review_decision",
        "review_note",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            output = dict(row)
            for field in ("report_item_ids", "ptr_pages", "report_pages"):
                output[field] = json.dumps(output[field], ensure_ascii=False)
            writer.writerow(output)


def _write_markdown(path: Path, result: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> None:
    summary = result.get("summary") or {}
    lines = [
        "# PTR 与 Report 人工验收包",
        "",
        "这是禁用态 PTR 草案的人工复核材料，不是正式 Run，也不能直接改变 Coverage Ledger。",
        "",
        f"- 规则：`{(result.get('rule') or {}).get('id')}@{(result.get('rule') or {}).get('version')}`",
        f"- 规则模式：`{result.get('comparison_mode')}`",
        f"- 机器摘要：`{summary.get('counts')}`",
        f"- `eligible == accounted`：`{summary.get('eligible') == summary.get('accounted')}`",
        f"- `conserved`：`{summary.get('conserved')}`",
        "",
        "## 复核步骤",
        "",
        "逐条检查范围、条款父子关系、正文、数值/单位、表格/附录引用和双方证据页码；在 `review.csv` 中填写 `review_status`、`review_decision` 和 `review_note`。机器状态和原因码保留原样。",
        "",
        "## 条款队列",
        "",
        "| 条款 | 机器状态 | 原因 | PTR 页 | Report 页 | 数值 | 单位 | 表格 | 复核状态 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            "| {clause_id} | {decision} | {reason_code} | {ptr_pages} | {report_pages} | {numeric_status} | {unit_status} | {table_status} | {review_status} |".format(
                **row
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_ptr_review_package(
    *,
    report_path: str | Path,
    ptr_path: str | Path,
    output_dir: str | Path,
    comparison_mode: str = "rules",
    ocr_mode: str = "auto",
) -> dict[str, Any]:
    """Write one immutable-input review package and return its manifest."""

    output = Path(output_dir).expanduser().resolve()
    if output.exists():
        raise FileExistsError(f"拒绝覆盖已有 PTR Review Package：{output}")
    comparison = compare_ptr_report(
        report_path,
        ptr_path,
        comparison_mode=comparison_mode,
        ocr_mode=ocr_mode,
    )
    rows = [_review_row(entry) for entry in comparison["coverage_ledger"]]
    manifest = {
        "schema_version": PTR_REVIEW_PACKAGE_SCHEMA_VERSION,
        "package_kind": "ptr_human_review",
        "publishable": False,
        "comparison_mode": comparison_mode,
        "rule": comparison["rule"],
        "inputs": comparison["inputs"],
        "summary": comparison["summary"],
        "llm": comparison["llm"],
        "review_policy": {
            "machine_decision_immutable": True,
            "review_status_values": ["pending", "confirmed", "rejected", "excluded"],
            "review_decision_does_not_publish": True,
        },
        "rows": rows,
        "coverage_ledger": comparison["coverage_ledger"],
    }
    output.mkdir(parents=True, exist_ok=False)
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _write_csv(output / "review.csv", rows)
    _write_markdown(output / "review.md", comparison, rows)
    return manifest


def build_batch_review_package(
    *,
    root: str | Path,
    samples: Iterable[str],
    output_dir: str | Path,
    ocr_mode: str = "auto",
) -> dict[str, Any]:
    """Build packages for named ``素材/ptr`` and ``素材/report`` samples."""

    base = Path(root).expanduser().resolve()
    destination = Path(output_dir).expanduser().resolve()
    sample_names = list(samples)
    if destination.exists():
        raise FileExistsError(f"拒绝覆盖已有 PTR Review Batch：{destination}")
    destination.mkdir(parents=True, exist_ok=False)
    manifests = []
    for sample in sample_names:
        ptr_files = sorted((base / "素材" / "ptr" / sample).glob("*.pdf"))
        report_files = sorted((base / "素材" / "report" / sample).glob("*.pdf"))
        if len(ptr_files) != 1 or len(report_files) != 1:
            raise FileNotFoundError(f"样本 {sample} 必须各有一个 PTR 和 Report PDF")
        manifests.append(
            build_ptr_review_package(
                report_path=report_files[0],
                ptr_path=ptr_files[0],
                output_dir=destination / sample,
                ocr_mode=ocr_mode,
            )
        )
    batch = {
        "schema_version": PTR_REVIEW_PACKAGE_SCHEMA_VERSION,
        "package_kind": "ptr_human_review_batch",
        "publishable": False,
        "samples": sample_names,
        "packages": manifests,
    }
    (destination / "batch.json").write_text(json.dumps(batch, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return batch


def validate_review_rows(
    manifest: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    *,
    reviewer: str,
) -> dict[str, Any]:
    """Validate reviewer actions without changing machine conclusions."""

    reviewer = str(reviewer or "").strip()
    if not reviewer:
        raise ValueError("reviewer 不能为空")
    expected = {str(row.get("ledger_id")): row for row in manifest.get("rows", [])}
    actual = {str(row.get("ledger_id")): row for row in rows}
    if set(expected) != set(actual):
        raise ValueError("复核 CSV 必须逐条覆盖 manifest 中的条款，不能增删对象")
    actions: list[dict[str, Any]] = []
    for ledger_id, machine in expected.items():
        row = actual[ledger_id]
        for field in ("clause_id", "decision", "reason_code", "scope_decision", "ptr_node_id"):
            if str(row.get(field, "")) != str(machine.get(field, "")):
                raise ValueError(f"条款 {ledger_id} 的机器字段 {field} 被修改")
        machine_decision = str(machine.get("decision"))
        status = str(row.get("review_status") or "")
        action = str(row.get("review_decision") or "")
        if machine_decision == EXCLUDED:
            if status != "excluded" or action not in {"", "confirm_excluded"}:
                raise ValueError(f"范围外条款 {ledger_id} 必须保持 excluded")
            action = "confirm_excluded"
        else:
            if status != "confirmed" or action not in REVIEW_DECISIONS - {"confirm_excluded"}:
                raise ValueError(f"条款 {ledger_id} 必须填写 confirmed 和有效复核动作")
        actions.append(
            {
                "ledger_id": ledger_id,
                "clause_id": machine.get("clause_id"),
                "machine_decision": machine_decision,
                "machine_reason_code": machine.get("reason_code"),
                "review_status": status,
                "review_decision": action,
                "review_note": str(row.get("review_note") or "").strip(),
                "reviewer": reviewer,
            }
        )
    return {
        "review_schema_version": "ptr-review-action-0.1",
        "reviewer": reviewer,
        "review_complete": True,
        "machine_conclusion_immutable": True,
        "publishable": False,
        "actions": actions,
    }


def apply_review_csv(
    *,
    manifest_path: str | Path,
    csv_path: str | Path,
    output_path: str | Path,
    reviewer: str,
) -> dict[str, Any]:
    """Validate a filled review CSV and write a non-publishable audit result."""

    manifest_file = Path(manifest_path).expanduser().resolve()
    csv_file = Path(csv_path).expanduser().resolve()
    output_file = Path(output_path).expanduser().resolve()
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    with csv_file.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    review = validate_review_rows(manifest, rows, reviewer=reviewer)
    result = {
        "schema_version": PTR_REVIEW_PACKAGE_SCHEMA_VERSION,
        "package_kind": "ptr_human_reviewed",
        "publishable": False,
        "source_manifest_sha256": _sha256_file(manifest_file),
        "machine_inputs": manifest.get("inputs"),
        "machine_summary": manifest.get("summary"),
        "machine_ledger": manifest.get("coverage_ledger"),
        "review": review,
    }
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def _sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "PTR_REVIEW_PACKAGE_SCHEMA_VERSION",
    "REVIEW_DECISIONS",
    "apply_review_csv",
    "build_batch_review_package",
    "build_ptr_review_package",
    "validate_review_rows",
]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成不可发布的 PTR 人工验收包")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--sample", action="append", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--ocr-mode", choices=("auto", "never", "required"), default="auto")
    args = parser.parse_args(argv)
    result = build_batch_review_package(
        root=args.root,
        samples=args.sample,
        output_dir=args.output,
        ocr_mode=args.ocr_mode,
    )
    print(json.dumps({"schema_version": result["schema_version"], "output": str(args.output.resolve()), "samples": result["samples"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
