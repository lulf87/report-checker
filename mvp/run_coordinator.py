"""把持久化 Run 交给独立 Worker 并通过发布闸门回写状态。"""

from __future__ import annotations

import json
import hashlib
import mimetypes
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping

from mvp.run_store import RunStore, RunStoreError
from mvp.worker_protocol import JobSpec, WorkerProtocolError, validate_worker_result


class CoordinatorError(RuntimeError):
    pass


def _rule_execution_outcomes(
    result: Mapping[str, Any],
    finding_counts: Mapping[str, int],
) -> dict[str, dict[str, Any]]:
    """Validate explicit non-executable rules before trusting Worker states."""

    declared = result.get("rule_execution_states")
    if declared is None:
        return {rule_id: {"state": "succeeded"} for rule_id in finding_counts}
    if not isinstance(declared, Mapping) or set(declared) != set(finding_counts):
        raise WorkerProtocolError("Worker rule_execution_states must exactly match the Run rule plan")
    outcomes: dict[str, dict[str, Any]] = {}
    for rule_id, count in finding_counts.items():
        item = declared[rule_id]
        if not isinstance(item, Mapping):
            raise WorkerProtocolError(f"Worker execution outcome is invalid: {rule_id}")
        state = item.get("state")
        if state not in {"succeeded", "not_applicable", "unsupported"}:
            raise WorkerProtocolError(f"Worker execution state is invalid: {rule_id}")
        reason_code = item.get("reason_code")
        reason_detail = item.get("reason_detail")
        if state in {"not_applicable", "unsupported"}:
            if count != 0 or not isinstance(reason_code, str) or not reason_code.strip():
                raise WorkerProtocolError(f"Worker non-executable rule requires zero Findings and a reason: {rule_id}")
            if reason_detail is not None and not isinstance(reason_detail, Mapping):
                raise WorkerProtocolError(f"Worker rule reason_detail is invalid: {rule_id}")
        elif count == 0:
            raise WorkerProtocolError(f"Worker succeeded rule has no Finding: {rule_id}")
        outcomes[rule_id] = {
            "state": state,
            "reason_code": reason_code,
            "reason_detail": reason_detail,
        }
    return outcomes


def _publish_worker_artifact_manifest(
    result: dict[str, Any],
    output_dir: Path,
    *,
    run_id: str,
) -> dict[str, Any]:
    """Validate worker-produced derivatives and atomically write their manifest.

    Evidence images are part of the published result contract.  A JSON result
    that points at a missing, absolute, or escaping path is rejected before it
    can reach SQLite.  The manifest is written with fsync + replace so readers
    never observe a partially written artifact index.
    """

    output_root = output_dir.resolve()
    if not output_root.is_dir():
        raise CoordinatorError("Worker output directory does not exist")
    entries: dict[str, dict[str, Any]] = {}

    def register(raw_path: Any, *, kind: str) -> None:
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise CoordinatorError(f"Worker {kind} artifact path is invalid")
        candidate = Path(raw_path)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise CoordinatorError(f"Worker {kind} artifact path must be relative")
        resolved = (output_root / candidate).resolve()
        if resolved != output_root and output_root not in resolved.parents:
            raise CoordinatorError(f"Worker {kind} artifact path escapes output directory")
        if not resolved.is_file():
            raise CoordinatorError(f"Worker {kind} artifact is missing: {candidate}")
        relative = resolved.relative_to(output_root).as_posix()
        if relative in entries:
            return
        digest = hashlib.sha256()
        with resolved.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        entries[relative] = {
            "path": relative,
            "kind": kind,
            "sha256": digest.hexdigest(),
            "size_bytes": resolved.stat().st_size,
            "media_type": mimetypes.guess_type(relative)[0] or "application/octet-stream",
        }

    for finding in result.get("findings", []):
        for evidence in finding.get("evidence", []) if isinstance(finding, dict) else []:
            if not isinstance(evidence, dict):
                raise CoordinatorError("Worker evidence artifact is invalid")
            if "image" in evidence:
                register(evidence["image"], kind="evidence")
            elif "path" in evidence:
                register(evidence["path"], kind="evidence")
    artifacts = result.get("artifacts", {})
    if isinstance(artifacts, dict):
        def walk_artifacts(value: Any) -> None:
            if isinstance(value, dict):
                if "path" in value:
                    register(value["path"], kind="artifact")
                for child in value.values():
                    walk_artifacts(child)
            elif isinstance(value, list):
                for child in value:
                    walk_artifacts(child)

        walk_artifacts(artifacts)

    manifest = {
        "schema_version": "worker-artifact-manifest-1.0",
        "run_id": run_id,
        "artifacts": [entries[key] for key in sorted(entries)],
    }
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=output_root, prefix=".artifact-manifest.", suffix=".tmp", delete=False
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(manifest, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, output_root / "artifact-manifest.json")
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
    result["artifact_manifest"] = manifest
    return manifest


def _fail_running_run(store: RunStore, run_id: str, code: str, message: str) -> None:
    """Close a failed worker attempt without losing a prior cancel request.

    A cancellation request is a state transition of its own.  Worker failure
    handling can run concurrently with that transition, so it must never
    leave ``cancel_requested`` stranded or overwrite a cancellation that has
    already won the race with a generic ``failed`` state.
    """

    def _close_cancel_if_requested() -> bool:
        try:
            current = store.get_run(run_id)
            if current["lifecycle_status"] == "cancel_requested":
                store.transition_run(
                    run_id,
                    "cancelled",
                    reason_code="USER_CANCELLED",
                    reason_message="Worker 失败或停止时已收到取消请求",
                )
                return True
            return current["lifecycle_status"] != "running"
        except RunStoreError:
            return True

    try:
        # If cancellation already committed, close it immediately.  In
        # particular this covers timeout/non-zero/protocol errors after the
        # HTTP cancel endpoint changed the lifecycle state.
        if _close_cancel_if_requested():
            return
        for execution in store.get_rule_executions(run_id):
            if execution["execution_state"] in {"pending", "running"}:
                try:
                    store.transition_rule_execution(
                        run_id,
                        execution["rule_id"],
                        "failed",
                        reason_code=code,
                        reason_detail={"message": message},
                    )
                except RunStoreError:
                    # Cancellation may have committed between the initial
                    # snapshot and this rule update.  The final lifecycle
                    # close below resolves that race.
                    if _close_cancel_if_requested():
                        return
                    raise
        current = store.get_run(run_id)
        if current["lifecycle_status"] == "cancel_requested":
            store.transition_run(
                run_id,
                "cancelled",
                reason_code="USER_CANCELLED",
                reason_message="Worker 失败或停止时已收到取消请求",
            )
        elif current["lifecycle_status"] == "running":
            store.transition_run(run_id, "failed", reason_code=code, reason_message=message)
    except RunStoreError:
        # Preserve the original worker/publish failure, but make one last
        # best-effort attempt to close a cancellation that won the CAS race.
        try:
            if store.get_run(run_id)["lifecycle_status"] == "cancel_requested":
                store.transition_run(
                    run_id,
                    "cancelled",
                    reason_code="USER_CANCELLED",
                    reason_message="Worker 失败或停止时已收到取消请求",
                )
        except RunStoreError:
            pass


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
    if output.exists():
        error = CoordinatorError(f"Refusing to overwrite existing Run output: {output}")
        store.transition_run(run_id, "running")
        _fail_running_run(store, run_id, "OUTPUT_ALREADY_EXISTS", str(error))
        raise error
    output.parent.mkdir(parents=True, exist_ok=True)
    work_output = output.with_name(f".{output.name}.work-{run_id}")
    if work_output.exists():
        error = CoordinatorError(f"Refusing to reuse existing Worker work directory: {work_output}")
        store.transition_run(run_id, "running")
        _fail_running_run(store, run_id, "WORK_DIRECTORY_ALREADY_EXISTS", str(error))
        raise error
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
        output_dir=work_output,
        preflight_plan_hash=run["preflight_plan_hash"],
        rule_bundle_id=run["rule_bundle_id"],
    )
    store.transition_run(run_id, "running")
    for execution in store.get_rule_executions(run_id):
        store.transition_rule_execution(run_id, execution["rule_id"], "running")

    job_file: Path | None = None
    output_published = False
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

        try:
            _publish_worker_artifact_manifest(result, work_output, run_id=run_id)
        except (CoordinatorError, OSError) as exc:
            _fail_running_run(store, run_id, "WORKER_ARTIFACT_INVALID", str(exc))
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
        try:
            execution_outcomes = _rule_execution_outcomes(result, finding_counts)
        except WorkerProtocolError as exc:
            _fail_running_run(store, run_id, "WORKER_PROTOCOL_INVALID", str(exc))
            raise CoordinatorError(str(exc)) from exc
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
                outcome = execution_outcomes[rule_id]
                store.transition_rule_execution(
                    run_id,
                    rule_id,
                    outcome["state"],
                    reason_code=outcome.get("reason_code"),
                    reason_detail=outcome.get("reason_detail"),
                    finding_count=count,
                )
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
        # Publish the complete, validated Worker directory as one filesystem
        # rename before exposing the succeeded Run.  The final directory is
        # never populated incrementally and an existing destination is never
        # overwritten.
        os.replace(work_output, output)
        output_published = True
        try:
            return store.publish_result(run_id, result)
        except Exception:
            # A cancellation or SQLite publish failure must not leave a
            # result directory that looks like a committed Run.
            if output_published and store.get_run(run_id)["lifecycle_status"] != "succeeded":
                shutil.rmtree(output, ignore_errors=True)
            raise
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
        if work_output.exists():
            shutil.rmtree(work_output, ignore_errors=True)
