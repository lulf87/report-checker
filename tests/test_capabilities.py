from __future__ import annotations

import json
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from mvp.capabilities import (
    MODE_CATALOG,
    RULE_CATALOG,
    capabilities_payload,
    rules_payload,
)
from mvp.capability_server import create_server


class CapabilityCatalogTests(unittest.TestCase):
    def test_catalog_lists_mode_boundaries_and_roles(self) -> None:
        self.assertEqual(
            tuple(MODE_CATALOG),
            (
                "report_self",
                "report_ptr",
                "report_ptr_report",
                "report_diff",
                "report_record_9706_1",
                "report_record_9706_202",
            ),
        )
        self.assertEqual(MODE_CATALOG["report_self"]["required_roles"], ("report",))
        self.assertEqual(
            MODE_CATALOG["report_ptr"]["required_roles"],
            ("report", "ptr"),
        )
        self.assertFalse(MODE_CATALOG["report_ptr"]["enabled"])
        self.assertEqual(
            MODE_CATALOG["report_ptr"]["disabled_reason_code"],
            "PTR_NOT_VALIDATED",
        )
        self.assertEqual(MODE_CATALOG["report_ptr"]["rule_ids"], ("PTR-P01",))
        self.assertFalse(MODE_CATALOG["report_ptr_report"]["enabled"])
        self.assertEqual(
            MODE_CATALOG["report_ptr_report"]["disabled_reason_code"],
            "PTR_NOT_VALIDATED",
        )
        self.assertFalse(MODE_CATALOG["report_diff"]["enabled"])
        self.assertEqual(MODE_CATALOG["report_diff"]["disabled_reason_code"], "DIFF_NOT_SPECIFIED")

    def test_rule_catalog_matches_mode_rule_plans(self) -> None:
        for mode_id, mode in MODE_CATALOG.items():
            for rule_id in mode["rule_ids"]:
                self.assertIn(rule_id, RULE_CATALOG)
                self.assertIn(mode_id, RULE_CATALOG[rule_id]["modes"])
        expected_rule_ids = {
            rule_id
            for mode in MODE_CATALOG.values()
            for rule_id in mode["rule_ids"]
        }
        # Early Report artifacts retain the historical combined alias until
        # they are regenerated; it remains registered for read compatibility.
        expected_rule_ids.add("REPORT-R09-R10")
        self.assertEqual(set(RULE_CATALOG), expected_rule_ids)

    def test_payloads_are_json_safe_and_return_fresh_lists(self) -> None:
        capabilities = capabilities_payload()
        rules = rules_payload()
        json.dumps(capabilities, ensure_ascii=False)
        json.dumps(rules, ensure_ascii=False)
        self.assertEqual(capabilities["schema_version"], "1.0.0")
        self.assertEqual(rules["schema_version"], "1.0.0")
        self.assertTrue(all("status" not in rule for rule in rules["rules"]))
        self.assertTrue(all("disabled_reason_code" in rule for rule in rules["rules"]))
        capabilities["modes"]["report_self"]["required_roles"].append("mutated")
        self.assertEqual(MODE_CATALOG["report_self"]["required_roles"], ("report",))

    def test_frozen_real_results_use_registered_rules_only(self) -> None:
        root = Path(__file__).resolve().parents[1]
        result_paths = [
            *sorted((root / "output/report-self-unified-20260930-v3").glob("*/result.json")),
            *sorted((root / "output/full-records-manual-reduction-20260930-v1").glob("*/*/result.json")),
        ]
        self.assertEqual(len(result_paths), 10)
        for result_path in result_paths:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
            mode = payload["mode"]
            self.assertIn(mode, MODE_CATALOG, result_path)
            allowed = set(MODE_CATALOG[mode]["rule_ids"])
            observed = {
                str(item.get("rule_id") or item.get("id"))
                for item in [*payload.get("findings", []), *payload.get("ledger", [])]
            }
            self.assertTrue(observed, result_path)
            # The checked-in v3 self-check artifacts predate the split of
            # REPORT-R09/R10. They remain readable as historical evidence;
            # newly generated runs use the current separate rule IDs.
            if mode == "report_self":
                allowed.add("REPORT-R09-R10")
            self.assertTrue(observed <= allowed, (result_path, sorted(observed - allowed)))
            self.assertTrue(observed <= set(RULE_CATALOG), (result_path, sorted(observed - set(RULE_CATALOG))))


class CapabilityServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = create_server("127.0.0.1", 0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def get_json(self, path: str) -> tuple[int, dict]:
        with urlopen(self.base_url + path, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_health_and_catalog_endpoints(self) -> None:
        with urlopen(self.base_url + "/healthz", timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertTrue(response.getheader("X-Request-ID"))
            self.assertEqual(json.loads(response.read().decode("utf-8")), {"status": "ok"})

        status, capabilities = self.get_json("/api/v1/capabilities?cache=0")
        self.assertEqual(status, 200)
        self.assertEqual(list(capabilities["modes"]), list(MODE_CATALOG))
        self.assertFalse(capabilities["modes"]["report_ptr"]["enabled"])

        status, rules = self.get_json("/api/v1/rules")
        self.assertEqual(status, 200)
        self.assertEqual([rule["id"] for rule in rules["rules"]], list(RULE_CATALOG))

    def test_head_unknown_and_write_methods_have_stable_http_contract(self) -> None:
        request = Request(self.base_url + "/healthz", method="HEAD")
        with urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader("Content-Type"), "application/json; charset=utf-8")
            self.assertEqual(response.read(), b"")

        with self.assertRaises(HTTPError) as missing:
            self.get_json("/api/v1/does-not-exist")
        self.assertEqual(missing.exception.code, 404)
        missing_payload = json.loads(missing.exception.read())
        self.assertEqual(missing_payload["error"]["code"], "NOT_FOUND")
        self.assertEqual(missing_payload["error"]["path"], "/api/v1/does-not-exist")
        self.assertTrue(missing_payload["error"]["request_id"])

        request = Request(self.base_url + "/api/v1/capabilities", method="POST", data=b"{}")
        with self.assertRaises(HTTPError) as method_error:
            urlopen(request, timeout=5)
        self.assertEqual(method_error.exception.code, 405)
        self.assertEqual(method_error.exception.headers.get("Allow"), "GET, HEAD")

        request = Request(self.base_url + "/healthz", method="CONNECT")
        with self.assertRaises(HTTPError) as unknown_method:
            urlopen(request, timeout=5)
        self.assertEqual(unknown_method.exception.code, 405)

    def test_server_rejects_non_local_bind_address(self) -> None:
        with self.assertRaisesRegex(ValueError, "local host"):
            create_server("0.0.0.0", 0)


if __name__ == "__main__":
    unittest.main()
