"""禁用态 PTR-P01 审计骨架。

这个模块故意不接入 ``mvp.run_modes`` 或能力目录。它只生成不可发布的
RequirementGraph 草案和 Finding 契约样例，供 PTR 规则冻结、人工验收和
测试夹具使用。任何未完成的声明范围、引用或父子条款映射都保持
``manual_required`` / ``not_evaluated``，绝不产生自动通过结论。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover - compatibility with older environments
    import fitz

from mvp.checker import extract_report_number
from mvp.ptr_report_contract import (
    PTR_P01_DISABLED_REASON,
    PTR_P01_RULE_ID,
    PTR_P01_RULE_VERSION,
    copy_ptr_report_contract,
    validate_ptr_report_contract,
)
from mvp.ptr_scope import extract_report_scope


PTR_P01_DRAFT_SCHEMA_VERSION = "ptr-p01-draft-0.1"
PTR_P01_FINDING_SCHEMA_VERSION = "ptr-p01-finding-0.1"

CLAUSE_RE = re.compile(
    r"^\s*(?P<clause>\d+(?:\.\d+){0,6})"
    r"(?:[.．、:：)）]|\s+)(?P<body>.+?)\s*$"
)
REFERENCE_PREFIX_RE = re.compile(
    r"(?:见|参见|详见|按照|依据|依照|符合|满足|参照)[^。；;\n]{0,100}"
)
REFERENCE_TOKEN_RE = re.compile(
    r"(?:表\s*[0-9A-Za-z一二三四五六七八九十]+|"
    r"附录\s*[A-Za-z一二三四五六七八九十]+|"
    r"(?:GB|YY)\s*[0-9][0-9A-Za-z.\-]*)"
)
class PtrP01DraftError(ValueError):
    """Raised when a draft cannot satisfy its own audit invariants."""


@dataclass(frozen=True)
class EvidenceRef:
    role: str
    pdf_page: int
    bbox: tuple[float, float, float, float]
    source_sha256: str
    semantic_role: str
    text: str
    extraction_method: str = "pymupdf_text_blocks"

    def as_dict(self) -> dict[str, Any]:
        return {
            # Draft documents do not yet have persisted Document UUIDs. The
            # deterministic draft reference still prevents the UI from
            # guessing a side from a filename and is replaced by the real
            # Document ID when a validated Run is published.
            "document_id": f"draft:{self.role}:{self.source_sha256[:16]}",
            "role": self.role,
            "pdf_page": self.pdf_page,
            "page_index": self.pdf_page - 1,
            "page_number": self.pdf_page,
            "bbox": [round(float(value), 3) for value in self.bbox],
            "source_sha256": self.source_sha256,
            "coordinate_space": "pdf_points",
            "precision": "region",
            "extraction_method": self.extraction_method,
            "semantic_role": self.semantic_role,
            "text_anchor": self.text,
            "text": self.text,
        }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _bbox(value: Sequence[float]) -> tuple[float, float, float, float]:
    if len(value) != 4:
        raise PtrP01DraftError("bbox 必须包含四个坐标")
    numbers = tuple(float(item) for item in value)
    if not all(number == number and abs(number) != float("inf") for number in numbers):
        raise PtrP01DraftError("bbox 坐标必须是有限数字")
    x0, y0, x1, y1 = numbers
    if x0 < 0 or y0 < 0 or x0 >= x1 or y0 >= y1:
        raise PtrP01DraftError("bbox 必须是非空的正矩形")
    return numbers


def _relative_or_absolute(path: Path, root: Path | None) -> str:
    if root is not None:
        try:
            return path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            pass
    return str(path)


def _document_info(path: Path, role: str, root: Path | None) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    digest = _sha256(path)
    with fitz.open(path) as document:
        page_geometry = [
            {
                "page": index + 1,
                "width": round(float(page.rect.width), 3),
                "height": round(float(page.rect.height), 3),
            }
            for index, page in enumerate(document)
        ]
        first_page_text = document[0].get_text("text") if document.page_count else ""
    return {
        "role": role,
        "path": _relative_or_absolute(path, root),
        "sha256": digest,
        "page_count": len(page_geometry),
        "page_geometry": page_geometry,
        "report_number": (
            list(extract_report_number(first_page_text))
            if role == "report" and extract_report_number(first_page_text) is not None
            else None
        ),
    }


def _blocks(path: Path, role: str, source_sha256: str) -> Iterable[tuple[int, int, tuple[float, float, float, float], str]]:
    with fitz.open(path) as document:
        for page_index, page in enumerate(document, start=1):
            for block_index, block in enumerate(page.get_text("blocks"), start=1):
                if len(block) < 5:
                    continue
                text = str(block[4] or "").strip()
                if not text:
                    continue
                rect = _bbox(block[:4])
                yield page_index, block_index, rect, text


def _evidence(
    *,
    role: str,
    page: int,
    rect: Sequence[float],
    source_sha256: str,
    semantic_role: str,
    text: str,
    extraction_method: str = "pymupdf_text_blocks",
) -> dict[str, Any]:
    return EvidenceRef(
        role=role,
        pdf_page=page,
        bbox=_bbox(rect),
        source_sha256=source_sha256,
        semantic_role=semantic_role,
        text=text,
        extraction_method=extraction_method,
    ).as_dict()


def _line_candidates(
    *,
    path: Path,
    role: str,
    source_sha256: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    clauses: list[dict[str, Any]] = []
    references: list[dict[str, Any]] = []
    occurrences: defaultdict[str, int] = defaultdict(int)
    for page, block_index, rect, block_text in _blocks(path, role, source_sha256):
        for line_index, raw_line in enumerate(block_text.splitlines(), start=1):
            line = " ".join(raw_line.split())
            if not line:
                continue
            match = CLAUSE_RE.match(line)
            clause_id = match.group("clause") if match else None
            body = match.group("body").strip() if match else line
            if clause_id and len(body) >= 2:
                occurrences[clause_id] += 1
                node_id = f"{role}:clause:{clause_id}:p{page}:o{occurrences[clause_id]}"
                clauses.append(
                    {
                        "node_id": node_id,
                        "kind": "ptr_clause_candidate" if role == "ptr" else "report_clause_candidate",
                        "clause_id": clause_id,
                        "text": line,
                        "validation_status": "manual_required",
                        "evidence": [
                            _evidence(
                                role=role,
                                page=page,
                                rect=rect,
                                source_sha256=source_sha256,
                                semantic_role="clause_candidate",
                                text=line,
                            )
                        ],
                        "source_block": {"block_index": block_index, "line_index": line_index},
                    }
                )
            snippets: list[str] = []
            snippets.extend(item.group(0).strip() for item in REFERENCE_PREFIX_RE.finditer(line))
            snippets.extend(item.group(0).strip() for item in REFERENCE_TOKEN_RE.finditer(line))
            seen: set[str] = set()
            for snippet in snippets:
                if not snippet or snippet in seen:
                    continue
                seen.add(snippet)
                references.append(
                    {
                        "reference_id": f"{role}:reference:p{page}:b{block_index}:l{line_index}:{len(references)+1}",
                        "kind": "reference_candidate",
                        "text": snippet,
                        "validation_status": "manual_required",
                        "evidence": [
                            _evidence(
                                role=role,
                                page=page,
                                rect=rect,
                                source_sha256=source_sha256,
                                semantic_role="reference_candidate",
                                text=line,
                            )
                        ],
                    }
                )
    return clauses, references


def _report_scope_candidates(
    path: Path,
    source_sha256: str,
) -> list[dict[str, Any]]:
    scope = extract_report_scope(path)
    source = scope.get("source") or {}
    raw_text = str(scope.get("raw_text") or source.get("text") or "")
    if not raw_text or not source.get("bbox"):
        return []
    if source.get("source_sha256") != source_sha256:
        raise PtrP01DraftError("Report 范围证据未绑定当前输入 SHA-256")
    page = int(source.get("pdf_page", 0))
    status = "candidate" if scope.get("status") == "parsed" else "manual_required"
    return [
        {
            "scope_id": f"report:declared-scope:p{page:03d}",
            "kind": "report_declared_scope_candidate",
            "raw_text": raw_text,
            "validation_status": status,
            "scope_set": scope,
            "evidence": [
                _evidence(
                    role="report",
                    page=page,
                    rect=source["bbox"],
                    source_sha256=source_sha256,
                    semantic_role="report_declared_scope",
                    text=raw_text,
                    extraction_method=str(source.get("extraction_method", "pymupdf_table_cell")),
                )
            ],
        }
    ]


def _parent_edges(clauses: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    by_clause: defaultdict[str, list[str]] = defaultdict(list)
    for node in clauses:
        by_clause[str(node["clause_id"])].append(str(node["node_id"]))
    edges: list[dict[str, Any]] = []
    for node in clauses:
        clause_id = str(node["clause_id"])
        parts = clause_id.split(".")
        if len(parts) < 2:
            continue
        parent_clause = ".".join(parts[:-1])
        parent_nodes = by_clause.get(parent_clause, [])
        edges.append(
            {
                "edge_id": f"parent-child:{node['node_id']}",
                "relation": "parent_child_candidate",
                "source_node_id": node["node_id"],
                "target_node_id": parent_nodes[0] if parent_nodes else None,
                "target_clause_id": parent_clause,
                "validation_status": "manual_required" if not parent_nodes else "candidate",
                "reason_code": "parent_clause_not_observed" if not parent_nodes else "parent_clause_observed",
            }
        )
    return edges


def _rule_freeze() -> dict[str, Any]:
    return {
        "status": "draft_not_validated",
        "automatic_pass_forbidden": True,
        "decision_policy": "任何范围、引用、父子条款或文本归属不确定时保持 manual_required；不使用 Report 结果反推 PTR 内容。",
        "declaration_scope": {
            "input": "Report 的‘检验项目’声明范围",
            "required_evidence": ["report_declared_scope"],
            "unresolved_policy": "not_evaluated/PTR_NOT_VALIDATED",
        },
        "references": {
            "input": "PTR 正文中的表、附录、GB/YY 标准和‘见/参见/依据/符合’引用",
            "required_evidence": ["reference_candidate"],
            "cycle_policy": "manual_required/reference_cycle_candidate",
            "unresolved_policy": "not_evaluated/PTR_NOT_VALIDATED",
        },
        "parent_child": {
            "input": "层级条款号及其正文归属",
            "mapping_policy": "仅按观察到的点号层级生成候选边，不把缺失父条款补成事实",
            "unresolved_policy": "not_evaluated/PTR_NOT_VALIDATED",
        },
        "missing_scope_items": {
            "decision": "not_evaluated",
            "reason_code": PTR_P01_DISABLED_REASON,
            "must_not_be_pass": True,
        },
        "out_of_scope_items": {
            "decision": "not_evaluated",
            "reason_code": PTR_P01_DISABLED_REASON,
            "must_not_be_pass": True,
        },
    }


def _finding_template(graph: Mapping[str, Any]) -> dict[str, Any]:
    evidence: list[dict[str, Any]] = []
    scopes = graph.get("report_declared_scope_candidates") or []
    if scopes:
        evidence.extend(scopes[0].get("evidence", []))
    clauses = graph.get("ptr_clause_candidates") or []
    if clauses:
        evidence.extend(clauses[0].get("evidence", []))
    return {
        "publishable": False,
        "schema_version": PTR_P01_FINDING_SCHEMA_VERSION,
        "id": "PTR-P01-DRAFT-REVIEW-001",
        "rule_id": PTR_P01_RULE_ID,
        "rule_version": PTR_P01_RULE_VERSION,
        "status": "manual",
        "machine_status": "manual",
        "title": "PTR 声明范围与 Report 正文覆盖待人工验收",
        "summary": "这是禁用态契约样例，不是已发布 Finding。",
        "reason_code": PTR_P01_DISABLED_REASON,
        "scope": {
            "scope_id": "ptr-p01:declaration-and-coverage",
            "kind": "declaration_scope_and_coverage",
        },
        "details": {
            "decision": "not_evaluated",
            "observations": [],
            "normalized_values": [],
            "expected": None,
            "comparison": None,
            "review_required": True,
        },
        "observations": [],
        "normalized_values": [],
        "expected": None,
        "comparison": None,
        "evidence_ids": [],
        "evidence_locations": evidence,
        "review_status": "pending",
        "contributes_to_overall": False,
    }


def build_ptr_p01_draft(
    report_path: str | Path,
    ptr_path: str | Path,
    *,
    root: str | Path | None = None,
) -> dict[str, Any]:
    report = Path(report_path).expanduser().resolve()
    ptr = Path(ptr_path).expanduser().resolve()
    root_path = Path(root).expanduser().resolve() if root is not None else None
    report_info = _document_info(report, "report", root_path)
    ptr_info = _document_info(ptr, "ptr", root_path)
    report_clauses, report_references = _line_candidates(
        path=report, role="report", source_sha256=report_info["sha256"]
    )
    ptr_clauses, ptr_references = _line_candidates(
        path=ptr, role="ptr", source_sha256=ptr_info["sha256"]
    )
    declaration_scopes = _report_scope_candidates(report, report_info["sha256"])
    clauses = [*report_clauses, *ptr_clauses]
    contract = copy_ptr_report_contract("report_ptr")
    graph = {
        "graph_schema_version": "requirement-graph-draft-0.1",
        "contract_schema_version": contract["schema_version"],
        "status": "draft_not_validated",
        "nodes": clauses,
        "edges": _parent_edges(clauses),
        "report_declared_scope_candidates": declaration_scopes,
        "ptr_clause_candidates": ptr_clauses,
        "reference_candidates": [*report_references, *ptr_references],
        "missing_scope_candidates": [],
        "out_of_scope_candidates": [],
        "coverage_audit": {
            "status": "not_evaluated",
            "reason_code": PTR_P01_DISABLED_REASON,
            "report_scope_candidate_count": len(declaration_scopes),
            "ptr_clause_candidate_count": len(ptr_clauses),
            "reference_candidate_count": len(ptr_references),
            "missing_count": None,
            "out_of_scope_count": None,
        },
        "rule_freeze": _rule_freeze(),
        "input_bindings": {
            "report": {
                "role": "report",
                "source_sha256": report_info["sha256"],
                "page_count": report_info["page_count"],
            },
            "ptr": {
                "role": "ptr",
                "source_sha256": ptr_info["sha256"],
                "page_count": ptr_info["page_count"],
            },
        },
    }
    result = {
        "schema_version": PTR_P01_DRAFT_SCHEMA_VERSION,
        "result_kind": "disabled_draft",
        "mode": "report_ptr",
        "publishable": False,
        "contract": contract,
        "input_contract": contract["input_contract"],
        "result_contract": contract["result_contract"],
        "rule": {
            "id": PTR_P01_RULE_ID,
            "version": PTR_P01_RULE_VERSION,
            "status": "disabled",
            "disabled_reason_code": PTR_P01_DISABLED_REASON,
        },
        "inputs": [report_info, ptr_info],
        "rule_execution": {
            "rule_id": PTR_P01_RULE_ID,
            "rule_version": PTR_P01_RULE_VERSION,
            "state": "unsupported",
            "reason_code": PTR_P01_DISABLED_REASON,
            "finding_count": 0,
        },
        "machine_overall_status": None,
        "status_counts": {"pass": 0, "warning": 0, "manual": 0, "error": 0},
        "findings": [],
        "finding_template": _finding_template(graph),
        "requirement_graph": graph,
    }
    validate_ptr_p01_draft(result)
    return result


def _validate_location(location: Mapping[str, Any], documents: Mapping[str, Mapping[str, Any]]) -> None:
    role = location.get("role")
    page = location.get("pdf_page")
    bbox = location.get("bbox")
    if role not in documents or not isinstance(page, int) or isinstance(page, bool):
        raise PtrP01DraftError("Evidence role 或页码无效")
    geometry = documents[role]["page_geometry"]
    if page < 1 or page > len(geometry):
        raise PtrP01DraftError("Evidence 页码超出输入文档")
    x0, y0, x1, y1 = _bbox(bbox)
    if x1 > geometry[page - 1]["width"] or y1 > geometry[page - 1]["height"]:
        raise PtrP01DraftError("Evidence bbox 超出页面范围")
    if location.get("source_sha256") != documents[role]["sha256"]:
        raise PtrP01DraftError("Evidence 未绑定当前输入 SHA-256")
    if location.get("page_index") != page - 1 or location.get("page_number") != page:
        raise PtrP01DraftError("Evidence 页码的 0/1 起算字段不一致")
    if location.get("coordinate_space") != "pdf_points":
        raise PtrP01DraftError("Evidence coordinate_space 必须是 pdf_points")


def validate_ptr_p01_draft(result: Mapping[str, Any]) -> None:
    if result.get("schema_version") != PTR_P01_DRAFT_SCHEMA_VERSION:
        raise PtrP01DraftError("PTR 草案 schema_version 无效")
    if result.get("mode") != "report_ptr" or result.get("publishable") is not False:
        raise PtrP01DraftError("PTR 草案必须明确为不可发布的 report_ptr 草案")
    try:
        validate_ptr_report_contract(result.get("contract", {}), expected_mode="report_ptr")
    except ValueError as error:
        raise PtrP01DraftError(str(error)) from error
    contract = result["contract"]
    if result.get("input_contract") != contract.get("input_contract"):
        raise PtrP01DraftError("PTR 草案 input_contract 必须与总合同一致")
    if result.get("result_contract") != contract.get("result_contract"):
        raise PtrP01DraftError("PTR 草案 result_contract 必须与总合同一致")
    rule = result.get("rule")
    execution = result.get("rule_execution")
    if not isinstance(rule, Mapping) or rule.get("id") != PTR_P01_RULE_ID or rule.get("status") != "disabled":
        raise PtrP01DraftError("PTR 草案规则状态无效")
    if not isinstance(execution, Mapping) or execution.get("state") != "unsupported" or execution.get("reason_code") != PTR_P01_DISABLED_REASON:
        raise PtrP01DraftError("PTR 草案执行状态必须是带原因的 unsupported")
    findings = result.get("findings")
    if findings != []:
        raise PtrP01DraftError("禁用态 PTR 草案不得发布 Finding")
    inputs = result.get("inputs")
    if not isinstance(inputs, list) or {item.get("role") for item in inputs if isinstance(item, Mapping)} != {"report", "ptr"}:
        raise PtrP01DraftError("PTR 草案必须绑定 Report 和 PTR 两个输入")
    documents = {str(item["role"]): item for item in inputs}
    for item in inputs:
        if not isinstance(item.get("sha256"), str) or not item.get("sha256"):
            raise PtrP01DraftError("输入缺少 SHA-256")
        if not isinstance(item.get("page_geometry"), list) or len(item["page_geometry"]) != item.get("page_count"):
            raise PtrP01DraftError("输入页面几何快照不完整")
    graph = result.get("requirement_graph")
    if not isinstance(graph, Mapping) or graph.get("status") != "draft_not_validated":
        raise PtrP01DraftError("RequirementGraph 必须保持 draft_not_validated")
    for collection_key in ("nodes", "report_declared_scope_candidates", "ptr_clause_candidates", "reference_candidates"):
        for item in graph.get(collection_key, []):
            for location in item.get("evidence", []):
                _validate_location(location, documents)
    template = result.get("finding_template")
    if not isinstance(template, Mapping) or template.get("publishable") is not False:
        raise PtrP01DraftError("Finding 模板必须明确不可发布")
    if template.get("machine_status") != "manual" or template.get("rule_version") != PTR_P01_RULE_VERSION:
        raise PtrP01DraftError("Finding 模板缺少正式机器状态或规则版本")
    if template.get("evidence_ids") != [] or template.get("review_status") != "pending":
        raise PtrP01DraftError("禁用态 Finding 模板不能伪造发布证据或复核完成")
    for location in template.get("evidence_locations", []):
        _validate_location(location, documents)


def write_ptr_p01_draft(
    report_path: str | Path,
    ptr_path: str | Path,
    output_path: str | Path,
    *,
    root: str | Path | None = None,
) -> dict[str, Any]:
    result = build_ptr_p01_draft(report_path, ptr_path, root=root)
    destination = Path(output_path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(destination)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="生成不可发布的 PTR-P01 RequirementGraph 草案")
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--ptr", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--root", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = write_ptr_p01_draft(args.report, args.ptr, args.output, root=args.root)
    print(
        json.dumps(
            {
                "result_kind": result["result_kind"],
                "publishable": result["publishable"],
                "rule_id": PTR_P01_RULE_ID,
                "rule_version": PTR_P01_RULE_VERSION,
                "reason_code": PTR_P01_DISABLED_REASON,
                "output": str(args.output.resolve()),
                "candidate_counts": {
                    "report_scope": len(result["requirement_graph"]["report_declared_scope_candidates"]),
                    "ptr_clauses": len(result["requirement_graph"]["ptr_clause_candidates"]),
                    "references": len(result["requirement_graph"]["reference_candidates"]),
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
