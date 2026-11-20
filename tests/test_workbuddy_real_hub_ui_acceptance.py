from __future__ import annotations

import unittest

from scripts.workbuddy_real_hub_ui_acceptance import proxy_action, task_text


class WorkBuddyRealHubUiAcceptanceTests(unittest.TestCase):
    def test_proxy_action_for_all_workbuddy_routes(self) -> None:
        cases = {
            "/integrations/workbuddy/v1/claim": "claim",
            "/integrations/workbuddy/v1/tasks/task-1/start": "start",
            "/integrations/workbuddy/v1/tasks/task-1/progress": "progress",
            "/integrations/workbuddy/v1/tasks/task-1/renew": "renew",
            "/integrations/workbuddy/v1/tasks/task-1/result": "result",
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                self.assertEqual(proxy_action(path), expected)

    def test_unknown_route_is_not_misclassified(self) -> None:
        self.assertEqual(proxy_action("/integrations/workbuddy/v1/tasks/task-1"), "unknown")
        self.assertEqual(proxy_action("/other/path"), "unknown")

    def test_task_text_extracts_only_text_parts(self) -> None:
        task = {
            "history": [
                {"parts": [{"kind": "text", "text": "prompt"}, {"kind": "image", "url": "ignored"}]},
                {"parts": [{"kind": "text", "text": "result"}]},
            ]
        }
        self.assertEqual(task_text(task), "prompt\nresult")

    def test_task_text_handles_empty_history(self) -> None:
        self.assertEqual(task_text({}), "")


if __name__ == "__main__":
    unittest.main()
