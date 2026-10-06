from __future__ import annotations

import threading
import unittest

from mvp.checker import REPORT_SELF_RULE_ORDER
from mvp.run_coordinator import _fail_running_run
from mvp.run_store import RunStore, RunStoreError


def _report_self_result() -> dict:
    findings = [
        {
            "id": rule_id,
            "rule_id": rule_id,
            "status": "error" if rule_id == "REPORT-R07" else "pass",
        }
        for rule_id in REPORT_SELF_RULE_ORDER
    ]
    return {
        "mode": "report_self",
        "machine_overall_status": "error",
        "status_counts": {"pass": 11, "warning": 0, "manual": 0, "error": 1},
        "findings": findings,
    }


class CancelRaceTests(unittest.TestCase):
    def _running_store(self) -> tuple[RunStore, dict]:
        store = RunStore()
        case = store.create_case("cancel race")
        run = store.create_run(
            case_id=case["id"],
            mode="report_self",
            inputs={"report_document_id": "report"},
        )
        store.transition_run(run["id"], "running")
        for execution in store.get_rule_executions(run["id"]):
            store.transition_rule_execution(
                run["id"], execution["rule_id"], "succeeded", finding_count=1
            )
        return store, run

    def test_worker_failure_closes_a_prior_cancel_request(self) -> None:
        store = RunStore()
        try:
            case = store.create_case("cancelled worker")
            run = store.create_run(
                case_id=case["id"],
                mode="report_self",
                inputs={"report_document_id": "report"},
            )
            store.transition_run(run["id"], "running")
            store.transition_run(
                run["id"],
                "cancel_requested",
                reason_code="USER_CANCEL_REQUESTED",
            )

            _fail_running_run(store, run["id"], "WORKER_TIMEOUT", "timed out")

            closed = store.get_run(run["id"])
            self.assertEqual(closed["lifecycle_status"], "cancelled")
            self.assertEqual(closed["machine_overall_status"], None)
        finally:
            store.close()

    def test_publish_cancel_race_cancels_instead_of_failing(self) -> None:
        store, run = self._running_store()
        try:
            result = _report_self_result()
            original_validate = store._validate_result
            entered = threading.Event()
            release = threading.Event()

            def delayed_validate(run_value, result_value):
                entered.set()
                if not release.wait(5):
                    raise AssertionError("publish validation barrier timed out")
                return original_validate(run_value, result_value)

            store._validate_result = delayed_validate
            outcome: dict[str, object] = {}

            def publish() -> None:
                try:
                    outcome["result"] = store.publish_result(run["id"], result)
                except RunStoreError as error:
                    outcome["error"] = error

            worker = threading.Thread(target=publish)
            worker.start()
            self.assertTrue(entered.wait(5), "publish did not reach validation barrier")
            store.transition_run(
                run["id"],
                "cancel_requested",
                reason_code="USER_CANCEL_REQUESTED",
            )
            release.set()
            worker.join(5)
            self.assertFalse(worker.is_alive(), "publish thread did not finish")

            self.assertIsInstance(outcome.get("error"), RunStoreError)
            self.assertEqual(getattr(outcome["error"], "code", None), "RUN_NOT_PUBLISHABLE")
            self.assertEqual(store.get_run(run["id"])["lifecycle_status"], "cancelled")
            self.assertEqual(store.list_findings(run["id"]), [])
        finally:
            store.close()

    def test_publish_called_after_cancel_request_closes_stale_state(self) -> None:
        store = RunStore()
        try:
            case = store.create_case("stale cancel")
            run = store.create_run(
                case_id=case["id"],
                mode="report_self",
                inputs={"report_document_id": "report"},
            )
            store.transition_run(run["id"], "running")
            store.transition_run(
                run["id"],
                "cancel_requested",
                reason_code="USER_CANCEL_REQUESTED",
            )
            with self.assertRaises(RunStoreError) as error:
                store.publish_result(run["id"], _report_self_result())
            self.assertEqual(error.exception.code, "RUN_NOT_PUBLISHABLE")
            self.assertEqual(store.get_run(run["id"])["lifecycle_status"], "cancelled")
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
