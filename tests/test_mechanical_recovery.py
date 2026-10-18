"""Controller recovery tests with a synthetic authenticated-worker proxy."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ansys_mcp import mechanical as host
from ansys_mcp.elevation import ElevatedLaunchTimeout


class Proxy:
    def __init__(self, directory, session_id):
        self.state_dir = directory
        self.session_id = session_id
        self.closed = False
        self.worker = {"pid": 123, "creation_time": "synthetic"}
        self.calls = []

    def summary(self):
        return {"ok": True, "worker": self.worker}

    def request(self, operation, payload, **kwargs):
        self.calls.append(operation)
        if operation == "request_status":
            return {"ok": True, "status": "complete", "outputs": {"request_id": payload["request_id"]}}
        action = payload["recipe"]["session"]["action"]
        return {"ok": True, "status": "closed" if action == "close" else "complete", "outputs": {"session": {
            "id": self.session_id, "backend": "workbench", "generation": 0,
            "state": "closed" if action == "close" else "ready", "closed": action == "close",
        }}, "diagnostics": [], "artifacts": []}

    def query(self, request_id):
        return {"ok": True, "status": "running", "request_id": request_id}

    def close(self):
        self.closed = True
        return {"ok": True}


class MechanicalRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workdir = self.root / "nested-project"
        self.workdir.mkdir()
        self.env = patch.dict(os.environ, {"ANSYS_MCP_ALLOWED_ROOTS": str(self.root)})
        self.env.start()
        self.session_id = "a" * 32
        self.worker_id = "b" * 32
        self.directory = self.workdir / "worker"
        self.directory.mkdir()
        self.proxy = Proxy(self.directory, self.session_id)
        self.attach = patch("ansys_mcp.elevation.ElevatedSession.attach", return_value=self.proxy)
        self.attach_mock = self.attach.start()
        self.launch = patch("ansys_mcp.elevation.ElevatedSession.launch", side_effect=AssertionError("recovery must not launch a worker"))
        self.launch_mock = self.launch.start()
        host._write_record("workers", self.worker_id, {"id": self.worker_id, "state_dir": str(self.directory),
                                                       "workdir": str(self.workdir), "worker": self.proxy.worker,
                                                       "closed": False, "elevated": False}, self.workdir)
        host._write_record("sessions", self.session_id, {"id": self.session_id, "worker_id": self.worker_id,
                                                        "elevated": False, "session": {"id": self.session_id, "closed": False, "backend": "workbench", "state": "ready"}}, self.workdir)
        host._ELEVATED.clear()
        host._ELEVATED_WORKERS.clear()

    def tearDown(self):
        host._ELEVATED.clear()
        host._ELEVATED_WORKERS.clear()
        self.launch.stop()
        self.attach.stop()
        self.env.stop()
        self.temp.cleanup()

    def test_stdio_cache_loss_restores_same_worker_for_session_and_request(self):
        result = host.run_mechanical_recipe({"session": {"action": "reuse", "id": self.session_id}, "operations": [{"op": "get", "member": "Name"}]}, self.workdir, self.workdir / "run", 1)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["outputs"]["session"]["id"], self.session_id)
        self.attach_mock.assert_called_once_with(self.directory)
        observed = host.mechanical_request_status(self.session_id, "original-request")
        self.assertEqual(observed["outputs"]["request_id"], "original-request")
        self.launch_mock.assert_not_called()

    def test_identity_change_rejected_without_recreation(self):
        self.proxy.worker = {"pid": 123, "creation_time": "different"}
        observed = host.mechanical_request_status(self.session_id, "original-request")
        self.assertFalse(observed["ok"])
        self.assertEqual(observed["diagnostics"][0]["code"], "MECHANICAL_WORKER_IDENTITY_CHANGED")
        self.launch_mock.assert_not_called()

    def test_cached_summary_does_not_contact_product_or_worker(self):
        summaries = host.session_summary()
        self.assertEqual(summaries[0]["id"], self.session_id)
        self.assertFalse(summaries[0]["connection_verified"])
        self.attach_mock.assert_not_called()
        self.assertEqual(self.proxy.calls, [])

    def test_outer_unknown_query_restores_same_worker(self):
        result = host.mechanical_elevated_request_status(self.worker_id, "original-outer")
        self.assertEqual(result["request_id"], "original-outer")
        self.assertEqual(result["status"], "running")
        self.launch_mock.assert_not_called()

    def test_acknowledged_actor_failure_keeps_observation_handles(self):
        class HandlerFailure(RuntimeError):
            request_id = "original-outer-request"
        def response(operation, payload, **kwargs):
            if operation == "session_summary":
                return {"sessions": [{"id": self.session_id, "backend": "workbench", "state": "disconnected", "closed": False, "last_request_id": "original-inner-request"}]}
            raise HandlerFailure("synthetic handler response lost")
        with patch.object(self.proxy, "request", side_effect=response):
            result = host.run_mechanical_recipe({"session": {"action": "reuse", "id": self.session_id}, "operations": [{"op": "get", "member": "Name"}]}, self.workdir, self.workdir / "run", 1)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["outputs"]["worker_request_id"], "original-outer-request")
        self.assertEqual(result["outputs"]["request_id"], "original-inner-request")
        self.launch_mock.assert_not_called()
        json.dumps(result)

    def test_launch_timeout_keeps_starting_record_and_original_handle(self):
        self.launch.stop()
        def timeout(*args, **kwargs):
            raise ElevatedLaunchTimeout(kwargs["state_dir"])
        with patch("ansys_mcp.elevation.ElevatedSession.launch", side_effect=timeout):
            result = host.run_mechanical_recipe({"launch": {"elevation": "never"}}, self.workdir, self.workdir / "run", 0.1)
        self.assertEqual(result["status"], "unknown")
        worker_id = result["outputs"]["worker_id"]
        record = host._read_record("workers", worker_id)
        self.assertEqual(record["state"], "starting")
        self.assertEqual(result["outputs"]["worker_request_id"], "startup")
        self.launch.start()


if __name__ == "__main__":
    unittest.main()
