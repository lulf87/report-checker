import unittest
from http.client import HTTPConnection
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from types import SimpleNamespace

from mvp.static_server import SafeStaticHandler, SafeStaticServer, _safe_relative, build_allowlist, is_local_request, is_loopback_host


ROOT = Path(__file__).resolve().parents[1]


class StaticServerAllowlistTests(unittest.TestCase):
    def test_upload_mode_contains_only_prototype_tree(self) -> None:
        allowlist = build_allowlist(ROOT, "upload")
        self.assertIn("/workbench-upload.html", allowlist)
        self.assertIn("/docs/prototypes/workbench-upload.html", allowlist)
        self.assertEqual(
            allowlist["/workbench-upload.html"],
            allowlist["/docs/prototypes/workbench-upload.html"],
        )
        self.assertIn("/docs/prototypes/vendor/pdfjs/pdf.min.mjs", allowlist)
        self.assertNotIn("/output/run_state.sqlite3", allowlist)
        self.assertFalse(any(key.startswith("/素材/") for key in allowlist))
        self.assertFalse(any(key.startswith("/.git") for key in allowlist))
        self.assertFalse(any(key.startswith("/.aws") for key in allowlist))
        self.assertFalse(any(path.suffix.lower() in {".db", ".sqlite", ".sqlite3"} for path in allowlist.values()))

    def test_real_mode_allows_manifest_results_and_declared_assets_only(self) -> None:
        if not (ROOT / "docs/prototypes/workbench-real.html").exists():
            self.skipTest("private real workbench fixture is unavailable")
        allowlist = build_allowlist(ROOT, "real")
        expected = {
            "/output/report-self-unified-20260930-v3/1347/result.json",
            "/output/report-self-unified-20260930-v3/1539/result.json",
            "/output/report-self-unified-20260930-v3/2795/result.json",
            "/output/report-self-unified-20260930-v3/2948/result.json",
            "/output/full-records-manual-reduction-20260930-v1/1347/report-record-9706-1/result.json",
            "/output/full-records-manual-reduction-20260930-v1/1539/report-record-9706-1/result.json",
            "/output/full-records-manual-reduction-20260930-v1/2948/report-record-9706-1/result.json",
            "/output/full-records-manual-reduction-20260930-v1/1539/report-record-9706-202/result.json",
            "/output/full-records-manual-reduction-20260930-v1/2795/report-record-9706-202/result.json",
            "/output/full-records-manual-reduction-20260930-v1/2948/report-record-9706-202/result.json",
        }
        self.assertTrue(expected.issubset(allowlist))
        self.assertIn("/docs/prototypes/workbench-real.html", allowlist)
        self.assertEqual(
            allowlist["/workbench-real.html"],
            allowlist["/docs/prototypes/workbench-real.html"],
        )
        self.assertNotIn("/output/run_state.sqlite3", allowlist)
        self.assertNotIn("/output/report-self-unified-20260930-v3/1347/secret.txt", allowlist)
        self.assertTrue(any(key.startswith("/素材/") and key.endswith(".pdf") for key in allowlist))
        self.assertTrue(any(key.startswith("/output/") and "/evidence/" in key for key in allowlist))
        self.assertFalse(any(key.startswith("/.git") for key in allowlist))
        self.assertFalse(any(key.startswith("/.aws") for key in allowlist))

    def test_loopback_host_parser_rejects_spoofed_host(self) -> None:
        for host in (None, "", "attacker.example", "127.0.0.1.evil", "127.0.0.1:bad", "localhost:", "10.0.0.1:8765"):
            with self.subTest(host=host):
                self.assertFalse(is_loopback_host(host))
        for host in ("127.0.0.1", "127.0.0.1:8765", "localhost:8765", "[::1]:8765"):
            with self.subTest(host=host):
                self.assertTrue(is_loopback_host(host))

    def test_url_path_normalizer_rejects_traversal_and_hidden_components(self) -> None:
        for path in ("../README.md", "output/../README.md", ".git/config", ".aws/credentials", "output//secret"):
            with self.subTest(path=path):
                self.assertIsNone(_safe_relative(path))

    def test_local_request_rejects_cross_site_origin_and_fetch_metadata(self) -> None:
        base = {"Host": "127.0.0.1:8765"}
        self.assertTrue(is_local_request(base, 8765))
        self.assertTrue(is_local_request({**base, "Origin": "http://localhost:8765"}, 8765))
        for headers in (
            {**base, "Origin": "https://attacker.example"},
            {**base, "Origin": "http://127.0.0.1:8767"},
            {**base, "Sec-Fetch-Site": "cross-site"},
        ):
            with self.subTest(headers=headers):
                self.assertFalse(is_local_request(headers, 8765))

    def test_handler_never_normalizes_traversal_into_allowed_file(self) -> None:
        handler = SafeStaticHandler.__new__(SafeStaticHandler)
        handler.server = SimpleNamespace(
            server_port=8765,
            mode="upload",
            allowlist={"/workbench-upload.html": ROOT / "docs/prototypes/workbench-upload.html"},
        )
        handler.headers = {"Host": "127.0.0.1:8765"}
        failures = []
        handler._send_error = failures.append
        for path in ("/../workbench-upload.html", "/%2e%2e/workbench-upload.html", "/.git/config", "/.aws/credentials", "/output/run-state.sqlite3"):
            with self.subTest(path=path):
                handler.path = path
                self.assertIsNone(handler._resolve_request())
        self.assertEqual(len(failures), 5)

    def test_handler_resolves_root_and_legacy_workbench_paths(self) -> None:
        upload_path = ROOT / "docs/prototypes/workbench-upload.html"
        handler = SafeStaticHandler.__new__(SafeStaticHandler)
        handler.server = SimpleNamespace(
            server_port=8765,
            mode="upload",
            allowlist={
                "/workbench-upload.html": upload_path,
                "/docs/prototypes/workbench-upload.html": upload_path,
            },
        )
        handler.headers = {"Host": "127.0.0.1:8765"}
        handler._send_error = self.fail
        for path in ("/", "/workbench-upload.html", "/docs/prototypes/workbench-upload.html"):
            with self.subTest(path=path):
                handler.path = path
                self.assertEqual(handler._resolve_request(), upload_path.resolve())

    def test_http_root_legacy_pages_and_vendor_assets_are_served(self) -> None:
        with TemporaryDirectory() as temporary:
            project = Path(temporary)
            prototypes = project / "docs/prototypes"
            vendor = prototypes / "vendor/pdfjs"
            vendor.mkdir(parents=True)
            (prototypes / "workbench-upload.html").write_text("upload workbench", encoding="utf-8")
            (prototypes / "workbench-real.html").write_text("real workbench", encoding="utf-8")
            (vendor / "pdf.min.mjs").write_text("export const fixture = true;", encoding="utf-8")
            for mode in ("upload", "real"):
                with self.subTest(mode=mode):
                    server = SafeStaticServer(("127.0.0.1", 0), mode, project)
                    thread = Thread(target=server.serve_forever, daemon=True)
                    thread.start()
                    try:
                        expected_root = f"{mode} workbench".encode()
                        for path, expected in (
                            ("/", expected_root),
                            (f"/workbench-{mode}.html", expected_root),
                            (f"/docs/prototypes/workbench-{mode}.html", expected_root),
                            ("/docs/prototypes/vendor/pdfjs/pdf.min.mjs", b"export const fixture = true;"),
                        ):
                            with self.subTest(path=path):
                                connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                                try:
                                    connection.request("GET", path, headers={"Connection": "close"})
                                    response = connection.getresponse()
                                    self.assertEqual(response.status, 200)
                                    self.assertEqual(response.read(), expected)
                                finally:
                                    connection.close()
                    finally:
                        server.shutdown()
                        server.server_close()
                        thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
