from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from mvp.capabilities import MODE_CATALOG, validate_mode
from mvp.run_full_records import run_full_records
from mvp.run_ptr_report import run_ptr_report
from mvp.run_report_self import run_report_self


# Backwards-compatible public name. The capability server and dispatcher now
# read the same catalog instead of maintaining separate mode definitions.
MODE_CAPABILITIES = MODE_CATALOG


class ModeDisabledError(RuntimeError):
    code = "MODE_DISABLED"

    def __init__(self, mode: str, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.mode = mode
        self.reason_code = reason_code
        self.message = message

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": {
                "mode": self.mode,
                "disabled_reason_code": self.reason_code,
            },
            "retryable": False,
        }


def run_mode(
    *,
    mode: str,
    report_path: str | Path,
    output_dir: str | Path,
    record_path: str | Path | None = None,
    ptr_path: str | Path | None = None,
    report_scope: Mapping[str, Any] | str | None = None,
) -> dict[str, Any]:
    decision = validate_mode(mode, operation="runner")
    if not decision["enabled"]:
        if decision["code"] == "MODE_DISABLED":
            capability = decision["capability"] or {}
            raise ModeDisabledError(
                mode,
                str(capability.get("disabled_reason_code")),
                str(decision["message"]),
            )
        raise ValueError(str(decision["message"]))
    capability = decision["capability"]
    if not capability["enabled"]:
        raise ModeDisabledError(
            mode,
            str(capability["disabled_reason_code"]),
            str(capability.get("disabled_message", "该模式尚未启用")),
        )
    if mode == "report_self":
        if record_path is not None or ptr_path is not None:
            raise ValueError("report_self accepts only a Report input")
        return run_report_self(report_path=report_path, output_dir=output_dir)
    required_roles = tuple(capability["required_roles"])
    if "ptr" in required_roles:
        if ptr_path is None:
            raise ValueError(f"{mode} requires its PTR input")
        if record_path is not None:
            raise ValueError(f"{mode} accepts PTR, not a Record input")
        # The capability is intentionally disabled today. Keep the explicit
        # branch so a future runner cannot accidentally consume a Record path
        # under the PTR contract.
        runner = capability.get("runner")
        if not runner:
            raise ModeDisabledError(
                mode,
                str(capability.get("disabled_reason_code")),
                str(capability.get("disabled_message", "该模式尚未启用")),
            )
        if mode == "report_ptr":
            return run_ptr_report(
                report_path=report_path,
                ptr_path=ptr_path,
                output_dir=output_dir,
                allow_formal_publish=True,
            )
        raise ValueError(f"{mode} runner contract is not registered")
    if record_path is None:
        raise ValueError(f"{mode} requires its Record input")
    if ptr_path is not None:
        raise ValueError(f"{mode} accepts a Record input, not PTR")
    return run_full_records(
        mode=mode,
        report_path=report_path,
        record_path=record_path,
        output_dir=output_dir,
        report_scope=report_scope,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one of the registered Report checking mode boundaries.")
    parser.add_argument("--mode", required=True, choices=tuple(MODE_CAPABILITIES))
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--record", type=Path)
    parser.add_argument("--ptr", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run_mode(
            mode=args.mode,
            report_path=args.report,
            record_path=args.record,
            ptr_path=args.ptr,
            output_dir=args.output,
        )
    except ModeDisabledError as exc:
        print(json.dumps({"error": exc.as_dict()}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
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
