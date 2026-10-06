from __future__ import annotations

import gc
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from mvp.run_store import RunStore, RunStoreError


NOW = "2026-10-05T00:00:00+00:00"
CASE_ID = "case-v6"
RUN_ID = "run-v6"
DOC_ID = "doc-v6"
BLOB_ID = "blob-v6"
FINDING_ID = "finding-v6"
EXECUTION_ID = "execution-v6"
EVIDENCE_ID = "evidence-v6"
ACTION_ID = "action-v6"
SHA256 = "a" * 64
SNAPSHOT = {
    "report": {
        "document_id": DOC_ID,
        "blob_sha256": SHA256,
        "size_bytes": 10,
        "media_type": "application/pdf",
        "page_count": 1,
        "page_geometry": [
            {
                "page_index": 0,
                "page_number": 1,
                "page_width": 612.0,
                "page_height": 792.0,
                "rotation": 0,
            }
        ],
    }
}


def _create_v6_database(path: Path, *, schema_version: int | None = 6, malformed: bool = False) -> None:
    """Create the pre-document-binding v6 shape without using RunStore."""

    connection = sqlite3.connect(path)
    connection.executescript(
        """
        PRAGMA foreign_keys = ON;
        CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE cases (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT,
            revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE runs (
            id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id), mode TEXT NOT NULL,
            lifecycle_status TEXT NOT NULL, machine_overall_status TEXT,
            inputs_json TEXT NOT NULL, planned_rule_ids_json TEXT NOT NULL DEFAULT '[]',
            input_snapshot_json TEXT, preflight_plan_hash TEXT, rule_bundle_id TEXT,
            finding_counts_json TEXT, published_summary_json TEXT,
            review_revision INTEGER NOT NULL DEFAULT 0, resolved_overall_status TEXT,
            resolved_overall_review_revision INTEGER, resolved_overall_computed_at TEXT,
            failure_code TEXT, failure_message TEXT, revision INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, started_at TEXT, finished_at TEXT
        );
        CREATE TABLE rule_executions (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            plan_order INTEGER NOT NULL, rule_id TEXT NOT NULL, rule_version TEXT,
            execution_state TEXT NOT NULL, started_at TEXT, finished_at TEXT,
            reason_code TEXT, reason_detail_json TEXT, finding_count INTEGER NOT NULL DEFAULT 0,
            contributes_to_overall INTEGER NOT NULL DEFAULT 1,
            UNIQUE(run_id, plan_order), UNIQUE(run_id, rule_id)
        );
        CREATE TABLE blobs (
            id TEXT PRIMARY KEY, sha256 TEXT NOT NULL UNIQUE, size_bytes INTEGER NOT NULL,
            media_type TEXT NOT NULL, storage_relpath TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL
        );
        CREATE TABLE documents (
            id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
            blob_id TEXT NOT NULL REFERENCES blobs(id), role TEXT NOT NULL,
            original_filename TEXT NOT NULL, page_count INTEGER NOT NULL, pdf_version TEXT,
            encrypted INTEGER NOT NULL DEFAULT 0, preflight_status TEXT NOT NULL,
            preflight_json TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE run_inputs (
            run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            role TEXT NOT NULL, document_id TEXT NOT NULL REFERENCES documents(id),
            blob_sha256 TEXT NOT NULL, PRIMARY KEY (run_id, role), UNIQUE(run_id, document_id)
        );
        CREATE TABLE findings (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            display_sequence INTEGER NOT NULL, rule_id TEXT NOT NULL,
            machine_status TEXT NOT NULL, title TEXT, summary TEXT, details_json TEXT NOT NULL,
            review_status TEXT NOT NULL DEFAULT 'not_required', review_resolution TEXT,
            resolved_status TEXT, created_at TEXT NOT NULL,
            UNIQUE(run_id, display_sequence), UNIQUE(run_id, id)
        );
        CREATE TABLE evidence (
            id TEXT PRIMARY KEY, finding_id TEXT NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
            role TEXT NOT NULL, pdf_page INTEGER NOT NULL, bbox_json TEXT NOT NULL,
            semantic_role TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE review_actions (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            finding_id TEXT NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
            action_type TEXT NOT NULL, base_run_review_revision INTEGER NOT NULL,
            observation_json TEXT, note TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE run_events (
            run_id TEXT NOT NULL REFERENCES runs(id), sequence INTEGER NOT NULL,
            event_type TEXT NOT NULL, payload_json TEXT NOT NULL, occurred_at TEXT NOT NULL,
            PRIMARY KEY (run_id, sequence)
        );
        """
    )
    if schema_version is not None:
        connection.execute("INSERT INTO schema_meta(key, value) VALUES('schema_version', ?)", (str(schema_version),))
    connection.execute("INSERT INTO cases VALUES(?, ?, ?, 1, ?, ?)", (CASE_ID, "v6 case", None, NOW, NOW))
    snapshot = dict(SNAPSHOT)
    if malformed:
        snapshot["report"] = dict(snapshot["report"])
        snapshot["report"]["document_id"] = "missing-document"
    connection.execute(
        """INSERT INTO runs(
            id, case_id, mode, lifecycle_status, machine_overall_status,
            inputs_json, planned_rule_ids_json, input_snapshot_json,
            revision, created_at, updated_at, finished_at
        ) VALUES(?, ?, 'report_self', 'succeeded', 'pass', ?, ?, ?, 1, ?, ?, ?)""",
        (RUN_ID, CASE_ID, json.dumps({"report_document_id": DOC_ID}), json.dumps(["REPORT-R01"]), json.dumps(snapshot), NOW, NOW, NOW),
    )
    connection.execute(
        "INSERT INTO blobs VALUES(?, ?, 10, 'application/pdf', 'aa/file.pdf', ?)",
        (BLOB_ID, SHA256, NOW),
    )
    connection.execute(
        """INSERT INTO documents VALUES(?, ?, ?, 'report', 'report.pdf', 1, '1.7', 0, 'passed', ?, ?)""",
        (DOC_ID, CASE_ID, BLOB_ID, json.dumps({"status": "passed", "page_count": 1, "page_geometry": SNAPSHOT["report"]["page_geometry"]}), NOW),
    )
    connection.execute(
        """INSERT INTO rule_executions(
            id, run_id, plan_order, rule_id, rule_version, execution_state,
            started_at, finished_at, finding_count
        ) VALUES(?, ?, 1, 'REPORT-R01', 'v1', 'succeeded', ?, ?, 1)""",
        (EXECUTION_ID, RUN_ID, NOW, NOW),
    )
    connection.execute(
        """INSERT INTO findings(
            id, run_id, display_sequence, rule_id, machine_status, title, summary,
            details_json, review_status, created_at
        ) VALUES(?, ?, 1, 'REPORT-R01', 'pass', 'title', 'summary', '{}', 'not_required', ?)""",
        (FINDING_ID, RUN_ID, NOW),
    )
    connection.execute(
        """INSERT INTO evidence(id, finding_id, role, pdf_page, bbox_json, semantic_role, created_at)
           VALUES(?, ?, 'report', 1, '[1,2,3,4]', 'source_observation', ?)""",
        (EVIDENCE_ID, FINDING_ID, NOW),
    )
    connection.execute(
        """INSERT INTO review_actions(
            id, run_id, finding_id, action_type, base_run_review_revision,
            observation_json, note, created_at
        ) VALUES(?, ?, ?, 'record_observation', 0, '{}', NULL, ?)""",
        (ACTION_ID, RUN_ID, FINDING_ID, NOW),
    )
    connection.commit()
    connection.close()


class SchemaMigrationTests(unittest.TestCase):
    def test_v6_upgrade_backfills_traceability_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "v6.sqlite3"
            _create_v6_database(database)
            store = RunStore(database)
            current = store.schema_version
            self.assertGreaterEqual(current, 7)
            finding = store.list_findings(RUN_ID)[0]
            self.assertEqual(finding["rule_execution_id"], EXECUTION_ID)
            self.assertEqual(finding["input_snapshot"]["report"]["document_id"], DOC_ID)
            evidence = finding["evidence"][0]
            self.assertEqual(evidence["run_id"], RUN_ID)
            self.assertEqual(evidence["rule_execution_id"], EXECUTION_ID)
            self.assertEqual(evidence["document_id"], DOC_ID)
            self.assertEqual(evidence["document_sha256"], SHA256)
            self.assertEqual(evidence["input_snapshot"]["report"]["page_geometry"][0]["page_width"], 612.0)
            self.assertEqual(store.get_run(RUN_ID)["review_revision"], 1)
            store.close()

            reopened = RunStore(database)
            with reopened._lock:
                version = reopened._connection.execute(
                    "SELECT value FROM schema_meta WHERE key='schema_version'"
                ).fetchone()["value"]
                migrations = reopened._connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                ).fetchall()
            self.assertEqual(int(version), reopened.schema_version)
            self.assertEqual(
                [row["version"] for row in migrations],
                list(range(7, reopened.schema_version + 1)),
            )
            reopened.close()

    def test_future_schema_version_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "future.sqlite3"
            _create_v6_database(database, schema_version=RunStore.schema_version + 1)
            with self.assertRaises(RunStoreError) as error:
                RunStore(database)
            self.assertEqual(error.exception.code, "UNSUPPORTED_SCHEMA_VERSION")

    def test_missing_schema_version_with_data_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "missing.sqlite3"
            _create_v6_database(database, schema_version=None)
            with self.assertRaises(RunStoreError) as error:
                RunStore(database)
            self.assertEqual(error.exception.code, "SCHEMA_VERSION_MISSING")

    def test_failed_v6_migration_rolls_back_and_preserves_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "broken.sqlite3"
            _create_v6_database(database, malformed=True)
            with self.assertRaises(RunStoreError) as error:
                RunStore(database)
            self.assertEqual(error.exception.code, "SCHEMA_MIGRATION_INTEGRITY_ERROR")
            gc.collect()
            connection = sqlite3.connect(database)
            version = connection.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0]
            columns = {row[1] for row in connection.execute("PRAGMA table_info(evidence)")}
            migrations = connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
            connection.close()
            self.assertEqual(version, "6")
            self.assertNotIn("document_id", columns)
            self.assertEqual(migrations, 0)


if __name__ == "__main__":
    unittest.main()
