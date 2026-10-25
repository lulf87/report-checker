"""Safety gate for PTR trial review packages.

This module verifies that a reviewed package covers the required trial samples
and that the source PDFs still have the hashes captured by the machine run. It
returns ``ready_for_signoff`` only; it never enables ``report_ptr`` or creates
a formal Run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from mvp.capabilities import validate_mode
from mvp.ptr_compare import PTR_COMPARE_RULE_ID, PTR_COMPARE_RULE_VERSION


PTR_ACCEPTANCE_SCHEMA_VERSION = "ptr-acceptance-gate-0.1"
DEFAULT_TRIAL_SAMPLES = ("2948", "0775", "1539")
PTR_LIFECYCLE_OPERATIONS = (
    "preflight",
    "create_run",
    "worker_job",
    "coordinator",
    "publish_result",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ptr_lifecycle_gate_diagnostics() -> dict[str, Any]:
    """Describe the formal PTR gate at every Run lifecycle boundary.

    A complete trial review is evidence for business sign-off.  It is not a
    capability switch.  Keeping the operation checks in the acceptance
    report makes that distinction visible and gives regression tests one
    stable diagnostic to assert against.
    """

    operation_checks: dict[str, dict[str, Any]] = {}
    capability: dict[str, Any] | None = None
    for operation in PTR_LIFECYCLE_OPERATIONS:
        decision = validate_mode("report_ptr", operation=operation)
        capability = decision.get("capability") or capability
        details = decision.get("details") or {}
        allowed = bool(decision.get("enabled"))
        operation_checks[operation] = {
            "allowed": allowed,
            "code": decision.get("code"),
            "status": decision.get("status"),
            "disabled_reason_code": details.get("disabled_reason_code"),
            "effect": (
                "plan_ready" if operation == "preflight" else "accepted"
            ) if allowed else (
                "blocked_plan" if operation == "preflight" else "rejected"
            ),
        }
    capability = capability or {}
    return {
        "mode": "report_ptr",
        "catalog_status": capability.get("status"),
        "enabled": bool(capability.get("enabled")),
        "disabled_reason_code": capability.get("disabled_reason_code"),
        "operations": operation_checks,
    }


def audit_review_batch(
    *,
    root: str | Path,
    batch_dir: str | Path,
    samples: Iterable[str] = DEFAULT_TRIAL_SAMPLES,
) -> dict[str, Any]:
    root_path = Path(root).expanduser().resolve()
    batch_path = Path(batch_dir).expanduser().resolve()
    sample_names = list(samples)
    results: list[dict[str, Any]] = []
    all_ready = True
    for sample in sample_names:
        reviewed_path = batch_path / sample / "reviewed.json"
        item: dict[str, Any] = {"sample": sample, "reviewed_path": str(reviewed_path), "ready": False}
        if not reviewed_path.is_file():
            item["reason_code"] = "REVIEWED_PACKAGE_MISSING"
            all_ready = False
            results.append(item)
            continue
        try:
            reviewed = json.loads(reviewed_path.read_text(encoding="utf-8"))
            inputs = reviewed.get("machine_inputs") or {}
            actions = (reviewed.get("review") or {}).get("actions") or []
            complete = bool((reviewed.get("review") or {}).get("review_complete"))
            package_policy_ok = reviewed.get("publishable") is False
            machine_rule = reviewed.get("machine_rule") or {}
            rule_version_ok = (
                machine_rule.get("id") == PTR_COMPARE_RULE_ID
                and machine_rule.get("version") == PTR_COMPARE_RULE_VERSION
            )
            machine_summary = reviewed.get("machine_summary") or {}
            machine_coverage = reviewed.get("machine_coverage_audit") or {}
            machine_coverage_ok = (
                machine_summary.get("conserved") is True
                and machine_coverage.get("status") == "conserved"
            )
            source_ok = True
            source_checks = []
            for role in ("report", "ptr"):
                source = inputs.get(role) or {}
                source_path = Path(str(source.get("path") or "")).expanduser()
                if not source_path.is_absolute():
                    source_path = root_path / source_path
                exists = source_path.is_file()
                actual = _sha256(source_path) if exists else None
                matches = exists and actual == source.get("source_sha256")
                source_checks.append({"role": role, "path": str(source_path), "matches": matches})
                source_ok = source_ok and matches
            action_keys = {str(action.get("ledger_id")) for action in actions}
            ledger = reviewed.get("machine_ledger") or []
            coverage_ok = len(actions) == len(ledger) and len(action_keys) == len(ledger) and all(
                str(entry.get("id")) in action_keys for entry in ledger
            )
            ready = (
                complete
                and source_ok
                and coverage_ok
                and package_policy_ok
                and rule_version_ok
                and machine_coverage_ok
            )
            if ready:
                reason_code = None
            elif not rule_version_ok:
                reason_code = "REVIEW_RULE_VERSION_MISMATCH"
            elif not machine_coverage_ok:
                reason_code = "MACHINE_COVERAGE_INCOMPLETE"
            elif not package_policy_ok:
                reason_code = "REVIEWED_PACKAGE_PUBLISHABLE"
            elif not coverage_ok:
                reason_code = "REVIEW_ACTION_COVERAGE_INCOMPLETE"
            else:
                reason_code = "REVIEW_GATE_INCOMPLETE"
            item.update(
                {
                    "ready": ready,
                    "review_complete": complete,
                    "package_policy_ok": package_policy_ok,
                    "rule_version_ok": rule_version_ok,
                    "machine_rule": machine_rule,
                    "machine_coverage_ok": machine_coverage_ok,
                    "source_checks": source_checks,
                    "action_count": len(actions),
                    "ledger_count": len(ledger),
                    "coverage_ok": coverage_ok,
                    "reason_code": reason_code,
                }
            )
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
            item["reason_code"] = "REVIEWED_PACKAGE_INVALID"
            item["error"] = str(error)
        all_ready = all_ready and bool(item.get("ready"))
        results.append(item)
    capability_gate = ptr_lifecycle_gate_diagnostics()
    capability_gate["next_action"] = (
        "COMPLETE_TRIAL_REVIEW"
        if not all_ready
        else "FORMAL_CAPABILITY_ENABLED"
        if capability_gate.get("enabled")
        else "BUSINESS_SIGNOFF_REQUIRED"
    )
    return {
        "schema_version": PTR_ACCEPTANCE_SCHEMA_VERSION,
        "status": "ready_for_signoff" if all_ready else "blocked",
        "can_enable_report_ptr": False,
        "capability_currently_enabled": bool(capability_gate.get("enabled")),
        "reason_code": "PTR_NOT_VALIDATED",
        "capability_gate": capability_gate,
        "samples": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="审计 PTR 试点人工验收包，不启用正式能力")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--batch", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    result = audit_review_batch(root=args.root, batch_dir=args.batch)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DEFAULT_TRIAL_SAMPLES",
    "PTR_ACCEPTANCE_SCHEMA_VERSION",
    "PTR_LIFECYCLE_OPERATIONS",
    "audit_review_batch",
    "ptr_lifecycle_gate_diagnostics",
]
