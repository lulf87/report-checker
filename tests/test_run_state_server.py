from __future__ import annotations

import base64
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import fitz

from mvp.run_state_server import create_server
from mvp.checker import REPORT_SELF_RULE_ORDER


class RunStateServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # This fixture intentionally exercises the legacy unresolved-input
        # queue path; production defaults remain strict.
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

    def request_json(self, path: str, *, method: str = "GET", payload: dict | None = None, csrf: bool = False):
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"} if body is not None else {}
        if csrf:
            headers["X-CSRF-Token"] = self.csrf
        request = Request(self.base_url + path, method=method, data=body, headers=headers)
        with urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_case_run_snapshot_events_and_queued_cancel(self) -> None:
        status, case = self.request_json(
            "/api/v1/cases", method="POST", payload={"name": "状态 API 测试"}, csrf=True
        )
        self.assertEqual(status, 201)
        status, preflight = self.request_json(
            f"/api/v1/cases/{case['id']}/runs:preflight",
            method="POST",
            payload={"mode": "report_self", "inputs": {"report_document_id": "doc-1"}},
            csrf=True,
        )
        self.assertEqual(status, 200)
        self.assertTrue(preflight["can_create_run"])
        self.assertEqual(preflight["coverage"]["ready"], len(REPORT_SELF_RULE_ORDER))
        status, run = self.request_json(
            f"/api/v1/cases/{case['id']}/runs",
            method="POST",
            payload={
                "mode": "report_self",
                "inputs": {"report_document_id": "doc-1"},
                "preflight_plan_hash": preflight["plan_hash"],
            },
            csrf=True,
        )
        self.assertEqual(status, 202)
        self.assertEqual(run["lifecycle_status"], "queued")
        self.assertEqual(run["planned_rule_ids"], list(REPORT_SELF_RULE_ORDER))

        status, events = self.request_json(f"/api/v1/runs/{run['id']}/events")
        self.assertEqual(status, 200)
        self.assertEqual([event["type"] for event in events["items"]], ["run_state_changed"])
        status, executions = self.request_json(f"/api/v1/runs/{run['id']}/rule-executions")
        self.assertEqual(status, 200)
        self.assertEqual(len(executions["items"]), len(REPORT_SELF_RULE_ORDER))
        self.assertTrue(all(item["execution_state"] == "pending" for item in executions["items"]))

        status, cancelled = self.request_json(
            f"/api/v1/runs/{run['id']}:cancel", method="POST", csrf=True
        )
        self.assertEqual(status, 200)
        self.assertEqual(cancelled["lifecycle_status"], "cancelled")
        self.assertIsNone(cancelled["machine_overall_status"])

    def test_ptr_is_rejected_without_creating_run(self) -> None:
        _, case = self.request_json(
            "/api/v1/cases", method="POST", payload={"name": "PTR 状态测试"}, csrf=True
        )
        status, preflight = self.request_json(
            f"/api/v1/cases/{case['id']}/runs:preflight",
            method="POST",
            payload={"mode": "report_ptr", "inputs": {"report_document_id": "r", "ptr_document_id": "p"}},
            csrf=True,
        )
        self.assertEqual(status, 200)
        self.assertFalse(preflight["can_create_run"])
        self.assertIn("PTR_NOT_VALIDATED", preflight["blocking_issues"])
        request = Request(
            self.base_url + f"/api/v1/cases/{case['id']}/runs",
            method="POST",
            data=json.dumps(
                {"mode": "report_ptr", "inputs": {"report_document_id": "r", "ptr_document_id": "p"}}
            ).encode(),
            headers={"Content-Type": "application/json", "X-CSRF-Token": self.csrf},
        )
        with self.assertRaises(HTTPError) as error:
            urlopen(request, timeout=5)
        self.assertEqual(error.exception.code, 409)
        payload = json.loads(error.exception.read())
        self.assertEqual(payload["error"]["code"], "MODE_DISABLED")
        self.assertEqual(payload["error"]["details"]["disabled_reason_code"], "PTR_NOT_VALIDATED")
        _, runs = self.request_json(f"/api/v1/cases/{case['id']}/runs")
        self.assertEqual(runs["items"], [])

    def test_write_requires_local_csrf_token(self) -> None:
        request = Request(
            self.base_url + "/api/v1/cases",
            method="POST",
            data=b'{"name":"blocked"}',
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(HTTPError) as error:
            urlopen(request, timeout=5)
        self.assertEqual(error.exception.code, 403)

    def test_reads_require_loopback_host_origin_and_fetch_metadata(self) -> None:
        for headers, expected_code in (
            ({"Host": "attacker.example"}, "INVALID_LOCAL_HOST"),
            ({"Host": "localhost:invalid"}, "INVALID_LOCAL_HOST"),
            ({"Origin": "https://evil.example"}, "CROSS_SITE_REQUEST_BLOCKED"),
            ({"Sec-Fetch-Site": "cross-site"}, "CROSS_SITE_REQUEST_BLOCKED"),
        ):
            request = Request(self.base_url + "/api/v1/session", headers=headers)
            with self.subTest(headers=headers), self.assertRaises(HTTPError) as error:
                urlopen(request, timeout=5)
            self.assertEqual(error.exception.code, 403)
            payload = json.loads(error.exception.read())
            self.assertEqual(payload["error"]["code"], expected_code)

    def test_expired_csrf_token_is_rejected(self) -> None:
        original_expiry = self.server.csrf_expires_at
        self.server.csrf_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        try:
            request = Request(
                self.base_url + "/api/v1/cases",
                method="POST",
                data=b'{"name":"expired token"}',
                headers={"Content-Type": "application/json", "X-CSRF-Token": self.csrf},
            )
            with self.assertRaises(HTTPError) as error:
                urlopen(request, timeout=5)
            self.assertEqual(error.exception.code, 403)
            payload = json.loads(error.exception.read())
            self.assertEqual(payload["error"]["code"], "CSRF_TOKEN_EXPIRED")
        finally:
            self.server.csrf_expires_at = original_expiry

    def test_review_actor_is_derived_from_session_not_client_header(self) -> None:
        case = self.server.store.create_case("actor header test")
        run = self.server.store.create_run(
            case_id=case["id"],
            mode="report_self",
            inputs={"report_document_id": "actor-report"},
            input_snapshot={
                "report": {
                    "document_id": "actor-report",
                    "blob_sha256": "a" * 64,
                    "page_count": 1,
                }
            },
        )
        self.server.store.transition_run(run["id"], "running")
        for execution in self.server.store.get_rule_executions(run["id"]):
            self.server.store.transition_rule_execution(run["id"], execution["rule_id"], "running")
            self.server.store.transition_rule_execution(
                run["id"],
                execution["rule_id"],
                "succeeded",
                finding_count=1,
            )
        self.server.store.publish_result(
            run["id"],
            {
                "mode": "report_self",
                "machine_overall_status": "manual",
                "status_counts": {"pass": 11, "warning": 0, "manual": 1, "error": 0},
                "findings": [
                    {
                        "id": rule_id,
                        "rule_id": rule_id,
                        "status": "manual" if rule_id == "REPORT-R01" else "pass",
                        "details": {},
                        "evidence_locations": [],
                    }
                    for rule_id in REPORT_SELF_RULE_ORDER
                ],
            },
        )
        finding_id = self.server.store.list_findings(run["id"])[0]["id"]
        request = Request(
            self.base_url + f"/api/v1/findings/{finding_id}/reviews",
            method="POST",
            data=json.dumps(
                {"action": "record_observation", "observation": {"raw_value": "x"}}
            ).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-CSRF-Token": self.csrf,
                "X-Actor-Id": "forged-actor",
            },
        )
        with urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(payload["action"]["actor_id"], self.server.session_id)
        self.assertNotEqual(payload["action"]["actor_id"], "forged-actor")

    def test_failed_bind_closes_without_partial_constructor_attributes(self) -> None:
        with self.assertRaises(OSError):
            create_server(host="127.0.0.1", port=self.server.server_port)

    def test_workbench_cors_preflight_and_write_from_static_port(self) -> None:
        origin = "http://127.0.0.1:8765"
        request = Request(
            self.base_url + "/api/v1/cases",
            method="OPTIONS",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type,x-csrf-token",
            },
        )
        with urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 204)
            self.assertEqual(response.headers.get("Access-Control-Allow-Origin"), origin)
            self.assertIn("POST", response.headers.get("Access-Control-Allow-Methods", ""))
            self.assertNotIn("X-Actor-Id", response.headers.get("Access-Control-Allow-Headers", ""))

        request = Request(self.base_url + "/api/v1/session", headers={"Origin": origin})
        with urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get("Access-Control-Allow-Origin"), origin)

        request = Request(
            self.base_url + "/api/v1/cases",
            method="POST",
            data='{"name":"跨源工作台"}'.encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Origin": origin,
                "X-CSRF-Token": self.csrf,
            },
        )
        with urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 201)
            self.assertEqual(response.headers.get("Access-Control-Allow-Origin"), origin)

    def test_document_content_keeps_utf8_filename_header_valid(self) -> None:
        _, case = self.request_json(
            "/api/v1/cases", method="POST", payload={"name": "PDF 内容响应测试"}, csrf=True
        )
        pdf = fitz.open()
        pdf.new_page().insert_text((72, 72), "content")
        content = pdf.tobytes()
        pdf.close()
        _, document = self.request_json(
            f"/api/v1/cases/{case['id']}/documents",
            method="POST",
            payload={
                "role": "report",
                "original_filename": "测试报告.pdf",
                "content_base64": base64.b64encode(content).decode("ascii"),
            },
            csrf=True,
        )
        request = Request(self.base_url + f"/api/v1/documents/{document['id']}/content")
        with urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertIn("filename*=UTF-8''", response.headers.get("Content-Disposition", ""))
            self.assertEqual(response.read(), content)
        request = Request(
            self.base_url + f"/api/v1/documents/{document['id']}/content",
            headers={"Range": "bytes=0-7"},
        )
        with urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.read(), content[:8])

    def test_default_server_rejects_unresolved_inputs(self) -> None:
        server = create_server(port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            with urlopen(base_url + "/api/v1/session", timeout=5) as response:
                csrf = json.loads(response.read())["csrf_token"]
            request = Request(
                base_url + "/api/v1/cases",
                method="POST",
                data=b'{"name":"strict"}',
                headers={"Content-Type": "application/json", "X-CSRF-Token": csrf},
            )
            with urlopen(request, timeout=5) as response:
                case = json.loads(response.read())
            request = Request(
                base_url + f"/api/v1/cases/{case['id']}/runs:preflight",
                method="POST",
                data=json.dumps(
                    {"mode": "report_self", "inputs": {"report_document_id": "missing"}}
                ).encode(),
                headers={"Content-Type": "application/json", "X-CSRF-Token": csrf},
            )
            with self.assertRaises(HTTPError) as error:
                urlopen(request, timeout=5)
            self.assertEqual(error.exception.code, 404)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_unresolved_opt_in_is_limited_to_memory_test_server(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(ValueError):
                create_server(
                    port=0,
                    database=str(Path(temporary) / "state.sqlite3"),
                    allow_unresolved_inputs=True,
                )

    def test_run_creation_rejects_missing_or_stale_preflight_hash(self) -> None:
        _, case = self.request_json(
            "/api/v1/cases", method="POST", payload={"name": "计划哈希测试"}, csrf=True
        )
        base = {
            "mode": "report_self",
            "inputs": {"report_document_id": "doc-plan"},
        }
        for payload, expected_code, expected_status in (
            (base, "RUN_PREFLIGHT_REQUIRED", 422),
            ({**base, "preflight_plan_hash": "stale"}, "RUN_PLAN_CHANGED", 409),
        ):
            request = Request(
                self.base_url + f"/api/v1/cases/{case['id']}/runs",
                method="POST",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json", "X-CSRF-Token": self.csrf},
            )
            with self.assertRaises(HTTPError) as error:
                urlopen(request, timeout=5)
            self.assertEqual(error.exception.code, expected_status)
            self.assertEqual(json.loads(error.exception.read())["error"]["code"], expected_code)
        _, runs = self.request_json(f"/api/v1/cases/{case['id']}/runs")
        self.assertEqual(runs["items"], [])


if __name__ == "__main__":
    unittest.main()
