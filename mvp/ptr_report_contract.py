"""PTR ↔ Report 输入、规则和结果合同。

本模块同时描述离线草案和正式 Runner 使用的输入、规则计划、结果与
证据字段。草案保持不可发布；正式能力通过业务授权和目录开关后，沿用
同一套角色命名与证据形状。它不解析 PDF，也不改变 PTR 的启用状态。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping


PTR_REPORT_CONTRACT_SCHEMA_VERSION = "ptr-report-contract-0.1"
PTR_REPORT_RESULT_SCHEMA_VERSION = "ptr-report-result-0.1"
PTR_P01_RULE_ID = "PTR-P01"
# Canonical PTR-P01 rule version.  The formal comparator, capability catalog,
# review gate and draft contract all consume this same value so a result can
# always be traced back to the rule set that produced it.
PTR_P01_RULE_VERSION = "0.5.1-draft"
PTR_P01_DISABLED_REASON = "PTR_NOT_VALIDATED"

PTR_REPORT_MODES = ("report_ptr", "report_ptr_report")
PTR_REPORT_INPUT_ROLES = ("report", "ptr")
PTR_REPORT_INPUT_FIELDS = {
    "report": "report_document_id",
    "ptr": "ptr_document_id",
}
PTR_REPORT_FORBIDDEN_ROLES = ("record_9706_1", "record_9706_202")
FINDING_STATUSES = ("pass", "warning", "manual", "error")


class PtrReportContractError(ValueError):
    """Raised when a PTR ↔ Report contract is incomplete or inconsistent."""


def _input_slots() -> list[dict[str, Any]]:
    return [
        {
            "role": role,
            "field": PTR_REPORT_INPUT_FIELDS[role],
            "required": True,
            "multiplicity": "one",
            "immutable_after_run_creation": True,
        }
        for role in PTR_REPORT_INPUT_ROLES
    ]


def ptr_report_contract_payload(
    mode: str = "report_ptr",
    *,
    enabled: bool = False,
) -> dict[str, Any]:
    """Return a JSON-safe snapshot of the PTR ↔ Report contract.

    ``enabled`` lets callers request the draft or formally enabled variant of
    the same contract. The capability catalog supplies the current state.
    """

    if mode not in PTR_REPORT_MODES:
        raise PtrReportContractError(f"不支持 PTR ↔ Report 模式: {mode}")
    status = "enabled" if enabled else "disabled"
    return {
        "schema_version": PTR_REPORT_CONTRACT_SCHEMA_VERSION,
        "mode": mode,
        "status": status,
        "input_contract": {
            "schema_version": "run-input-contract-1.0",
            "roles": list(PTR_REPORT_INPUT_ROLES),
            "slots": _input_slots(),
            "forbidden_roles": list(PTR_REPORT_FORBIDDEN_ROLES),
            "pairing": "exactly_one_report_and_one_ptr",
            "source_of_truth": "RunInput.role_and_document_id",
        },
        "rule_plan": [
            {
                "rule_id": PTR_P01_RULE_ID,
                "version": PTR_P01_RULE_VERSION,
                "status": status,
                "reason_code": None if enabled else PTR_P01_DISABLED_REASON,
                "scope": "report_declared_scope_ptr_reference_expansion_report_coverage",
            }
        ],
        "result_contract": {
            "schema_version": PTR_REPORT_RESULT_SCHEMA_VERSION,
            "publishable": bool(enabled),
            "finding_statuses": list(FINDING_STATUSES),
            "finding_required_fields": [
                "id",
                "rule_id",
                "rule_version",
                "machine_status",
                "reason_code",
                "scope",
                "observations",
                "normalized_values",
                "expected",
                "comparison",
                "evidence_ids",
                "contributes_to_overall",
            ],
            "evidence_required_fields": [
                "document_id",
                "role",
                "source_sha256",
                "page_index",
                "page_number",
                "bbox",
                "coordinate_space",
                "semantic_role",
            ],
            "overall_status": "null_until_validated_and_published" if not enabled else "severity_aggregate",
        },
        "coverage_contract": {
            "declared_scope": "report_declared_scope",
            "reference_expansion": "ptr_reference_candidates",
            "parent_child_mapping": "ptr_clause_parent_child_candidates",
            "missing_scope_items": "explicit_candidate_list",
            "out_of_scope_items": "explicit_candidate_list",
            "unresolved_policy": "manual_required_with_reason_code",
            "automatic_pass_forbidden_until_validation": not enabled,
        },
    }


def validate_ptr_report_contract(
    contract: Mapping[str, Any],
    *,
    expected_mode: str | None = None,
) -> None:
    """Validate the structural contract without validating PDF semantics."""

    if not isinstance(contract, Mapping):
        raise PtrReportContractError("PTR ↔ Report contract 必须是对象")
    if contract.get("schema_version") != PTR_REPORT_CONTRACT_SCHEMA_VERSION:
        raise PtrReportContractError("PTR ↔ Report contract schema_version 无效")
    mode = contract.get("mode")
    if mode not in PTR_REPORT_MODES or (expected_mode is not None and mode != expected_mode):
        raise PtrReportContractError("PTR ↔ Report contract mode 无效")
    status = contract.get("status")
    if status not in {"disabled", "enabled"}:
        raise PtrReportContractError("PTR ↔ Report contract status 无效")

    input_contract = contract.get("input_contract")
    if not isinstance(input_contract, Mapping):
        raise PtrReportContractError("PTR ↔ Report contract 缺少 input_contract")
    if input_contract.get("schema_version") != "run-input-contract-1.0":
        raise PtrReportContractError("PTR input_contract schema_version 无效")
    if tuple(input_contract.get("roles", ())) != PTR_REPORT_INPUT_ROLES:
        raise PtrReportContractError("PTR input_contract 必须精确包含 report 和 ptr")
    if tuple(input_contract.get("forbidden_roles", ())) != PTR_REPORT_FORBIDDEN_ROLES:
        raise PtrReportContractError("PTR input_contract forbidden_roles 无效")
    slots = input_contract.get("slots")
    if not isinstance(slots, list) or len(slots) != len(PTR_REPORT_INPUT_ROLES):
        raise PtrReportContractError("PTR input_contract slots 不完整")
    observed_slots = [(item.get("role"), item.get("field")) for item in slots if isinstance(item, Mapping)]
    expected_slots = [(role, PTR_REPORT_INPUT_FIELDS[role]) for role in PTR_REPORT_INPUT_ROLES]
    if observed_slots != expected_slots:
        raise PtrReportContractError("PTR input_contract 的角色字段映射无效")
    if input_contract.get("pairing") != "exactly_one_report_and_one_ptr":
        raise PtrReportContractError("PTR input_contract pairing 规则无效")

    rule_plan = contract.get("rule_plan")
    if not isinstance(rule_plan, list) or len(rule_plan) != 1:
        raise PtrReportContractError("PTR 规则计划必须只有一个 PTR-P01 计划项")
    rule = rule_plan[0]
    if not isinstance(rule, Mapping) or rule.get("rule_id") != PTR_P01_RULE_ID:
        raise PtrReportContractError("PTR 规则计划缺少 PTR-P01")
    if rule.get("version") != PTR_P01_RULE_VERSION:
        raise PtrReportContractError("PTR-P01 版本未冻结")
    if rule.get("status") != status:
        raise PtrReportContractError("PTR-P01 规则状态与合同状态不一致")
    if status == "disabled" and rule.get("reason_code") != PTR_P01_DISABLED_REASON:
        raise PtrReportContractError("禁用态 PTR-P01 必须带 PTR_NOT_VALIDATED")
    if status == "enabled" and rule.get("reason_code") is not None:
        raise PtrReportContractError("启用态 PTR-P01 不能保留禁用原因码")

    result_contract = contract.get("result_contract")
    if not isinstance(result_contract, Mapping):
        raise PtrReportContractError("PTR ↔ Report contract 缺少 result_contract")
    if result_contract.get("schema_version") != PTR_REPORT_RESULT_SCHEMA_VERSION:
        raise PtrReportContractError("PTR result_contract schema_version 无效")
    if tuple(result_contract.get("finding_statuses", ())) != FINDING_STATUSES:
        raise PtrReportContractError("PTR Finding 状态集合无效")
    if bool(result_contract.get("publishable")) != (status == "enabled"):
        raise PtrReportContractError("PTR result_contract publishable 与能力状态不一致")


def input_document_descriptors(
    mode: str,
    normalized_inputs: Mapping[str, Any],
    input_snapshot: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Convert Run input IDs and snapshots to a stable, role-labelled list.

    The legacy API keeps ``inputs`` as a field-to-ID mapping. This list is the
    explicit contract consumed by the UI and diagnostics, so it never needs to
    infer PTR versus Report from a filename.
    """

    if mode not in PTR_REPORT_MODES:
        raise PtrReportContractError(f"不是 PTR ↔ Report 模式: {mode}")
    snapshot = input_snapshot or {}
    result: list[dict[str, Any]] = []
    for role in PTR_REPORT_INPUT_ROLES:
        field = PTR_REPORT_INPUT_FIELDS[role]
        item = snapshot.get(role)
        descriptor: dict[str, Any] = {
            "role": role,
            "input_field": field,
            "document_id": normalized_inputs.get(field),
            "blob_sha256": None,
            "page_count": None,
            "page_geometry": [],
            "template_status": "unknown",
            "unresolved": False,
            "reason_code": None,
        }
        if isinstance(item, Mapping):
            for key in (
                "document_id",
                "blob_sha256",
                "size_bytes",
                "media_type",
                "original_filename",
                "page_count",
                "pdf_version",
                "preflight_status",
                "page_geometry",
            ):
                if key in item:
                    descriptor[key] = item[key]
            if item.get("unresolved"):
                descriptor["unresolved"] = True
                descriptor["reason_code"] = "DOCUMENT_NOT_RESOLVED"
        result.append(descriptor)
    return result


def mode_input_contract_payload(mode: str) -> dict[str, Any]:
    """Return a small capability-facing input contract for a PTR mode."""

    contract = ptr_report_contract_payload(mode, enabled=False)
    return {
        "schema_version": contract["input_contract"]["schema_version"],
        "roles": contract["input_contract"]["roles"],
        "slots": contract["input_contract"]["slots"],
        "forbidden_roles": contract["input_contract"]["forbidden_roles"],
        "pairing": contract["input_contract"]["pairing"],
    }


def mode_result_contract_payload(mode: str, *, enabled: bool = False) -> dict[str, Any]:
    """Return the mode result shape exposed by capabilities."""

    contract = ptr_report_contract_payload(mode, enabled=enabled)
    return deepcopy(contract["result_contract"])


def copy_ptr_report_contract(mode: str = "report_ptr", *, enabled: bool = False) -> dict[str, Any]:
    """Return a defensive copy suitable for API responses."""

    contract = ptr_report_contract_payload(mode, enabled=enabled)
    validate_ptr_report_contract(contract, expected_mode=mode)
    return deepcopy(contract)
