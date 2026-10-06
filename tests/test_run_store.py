from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mvp.run_store import RunStore, RunStoreError, _json, _now
from mvp.checker import REPORT_SELF_RULE_ORDER


class RunStoreTests(unittest.TestCase):
    def test_case_and_run_survive_reopen_with_initial_event(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "state.sqlite3"
            store = RunStore(database)
            case = store.create_case("QW2025-2948 核对任务", "阶段四")
            run = store.create_run(
                case_id=case["id"],
                mode="report_self",
                inputs={"report_document_id": "document-report-1"},
                preflight_plan_hash="plan-hash",
            )
            self.assertEqual(run["lifecycle_status"], "queued")
            self.assertEqual(run["machine_overall_status"], None)
            self.assertIn("REPORT-R07", run["planned_rule_ids"])
            self.assertEqual(run["rule_bundle_id"], "report-checks-mvp-2026-09-30")
            self.assertEqual([event["type"] for event in store.list_events(run["id"])], ["run_state_changed"])
            store.close()

            reopened = RunStore(database)
            self.assertEqual(reopened.get_case(case["id"])["run_count"], 1)
            self.assertEqual(reopened.get_run(run["id"])["inputs"], {"report_document_id": "document-report-1"})
            reopened.close()

    def test_mode_and_input_contracts_are_enforced_before_insert(self) -> None:
        store = RunStore()
        case = store.create_case("case")
        with self.assertRaises(RunStoreError) as disabled:
            store.create_run(
                case_id=case["id"],
                mode="report_ptr",
                inputs={"report_document_id": "r", "ptr_document_id": "p"},
            )
        self.assertEqual(disabled.exception.code, "MODE_DISABLED")
        with self.assertRaises(RunStoreError) as extra:
            store.create_run(
                case_id=case["id"],
                mode="report_self",
                inputs={"report_document_id": "r", "record_document_id": "x"},
            )
        self.assertEqual(extra.exception.code, "INVALID_RUN_INPUTS")
        self.assertEqual(store.list_runs(case["id"]), [])
        store.close()

    def test_transition_matrix_and_machine_status_visibility(self) -> None:
        store = RunStore()
        case = store.create_case("case")
        run = store.create_run(
            case_id=case["id"], mode="report_self", inputs={"report_document_id": "r"}
        )
        running = store.transition_run(run["id"], "running")
        self.assertIsNotNone(running["started_at"])
        succeeded = store.transition_run(run["id"], "succeeded", machine_overall_status="error")
        self.assertEqual(succeeded["machine_overall_status"], "error")
        self.assertIsNotNone(succeeded["finished_at"])
        with self.assertRaises(RunStoreError) as terminal:
            store.transition_run(run["id"], "running")
        self.assertEqual(terminal.exception.code, "INVALID_RUN_TRANSITION")
        self.assertEqual([event["sequence"] for event in store.list_events(run["id"])], [1, 2, 3])
        store.close()

    def test_queued_cancel_and_restart_recovery_are_distinct_paths(self) -> None:
        store = RunStore()
        case = store.create_case("case")
        queued = store.create_run(
            case_id=case["id"], mode="report_self", inputs={"report_document_id": "r1"}
        )
        cancelled = store.transition_run(queued["id"], "cancelled")
        self.assertEqual(cancelled["lifecycle_status"], "cancelled")
        self.assertIsNone(cancelled["machine_overall_status"])

        running = store.create_run(
            case_id=case["id"], mode="report_self", inputs={"report_document_id": "r2"}
        )
        store.transition_run(running["id"], "running")
        recovered = store.recover_interrupted_runs()
        self.assertEqual([item["id"] for item in recovered], [running["id"]])
        recovered_run = store.get_run(running["id"])
        self.assertEqual(recovered_run["lifecycle_status"], "interrupted")
        self.assertEqual(recovered_run["failure"]["code"], "RUN_INTERRUPTED_ON_RESTART")
        self.assertIsNone(recovered_run["machine_overall_status"])
        self.assertEqual(store.recover_interrupted_runs(), [])
        store.close()

    def test_list_queued_runs_excludes_cancelled_and_running_jobs(self) -> None:
        store = RunStore()
        case = store.create_case("queued listing")
        queued = store.create_run(
            case_id=case["id"], mode="report_self", inputs={"report_document_id": "queued"}
        )
        running = store.create_run(
            case_id=case["id"], mode="report_self", inputs={"report_document_id": "running"}
        )
        store.transition_run(running["id"], "running")
        cancelled = store.create_run(
            case_id=case["id"], mode="report_self", inputs={"report_document_id": "cancelled"}
        )
        store.transition_run(cancelled["id"], "cancelled")
        self.assertEqual([item["id"] for item in store.list_queued_runs()], [queued["id"]])
        store.close()

    def test_recovery_quarantines_a_legacy_disabled_mode(self) -> None:
        store = RunStore()
        case = store.create_case("legacy ptr")
        timestamp = _now()
        with store._lock:
            store._connection.execute(
                """INSERT INTO runs(
                    id, case_id, mode, lifecycle_status, inputs_json,
                    planned_rule_ids_json, revision, created_at, updated_at
                ) VALUES(?, ?, 'report_ptr', 'running', ?, ?, 1, ?, ?)""",
                (
                    "legacy-ptr-run",
                    case["id"],
                    _json({"report_document_id": "r", "ptr_document_id": "p"}),
                    _json(["PTR-P01"]),
                    timestamp,
                    timestamp,
                ),
            )
        recovered = store.recover_interrupted_runs()
        self.assertEqual(recovered[0]["lifecycle_status"], "interrupted")
        self.assertEqual(store.get_run("legacy-ptr-run")["failure"]["code"], "MODE_DISABLED")
        store.close()

    def test_cancel_requested_can_finish_cancelled_but_not_succeed(self) -> None:
        store = RunStore()
        case = store.create_case("case")
        run = store.create_run(
            case_id=case["id"], mode="report_record_9706_1",
            inputs={"report_document_id": "r", "record_document_id": "record"},
        )
        store.transition_run(run["id"], "running")
        requested = store.transition_run(run["id"], "cancel_requested")
        self.assertEqual(requested["lifecycle_status"], "cancel_requested")
        with self.assertRaises(RunStoreError):
            store.transition_run(run["id"], "succeeded", machine_overall_status="pass")
        cancelled = store.transition_run(run["id"], "cancelled")
        self.assertEqual(cancelled["lifecycle_status"], "cancelled")
        store.close()

    def test_rule_execution_ledger_and_atomic_publish_summary(self) -> None:
        store = RunStore()
        case = store.create_case("case")
        run = store.create_run(
            case_id=case["id"], mode="report_self", inputs={"report_document_id": "r"}
        )
        self.assertEqual(len(store.get_rule_executions(run["id"])), len(REPORT_SELF_RULE_ORDER))
        store.transition_run(run["id"], "running")
        for rule in store.get_rule_executions(run["id"]):
            store.transition_rule_execution(run["id"], rule["rule_id"], "running")
            store.transition_rule_execution(run["id"], rule["rule_id"], "succeeded", finding_count=1)
        result = {
            "schema_version": "report-self-1.0",
            "mode": "report_self",
            "lifecycle_status": "succeeded",
            "machine_overall_status": "error",
            "status_counts": {"pass": 11, "warning": 0, "manual": 0, "error": 1},
            "coverage": {"planned": 12, "completed": 12},
            "findings": [
                {"id": rule_id, "rule_id": rule_id, "status": "error" if rule_id == "REPORT-R07" else "pass"}
                for rule_id in REPORT_SELF_RULE_ORDER
            ],
        }
        published = store.publish_result(run["id"], result)
        self.assertEqual(published["lifecycle_status"], "succeeded")
        self.assertEqual(published["finding_counts"]["error"], 1)
        self.assertEqual(published["published_summary"]["mode"], "report_self")
        self.assertEqual(store.list_events(run["id"])[-1]["type"], "run_state_changed")
        with self.assertRaises(RunStoreError) as duplicate:
            store.publish_result(run["id"], result)
        self.assertEqual(duplicate.exception.code, "RUN_NOT_PUBLISHABLE")
        self.assertEqual(len(store.list_findings(run["id"])), len(REPORT_SELF_RULE_ORDER))
        store.close()

    def test_repeated_report_self_runs_scope_colliding_finding_ids(self) -> None:
        store = RunStore()
        case = store.create_case("重复运行 ID")

        timestamp = _now()
        with store._lock:
            store._connection.execute(
                "INSERT INTO blobs(id, sha256, size_bytes, media_type, storage_relpath, created_at) VALUES(?, ?, ?, ?, ?, ?)",
                ("blob-repeated", "a" * 64, 1, "application/pdf", "aa/" + "a" * 64 + ".pdf", timestamp),
            )
            for document_id in ("r1", "r2"):
                store._connection.execute(
                    """INSERT INTO documents(
                        id, case_id, blob_id, role, original_filename, page_count, pdf_version,
                        encrypted, preflight_status, preflight_json, created_at
                    ) VALUES(?, ?, ?, 'report', ?, 1, '1.7', 0, 'passed', ?, ?)""",
                    (document_id, case["id"], "blob-repeated", document_id + ".pdf",
                     _json({"status": "passed", "page_count": 1, "page_geometry": [{"page_width": 612.0, "page_height": 792.0, "rotation": 0}]}), timestamp),
                )

        def publish_one(run_id: str) -> dict:
            store.transition_run(run_id, "running")
            for execution in store.get_rule_executions(run_id):
                store.transition_rule_execution(run_id, execution["rule_id"], "running")
                store.transition_rule_execution(run_id, execution["rule_id"], "succeeded", finding_count=1)
            return store.publish_result(
                run_id,
                {
                    "mode": "report_self",
                    "machine_overall_status": "error",
                    "status_counts": {"pass": 11, "warning": 0, "manual": 0, "error": 1},
                    "findings": [
                        {
                            "id": rule_id,
                            "rule_id": rule_id,
                            "status": "error" if rule_id == "REPORT-R07" else "pass",
                            "evidence_locations": (
                                [{"role": "report", "pdf_page": 1, "bbox": [0, 0, 10, 10]}]
                                if rule_id == "REPORT-R01"
                                else []
                            ),
                        }
                        for rule_id in REPORT_SELF_RULE_ORDER
                    ],
                },
            )

        def snapshot(document_id: str) -> dict:
            return {
                "report": {
                    "document_id": document_id,
                    "blob_sha256": "a" * 64,
                    "page_count": 1,
                    "page_geometry": [{"page_width": 612.0, "page_height": 792.0, "rotation": 0}],
                }
            }

        first = store.create_run(
            case_id=case["id"], mode="report_self", inputs={"report_document_id": "r1"},
            input_snapshot=snapshot("r1"),
        )
        second = store.create_run(
            case_id=case["id"], mode="report_self", inputs={"report_document_id": "r2"},
            input_snapshot=snapshot("r2"),
        )
        self.assertEqual(publish_one(first["id"])["lifecycle_status"], "succeeded")
        self.assertEqual(publish_one(second["id"])["lifecycle_status"], "succeeded")

        first_ids = {item["id"] for item in store.list_findings(first["id"])}
        second_findings = store.list_findings(second["id"])
        second_ids = [item["id"] for item in second_findings]
        self.assertEqual(first_ids, set(REPORT_SELF_RULE_ORDER))
        self.assertTrue(all(item.endswith(f"@{second['id']}") for item in second_ids))
        self.assertEqual(len(set(second_ids)), len(REPORT_SELF_RULE_ORDER))
        second_executions = store.get_rule_executions(second["id"])
        self.assertTrue(all(execution["finding_ids"] == [f"{execution['rule_id']}@{second['id']}"] for execution in second_executions))
        second_r01_id = f"REPORT-R01@{second['id']}"
        with store._lock:
            evidence_row = store._connection.execute(
                "SELECT finding_id FROM evidence WHERE run_id = ?", (second["id"],)
            ).fetchone()
        self.assertIsNotNone(evidence_row)
        self.assertEqual(evidence_row["finding_id"], second_r01_id)
        published_event = store.list_events(second["id"])[-2]
        self.assertEqual(published_event["type"], "findings_published")
        self.assertEqual(set(published_event["payload"]["finding_ids"]), set(second_ids))
        store.close()

    def test_publish_gate_marks_run_failed_when_rule_execution_is_pending(self) -> None:
        store = RunStore()
        case = store.create_case("case")
        run = store.create_run(
            case_id=case["id"], mode="report_self", inputs={"report_document_id": "r"}
        )
        store.transition_run(run["id"], "running")
        result = {
            "mode": "report_self",
            "machine_overall_status": "pass",
            "status_counts": {"pass": 0, "warning": 0, "manual": 0, "error": 0},
            "findings": [],
        }
        with self.assertRaises(RunStoreError) as error:
            store.publish_result(run["id"], result)
        self.assertEqual(error.exception.code, "RULE_EXECUTION_INCOMPLETE")
        failed = store.get_run(run["id"])
        self.assertEqual(failed["lifecycle_status"], "failed")
        self.assertIsNone(failed["machine_overall_status"])
        self.assertEqual(failed["failure"]["code"], "RULE_EXECUTION_INCOMPLETE")
        store.close()

    def test_publish_gate_rejects_rule_finding_count_mismatch(self) -> None:
        store = RunStore()
        case = store.create_case("count mismatch")
        run = store.create_run(
            case_id=case["id"], mode="report_self", inputs={"report_document_id": "r"}
        )
        store.transition_run(run["id"], "running")
        for rule in store.get_rule_executions(run["id"]):
            store.transition_rule_execution(run["id"], rule["rule_id"], "running")
            store.transition_rule_execution(
                run["id"], rule["rule_id"], "succeeded", finding_count=0
            )
        with self.assertRaises(RunStoreError) as error:
            store.publish_result(
                run["id"],
                {
                    "mode": "report_self",
                    "machine_overall_status": "pass",
                    "status_counts": {"pass": 1, "warning": 0, "manual": 0, "error": 0},
                    "findings": [{"id": "REPORT-R01-1", "rule_id": "REPORT-R01", "status": "pass"}],
                },
            )
        self.assertEqual(error.exception.code, "RULE_FINDING_COUNT_MISMATCH")
        self.assertEqual(store.get_run(run["id"])["lifecycle_status"], "failed")
        store.close()

    def test_publish_gate_rejects_succeeded_rule_without_finding(self) -> None:
        store = RunStore()
        case = store.create_case("missing finding")
        run = store.create_run(
            case_id=case["id"], mode="report_self", inputs={"report_document_id": "r"}
        )
        store.transition_run(run["id"], "running")
        for execution in store.get_rule_executions(run["id"]):
            store.transition_rule_execution(run["id"], execution["rule_id"], "running")
            store.transition_rule_execution(
                run["id"], execution["rule_id"], "succeeded", finding_count=0
            )
        with self.assertRaises(RunStoreError) as error:
            store.publish_result(
                run["id"],
                {
                    "mode": "report_self",
                    "machine_overall_status": "pass",
                    "status_counts": {"pass": 0, "warning": 0, "manual": 0, "error": 0},
                    "findings": [],
                },
            )
        self.assertEqual(error.exception.code, "RULE_EXECUTION_NO_FINDING")
        self.assertEqual(store.get_run(run["id"])["lifecycle_status"], "failed")
        self.assertEqual(store.list_findings(run["id"]), [])
        store.close()

    def test_publish_gate_allows_explicit_non_applicable_rules_without_findings(self) -> None:
        store = RunStore()
        case = store.create_case("explicit rule disposition")
        run = store.create_run(
            case_id=case["id"], mode="report_self", inputs={"report_document_id": "r"}
        )
        store.transition_run(run["id"], "running")
        executions = store.get_rule_executions(run["id"])
        for index, execution in enumerate(executions):
            store.transition_rule_execution(run["id"], execution["rule_id"], "running")
            if index == 0:
                store.transition_rule_execution(
                    run["id"], execution["rule_id"], "succeeded", finding_count=1
                )
            else:
                store.transition_rule_execution(
                    run["id"],
                    execution["rule_id"],
                    "not_applicable",
                    reason_code=f"FIXTURE_{execution['rule_id']}_NOT_APPLICABLE",
                )
        published = store.publish_result(
            run["id"],
            {
                "mode": "report_self",
                "machine_overall_status": "pass",
                "status_counts": {"pass": 1, "warning": 0, "manual": 0, "error": 0},
                "findings": [
                    {"id": "REPORT-R01-FIXTURE", "rule_id": executions[0]["rule_id"], "status": "pass"}
                ],
            },
        )
        self.assertEqual(published["lifecycle_status"], "succeeded")
        self.assertEqual(published["finding_counts"]["pass"], 1)
        store.close()

    def test_published_findings_evidence_and_review_are_auditable(self) -> None:
        store = RunStore()
        case = store.create_case("case")
        timestamp = _now()
        with store._lock:
            store._connection.execute(
                "INSERT INTO blobs(id, sha256, size_bytes, media_type, storage_relpath, created_at) VALUES(?, ?, ?, ?, ?, ?)",
                ("blob-review", "a" * 64, 1, "application/pdf", "aa/" + "a" * 64 + ".pdf", timestamp),
            )
            store._connection.execute(
                """INSERT INTO documents(
                    id, case_id, blob_id, role, original_filename, page_count, pdf_version,
                    encrypted, preflight_status, preflight_json, created_at
                ) VALUES(?, ?, ?, 'report', 'report.pdf', 1, '1.7', 0, 'passed', ?, ?)""",
                ("report", case["id"], "blob-review",
                 _json({"status": "passed", "page_count": 1, "page_geometry": [{"page_width": 612.0, "page_height": 792.0, "rotation": 0}]}), timestamp),
            )
        run = store.create_run(
            case_id=case["id"],
            mode="report_self",
            inputs={"report_document_id": "report"},
                input_snapshot={
                    "report": {
                        "document_id": "report",
                        "blob_sha256": "a" * 64,
                        "page_count": 1,
                        "page_geometry": [{"page_width": 612.0, "page_height": 792.0, "rotation": 0}],
                    }
                },
        )
        store.transition_run(run["id"], "running")
        for rule in store.get_rule_executions(run["id"]):
            store.transition_rule_execution(run["id"], rule["rule_id"], "running")
            if rule["rule_id"] == "REPORT-R01":
                store.transition_rule_execution(
                    run["id"], rule["rule_id"], "succeeded", finding_count=1
                )
            else:
                store.transition_rule_execution(
                    run["id"],
                    rule["rule_id"],
                    "not_applicable",
                    reason_code=f"FIXTURE_{rule['rule_id']}_NOT_APPLICABLE",
                )
        store.publish_result(
            run["id"],
            {
                "mode": "report_self",
                "machine_overall_status": "manual",
                "status_counts": {"pass": 0, "warning": 0, "manual": 1, "error": 0},
                "findings": [
                    {
                        "id": "REPORT-R01-MANUAL",
                        "rule_id": "REPORT-R01",
                        "status": "manual",
                        "details": {"contributes_to_overall": True},
                        "evidence_locations": [{"role": "report", "pdf_page": 1, "bbox": [0, 0, 10, 10]}],
                    }
                ],
            },
        )
        finding = store.list_findings(run["id"])[0]
        self.assertEqual(finding["review_status"], "pending")
        self.assertEqual(finding["input_snapshot"]["report"]["blob_sha256"], "a" * 64)
        self.assertTrue(finding["rule_execution_id"])
        self.assertEqual(len(finding["evidence"]), 1)
        self.assertEqual(finding["evidence"][0]["run_id"], run["id"])
        self.assertEqual(finding["evidence"][0]["rule_execution_id"], finding["rule_execution_id"])
        self.assertEqual(finding["evidence"][0]["document_sha256"], "a" * 64)
        reviewed = store.append_review_action(
            finding["id"],
            "confirm_candidate",
            actor_id="tester",
            observation={"candidate_id": "candidate-1", "confirmation": True},
        )
        self.assertEqual(reviewed["review_revision"], 1)
        self.assertEqual(reviewed["finding"]["review_resolution"], "consistent")
        self.assertEqual(reviewed["finding"]["resolved_status"], "warning")
        self.assertEqual(store.get_run(run["id"])["resolved_overall_status"], "warning")
        with self.assertRaises(RunStoreError) as readonly:
            store.append_review_action(
                finding["id"],
                "record_observation",
                base_review_revision=1,
                observation={"resolved_status": "pass"},
            )
        self.assertEqual(readonly.exception.code, "READ_ONLY_FIELD_SUBMITTED")
        withdrawn = store.append_review_action(
            finding["id"],
            "withdraw",
            base_review_revision=1,
            observation={"review_action_id": reviewed["action"]["id"]},
        )
        self.assertEqual(withdrawn["finding"]["review_status"], "pending")
        self.assertIsNone(withdrawn["finding"]["resolved_status"])
        self.assertEqual(store.get_run(run["id"])["resolved_overall_status"], "manual")
        with self.assertRaises(RunStoreError) as repeated_withdraw:
            store.append_review_action(
                finding["id"],
                "withdraw",
                base_review_revision=2,
                observation={"review_action_id": reviewed["action"]["id"]},
            )
        self.assertEqual(repeated_withdraw.exception.code, "REVIEW_ACTION_HAS_DEPENDENTS")
        second_review = store.append_review_action(
            finding["id"],
            "confirm_candidate",
            base_review_revision=2,
            observation={"candidate_id": "candidate-2", "confirmation": True},
        )
        self.assertEqual(second_review["review_revision"], 3)
        with self.assertRaises(RunStoreError) as conflict:
            store.append_review_action(
                finding["id"],
                "withdraw",
                base_review_revision=2,
                observation={"review_action_id": second_review["action"]["id"]},
            )
        self.assertEqual(conflict.exception.code, "REVIEW_REVISION_CONFLICT")
        store.close()


if __name__ == "__main__":
    unittest.main()
