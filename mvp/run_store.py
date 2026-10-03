"""SQLite 持久化的 Case/Run 生命周期、Finding、Evidence 和复核审计。"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from mvp.capabilities import MODE_CATALOG, RULE_CATALOG, validate_mode


LIFECYCLE_STATUSES = (
    "queued",
    "running",
    "cancel_requested",
    "succeeded",
    "failed",
    "cancelled",
    "interrupted",
)

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"running", "failed", "cancelled"}),
    "running": frozenset({"succeeded", "failed", "cancel_requested", "interrupted"}),
    "cancel_requested": frozenset({"cancelled", "failed", "interrupted"}),
    "succeeded": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
    "interrupted": frozenset(),
}

MACHINE_STATUSES = frozenset({"pass", "warning", "manual", "error"})
RULE_EXECUTION_STATES = frozenset({"pending", "running", "succeeded", "not_applicable", "unsupported", "failed"})
TERMINAL_RULE_EXECUTION_STATES = frozenset({"succeeded", "not_applicable", "unsupported", "failed"})
ALLOWED_RULE_EXECUTION_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"running", "succeeded", "not_applicable", "unsupported", "failed"}),
    "running": frozenset({"succeeded", "not_applicable", "unsupported", "failed"}),
    "succeeded": frozenset(),
    "not_applicable": frozenset(),
    "unsupported": frozenset(),
    "failed": frozenset(),
}


class RunStoreError(RuntimeError):
    """Stable storage/domain error suitable for an HTTP adapter."""

    def __init__(self, code: str, message: str, *, status: int = 422, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = dict(details or {})

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
            "retryable": self.code in {"RUN_PLAN_CHANGED", "RUN_NOT_READY"},
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _uuid() -> str:
    return str(uuid4())


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _parse_json(value: str) -> Any:
    return json.loads(value)


class RunStore:
    """Thread-safe SQLite repository for the local checker domain."""

    schema_version = 7

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.path,
            check_same_thread=False,
            isolation_level=None,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        if self.path != ":memory:":
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA synchronous = NORMAL")
        self._initialize()

    def _initialize(self) -> None:
        with self._lock:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cases (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT,
                    revision INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL REFERENCES cases(id),
                    mode TEXT NOT NULL,
                    lifecycle_status TEXT NOT NULL,
                    machine_overall_status TEXT,
                    inputs_json TEXT NOT NULL,
                    planned_rule_ids_json TEXT NOT NULL,
                    input_snapshot_json TEXT,
                    preflight_plan_hash TEXT,
                    rule_bundle_id TEXT,
                    finding_counts_json TEXT,
                    published_summary_json TEXT,
                    review_revision INTEGER NOT NULL DEFAULT 0,
                    resolved_overall_status TEXT,
                    resolved_overall_review_revision INTEGER,
                    resolved_overall_computed_at TEXT,
                    failure_code TEXT,
                    failure_message TEXT,
                    revision INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT
                );
                CREATE INDEX IF NOT EXISTS runs_case_updated_idx ON runs(case_id, updated_at DESC);
                CREATE TABLE IF NOT EXISTS rule_executions (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    plan_order INTEGER NOT NULL,
                    rule_id TEXT NOT NULL,
                    rule_version TEXT,
                    execution_state TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    reason_code TEXT,
                    reason_detail_json TEXT,
                    finding_count INTEGER NOT NULL DEFAULT 0,
                    contributes_to_overall INTEGER NOT NULL DEFAULT 1,
                    finding_ids_json TEXT,
                    UNIQUE(run_id, plan_order),
                    UNIQUE(run_id, rule_id)
                );
                CREATE INDEX IF NOT EXISTS rule_executions_run_idx ON rule_executions(run_id, plan_order);
                CREATE TABLE IF NOT EXISTS blobs (
                    id TEXT PRIMARY KEY,
                    sha256 TEXT NOT NULL UNIQUE,
                    size_bytes INTEGER NOT NULL,
                    media_type TEXT NOT NULL,
                    storage_relpath TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL REFERENCES cases(id),
                    blob_id TEXT NOT NULL REFERENCES blobs(id),
                    role TEXT NOT NULL,
                    original_filename TEXT NOT NULL,
                    page_count INTEGER NOT NULL,
                    pdf_version TEXT,
                    encrypted INTEGER NOT NULL DEFAULT 0,
                    preflight_status TEXT NOT NULL,
                    preflight_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS documents_case_role_idx ON documents(case_id, role, created_at DESC);
                CREATE TABLE IF NOT EXISTS run_inputs (
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    role TEXT NOT NULL,
                    document_id TEXT NOT NULL REFERENCES documents(id),
                    blob_sha256 TEXT NOT NULL,
                    PRIMARY KEY (run_id, role),
                    UNIQUE(run_id, document_id)
                );
                CREATE TABLE IF NOT EXISTS findings (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    display_sequence INTEGER NOT NULL,
                    rule_id TEXT NOT NULL,
                    rule_execution_id TEXT,
                    input_snapshot_json TEXT,
                    machine_status TEXT NOT NULL,
                    title TEXT,
                    summary TEXT,
                    details_json TEXT NOT NULL,
                    review_status TEXT NOT NULL DEFAULT 'not_required',
                    review_resolution TEXT,
                    resolved_status TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(run_id, display_sequence),
                    UNIQUE(run_id, id)
                );
                CREATE INDEX IF NOT EXISTS findings_run_status_idx ON findings(run_id, machine_status, display_sequence);
                CREATE INDEX IF NOT EXISTS findings_run_rule_idx ON findings(run_id, rule_id, display_sequence);
                CREATE TABLE IF NOT EXISTS evidence (
                    id TEXT PRIMARY KEY,
                    finding_id TEXT NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
                    run_id TEXT REFERENCES runs(id) ON DELETE CASCADE,
                    rule_execution_id TEXT,
                    document_sha256 TEXT,
                    input_snapshot_json TEXT,
                    role TEXT NOT NULL,
                    pdf_page INTEGER NOT NULL,
                    bbox_json TEXT NOT NULL,
                    semantic_role TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS review_actions (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    finding_id TEXT NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
                    actor_id TEXT,
                    action_type TEXT NOT NULL,
                    base_run_review_revision INTEGER NOT NULL,
                    new_run_review_revision INTEGER,
                    observation_json TEXT,
                    note TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS run_events (
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    sequence INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, sequence)
                );
                """
            )
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._migrate_schema_locked()
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    def _migrate_schema_locked(self) -> None:
        """Apply all additive migrations as one re-entrant startup transaction."""

        existing_columns = {
            str(item["name"])
            for item in self._connection.execute("PRAGMA table_info(runs)").fetchall()
        }
        if "planned_rule_ids_json" not in existing_columns:
            self._connection.execute("ALTER TABLE runs ADD COLUMN planned_rule_ids_json TEXT NOT NULL DEFAULT '[]'")
        if "input_snapshot_json" not in existing_columns:
            self._connection.execute("ALTER TABLE runs ADD COLUMN input_snapshot_json TEXT")
        if "finding_counts_json" not in existing_columns:
            self._connection.execute("ALTER TABLE runs ADD COLUMN finding_counts_json TEXT")
        if "published_summary_json" not in existing_columns:
            self._connection.execute("ALTER TABLE runs ADD COLUMN published_summary_json TEXT")
        if "review_revision" not in existing_columns:
            self._connection.execute("ALTER TABLE runs ADD COLUMN review_revision INTEGER NOT NULL DEFAULT 0")
        if "resolved_overall_status" not in existing_columns:
            self._connection.execute("ALTER TABLE runs ADD COLUMN resolved_overall_status TEXT")
        if "resolved_overall_review_revision" not in existing_columns:
            self._connection.execute("ALTER TABLE runs ADD COLUMN resolved_overall_review_revision INTEGER")
        if "resolved_overall_computed_at" not in existing_columns:
            self._connection.execute("ALTER TABLE runs ADD COLUMN resolved_overall_computed_at TEXT")
        rule_execution_columns = {
            str(item["name"])
            for item in self._connection.execute("PRAGMA table_info(rule_executions)").fetchall()
        }
        if "finding_ids_json" not in rule_execution_columns:
            self._connection.execute("ALTER TABLE rule_executions ADD COLUMN finding_ids_json TEXT")
        if "contributes_to_overall" not in rule_execution_columns:
            self._connection.execute(
                "ALTER TABLE rule_executions ADD COLUMN contributes_to_overall INTEGER NOT NULL DEFAULT 1"
            )
        finding_columns = {
            str(item["name"])
            for item in self._connection.execute("PRAGMA table_info(findings)").fetchall()
        }
        if "rule_execution_id" not in finding_columns:
            self._connection.execute("ALTER TABLE findings ADD COLUMN rule_execution_id TEXT")
        if "input_snapshot_json" not in finding_columns:
            self._connection.execute("ALTER TABLE findings ADD COLUMN input_snapshot_json TEXT")
        evidence_columns = {
            str(item["name"])
            for item in self._connection.execute("PRAGMA table_info(evidence)").fetchall()
        }
        if "run_id" not in evidence_columns:
            self._connection.execute("ALTER TABLE evidence ADD COLUMN run_id TEXT")
        if "rule_execution_id" not in evidence_columns:
            self._connection.execute("ALTER TABLE evidence ADD COLUMN rule_execution_id TEXT")
        if "document_sha256" not in evidence_columns:
            self._connection.execute("ALTER TABLE evidence ADD COLUMN document_sha256 TEXT")
        if "input_snapshot_json" not in evidence_columns:
            self._connection.execute("ALTER TABLE evidence ADD COLUMN input_snapshot_json TEXT")
        review_action_columns = {
            str(item["name"])
            for item in self._connection.execute("PRAGMA table_info(review_actions)").fetchall()
        }
        if "actor_id" not in review_action_columns:
            self._connection.execute("ALTER TABLE review_actions ADD COLUMN actor_id TEXT")
        if "new_run_review_revision" not in review_action_columns:
            self._connection.execute("ALTER TABLE review_actions ADD COLUMN new_run_review_revision INTEGER")
        # Backfill the traceability columns for databases created before v6/v7.
        # Legacy rows without a matching execution plan remain explicitly
        # nullable; new published rows are always written with these links.
        self._connection.execute(
            """UPDATE findings
               SET rule_execution_id = (
                       SELECT re.id FROM rule_executions re
                       WHERE re.run_id = findings.run_id AND re.rule_id = findings.rule_id
                       ORDER BY re.plan_order LIMIT 1
                   ),
                   input_snapshot_json = (
                       SELECT r.input_snapshot_json FROM runs r WHERE r.id = findings.run_id
                   )
               WHERE rule_execution_id IS NULL"""
        )
        self._connection.execute(
            """UPDATE evidence
               SET run_id = (SELECT f.run_id FROM findings f WHERE f.id = evidence.finding_id),
                   rule_execution_id = (
                       SELECT f.rule_execution_id FROM findings f WHERE f.id = evidence.finding_id
                   ),
                   input_snapshot_json = (
                       SELECT f.input_snapshot_json FROM findings f WHERE f.id = evidence.finding_id
                   )
               WHERE run_id IS NULL OR rule_execution_id IS NULL OR input_snapshot_json IS NULL"""
        )
        row = self._connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            self._connection.execute(
                "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?)",
                (str(self.schema_version),),
            )
        elif int(row["value"]) > self.schema_version:
            raise RunStoreError(
                "UNSUPPORTED_SCHEMA_VERSION",
                "SQLite 数据库版本不受当前运行时支持",
                status=500,
                details={"expected_max": self.schema_version, "actual": int(row["value"])},
            )
        elif int(row["value"]) < self.schema_version:
            self._connection.execute(
                "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
                (str(self.schema_version),),
            )
        required_columns = {
            "runs": {"planned_rule_ids_json", "input_snapshot_json", "review_revision"},
            "rule_executions": {"contributes_to_overall", "finding_ids_json"},
            "findings": {"rule_execution_id", "input_snapshot_json"},
            "evidence": {"run_id", "rule_execution_id", "document_sha256", "input_snapshot_json"},
        }
        for table, required in required_columns.items():
            actual = {str(item["name"]) for item in self._connection.execute(f"PRAGMA table_info({table})")}
            if not required.issubset(actual):
                raise RunStoreError(
                    "SCHEMA_INTEGRITY_ERROR",
                    "SQLite 数据库缺少当前版本所需字段",
                    status=500,
                    details={"table": table, "missing": sorted(required - actual)},
                )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def require_mode_enabled(self, mode: str, *, operation: str = "execute") -> dict[str, Any]:
        """Enforce the shared mode gate for a lifecycle operation."""

        decision = validate_mode(mode, operation=operation)
        if not decision["enabled"]:
            raise RunStoreError(
                str(decision["code"]),
                str(decision["message"]),
                status=int(decision["status"]),
                details=decision["details"],
            )
        return dict(decision["capability"])

    def create_case(self, name: str, description: str | None = None) -> dict[str, Any]:
        clean_name = name.strip() if isinstance(name, str) else ""
        if not 1 <= len(clean_name) <= 120:
            raise RunStoreError("INVALID_CASE_NAME", "Case 名称长度必须为 1–120 个字符")
        if description is not None and (not isinstance(description, str) or len(description) > 2000):
            raise RunStoreError("INVALID_CASE_DESCRIPTION", "Case 说明最多 2000 个字符")
        case_id = _uuid()
        timestamp = _now()
        with self._lock:
            self._connection.execute(
                "INSERT INTO cases(id, name, description, created_at, updated_at) VALUES(?, ?, ?, ?, ?)",
                (case_id, clean_name, description, timestamp, timestamp),
            )
            return self.get_case(case_id)

    def get_case(self, case_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
            if row is None:
                raise RunStoreError("CASE_NOT_FOUND", "Case 不存在", status=404, details={"case_id": case_id})
            counts = self._connection.execute(
                "SELECT COUNT(*) AS run_count FROM runs WHERE case_id = ?", (case_id,)
            ).fetchone()
            document_counts = self._connection.execute(
                "SELECT COUNT(*) AS document_count FROM documents WHERE case_id = ?", (case_id,)
            ).fetchone()
            return {
                "id": row["id"],
                "name": row["name"],
                "description": row["description"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "revision": row["revision"],
                "document_count": int(document_counts["document_count"]),
                "run_count": int(counts["run_count"]),
            }

    def list_cases(self, *, query: str | None = None, limit: int = 25) -> list[dict[str, Any]]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise RunStoreError("INVALID_LIMIT", "limit 必须在 1–100 之间")
        with self._lock:
            if query:
                like = f"%{query.strip()}%"
                rows = self._connection.execute(
                    "SELECT id FROM cases WHERE name LIKE ? OR description LIKE ? ORDER BY updated_at DESC LIMIT ?",
                    (like, like, limit),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT id FROM cases ORDER BY updated_at DESC LIMIT ?", (limit,)
                ).fetchall()
            return [self.get_case(row["id"]) for row in rows]

    @staticmethod
    def _normalize_inputs(mode: str, inputs: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(inputs, Mapping):
            raise RunStoreError("INVALID_RUN_INPUTS", "Run inputs 必须是对象")
        capability = MODE_CATALOG[mode]
        expected = {
            "report": "report_document_id",
            "ptr": "ptr_document_id",
            "record_9706_1": "record_document_id",
            "record_9706_202": "record_document_id",
        }
        expected_keys = {expected[role] for role in capability["required_roles"]}
        actual_keys = set(inputs)
        if actual_keys != expected_keys:
            raise RunStoreError(
                "INVALID_RUN_INPUTS",
                "Run 输入角色与模式不匹配",
                details={"expected": sorted(expected_keys), "actual": sorted(actual_keys)},
            )
        normalized: dict[str, Any] = {}
        for key in sorted(expected_keys):
            value = inputs[key]
            if not isinstance(value, str) or not value.strip():
                raise RunStoreError("INVALID_RUN_INPUTS", "Run 输入引用必须是非空字符串", details={"field": key})
            normalized[key] = value
        return normalized

    def create_run(
        self,
        *,
        case_id: str,
        mode: str,
        inputs: Mapping[str, Any],
        input_snapshot: Mapping[str, Any] | None = None,
        preflight_plan_hash: str | None = None,
        rule_bundle_id: str | None = None,
    ) -> dict[str, Any]:
        capability = self.require_mode_enabled(mode, operation="create_run")
        self.get_case(case_id)
        normalized_inputs = self._normalize_inputs(mode, inputs)
        planned_rule_ids = list(capability["rule_ids"])
        rule_bundle_id = rule_bundle_id or "report-checks-mvp-2026-09-30"
        run_id = _uuid()
        timestamp = _now()
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._connection.execute(
                    """INSERT INTO runs(
                        id, case_id, mode, lifecycle_status, machine_overall_status,
                        inputs_json, planned_rule_ids_json, input_snapshot_json, preflight_plan_hash, rule_bundle_id, revision,
                        created_at, updated_at
                    ) VALUES(?, ?, ?, 'queued', NULL, ?, ?, ?, ?, ?, 1, ?, ?)""",
                    (
                        run_id,
                        case_id,
                        mode,
                        _json(normalized_inputs),
                        _json(planned_rule_ids),
                        _json(input_snapshot) if input_snapshot is not None else None,
                        preflight_plan_hash,
                        rule_bundle_id,
                        timestamp,
                        timestamp,
                    ),
                )
                for plan_order, rule_id in enumerate(planned_rule_ids, start=1):
                    rule = RULE_CATALOG.get(rule_id, {})
                    self._connection.execute(
                        """INSERT INTO rule_executions(
                            id, run_id, plan_order, rule_id, rule_version, execution_state
                        ) VALUES(?, ?, ?, ?, ?, 'pending')""",
                        ( _uuid(), run_id, plan_order, rule_id, rule.get("version") ),
                    )
                self._append_event_locked(
                    run_id,
                    "run_state_changed",
                    {"from": None, "to": "queued", "mode": mode},
                )
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
            return self.get_run(run_id)

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                raise RunStoreError("RUN_NOT_FOUND", "Run 不存在", status=404, details={"run_id": run_id})
            run = self._run_from_row(row)
            self._attach_run_inputs_locked(run)
            return run

    def list_runs(self, case_id: str, *, limit: int = 25) -> list[dict[str, Any]]:
        self.get_case(case_id)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise RunStoreError("INVALID_LIMIT", "limit 必须在 1–100 之间")
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM runs WHERE case_id = ? ORDER BY updated_at DESC LIMIT ?",
                (case_id, limit),
            ).fetchall()
            runs = [self._run_from_row(row) for row in rows]
            for run in runs:
                self._attach_run_inputs_locked(run)
            return runs

    def get_rule_executions(self, run_id: str) -> list[dict[str, Any]]:
        self.get_run(run_id)
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM rule_executions WHERE run_id = ? ORDER BY plan_order",
                (run_id,),
            ).fetchall()
            return [self._rule_execution_from_row(row) for row in rows]

    def transition_rule_execution(
        self,
        run_id: str,
        rule_id: str,
        target: str,
        *,
        reason_code: str | None = None,
        reason_detail: Mapping[str, Any] | None = None,
        finding_count: int = 0,
    ) -> dict[str, Any]:
        if target not in RULE_EXECUTION_STATES:
            raise RunStoreError("INVALID_RULE_EXECUTION_STATE", "未知的规则执行状态")
        if not isinstance(finding_count, int) or finding_count < 0:
            raise RunStoreError("INVALID_FINDING_COUNT", "finding_count 必须是非负整数")
        if target in {"not_applicable", "unsupported", "failed"} and not reason_code:
            raise RunStoreError("RULE_EXECUTION_REASON_REQUIRED", "该规则终态必须提供 reason_code")
        with self._lock:
            run_row = self._connection.execute(
                "SELECT lifecycle_status FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            if run_row is None:
                raise RunStoreError("RUN_NOT_FOUND", "Run 不存在", status=404, details={"run_id": run_id})
            if run_row["lifecycle_status"] != "running":
                raise RunStoreError(
                    "RUN_NOT_RUNNING",
                    "只有 running Run 可以更新规则执行状态",
                    status=409,
                    details={"run_id": run_id, "lifecycle_status": run_row["lifecycle_status"]},
                )
            row = self._connection.execute(
                "SELECT * FROM rule_executions WHERE run_id = ? AND rule_id = ?",
                (run_id, rule_id),
            ).fetchone()
            if row is None:
                raise RunStoreError(
                    "RULE_EXECUTION_NOT_FOUND",
                    "Run 计划中不存在该规则",
                    status=404,
                    details={"run_id": run_id, "rule_id": rule_id},
                )
            current = str(row["execution_state"])
            if target not in ALLOWED_RULE_EXECUTION_TRANSITIONS[current]:
                raise RunStoreError(
                    "INVALID_RULE_EXECUTION_TRANSITION",
                    "规则执行状态迁移不允许",
                    status=409,
                    details={"run_id": run_id, "rule_id": rule_id, "from": current, "to": target},
                )
            timestamp = _now()
            started_at = row["started_at"] or (timestamp if target == "running" else None)
            finished_at = timestamp if target in TERMINAL_RULE_EXECUTION_STATES else None
            self._connection.execute(
                """UPDATE rule_executions SET execution_state = ?, started_at = ?, finished_at = ?,
                   reason_code = ?, reason_detail_json = ?, finding_count = ?
                   WHERE run_id = ? AND rule_id = ?""",
                (
                    target,
                    started_at,
                    finished_at,
                    reason_code,
                    _json(reason_detail) if reason_detail is not None else None,
                    finding_count,
                    run_id,
                    rule_id,
                ),
            )
            return self._rule_execution_from_row(
                self._connection.execute(
                    "SELECT * FROM rule_executions WHERE run_id = ? AND rule_id = ?",
                    (run_id, rule_id),
                ).fetchone()
            )

    def publish_result(self, run_id: str, result: Mapping[str, Any]) -> dict[str, Any]:
        """Validate a worker result and atomically publish its Run summary."""

        run = self.get_run(run_id)
        if run["lifecycle_status"] != "running":
            raise RunStoreError(
                "RUN_NOT_PUBLISHABLE",
                "只有 running Run 可以发布结果",
                status=409,
                details={"run_id": run_id, "lifecycle_status": run["lifecycle_status"]},
            )
        try:
            self.require_mode_enabled(run["mode"], operation="publish_result")
        except RunStoreError as error:
            self.transition_run(
                run_id,
                "failed",
                reason_code=error.code,
                reason_message=error.message,
            )
            raise
        try:
            self._validate_result(run, result)
            executions = self.get_rule_executions(run_id)
            planned_rule_ids = set(run["planned_rule_ids"])
            execution_rule_ids = {item["rule_id"] for item in executions}
            if execution_rule_ids != planned_rule_ids:
                raise RunStoreError(
                    "RULE_EXECUTION_PLAN_MISMATCH",
                    "Run 的规则执行记录与 ModePlan 不一致",
                    details={
                        "missing": sorted(planned_rule_ids - execution_rule_ids),
                        "unexpected": sorted(execution_rule_ids - planned_rule_ids),
                    },
                )
            invalid = [item for item in executions if item["execution_state"] not in TERMINAL_RULE_EXECUTION_STATES]
            failed = [item for item in executions if item["execution_state"] == "failed"]
            if invalid or failed:
                raise RunStoreError(
                    "RULE_EXECUTION_INCOMPLETE",
                    "存在未完成或失败的计划规则，不能发布 Run",
                    details={"pending": [item["rule_id"] for item in invalid], "failed": [item["rule_id"] for item in failed]},
                )
            findings_by_rule = {
                rule_id: sum(1 for finding in result["findings"] if finding.get("rule_id") == rule_id)
                for rule_id in planned_rule_ids
            }
            count_mismatches = [
                {
                    "rule_id": execution["rule_id"],
                    "ledger": execution["finding_count"],
                    "result": findings_by_rule.get(execution["rule_id"], 0),
                }
                for execution in executions
                if execution["finding_count"] != findings_by_rule.get(execution["rule_id"], 0)
            ]
            if count_mismatches:
                raise RunStoreError(
                    "RULE_FINDING_COUNT_MISMATCH",
                    "规则执行账本的 Finding 数量与待发布结果不一致",
                    details={"mismatches": count_mismatches},
                )
            if executions and not result["findings"] and all(
                item["execution_state"] in {"not_applicable", "unsupported"} for item in executions
            ):
                raise RunStoreError(
                    "NO_EXECUTABLE_RULES",
                    "所有计划规则均未产生可发布结果",
                    details={"run_id": run_id},
                )
        except RunStoreError as error:
            self.transition_run(
                run_id,
                "failed",
                reason_code=error.code,
                reason_message=error.message,
            )
            raise
        # Recompute summary counters from the immutable Finding payload. The
        # worker-provided counters were validated above, but are never trusted
        # as the persisted source of truth.
        status_counts = self._status_counts_from_findings(result["findings"])
        machine_overall_status = next(
            (
                status
                for status in ("error", "manual", "warning", "pass")
                if status_counts[status]
            ),
            "pass",
        )
        summary = {
            "schema_version": result.get("schema_version"),
            "mode": result["mode"],
            "machine_overall_status": machine_overall_status,
            "status_counts": status_counts,
            "coverage": result.get("coverage"),
        }
        timestamp = _now()
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                execution_by_rule = {item["rule_id"]: item for item in executions}
                persisted_finding_ids = self._insert_findings_locked(
                    run_id,
                    result["findings"],
                    timestamp,
                    run["planned_rule_ids"],
                    execution_by_rule=execution_by_rule,
                    input_snapshot=run.get("input_snapshot"),
                )
                finding_ids_by_rule: dict[str, list[str]] = {rule_id: [] for rule_id in run["planned_rule_ids"]}
                for finding in result["findings"]:
                    rule_id = str(finding.get("rule_id") or "")
                    finding_ids_by_rule.setdefault(rule_id, []).append(
                        persisted_finding_ids[str(finding["id"])]
                    )
                for rule_id, finding_ids in finding_ids_by_rule.items():
                    self._connection.execute(
                        "UPDATE rule_executions SET finding_ids_json = ? WHERE run_id = ? AND rule_id = ?",
                        (_json(finding_ids), run_id, rule_id),
                    )
                update_cursor = self._connection.execute(
                        """UPDATE runs SET lifecycle_status = 'succeeded', machine_overall_status = ?,
                       finding_counts_json = ?, published_summary_json = ?, revision = revision + 1,
                       updated_at = ?, finished_at = ? WHERE id = ? AND lifecycle_status = 'running'""",
                    (
                        machine_overall_status,
                        _json(status_counts),
                        _json(summary),
                        timestamp,
                        timestamp,
                        run_id,
                    ),
                )
                if update_cursor.rowcount != 1:
                    raise RunStoreError(
                        "RUN_NOT_PUBLISHABLE",
                        "Run 在发布期间已被其他状态迁移占用",
                        status=409,
                        details={"run_id": run_id},
                    )
                self._append_event_locked(
                    run_id,
                    "findings_published",
                    {
                        "finding_count": len(result["findings"]),
                        "finding_ids": [
                            persisted_finding_ids[str(finding["id"])] for finding in result["findings"]
                        ],
                    },
                )
                self._append_event_locked(
                    run_id,
                    "run_state_changed",
                    {"from": "running", "to": "succeeded", "summary": summary},
                )
                self._connection.execute("COMMIT")
            except RunStoreError as error:
                self._connection.execute("ROLLBACK")
                try:
                    self.transition_run(
                        run_id,
                        "failed",
                        reason_code=error.code,
                        reason_message=error.message,
                    )
                except RunStoreError:
                    pass
                raise
            except Exception as error:
                self._connection.execute("ROLLBACK")
                failure = RunStoreError(
                    "PUBLISH_TRANSACTION_FAILED",
                    "Worker 结果发布事务失败",
                    status=500,
                    details={"error_type": type(error).__name__},
                )
                try:
                    self.transition_run(
                        run_id,
                        "failed",
                        reason_code=failure.code,
                        reason_message=failure.message,
                    )
                except RunStoreError:
                    pass
                raise failure from error
        return self.get_run(run_id)

    def _insert_findings_locked(
        self,
        run_id: str,
        findings: list[Mapping[str, Any]],
        timestamp: str,
        planned_rule_ids: list[str] | None = None,
        *,
        execution_by_rule: Mapping[str, Mapping[str, Any]] | None = None,
        input_snapshot: Mapping[str, Any] | None = None,
    ) -> dict[str, str]:
        status_rank = {"error": 0, "manual": 1, "warning": 2, "pass": 3}
        rule_rank = {rule_id: index for index, rule_id in enumerate(planned_rule_ids or ())}
        findings = sorted(
            findings,
            key=lambda item: (
                status_rank.get(str(item.get("status")), 99),
                rule_rank.get(str(item.get("rule_id") or ""), 99),
                str(item.get("id") or ""),
            ),
        )
        seen_finding_ids: set[str] = set()
        persisted_finding_ids: dict[str, str] = {}
        for display_sequence, finding in enumerate(findings, start=1):
            source_finding_id = finding.get("id")
            if (
                not isinstance(source_finding_id, str)
                or not source_finding_id.strip()
                or source_finding_id in seen_finding_ids
            ):
                raise RunStoreError("WORKER_RESULT_FINDING_ID_INVALID", "Worker Finding ID 缺失或重复")
            seen_finding_ids.add(source_finding_id)
            finding_id = source_finding_id
            # Worker IDs are stable within a result, but may repeat across
            # separate Runs. Preserve the first occurrence for human-readable
            # compatibility and scope later collisions to this Run so the
            # database's global Finding primary key remains unique.
            existing = self._connection.execute(
                "SELECT 1 FROM findings WHERE id = ? LIMIT 1", (finding_id,)
            ).fetchone()
            if existing is not None:
                suffix = f"@{run_id}"
                candidate = f"{source_finding_id}{suffix}"
                attempt = 2
                while self._connection.execute(
                    "SELECT 1 FROM findings WHERE id = ? LIMIT 1", (candidate,)
                ).fetchone() is not None:
                    candidate = f"{source_finding_id}{suffix}-{attempt}"
                    attempt += 1
                finding_id = candidate
            persisted_finding_ids[source_finding_id] = finding_id
            locations = finding.get("evidence_locations", [])
            if not isinstance(locations, list):
                raise RunStoreError("WORKER_RESULT_EVIDENCE_INVALID", "Worker evidence_locations 必须是数组")
            details = finding.get("details", {})
            try:
                _json(details)
            except (TypeError, ValueError) as exc:
                raise RunStoreError("WORKER_RESULT_DETAILS_INVALID", "Worker Finding details 不是可序列化 JSON") from exc
            rule_id = str(finding.get("rule_id") or "")
            execution = (execution_by_rule or {}).get(rule_id)
            if execution is None:
                raise RunStoreError(
                    "WORKER_RESULT_RULE_OUT_OF_PLAN",
                    "Worker Finding 没有对应的当前 Run 规则执行记录",
                    details={"rule_id": rule_id},
                )

            self._connection.execute(
                """INSERT INTO findings(
                    id, run_id, display_sequence, rule_id, rule_execution_id, input_snapshot_json,
                    machine_status, title, summary, details_json, review_status, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    finding_id,
                    run_id,
                    display_sequence,
                    rule_id,
                    execution["id"],
                    _json(input_snapshot) if input_snapshot is not None else None,
                    finding["status"],
                    finding.get("title"),
                    finding.get("summary"),
                    _json(details),
                    "pending" if finding["status"] == "manual" else "not_required",
                    timestamp,
                ),
            )
            for location in locations:
                if not isinstance(location, Mapping):
                    raise RunStoreError("WORKER_RESULT_EVIDENCE_INVALID", "Worker evidence 必须是对象")
                role = location.get("role")
                page = location.get("pdf_page")
                bbox = location.get("bbox")
                if (
                    not isinstance(role, str)
                    or not role.strip()
                    or not isinstance(page, int)
                    or isinstance(page, bool)
                    or page < 1
                    or not isinstance(bbox, (list, tuple))
                    or len(bbox) != 4
                    or any(
                        not isinstance(value, (int, float))
                        or isinstance(value, bool)
                        or not math.isfinite(float(value))
                        for value in bbox
                    )
                    or bbox[0] < 0
                    or bbox[1] < 0
                    or bbox[0] >= bbox[2]
                    or bbox[1] >= bbox[3]
                ):
                    raise RunStoreError("WORKER_RESULT_EVIDENCE_INVALID", "Worker evidence 坐标无效")
                snapshot_item = (input_snapshot or {}).get(role)
                document_sha256 = (
                    snapshot_item.get("blob_sha256")
                    if isinstance(snapshot_item, Mapping) and isinstance(snapshot_item.get("blob_sha256"), str)
                    else None
                )
                self._connection.execute(
                    """INSERT INTO evidence(
                        id, finding_id, run_id, rule_execution_id, document_sha256,
                        input_snapshot_json, role, pdf_page, bbox_json, semantic_role, created_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        _uuid(),
                        finding_id,
                        run_id,
                        execution["id"],
                        document_sha256,
                        _json(input_snapshot) if input_snapshot is not None else None,
                        role.strip(),
                        page,
                        _json([float(value) for value in bbox]),
                        location.get("semantic_role"),
                        timestamp,
                    ),
                )

        return persisted_finding_ids

    @staticmethod
    def _status_counts_from_findings(findings: list[Mapping[str, Any]]) -> dict[str, int]:
        counts = {status: 0 for status in ("pass", "warning", "manual", "error")}
        for finding in findings:
            status = finding.get("status")
            if status not in counts:
                raise RunStoreError("WORKER_RESULT_SCHEMA_INVALID", "Worker Finding 状态无效")
            counts[str(status)] += 1
        return counts

    @staticmethod
    def _validate_result(run: Mapping[str, Any], result: Mapping[str, Any]) -> None:
        if not isinstance(result, Mapping) or result.get("mode") != run["mode"]:
            raise RunStoreError("WORKER_RESULT_MODE_MISMATCH", "Worker 结果模式与 Run 不一致")
        if result.get("lifecycle_status") not in {None, "succeeded"}:
            raise RunStoreError("WORKER_RESULT_NOT_SUCCEEDED", "Worker 结果不是 succeeded")
        if result.get("machine_overall_status") not in MACHINE_STATUSES:
            raise RunStoreError("INVALID_MACHINE_STATUS", "Worker 结果的机器总体状态无效")
        findings = result.get("findings")
        counts = result.get("status_counts")
        if not isinstance(findings, list) or not isinstance(counts, Mapping):
            raise RunStoreError("WORKER_RESULT_SCHEMA_INVALID", "Worker 结果缺少 findings 或 status_counts")
        expected_counts = {status: 0 for status in ("pass", "warning", "manual", "error")}
        allowed_rule_ids = set(run["planned_rule_ids"])
        for finding in findings:
            if (
                not isinstance(finding, Mapping)
                or finding.get("status") not in expected_counts
                or not isinstance(finding.get("rule_id"), str)
                or not finding.get("rule_id", "").strip()
            ):
                raise RunStoreError("WORKER_RESULT_SCHEMA_INVALID", "Worker Finding 状态无效")
            expected_counts[str(finding["status"])] += 1
            rule_id = str(finding["rule_id"])
            if rule_id not in allowed_rule_ids:
                raise RunStoreError(
                    "WORKER_RESULT_RULE_OUT_OF_PLAN",
                    "Worker Finding 使用了当前 ModePlan 之外的规则",
                    details={"rule_id": rule_id, "mode": run["mode"]},
                )
            if run["mode"] != "report_self" and rule_id.startswith("REPORT-"):
                raise RunStoreError("WORKER_RESULT_RULE_LEAK", "对比模式不能发布 REPORT-* Finding")
            locations = finding.get("evidence_locations", [])
            if not isinstance(locations, list):
                raise RunStoreError("WORKER_RESULT_EVIDENCE_INVALID", "Worker evidence_locations 必须是数组")
            snapshot = run.get("input_snapshot") or {}
            for location in locations:
                if not isinstance(location, Mapping):
                    raise RunStoreError("WORKER_RESULT_EVIDENCE_INVALID", "Worker evidence 必须是对象")
                role = location.get("role")
                page = location.get("pdf_page")
                if snapshot:
                    if role not in snapshot:
                        raise RunStoreError(
                            "WORKER_RESULT_EVIDENCE_ROLE_INVALID",
                            "Worker evidence role 不属于当前 Run 输入",
                            details={"role": role, "allowed": sorted(snapshot)},
                        )
                    max_page = snapshot[role].get("page_count")
                    if not isinstance(page, int) or isinstance(page, bool) or page < 1:
                        raise RunStoreError(
                            "WORKER_RESULT_EVIDENCE_PAGE_INVALID",
                            "Worker evidence 页码必须从 1 开始",
                            details={"role": role, "pdf_page": page},
                        )
                    if isinstance(max_page, int) and page > max_page:
                        raise RunStoreError(
                            "WORKER_RESULT_EVIDENCE_PAGE_INVALID",
                            "Worker evidence 页码超出输入 PDF 页数",
                            details={"role": role, "pdf_page": page, "page_count": max_page},
                        )
                bbox = location.get("bbox")
                if (
                    not isinstance(bbox, (list, tuple))
                    or len(bbox) != 4
                    or any(
                        not isinstance(value, (int, float))
                        or isinstance(value, bool)
                        or not math.isfinite(float(value))
                        for value in bbox
                    )
                    or bbox[0] < 0
                    or bbox[1] < 0
                    or bbox[0] >= bbox[2]
                    or bbox[1] >= bbox[3]
                ):
                    raise RunStoreError("WORKER_RESULT_EVIDENCE_INVALID", "Worker evidence 坐标无效")
        if set(counts) != set(expected_counts) or any(
            not isinstance(counts.get(key), int) or isinstance(counts.get(key), bool)
            for key in expected_counts
        ):
            raise RunStoreError("WORKER_RESULT_COUNTS_MISMATCH", "Worker status_counts 的字段或类型无效")
        if {key: counts[key] for key in expected_counts} != expected_counts:
            raise RunStoreError("WORKER_RESULT_COUNTS_MISMATCH", "Worker status_counts 与 Finding 数量不一致")
        expected_overall = (
            "error" if expected_counts["error"] else
            "manual" if expected_counts["manual"] else
            "warning" if expected_counts["warning"] else
            "pass"
        )
        if result["machine_overall_status"] != expected_overall:
            raise RunStoreError("WORKER_RESULT_OVERALL_MISMATCH", "Worker 总体状态与 Finding 状态不一致")

    def transition_run(
        self,
        run_id: str,
        target: str,
        *,
        reason_code: str | None = None,
        reason_message: str | None = None,
        machine_overall_status: str | None = None,
    ) -> dict[str, Any]:
        if target not in LIFECYCLE_STATUSES:
            raise RunStoreError("INVALID_LIFECYCLE_STATUS", "未知的 Run 生命周期状态")
        with self._lock:
            row = self._connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                raise RunStoreError("RUN_NOT_FOUND", "Run 不存在", status=404, details={"run_id": run_id})
            current = str(row["lifecycle_status"])
            if target in {"running", "succeeded"}:
                self.require_mode_enabled(str(row["mode"]), operation=f"transition:{target}")
            if target not in ALLOWED_TRANSITIONS[current]:
                raise RunStoreError(
                    "INVALID_RUN_TRANSITION",
                    "Run 生命周期迁移不允许",
                    status=409,
                    details={"run_id": run_id, "from": current, "to": target},
                )
            if target == "succeeded" and machine_overall_status not in MACHINE_STATUSES:
                raise RunStoreError(
                    "MACHINE_STATUS_REQUIRED",
                    "succeeded Run 必须提供机器总体状态",
                    details={"allowed": sorted(MACHINE_STATUSES)},
                )
            if target != "succeeded" and machine_overall_status is not None:
                raise RunStoreError("MACHINE_STATUS_FORBIDDEN", "非 succeeded Run 不保存机器总体状态")
            if target in {"failed", "interrupted"} and not reason_code:
                raise RunStoreError(
                    "FAILURE_REASON_REQUIRED",
                    "failed 或 interrupted Run 必须提供 reason_code",
                )
            timestamp = _now()
            started_at = row["started_at"]
            finished_at = row["finished_at"]
            if target == "running" and started_at is None:
                started_at = timestamp
            if target in {"succeeded", "failed", "cancelled", "interrupted"}:
                finished_at = timestamp
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                update_cursor = self._connection.execute(
                    """UPDATE runs SET lifecycle_status = ?, machine_overall_status = ?,
                       failure_code = ?, failure_message = ?, revision = revision + 1,
                       updated_at = ?, started_at = ?, finished_at = ?
                       WHERE id = ? AND lifecycle_status = ? AND revision = ?""",
                    (
                        target,
                        machine_overall_status if target == "succeeded" else None,
                        reason_code if target in {"failed", "interrupted"} else None,
                        reason_message if target in {"failed", "interrupted"} else None,
                        timestamp,
                        started_at,
                        finished_at,
                        run_id,
                        current,
                        row["revision"],
                    ),
                )
                if update_cursor.rowcount != 1:
                    raise RunStoreError(
                        "REVISION_CONFLICT",
                        "Run 在状态迁移期间已被其他写入者更新",
                        status=409,
                        details={"run_id": run_id, "revision": row["revision"]},
                    )
                self._append_event_locked(
                    run_id,
                    "run_state_changed",
                    {
                        "from": current,
                        "to": target,
                        "reason_code": reason_code,
                        "reason_message": reason_message,
                    },
                )
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
            return self.get_run(run_id)

    def recover_interrupted_runs(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT id FROM runs WHERE lifecycle_status IN ('running', 'cancel_requested') ORDER BY created_at"
            ).fetchall()
        recovered = []
        for row in rows:
            run = self.get_run(row["id"])
            decision = validate_mode(run["mode"], operation="recover")
            if decision["enabled"]:
                reason_code = "RUN_INTERRUPTED_ON_RESTART"
                reason_message = "应用启动恢复时发现遗留的运行状态"
            else:
                reason_code = str(decision["code"])
                reason_message = str(decision["message"])
            recovered.append(
                self.transition_run(
                    row["id"],
                    "interrupted",
                    reason_code=reason_code,
                    reason_message=reason_message,
                )
            )
        return recovered

    def list_events(self, run_id: str) -> list[dict[str, Any]]:
        self.get_run(run_id)
        with self._lock:
            rows = self._connection.execute(
                "SELECT sequence, event_type, payload_json, occurred_at FROM run_events WHERE run_id = ? ORDER BY sequence",
                (run_id,),
            ).fetchall()
            return [
                {
                    "run_id": run_id,
                    "sequence": row["sequence"],
                    "type": row["event_type"],
                    "payload": _parse_json(row["payload_json"]),
                    "occurred_at": row["occurred_at"],
                }
                for row in rows
            ]

    def list_findings(self, run_id: str) -> list[dict[str, Any]]:
        run = self.get_run(run_id)
        if run["lifecycle_status"] != "succeeded":
            return []
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM findings WHERE run_id = ? ORDER BY display_sequence",
                (run_id,),
            ).fetchall()
            findings = []
            for row in rows:
                finding = self._finding_from_row(row)
                self._attach_evidence_locked(finding)
                findings.append(finding)
            return findings

    def get_finding(self, finding_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute(
                """SELECT f.*, r.lifecycle_status
                   FROM findings f JOIN runs r ON r.id = f.run_id
                   WHERE f.id = ?""",
                (finding_id,),
            ).fetchone()
            if row is None:
                raise RunStoreError(
                    "FINDING_NOT_FOUND",
                    "Finding 不存在",
                    status=404,
                    details={"finding_id": finding_id},
                )
            if row["lifecycle_status"] != "succeeded":
                raise RunStoreError(
                    "FINDING_NOT_PUBLISHED",
                    "Run 结果尚未发布，Finding 暂不可查询",
                    status=409,
                    details={"finding_id": finding_id, "lifecycle_status": row["lifecycle_status"]},
                )
            finding = self._finding_from_row(row)
            self._attach_evidence_locked(finding)
            return finding

    def list_reviews(self, run_id: str) -> list[dict[str, Any]]:
        self.get_run(run_id)
        with self._lock:
            rows = self._connection.execute(
                """SELECT id, run_id, finding_id, actor_id, action_type,
                          base_run_review_revision, new_run_review_revision,
                          observation_json, note, created_at
                   FROM review_actions WHERE run_id = ? ORDER BY created_at, id""",
                (run_id,),
            ).fetchall()
            return [self._review_action_from_row(row) for row in rows]

    def append_review_action(
        self,
        finding_id: str,
        action_type: str,
        *,
        actor_id: str | None = None,
        base_review_revision: int | None = None,
        observation: Mapping[str, Any] | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        allowed_actions = {
            "confirm_candidate",
            "record_observation",
            "mark_source_unreadable",
            "withdraw",
        }
        if action_type not in allowed_actions:
            raise RunStoreError(
                "INVALID_REVIEW_ACTION",
                "审核动作不受支持",
                details={"action_type": action_type, "allowed": sorted(allowed_actions)},
            )
        if actor_id is not None and (not isinstance(actor_id, str) or not actor_id.strip()):
            raise RunStoreError("INVALID_REVIEW_ACTOR", "审核人标识必须是非空字符串")
        if observation is not None and not isinstance(observation, Mapping):
            raise RunStoreError("INVALID_REVIEW_OBSERVATION", "审核观察记录必须是对象")
        if observation is not None:
            readonly_fields = {
                "machine_status",
                "resolved_status",
                "review_resolution",
                "overall_status",
                "comparison",
                "normalized_values",
            }
            submitted_readonly = sorted(readonly_fields.intersection(observation.keys()))
            if submitted_readonly:
                raise RunStoreError(
                    "READ_ONLY_FIELD_SUBMITTED",
                    "客户端不能提交由服务端派生的复核字段",
                    details={"fields": submitted_readonly},
                )
            try:
                _json(observation)
            except (TypeError, ValueError) as exc:
                raise RunStoreError("INVALID_REVIEW_OBSERVATION", "审核观察记录不是可序列化 JSON") from exc
        if note is not None and (not isinstance(note, str) or len(note) > 4000):
            raise RunStoreError("INVALID_REVIEW_NOTE", "审核备注最多 4000 个字符")
        with self._lock:
            row = self._connection.execute(
                """SELECT f.*, r.lifecycle_status, r.review_revision
                   FROM findings f JOIN runs r ON r.id = f.run_id
                   WHERE f.id = ?""",
                (finding_id,),
            ).fetchone()
            if row is None:
                raise RunStoreError("FINDING_NOT_FOUND", "Finding 不存在", status=404, details={"finding_id": finding_id})
            if row["lifecycle_status"] != "succeeded":
                raise RunStoreError("RUN_NOT_REVIEWABLE", "只有 succeeded Run 可以审核", status=409)
            if row["machine_status"] != "manual":
                raise RunStoreError("FINDING_NOT_REVIEWABLE", "只有 manual Finding 可以审核", status=409)
            current_revision = int(row["review_revision"])
            if base_review_revision is None:
                base_review_revision = current_revision
            if (
                not isinstance(base_review_revision, int)
                or isinstance(base_review_revision, bool)
                or base_review_revision != current_revision
            ):
                raise RunStoreError(
                    "REVIEW_REVISION_CONFLICT",
                    "审核版本已变化，请基于最新审核版本重试",
                    status=409,
                    details={"current_revision": current_revision},
                )
            # Review outcomes are derived exclusively by the server. A review
            # action may carry source observations, but it cannot submit a
            # status or comparison result that changes the audit conclusion.
            derived_outcome = {
                "confirm_candidate": ("consistent", "warning"),
                "record_observation": ("indeterminate", "manual"),
                "mark_source_unreadable": ("indeterminate", "manual"),
                "withdraw": (None, None),
            }[action_type]
            review_resolution, resolved_status = derived_outcome
            review_status = "pending" if action_type == "withdraw" else "resolved"
            timestamp = _now()
            action_id = _uuid()
            new_revision = current_revision + 1
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = self._connection.execute(
                    """UPDATE runs SET review_revision = ?, revision = revision + 1,
                              updated_at = ?
                       WHERE id = ? AND lifecycle_status = 'succeeded' AND review_revision = ?""",
                    (new_revision, timestamp, row["run_id"], current_revision),
                )
                if cursor.rowcount != 1:
                    raise RunStoreError("REVIEW_REVISION_CONFLICT", "审核版本已变化，请基于最新审核版本重试", status=409)
                self._connection.execute(
                    """INSERT INTO review_actions(
                        id, run_id, finding_id, actor_id, action_type,
                        base_run_review_revision, new_run_review_revision,
                        observation_json, note, created_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        action_id,
                        row["run_id"],
                        finding_id,
                        actor_id.strip() if isinstance(actor_id, str) else None,
                        action_type,
                        current_revision,
                        new_revision,
                        _json(observation) if observation is not None else None,
                        note,
                        timestamp,
                    ),
                )
                self._connection.execute(
                    """UPDATE findings SET review_status = ?, review_resolution = ?, resolved_status = ?
                       WHERE id = ?""",
                    (review_status, review_resolution, resolved_status, finding_id),
                )
                aggregate_rows = self._connection.execute(
                    "SELECT machine_status, review_status, resolved_status, details_json FROM findings WHERE run_id = ?",
                    (row["run_id"],),
                ).fetchall()
                aggregate_statuses: list[str] = []
                for aggregate_row in aggregate_rows:
                    details = _parse_json(aggregate_row["details_json"])
                    if isinstance(details, Mapping) and details.get("contributes_to_overall") is False:
                        continue
                    aggregate_statuses.append(
                        aggregate_row["resolved_status"]
                        if aggregate_row["review_status"] == "resolved" and aggregate_row["resolved_status"]
                        else aggregate_row["machine_status"]
                    )
                resolved_overall = next(
                    (status for status in ("error", "manual", "warning", "pass") if status in aggregate_statuses),
                    None,
                )
                self._connection.execute(
                    """UPDATE runs SET resolved_overall_status = ?,
                              resolved_overall_review_revision = ?, resolved_overall_computed_at = ?
                       WHERE id = ?""",
                    (resolved_overall, new_revision, timestamp, row["run_id"]),
                )
                self._append_event_locked(
                    row["run_id"],
                    "review_action_appended",
                    {
                        "finding_id": finding_id,
                        "action_id": action_id,
                        "action_type": action_type,
                        "review_revision": new_revision,
                    },
                )
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
            return {
                "action": self._review_action_from_row(
                    self._connection.execute(
                        "SELECT id, run_id, finding_id, actor_id, action_type, base_run_review_revision, new_run_review_revision, observation_json, note, created_at FROM review_actions WHERE id = ?",
                        (action_id,),
                    ).fetchone()
                ),
                "finding": self.get_finding(finding_id),
                "review_revision": new_revision,
            }

    def _attach_evidence_locked(self, finding: dict[str, Any]) -> None:
        rows = self._connection.execute(
            """SELECT id, role, pdf_page, bbox_json, semantic_role, created_at,
                      run_id, rule_execution_id, document_sha256, input_snapshot_json
               FROM evidence WHERE finding_id = ? ORDER BY rowid""",
            (finding["id"],),
        ).fetchall()
        finding["evidence"] = [
            {
                "id": row["id"],
                "role": row["role"],
                "pdf_page": row["pdf_page"],
                "bbox": _parse_json(row["bbox_json"]),
                "semantic_role": row["semantic_role"],
                "run_id": row["run_id"],
                "rule_execution_id": row["rule_execution_id"],
                "document_sha256": row["document_sha256"],
                "input_snapshot": _parse_json(row["input_snapshot_json"])
                if row["input_snapshot_json"]
                else None,
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def _attach_run_inputs_locked(self, run: dict[str, Any]) -> None:
        rows = self._connection.execute(
            "SELECT role, document_id, blob_sha256 FROM run_inputs WHERE run_id = ? ORDER BY role",
            (run["id"],),
        ).fetchall()
        run["run_inputs"] = [
            {"role": row["role"], "document_id": row["document_id"], "blob_sha256": row["blob_sha256"]}
            for row in rows
        ]

    @staticmethod
    def _finding_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "run_id": row["run_id"],
            "display_sequence": row["display_sequence"],
            "rule_id": row["rule_id"],
            "rule_execution_id": row["rule_execution_id"],
            "input_snapshot": _parse_json(row["input_snapshot_json"])
            if row["input_snapshot_json"]
            else None,
            "machine_status": row["machine_status"],
            "title": row["title"],
            "summary": row["summary"],
            "details": _parse_json(row["details_json"]),
            "review_status": row["review_status"],
            "review_resolution": row["review_resolution"],
            "resolved_status": row["resolved_status"],
            "created_at": row["created_at"],
            "evidence": [],
        }

    @staticmethod
    def _review_action_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "run_id": row["run_id"],
            "finding_id": row["finding_id"],
            "actor_id": row["actor_id"],
            "action_type": row["action_type"],
            "base_run_review_revision": row["base_run_review_revision"],
            "new_run_review_revision": row["new_run_review_revision"],
            "observation": _parse_json(row["observation_json"]) if row["observation_json"] else None,
            "note": row["note"],
            "created_at": row["created_at"],
        }

    def _append_event_locked(self, run_id: str, event_type: str, payload: Mapping[str, Any]) -> None:
        row = self._connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) AS sequence FROM run_events WHERE run_id = ?", (run_id,)
        ).fetchone()
        self._connection.execute(
            "INSERT INTO run_events(run_id, sequence, event_type, payload_json, occurred_at) VALUES(?, ?, ?, ?, ?)",
            (run_id, int(row["sequence"]) + 1, event_type, _json(payload), _now()),
        )

    @staticmethod
    def _run_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "case_id": row["case_id"],
            "mode": row["mode"],
            "lifecycle_status": row["lifecycle_status"],
            "machine_overall_status": row["machine_overall_status"],
            "inputs": _parse_json(row["inputs_json"]),
            "planned_rule_ids": _parse_json(row["planned_rule_ids_json"]),
            "input_snapshot": _parse_json(row["input_snapshot_json"]) if row["input_snapshot_json"] else None,
            "preflight_plan_hash": row["preflight_plan_hash"],
            "rule_bundle_id": row["rule_bundle_id"],
            "finding_counts": _parse_json(row["finding_counts_json"]) if row["finding_counts_json"] else None,
            "published_summary": _parse_json(row["published_summary_json"]) if row["published_summary_json"] else None,
            "review_revision": row["review_revision"],
            "resolved_overall_status": row["resolved_overall_status"],
            "resolved_overall_review_revision": row["resolved_overall_review_revision"],
            "resolved_overall_computed_at": row["resolved_overall_computed_at"],
            "failure": (
                {"code": row["failure_code"], "message": row["failure_message"]}
                if row["failure_code"] is not None
                else None
            ),
            "revision": row["revision"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
        }

    @staticmethod
    def _rule_execution_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "run_id": row["run_id"],
            "plan_order": row["plan_order"],
            "rule_id": row["rule_id"],
            "rule_version": row["rule_version"],
            "execution_state": row["execution_state"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "reason_code": row["reason_code"],
            "reason_detail": _parse_json(row["reason_detail_json"]) if row["reason_detail_json"] else None,
            "finding_count": row["finding_count"],
            "contributes_to_overall": bool(row["contributes_to_overall"]),
            "finding_ids": _parse_json(row["finding_ids_json"]) if row["finding_ids_json"] else None,
        }
