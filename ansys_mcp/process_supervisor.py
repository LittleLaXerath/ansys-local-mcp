from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProcessResult:
    returncode: int | None
    pid: int
    timed_out: bool
    detached: bool
    stdout_path: Path
    stderr_path: Path
    stdout_tail: str
    stderr_tail: str
    elapsed_s: float
    termination_error: str | None = None


_DETACHED_PROCESSES: dict[int, subprocess.Popen[bytes]] = {}


def run_process(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout_seconds: float,
    run_dir: Path,
    log_name: str,
    detached: bool = False,
) -> ProcessResult:
    started = time.monotonic()
    stdout_path = run_dir / f"{log_name}.stdout.log"
    stderr_path = run_dir / f"{log_name}.stderr.log"
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
    if os.name == "nt" and not detached:
        creationflags |= getattr(subprocess, "CREATE_SUSPENDED", 0x00000004)
    job_handle: int | None = None
    with stdout_path.open("wb") as stdout_file, stderr_path.open("wb") as stderr_file:
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=env,
            stdout=stdout_file,
            stderr=stderr_file,
            creationflags=creationflags,
            start_new_session=os.name != "nt",
        )
        if detached:
            _track_detached_process(proc)
            return ProcessResult(
                returncode=None,
                pid=proc.pid,
                timed_out=False,
                detached=True,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                stdout_tail="",
                stderr_tail="",
                elapsed_s=round(time.monotonic() - started, 3),
            )
        if os.name == "nt":
            job_handle, job_error = _create_windows_job(proc)
            resume_error = _resume_windows_process(proc)
            if resume_error:
                proc.kill()
                if job_handle:
                    _close_windows_handle(job_handle)
                raise OSError(resume_error)
        else:
            job_error = None
        try:
            returncode = proc.wait(timeout=max(0.001, timeout_seconds))
            timed_out = False
            termination_error = None
        except subprocess.TimeoutExpired:
            timed_out = True
            termination_error = _terminate_process_tree(proc, job_handle)
            try:
                returncode = proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                returncode = proc.wait(timeout=5)
        finally:
            if job_handle:
                _close_windows_handle(job_handle)
        if job_error and not termination_error:
            termination_error = job_error

    return ProcessResult(
        returncode=returncode,
        pid=proc.pid,
        timed_out=timed_out,
        detached=False,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        stdout_tail=_tail_file(stdout_path),
        stderr_tail=_tail_file(stderr_path),
        elapsed_s=round(time.monotonic() - started, 3),
        termination_error=termination_error,
    )


def _terminate_process_tree(proc: subprocess.Popen[bytes], job_handle: int | None = None) -> str | None:
    try:
        if os.name == "nt":
            if job_handle:
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                if kernel32.TerminateJobObject(ctypes.c_void_p(job_handle), 1):
                    return None
                job_error = ctypes.get_last_error()
            else:
                job_error = None
            completed = subprocess.run(
                ["taskkill.exe", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=20,
            )
            if completed.returncode not in {0, 128}:
                proc.kill()
                details = completed.stderr.strip() or f"taskkill exited {completed.returncode}"
                if job_error:
                    details = f"TerminateJobObject error {job_error}; {details}"
                return details
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception as exc:  # pragma: no cover - platform fallback
        try:
            proc.kill()
        except Exception:
            pass
        return repr(exc)
    return None


def _create_windows_job(proc: subprocess.Popen[bytes]) -> tuple[int | None, str | None]:
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None, f"CreateJobObjectW error {ctypes.get_last_error()}"
        process_handle = ctypes.c_void_p(int(getattr(proc, "_handle")))
        if not kernel32.AssignProcessToJobObject(ctypes.c_void_p(job), process_handle):
            error = ctypes.get_last_error()
            kernel32.CloseHandle(ctypes.c_void_p(job))
            return None, f"AssignProcessToJobObject error {error}"
        return int(job), None
    except Exception as exc:  # pragma: no cover - platform fallback
        return None, repr(exc)


def _close_windows_handle(handle: int) -> None:
    try:
        ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(ctypes.c_void_p(handle))
    except Exception:
        pass


def _resume_windows_process(proc: subprocess.Popen[bytes]) -> str | None:
    try:
        ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
        status = ntdll.NtResumeProcess(ctypes.c_void_p(int(getattr(proc, "_handle"))))
        if status != 0:
            return f"NtResumeProcess status {status}"
    except Exception as exc:  # pragma: no cover - platform fallback
        return repr(exc)
    return None


def _track_detached_process(proc: subprocess.Popen[bytes]) -> None:
    _DETACHED_PROCESSES[proc.pid] = proc

    def reap() -> None:
        try:
            proc.wait()
        finally:
            _DETACHED_PROCESSES.pop(proc.pid, None)

    threading.Thread(target=reap, name=f"ansys-mcp-reap-{proc.pid}", daemon=True).start()


def _tail_file(path: Path, limit: int = 20000) -> str:
    if not path.exists():
        return ""
    with path.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(max(0, size - limit))
        return stream.read().decode("utf-8", errors="replace")
