from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ansys_mcp.process_supervisor import IndependentWorkerLaunchError, launch_independent_process, prepare_python_com_cache


PROJECT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "nt", "Windows COM diagnostics")
class WorkerDiagnosticTests(unittest.TestCase):
    def test_explicit_wmi_denial_retains_phase_and_does_not_start_an_alternative(self):
        prepare_python_com_cache()
        factory = SimpleNamespace(SpawnInstance_=lambda: SimpleNamespace())
        process = SimpleNamespace(Methods_=lambda name: SimpleNamespace(InParameters=factory))
        class Denied(RuntimeError):
            excepinfo = (0, "SWbemServicesEx", "Access denied", None, 0, -2147217405)
        service = SimpleNamespace(Get=lambda name: factory if name == "Win32_ProcessStartup" else process,
                                  ExecMethod=lambda *args: (_ for _ in ()).throw(Denied("Synthetic access denied")))
        with patch("win32com.client.GetObject", return_value=service), patch("subprocess.Popen") as fallback:
            with self.assertRaises(IndependentWorkerLaunchError) as caught:
                launch_independent_process(["python.exe", "-c", "pass"], cwd=PROJECT, env={})
        self.assertEqual(caught.exception.code, "WINDOWS_WORKER_ACCESS_DENIED")
        self.assertEqual(caught.exception.phase, "wmi_create")
        fallback.assert_not_called()

    def test_denied_job_remains_a_known_launch_failure_on_requery(self):
        from ansys_mcp.jobs import start_workflow, get_workflow_status
        parent = PROJECT / ".ansys_mcp"
        parent.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as temporary, patch.dict(os.environ, {"ANSYS_MCP_ALLOWED_ROOTS": temporary}):
            with patch("ansys_mcp.jobs.launch_independent_process", side_effect=IndependentWorkerLaunchError("wmi_create", "test denial", True)):
                result = start_workflow(workflow={"version": "1", "workdir": temporary, "steps": [{"type": "assert", "path": temporary}]})
            self.assertEqual(result["outcome"], "not_started")
            observed = get_workflow_status(result["run_id"])
            self.assertEqual(observed["status"], "failed")
            self.assertEqual(observed["outcome"], "not_started")
            self.assertEqual(observed["diagnostics"][0]["phase"], "wmi_create")


if __name__ == "__main__":
    unittest.main()
