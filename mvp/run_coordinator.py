"""把持久化 Run 交给独立 Worker 并通过发布闸门回写状态。"""

from __future__ import annotations

import json
import hashlib
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from mvp.run_store import RunStore, RunStoreError
from mvp.worker_protocol import JobSpec, WorkerProtocolError, validate_worker_result


class CoordinatorError(RuntimeError):
    pass


def _fail_running_run(store: RunStore, run_id: str, code: str, message: str) -> None:
    try:
        for execution in store.get_rule_executions(run_id):
            if execution["execution_state"] in {"pending", "running"}:
                store.transition_rule_execution(
                    run_id,
                    execution["rule_id"],
                    "failed",
                    reason_code=code,
                    reason_detail={"message": message},
                )
        if store.get_run(run_id)["lifecycle_status"] == "running":
            store.transition_run(run_id, "failed", reason_code=code, reason_message=message)
    except RunStoreError:
        # Preserve the original worker/publish failure. The stored state is
        # still inspected by the caller and startup recovery handles leftovers.
        return


def _verify_input_snapshot(run: dict[str, Any], report: Path, record: Path | None) -> None:
    snapshot = run.get("input_snapshot") or {}
    if not snapshot or any(item.get("unresolved") for item in snapshot.values() if isinstance(item, dict)):
        return
    paths = {"report": report}
    if record is not None:
        if "record_9706_1" in snapshot:
            paths["record_9706_1"] = record
        if "record_9706_202" in snapshot:
            paths["record_9706_202"] = record
    for role, item in snapshot.items():
        if role not in paths or not isinstance(item, dict):
            raise RunStoreError("RUN_INPUT_SNAPSHOT_INVALID", "Run 输入快照缺少可执行文件引用")
        expected_sha = item.get("blob_sha256")
        if not isinstance(expected_sha, str) or len(expected_sha) != 64:
            raise RunStoreError("RUN_INPUT_SNAPSHOT_INVALID", "Run 输入快照缺少 Blob SHA-256")
        digest = hashlib.sha256()
        try:
            with paths[role].open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as exc:
            raise RunStoreError("RUN_INPUT_CONTENT_UNAVAILABLE", "Run 输入文件不可读取") from exc
        if digest.hexdigest() != expected_sha:
            raise RunStoreError(
                "RUN_INPUT_HASH_MISMATCH",
                "Run 输入文件哈希与创建时快照不一致",
                details={"role": role},
            )


def execute_persisted_run(
    store: RunStore,
    run_id: str,
    *,
    report_path: str | Path,
    output_dir: str | Path,
    record_path: str | Path | None = None,
    timeout_seconds: int = 3600,
    python_executable: str | None = None,
) -> dict[str, Any]:
    """Run one queued job in a child process and publish its validated summary."""

    run = store.get_run(run_id)
    try:
        store.require_mode_enabled(run["mode"], operation="coordinator")
    except RunStoreError as error:
        if run["lifecycle_status"] == "queued":
            store.transition_run(run_id, "failed", reason_code=error.code, reason_message=error.message)
        elif run["lifecycle_status"] == "running":
            _fail_running_run(store, run_id, error.code, error.message)
        raise CoordinatorError(error.message) from error
    if run["lifecycle_status"] != "queued":
        raise CoordinatorError(f"Run {run_id} is not queued")
    report = Path(report_path).expanduser().resolve()
    output = Path(output_dir).expanduser().resolve()
    record = Path(record_path).expanduser().resolve() if record_path is not None else None
    try:
        _verify_input_snapshot(run, report, record)
    except RunStoreError as error:
        # A queued Run needs one explicit lifecycle transition before the
        # stable input failure can be recorded.
        store.transition_run(run_id, "running")
        _fail_running_run(store, run_id, error.code, error.message)
        raise CoordinatorError(error.message) from error
    job = JobSpec(
        run_id=run_id,
        mode=run["mode"],
        report_path=report,
        record_path=record,
        output_dir=output,
        preflight_plan_hash=run["preflight_plan_hash"],
        rule_bundle_id=run["rule_bundle_id"],
    )
    store.transition_run(run_id, "running")
    for execution in store.get_rule_executions(run_id):
        store.transition_rule_execution(run_id, execution["rule_id"], "running")

    job_file: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8", delete=False) as handle:
            job_file = Path(handle.name)
            json.dump(job.as_dict(), handle, ensure_ascii=False, separators=(",", ":"))
        command = [python_executable or sys.executable, "-m", "mvp.run_worker", "--job", str(job_file)]
        completed = subprocess.run(
            command,
            cwd=str(Path(__file__).resolve().parents[1]),
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
        if completed.returncode != 0:
            message = (completed.stderr or completed.stdout or "Worker 返回非零退出码").strip()
            _fail_running_run(store, run_id, "WORKER_FAILED", message[:1000])
            raise CoordinatorError(message[:1000])
        try:
            worker_payload = json.loads(completed.stdout.strip())
            validate_worker_result(worker_payload, job)
            result_path = Path(worker_payload["result_path"])
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, WorkerProtocolError) as exc:
            _fail_running_run(store, run_id, "WORKER_PROTOCOL_INVALID", str(exc))
            raise CoordinatorError(str(exc)) from exc

        if result.get("mode") != run["mode"]:
            _fail_running_run(store, run_id, "WORKER_RESULT_MODE_MISMATCH", "Worker result mode 与 Run 不一致")
            raise CoordinatorError("Worker result mode 与 Run 不一致")
        findings = result.get("findings", [])
        planned_rule_ids = set(run["planned_rule_ids"])
        finding_counts = {rule_id: 0 for rule_id in planned_rule_ids}
        for finding in findings:
            rule_id = str(finding.get("rule_id") or finding.get("id") or "")
            if rule_id not in planned_rule_ids:
                _fail_running_run(store, run_id, "WORKER_RESULT_RULE_OUT_OF_PLAN", rule_id)
                raise CoordinatorError(f"Worker result rule outside plan: {rule_id}")
            finding_counts[rule_id] += 1
        # A cancel request may arrive while the child process is finishing.
        # Never publish a completed payload after the caller has won that CAS
        # race; close the Run as cancelled instead.
        current = store.get_run(run_id)
        if current["lifecycle_status"] == "cancel_requested":
            store.transition_run(
                run_id,
                "cancelled",
                reason_code="USER_CANCELLED",
                reason_message="Worker 完成前收到取消请求",
            )
            return store.get_run(run_id)
        if current["lifecycle_status"] != "running":
            raise CoordinatorError(f"Run {run_id} is no longer publishable")
        try:
            for rule_id, count in finding_counts.items():
                store.transition_rule_execution(run_id, rule_id, "succeeded", finding_count=count)
        except RunStoreError:
            current = store.get_run(run_id)
            if current["lifecycle_status"] == "cancel_requested":
                store.transition_run(
                    run_id,
                    "cancelled",
                    reason_code="USER_CANCELLED",
                    reason_message="Worker 完成前收到取消请求",
                )
                return store.get_run(run_id)
            raise
        return store.publish_result(run_id, result)
    except subprocess.TimeoutExpired as exc:
        _fail_running_run(store, run_id, "WORKER_TIMEOUT", str(exc))
        raise CoordinatorError("Worker 执行超时") from exc
    except (OSError, RunStoreError) as exc:
        if isinstance(exc, RunStoreError) and store.get_run(run_id)["lifecycle_status"] == "failed":
            raise
        _fail_running_run(store, run_id, "COORDINATOR_FAILED", str(exc))
        raise
    finally:
        if job_file is not None:
            try:
                job_file.unlink()
            except FileNotFoundError:
                pass
