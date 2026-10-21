from __future__ import annotations

import json
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from mvp.checker import REPORT_SELF_RULE_ORDER
from mvp.review_notes import ReviewNoteError, ReviewNoteStore
from mvp.run_state_server import create_server


class ReviewNoteStoreTests(unittest.TestCase):
    def test_latest_decision_wins_and_cleared_hides(self) -> None:
        store = ReviewNoteStore()
        store.add(subject="QW2024-3944", mode="report_self", finding_key="REPORT-R02@run1", decision="is_problem", note="", run_id="run1")
        store.add(subject="QW2024-3944", mode="report_self", finding_key="REPORT-R02@run1", decision="not_problem", note="一次性附件，无标签", run_id="run1")
        store.add(subject="QW2024-3944", mode="report_self", finding_key="REPORT-R06", decision="notified", note="", run_id="run1")
        latest = store.latest(subject="QW2024-3944", mode="report_self")
        self.assertEqual(latest["REPORT-R02"]["decision"], "not_problem")
        self.assertEqual(latest["REPORT-R02"]["note"], "一次性附件，无标签")
        store.add(subject="QW2024-3944", mode="report_self", finding_key="REPORT-R06", decision="cleared", note="", run_id="run2")
        self.assertNotIn("REPORT-R06", store.latest(subject="QW2024-3944", mode="report_self"))
        self.assertEqual(store.latest(subject="QW2024-3944", mode="report_record_9706_1"), {})

    def test_invalid_input_is_rejected(self) -> None:
        store = ReviewNoteStore()
        with self.assertRaises(ReviewNoteError):
            store.add(subject="s", mode="m", finding_key="X", decision="approve", note="", run_id="r")
        with self.assertRaises(ReviewNoteError):
            store.add(subject="s", mode="m", finding_key="X", decision="notified", note="字" * 2001, run_id="r")


class ReviewNoteApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = create_server(port=0, allow_unresolved_inputs=True)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"
        with urlopen(cls.base_url + "/api/v1/session", timeout=5) as response:
            cls.csrf = json.loads(response.read())["csrf_token"]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def request_json(self, path: str, *, method: str = "GET", payload: dict | None = None):
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json", "X-CSRF-Token": self.csrf} if body is not None else {}
        with urlopen(Request(self.base_url + path, method=method, data=body, headers=headers), timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def make_run(self, name: str) -> dict:
        store = self.server.store
        case = store.create_case(name)
        run = store.create_run(
            case_id=case["id"],
            mode="report_self",
            inputs={"report_document_id": f"{name}-report"},
            input_snapshot={"report": {"document_id": f"{name}-report", "blob_sha256": "b" * 64, "page_count": 1}},
        )
        store.transition_run(run["id"], "running")
        for execution in store.get_rule_executions(run["id"]):
            store.transition_rule_execution(run["id"], execution["rule_id"], "running")
            store.transition_rule_execution(run["id"], execution["rule_id"], "succeeded", finding_count=1)
        store.publish_result(
            run["id"],
            {
                "mode": "report_self",
                "machine_overall_status": "error",
                "status_counts": {"pass": len(REPORT_SELF_RULE_ORDER) - 1, "warning": 0, "manual": 0, "error": 1},
                "findings": [
                    {"id": rule_id, "rule_id": rule_id, "status": "error" if rule_id == "REPORT-R02" else "pass", "details": {}, "evidence_locations": []}
                    for rule_id in REPORT_SELF_RULE_ORDER
                ],
            },
        )
        return store.get_run(run["id"])

    def test_notes_are_saved_and_carried_to_a_later_run_of_the_same_report(self) -> None:
        first = self.make_run("notes-first")
        status, saved = self.request_json(
            f"/api/v1/runs/{first['id']}/notes",
            method="POST",
            payload={"finding_key": f"REPORT-R02@{first['id']}", "decision": "not_problem", "note": "附件无标签属正常"},
        )
        self.assertEqual(status, 201)
        self.assertEqual(saved["finding_key"], "REPORT-R02")
        _, listed = self.request_json(f"/api/v1/runs/{first['id']}/notes")
        self.assertEqual(listed["items"][0]["decision"], "not_problem")
        self.assertFalse(listed["items"][0]["carried_over"])

        second = self.make_run("notes-second")
        _, carried = self.request_json(f"/api/v1/runs/{second['id']}/notes")
        self.assertEqual(carried["items"][0]["note"], "附件无标签属正常")
        self.assertTrue(carried["items"][0]["carried_over"])

    def test_invalid_decision_is_rejected(self) -> None:
        run = self.make_run("notes-invalid")
        with self.assertRaises(HTTPError) as caught:
            self.request_json(f"/api/v1/runs/{run['id']}/notes", method="POST", payload={"finding_key": "REPORT-R02", "decision": "ok"})
        self.assertEqual(caught.exception.code, 422)

    def test_recent_runs_list_includes_case_name(self) -> None:
        run = self.make_run("notes-recent")
        _, payload = self.request_json("/api/v1/runs?limit=5")
        ids = [item["id"] for item in payload["items"]]
        self.assertIn(run["id"], ids)
        item = next(item for item in payload["items"] if item["id"] == run["id"])
        self.assertEqual(item["case_name"], "notes-recent")
        self.assertNotIn("published_summary", item)


if __name__ == "__main__":
    unittest.main()
