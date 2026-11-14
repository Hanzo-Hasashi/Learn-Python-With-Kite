import os
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from runner.app import app, reserve_execution


class KiteRunnerApiTests(unittest.TestCase):
    def setUp(self):
        self.client_context = TestClient(app)
        self.client = self.client_context.__enter__()
        self.environment = patch.dict(os.environ, {"RUNNER_SHARED_SECRET": "runner-test-secret"})
        self.environment.__enter__()

    def tearDown(self):
        self.environment.__exit__(None, None, None)
        self.client_context.__exit__(None, None, None)

    def test_run_requires_shared_secret_without_starting_code(self):
        with patch("runner.app.subprocess.Popen") as popen:
            response = self.client.post("/run", json={"code": "print('not executed')"})
        self.assertEqual(response.status_code, 401)
        popen.assert_not_called()

    def test_non_linux_runner_refuses_to_execute_code(self):
        with patch("runner.app.sys.platform", "win32"), patch("runner.app.subprocess.Popen") as popen:
            response = self.client.post(
                "/run",
                headers={"X-Kite-Runner-Key": "runner-test-secret"},
                json={"code": "print('not executed')"},
            )
        self.assertEqual(response.status_code, 503)
        popen.assert_not_called()

    def test_execution_input_is_bounded(self):
        response = self.client.post(
            "/run",
            headers={"X-Kite-Runner-Key": "runner-test-secret"},
            json={"code": "x" * 20_001},
        )
        self.assertEqual(response.status_code, 422)

    def test_cancel_requires_shared_secret_and_kills_the_registered_process(self):
        execution_id = "a" * 32
        process = Mock()
        process.poll.return_value = None
        with patch.dict("runner.app.ACTIVE_EXECUTIONS", {execution_id: process}, clear=True), \
                patch.dict("runner.app.CANCELLED_EXECUTIONS", {}, clear=True), \
                patch.dict("runner.app.FINISHED_EXECUTIONS", {}, clear=True), \
                patch("runner.app.kill_group") as kill_group:
            unauthorized = self.client.post(f"/cancel/{execution_id}")
            self.assertEqual(unauthorized.status_code, 401)
            response = self.client.post(
                f"/cancel/{execution_id}",
                headers={"X-Kite-Runner-Key": "runner-test-secret"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["cancelled"])
        kill_group.assert_called_once_with(process)

    def test_cancel_before_process_registration_is_honored_when_execution_starts(self):
        execution_id = "b" * 32
        with patch.dict("runner.app.ACTIVE_EXECUTIONS", {}, clear=True), \
                patch.dict("runner.app.CANCELLED_EXECUTIONS", {}, clear=True), \
                patch.dict("runner.app.FINISHED_EXECUTIONS", {}, clear=True):
            response = self.client.post(
                f"/cancel/{execution_id}",
                headers={"X-Kite-Runner-Key": "runner-test-secret"},
            )
            should_run = reserve_execution(execution_id)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["cancelled"])
        self.assertFalse(should_run)


if __name__ == "__main__":
    unittest.main()
