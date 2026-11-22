from __future__ import annotations

import json
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

from scripts import workbuddy_ui_fixture as fixture


class WorkBuddyUiFixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        fixture.STATE = fixture.State()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), fixture.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, path: str, payload: dict | None = None, token: str | None = "t" * 40):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if token is not None:
            headers[fixture.TOKEN_HEADER] = token
        request = urllib.request.Request(self.base + path, data=data, headers=headers)
        with urllib.request.urlopen(request, timeout=2) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_full_claim_start_progress_renew_result_roundtrip(self) -> None:
        claim = self.request("/integrations/workbuddy/v1/claim", {})
        self.assertTrue(claim["claimed"])
        task_id = claim["task"]["id"]
        lease = claim["lease"]
        self.assertIn("NETIVORA-WORKBUDDY-UI", claim["task"]["history"][0]["parts"][0]["text"])

        start = self.request(
            f"/integrations/workbuddy/v1/tasks/{task_id}/start", lease
        )
        self.assertEqual(start["status"]["state"], "working")

        progress = self.request(
            f"/integrations/workbuddy/v1/tasks/{task_id}/progress",
            {**lease, "sequence": 1, "message": "UI test started"},
        )
        self.assertFalse(progress["idempotent_replay"])

        renew = self.request(
            f"/integrations/workbuddy/v1/tasks/{task_id}/renew", lease
        )
        self.assertIn("lease_expires_at", renew)

        result = self.request(
            f"/integrations/workbuddy/v1/tasks/{task_id}/result",
            {
                **lease,
                "result_key": "workbuddy-ui-fixture-result-0001",
                "state": "completed",
                "output": "NETIVORA-WORKBUDDY-UI-ROUNDTRIP-PASS",
            },
        )
        self.assertEqual(result["task"]["status"]["state"], "completed")
        self.assertEqual(fixture.STATE.report()["result"], "PASS")
        self.assertEqual(
            [item["event"] for item in fixture.STATE.report()["events"]],
            ["claim", "start", "progress", "renew", "result"],
        )

    def test_claim_requires_present_long_token_header(self) -> None:
        request = urllib.request.Request(
            self.base + "/integrations/workbuddy/v1/claim",
            data=b"{}",
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(Exception) as caught:
            urllib.request.urlopen(request, timeout=2)
        self.assertEqual(getattr(caught.exception, "code", None), 401)

    def test_report_does_not_expose_lease_token(self) -> None:
        claim = self.request("/integrations/workbuddy/v1/claim", {})
        lease_token = claim["lease"]["lease_token"]
        report = self.request("/__test__/report")
        self.assertNotIn(lease_token, json.dumps(report))


if __name__ == "__main__":
    unittest.main()
