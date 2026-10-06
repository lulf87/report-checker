from __future__ import annotations

import unittest

from mvp.run_store import RunStore, RunStoreError


class ReviewActionValidationTests(unittest.TestCase):
    def _manual_store(self) -> tuple[RunStore, list[dict]]:
        store = RunStore()
        case = store.create_case("review validation")
        run = store.create_run(
            case_id=case["id"],
            mode="report_self",
            inputs={"report_document_id": "report"},
        )
        store.transition_run(run["id"], "running")
        executions = store.get_rule_executions(run["id"])
        for index, execution in enumerate(executions):
            store.transition_rule_execution(run["id"], execution["rule_id"], "running")
            if index == 0:
                store.transition_rule_execution(
                    run["id"], execution["rule_id"], "succeeded", finding_count=2
                )
            else:
                store.transition_rule_execution(
                    run["id"],
                    execution["rule_id"],
                    "not_applicable",
                    reason_code=f"FIXTURE_{execution['rule_id']}_NA",
                )
        store.publish_result(
            run["id"],
            {
                "mode": "report_self",
                "machine_overall_status": "manual",
                "status_counts": {"pass": 0, "warning": 0, "manual": 2, "error": 0},
                "findings": [
                    {"id": "manual-1", "rule_id": executions[0]["rule_id"], "status": "manual"},
                    {"id": "manual-2", "rule_id": executions[0]["rule_id"], "status": "manual"},
                ],
            },
        )
        return store, store.list_findings(run["id"])

    def test_action_specific_observations_are_required(self) -> None:
        store, findings = self._manual_store()
        try:
            finding_id = findings[0]["id"]
            for action_type in ("record_observation", "mark_source_unreadable"):
                with self.assertRaises(RunStoreError) as error:
                    store.append_review_action(finding_id, action_type, observation={})
                self.assertEqual(error.exception.code, "INVALID_REVIEW_OBSERVATION")
            with self.assertRaises(RunStoreError) as missing_candidate:
                store.append_review_action(
                    finding_id,
                    "confirm_candidate",
                    observation={"confirmation": True},
                )
            self.assertEqual(missing_candidate.exception.code, "REVIEW_INPUT_SCHEMA_MISMATCH")
            with self.assertRaises(RunStoreError) as missing_confirmation:
                store.append_review_action(
                    finding_id,
                    "confirm_candidate",
                    observation={"candidate_id": "candidate-1", "confirmation": False},
                )
            self.assertEqual(missing_confirmation.exception.code, "REVIEW_INPUT_SCHEMA_MISMATCH")
            self.assertEqual(store.get_run(findings[0]["run_id"])["review_revision"], 0)
        finally:
            store.close()

    def test_withdraw_requires_current_action_for_same_finding(self) -> None:
        store, findings = self._manual_store()
        try:
            first = store.append_review_action(
                findings[0]["id"],
                "confirm_candidate",
                observation={"candidate_id": "candidate-1", "confirmation": True},
            )
            action_id = first["action"]["id"]
            with self.assertRaises(RunStoreError) as missing_target:
                store.append_review_action(
                    findings[0]["id"],
                    "withdraw",
                    base_review_revision=1,
                    observation={"review_action_id": "does-not-exist"},
                )
            self.assertEqual(missing_target.exception.code, "REVIEW_ACTION_NOT_FOUND")
            with self.assertRaises(RunStoreError) as wrong_finding:
                store.append_review_action(
                    findings[1]["id"],
                    "withdraw",
                    base_review_revision=1,
                    observation={"review_action_id": action_id},
                )
            self.assertEqual(wrong_finding.exception.code, "REVIEW_ACTION_NOT_FOUND")
            withdrawn = store.append_review_action(
                findings[0]["id"],
                "withdraw",
                base_review_revision=1,
                observation={"review_action_id": action_id},
            )
            self.assertEqual(withdrawn["finding"]["review_status"], "pending")
            with self.assertRaises(RunStoreError) as repeated:
                store.append_review_action(
                    findings[0]["id"],
                    "withdraw",
                    base_review_revision=2,
                    observation={"review_action_id": action_id},
                )
            self.assertEqual(repeated.exception.code, "REVIEW_ACTION_HAS_DEPENDENTS")
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
