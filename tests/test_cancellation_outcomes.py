"""Integration regressions for cancellation acknowledgements and product outcomes.

All product entry points are mocked. No Ansys process is started.
"""
from __future__ import annotations

import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from ansys_mcp.job_worker import _publish_result
from ansys_mcp.jobs import _forced_stop_record, _job_dir, _write_json, cancel_workflow, get_workflow_status, pending_observations
from ansys_mcp.process_supervisor import CancellationToken, cancellation_context
from ansys_mcp.workflow import run_workflow


class CancellationOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {"ANSYS_MCP_ALLOWED_ROOTS": str(self.root)})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def unknown_packet(self, code="WORKFLOW_CANCELLED"):
        return {"ok": False, "status": "unknown", "outputs": {"worker_id": "same-worker", "worker_request_id": "same-request"},
                "diagnostics": [{"code": code, "severity": "error", "message": "Cancellation sent; original product request is still being observed."}], "artifacts": []}

    def workflow(self):
        return {"version": "1", "workdir": str(self.root), "steps": [
            {"id": "model", "type": "mechanical", "recipe": {}, "continue_on_error": True},
            {"id": "later", "type": "file", "action": "write_text", "path": "later.txt", "content": "must not execute"},
        ], "artifacts": [{"path": "${steps.later.outputs.path}"}]}

    def test_cancelled_request_with_live_actor_remains_observable_until_needs_attention(self):
        run_id = uuid.uuid4().hex
        directory = _job_dir(run_id)
        directory.mkdir(parents=True)
        token = CancellationToken(directory / "cancel.request.json")

        def cancel_during_request(*args, **kwargs):
            _write_json(token.cancel_file, {"requested": True})
            return self.unknown_packet()

        with patch("ansys_mcp.mechanical.run_mechanical_recipe", side_effect=cancel_during_request) as execute:
            with cancellation_context(token):
                result = run_workflow(workflow=self.workflow())
            self.assertEqual(result["status"], "unknown", result)
            self.assertTrue(token.cancelled)
            self.assertEqual(len(result["steps"]), 1)
            self.assertFalse((self.root / "later.txt").exists())
            manifest = {"run_id": run_id, "status": "running", "worker": None}
            _publish_result(directory, manifest, result)
            with patch("ansys_mcp.mechanical.mechanical_elevated_request_status", return_value={"ok": True, "status": "running"}) as observe, \
                    patch("ansys_mcp.mechanical.cancel_mechanical_request", return_value={"ok": True, "status": "cancel_requested"}) as cancel, \
                    patch("ansys_mcp.jobs.terminate_owned_process") as terminate:
                running = get_workflow_status(run_id)
                self.assertIn(running["status"], {"unknown", "cancel_requested"})
                self.assertEqual(running["product_observations"][0]["observation"]["status"], "running")
                cancelled = cancel_workflow(run_id)
                self.assertEqual(cancelled["status"], "cancel_requested")
                observe.assert_called_with("same-worker", "same-request")
                cancel.assert_called_once_with(worker_id="same-worker", worker_request_id="same-request", grace_seconds=5.0)
                terminate.assert_not_called()
            with patch("ansys_mcp.mechanical.mechanical_elevated_request_status", return_value={"ok": True, "status": "cancelled"}):
                settled = get_workflow_status(run_id)
            self.assertEqual(settled["status"], "needs_attention")
            self.assertEqual(settled["original_result"]["status"], "unknown")
            self.assertTrue(settled["workflow_resume_required"])
            self.assertFalse((self.root / "later.txt").exists())
            execute.assert_called_once()

    def test_future_artifact_does_not_mask_unknown_product_or_run_later_step(self):
        with patch("ansys_mcp.mechanical.run_mechanical_recipe", return_value=self.unknown_packet("MECHANICAL_OUTCOME_UNKNOWN")), \
                patch("ansys_mcp.workflow._run_file_step") as later:
            result = run_workflow(workflow=self.workflow())
        self.assertEqual(result["status"], "unknown", result)
        self.assertEqual(pending_observations(result)[0]["worker_request_id"], "same-request")
        later.assert_not_called()

    def test_exception_after_unknown_step_cannot_downgrade_its_observation(self):
        workflow = self.workflow()
        with patch("ansys_mcp.mechanical.run_mechanical_recipe", return_value=self.unknown_packet()), \
                patch("ansys_mcp.workflow._write_manifest_atomic", side_effect=[None, OSError("synthetic progress metadata failure"), None]):
            result = run_workflow(workflow=workflow)
        self.assertEqual(result["status"], "unknown", result)
        self.assertEqual(result["stage"], "unknown")
        self.assertEqual(pending_observations(result)[0]["worker_id"], "same-worker")

    def test_late_cancellation_does_not_replace_an_observed_complete_result(self):
        run_id = uuid.uuid4().hex
        directory = _job_dir(run_id)
        directory.mkdir(parents=True)
        _write_json(directory / "cancel.request.json", {"requested": True})
        completed = {"ok": True, "status": "complete", "stage": "complete", "steps": [{"id": "done", "type": "file", "ok": True, "status": "complete"}], "diagnostics": []}
        _publish_result(directory, {"run_id": run_id, "worker": None}, completed)
        self.assertEqual(get_workflow_status(run_id)["status"], "complete")
        self.assertTrue(get_workflow_status(run_id)["ok"])

    def test_older_terminal_labels_with_pending_handles_resume_observation_only(self):
        summary = {"ok": False, "status": "cancelled", "steps": [{"id": "model", "type": "mechanical", **self.unknown_packet()}]}
        for old_status in ("cancelled", "failed", "complete"):
            with self.subTest(old_status=old_status):
                run_id = uuid.uuid4().hex
                directory = _job_dir(run_id)
                directory.mkdir(parents=True)
                _write_json(directory / "result.json", summary)
                # Include no cached handles: the reader must also recover the
                # original observation pair from the recorded step itself.
                _write_json(directory / "job.json", {"run_id": run_id, "status": old_status, "worker": None, "result_path": "result.json"})
                with patch("ansys_mcp.mechanical.mechanical_elevated_request_status", return_value={"ok": True, "status": "running"}) as observe:
                    result = get_workflow_status(run_id)
                self.assertEqual(result["status"], "unknown")
                observe.assert_called_once_with("same-worker", "same-request")

    def test_both_force_stop_watchdogs_keep_product_outcome_unknown(self):
        handle = {"step_id": "model", "type": "mechanical", "worker_id": "same-worker", "worker_request_id": "same-request"}
        record = _forced_stop_record({"run_id": "synthetic", "pending_observations": [handle]}, "Worker stopped; product still needs observation.")
        self.assertEqual(record["status"], "unknown")
        self.assertEqual(record["outcome"], "unknown")
        self.assertEqual(record["pending_observations"], [handle])


if __name__ == "__main__":
    unittest.main()
