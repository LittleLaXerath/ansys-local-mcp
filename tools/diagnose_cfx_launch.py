"""Diagnose the CFX launcher hand-off boundary without starting any product.

Runs fake launchers through the same supervisor used by workflow steps and
records whether the hand-off child completes, is killed on early exit, or is
cleaned up by timeout/cancellation. It also prints a template for a later,
explicitly authorized comparison between the official entry point and the MCP
step; this tool never launches cfx5pre/cfx5solve/cfx5post.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ansys_mcp.config import require_allowed_path
from ansys_mcp.jobs import _write_json
from ansys_mcp.process_supervisor import CancellationToken, run_process

PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))


def _pid_alive(pid: int) -> bool:
    if os.name != "nt":  # pragma: no cover
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == 259
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def _terminate(pid: int) -> None:
    if not _pid_alive(pid):
        return
    subprocess.run(["taskkill.exe", "/PID", str(pid), "/T", "/F"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)


def _child_script(directory: Path, delay: float, marker: Path) -> Path:
    script = directory / "child.py"
    script.write_text(
        "import pathlib, time\n"
        f"time.sleep({delay!r})\n"
        f"pathlib.Path({str(marker)!r}).write_text('done')\n",
        encoding="utf-8")
    return script


def _launcher(directory: Path, child: Path, pid_file: Path, exit_delay: float = 0.05) -> Path:
    launcher = directory / "launcher.py"
    launcher.write_text(
        "import pathlib, subprocess, sys, time\n"
        f"child = subprocess.Popen([sys.executable, {str(child)!r}])\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(child.pid))\n"
        f"time.sleep({exit_delay!r})\n",
        encoding="utf-8")
    return launcher


def _case(name: str, launcher: Path, *, supervise: bool, timeout: float,
          cancel_after: float | None, directory: Path, marker: Path, pid_file: Path) -> dict[str, Any]:
    run_dir = directory / f"run-{name}-{'supervised' if supervise else 'default'}"
    token = CancellationToken() if cancel_after is not None else None
    timer = None
    if token is not None:
        timer = threading.Timer(cancel_after, token.cancel)
        timer.start()
    started = time.monotonic()
    try:
        process = run_process([sys.executable, str(launcher)], cwd=directory, env=os.environ.copy(),
                              timeout_seconds=timeout, run_dir=run_dir, log_name=name,
                              supervise_handoff=supervise, cancellation_token=token)
        elapsed = round(time.monotonic() - started, 3)
    finally:
        if timer is not None:
            timer.cancel()
    child_pid = int(pid_file.read_text(encoding="ascii")) if pid_file.exists() else None
    deadline = time.monotonic() + 2.0
    while child_pid and _pid_alive(child_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    child_alive = bool(child_pid and _pid_alive(child_pid))
    if child_alive and child_pid:
        _terminate(child_pid)
    return {
        "case": name,
        "supervise_handoff": supervise,
        "returncode": process.returncode,
        "timed_out": process.timed_out,
        "cancelled": process.cancelled,
        "handoff_supervised": process.handoff_supervised,
        "job_processes_at_exit": process.job_processes_at_exit,
        "elapsed_s": elapsed,
        "child_alive_after_return": child_alive,
        "child_marker_written": marker.exists(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workdir", type=Path, default=PROJECT)
    args = parser.parse_args()
    output = require_allowed_path(args.output, expect_file=True)
    workdir = require_allowed_path(args.workdir, must_exist=True, expect_file=False)
    directory = workdir / ".ansys_mcp" / "cfx-launch-diagnostic"
    shutil.rmtree(directory, ignore_errors=True)
    directory.mkdir(parents=True)
    observations: list[dict[str, Any]] = []
    definitions = [
        ("direct", 0.0, 10.0, None),
        ("handoff", 0.7, 10.0, None),
        ("timeout", 30.0, 0.8, None),
        ("cancel", 30.0, 10.0, 0.4),
    ]
    for name, delay, timeout, cancel_after in definitions:
        marker = directory / f"{name}.marker"
        pid_file = directory / f"{name}.child.pid"
        marker.unlink(missing_ok=True)
        child = _child_script(directory, delay, marker)
        launcher = _launcher(directory, child, pid_file)
        policies = [False, True] if name in {"handoff", "timeout", "cancel"} else [False]
        for supervise in policies:
            if name != "direct":
                marker.unlink(missing_ok=True)
                pid_file.unlink(missing_ok=True)
            observations.append(_case(name, launcher, supervise=supervise, timeout=timeout,
                                      cancel_after=cancel_after, directory=directory,
                                      marker=marker, pid_file=pid_file))
    report = {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "platform": sys.platform,
        "purpose": "Fake-launcher hand-off matrix through the shared supervisor; no Ansys product is launched.",
        "observations": observations,
        "interpretation": {
            "default_policy": "The supervisor returns when the direct launcher exits; closing the "
                              "kill-on-close job terminates the hand-off child.",
            "supervised_policy": "The supervisor waits for the job's remaining processes; timeout and "
                                 "cancellation still terminate the whole job.",
        },
        "authorized_comparison_template": {
            "when": "Only after explicit product acceptance is enabled; never by this diagnostic.",
            "official": "cfx5solve -def <DEF> -double -part <N> -name <NAME>",
            "mcp": "validate_workflow(..., validation_mode='schema') then run_workflow with one step "
                   "{\"type\": \"cfx\", \"recipe\": {\"operation\": {\"op\": \"solve\", ...}}}",
            "capture": ["exact command line", "working directory", "environment diff", "creation time",
                        "process tree and job membership", "log growth over time", "result file mtime/size"],
        },
    }
    _write_json(output, report)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
