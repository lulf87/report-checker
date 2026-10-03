"""父进程与 PDF Worker 之间的最小 JSON 协议。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from mvp.capabilities import validate_mode


JOB_SCHEMA_VERSION = "worker-job-1.0"
RESULT_SCHEMA_VERSION = "worker-result-1.0"


class WorkerProtocolError(ValueError):
    pass


@dataclass(frozen=True)
class JobSpec:
    run_id: str
    mode: str
    report_path: Path
    output_dir: Path
    record_path: Path | None = None
    preflight_plan_hash: str | None = None
    rule_bundle_id: str | None = None

    def __post_init__(self) -> None:
        if not self.run_id.strip():
            raise WorkerProtocolError("run_id 必须是非空字符串")
        decision = validate_mode(self.mode, operation="worker_job")
        if not decision["enabled"]:
            raise WorkerProtocolError(f"{decision['code']}: {decision['message']}")
        if not self.report_path.is_absolute() or not self.output_dir.is_absolute():
            raise WorkerProtocolError("Worker JobSpec 的路径必须是绝对路径")
        if self.mode != "report_self" and self.record_path is None:
            raise WorkerProtocolError("Record 模式必须提供 record_path")
        if self.mode == "report_self" and self.record_path is not None:
            raise WorkerProtocolError("Report 自检不能附带 record_path")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": JOB_SCHEMA_VERSION,
            "run_id": self.run_id,
            "mode": self.mode,
            "report_path": str(self.report_path),
            "record_path": str(self.record_path) if self.record_path is not None else None,
            "output_dir": str(self.output_dir),
            "preflight_plan_hash": self.preflight_plan_hash,
            "rule_bundle_id": self.rule_bundle_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "JobSpec":
        if payload.get("schema_version") != JOB_SCHEMA_VERSION:
            raise WorkerProtocolError("Worker JobSpec schema_version 无效")
        try:
            report_path = Path(str(payload["report_path"])).expanduser().resolve()
            output_dir = Path(str(payload["output_dir"])).expanduser().resolve()
            record_raw = payload.get("record_path")
            record_path = Path(str(record_raw)).expanduser().resolve() if record_raw else None
            return cls(
                run_id=str(payload["run_id"]),
                mode=str(payload["mode"]),
                report_path=report_path,
                record_path=record_path,
                output_dir=output_dir,
                preflight_plan_hash=payload.get("preflight_plan_hash"),
                rule_bundle_id=payload.get("rule_bundle_id"),
            )
        except KeyError as exc:
            raise WorkerProtocolError(f"Worker JobSpec 缺少字段: {exc.args[0]}") from exc


def worker_result_payload(job: JobSpec, result: Mapping[str, Any]) -> dict[str, Any]:
    result_path = job.output_dir / "result.json"
    if not result_path.is_file():
        raise WorkerProtocolError("Worker 未生成 result.json")
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "run_id": job.run_id,
        "mode": job.mode,
        "result_path": str(result_path),
        "machine_overall_status": result.get("machine_overall_status"),
        "status_counts": result.get("status_counts"),
    }


def validate_worker_result(payload: Mapping[str, Any], job: JobSpec) -> dict[str, Any]:
    if payload.get("schema_version") != RESULT_SCHEMA_VERSION:
        raise WorkerProtocolError("WorkerResult schema_version 无效")
    if payload.get("run_id") != job.run_id or payload.get("mode") != job.mode:
        raise WorkerProtocolError("WorkerResult 的 run_id 或 mode 与 JobSpec 不一致")
    result_path = Path(str(payload.get("result_path", ""))).expanduser().resolve()
    expected_path = (job.output_dir / "result.json").resolve()
    if result_path != expected_path:
        raise WorkerProtocolError("WorkerResult result_path 必须指向 JobSpec output_dir/result.json")
    if not result_path.is_file():
        raise WorkerProtocolError("WorkerResult 指向的 result.json 不存在")
    return dict(payload)
