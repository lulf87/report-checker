from __future__ import annotations

import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from mvp.run_state_server import create_server


class CsrfRotationTests(unittest.TestCase):
    """A workbench left open past the 8-hour token lifetime must recover."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.server = create_server(
            port=0,
            database=str(root / "state.sqlite3"),
            storage_root=str(root / "blobs"),
            output_root=str(root / "runs"),
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def session(self) -> dict:
        with urlopen(self.base + "/api/v1/session", timeout=5) as response:
            return json.loads(response.read())

    def create_case(self, token: str) -> int:
        request = Request(
            self.base + "/api/v1/cases",
            method="POST",
            data=json.dumps({"name": "token"}).encode(),
            headers={"Content-Type": "application/json", "X-CSRF-Token": token},
        )
        try:
            with urlopen(request, timeout=5) as response:
                return response.status
        except HTTPError as error:
            return error.code

    def test_expired_token_is_replaced_by_the_session_endpoint(self) -> None:
        old = self.session()["csrf_token"]
        self.server.csrf_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.assertEqual(self.create_case(old), 403)
        fresh = self.session()
        self.assertNotEqual(fresh["csrf_token"], old)
        self.assertGreater(
            datetime.fromisoformat(fresh["expires_at"].replace("Z", "+00:00")),
            datetime.now(timezone.utc) + timedelta(hours=7),
        )
        self.assertEqual(self.create_case(fresh["csrf_token"]), 201)
        self.assertEqual(self.create_case(old), 403)

    def test_valid_token_is_not_rotated(self) -> None:
        self.assertEqual(self.session()["csrf_token"], self.session()["csrf_token"])


if __name__ == "__main__":
    unittest.main()
