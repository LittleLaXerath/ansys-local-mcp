"""Managed Fluent actors using the existing authenticated execution worker.

Registry files are observations, never permission to attach to arbitrary Fluent
ports. Reconnection authenticates the original worker and its creation identity.
"""
from __future__ import annotations

import contextlib
import importlib.metadata
import json
import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .config import allowed_roots, known_paths, merge_local_no_proxy, require_allowed_path
from .elevation import ElevatedSession, ElevatedRequestTimeout
from .jobs import _read_json, _write_json
from .process_supervisor import current_cancellation_token, process_identity, process_identity_matches
from .pyfluent_recipe_runner import _launch_kwargs, _read_case, _run_operation, _write_outputs
from .schemas import PyFluentRecipe


class FluentError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def load_pyfluent(runtime: str) -> Any:
    if runtime == "bundled":
        source = known_paths()["pyfluent_core"]
        if not source.is_dir():
            raise FluentError("PYFLUENT_IMPORT_ERROR", "Bundled PyFluent is unavailable.")
        sys.path.insert(0, str(source))
    try:
        import ansys.fluent.core as pyfluent
    except Exception as exc:
        raise FluentError("PYFLUENT_IMPORT_ERROR", f"Explicit {runtime} runtime import failed: {exc}") from exc
    if runtime == "bundled" and not Path(pyfluent.__file__).resolve().is_relative_to(source.resolve()):
        raise FluentError("PYFLUENT_RUNTIME_MISMATCH", "Another PyFluent runtime is already imported; no fallback was attempted.")
    if runtime == "client" and Path(pyfluent.__file__).resolve().is_relative_to(known_paths()["pyfluent_core"].resolve()):
        raise FluentError("PYFLUENT_RUNTIME_MISMATCH", "Client runtime resolved to bundled PyFluent; use a fresh worker.")
    return pyfluent


def validate_runtime(runtime: str) -> None:
    # Validation does not import a product client or start a process.
    if runtime == "bundled":
        if not known_paths()["pyfluent_core"].is_dir():
            raise FluentError("PYFLUENT_IMPORT_ERROR", "Bundled PyFluent directory is missing.")
    else:
        try:
            importlib.metadata.version("ansys-fluent-core")
        except importlib.metadata.PackageNotFoundError as exc:
            raise FluentError("PYFLUENT_IMPORT_ERROR", "Install ansys-fluent-core in the server environment for runtime=client.") from exc


def prepare_recipe(model: PyFluentRecipe, workdir: Path) -> dict[str, Any]:
    payload = model.model_dump(exclude_none=True)
    if "launch" not in model.model_fields_set and model.session.action != "new":
        payload.pop("launch", None)
    payload["workdir"] = str(workdir)
    if model.launch.cwd:
        payload["launch"]["cwd"] = str(require_allowed_path(model.launch.cwd, base=workdir, expect_file=False))
    if isinstance(payload.get("read"), str):
        payload["read"] = {"case": payload["read"]}
    for spec, reading in [(payload.get("read"), True), (payload.get("write"), False),
                          *[(op, op["op"] == "read") for op in payload["operations"] if op["op"] in {"read", "write"}]]:
        for key in ("case", "case_file", "case_data"):
            if spec and spec.get(key):
                spec[key] = str(require_allowed_path(spec[key], base=workdir, must_exist=reading, expect_file=True))
    def normalize_paths(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"file_name", "filename", "file_path", "script_path", "path_name"} and isinstance(child, str):
                    value[key] = str(require_allowed_path(child, base=workdir))
                elif key not in {"path", "select"}:
                    normalize_paths(child)
        elif isinstance(value, list):
            for child in value:
                normalize_paths(child)
    normalize_paths(payload["operations"])
    return payload


def _record_path(session_id: str, workdir: Path | None = None) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", session_id):
        raise FluentError("PYFLUENT_SESSION_MISSING", "Invalid managed session identity.")
    roots = allowed_roots()
    if workdir is not None:
        workdir = require_allowed_path(workdir, expect_file=False)
        root = max((root for root in roots if workdir.is_relative_to(root)), key=lambda root: len(root.parts))
        return require_allowed_path(root / ".ansys_mcp" / "fluent-registry" / (session_id + ".json"))
    for root in roots:
        path = require_allowed_path(root / ".ansys_mcp" / "fluent-registry" / (session_id + ".json"))
        if path.is_file():
            return path
    raise FluentError("PYFLUENT_SESSION_MISSING", "Managed session record is absent; no product was launched.")


def session_summary() -> list[dict[str, Any]]:
    records = []
    for root in allowed_roots():
        directory = require_allowed_path(root / ".ansys_mcp" / "fluent-registry")
        if directory.is_dir():
            for path in directory.glob("*.json"):
                try:
                    record = _read_json(require_allowed_path(path, must_exist=True))
                    records.append({"id": record["id"], "state": record["state"],
                                    "session": record.get("session"), "request_id": record.get("request_id"),
                                    "worker_alive": process_identity_matches(record.get("worker")),
                                    "cached": True})
                except (OSError, ValueError, KeyError):
                    continue
    return records


def _failure(code: str, message: str, **outputs: Any) -> dict[str, Any]:
    return {"ok": False, "status": "failed", "outputs": outputs, "artifacts": [],
            "diagnostics": [{"code": code, "severity": "error", "message": message}]}


def _observe(proxy: ElevatedSession, record: dict[str, Any], request_id: str) -> dict[str, Any]:
    observation = proxy.query(request_id)
    result = observation.get("result")
    if observation.get("status") in {"complete", "cancelled"} and isinstance(result, dict):
        result.setdefault("outputs", {}).update(session_id=record["id"], request_id=request_id)
        return result
    if observation.get("status") == "failed":
        return _failure("PYFLUENT_WORKER_ERROR", observation.get("error", "Worker failed."), request_id=request_id)
    return {"ok": False, "status": "unknown", "outputs": {"session": record.get("session"),
            "session_id": record["id"], "request_id": request_id, "observation": observation}, "artifacts": [],
            "diagnostics": [{"code": "PYFLUENT_OUTCOME_UNKNOWN", "severity": "warning",
                             "message": "Observe this original request; no operation was replayed."}]}


def run_persistent(model: PyFluentRecipe, *, workdir: Path, run_dir: Path,
                   timeout_seconds: float) -> dict[str, Any]:
    started = time.monotonic()
    record: dict[str, Any] = {}
    path = None
    try:
        payload = prepare_recipe(model, workdir)
        if model.session.action == "new":
            session_id = uuid.uuid4().hex
            path = _record_path(session_id, workdir)
            path.parent.mkdir(parents=True, exist_ok=True)
            state_dir = require_allowed_path(path.parent / session_id)
            record = {"id": session_id, "state": "starting", "state_dir": str(state_dir),
                      "runtime": model.runtime, "worker": None}
            _write_json(path, record)
            proxy = ElevatedSession.launch("ansys_mcp.fluent_session", state_dir=state_dir,
                                           timeout_seconds=min(timeout_seconds, model.launch.start_timeout), elevated=False)
            record["worker"] = proxy.summary()["worker"]
            payload["session"]["id"] = session_id
        else:
            path = _record_path(model.session.id or "")
            record = _read_json(path)
            if model.runtime != record["runtime"]:
                raise FluentError("PYFLUENT_RUNTIME_MISMATCH", "Reuse must select the original runtime.")
            proxy = ElevatedSession.attach(require_allowed_path(record["state_dir"], must_exist=True, expect_file=False))
            if proxy.summary()["worker"] != record["worker"]:
                raise FluentError("PYFLUENT_WORKER_IDENTITY_CHANGED", "Worker creation identity changed; nothing was relaunched.")
            if model.session.request_id:
                return _observe(proxy, record, model.session.request_id)
            if record.get("request_id") and record["state"] in {"pending", "unknown"}:
                previous = _observe(proxy, record, record["request_id"])
                if previous["status"] == "unknown":
                    return previous
                record.update(state=previous["status"], session=previous.get("outputs", {}).get("session"))
                _write_json(path, record)
        remaining = timeout_seconds - (time.monotonic() - started)
        if remaining <= 0:
            raise FluentError("PROCESS_TIMEOUT", "Worker startup consumed the step deadline; no recipe was submitted.")
        request_id = uuid.uuid4().hex
        record.update(request_id=request_id, state="pending")
        _write_json(path, record)  # Persist before submitting the mutation, including its recovery ID.
        result = proxy.request("recipe", {"recipe": payload, "run_dir": str(run_dir)},
                               timeout_seconds=remaining, request_id=request_id)
        record.update(state=result["status"], session=result.get("outputs", {}).get("session"))
        try:
            _write_json(path, record)
        except OSError as exc:
            result["diagnostics"].append({"code": "PYFLUENT_REGISTRY_WRITE_ERROR", "severity": "warning", "message": str(exc)})
        result["outputs"].update(request_id=request_id, session_id=record["id"])
        if model.session.action == "close" and result["ok"]:
            try:
                proxy.close()
            except Exception as exc:
                result["diagnostics"].append({"code": "PYFLUENT_WORKER_CLOSE_ERROR", "severity": "warning", "message": str(exc)})
        return result
    except ElevatedRequestTimeout as exc:
        record.update(state="unknown", request_id=exc.request_id)
        if path:
            _write_json(path, record)
        return {**_failure("PYFLUENT_OUTCOME_UNKNOWN", str(exc), session_id=record.get("id"), request_id=exc.request_id), "status": "unknown"}
    except Exception as exc:
        from .process_supervisor import WorkflowCancelled
        if isinstance(exc, WorkflowCancelled):
            request_id = getattr(exc, "request_id", None) or record.get("request_id")
            if path:
                record.update(state="unknown", request_id=request_id, cancel_requested=True)
                _write_json(path, record)
            return {**_failure("PYFLUENT_CANCELLATION_PENDING", str(exc), session_id=record.get("id"),
                              request_id=request_id, cancel_requested=True), "status": "unknown"}
        # A failed launch must not be silently retried or granted a new identity.
        return _failure(getattr(exc, "code", "PYFLUENT_WORKER_ERROR"), str(exc),
                        session_id=record.get("id"), request_id=record.get("request_id"), state_dir=record.get("state_dir"))


class FluentActor:
    def __init__(self, session_id: str, runtime: str, directory: Path):
        self.id, self.runtime, self.directory = session_id, runtime, directory
        self.solver = None
        self.generation = 0
        self.state = "starting"
        self.owned: list[dict[str, Any]] = []
        self.client_version = None
        self.product_version = None
        self._lock = threading.RLock()
        self._counts: dict[str, int] = {}
        self._callbacks: list[tuple[Any, Any]] = []
        self.warnings: list[dict[str, Any]] = []

    def summary(self) -> dict[str, Any]:
        return {"id": self.id, "state": self.state, "generation": self.generation,
                "runtime": self.runtime, "python": sys.executable, "python_version": sys.version.split()[0],
                "client_version": self.client_version, "product_version": self.product_version,
                "owned_processes": self.owned, "directory": str(self.directory),
                "live_owned_processes": [identity for identity in self.owned if process_identity_matches(identity)]}

    def _append(self, filename: str, data: dict[str, Any]) -> None:
        with self._lock:
            path = require_allowed_path(self.directory / filename)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"observed_at": time.time(), "generation": self.generation, **data}, default=_json_value) + "\n")

    def _monitor(self) -> None:
        try:
            for name in self.solver.monitors.get_monitor_set_names():
                xs, columns = self.solver.monitors.get_monitor_set_data(monitor_set_name=name)
                count = self._counts.get(name, 0)
                if len(xs) < count:
                    count = 0
                for index in range(count, len(xs)):
                    self._append("monitors.jsonl", {"monitor": name, "index": xs[index],
                                 "values": {key: values[index] for key, values in columns.items()}})
                self._counts[name] = len(xs)
        except Exception as exc:
            self._append("monitor-errors.jsonl", {"error": str(exc)})

    def _event(self, session: Any = None, event_info: Any = None, **kwargs: Any) -> None:
        info = {key: getattr(event_info, key) for key in ("index", "time", "time_step") if hasattr(event_info, key)}
        self._append("events.jsonl", {"event": type(event_info).__name__, **info})

    def _attach_streams(self, pyfluent: Any, monitoring: bool) -> None:
        if not monitoring:
            return
        self._counts.clear()
        try:
            manager = self.solver.monitors
            self._callbacks.append((manager, manager.register_callback(self._monitor)))
        except Exception as exc:
            self.warnings.append({"code": "PYFLUENT_MONITORS_UNAVAILABLE", "severity": "warning", "message": str(exc)})
        enum = getattr(pyfluent, "SolverEvent", None)
        for name in ("ITERATION_ENDED", "TIMESTEP_ENDED"):
            if enum is not None and hasattr(enum, name):
                try:
                    manager = self.solver.events
                    self._callbacks.append((manager, manager.register_callback(getattr(enum, name), self._event)))
                except Exception as exc:
                    self.warnings.append({"code": "PYFLUENT_EVENTS_UNAVAILABLE", "severity": "warning", "message": str(exc)})

    def _detach_streams(self) -> None:
        for manager, callback in self._callbacks:
            with contextlib.suppress(Exception):
                manager.unregister_callback(callback)
        self._callbacks.clear()

    def start(self, payload: dict[str, Any]) -> None:
        import psutil
        pyfluent = load_pyfluent(self.runtime)
        self.client_version = pyfluent.__version__
        self.directory.mkdir(parents=True, exist_ok=True)
        before = {p.pid for p in psutil.Process().children(recursive=True)}
        launch = _launch_kwargs(payload)
        child_env = merge_local_no_proxy(os.environ)
        if self.runtime == "bundled":
            child_env["PYTHONPATH"] = str(known_paths()["pyfluent_core"]) + os.pathsep + child_env.get("PYTHONPATH", "")
        launch.update(start_transcript=False, cleanup_on_exit=True, start_watchdog=True,
                      env=child_env, fluent_path=str(known_paths()["fluent"]))
        try:
            self.solver = pyfluent.launch_fluent(**launch)
        finally:
            for process in psutil.Process().children(recursive=True):
                if process.pid not in before:
                    identity = process_identity(process.pid)
                    if identity:
                        self.owned.append(identity)
                        token = current_cancellation_token()
                        if token:
                            token.register_process(identity)
        self.product_version = str(self.solver.get_fluent_version())
        if payload["launch"].get("start_transcript", True):
            self.solver.transcript.start(file_name=str(self.directory / "transcript.log"), write_to_stdout=False)
        self._attach_streams(pyfluent, payload.get("monitoring", True))
        self.state = "ready"

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        values: dict[str, Any] = {}
        operations = []
        token = current_cancellation_token()
        action = payload["session"]["action"]
        try:
            if token:
                for identity in self.owned:
                    token.register_process(identity)
                token.raise_if_cancelled()
            expected = payload["session"].get("expected_generation")
            if expected is not None and expected != self.generation:
                raise FluentError("PYFLUENT_GENERATION_MISMATCH", "State generation changed; inspect the session before modifying it.")
            if action == "summary":
                if self.state not in {"closed", "starting", "failed"} and not self.solver.is_server_healthy():
                    self.state = "disconnected"
                return {"ok": True, "status": self.state, "outputs": {"session": self.summary()}, "diagnostics": [], "artifacts": []}
            if self.state in {"closed", "failed", "disconnected", "close_failed"} and action != "close":
                raise FluentError("PYFLUENT_SESSION_UNAVAILABLE", "Session is not usable; no solver was relaunched.")
            if action == "close":
                self._detach_streams()
                if self.solver is not None:
                    self.solver.exit()
                # A failed launcher may leave children without a usable gRPC client.
                from .process_supervisor import terminate_owned_process
                for identity in self.owned:
                    if process_identity_matches(identity):
                        terminate_owned_process(identity)
                if any(process_identity_matches(identity) for identity in self.owned):
                    raise FluentError("PYFLUENT_CLOSE_INCOMPLETE", "Owned product processes remain; shutdown is not confirmed.")
                self.state = "closed"
            else:
                self.state = "running"
                if payload.get("read"):
                    self.generation += 1
                    self._counts.clear()
                    _read_case(self.solver, payload)
                for index, operation in enumerate(payload.get("operations", [])):
                    if token:
                        token.raise_if_cancelled()
                    try:
                        if operation["op"] not in {"get_state", "inspect_path", "compute_reports", "write"}:
                            self.generation += 1
                        if operation["op"] == "read":
                            self._counts.clear()
                        if operation["op"] == "call" and operation["path"] == "switch_to_solver":
                            self._detach_streams()
                            self.solver = self.solver.switch_to_solver()
                            self._attach_streams(load_pyfluent(self.runtime), payload.get("monitoring", True))
                            value = {"mode": "solver", "generation": self.generation}
                        else:
                            value = _run_operation(self.solver, operation)
                        value = json.loads(json.dumps(value, default=_json_value))
                        operations.append({"index": index, "ok": True, "value": value})
                        if operation.get("store_as"):
                            values[operation["store_as"]] = value
                    except Exception as exc:
                        operations.append({"index": index, "ok": False, "error": str(exc)})
                        if not operation.get("continue_on_error"):
                            raise
                _write_outputs(self.solver, payload)
                if payload.get("monitoring", True):
                    self._monitor()
                self.state = "ready"
            errors = [item for item in operations if not item["ok"]]
            return {"ok": not errors, "status": "closed" if action == "close" else "complete",
                    "outputs": {"session": self.summary(), "results": values, "operations": operations,
                                "transcript": str(self.directory / "transcript.log"),
                                "monitors": str(self.directory / "monitors.jsonl"), "events": str(self.directory / "events.jsonl")},
                    "diagnostics": [*self.warnings, *([{"code": "PYFLUENT_OPERATION_ERROR", "severity": "error", "message": "An operation failed."}] if errors else [])], "artifacts": []}
        except Exception as exc:
            if action == "close":
                self.state = "close_failed"
            elif token and token.cancelled:
                self.state = "disconnected"
            elif self.state == "running":
                self.state = "operation_failed"
            return _failure(getattr(exc, "code", "PYFLUENT_OPERATION_ERROR"), str(exc),
                            session=self.summary(), operations=operations, results=values)
        finally:
            if token and not token.cancelled:
                for identity in self.owned:
                    token.unregister_process(identity)


def _json_value(value: Any) -> Any:
    if hasattr(value, "tolist"):
        return value.tolist()
    from google.protobuf.message import Message
    if isinstance(value, Message):
        from google.protobuf.json_format import MessageToDict
        return MessageToDict(value, preserving_proto_field_name=True)
    raise TypeError(f"Non-JSON Fluent value: {type(value).__name__}; inspect its path instead.")


_ACTOR: FluentActor | None = None


def worker_dispatch(operation: str, payload: dict[str, Any]) -> dict[str, Any]:
    global _ACTOR
    if operation != "recipe":
        raise ValueError("Unknown Fluent worker operation.")
    raw = payload["recipe"]
    workdir = require_allowed_path(raw["workdir"], must_exist=True, expect_file=False)
    directory = require_allowed_path(payload["run_dir"], must_exist=True, expect_file=False)
    # Validate untrusted IPC again; the internal new ID was generated by the controller.
    public = {key: value for key, value in raw.items() if key != "workdir"}
    if public["session"]["action"] == "new":
        session_id = public["session"].pop("id")
    else:
        session_id = public["session"]["id"]
    if not re.fullmatch(r"[a-f0-9]{32}", session_id):
        raise ValueError("Invalid managed Fluent identity.")
    model = PyFluentRecipe.model_validate(public)
    prepared = prepare_recipe(model, workdir)
    prepared["session"]["id"] = session_id
    if model.session.action == "new":
        if _ACTOR is not None:
            return _failure("PYFLUENT_SESSION_EXISTS", "This worker already owns a session; nothing was relaunched.")
        _ACTOR = FluentActor(session_id, model.runtime, directory / ("fluent-" + session_id))
        try:
            _ACTOR.start(prepared)
        except Exception as exc:
            _ACTOR.state = "failed"
            return _failure(getattr(exc, "code", "PYFLUENT_LAUNCH_ERROR"), str(exc), session=_ACTOR.summary())
    if _ACTOR is None or _ACTOR.id != session_id:
        return _failure("PYFLUENT_SESSION_MISSING", "Worker does not own this session.")
    return _ACTOR.execute(prepared)
