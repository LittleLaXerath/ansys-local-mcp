"""Durable, independently supervised workflow jobs.

Only this module's worker processes are cancellable. A stale manifest never
causes execution or a retry; OS PID and creation identity are always checked.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import allowed_roots, default_workdir, require_allowed_path
from .process_supervisor import launch_independent_process, process_identity_matches, terminate_owned_process


_RUN_ID = re.compile(r"^[a-f0-9]{32}$")
_TERMINAL = {"complete", "failed", "timeout", "cancelled", "interrupted", "needs_attention"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, value: Any) -> None:
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, indent=2, ensure_ascii=True), encoding="utf-8")
        # A Windows reader or antivirus can briefly hold a destination without
        # FILE_SHARE_DELETE. Retry only this metadata publication, never work.
        deadline = time.monotonic() + 2
        while True:
            try:
                temporary.replace(path)
                break
            except PermissionError:
                if os.name != "nt" or time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Invalid job record.")
    return value


def _job_dir(run_id: str) -> Path:
    if not _RUN_ID.fullmatch(run_id):
        raise ValueError("Invalid workflow run_id.")
    return require_allowed_path(default_workdir() / ".ansys_mcp" / "jobs" / run_id)


def _failure(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "status": "failed", "diagnostics": [{"code": code, "severity": "error", "message": message}]}


def pending_observations(summary: dict[str, Any]) -> list[dict[str, Any]]:
    """Keep only observation handles; never retain a recipe for automatic replay."""
    handles = []
    for step in summary.get("steps", []):
        if step.get("status") != "unknown":
            continue
        outputs = step.get("outputs", {})
        handle = {"step_id": step.get("id"), "type": step.get("type")}
        session = outputs.get("session", {})
        if session.get("id") and outputs.get("request_id"):
            handle.update(session_id=session["id"], request_id=outputs["request_id"])
        if outputs.get("worker_id") and outputs.get("worker_request_id"):
            handle.update(worker_id=outputs["worker_id"], worker_request_id=outputs["worker_request_id"])
        handles.append(handle)
    return handles


def _forced_stop_record(manifest: dict[str, Any], message: str) -> dict[str, Any]:
    result = dict(manifest, ok=False, status="unknown", outcome="unknown", updated_at=_now(), cancellation_requested=True)
    result.pop("result", None)
    result["diagnostics"] = [*manifest.get("diagnostics", []), {
        "code": "JOB_WORKER_FORCE_STOPPED", "severity": "warning", "message": message,
    }]
    return result


def _observe_products(handles: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    observations = []
    all_terminal = bool(handles)
    for handle in handles:
        observation: dict[str, Any]
        try:
            if handle.get("worker_id") and handle.get("worker_request_id"):
                from .mechanical import mechanical_elevated_request_status
                observation = mechanical_elevated_request_status(handle["worker_id"], handle["worker_request_id"])
                # A completed IPC envelope can contain a still-live Mechanical
                # future. Observe that original future, rather than its wrapper.
                result = observation.get("result")
                if isinstance(result, dict) and result.get("status") == "unknown":
                    observation = result
                    outputs = result.get("outputs", {})
                    session_id = outputs.get("session", {}).get("id")
                    request_id = outputs.get("request_id")
                    if session_id and request_id:
                        from .mechanical import mechanical_request_status
                        observation = mechanical_request_status(session_id, request_id)
            elif handle.get("session_id") and handle.get("request_id"):
                from .mechanical import mechanical_request_status
                observation = mechanical_request_status(handle["session_id"], handle["request_id"])
            else:
                observation = {"ok": False, "status": "unknown", "diagnostics": [{"code": "JOB_OBSERVATION_HANDLE_MISSING", "message": "No product observation handle was returned; the operation was not replayed."}]}
        except Exception as exc:
            observation = {"ok": False, "status": "unknown", "diagnostics": [{"code": "JOB_OBSERVATION_UNAVAILABLE", "message": str(exc)}]}
        codes = {item.get("code") for item in observation.get("diagnostics", [])}
        unresolved = bool(codes.intersection({
            "MECHANICAL_SESSION_MISSING", "MECHANICAL_REQUEST_MISSING", "MECHANICAL_WORKER_MISSING",
            "MECHANICAL_WORKER_UNAVAILABLE", "MECHANICAL_WORKER_IDENTITY_CHANGED", "MECHANICAL_REGISTRY_ID_INVALID",
            "JOB_OBSERVATION_HANDLE_MISSING", "JOB_OBSERVATION_UNAVAILABLE",
        }))
        terminal = observation.get("status") in {"complete", "completed", "failed", "cancelled", "closed", "needs_attention"} and not unresolved
        all_terminal = all_terminal and terminal
        observations.append({**handle, "observation": observation, "terminal": terminal})
    return observations, all_terminal


def start_workflow(
    workflow: dict[str, Any] | None = None,
    workflow_path: str | None = None,
    variables: dict[str, Any] | None = None,
    timeout_seconds: int = 7200,
) -> dict[str, Any]:
    from .workflow import load_workflow, validate_workflow

    if timeout_seconds <= 0:
        return _failure("VALIDATION_ERROR", "timeout_seconds must be positive.")
    validation = validate_workflow(workflow, workflow_path, variables)
    if not validation["ok"]:
        return validation
    run_id = uuid.uuid4().hex
    try:
        model = load_workflow(workflow, workflow_path, variables)
        if model.workdir is None:
            model = model.model_copy(update={"workdir": str(default_workdir())})
        directory = _job_dir(run_id)
        directory.mkdir(parents=True, exist_ok=False)
        _write_json(directory / "request.json", {"workflow": model.model_dump(mode="json", exclude_none=True), "timeout_seconds": timeout_seconds})
        manifest = {
            "version": "1", "run_id": run_id, "status": "starting", "ok": True,
            "created_at": _now(), "updated_at": _now(), "worker": None,
            "owned_processes": [], "result_path": None, "diagnostics": [],
        }
        _write_json(directory / "job.json", manifest)
        env = os.environ.copy()
        env["ANSYS_MCP_ALLOWED_ROOTS"] = ";".join(str(root) for root in allowed_roots())
        package_root = str(Path(__file__).resolve().parent.parent)
        env["PYTHONPATH"] = package_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        identity = launch_independent_process(
            [sys.executable, "-m", "ansys_mcp.job_worker", str(directory)], cwd=directory, env=env,
        )
        manifest["worker"] = identity
        _write_json(directory / "job.json", manifest)
        _write_json(directory / "launch.ready.json", {"worker": identity})
        return get_workflow_status(run_id)
    except Exception as exc:
        # A launch with an unknown outcome must be inspected, never replayed.
        result = _failure("JOB_LAUNCH_ERROR", str(exc))
        result["run_id"] = run_id
        return result


def get_workflow_status(run_id: str) -> dict[str, Any]:
    try:
        directory = _job_dir(run_id)
        manifest = _read_json(directory / "job.json")
        if manifest.get("run_id") != run_id:
            raise ValueError("Job identity does not match its directory.")
        stored_result = _read_json(directory / "result.json") if manifest.get("result_path") else None
        handles = manifest.get("pending_observations") or pending_observations(stored_result or {})
        if handles and manifest.get("status") != "needs_attention":
            # Recover older records which incorrectly labelled a cancellation
            # or artifact error as terminal despite an active product handle.
            manifest.update(ok=False, status="unknown", pending_observations=handles)
        live = process_identity_matches(manifest.get("worker"))
        if not live and manifest.get("status") not in _TERMINAL and manifest.get("status") != "unknown":
            # The worker may have atomically published its final record between
            # our first read and the OS liveness check. Once it is dead a second
            # read observes that publication instead of inventing an interrupt.
            manifest = _read_json(directory / "job.json")
            handles = manifest.get("pending_observations") or pending_observations(stored_result or {})
            if handles and manifest.get("status") != "needs_attention":
                manifest.update(ok=False, status="unknown", pending_observations=handles)
            live = process_identity_matches(manifest.get("worker"))
        result = dict(manifest, worker_alive=live, job_dir=str(directory))
        if manifest.get("status") not in _TERMINAL and manifest.get("status") != "unknown" and not live:
            result.update(ok=False, status="interrupted", outcome="unknown")
            result["diagnostics"] = [*manifest.get("diagnostics", []), {
                "code": "JOB_WORKER_MISSING", "severity": "error",
                "message": "Recorded worker is absent or its creation identity differs; execution was not replayed.",
            }]
        elif manifest.get("status") not in _TERMINAL and (directory / "cancel.request.json").exists():
            result["status"] = "cancel_requested"
        if manifest.get("result_path"):
            # Never follow a path supplied by a manifest to outside its job.
            result["result"] = stored_result or _read_json(directory / "result.json")
        if manifest.get("status") == "unknown":
            observations, settled = _observe_products(manifest.get("pending_observations", []))
            result.update(product_observations=observations, outcome="unknown", original_result=result.get("result"))
            if manifest.get("domain_cancel_requested"):
                result["status"] = "cancel_requested"
            if settled:
                result.update(status="needs_attention", workflow_resume_required=True)
                if not live:
                    manifest.update(status="needs_attention", workflow_resume_required=True,
                                    product_observations=observations, updated_at=_now())
                    _write_json(directory / "job.json", manifest)
            elif not observations:
                result["observation_reason"] = "No product observation handle was returned; no execution was replayed."
        elif manifest.get("status") == "needs_attention":
            result["original_result"] = result.get("result")
        return result
    except (OSError, ValueError, TypeError) as exc:
        return dict(_failure("JOB_NOT_FOUND", str(exc)), run_id=run_id)


def cancel_workflow(run_id: str, grace_seconds: float = 5.0) -> dict[str, Any]:
    status = get_workflow_status(run_id)
    if status.get("status") in {"unknown", "cancel_requested"} and status.get("pending_observations"):
        cancellations = []
        from .mechanical import cancel_mechanical_request
        for handle in status["pending_observations"]:
            arguments = {key: handle[key] for key in ("session_id", "request_id", "worker_id", "worker_request_id") if key in handle}
            if not arguments:
                continue
            try:
                response = cancel_mechanical_request(**arguments, grace_seconds=grace_seconds)
            except Exception as exc:
                response = _failure("JOB_PRODUCT_CANCEL_UNAVAILABLE", str(exc))
            cancellations.append({**handle, "cancellation": response})
        directory = _job_dir(run_id)
        manifest = _read_json(directory / "job.json")
        manifest.update(cancel_observations=cancellations, updated_at=_now())
        if any(item["cancellation"].get("ok") for item in cancellations):
            manifest["domain_cancel_requested"] = True
        _write_json(directory / "job.json", manifest)
        return get_workflow_status(run_id)
    if status.get("status") in _TERMINAL or not status.get("worker_alive"):
        return status
    directory = _job_dir(run_id)
    _write_json(directory / "cancel.request.json", {"requested_at": _now()})
    # This watchdog survives the calling request, but does not need to outlive
    # the server: the independent worker also watches this cancellation file.
    def force_after_grace() -> None:
        deadline = time.monotonic() + max(0.1, grace_seconds)
        while time.monotonic() < deadline:
            observed = get_workflow_status(run_id)
            if observed.get("status") in _TERMINAL:
                return
            time.sleep(0.1)
        observed = get_workflow_status(run_id)
        if observed.get("status") in _TERMINAL:
            return
        for identity in observed.get("owned_processes", []):
            terminate_owned_process(identity)
        # Terminating the owning worker also closes its Windows Job handles,
        # which tears down only descendants created by that worker.
        if terminate_owned_process(observed.get("worker") or {}):
            latest = _read_json(directory / "job.json")
            if latest.get("status") in _TERMINAL:
                return
            observed = latest
            observed = _forced_stop_record(observed, "Owned worker was stopped after the cancellation grace period. Product outcomes remain unverified; observe the original request handles.")
            _write_json(directory / "job.json", observed)

    threading.Thread(target=force_after_grace, name="ansys-mcp-cancel-" + run_id[:8], daemon=True).start()
    return get_workflow_status(run_id)
