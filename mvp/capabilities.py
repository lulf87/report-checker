"""静态能力与规则目录。

该目录是调度器、只读 API 和前端之间共享的单一事实来源。目录只描述
已经冻结的输入角色、启用状态和规则计划，不执行任何 PDF 检查。
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import Any


DOCUMENT_ROLES = (
    "report",
    "ptr",
    "record_9706_1",
    "record_9706_202",
)

RULE_BUNDLE_ID = "report-checks-mvp-2026-09-30"


def validate_mode(mode: str, *, operation: str = "execute") -> dict[str, Any]:
    """Return one authoritative decision for every mode lifecycle entry.

    Callers keep their own domain exception types, while this helper keeps the
    supported/disabled decision and its stable error metadata in one place.
    ``operation`` is recorded in the decision so logs and API errors can name
    the rejected lifecycle boundary (preflight, queue, worker, publish, ...).
    """

    capability = MODE_CATALOG.get(mode)
    if capability is None:
        return {
            "enabled": False,
            "code": "UNSUPPORTED_MODE",
            "message": "模式不受支持",
            "status": 422,
            "details": {"mode": mode, "operation": operation},
            "capability": None,
        }
    if not capability["enabled"]:
        return {
            "enabled": False,
            "code": "MODE_DISABLED",
            "message": str(capability.get("disabled_message", "模式尚未启用")),
            "status": 409,
            "details": {
                "mode": mode,
                "operation": operation,
                "disabled_reason_code": capability.get("disabled_reason_code"),
            },
            "capability": capability,
        }
    return {
        "enabled": True,
        "code": None,
        "message": None,
        "status": 200,
        "details": {"mode": mode, "operation": operation},
        "capability": capability,
    }


# Keep tuples internally so the dispatcher remains immutable by convention and
# existing callers can continue to inspect required_roles as tuples.
MODE_CATALOG: dict[str, dict[str, Any]] = {
    "report_self": {
        "label": "Report 自检",
        "enabled": True,
        "status": "enabled",
        "required_roles": ("report",),
        "forbidden_roles": ("ptr", "record_9706_1", "record_9706_202"),
        "includes_report_self": True,
        "rule_ids": (
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
        ),
        "runner": "mvp.run_report_self:run_report_self",
    },
    "report_ptr": {
        "label": "Report + PTR 比对",
        "enabled": False,
        "status": "disabled",
        "required_roles": ("report", "ptr"),
        "forbidden_roles": ("record_9706_1", "record_9706_202"),
        "includes_report_self": False,
        "rule_ids": ("PTR-P01",),
        "runner": None,
        "disabled_reason_code": "PTR_NOT_VALIDATED",
        "disabled_message": "PTR 检查尚未启用",
    },
    "report_ptr_report": {
        "label": "PTR + Report 组合比对",
        "enabled": False,
        "status": "disabled",
        "required_roles": ("report", "ptr"),
        "forbidden_roles": ("record_9706_1", "record_9706_202"),
        "includes_report_self": True,
        "rule_ids": ("PTR-P01",),
        "runner": None,
        "disabled_reason_code": "PTR_NOT_VALIDATED",
        "disabled_message": "PTR + Report 组合模式尚未启用；PTR 证据链未完成验证",
    },
    "report_diff": {
        "label": "Report 差异模式",
        "enabled": False,
        "status": "disabled",
        # The difference-mode input contract is deliberately not guessed
        # until its two-document roles and rule plan are frozen.
        "required_roles": (),
        "forbidden_roles": ("report", "ptr", "record_9706_1", "record_9706_202"),
        "includes_report_self": False,
        "rule_ids": (),
        "runner": None,
        "disabled_reason_code": "DIFF_NOT_SPECIFIED",
        "disabled_message": "差异模式的输入角色与规则计划尚未冻结",
    },
    "report_record_9706_1": {
        "label": "Report + GB 9706.1 Record",
        "enabled": True,
        "status": "enabled",
        "required_roles": ("report", "record_9706_1"),
        "forbidden_roles": ("ptr", "record_9706_202"),
        "includes_report_self": False,
        "rule_ids": (
            "RECORD61-IDENTITY",
            "RECORD61-BODY-STATUS",
            "RECORD61-BODY-NUMERIC",
            "RECORD61-BODY-PERCENT",
            "RECORD61-SEQUENCE-CONCLUSION",
            "RECORD61-SCOPE",
            "RECORD61-STRUCTURE",
            "RECORD61-NUMERIC-DISCOVERY",
            "RECORD61-METADATA",
            "RECORD61-NONCONFORMING-ALERT",
        ),
        "runner": "mvp.run_full_records:run_full_records",
    },
    "report_record_9706_202": {
        "label": "Report + GB 9706.202 Record",
        "enabled": True,
        "status": "enabled",
        "required_roles": ("report", "record_9706_202"),
        "forbidden_roles": ("ptr", "record_9706_1"),
        "includes_report_self": False,
        "rule_ids": (
            "RECORD202-NUMBER",
            "RECORD202-BODY-STATUS",
            "RECORD202-BODY-NUMERIC",
            "RECORD202-BODY-PERCENT",
            "RECORD202-SYMBOLS",
            "RECORD202-STRUCTURE",
            "RECORD202-SCOPE",
            "RECORD202-IDENTITY",
            "RECORD202-METADATA",
            "RECORD202-NONCONFORMING",
        ),
        "runner": "mvp.run_full_records:run_full_records",
    },
}


RULE_CATALOG: dict[str, dict[str, Any]] = {
    "REPORT-R01": {
        "label": "身份字段",
        "title": "Report 身份字段一致性",
        "version": "1.0.0",
        "source": "report_baseline",
        "required_component_ids": ("pdf_parser", "report_template_bundle"),
        "family": "report",
        "status": "validated",
        "catalog_status": "enabled",
        "enabled": True,
        "modes": ("report_self",),
    },
    "REPORT-R02": {
        "label": "中文标签字段",
        "title": "样品描述字段与中文标签",
        "version": "1.0.0",
        "source": "report_photo_rules",
        "required_component_ids": ("pdf_parser", "report_photo_parser"),
        "family": "report",
        "status": "validated",
        "catalog_status": "enabled",
        "enabled": True,
        "modes": ("report_self",),
    },
    "REPORT-R03": {
        "label": "生产日期",
        "title": "生产日期值与格式",
        "version": "1.0.0",
        "source": "report_photo_rules",
        "required_component_ids": ("pdf_parser", "report_photo_parser"),
        "family": "report",
        "status": "validated",
        "catalog_status": "enabled",
        "enabled": True,
        "modes": ("report_self",),
    },
    "REPORT-R04": {
        "label": "样品描述覆盖",
        "title": "样品描述字段在照片页的覆盖",
        "version": "1.0.0",
        "source": "report_photo_rules",
        "required_component_ids": ("pdf_parser", "report_photo_parser"),
        "family": "report",
        "status": "validated",
        "catalog_status": "enabled",
        "enabled": True,
        "modes": ("report_self",),
    },
    "REPORT-R05": {
        "label": "实物照片",
        "title": "每个对象的实物照片",
        "version": "1.0.0",
        "source": "report_photo_rules",
        "required_component_ids": ("pdf_parser", "report_photo_parser"),
        "family": "report",
        "status": "validated",
        "catalog_status": "enabled",
        "enabled": True,
        "modes": ("report_self",),
    },
    "REPORT-R06": {
        "label": "中文标签照片",
        "title": "每个对象的中文标签照片",
        "version": "1.0.0",
        "source": "report_photo_rules",
        "required_component_ids": ("pdf_parser", "report_photo_parser"),
        "family": "report",
        "status": "validated",
        "catalog_status": "enabled",
        "enabled": True,
        "modes": ("report_self",),
    },
    "REPORT-R11": {
        "label": "页码",
        "title": "Report 打印页码连续性",
        "version": "1.0.0",
        "source": "report_baseline",
        "required_component_ids": ("pdf_parser", "report_template_bundle"),
        "family": "report",
        "status": "validated",
        "catalog_status": "enabled",
        "enabled": True,
        "modes": ("report_self",),
    },
    "REPORT-R09-R10": {
        "label": "序号",
        "title": "Report 序号连续与跨页续项",
        "version": "1.0.0",
        "source": "report_baseline",
        "required_component_ids": ("pdf_parser", "report_template_bundle"),
        "family": "report",
        "status": "validated",
        "catalog_status": "enabled",
        "enabled": True,
        "modes": ("report_self",),
    },
    "REPORT-R07": {
        "label": "结论",
        "title": "Report 多行结果与单项结论",
        "version": "1.0.0",
        "source": "report_baseline",
        "required_component_ids": ("pdf_parser", "report_template_bundle"),
        "family": "report",
        "status": "validated",
        "catalog_status": "enabled",
        "enabled": True,
        "modes": ("report_self",),
    },
    "REPORT-R07-B": {
        "label": "接受标准",
        "title": "实测结果与接受标准",
        "version": "1.0.0",
        "source": "report_numeric_rules",
        "required_component_ids": ("pdf_parser", "report_template_bundle"),
        "family": "report",
        "status": "validated",
        "catalog_status": "enabled",
        "enabled": True,
        "modes": ("report_self",),
    },
    "REPORT-R08": {
        "label": "完整性",
        "title": "检验项目字段漏填",
        "version": "1.0.0",
        "source": "report_baseline",
        "required_component_ids": ("pdf_parser", "report_template_bundle"),
        "family": "report",
        "status": "validated",
        "catalog_status": "enabled",
        "enabled": True,
        "modes": ("report_self",),
    },
    "REPORT-R09": {
        "label": "项目序号",
        "title": "新检验项目序号连续性",
        "version": "1.0.0",
        "source": "report_baseline",
        "required_component_ids": ("pdf_parser", "report_template_bundle"),
        "family": "report",
        "status": "validated",
        "catalog_status": "enabled",
        "enabled": True,
        "modes": ("report_self",),
    },
    "REPORT-R10": {
        "label": "跨页续项",
        "title": "跨页首项续N检查",
        "version": "1.0.0",
        "source": "report_baseline",
        "required_component_ids": ("pdf_parser", "report_template_bundle"),
        "family": "report",
        "status": "validated",
        "catalog_status": "enabled",
        "enabled": True,
        "modes": ("report_self",),
    },
    "RECORD61-IDENTITY": {
        "label": "9706.1 身份字段",
        "title": "9706.1 Record 身份字段与 Report",
        "version": "1.0.0",
        "source": "mode_specific",
        "required_component_ids": ("pdf_parser", "report_template_bundle", "record61_template_bundle"),
        "family": "record_9706_1",
        "status": "validated",
        "catalog_status": "enabled",
        "enabled": True,
        "modes": ("report_record_9706_1",),
    },
    "RECORD61-BODY-STATUS": {
        "label": "9706.1 状态行",
        "title": "9706.1 Record 正文状态与 Report 结论",
        "version": "1.0.0",
        "source": "mode_specific",
        "required_component_ids": ("pdf_parser", "report_template_bundle", "record61_template_bundle", "ink_parser"),
        "family": "record_9706_1",
        "status": "validated",
        "catalog_status": "enabled",
        "enabled": True,
        "modes": ("report_record_9706_1",),
    },
    "RECORD61-BODY-NUMERIC": {
        "label": "9706.1 数值",
        "title": "9706.1 Record 实测值、单位换算与 Report",
        "version": "1.0.0",
        "source": "mode_specific",
        "required_component_ids": ("pdf_parser", "report_template_bundle", "record61_template_bundle", "ink_parser", "ocr_apple_vision", "ocr_tesseract", "evidence_renderer"),
        "family": "record_9706_1",
        "status": "validated",
        "catalog_status": "enabled",
        "enabled": True,
        "modes": ("report_record_9706_1",),
    },
    "RECORD61-BODY-PERCENT": {
        "label": "9706.1 百分比",
        "title": "9706.1 Record 百分比与 Report",
        "version": "1.0.0",
        "source": "mode_specific",
        "required_component_ids": ("pdf_parser", "report_template_bundle", "record61_template_bundle", "ink_parser", "ocr_apple_vision", "ocr_tesseract", "evidence_renderer"),
        "family": "record_9706_1",
        "status": "validated",
        "catalog_status": "enabled",
        "enabled": True,
        "modes": ("report_record_9706_1",),
    },
    "RECORD61-SEQUENCE-CONCLUSION": {
        "label": "9706.1 序号结论",
        "title": "9706.1 Report 序号与单项结论",
        "version": "1.0.0",
        "source": "mode_specific",
        "required_component_ids": ("pdf_parser", "report_template_bundle", "record61_template_bundle"),
        "family": "record_9706_1",
        "status": "validated",
        "catalog_status": "enabled",
        "enabled": True,
        "modes": ("report_record_9706_1",),
    },
    "RECORD61-SCOPE": {
        "label": "9706.1 覆盖范围",
        "title": "9706.1 Record 与 Report 覆盖范围",
        "version": "1.0.0",
        "source": "mode_specific",
        "required_component_ids": ("pdf_parser", "report_template_bundle", "record61_template_bundle"),
        "family": "record_9706_1",
        "status": "validated",
        "catalog_status": "enabled",
        "enabled": True,
        "modes": ("report_record_9706_1",),
    },
    "RECORD202-NUMBER": {
        "label": "9706.202 报告编号",
        "title": "9706.202 Record 逐页报告编号",
        "version": "1.0.0",
        "source": "mode_specific",
        "required_component_ids": ("pdf_parser", "report_template_bundle", "record202_template_bundle"),
        "family": "record_9706_202",
        "status": "validated",
        "catalog_status": "enabled",
        "enabled": True,
        "modes": ("report_record_9706_202",),
    },
    "RECORD202-BODY-STATUS": {
        "label": "9706.202 状态符号",
        "title": "9706.202 Ink 符号与 Report 结果",
        "version": "1.0.0",
        "source": "mode_specific",
        "required_component_ids": ("pdf_parser", "report_template_bundle", "record202_template_bundle", "ink_parser"),
        "family": "record_9706_202",
        "status": "validated",
        "catalog_status": "enabled",
        "enabled": True,
        "modes": ("report_record_9706_202",),
    },
    "RECORD202-BODY-NUMERIC": {
        "label": "9706.202 数值",
        "title": "9706.202 Record 实测值、单位换算与 Report",
        "version": "1.0.0",
        "source": "mode_specific",
        "required_component_ids": ("pdf_parser", "report_template_bundle", "record202_template_bundle", "ink_parser"),
        "family": "record_9706_202",
        "status": "validated",
        "catalog_status": "enabled",
        "enabled": True,
        "modes": ("report_record_9706_202",),
    },
    "RECORD202-BODY-PERCENT": {
        "label": "9706.202 百分比",
        "title": "9706.202 百分比与 Report",
        "version": "1.0.0",
        "source": "mode_specific",
        "required_component_ids": ("pdf_parser", "report_template_bundle", "record202_template_bundle", "ink_parser"),
        "family": "record_9706_202",
        "status": "validated",
        "catalog_status": "enabled",
        "enabled": True,
        "modes": ("report_record_9706_202",),
    },
    "RECORD61-NONCONFORMING-ALERT": {
        "label": "9706.1 不符合警示",
        "title": "9706.1 Record 明确不符合警示",
        "version": "1.0.0",
        "source": "mode_specific",
        "required_component_ids": ("pdf_parser", "record61_template_bundle", "ink_parser"),
        "family": "record_9706_1",
        "status": "validated",
        "catalog_status": "enabled",
        "enabled": True,
        "modes": ("report_record_9706_1",),
    },
    "RECORD61-STRUCTURE": {
        "label": "9706.1 页码表头结构",
        "title": "9706.1 Record 页码、表头和模板结构",
        "version": "1.0.0",
        "source": "mode_specific",
        "required_component_ids": ("pdf_parser", "record61_template_bundle"),
        "family": "record_9706_1",
        "status": "validated",
        "catalog_status": "enabled",
        "enabled": True,
        "modes": ("report_record_9706_1",),
    },
    "RECORD61-NUMERIC-DISCOVERY": {
        "label": "9706.1 数值发现",
        "title": "9706.1 模板外数值目标显式发现",
        "version": "1.0.0",
        "source": "mode_specific",
        "required_component_ids": ("pdf_parser", "report_template_bundle", "record61_template_bundle"),
        "family": "record_9706_1",
        "status": "validated",
        "catalog_status": "enabled",
        "enabled": True,
        "modes": ("report_record_9706_1",),
    },
    "RECORD61-METADATA": {
        "label": "9706.1 元数据",
        "title": "9706.1 Record/Report 日期、仪器、人员、签字和备注",
        "version": "1.0.0",
        "source": "mode_specific",
        "required_component_ids": ("pdf_parser", "report_template_bundle", "record61_template_bundle"),
        "family": "record_9706_1",
        "status": "validated",
        "catalog_status": "enabled",
        "enabled": True,
        "modes": ("report_record_9706_1",),
    },
    "RECORD202-SYMBOLS": {
        "label": "9706.202 图例",
        "title": "9706.202 四种状态符号图例核验",
        "version": "1.0.0",
        "source": "mode_specific",
        "required_component_ids": ("pdf_parser", "record202_template_bundle", "ink_parser"),
        "family": "record_9706_202",
        "status": "validated",
        "catalog_status": "enabled",
        "enabled": True,
        "modes": ("report_record_9706_202",),
    },
    "RECORD202-STRUCTURE": {
        "label": "9706.202 页码表头结构",
        "title": "9706.202 Record/Report 页码、表头和模板结构",
        "version": "1.0.0",
        "source": "mode_specific",
        "required_component_ids": ("pdf_parser", "record202_template_bundle", "report_template_bundle"),
        "family": "record_9706_202",
        "status": "validated",
        "catalog_status": "enabled",
        "enabled": True,
        "modes": ("report_record_9706_202",),
    },
    "RECORD202-SCOPE": {
        "label": "9706.202 字段映射",
        "title": "9706.202 项目、条款、要求和出现次序字段比较",
        "version": "1.0.0",
        "source": "mode_specific",
        "required_component_ids": ("pdf_parser", "report_template_bundle", "record202_template_bundle"),
        "family": "record_9706_202",
        "status": "validated",
        "catalog_status": "enabled",
        "enabled": True,
        "modes": ("report_record_9706_202",),
    },
    "RECORD202-IDENTITY": {
        "label": "9706.202 身份字段",
        "title": "9706.202 固定身份和产品字段与 Report",
        "version": "1.0.0",
        "source": "mode_specific",
        "required_component_ids": ("pdf_parser", "report_template_bundle", "record202_template_bundle"),
        "family": "record_9706_202",
        "status": "validated",
        "catalog_status": "enabled",
        "enabled": True,
        "modes": ("report_record_9706_202",),
    },
    "RECORD202-METADATA": {
        "label": "9706.202 元数据",
        "title": "9706.202 日期、仪器、人员、签字和备注",
        "version": "1.0.0",
        "source": "mode_specific",
        "required_component_ids": ("pdf_parser", "report_template_bundle", "record202_template_bundle"),
        "family": "record_9706_202",
        "status": "validated",
        "catalog_status": "enabled",
        "enabled": True,
        "modes": ("report_record_9706_202",),
    },
    "RECORD202-NONCONFORMING": {
        "label": "9706.202 不符合警示",
        "title": "9706.202 Record 明确不符合警示",
        "version": "1.0.0",
        "source": "mode_specific",
        "required_component_ids": ("pdf_parser", "record202_template_bundle", "ink_parser"),
        "family": "record_9706_202",
        "status": "validated",
        "catalog_status": "enabled",
        "enabled": True,
        "modes": ("report_record_9706_202",),
    },
    "PTR-P01": {
        "label": "PTR 声明范围与正文覆盖",
        "title": "PTR 声明范围与正文覆盖",
        "version": None,
        "source": "mode_specific",
        "required_component_ids": (),
        "family": "ptr",
        "status": "disabled",
        "catalog_status": "disabled",
        "enabled": False,
        "modes": ("report_ptr", "report_ptr_report"),
        "disabled_reason_code": "PTR_NOT_VALIDATED",
    },
}


def _mode_payload(mode_id: str, mode: dict[str, Any]) -> dict[str, Any]:
    payload = dict(mode)
    payload["id"] = mode_id
    for field in ("required_roles", "forbidden_roles", "rule_ids"):
        payload[field] = list(payload[field])
    return payload


def _rule_payload(rule_id: str, rule: dict[str, Any]) -> dict[str, Any]:
    payload = dict(rule)
    payload["id"] = rule_id
    payload["modes"] = list(payload["modes"])
    payload["required_component_ids"] = list(payload.get("required_component_ids", ()))
    payload.pop("status", None)
    payload.setdefault("disabled_reason_code", None)
    return payload


def capabilities_payload() -> dict[str, Any]:
    """Return a JSON-safe snapshot of the mode catalog."""

    return {
        "schema_version": "1.0.0",
        "api_version": "v1",
        "modes": {
            mode_id: {
                **_mode_payload(mode_id, mode),
                "includes_report_baseline": bool(mode["includes_report_self"]),
            }
            for mode_id, mode in MODE_CATALOG.items()
        },
        "document_roles": list(DOCUMENT_ROLES),
        "uploads": {
            "accepted_media_types": ["application/pdf"],
            "max_file_bytes": 524288000,
            "max_pages": 2000,
            "encrypted_pdf_supported": False,
        },
        "page_render": {
            "min_scale": 0.5,
            "max_scale": 3.0,
            "formats": ["png", "webp"],
        },
        "max_concurrent_runs": 1,
    }


def rules_payload() -> dict[str, Any]:
    """Return a JSON-safe snapshot of the rule catalog."""

    return {
        "schema_version": "1.0.0",
        "api_version": "v1",
        "rule_bundle": {
            "id": RULE_BUNDLE_ID,
            "sha256": rule_bundle_sha256(),
        },
        "rules": [_rule_payload(rule_id, rule) for rule_id, rule in RULE_CATALOG.items()],
    }


def rule_bundle_sha256() -> str:
    """Hash the stable rule identity/config fields used by future preflight."""

    canonical = []
    for rule_id, rule in RULE_CATALOG.items():
        canonical.append(
            {
                "id": rule_id,
                "version": rule.get("version"),
                "modes": list(rule["modes"]),
                "catalog_status": rule.get("catalog_status"),
                "required_component_ids": list(rule.get("required_component_ids", ())),
                "disabled_reason_code": rule.get("disabled_reason_code"),
            }
        )
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def preflight_plan_hash(
    mode: str,
    inputs: dict[str, Any],
    input_snapshot: dict[str, Any] | None = None,
) -> str:
    """Create the deterministic plan hash used by the local state API."""

    if mode not in MODE_CATALOG:
        raise KeyError(mode)
    canonical = {
        "mode": mode,
        "inputs": inputs,
        "rule_bundle_id": RULE_BUNDLE_ID,
        "rule_bundle_sha256": rule_bundle_sha256(),
        "planned_rule_ids": list(MODE_CATALOG[mode]["rule_ids"]),
        "input_snapshot": input_snapshot or {},
    }
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def catalog_snapshot() -> dict[str, Any]:
    """Return both catalogs for callers that need one atomic snapshot."""

    return deepcopy({"capabilities": capabilities_payload(), "rules": rules_payload()})
