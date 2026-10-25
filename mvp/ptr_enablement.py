"""Explicit business-signoff verification for the disabled PTR capability.

The verifier is intentionally read-only.  It checks that a signed approval is
bound to the exact acceptance-gate JSON and current draft rule version, then
returns a decision that a separately authorized deployment step may consume.
It never mutates :mod:`mvp.capabilities` and never enables ``report_ptr`` by
itself.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from mvp.capabilities import validate_mode
from mvp.ptr_acceptance import DEFAULT_TRIAL_SAMPLES
from mvp.ptr_compare import PTR_COMPARE_RULE_ID, PTR_COMPARE_RULE_VERSION


PTR_SIGNOFF_SCHEMA_VERSION = "ptr-business-signoff-0.1"
PTR_ENABLEMENT_SCHEMA_VERSION = "ptr-enablement-0.1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return None, str(error)
    if not isinstance(value, dict):
        return None, "JSON 顶层必须是对象"
    return value, None


def verify_ptr_enablement(
    *,
    acceptance_gate_path: str | Path,
    signoff_path: str | Path,
) -> dict[str, Any]:
    """Verify a business signoff without changing the capability catalog."""

    gate_path = Path(acceptance_gate_path).expanduser().resolve()
    approval_path = Path(signoff_path).expanduser().resolve()
    checks: list[dict[str, Any]] = []

    gate, gate_error = _load_json(gate_path)
    gate_hash = _sha256(gate_path) if gate_path.is_file() else None
    if gate_error or gate is None:
        checks.append({"id": "acceptance_gate_readable", "passed": False, "detail": gate_error or "缺少验收门文件"})
        gate = {}
    else:
        checks.append({"id": "acceptance_gate_readable", "passed": True})

    signoff, signoff_error = _load_json(approval_path)
    if signoff_error or signoff is None:
        checks.append({"id": "business_signoff_readable", "passed": False, "detail": signoff_error or "缺少业务签字文件"})
        signoff = {}
    else:
        checks.append({"id": "business_signoff_readable", "passed": True})

    checks.extend(
        [
            {
                "id": "acceptance_gate_ready",
                "passed": gate.get("status") == "ready_for_signoff"
                and gate.get("can_enable_report_ptr") is False,
                "detail": gate.get("status"),
            },
            {
                "id": "catalog_still_disabled",
                "passed": (
                    (gate.get("capability_gate") or {}).get("enabled") is False
                    and (gate.get("capability_gate") or {}).get("disabled_reason_code") == "PTR_NOT_VALIDATED"
                ),
            },
            {
                "id": "all_trial_samples_ready",
                "passed": (
                    {str(item.get("sample")) for item in gate.get("samples", []) if item.get("ready")}
                    == set(DEFAULT_TRIAL_SAMPLES)
                ),
            },
            {
                "id": "signoff_schema",
                "passed": signoff.get("schema_version") == PTR_SIGNOFF_SCHEMA_VERSION,
            },
            {
                "id": "signoff_mode",
                "passed": signoff.get("mode") == "report_ptr",
            },
            {
                "id": "signoff_approved",
                "passed": signoff.get("approved") is True and bool(str(signoff.get("reviewer") or "").strip()),
            },
            {
                "id": "signoff_rule_version",
                "passed": (
                    (signoff.get("rule") or {}).get("id") == PTR_COMPARE_RULE_ID
                    and (signoff.get("rule") or {}).get("version") == PTR_COMPARE_RULE_VERSION
                ),
            },
            {
                "id": "acceptance_gate_hash_bound",
                "passed": bool(gate_hash) and signoff.get("acceptance_gate_sha256") == gate_hash,
            },
        ]
    )
    approved_samples = signoff.get("samples") if isinstance(signoff.get("samples"), list) else []
    sample_ids = {str(item.get("sample")) for item in approved_samples if isinstance(item, Mapping)}
    checks.append(
        {
            "id": "sample_signoffs_complete",
            "passed": sample_ids == set(DEFAULT_TRIAL_SAMPLES)
            and all(item.get("approved") is True for item in approved_samples if isinstance(item, Mapping)),
        }
    )
    checks.append(
        {
            "id": "signoff_timestamp",
            "passed": bool(str(signoff.get("signed_at") or "").strip()),
        }
    )

    passed = all(bool(item.get("passed")) for item in checks)
    mode_decision = validate_mode("report_ptr", operation="enablement_verification")
    return {
        "schema_version": PTR_ENABLEMENT_SCHEMA_VERSION,
        "mode": "report_ptr",
        "status": "approved_for_enablement" if passed else "blocked",
        "can_enable_report_ptr": passed,
        "reason_code": None if passed else "BUSINESS_SIGNOFF_REQUIRED",
        "acceptance_gate_path": str(gate_path),
        "acceptance_gate_sha256": gate_hash,
        "signoff_path": str(approval_path),
        "current_catalog": {
            "enabled": bool((mode_decision.get("capability") or {}).get("enabled")),
            "status": (mode_decision.get("capability") or {}).get("status"),
            "disabled_reason_code": (mode_decision.get("details") or {}).get("disabled_reason_code"),
        },
        "checks": checks,
        "mutation": "none",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="验证 PTR 业务签字，不修改能力开关")
    parser.add_argument("--gate", required=True, type=Path)
    parser.add_argument("--signoff", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    result = verify_ptr_enablement(acceptance_gate_path=args.gate, signoff_path=args.signoff)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["can_enable_report_ptr"] else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "PTR_ENABLEMENT_SCHEMA_VERSION",
    "PTR_SIGNOFF_SCHEMA_VERSION",
    "verify_ptr_enablement",
]
