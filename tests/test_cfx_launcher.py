from __future__ import annotations

import ctypes
import os
import shutil
import sys
import time
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
os.environ["ANSYS_MCP_ALLOWED_ROOTS"] = str(PROJECT)
os.chdir(PROJECT)

from ansys_mcp.process_supervisor import run_process


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
    """Document the launcher boundary that the CFX step must verify around.

    A CFX launcher can exit after handing the work to another process. The
    supervisor only waits for the direct child, so it returns early; closing the
    kill-on-close job then terminates the hand-off process. A zero exit code is
    therefore never sufficient evidence of a finished solve.
    """

    def setUp(self) -> None:
        self.scratch = PROJECT / ".ansys_mcp" / "cfx-launcher-tests"
        shutil.rmtree(self.scratch, ignore_errors=True)
        self.scratch.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.scratch, ignore_errors=True)

    def test_handoff_child_is_not_reported_as_completed(self) -> None:
        marker = self.scratch / "child.marker"
        child_pid_file = self.scratch / "child.pid"
        launcher = self.scratch / "launcher.py"
        launcher.write_text(
            "import pathlib, subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c',\n"
            "    'import pathlib, time; time.sleep(2.0); pathlib.Path(r\"" + str(marker) + "\").write_text(\"done\")'])\n"
            "pathlib.Path(r\"" + str(child_pid_file) + "\").write_text(str(child.pid))\n"
            "time.sleep(0.05)\n",
            encoding="utf-8",
        )
        started = time.monotonic()
        process = run_process([sys.executable, str(launcher)], cwd=PROJECT, env=os.environ.copy(),
                              timeout_seconds=10, run_dir=self.scratch, log_name="handoff")
        elapsed = time.monotonic() - started
        self.assertEqual(process.returncode, 0)
        self.assertFalse(process.timed_out)
        self.assertLess(elapsed, 1.5, "supervisor waited for the hand-off child; the boundary changed")
        child_pid = int(child_pid_file.read_text(encoding="ascii"))
        deadline = time.monotonic() + 2.5
        while _pid_alive(child_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(_pid_alive(child_pid), "hand-off child survived the supervisor's job close")
        self.assertFalse(marker.exists(), "hand-off child finished despite the supervisor returning early")

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
