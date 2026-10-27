from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator


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
    cancelled: bool = False
    identity: dict[str, Any] | None = None


_DETACHED_PROCESSES: dict[int, subprocess.Popen[bytes]] = {}
_COM_CACHE_LOCK = threading.Lock()
_COM_CACHE_PATH: Path | None = None


def prepare_python_com_cache(directory: Path | None = None) -> Path:
    """Import win32com against a private, writable generated-type cache.

    pywin32 resolves its ``gen_py`` cache from ``%TEMP%`` (unless a registry
    override exists) while ``import win32com.client`` runs. A cache created by
    another identity can be unreadable, and ``gencache.Rebuild`` then fails on
    ``__init__.py`` before any worker process exists. Importing once with a
    private TEMP keeps that shared cache untouched; process children still
    inherit the unmodified environment, so product scratch files are unaffected.
    """
    global _COM_CACHE_PATH
    target = (Path(directory) if directory is not None
              else Path(tempfile.gettempdir()) / "ansys_mcp_com_cache" / str(os.getpid()))
    with _COM_CACHE_LOCK:
        if _COM_CACHE_PATH is not None:
            return _COM_CACHE_PATH
        target.mkdir(parents=True, exist_ok=True)
        probe = target / ".write-probe"
        probe.write_text("", encoding="ascii")
        probe.unlink()
        saved = {name: os.environ.get(name) for name in ("TEMP", "TMP")}
        for name in saved:
            os.environ[name] = str(target)
        try:
            import win32com.client  # noqa: F401 - imported once to pin the cache path
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
        _COM_CACHE_PATH = target
    return target


def prime_python_com_cache(directory: Path | None = None) -> Path | None:
    """Best-effort COM cache priming for worker processes without pywin32."""
    if os.name != "nt":  # pragma: no cover - Linux CI
        return None
    try:
        return prepare_python_com_cache(directory)
    except ImportError:  # pragma: no cover - pywin32 absent
        return None


class WorkflowCancelled(RuntimeError):
    """Cooperative cancellation; callers must not retry a cancelled operation."""


class CancellationToken:
    def __init__(
        self,
        cancel_file: Path | None = None,
        on_process: Callable[[dict[str, Any], bool], None] | None = None,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.cancel_file = cancel_file
        self._event = threading.Event()
        self.on_process = on_process
        self.on_progress = on_progress
        self._process_lock = threading.Lock()
        self._processes: dict[tuple[int, str], dict[str, Any]] = {}
        self.observer_errors: list[str] = []

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set() or bool(self.cancel_file and self.cancel_file.exists())

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise WorkflowCancelled("Workflow cancellation requested; no operation was replayed.")

    def register_process(self, identity: dict[str, Any]) -> None:
        with self._process_lock:
            self._processes[(identity["pid"], identity["creation_time"])] = identity
        if self.on_process:
            try:
                self.on_process(identity, True)
            except Exception as exc:
                self.observer_errors.append(type(exc).__name__)
        if self.cancelled:
            # A blocking product launcher may return after cancellation. It
            # must not introduce a fresh owned process into a cancelled task.
            terminate_owned_process(identity)

    def unregister_process(self, identity: dict[str, Any]) -> None:
        with self._process_lock:
            self._processes.pop((identity["pid"], identity["creation_time"]), None)
        if self.on_process:
            try:
                self.on_process(identity, False)
            except Exception as exc:
                self.observer_errors.append(type(exc).__name__)

    def owned_processes(self) -> list[dict[str, Any]]:
        with self._process_lock:
            return [dict(identity) for identity in self._processes.values()]


_CANCELLATION: ContextVar[CancellationToken | None] = ContextVar("ansys_mcp_cancellation", default=None)


def current_cancellation_token() -> CancellationToken | None:
    return _CANCELLATION.get()


@contextmanager
def cancellation_context(token: CancellationToken) -> Iterator[CancellationToken]:
    marker = _CANCELLATION.set(token)
    try:
        yield token
    finally:
        _CANCELLATION.reset(marker)


def run_process(
    argv: list[str] | str,
    *,
    cwd: Path,
    env: dict[str, str],
    timeout_seconds: float,
    run_dir: Path,
    log_name: str,
    detached: bool = False,
    cancellation_token: CancellationToken | None = None,
) -> ProcessResult:
    token = cancellation_token or current_cancellation_token()
    if token:
        token.raise_if_cancelled()
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive.")
    started = time.monotonic()
    run_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = run_dir / f"{log_name}.stdout.log"
    stderr_path = run_dir / f"{log_name}.stderr.log"
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
    if os.name == "nt" and not detached:
        creationflags |= getattr(subprocess, "CREATE_SUSPENDED", 0x00000004)
    job_handle: int | None = None
    with stdout_path.open("wb") as stdout_file, stderr_path.open("wb") as stderr_file:
        proc = subprocess.Popen(
            getattr(argv, "windows_commandline", argv) if os.name == "nt" else argv,
            cwd=str(cwd),
            env=env,
            stdout=stdout_file,
            stderr=stderr_file,
            creationflags=creationflags,
            start_new_session=os.name != "nt",
            stdin=subprocess.DEVNULL,
        )
        identity = process_identity(proc.pid)
        if token and identity:
            try:
                token.register_process(identity)
                token.raise_if_cancelled()
            except BaseException:
                _terminate_process_tree(proc)
                proc.wait(timeout=5)
                try:
                    token.unregister_process(identity)
                except Exception:
                    pass
                raise
        if detached:
            _track_detached_process(proc, token, identity)
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
                identity=identity,
            )
        if os.name == "nt":
            job_handle, job_error = _create_windows_job(proc)
            resume_error = _resume_windows_process(proc)
            if resume_error:
                proc.kill()
                proc.wait(timeout=5)
                if job_handle:
                    _close_windows_handle(job_handle)
                if token and identity:
                    token.unregister_process(identity)
                raise OSError(resume_error)
        else:
            job_error = None
        timed_out = False
        cancelled = False
        termination_error = None
        deadline = started + timeout_seconds
        try:
            while proc.poll() is None:
                cancelled = bool(token and token.cancelled)
                timed_out = not cancelled and time.monotonic() >= deadline
                if cancelled or timed_out:
                    termination_error = _terminate_process_tree(proc, job_handle)
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=3)
                    break
                try:
                    proc.wait(timeout=min(0.1, max(0.001, deadline - time.monotonic())))
                except subprocess.TimeoutExpired:
                    pass
            returncode = proc.returncode
        finally:
            if job_handle:
                _close_windows_handle(job_handle)
            if token and identity:
                token.unregister_process(identity)
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
        cancelled=cancelled,
        identity=identity,
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
                timeout=3,
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
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        kernel32.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong]
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None, f"CreateJobObjectW error {ctypes.get_last_error()}"
        # Closing the supervisor (including a crashed job worker) must not leave
        # its solver descendants running. This applies only to our new job.
        limits = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        limits.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(ctypes.c_void_p(job), 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.get_last_error()
            kernel32.CloseHandle(ctypes.c_void_p(job))
            return None, f"SetInformationJobObject error {error}"
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
        ntdll.NtResumeProcess.argtypes = [ctypes.c_void_p]
        ntdll.NtResumeProcess.restype = ctypes.c_long
        status = ntdll.NtResumeProcess(ctypes.c_void_p(int(getattr(proc, "_handle"))))
        if status != 0:
            return f"NtResumeProcess status {status}"
    except Exception as exc:  # pragma: no cover - platform fallback
        return repr(exc)
    return None


def _track_detached_process(
    proc: subprocess.Popen[bytes], token: CancellationToken | None = None, identity: dict[str, Any] | None = None
) -> None:
    _DETACHED_PROCESSES[proc.pid] = proc

    def reap() -> None:
        try:
            proc.wait()
        finally:
            _DETACHED_PROCESSES.pop(proc.pid, None)
            if token and identity:
                token.unregister_process(identity)

    threading.Thread(target=reap, name=f"ansys-mcp-reap-{proc.pid}", daemon=True).start()


def _tail_file(path: Path, limit: int = 20000) -> str:
    if not path.exists():
        return ""
    with path.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(max(0, size - limit))
        return stream.read().decode("utf-8", errors="replace")


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32), ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_uint32), ("SchedulingClass", ctypes.c_uint32),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount"
    )]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION), ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _windows_process_info(handle: int) -> dict[str, Any] | None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    creation, exit_time, kernel, user = (ctypes.c_uint64() for _ in range(4))
    code = ctypes.c_ulong()
    if not kernel32.GetProcessTimes(ctypes.c_void_p(handle), ctypes.byref(creation), ctypes.byref(exit_time), ctypes.byref(kernel), ctypes.byref(user)):
        return None
    if not kernel32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code)):
        return None
    return {"creation_time": str(creation.value), "alive": code.value == 259}


def process_identity(pid: int) -> dict[str, Any] | None:
    """Read a live OS creation identity, rather than trusting a PID in a file."""
    if pid <= 0:
        return None
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            info = _windows_process_info(int(handle))
            return {"pid": pid, "creation_time": info["creation_time"]} if info and info["alive"] else None
        finally:
            _close_windows_handle(int(handle))
    try:  # Linux creation ticks avoid interpreting a recycled PID as our worker.
        stat = Path(f"/proc/{pid}/stat").read_text()
        fields = stat[stat.rfind(")") + 2:].split()
        if fields[0] == "Z":
            return None
        return {"pid": pid, "creation_time": fields[19]}
    except (OSError, IndexError):
        return None


def process_identity_matches(identity: dict[str, Any] | None) -> bool:
    if not identity or not isinstance(identity.get("pid"), int):
        return False
    observed = process_identity(identity["pid"])
    return observed is not None and observed == identity


def terminate_owned_process(identity: dict[str, Any]) -> bool:
    """Terminate exactly the OS process identified by a recorded creation time.

    Windows keeps the verified handle open through TerminateProcess so a PID
    recycled between verification and termination cannot target another app.
    Owned descendants are handled by the worker's kill-on-close Job Objects.
    """
    pid = identity.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = kernel32.OpenProcess(0x1000 | 0x0001, False, pid)
        if not handle:
            return False
        try:
            info = _windows_process_info(int(handle))
            if not info or not info["alive"] or info["creation_time"] != identity.get("creation_time"):
                return False
            return bool(kernel32.TerminateProcess(ctypes.c_void_p(handle), 1))
        finally:
            _close_windows_handle(int(handle))
    if not process_identity_matches(identity):  # pragma: no cover - Linux CI
        return False
    try:
        os.kill(pid, signal.SIGKILL)
        return True
    except OSError:
        return False


def launch_independent_process(argv: list[str], *, cwd: Path, env: dict[str, str],
                               com_cache_dir: Path | None = None) -> dict[str, Any]:
    """Create a durable worker outside the MCP client's process lifetime.

    Windows MCP stdio clients use kill-on-close Job Objects without breakaway.
    CREATE_NEW_PROCESS_GROUP and an intermediate parent do not escape those
    inherited Jobs. Local Win32_Process.Create uses the caller's credentials
    without associating the new process with the caller's Job. No Job policy,
    service, registry or security setting is changed.
    https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects
    """
    if os.name != "nt":  # pragma: no cover - Linux CI
        proc = subprocess.Popen(argv, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        identity = process_identity(proc.pid)
        _track_detached_process(proc)
    else:
        try:
            # Importing win32com resolves pywin32's gen_py cache. Use a private
            # writable location instead of a shared cache owned by another
            # identity; otherwise Rebuild() fails before Win32_Process.Create.
            prepare_python_com_cache(com_cache_dir)
            import pythoncom
            import win32com.client
        except ImportError as exc:
            raise RuntimeError("Independent Windows workers require pywin32; no lifetime-bound fallback was started.") from exc

        def create() -> int:
            services = win32com.client.GetObject("winmgmts:{impersonationLevel=impersonate}!\\\\.\\root\\cimv2")
            startup = services.Get("Win32_ProcessStartup").SpawnInstance_()
            startup.ShowWindow = 0
            startup.EnvironmentVariables = [str(key) + "=" + str(value) for key, value in env.items()]
            method = services.Get("Win32_Process").Methods_("Create")
            parameters = method.InParameters.SpawnInstance_()
            parameters.CommandLine = subprocess.list2cmdline(argv)
            parameters.CurrentDirectory = str(cwd)
            parameters.ProcessStartupInformation = startup
            output = services.ExecMethod("Win32_Process", "Create", parameters)
            code = int(output.Properties_("ReturnValue").Value)
            if code:
                raise OSError("Independent Windows worker creation failed (WMI return code " + str(code) + "); no fallback was started.")
            return int(output.Properties_("ProcessId").Value)

        pythoncom.CoInitialize()
        try:
            # create() releases all apartment-bound COM objects before the
            # CoUninitialize call, avoiding cross-thread IUnknown finalization.
            pid = create()
        finally:
            pythoncom.CoUninitialize()
        identity = process_identity(pid)
    if identity is None:
        raise RuntimeError("Independent worker exited before its creation identity could be verified.")
    return identity
