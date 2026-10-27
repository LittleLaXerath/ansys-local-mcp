"""Explicit UAC execution sessions over authenticated, local-only JSON IPC.

The server stays unelevated. This module neither changes Windows security
settings nor accepts arbitrary remote hosts. Private bootstrap credentials are
restricted to the current Windows user and SYSTEM before they are written.
"""
from __future__ import annotations

import ctypes
import json
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
import uuid
from multiprocessing.connection import Client
from pathlib import Path
from typing import Any

from .config import allowed_roots, merge_local_no_proxy, require_allowed_path
from .jobs import _read_json, _write_json
from .process_supervisor import WorkflowCancelled, current_cancellation_token, launch_independent_process, process_identity_matches


class ElevationError(RuntimeError):
    def __init__(self, message: str, *, request_id: str | None = None):
        self.request_id = request_id
        super().__init__(message)


class ElevatedRequestRejected(ElevationError):
    """The worker explicitly rejected a request before executing its handler."""


class ElevatedRequestTimeout(ElevationError):
    def __init__(self, request_id: str):
        super().__init__("Execution outcome is unknown; query request " + request_id + " instead of replaying it.", request_id=request_id)


class ElevatedLaunchTimeout(ElevationError):
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        super().__init__("Execution worker launch timed out; inspect and attach the same state directory before another launch.")


def _restrict_directory(path: Path) -> None:
    if os.name != "nt":
        path.chmod(0o700)
        return
    identity = subprocess.run(["whoami.exe", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, check=True, timeout=10)
    match = re.search(r"S-1-\d+(?:-\d+)+", identity.stdout)
    if not match:
        raise ElevationError("Unable to establish the current Windows user SID.")
    subprocess.run(
        ["icacls.exe", str(path), "/inheritance:r", "/grant:r", "*" + match.group(0) + ":(OI)(CI)F", "*S-1-5-18:(OI)(CI)F"],
        check=True, capture_output=True, timeout=10,
    )


def _launch_uac(parameters: list[str], directory: Path) -> None:
    from ctypes import wintypes

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD), ("fMask", wintypes.ULONG), ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD), ("hIcon", wintypes.HANDLE), ("hProcess", wintypes.HANDLE),
        ]

    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x00000040 | 0x00000100  # NOCLOSEPROCESS, NOASYNC
    info.lpVerb = "runas"
    info.lpFile = sys.executable
    info.lpParameters = subprocess.list2cmdline(parameters)
    info.lpDirectory = str(directory)
    info.nShow = 0
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        code = ctypes.get_last_error()
        if code == 1223:
            raise ElevationError("Windows elevation was declined; no operation was started.")
        raise ElevationError("Windows elevation launch failed (error " + str(code) + ").")
    if info.hProcess:
        ctypes.WinDLL("kernel32").CloseHandle(ctypes.c_void_p(info.hProcess))


class ElevatedSession:
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        self._bootstrap = _read_json(state_dir / "bootstrap.json")
        self._ready = _read_json(state_dir / "ready.json")
        self._lock = threading.Lock()

    @classmethod
    def launch(
        cls, handler_module: str, handler_function: str = "worker_dispatch", *, state_dir: Path,
        timeout_seconds: float = 120, elevated: bool = True,
    ) -> "ElevatedSession":
        if not re.fullmatch(r"ansys_mcp(?:\.[a-zA-Z_][a-zA-Z_0-9]*)+", handler_module):
            raise ElevationError("Execution handlers must be installed ansys_mcp modules.")
        if not re.fullmatch(r"[a-zA-Z_][a-zA-Z_0-9]*", handler_function):
            raise ElevationError("Invalid execution handler name.")
        if elevated and os.name != "nt":
            raise ElevationError("UAC elevation is available only on Windows.")
        directory = require_allowed_path(state_dir)
        directory.mkdir(parents=True, exist_ok=False)
        _restrict_directory(directory)
        _write_json(directory / "bootstrap.json", {
            "version": "1", "authkey": secrets.token_hex(32), "handler_module": handler_module,
            "handler_function": handler_function, "elevated": elevated,
            "allowed_roots": [str(root) for root in allowed_roots()],
        })
        worker = Path(__file__).with_name("elevation_worker.py")
        parameters = [str(worker), str(directory / "bootstrap.json")]
        env = merge_local_no_proxy(os.environ)
        if elevated:
            # A runas child can inherit an MCP stdio client's kill-on-close
            # Job. Ask an independent, ordinary WMI process to request UAC so
            # the administrator actor also has an independent lifetime.
            bootstrap = Path(__file__).with_name("elevation_bootstrap.py")
            starter = launch_independent_process([sys.executable, str(bootstrap), str(directory / "bootstrap.json")],
                                                 cwd=worker.parent.parent, env=env,
                                                 com_cache_dir=directory / "comcache")
            _write_json(directory / "launcher.json", {"worker": starter, "mode": "independent_uac_bootstrap"})
        else:
            launch_independent_process([sys.executable, *parameters], cwd=worker.parent.parent, env=env,
                                       com_cache_dir=directory / "comcache")
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if (directory / "ready.json").exists():
                result = cls(directory)
                if process_identity_matches(result._ready.get("worker")):
                    return result
                raise ElevationError("Execution worker exited before initialization completed.")
            if (directory / "startup-error.json").exists():
                error = _read_json(directory / "startup-error.json")
                raise ElevationError(error.get("message", "Execution worker initialization failed."))
            time.sleep(0.05)
        raise ElevatedLaunchTimeout(directory)

    @classmethod
    def attach(cls, state_dir: Path) -> "ElevatedSession":
        session = cls(require_allowed_path(state_dir, must_exist=True, expect_file=False))
        if not process_identity_matches(session._ready.get("worker")):
            raise ElevationError("Execution worker is absent or its creation identity differs.")
        return session

    def _exchange(self, message: dict[str, Any], timeout_seconds: float = 10) -> dict[str, Any]:
        if not process_identity_matches(self._ready.get("worker")):
            raise ElevationError("Execution worker is absent; no operation was replayed.")
        address = self._ready.get("address")
        if not isinstance(address, list) or len(address) != 2 or address[0] != "127.0.0.1":
            raise ElevationError("Execution session is not bound to the local loopback interface.")
        responses: queue.Queue[Any] = queue.Queue(maxsize=1)

        def exchange() -> None:
            try:
                connection = Client(tuple(address), authkey=bytes.fromhex(self._bootstrap["authkey"]))
                try:
                    connection.send_bytes(json.dumps(message).encode("utf-8"))
                    if not connection.poll(timeout_seconds):
                        raise TimeoutError("Execution worker response timed out.")
                    responses.put(json.loads(connection.recv_bytes(64 * 1024 * 1024)))
                finally:
                    connection.close()
            except BaseException as exc:
                responses.put(exc)

        with self._lock:
            threading.Thread(target=exchange, daemon=True).start()
            try:
                response = responses.get(timeout=timeout_seconds + 0.2)
            except queue.Empty:
                raise ElevatedRequestTimeout(str(message.get("request_id", "unknown"))) from None
        if isinstance(response, BaseException):
            raise ElevationError(str(response)) from response
        return response

    def request(self, operation: str, payload: dict[str, Any], timeout_seconds: float = 120) -> dict[str, Any]:
        token = current_cancellation_token()
        if token:
            token.raise_if_cancelled()
        request_id = uuid.uuid4().hex
        try:
            acknowledgement = self._exchange({"command": "execute", "request_id": request_id, "operation": operation, "payload": payload})
        except Exception as exc:
            raise ElevatedRequestTimeout(request_id) from exc
        if not isinstance(acknowledgement, dict):
            raise ElevatedRequestTimeout(request_id)
        if not acknowledgement.get("ok"):
            raise ElevatedRequestRejected(acknowledgement.get("error", "Execution request rejected."))
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if token and token.cancelled:
                try:
                    self.cancel(request_id)
                except Exception as exc:
                    raise ElevatedRequestTimeout(request_id) from exc
                exc = WorkflowCancelled("Cancellation was requested through authenticated local IPC; observe the same request identity to verify product shutdown.")
                exc.request_id = request_id
                raise exc
            try:
                response = self.query(request_id)
            except Exception as exc:
                raise ElevatedRequestTimeout(request_id) from exc
            if not isinstance(response, dict):
                raise ElevationError("Execution observation is invalid; inspect the original request.", request_id=request_id)
            if response.get("status") == "complete":
                if not isinstance(response.get("result"), dict):
                    raise ElevationError("Completed execution response omitted its result; inspect the original request.", request_id=request_id)
                return response["result"]
            if response.get("status") == "cancelled":
                return response.get("result", {"ok": False, "status": "cancelled", "diagnostics": [{"code": "WORKFLOW_CANCELLED", "message": "Execution request was cancelled."}]})
            if response.get("status") == "failed":
                raise ElevationError(response.get("error", "Execution handler failed."), request_id=request_id)
            time.sleep(0.05)
        raise ElevatedRequestTimeout(request_id)

    def query(self, request_id: str) -> dict[str, Any]:
        return self._exchange({"command": "query", "request_id": request_id})

    def summary(self) -> dict[str, Any]:
        return self._exchange({"command": "summary"})

    def cancel(self, request_id: str, grace_seconds: float = 5.0) -> dict[str, Any]:
        """Ask the privileged worker to cancel its own verified processes.

        The unelevated controller never attempts to kill an administrator PID.
        A completed request is not replayed and is not retrospectively cancelled.
        """
        return self._exchange({"command": "cancel", "request_id": request_id, "grace_seconds": max(0.1, grace_seconds)})

    def close(self) -> dict[str, Any]:
        return self._exchange({"command": "close"})
