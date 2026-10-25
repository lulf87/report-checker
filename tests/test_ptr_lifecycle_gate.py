from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from mvp.capabilities import MODE_CATALOG, validate_mode
from mvp.run_coordinator import CoordinatorError, execute_persisted_run
from mvp.run_store import RunStore, RunStoreError, _json, _now
from mvp.worker_protocol import JobSpec, WorkerProtocolError


def _insert_legacy_ptr_run(store: RunStore, *, lifecycle: str) -> str:
    """Seed a pre-gate row to exercise defense-in-depth lifecycle checks."""

    case = store.create_case(f"legacy PTR {uuid4().hex[:8]}")
    run_id = f"legacy-ptr-{uuid4().hex}"
    timestamp = _now()
    inputs = {"report_document_id": "report-document", "ptr_document_id": "ptr-document"}
    with store._lock:
        store._connection.execute(
            """INSERT INTO runs(
                id, case_id, mode, lifecycle_status, machine_overall_status,
                inputs_json, planned_rule_ids_json, input_snapshot_json,
                preflight_plan_hash, rule_bundle_id, revision, created_at, updated_at
            ) VALUES(?, ?, 'report_ptr', ?, NULL, ?, ?, NULL, NULL, ?, 1, ?, ?)""",
            (
                run_id,
                case["id"],
                lifecycle,
                _json(inputs),
                _json(["PTR-P01"]),
                "report-checks-mvp-2026-09-30",
                timestamp,
                timestamp,
            ),
        )
        store._connection.execute(
            """INSERT INTO rule_executions(
                id, run_id, plan_order, rule_id, rule_version, execution_state
            ) VALUES(?, ?, 1, 'PTR-P01', '0.1.0-draft', 'pending')""",
            (f"execution-{run_id}", run_id),
        )
        store._append_event_locked(run_id, "run_state_changed", {"from": None, "to": lifecycle})
    return run_id


class PtrLifecycleGateTests(unittest.TestCase):
    def test_ptr_report_alias_is_disabled_at_the_same_worker_boundary(self) -> None:
        for mode in ("report_ptr", "report_ptr_report"):
            with self.subTest(mode=mode):
                with patch.dict(
                    MODE_CATALOG[mode],
                    {"enabled": False, "status": "disabled", "disabled_reason_code": "PTR_NOT_VALIDATED"},
                ):
                    decision = validate_mode(mode, operation="worker_job")
                self.assertFalse(decision["enabled"])
                self.assertEqual(decision["code"], "MODE_DISABLED")
                self.assertEqual(decision["details"]["disabled_reason_code"], "PTR_NOT_VALIDATED")

    def test_worker_job_rejects_disabled_ptr_before_path_or_role_processing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.dict(
                MODE_CATALOG["report_ptr"],
                {"enabled": False, "status": "disabled", "disabled_reason_code": "PTR_NOT_VALIDATED"},
            ), self.assertRaises(WorkerProtocolError) as error:
                JobSpec(
                    run_id="ptr-worker-test",
                    mode="report_ptr",
                    report_path=root / "report.pdf",
                    ptr_path=root / "ptr.pdf",
                    output_dir=root / "output",
                )
        self.assertIn("MODE_DISABLED", str(error.exception))
        self.assertIn("PTR_NOT_VALIDATED", str(error.exception))

    def test_coordinator_rejects_legacy_queued_ptr_without_starting_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = RunStore(root / "state.sqlite3")
            try:
                run_id = _insert_legacy_ptr_run(store, lifecycle="queued")
                output = root / "published"
                with patch.dict(
                    MODE_CATALOG["report_ptr"],
                    {"enabled": False, "status": "disabled", "disabled_reason_code": "PTR_NOT_VALIDATED"},
                ), patch("mvp.run_coordinator.subprocess.run") as worker:
                    with self.assertRaises(CoordinatorError) as error:
                        execute_persisted_run(
                            store,
                            run_id,
                            report_path=root / "report.pdf",
                            ptr_path=root / "ptr.pdf",
                            output_dir=output,
                        )
                worker.assert_not_called()
                self.assertIn("PTR 检查尚未启用", str(error.exception))
                failed = store.get_run(run_id)
                self.assertEqual(failed["lifecycle_status"], "failed")
                self.assertEqual(failed["failure"]["code"], "MODE_DISABLED")
                self.assertFalse(output.exists())
            finally:
                store.close()

    def test_publish_rejects_legacy_running_ptr_and_never_publishes_empty_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = RunStore(Path(temporary) / "state.sqlite3")
            try:
                run_id = _insert_legacy_ptr_run(store, lifecycle="running")
                with patch.dict(
                    MODE_CATALOG["report_ptr"],
                    {"enabled": False, "status": "disabled", "disabled_reason_code": "PTR_NOT_VALIDATED"},
                ), self.assertRaises(RunStoreError) as error:
                    store.publish_result(
                        run_id,
                        {
                            "mode": "report_ptr",
                            "machine_overall_status": "pass",
                            "status_counts": {"pass": 0, "warning": 0, "manual": 0, "error": 0},
                            "findings": [],
                        },
                    )
                self.assertEqual(error.exception.code, "MODE_DISABLED")
                failed = store.get_run(run_id)
                self.assertEqual(failed["lifecycle_status"], "failed")
                self.assertEqual(failed["failure"]["code"], "MODE_DISABLED")
                self.assertEqual(store.list_findings(run_id), [])
            finally:
                store.close()

    def test_transition_to_running_rejects_legacy_ptr_without_changing_state(self) -> None:
        store = RunStore()
        try:
            run_id = _insert_legacy_ptr_run(store, lifecycle="queued")
            with patch.dict(
                MODE_CATALOG["report_ptr"],
                {"enabled": False, "status": "disabled", "disabled_reason_code": "PTR_NOT_VALIDATED"},
            ), self.assertRaises(RunStoreError) as error:
                store.transition_run(run_id, "running")
            self.assertEqual(error.exception.code, "MODE_DISABLED")
            self.assertEqual(store.get_run(run_id)["lifecycle_status"], "queued")
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
