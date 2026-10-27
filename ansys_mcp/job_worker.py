"""Private entry point for durable workflow jobs (not an MCP transport)."""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .jobs import _TERMINAL, _forced_stop_record, _now, _read_json, _write_json, pending_observations
from .process_supervisor import (
    CancellationToken, WorkflowCancelled, cancellation_context, prime_python_com_cache,
    process_identity, terminate_owned_process,
)


def _publish_result(directory: Path, manifest: dict[str, Any], result: dict[str, Any]) -> None:
    handles = pending_observations(result)
    if handles:
        result = dict(result, ok=False, status="unknown", stage="unknown")
    _write_json(directory / "result.json", result)
    manifest.update(status=result["status"], ok=result["ok"], updated_at=_now(), finished_at=_now(),
                    workflow_run_id=result.get("run_id"), result_path="result.json",
                    diagnostics=result.get("diagnostics", []), pending_observations=handles)
    _write_json(directory / "job.json", manifest)


def main() -> None:
    directory = Path(sys.argv[1]).resolve(strict=True)
    # Pin pywin32's generated-type cache to this job's private directory before
    # any product module is imported; product children keep the inherited TEMP.
    prime_python_com_cache(directory / "comcache")
    sys.stdout = (directory / "worker.stdout.log").open("a", encoding="utf-8", buffering=1)
    sys.stderr = (directory / "worker.stderr.log").open("a", encoding="utf-8", buffering=1)
    ready = directory / "launch.ready.json"
    deadline = time.monotonic() + 10
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    if not ready.exists():
        raise RuntimeError("Worker launch handshake expired; workflow was not started.")
    expected = _read_json(ready).get("worker")
    current = process_identity(os.getpid())
    # Windows venv python.exe may be a redirector which owns a second, real
    # interpreter process. Verify that exact parent before recording our PID.
    if expected != current and expected != process_identity(os.getppid()):
        raise RuntimeError("Worker creation identity mismatch; workflow was not started.")
    manifest = _read_json(directory / "job.json")
    manifest["launcher"] = expected
    manifest["worker"] = current
    request = _read_json(directory / "request.json")
    lock = threading.Lock()

    def on_process(identity: dict[str, Any], active: bool) -> None:
        with lock:
            children = [value for value in manifest.get("owned_processes", []) if value != identity]
            if active:
                children.append(identity)
            manifest.update(owned_processes=children, updated_at=_now())
            _write_json(directory / "job.json", manifest)

    def on_progress(summary: dict[str, Any]) -> None:
        steps = [{key: value for key, value in step.items() if key in {"id", "type", "status", "ok", "elapsed_s", "attempt_count"}}
                 for step in summary.get("steps", [])]
        with lock:
            manifest.update(workflow_run_id=summary.get("run_id"), updated_at=_now(), progress={
                "status": summary.get("status"), "stage": summary.get("stage"),
                "completed_steps": len(steps), "steps": steps,
                "diagnostics": summary.get("diagnostics", []),
            })
            manifest["pending_observations"] = pending_observations(summary)
            _write_json(directory / "job.json", manifest)

    token = CancellationToken(directory / "cancel.request.json", on_process=on_process, on_progress=on_progress)
    finished = threading.Event()

    def cancellation_watchdog() -> None:
        while not finished.wait(0.1):
            if not token.cancelled:
                continue
            if finished.wait(5):
                return
            with lock:
                if manifest.get("status") in _TERMINAL and not manifest.get("pending_observations"):
                    return
                for identity in manifest.get("owned_processes", []):
                    terminate_owned_process(identity)
                manifest.update(_forced_stop_record(manifest, "Owned worker stopped after the cancellation grace period. Product outcomes remain unverified; observe the original request handles."))
                _write_json(directory / "job.json", manifest)
            # Closing this process releases its kill-on-close Windows Job handles.
            os._exit(1)

    threading.Thread(target=cancellation_watchdog, name="ansys-mcp-worker-cancel", daemon=True).start()
    with lock:
        manifest.update(status="running", started_at=_now(), updated_at=_now())
        _write_json(directory / "job.json", manifest)
    try:
        token.raise_if_cancelled()
        from .workflow import run_workflow
        with cancellation_context(token):
            result = run_workflow(workflow=request["workflow"], timeout_seconds=request["timeout_seconds"])
        # run_workflow reports the actual observed outcome. A cancellation
        # arriving after completion must not retroactively replace it.
        with lock:
            _publish_result(directory, manifest, result)
    except BaseException as exc:
        with lock:
            manifest.update(
                status=("cancelled" if isinstance(exc, WorkflowCancelled) and not manifest.get("pending_observations")
                        else "unknown" if token.cancelled or manifest.get("pending_observations") else "failed"),
                ok=False, updated_at=_now(), finished_at=_now(),
                diagnostics=[{"code": "WORKFLOW_CANCELLED" if token.cancelled else "JOB_WORKER_ERROR", "severity": "error", "message": str(exc)}],
            )
            _write_json(directory / "job.json", manifest)
    finally:
        finished.set()


if __name__ == "__main__":
    main()
