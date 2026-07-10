from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT = Path(__file__).resolve().parents[1]
os.environ["ANSYS_MCP_ALLOWED_ROOTS"] = str(PROJECT)
os.chdir(PROJECT)

from ansys_mcp.process_supervisor import run_process
from ansys_mcp.schemas import FileStep
from ansys_mcp.workflow import _error_step, _ok_step, _run_step_with_retry, run_workflow


class ProcessSupervisorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = PROJECT / ".ansys_mcp" / "process-tests"
        self.scratch.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.scratch, ignore_errors=True)

    def test_nonzero_exit_keeps_complete_logs(self) -> None:
        process = run_process(
            [sys.executable, "-c", "import sys; print('stdout-line'); print('stderr-line', file=sys.stderr); raise SystemExit(7)"],
            cwd=PROJECT,
            env=os.environ.copy(),
            timeout_seconds=10,
            run_dir=self.scratch,
            log_name="nonzero",
        )
        self.assertEqual(process.returncode, 7)
        self.assertIn("stdout-line", process.stdout_path.read_text(encoding="utf-8"))
        self.assertIn("stderr-line", process.stderr_path.read_text(encoding="utf-8"))
        self.assertIn("stdout-line", process.stdout_tail)

    def test_timeout_is_strict(self) -> None:
        started = time.monotonic()
        process = run_process(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            cwd=PROJECT,
            env=os.environ.copy(),
            timeout_seconds=0.25,
            run_dir=self.scratch,
            log_name="timeout",
        )
        self.assertTrue(process.timed_out)
        self.assertLess(time.monotonic() - started, 5)

    @unittest.skipUnless(os.name == "nt", "Windows process-tree behavior")
    def test_timeout_terminates_child_process_tree(self) -> None:
        child_pid_file = self.scratch / "child.pid"
        parent_script = self.scratch / "parent.py"
        parent_script.write_text(
            "import pathlib, subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "pathlib.Path(sys.argv[1]).write_text(str(child.pid), encoding='ascii')\n"
            "time.sleep(60)\n",
            encoding="utf-8",
        )
        process = run_process(
            [sys.executable, str(parent_script), str(child_pid_file)],
            cwd=PROJECT,
            env=os.environ.copy(),
            timeout_seconds=0.75,
            run_dir=self.scratch,
            log_name="tree",
        )
        self.assertTrue(process.timed_out)
        self.assertTrue(child_pid_file.exists())
        child_pid = int(child_pid_file.read_text(encoding="ascii"))
        deadline = time.monotonic() + 3
        while _pid_alive(child_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(_pid_alive(child_pid), f"child process {child_pid} survived timeout")

    def test_detached_result_is_launched_and_cleaned_up(self) -> None:
        process = run_process(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            cwd=PROJECT,
            env=os.environ.copy(),
            timeout_seconds=10,
            run_dir=self.scratch,
            log_name="detached",
            detached=True,
        )
        self.assertTrue(process.detached)
        self.assertIsNone(process.returncode)
        if os.name == "nt":
            subprocess.run(
                ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        else:  # pragma: no cover
            os.kill(process.pid, 9)


class RetryAndManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = PROJECT / ".ansys_mcp" / "retry-tests"
        self.scratch.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.scratch, ignore_errors=True)

    def test_retry_reaches_success_for_retryable_code(self) -> None:
        step = FileStep.model_validate(
            {
                "id": "retry",
                "type": "file",
                "action": "exists",
                "path": str(PROJECT),
                "retry": {"max_attempts": 2, "backoff_seconds": 0},
            }
        )
        calls = {"count": 0}

        def fake_run(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                return _error_step("retry", "file", time.monotonic(), "PROCESS_NONZERO", "first")
            return _ok_step("retry", "file", time.monotonic())

        with patch("ansys_mcp.workflow._run_step_once", side_effect=fake_run):
            result = _run_step_with_retry(
                step,
                "retry",
                PROJECT,
                self.scratch,
                time.monotonic() + 10,
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["attempt_count"], 2)

    def test_manifest_is_written_running_then_complete(self) -> None:
        statuses: list[str] = []

        def capture(run_dir, summary):
            statuses.append(summary["status"])

        with patch("ansys_mcp.workflow._write_manifest_atomic", side_effect=capture):
            result = run_workflow(
                workflow={
                    "version": "1",
                    "workdir": str(PROJECT),
                    "steps": [{"id": "check", "type": "assert", "path": str(PROJECT)}],
                }
            )
        self.assertTrue(result["ok"], result)
        self.assertEqual(statuses[0], "running")
        self.assertEqual(statuses[-1], "complete")


def _pid_alive(pid: int) -> bool:
    if os.name != "nt":  # pragma: no cover
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    process_query_limited_information = 0x1000
    still_active = 259
    handle = ctypes.windll.kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == still_active
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


if __name__ == "__main__":
    unittest.main()
