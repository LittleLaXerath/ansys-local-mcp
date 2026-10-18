"""Mechanical sessions, Workbench-first execution, and typed object transport.

Sessions are owned by this MCP process (or its authenticated elevated worker).
Neither backend attaches to an arbitrary running product, retries an unknown
mutation, or silently switches between Workbench and standalone Mechanical.
"""
from __future__ import annotations

import concurrent.futures
import contextvars
import ctypes
import importlib
import json
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Any

from .config import allowed_roots, discover_installation, known_paths, require_allowed_path
from .mechanical_schema import MechanicalOperation, MechanicalProjectSpec, MechanicalRecipe
from .workflow_paths import PathChecks
from .process_supervisor import current_cancellation_token, process_identity, process_identity_matches, terminate_owned_process, WorkflowCancelled


class MechanicalError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _exception_code(error: BaseException, default: str) -> str:
    value = getattr(error, "code", None)
    return value if isinstance(value, str) and value else default


def _literal(value: str) -> str:
    return "u" + json.dumps(str(value), ensure_ascii=True)


def _check_launch_privileges(executable: Path) -> None:
    """Detect an explicit Windows requireAdministrator manifest before execution."""
    if os.name != "nt" or ctypes.windll.shell32.IsUserAnAdmin():
        return
    required = False
    try:
        import winreg
        key_name = r"Software\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers"
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                try:
                    with winreg.OpenKey(hive, key_name, 0, winreg.KEY_READ | view) as key:
                        value, _ = winreg.QueryValueEx(key, str(executable))
                        required = required or "RUNASADMIN" in str(value).upper()
                except OSError:
                    pass
    except ImportError:
        pass
    try:
        import win32api
        handle = win32api.LoadLibraryEx(str(executable), 0, 2)
        try:
            manifest = win32api.LoadResource(handle, 24, 1)
        finally:
            win32api.FreeLibrary(handle)
        required = required or b'requireAdministrator' in manifest
    except Exception:
        pass  # The actual launcher remains authoritative when no manifest exists.
    if required:
        raise MechanicalError("MECHANICAL_ELEVATION_REQUIRED", "The selected product requires an administrator token; no product request has executed.")


def _dependencies(backend: str) -> tuple[Any, Any | None]:
    try:
        mechanical = importlib.import_module("ansys.mechanical.core")
        workbench = importlib.import_module("ansys.workbench.core") if backend == "workbench" else None
    except ImportError as exc:
        raise MechanicalError("MECHANICAL_DEPENDENCY_MISSING", "The selected backend requires installed PyMechanical" +
                              (" and PyWorkbench." if backend == "workbench" else ".")) from exc
    return mechanical, workbench


def _typed_paths(value: Any):
    if isinstance(value, dict):
        if value.get("$type") == "path":
            yield value["path"], value["role"], value.get("kind", "file") == "file"
        for item in value.values():
            yield from _typed_paths(item)
    elif isinstance(value, list):
        for item in value:
            yield from _typed_paths(item)


_PATH_MEMBERS = {"filepath", "filename", "directory", "workingdirectory", "projectdirectory", "outputfile", "resultfile", "path"}
_FILE_METHODS = {"Open", "SaveAs", "Archive", "Unarchive", "Import", "ImportGeometry", "ImportMaterials", "Export", "ExportToTextFile", "ExportImage", "ExportAnimation", "SetFile", "ReadFile", "WriteFile", "ReadXml", "WriteXml", "ReadXmlSchema", "WriteXmlSchema", "ReadMaterial", "ReadMaterials", "WriteMaterials"}


def _path_member(name: str) -> bool:
    normalized = name.replace("_", "").lower()
    return normalized in _PATH_MEMBERS or normalized.endswith(("filepath", "filename", "directory", "folder", "path"))


def _validate_argument_paths(operation: MechanicalOperation, checks: PathChecks) -> None:
    def visit(value: Any, *, require_typed: bool = False) -> None:
        if isinstance(value, dict):
            if value.get("$type") == "path":
                checks.resolve(value["path"], input_file=value["role"] == "input", expect_file=value.get("kind", "file") == "file")
                return
            if require_typed and value.get("$type") in {"ref", "local_ref"}:
                raise ValueError("A file API argument must use an explicit typed path, not an opaque reference.")
            for key, item in value.items():
                visit(item, require_typed=_path_member(str(key)))
        elif isinstance(value, list):
            for item in value:
                visit(item, require_typed=require_typed)
        elif isinstance(value, str):
            if Path(value).is_absolute() or PureWindowsPath(value).is_absolute():
                raise ValueError("Absolute API argument paths require {$type: path, path: ..., role: input|output}.")
            if ("/" in value and any(part in {".", ".."} for part in value.split("/"))) or "\\" in value:
                raise ValueError("Path-like API strings require explicit typed paths.")
            if require_typed and value:
                raise ValueError("File API arguments require explicit typed paths, including relative paths.")
    effective_op = operation.action if operation.op == "engineering_data" else operation.op
    file_call = effective_op == "call" and (operation.member in _FILE_METHODS or (operation.member or "").startswith(("Open", "Save", "Import", "Export", "Archive", "Unarchive", "ReadFile", "WriteFile")))
    file_positions = {"ImportMaterial": {1}, "ReadMaterial": {1}, "WriteMaterials": {1}, "Unarchive": {0, 1}}.get(operation.member, {0})
    for index, item in enumerate(operation.args):
        visit(item, require_typed=file_call and index in file_positions)
    for key, item in operation.kwargs.items():
        visit(item, require_typed=_path_member(key) or (file_call and key.casefold() in {"source", "destination", "target", "file", "folder"}))
    visit(operation.value, require_typed=effective_op == "set" and _path_member(operation.member or ""))


def mechanical_produced_paths(recipe: MechanicalRecipe | dict[str, Any]) -> list[str]:
    model = recipe if isinstance(recipe, MechanicalRecipe) else MechanicalRecipe.model_validate(recipe)
    result = [model.project.save_path] if model.project.save_path else []
    for operation in model.operations:
        if operation.op == "project_save" and operation.path:
            result.append(operation.path)
        result.extend(path for path, role, _ in _typed_paths(operation.model_dump()) if role == "output")
    return list(dict.fromkeys(result))


def validate_mechanical_recipe_paths(
    recipe: MechanicalRecipe | dict[str, Any], workdir: Path, path_checks: PathChecks | None = None,
) -> list[dict[str, Any]]:
    """Pure path/schema validation. Does not import PyAnsys or launch a product."""
    model = recipe if isinstance(recipe, MechanicalRecipe) else MechanicalRecipe.model_validate(recipe)
    checks = path_checks or PathChecks(workdir)
    for path in (model.project.open_path, model.project.geometry_path):
        if path:
            checks.resolve(path, input_file=True, expect_file=True)
    for operation in model.operations:
        _validate_argument_paths(operation, checks)
        if operation.script_path:
            checks.resolve(operation.script_path, input_file=True, expect_file=True)
        if operation.op == "project_open" and operation.path:
            checks.resolve(operation.path, input_file=True, expect_file=True)
        if operation.op == "project_reopen" and operation.path:
            checks.resolve(operation.path, input_file=True, expect_file=True)
        for path, role, is_file in _typed_paths(operation.model_dump()):
            checks.resolve(path, input_file=role == "input", expect_file=is_file)
            if role == "output":
                checks.declare(path)
        if operation.op == "project_save" and operation.path:
            checks.resolve(operation.path, expect_file=True)
            checks.declare(operation.path)
    if model.project.save_path:
        checks.resolve(model.project.save_path, expect_file=True)
    return []


def _normalize_paths(recipe: MechanicalRecipe, workdir: Path) -> MechanicalRecipe:
    payload = recipe.model_dump()
    for field_name in ("open_path", "save_path", "geometry_path"):
        if payload["project"][field_name]:
            payload["project"][field_name] = str(require_allowed_path(payload["project"][field_name], base=workdir))
    def visit(value: Any) -> Any:
        if isinstance(value, dict):
            result = {key: visit(item) for key, item in value.items()}
            if result.get("$type") == "path":
                result["path"] = str(require_allowed_path(result["path"], base=workdir, expect_file=result.get("kind", "file") == "file"))
            return result
        if isinstance(value, list):
            return [visit(item) for item in value]
        return value
    for operation in payload["operations"]:
        for field_name in ("path", "script_path"):
            if operation[field_name]:
                operation[field_name] = str(require_allowed_path(operation[field_name], base=workdir, expect_file=True))
        for field_name in ("target", "value", "args", "kwargs", "category"):
            operation[field_name] = visit(operation[field_name])
    return MechanicalRecipe.model_validate(payload)


class ProductBackend:
    """Provider boundary; mock tests inject this interface without product imports."""
    name = "product"

    def __init__(self):
        self.client: Any = None
        self.workbench: Any = None
        self.system_name: str | None = None
        self.version: str | None = None
        self.project_path: str | None = None
        self.verified_transport = False
        self.owned_processes: list[dict[str, Any]] = []
        self._cancellation_token = None

    def _track_process(self, pid: int) -> None:
        identity = process_identity(pid)
        if identity is None:
            raise MechanicalError("MECHANICAL_PROCESS_IDENTITY_MISSING", "The launched product process could not be verified.")
        if identity not in self.owned_processes:
            self.owned_processes.append(identity)
            self._cancellation_token = current_cancellation_token()
            if self._cancellation_token:
                self._cancellation_token.register_process(identity)

    def _track_mechanical(self) -> None:
        pid = self.client.run_python_script("import System\nstr(System.Diagnostics.Process.GetCurrentProcess().Id)")
        self._track_process(int(pid))

    def _finish_owned_processes(self) -> None:
        for identity in self.owned_processes:
            if process_identity_matches(identity):
                terminate_owned_process(identity)
            if process_identity_matches(identity):
                raise MechanicalError("MECHANICAL_CLOSE_INCOMPLETE", "An owned product process remains live after shutdown.")
            if self._cancellation_token:
                self._cancellation_token.unregister_process(identity)

    def start(self, recipe: MechanicalRecipe, directory: Path) -> None:
        raise NotImplementedError

    def execute(self, request: dict[str, Any], *, engineering_data: bool = False) -> dict[str, Any]:
        source = Path(__file__).with_name("mechanical_dispatch.py").read_text(encoding="utf-8")
        # Assign before source execution, because its last block consumes this packet.
        code = "_MCP_REQUEST_JSON = " + _literal(json.dumps(request, ensure_ascii=True)) + "\n" + source
        if engineering_data:
            if self.workbench is None:
                raise MechanicalError("MECHANICAL_BACKEND_UNSUPPORTED", "Engineering Data requires Workbench.")
            code = ("EngineeringData = GetSystem(Name=" + _literal(self.system_name or "") +
                    ").GetContainer(ComponentName='Engineering Data')\n" + code +
                    "\nwb_script_result = _MCP_RESPONSE_JSON\n")
            raw = self._wb_run(code)
        else:
            raw = self.client.run_python_script(code + "\n_MCP_RESPONSE_JSON\n")
        result = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(result, dict) or type(result.get("ok")) is not bool or "generation" not in result:
            raise MechanicalError("MECHANICAL_PROTOCOL_ERROR", "The product returned an invalid bridge response; execution outcome is unknown.")
        return result

    def project_open(self, path: str, *, reopen: bool = False, system_name: str | None = None) -> None:
        raise NotImplementedError

    def project_save(self, path: str, *, overwrite: bool) -> None:
        raise NotImplementedError

    def close(self) -> None:
        if self.client is not None:
            try:
                self.client.exit(force=True)
            finally:
                self._finish_owned_processes()
                self.client = None


class WorkbenchBackend(ProductBackend):
    name = "workbench"

    def start(self, recipe: MechanicalRecipe, directory: Path) -> None:
        mechanical, workbench = _dependencies(self.name)
        installation = discover_installation()
        if recipe.launch.product_version and recipe.launch.product_version != installation.version_tag:
            raise MechanicalError("MECHANICAL_VERSION_UNAVAILABLE", "The requested version is not the resolved installation; select its installation root explicitly.")
        _check_launch_privileges(known_paths(installation.root)["runwb2"])
        self._mechanical_module = mechanical
        if os.name == "nt":
            # The SDK's WMI launcher lives on this session's serial COM thread.
            self._pythoncom = importlib.import_module("pythoncom")
            self._pythoncom.CoInitialize()
        self.workbench = workbench.launch_workbench(
            show_gui=recipe.launch.show_gui, version=recipe.launch.product_version or installation.version_tag,
            client_workdir=str(directory), server_workdir=str(directory),
        )
        self.version = str(self.workbench.server_version)
        wb_pid = self._wb_run("import System\nwb_script_result=json.dumps(System.Diagnostics.Process.GetCurrentProcess().Id)")
        self._track_process(int(wb_pid))
        project = recipe.project
        lines = ["import json"]
        if project.open_path:
            lines.append("Open(FilePath=" + _literal(project.open_path) + ")")
            self.project_path = project.open_path
        if project.system_name:
            lines.append("_mcp_system = GetSystem(Name=" + _literal(project.system_name) + ")")
        elif project.open_path:
            lines.extend([
                "_mcp_candidates = []", "for _mcp_candidate in GetAllSystems():", "    try:",
                "        _mcp_candidate.GetContainer(ComponentName='Model')",
                "        _mcp_candidates.append(_mcp_candidate)", "    except Exception:", "        pass",
                "if len(_mcp_candidates) != 1:",
                "    raise Exception('Select a unique Mechanical system_name for this project.')",
                "_mcp_system = _mcp_candidates[0]",
            ])
        else:
            kwargs = "TemplateName=" + _literal(project.template_name)
            if project.solver:
                kwargs += ", Solver=" + _literal(project.solver)
            lines.append("_mcp_system = GetTemplate(" + kwargs + ").CreateSystem()")
        if project.geometry_path:
            lines.append("_mcp_system.GetContainer(ComponentName='Geometry').SetFile(FilePath=" + _literal(project.geometry_path) + ")")
        lines.append("wb_script_result = json.dumps(_mcp_system.Name)")
        self.system_name = self._wb_run("\n".join(lines))
        if not isinstance(self.system_name, str) or not self.system_name:
            raise MechanicalError("MECHANICAL_WORKBENCH_PROTOCOL_ERROR", "Workbench did not return a Mechanical system name.")
        self._connect_model()
        self.verified_transport = True

    def _connect_model(self) -> None:
        # Use the documented system service rather than launching a second model.
        port = self._wb_run("wb_script_result=json.dumps(LaunchMechanicalServerOnSystem(SystemName=" + _literal(self.system_name or "") + "))")
        if type(port) is not int or not 0 < port < 65536:
            raise MechanicalError("MECHANICAL_WORKBENCH_PROTOCOL_ERROR", "Workbench did not return a valid Mechanical server port.")
        self.client = self._mechanical_module.connect_to_mechanical(
            ip="127.0.0.1", port=port, cleanup_on_exit=False, clear_on_connect=False,
        )
        self._track_mechanical()

    def _wb_run(self, source: str) -> Any:
        # PyWorkbench logs script exceptions and returns None. A product-side
        # envelope is mandatory to distinguish a void result from a failed call.
        lines = ["import json", "wb_script_result='null'", "try:"]
        lines.extend("    " + line for line in source.splitlines())
        lines.extend([
            "    _mcp_value=json.loads(wb_script_result)",
            "    wb_script_result=json.dumps({'ok':True,'value':_mcp_value})",
            "except Exception as _mcp_error:",
            "    wb_script_result=json.dumps({'ok':False,'error':str(_mcp_error)})",
        ])
        response = self.workbench.run_script_string("\n".join(lines), log_level="error")
        if not isinstance(response, dict) or type(response.get("ok")) is not bool:
            raise MechanicalError("MECHANICAL_PROTOCOL_ERROR", "Workbench script response was lost; the execution outcome is unknown.")
        if not response["ok"]:
            raise MechanicalError("MECHANICAL_WORKBENCH_API_ERROR", response.get("error", "Workbench operation failed."))
        return response.get("value")

    def project_open(self, path: str, *, reopen: bool = False, system_name: str | None = None) -> None:
        self._wb_run("StopMechanicalServerOnSystem(SystemName=" + _literal(self.system_name or "") + ")")
        self.client = None
        lines = ["import json", "Open(FilePath=" + _literal(path) + ")"]
        requested = system_name or (self.system_name if reopen else None)
        if requested:
            lines.append("_mcp_system = GetSystem(Name=" + _literal(requested) + ")")
        else:
            lines.extend([
                "_mcp_candidates = []", "for _mcp_candidate in GetAllSystems():", "    try:",
                "        _mcp_candidate.GetContainer(ComponentName='Model')",
                "        _mcp_candidates.append(_mcp_candidate)", "    except Exception:", "        pass",
                "if len(_mcp_candidates) != 1:",
                "    raise Exception('Select a unique Mechanical system_name for this project.')",
                "_mcp_system = _mcp_candidates[0]",
            ])
        lines.append("wb_script_result = json.dumps(_mcp_system.Name)")
        code = "\n".join(lines)
        self.system_name = self._wb_run(code)
        self._connect_model()
        self.project_path = path

    def project_save(self, path: str, *, overwrite: bool) -> None:
        self._wb_run("Save(FilePath=" + _literal(path) + ", Overwrite=" + repr(overwrite) + ")")
        self.project_path = path

    def close(self) -> None:
        if self.workbench is not None:
            error = None
            try:
                if self.client is not None:
                    self._wb_run("StopMechanicalServerOnSystem(SystemName=" + _literal(self.system_name or "") + ")")
                    self.client = None
                self.workbench.exit()
            except Exception as exc:
                error = exc
            finally:
                self._finish_owned_processes()
                self.workbench = None
                if hasattr(self, "_pythoncom"):
                    self._pythoncom.CoUninitialize()
            if error and not self.owned_processes:
                raise error


class PyMechanicalBackend(ProductBackend):
    name = "pymechanical"

    def start(self, recipe: MechanicalRecipe, directory: Path) -> None:
        mechanical, _ = _dependencies(self.name)
        installation = discover_installation()
        if recipe.launch.product_version and recipe.launch.product_version != installation.version_tag:
            raise MechanicalError("MECHANICAL_VERSION_UNAVAILABLE", "The requested version is not the resolved installation; select its installation root explicitly.")
        executable = known_paths(installation.root)["mechanical"]
        _check_launch_privileges(executable)
        self.client = mechanical.launch_mechanical(
            exec_file=str(executable), batch=not recipe.launch.show_gui, allow_input=False,
            cleanup_on_exit=False, clear_on_connect=False, start_instance=True,
        )
        self.version = str(self.client.version)
        self._track_mechanical()
        self.verified_transport = True
        if recipe.project.open_path:
            self.project_open(recipe.project.open_path)
        if recipe.project.geometry_path:
            self.client.run_python_script(
                "import Ansys\n_mcp_import = ExtAPI.DataModel.Project.Model.GeometryImportGroup.AddGeometryImport()\n" +
                "_mcp_import.Import(" + _literal(recipe.project.geometry_path) +
                ", Ansys.Mechanical.DataModel.Enums.GeometryImportPreference.Format.Automatic, " +
                "Ansys.ACT.Mechanical.Utilities.GeometryImportPreferences())"
            )

    def project_open(self, path: str, *, reopen: bool = False, system_name: str | None = None) -> None:
        if system_name:
            raise MechanicalError("MECHANICAL_BACKEND_UNSUPPORTED", "Standalone Mechanical does not select Workbench systems.")
        self.client.run_python_script("ExtAPI.DataModel.Project.Open(" + _literal(path) + ")")
        self.project_path = path

    def project_save(self, path: str, *, overwrite: bool) -> None:
        self.client.run_python_script("ExtAPI.DataModel.Project.SaveAs(" + _literal(path) + ", " + repr(overwrite) + ")")
        self.project_path = path


@dataclass
class _Session:
    id: str
    backend_name: str
    workdir: Path
    directory: Path
    backend: ProductBackend | None = None
    generation: int = 0
    state: str = "starting"
    created_at: float = field(default_factory=time.time)
    executor: concurrent.futures.ThreadPoolExecutor = field(default_factory=lambda: concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="mechanical-session"))
    lock: threading.RLock = field(default_factory=threading.RLock)
    active_request: str | None = None
    futures: dict[str, concurrent.futures.Future] = field(default_factory=dict)
    tokens: dict[str, Any] = field(default_factory=dict)
    closed: bool = False

    def summary(self) -> dict[str, Any]:
        with self.lock:
            return {"id": self.id, "backend": self.backend_name, "generation": self.generation,
                    "state": self.state, "owned": True, "created_at": self.created_at,
                    "version": self.backend.version if self.backend else None,
                    "system_name": self.backend.system_name if self.backend else None,
                    "active_request": self.active_request, "closed": self.closed,
                    "last_request_id": next(reversed(self.futures), None),
                    "owned_processes": list(self.backend.owned_processes) if self.backend else []}


_SESSIONS: dict[str, _Session] = {}
_REGISTRY_LOCK = threading.RLock()
_ELEVATED: dict[str, Any] = {}
_ELEVATED_WORKERS: dict[str, Any] = {}
_WORKER_CONTEXT: contextvars.ContextVar[bool] = contextvars.ContextVar("mechanical_worker_context", default=False)
_RECORD_ID = re.compile(r"^[a-f0-9]{32}$")


def _record_path(kind: str, record_id: str, workdir: Path) -> Path:
    if kind not in {"sessions", "workers"} or not _RECORD_ID.fullmatch(record_id):
        raise MechanicalError("MECHANICAL_REGISTRY_ID_INVALID", "Invalid Mechanical registry id.")
    # Windows temp directories can arrive through an 8.3 alias while allowed
    # roots are canonical long paths. Select the same registry root for both.
    workdir = require_allowed_path(workdir, expect_file=False)
    candidates = [root for root in allowed_roots() if workdir == root or workdir.is_relative_to(root)]
    registry_root = max(candidates, key=lambda root: len(root.parts)) if candidates else workdir
    return require_allowed_path(registry_root / ".ansys_mcp" / "mechanical-registry" / kind / (record_id + ".json"))


def _write_record(kind: str, record_id: str, value: dict[str, Any], workdir: Path) -> None:
    path = _record_path(kind, record_id, workdir)
    path.parent.mkdir(parents=True, exist_ok=True)
    from .jobs import _write_json
    _write_json(path, value)


def _save_observation(kind: str, record_id: str, value: dict[str, Any], workdir: Path, result: dict[str, Any]) -> None:
    """A metadata failure cannot turn a completed product request into a retry."""
    try:
        _write_record(kind, record_id, value, workdir)
    except (OSError, ValueError, TypeError):
        result.setdefault("diagnostics", []).append({"code": "MECHANICAL_RECOVERY_METADATA_UNAVAILABLE", "severity": "warning",
                                                     "message": "The product observation is preserved, but local recovery metadata could not be updated."})
        result.setdefault("outputs", {})["session_persistence"] = "process_memory_only"


def _read_record(kind: str, record_id: str) -> dict[str, Any] | None:
    if not _RECORD_ID.fullmatch(record_id):
        return None
    for root in allowed_roots():
        path = _record_path(kind, record_id, root)
        if path.is_file():
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict) and value.get("id") == record_id:
                return value
    return None


def _restore_worker(worker_id: str) -> Any:
    proxy = _ELEVATED_WORKERS.get(worker_id)
    if proxy is not None:
        return proxy
    record = _read_record("workers", worker_id)
    if not record or record.get("closed"):
        raise MechanicalError("MECHANICAL_WORKER_MISSING", "The recorded execution worker is unavailable; no product was relaunched.")
    from .elevation import ElevatedSession
    directory = require_allowed_path(record["state_dir"], must_exist=True, expect_file=False)
    proxy = ElevatedSession.attach(directory)
    observation = proxy.summary()  # Authenticate the same worker, without a product operation.
    if record.get("worker") is not None and observation.get("worker") != record.get("worker"):
        raise MechanicalError("MECHANICAL_WORKER_IDENTITY_CHANGED", "Recorded worker identity differs; no product was relaunched.")
    if record.get("worker") is None:
        record.update(worker=observation.get("worker"), state="ready")
        _write_record("workers", worker_id, record, Path(record["workdir"]))
    _ELEVATED_WORKERS[worker_id] = proxy
    return proxy


def _restore_session(session_id: str) -> Any:
    proxy = _ELEVATED.get(session_id)
    if proxy is not None:
        return proxy
    record = _read_record("sessions", session_id)
    if not record or record.get("session", {}).get("closed"):
        raise MechanicalError("MECHANICAL_SESSION_MISSING", "The recorded Mechanical session is unavailable; no product was relaunched.")
    proxy = _restore_worker(record["worker_id"])
    _ELEVATED[session_id] = proxy
    return proxy


def session_summary() -> list[dict[str, Any]]:
    """Read cached metadata only; never starts or probes a product."""
    with _REGISTRY_LOCK:
        result = {session.id: session.summary() for session in _SESSIONS.values()}
    if not _WORKER_CONTEXT.get():
        for root in allowed_roots():
            directory = root / ".ansys_mcp" / "mechanical-registry" / "sessions"
            if not directory.is_dir():
                continue
            for path in directory.glob("*.json"):
                if not _RECORD_ID.fullmatch(path.stem):
                    continue
                try:
                    record = json.loads(path.read_text(encoding="utf-8"))
                    cached = record["session"]
                    result.setdefault(record["id"], {**cached, "worker_managed": True,
                                                      "connection_verified": False, "elevated": record.get("elevated", False)})
                except (OSError, ValueError, KeyError, TypeError):
                    continue
    return list(result.values())


def mechanical_request_status(session_id: str, request_id: str) -> dict[str, Any]:
    if not _WORKER_CONTEXT.get() and (session_id in _ELEVATED or _read_record("sessions", session_id)):
        try:
            return _restore_session(session_id).request("request_status", {"session_id": session_id, "request_id": request_id}, timeout_seconds=10)
        except Exception as exc:
            return _failure(getattr(exc, "code", "MECHANICAL_WORKER_UNAVAILABLE"), str(exc))
    with _REGISTRY_LOCK:
        session = _SESSIONS.get(session_id)
    if session is None:
        return _failure("MECHANICAL_SESSION_MISSING", "Mechanical session is not owned by this server.")
    future = session.futures.get(request_id)
    if future is None:
        return _failure("MECHANICAL_REQUEST_MISSING", "Mechanical request id is unknown.")
    if not future.done():
        return {"ok": True, "status": "running", "outputs": {"session": session.summary(), "request_id": request_id}, "diagnostics": [], "artifacts": []}
    try:
        return future.result()
    except Exception as exc:
        return _failure(getattr(exc, "code", "MECHANICAL_EXECUTION_ERROR"), str(exc))


def _failure(code: str, message: str, *, status: str = "failed", outputs: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(code, str):
        code = "MECHANICAL_EXECUTION_UNKNOWN"
    return {"ok": False, "status": status, "outputs": outputs or {},
            "diagnostics": [{"code": code, "message": message, "severity": "error"}], "artifacts": []}


def _record_evidence(session: _Session, operation: MechanicalOperation, result: dict[str, Any]) -> None:
    if not session.backend.verified_transport:
        return
    try:
        from .capabilities import record_runtime_evidence
    except ImportError:
        return
    # Success only records API reachability/object handling. It never asserts a
    # solve or engineering verification merely because a method returned.
    name = result.get("qualified_api_name")
    if not name:
        return
    try:
        record_runtime_evidence({"product": "mechanical", "version": session.backend.version,
                                 "qualified_name": name, "runtime_reachable": True,
                                 "validation_stage": "object", "evidence_source": "runtime_probe",
                                 "backend": session.backend_name})
    except (OSError, ValueError, TypeError):
        # Catalog bookkeeping cannot convert a completed product mutation into
        # an ambiguous failure that might tempt a caller to replay it.
        pass


def _execute(session: _Session, recipe: MechanicalRecipe, run_dir: Path, request_id: str, created: bool) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    response: dict[str, Any] | None = None
    request_token = current_cancellation_token()
    try:
        if request_token and session.backend:
            for identity in session.backend.owned_processes:
                request_token.register_process(identity)
        if session.backend is None:
            backend = WorkbenchBackend() if recipe.backend == "workbench" else PyMechanicalBackend()
            # Retain partial ownership if startup fails after a product was created.
            session.backend = backend
            backend.start(recipe, session.directory)
        with session.lock:
            session.state = "running"
        for index, operation in enumerate(recipe.operations):
            token = current_cancellation_token()
            if token:
                token.raise_if_cancelled()
            operation_id = operation.id or f"operation_{index + 1}"
            if operation.op in {"project_open", "project_reopen", "project_save"}:
                path = operation.path or session.backend.project_path
                if not path:
                    raise MechanicalError("MECHANICAL_PROJECT_PATH_REQUIRED", "Reopen requires a previously opened or saved project path.")
                resolved = require_allowed_path(path, base=session.workdir, must_exist=operation.op != "project_save", expect_file=True)
                if operation.op == "project_save":
                    if resolved.exists() and not operation.overwrite:
                        raise MechanicalError("MECHANICAL_OVERWRITE_DENIED", "Project exists; overwrite must be explicit.")
                    resolved.parent.mkdir(parents=True, exist_ok=True)
                    session.backend.project_save(str(resolved), overwrite=operation.overwrite)
                    if not resolved.is_file():
                        raise MechanicalError("MECHANICAL_ARTIFACT_MISSING", "Product did not create the saved project file.")
                    artifacts.append({"path": str(resolved), "name": operation_id, "size": resolved.stat().st_size})
                else:
                    options = {"reopen": operation.op == "project_reopen"}
                    if operation.system_name:
                        options["system_name"] = operation.system_name
                    session.backend.project_open(str(resolved), **options)
                    session.generation += 1
                results.append({"id": operation_id, "ok": True, "value": {"path": str(resolved), "generation": session.generation}})
                continue
            if operation.script_path:
                require_allowed_path(operation.script_path, base=session.workdir, must_exist=True, expect_file=True)
            for path, role, is_file in _typed_paths(operation.model_dump()):
                resolved = require_allowed_path(path, base=session.workdir, must_exist=role == "input", expect_file=is_file)
                if role == "output":
                    (resolved.parent if is_file else resolved).mkdir(parents=True, exist_ok=True)
            payload = operation.model_dump(exclude_none=True)
            engineering_data = operation.op == "engineering_data"
            if engineering_data:
                payload["op"] = operation.action
                if payload["target"] == "Model":
                    payload["target"] = "EngineeringData"
            result = session.backend.execute({
                "session_id": session.id, "generation": session.generation, "operation": payload,
                "namespace": "engineering_data" if engineering_data else "mechanical",
                "backend": session.backend_name, "product_version": session.backend.version,
                "max_inline_bytes": recipe.max_inline_bytes,
                "artifact_path": str(run_dir / f"{request_id}.{operation_id}.json"),
            }, engineering_data=engineering_data)
            session.generation = result["generation"]
            result["id"] = operation_id
            results.append(result)
            if result.get("artifact"):
                artifact = result["artifact"]
                artifact_path = require_allowed_path(artifact["path"], base=run_dir, must_exist=True, expect_file=True)
                if artifact_path.parent != run_dir:
                    raise MechanicalError("MECHANICAL_PROTOCOL_ERROR", "Product returned an unexpected artifact location.")
                artifacts.append({**artifact, "name": operation_id})
            if not result["ok"]:
                error = result.get("error", {})
                diagnostics.append({"code": error.get("code", "MECHANICAL_API_ERROR"), "message": error.get("message", "Product operation failed."), "operation_id": operation_id, "severity": "error"})
                break
            _record_evidence(session, operation, result)
        if not diagnostics and recipe.project.save_path:
            path = Path(recipe.project.save_path)
            if path.exists() and not recipe.project.overwrite:
                raise MechanicalError("MECHANICAL_OVERWRITE_DENIED", "Project exists; overwrite must be explicit.")
            path.parent.mkdir(parents=True, exist_ok=True)
            session.backend.project_save(str(path), overwrite=recipe.project.overwrite)
            if not path.is_file():
                raise MechanicalError("MECHANICAL_ARTIFACT_MISSING", "Product did not create the saved project file.")
            artifacts.append({"path": str(path), "name": "project", "size": path.stat().st_size})
        with session.lock:
            session.state = "ready" if not diagnostics else "operation_failed"
        response = {"ok": not diagnostics, "status": "complete" if not diagnostics else "failed",
                "outputs": {"session": session.summary(), "request_id": request_id, "operations": results,
                            "values": {item["id"]: item.get("value", {"artifact": item.get("artifact")}) for item in results if item.get("ok")}},
                "diagnostics": diagnostics, "artifacts": artifacts}
        return response
    except Exception as exc:
        code = _exception_code(exc, "MECHANICAL_EXECUTION_UNKNOWN")
        if isinstance(exc, WorkflowCancelled) or (request_token is not None and request_token.cancelled):
            code = "WORKFLOW_CANCELLED"
        if (getattr(exc, "winerror", None) == 740 and not results and session.backend is not None
                and session.backend.client is None and session.backend.workbench is None):
            # Windows explicitly refused process creation before product code ran.
            code = "MECHANICAL_ELEVATION_REQUIRED"
        with session.lock:
            session.state = "failed" if session.backend is None or isinstance(exc, MechanicalError) else "disconnected"
        response = _failure(code, str(exc), status="cancelled" if code == "WORKFLOW_CANCELLED" else ("unknown" if code in {"MECHANICAL_EXECUTION_UNKNOWN", "MECHANICAL_PROTOCOL_ERROR"} else "failed"),
                            outputs={"session": session.summary(), "request_id": request_id, "operations": results})
        response["diagnostics"][0]["exception_type"] = type(exc).__module__ + "." + type(exc).__name__
        return response
    finally:
        if created and recipe.mode == "batch" and session.backend is not None:
            try:
                session.backend.close()
                with session.lock:
                    session.closed = True
                    session.state = "closed"
                session.executor.shutdown(wait=False)
            except Exception:
                # Preserve ownership so a failed shutdown can be diagnosed/closed.
                with session.lock:
                    session.state = "close_failed"
        with session.lock:
            session.active_request = None
        if request_token and session.backend:
            for identity in session.backend.owned_processes:
                request_token.unregister_process(identity)
        if response is not None:
            response["outputs"]["session"] = session.summary()


def _managed_recipe(recipe: MechanicalRecipe, workdir: Path, run_dir: Path, timeout_seconds: float, *, elevated: bool) -> dict[str, Any]:
    from .elevation import ElevatedLaunchTimeout, ElevatedRequestRejected, ElevatedRequestTimeout, ElevatedSession
    token = current_cancellation_token()
    if token:
        token.raise_if_cancelled()
    if recipe.session.id:
        cached = _read_record("sessions", recipe.session.id)
        if cached and cached.get("session", {}).get("closed") and recipe.session.action in {"summary", "close"}:
            result = {"ok": True, "status": "closed", "outputs": {"session": cached["session"]}, "diagnostics": [], "artifacts": []}
            if recipe.session.action == "close":
                record = _read_record("workers", cached["worker_id"])
                if record and not record.get("closed"):
                    try:
                        _restore_worker(cached["worker_id"]).close()
                        record["closed"] = True
                        _save_observation("workers", cached["worker_id"], record, workdir, result)
                    except Exception:
                        result["diagnostics"].append({"code": "MECHANICAL_WORKER_CLOSE_UNVERIFIED", "severity": "warning", "message": "The product session is closed; its execution worker could not be confirmed closed."})
            return result
    proxy = _restore_session(recipe.session.id) if recipe.session.id else None
    if proxy is None:
        worker_id = uuid.uuid4().hex
        directory = workdir / ".ansys_mcp" / "mechanical-workers" / uuid.uuid4().hex
        _write_record("workers", worker_id, {"id": worker_id, "state_dir": str(directory), "worker": None,
                                            "elevated": elevated, "workdir": str(workdir), "closed": False, "state": "starting"}, workdir)
        try:
            proxy = ElevatedSession.launch(__name__, state_dir=directory, timeout_seconds=timeout_seconds, elevated=elevated)
        except ElevatedLaunchTimeout:
            return _failure("MECHANICAL_WORKER_STARTUP_UNKNOWN", "The original execution worker may still be starting. Observe the same worker id; no product request has been submitted.", status="unknown",
                            outputs={"worker_id": worker_id, "worker_request_id": "startup"})
        except Exception:
            _write_record("workers", worker_id, {"id": worker_id, "state_dir": str(directory), "worker": None,
                                                "elevated": elevated, "workdir": str(workdir), "closed": True, "state": "launch_failed"}, workdir)
            raise
        _ELEVATED_WORKERS[worker_id] = proxy
    worker_id = next((key for key, value in _ELEVATED_WORKERS.items() if value is proxy), uuid.uuid4().hex)
    _ELEVATED_WORKERS[worker_id] = proxy
    worker_record = {"id": worker_id, "state_dir": str(proxy.state_dir), "worker": proxy.summary().get("worker"),
                     "elevated": elevated, "workdir": str(workdir), "closed": False}
    previous = _read_record("workers", worker_id)
    if previous:
        worker_record = {**previous, "worker": worker_record["worker"], "state": "ready"}
    _write_record("workers", worker_id, worker_record, workdir)
    payload = recipe.model_dump()
    payload["launch"]["elevation"] = "never"
    try:
        result = proxy.request("execute_recipe", {"recipe": payload, "workdir": str(workdir), "run_dir": str(run_dir), "timeout_seconds": timeout_seconds}, timeout_seconds=timeout_seconds)
    except ElevatedRequestRejected as exc:
        result = _failure("MECHANICAL_WORKER_REQUEST_REJECTED", str(exc), outputs={"worker_id": worker_id})
        if recipe.session.action == "new":
            proxy.close()
            worker_record["closed"] = True
            _save_observation("workers", worker_id, worker_record, workdir, result)
        return result
    except ElevatedRequestTimeout as exc:
        return _failure("MECHANICAL_ELEVATED_OUTCOME_UNKNOWN", "The execution worker is still processing this request; query its request id instead of replaying it.", status="unknown",
                        outputs={"worker_request_id": exc.request_id, "worker_id": worker_id})
    except WorkflowCancelled as exc:
        if not getattr(exc, "request_id", None) and recipe.session.action == "new":
            result = _failure("WORKFLOW_CANCELLED", "Cancellation occurred before any product request was submitted.", status="cancelled", outputs={"worker_id": worker_id})
            proxy.close()
            worker_record["closed"] = True
            _save_observation("workers", worker_id, worker_record, workdir, result)
            return result
        return _failure("WORKFLOW_CANCELLED", "Cancellation was requested in the execution worker; observe the same request until its product processes stop.", status="unknown",
                        outputs={"worker_request_id": getattr(exc, "request_id", None), "worker_id": worker_id})
    except Exception as exc:
        output: dict[str, Any] = {"worker_id": worker_id, "worker_request_id": getattr(exc, "request_id", None)}
        try:
            # This is a read-only observation of the same actor, never a retry.
            summaries = proxy.request("session_summary", {}, timeout_seconds=5).get("sessions", [])
            candidate = next((item for item in summaries if item.get("id") == recipe.session.id), None)
            if candidate is None and recipe.session.action == "new" and len(summaries) == 1:
                candidate = summaries[0]
            if candidate:
                output.update(session=candidate, request_id=candidate.get("active_request") or candidate.get("last_request_id"))
                _ELEVATED[candidate["id"]] = proxy
                _write_record("sessions", candidate["id"], {"id": candidate["id"], "worker_id": worker_id, "elevated": worker_record["elevated"], "session": candidate}, workdir)
        except Exception:
            pass
        return _failure("MECHANICAL_WORKER_RESPONSE_UNKNOWN", str(exc), status="unknown", outputs=output)
    session = result.get("outputs", {}).get("session", {})
    if session.get("id"):
        _ELEVATED[session["id"]] = proxy
        _save_observation("sessions", session["id"], {"id": session["id"], "worker_id": worker_id, "elevated": worker_record["elevated"], "session": session}, workdir, result)
    if (recipe.session.action == "new" and recipe.launch.elevation == "auto" and not elevated
            and result.get("diagnostics") and result["diagnostics"][0]["code"] == "MECHANICAL_ELEVATION_REQUIRED"):
        if session.get("id"):
            closed = proxy.request("execute_recipe", {"recipe": {"backend": recipe.backend, "launch": {"elevation": "never"}, "session": {"action": "close", "id": session["id"]}},
                                              "workdir": str(workdir), "run_dir": str(run_dir), "timeout_seconds": 15}, timeout_seconds=20)
            _save_observation("sessions", session["id"], {"id": session["id"], "worker_id": worker_id, "elevated": worker_record["elevated"], "session": closed.get("outputs", {}).get("session", session)}, workdir, result)
        proxy.close()
        worker_record["closed"] = True
        _save_observation("workers", worker_id, worker_record, workdir, result)
        return _managed_recipe(recipe, workdir, run_dir, timeout_seconds, elevated=True)
    if session.get("closed"):
        proxy.close()
        worker_record["closed"] = True
        _save_observation("workers", worker_id, worker_record, workdir, result)
    result.setdefault("outputs", {})["worker_id"] = worker_id
    return result


def _elevated_recipe(recipe: MechanicalRecipe, workdir: Path, run_dir: Path, timeout_seconds: float) -> dict[str, Any]:
    return _managed_recipe(recipe, workdir, run_dir, timeout_seconds, elevated=True)


def mechanical_elevated_request_status(worker_id: str, worker_request_id: str) -> dict[str, Any]:
    """Observe the same administrator request after a transport timeout."""
    try:
        proxy = _restore_worker(worker_id)
        if worker_request_id == "startup":
            return {"ok": True, "status": "needs_attention", "outputs": {"worker_id": worker_id, "worker": proxy.summary().get("worker"),
                                                                                 "product_request_submitted": False},
                    "diagnostics": [{"code": "MECHANICAL_WORKER_READY_NO_REPLAY", "severity": "warning", "message": "The same worker is ready. The original product recipe was not submitted and was not replayed."}], "artifacts": []}
        observation = proxy.query(worker_request_id)
    except Exception as exc:
        record = _read_record("workers", worker_id)
        if record and record.get("state") == "starting" and record.get("worker") is None:
            return {"ok": True, "status": "starting", "outputs": {"worker_id": worker_id, "worker_request_id": worker_request_id},
                    "diagnostics": [{"code": "MECHANICAL_WORKER_STARTING", "severity": "warning", "message": "The original worker has not reported a verifiable ready identity; no launch was repeated."}], "artifacts": []}
        return _failure(getattr(exc, "code", "MECHANICAL_WORKER_UNAVAILABLE"), str(exc))
    result = observation.get("result")
    if isinstance(result, dict):
        session = result.get("outputs", {}).get("session", {})
        if session.get("id"):
            _ELEVATED[session["id"]] = proxy
            record = _read_record("workers", worker_id)
            if record:
                _save_observation("sessions", session["id"], {"id": session["id"], "worker_id": worker_id, "elevated": record["elevated"], "session": session}, Path(record["workdir"]), observation)
    return observation


def _cancel_local_request(session_id: str, request_id: str, grace_seconds: float) -> dict[str, Any]:
    session = _SESSIONS.get(session_id)
    if session is None or request_id not in session.futures:
        return _failure("MECHANICAL_REQUEST_MISSING", "The request is not owned by this execution worker.")
    future = session.futures[request_id]
    if future.done():
        return {"ok": True, "status": "complete", "outputs": {"session": session.summary(), "request_id": request_id}, "diagnostics": [], "artifacts": []}
    token = session.tokens.get(request_id)
    if token:
        token.cancel()
    if future.cancel():
        session.active_request = None
        return {"ok": True, "status": "cancelled", "outputs": {"session": session.summary(), "request_id": request_id}, "diagnostics": [], "artifacts": []}
    def stop_only_owned():
        deadline = time.monotonic() + max(0.1, grace_seconds)
        while time.monotonic() < deadline and not future.done():
            time.sleep(0.05)
        if not future.done() and session.backend:
            for identity in session.backend.owned_processes:
                terminate_owned_process(identity)
            session.state = "disconnected"
    threading.Thread(target=stop_only_owned, daemon=True, name="mechanical-owned-cancellation").start()
    return {"ok": True, "status": "cancelling", "outputs": {"session": session.summary(), "request_id": request_id}, "diagnostics": [], "artifacts": []}


def cancel_mechanical_request(
    session_id: str | None = None, request_id: str | None = None,
    worker_id: str | None = None, worker_request_id: str | None = None, grace_seconds: float = 5,
) -> dict[str, Any]:
    """Cancel through the same authenticated owner, never from a controller PID."""
    try:
        if session_id and request_id:
            if _WORKER_CONTEXT.get():
                return _cancel_local_request(session_id, request_id, grace_seconds)
            proxy = _restore_session(session_id)
            return proxy.request("cancel_request", {"session_id": session_id, "request_id": request_id, "grace_seconds": grace_seconds}, timeout_seconds=10)
        if worker_id and worker_request_id:
            return _restore_worker(worker_id).cancel(worker_request_id, grace_seconds=grace_seconds)
        return _failure("MECHANICAL_CANCEL_HANDLE_REQUIRED", "Provide the original session/request or worker/request pair.")
    except Exception as exc:
        return _failure(getattr(exc, "code", "MECHANICAL_CANCEL_FAILED"), str(exc))


def run_mechanical_recipe(
    recipe: MechanicalRecipe | dict[str, Any], workdir: Path, run_dir: Path, timeout_seconds: float,
) -> dict[str, Any]:
    """Run one serialized recipe, retaining live sessions across MCP requests.

    A host observation timeout leaves the original future running and returns its
    request id. Call mechanical_request_status to observe it; never replay it.
    """
    try:
        model = recipe if isinstance(recipe, MechanicalRecipe) else MechanicalRecipe.model_validate(recipe)
        workdir = require_allowed_path(workdir, expect_file=False)
        run_dir = require_allowed_path(run_dir, base=workdir, expect_file=False)
        validate_mechanical_recipe_paths(model, workdir)
        model = _normalize_paths(model, workdir)
        if timeout_seconds <= 0:
            raise ValueError("Mechanical timeout must be positive.")
        if not _WORKER_CONTEXT.get():
            return _managed_recipe(model, workdir, run_dir, timeout_seconds, elevated=model.launch.elevation == "require")
        if model.session.action in {"summary", "close"}:
            with _REGISTRY_LOCK:
                session = _SESSIONS.get(model.session.id or "")
            if session is None:
                raise MechanicalError("MECHANICAL_SESSION_MISSING", "Mechanical session is not owned by this server.")
            if model.session.action == "close":
                with session.lock:
                    if session.active_request:
                        raise MechanicalError("MECHANICAL_SESSION_BUSY", "Wait for the active request before closing this session.")
                    if session.closed:
                        return {"ok": True, "status": "closed", "outputs": {"session": session.summary()}, "diagnostics": [], "artifacts": []}
                    request_id = uuid.uuid4().hex
                    session.active_request = request_id
                    def close_on_session_thread():
                        close_token = current_cancellation_token()
                        try:
                            if session.backend is not None:
                                if close_token:
                                    for identity in session.backend.owned_processes:
                                        close_token.register_process(identity)
                                session.backend.close()
                            session.closed = True
                            session.state = "closed"
                            return {"ok": True, "status": "closed", "outputs": {"session": session.summary(), "request_id": request_id}, "diagnostics": [], "artifacts": []}
                        except Exception as exc:
                            session.state = "close_failed"
                            return _failure(getattr(exc, "code", "MECHANICAL_CLOSE_FAILED"), str(exc), outputs={"session": session.summary(), "request_id": request_id})
                        finally:
                            if close_token and session.backend:
                                for identity in session.backend.owned_processes:
                                    close_token.unregister_process(identity)
                            session.active_request = None
                    context = contextvars.copy_context()
                    future = session.executor.submit(context.run, close_on_session_thread)
                    session.futures[request_id] = future
                try:
                    result = future.result(timeout=timeout_seconds)
                    result["outputs"]["session"] = session.summary()
                    if session.closed:
                        session.executor.shutdown(wait=False)
                    return result
                except concurrent.futures.TimeoutError:
                    return _failure("MECHANICAL_OUTCOME_UNKNOWN", "Session shutdown is still running; observe its request id.", status="unknown", outputs={"session": session.summary(), "request_id": request_id})
            return {"ok": True, "status": session.state, "outputs": {"session": session.summary()}, "diagnostics": [], "artifacts": []}
        created = model.session.action == "new"
        if created:
            # Check required dependencies before creating a session or launching.
            _dependencies(model.backend)
            session_id = uuid.uuid4().hex
            directory = require_allowed_path(workdir / ".ansys_mcp" / "mechanical" / session_id, expect_file=False)
            directory.mkdir(parents=True, exist_ok=True)
            session = _Session(session_id, model.backend, workdir, directory)
            with _REGISTRY_LOCK:
                _SESSIONS[session_id] = session
        else:
            with _REGISTRY_LOCK:
                session = _SESSIONS.get(model.session.id or "")
            if session is None:
                raise MechanicalError("MECHANICAL_SESSION_MISSING", "Mechanical session is not owned by this server.")
            if session.backend_name != model.backend:
                raise MechanicalError("MECHANICAL_BACKEND_MISMATCH", "An existing session cannot change backend.")
        run_dir.mkdir(parents=True, exist_ok=True)
        request_id = uuid.uuid4().hex
        with session.lock:
            if session.closed:
                raise MechanicalError("MECHANICAL_SESSION_CLOSED", "Mechanical session has been closed.")
            if session.active_request:
                raise MechanicalError("MECHANICAL_SESSION_BUSY", "A request already owns this session; observe its request id before submitting another mutation.")
            if session.state in {"disconnected", "failed", "close_failed"}:
                raise MechanicalError("MECHANICAL_SESSION_DISCONNECTED", "Session outcome is unknown; no operations are replayed or switched to another backend.")
            session.active_request = request_id
            session.tokens[request_id] = current_cancellation_token()
            context = contextvars.copy_context()
            future = session.executor.submit(context.run, _execute, session, model, run_dir, request_id, created)
            session.futures[request_id] = future
        try:
            response = future.result(timeout=timeout_seconds)
            if (created and model.launch.elevation == "auto" and response.get("diagnostics")
                    and response["diagnostics"][0]["code"] == "MECHANICAL_ELEVATION_REQUIRED"):
                return _elevated_recipe(model, workdir, run_dir, timeout_seconds)
            return response
        except concurrent.futures.TimeoutError:
            return _failure("MECHANICAL_OUTCOME_UNKNOWN", "The observation timeout expired. The original product request remains live; query its request id before continuing.", status="unknown",
                            outputs={"session": session.summary(), "request_id": request_id})
    except Exception as exc:
        if isinstance(exc, WorkflowCancelled):
            return _failure("WORKFLOW_CANCELLED", str(exc), status="cancelled")
        return _failure(_exception_code(exc, "MECHANICAL_VALIDATION_ERROR"), str(exc))


def _worker_dispatch(operation: str, payload: dict[str, Any]) -> dict[str, Any]:
    if operation == "execute_recipe":
        return run_mechanical_recipe(payload["recipe"], Path(payload["workdir"]), Path(payload["run_dir"]), float(payload["timeout_seconds"]))
    if operation == "session_summary":
        return {"ok": True, "sessions": session_summary()}
    if operation == "request_status":
        return mechanical_request_status(payload["session_id"], payload["request_id"])
    if operation == "cancel_request":
        return _cancel_local_request(payload["session_id"], payload["request_id"], float(payload.get("grace_seconds", 5)))
    raise MechanicalError("MECHANICAL_WORKER_OPERATION_UNKNOWN", "Unknown Mechanical worker operation.")


def worker_dispatch(operation: str, payload: dict[str, Any]) -> dict[str, Any]:
    """JSON-only handler; all operations remain in this worker's local registry."""
    marker = _WORKER_CONTEXT.set(True)
    try:
        return _worker_dispatch(operation, payload)
    finally:
        _WORKER_CONTEXT.reset(marker)


MechanicalProject = MechanicalProjectSpec
