from __future__ import annotations

import ctypes
import os
import shutil
import sys
import threading
import time
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
os.environ["ANSYS_MCP_ALLOWED_ROOTS"] = str(PROJECT)
os.chdir(PROJECT)

from ansys_mcp.process_supervisor import CancellationToken, run_process


def _pid_alive(pid: int) -> bool:
    if os.name != "nt":  # pragma: no cover
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        still_active = 259
        code = ctypes.c_ulong()
        if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == still_active
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


@unittest.skipUnless(os.name == "nt", "CFX launcher hand-off is a Windows process-tree behavior")
class CFXLauncherHandoffTests(unittest.TestCase):
    """Supervise a launcher that hands the real work to a child process.

    The default policy returns when the direct child exits and closes the
    kill-on-close job, which terminates the hand-off child. The opt-in
    ``supervise_handoff`` policy keeps waiting for the job's remaining
    processes, while the deadline and cancellation still apply.
    """

    def setUp(self) -> None:
        self.scratch = PROJECT / ".ansys_mcp" / "cfx-launcher-tests"
        shutil.rmtree(self.scratch, ignore_errors=True)
        self.scratch.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.scratch, ignore_errors=True)

    def _handoff_launcher(self, child_delay: float) -> tuple[Path, Path, Path]:
        marker = self.scratch / "child.marker"
        child_pid_file = self.scratch / "child.pid"
        child_script = self.scratch / "child.py"
        child_script.write_text(
            "import pathlib, time\n"
            "time.sleep(%r)\n"
            "pathlib.Path(%r).write_text('done')\n" % (child_delay, str(marker)),
            encoding="utf-8")
        launcher = self.scratch / "launcher.py"
        launcher.write_text(
            "import pathlib, subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, %r])\n"
            "pathlib.Path(%r).write_text(str(child.pid))\n"
            "time.sleep(0.05)\n" % (str(child_script), str(child_pid_file)),
            encoding="utf-8")
        return launcher, marker, child_pid_file

    def test_default_policy_returns_early_and_kills_the_handoff_child(self) -> None:
        launcher, marker, child_pid_file = self._handoff_launcher(2.0)
        started = time.monotonic()
        process = run_process([sys.executable, str(launcher)], cwd=PROJECT, env=os.environ.copy(),
                              timeout_seconds=10, run_dir=self.scratch, log_name="handoff-default")
        elapsed = time.monotonic() - started
        self.assertEqual(process.returncode, 0)
        self.assertFalse(process.handoff_supervised)
        self.assertLess(elapsed, 1.5, "default policy waited for the hand-off child; the boundary changed")
        child_pid = int(child_pid_file.read_text(encoding="ascii"))
        deadline = time.monotonic() + 2.5
        while _pid_alive(child_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(_pid_alive(child_pid))
        self.assertFalse(marker.exists())

    def test_supervise_handoff_waits_for_the_child_to_finish(self) -> None:
        launcher, marker, child_pid_file = self._handoff_launcher(0.8)
        started = time.monotonic()
        process = run_process([sys.executable, str(launcher)], cwd=PROJECT, env=os.environ.copy(),
                              timeout_seconds=10, run_dir=self.scratch, log_name="handoff-supervised",
                              supervise_handoff=True)
        elapsed = time.monotonic() - started
        self.assertEqual(process.returncode, 0)
        self.assertTrue(process.handoff_supervised)
        self.assertEqual(process.job_processes_at_exit, 0)
        self.assertGreaterEqual(elapsed, 0.7, "supervisor returned before the hand-off child finished")
        self.assertTrue(marker.exists(), "hand-off child did not complete under supervision")
        child_pid = int(child_pid_file.read_text(encoding="ascii"))
        self.assertFalse(_pid_alive(child_pid))

    def test_supervise_handoff_timeout_still_terminates_the_child(self) -> None:
        launcher, marker, child_pid_file = self._handoff_launcher(30.0)
        started = time.monotonic()
        process = run_process([sys.executable, str(launcher)], cwd=PROJECT, env=os.environ.copy(),
                              timeout_seconds=0.8, run_dir=self.scratch, log_name="handoff-timeout",
                              supervise_handoff=True)
        self.assertTrue(process.timed_out)
        self.assertLess(time.monotonic() - started, 5)
        child_pid = int(child_pid_file.read_text(encoding="ascii"))
        deadline = time.monotonic() + 2.5
        while _pid_alive(child_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(_pid_alive(child_pid), "hand-off child survived the supervised timeout")
        self.assertFalse(marker.exists())

    def test_supervise_handoff_cancellation_still_terminates_the_child(self) -> None:
        launcher, marker, child_pid_file = self._handoff_launcher(30.0)
        token = CancellationToken()
        timer = threading.Timer(0.4, token.cancel)
        timer.start()
        self.addCleanup(timer.cancel)
        process = run_process([sys.executable, str(launcher)], cwd=PROJECT, env=os.environ.copy(),
                              timeout_seconds=10, run_dir=self.scratch, log_name="handoff-cancel",
                              supervise_handoff=True, cancellation_token=token)
        self.assertTrue(process.cancelled)
        child_pid = int(child_pid_file.read_text(encoding="ascii"))
        deadline = time.monotonic() + 2.5
        while _pid_alive(child_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(_pid_alive(child_pid), "hand-off child survived cancellation")
        self.assertFalse(marker.exists())

    def test_delayed_output_is_captured_before_the_child_exits(self) -> None:
        script = self.scratch / "delayed.py"
        script.write_text("import time\nprint('start', flush=True)\ntime.sleep(0.2)\nprint('late-line', flush=True)\n",
                          encoding="utf-8")
        process = run_process([sys.executable, str(script)], cwd=PROJECT, env=os.environ.copy(),
                              timeout_seconds=10, run_dir=self.scratch, log_name="delayed")
        self.assertEqual(process.returncode, 0)
        self.assertIn("late-line", process.stdout_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
