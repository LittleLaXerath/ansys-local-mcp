from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .command_registry import command_argv, command_specs, inspect_commands, supported_command_ids
from .config import (
    ANSYS_ROOT,
    VERSION_TAG,
    allowed_roots,
    ansys_env,
    default_workdir,
    known_paths,
    require_allowed_file,
    require_allowed_path,
    safe_workdir,
)
from .runtime import ansys_python_executable


ERROR_VALIDATION = "VALIDATION_ERROR"
ERROR_PATH_DENIED = "PATH_DENIED"
ERROR_EXECUTABLE_MISSING = "EXECUTABLE_MISSING"
ERROR_PROCESS_TIMEOUT = "PROCESS_TIMEOUT"
ERROR_PROCESS_NONZERO = "PROCESS_NONZERO"
ERROR_PYFLUENT_IMPORT = "PYFLUENT_IMPORT_ERROR"
ERROR_PYFLUENT_OPERATION = "PYFLUENT_OPERATION_ERROR"
ERROR_ARTIFACT_MISSING = "ARTIFACT_MISSING"

STEP_TYPES = ("command", "python_script", "pyfluent", "file", "assert", "collect")
PYFLUENT_OPERATIONS = (
    "read",
    "write",
    "set_state",
    "set_constant",
    "get_state",
    "call",
    "tui",
    "iterate",
    "calculate",
    "create",
    "compute_reports",
    "inspect_path",
)

PATH_SUFFIXES = (
    ".cas",
    ".cas.h5",
    ".dat",
    ".dat.h5",
    ".jou",
    ".msh",
    ".msh.gz",
    ".json",
    ".txt",
    ".trn",
    ".log",
    ".py",
    ".wbjn",
    ".out",
)


class WorkflowProblem(Exception):
    def __init__(self, code: str, message: str, step_id: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.step_id = step_id

    def diagnostic(self) -> dict[str, Any]:
        data = {"code": self.code, "message": self.message}
        if self.step_id:
            data["step_id"] = self.step_id
        return data


class ArtifactSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    path: str
    required: bool = True


class WorkflowStep(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str | None = None
    type: Literal["command", "python_script", "pyfluent", "file", "assert", "collect"]
    continue_on_error: bool | None = None
    timeout_seconds: int | None = None
    command_id: str | None = None
    args: list[Any] = Field(default_factory=list)
    script_path: str | None = None
    arguments: list[Any] = Field(default_factory=list)
    action: str | None = None
    path: str | None = None
    source: str | None = None
    target: str | None = None
    content: str | None = None
    exists: bool | None = None
    artifacts: list[ArtifactSpec] = Field(default_factory=list)
    launch: dict[str, Any] = Field(default_factory=dict)
    read: Any = None
    operations: list[dict[str, Any]] = Field(default_factory=list)
    write: dict[str, Any] = Field(default_factory=dict)


class WorkflowModel(BaseModel):
    model_config = ConfigDict(extra="allow")

    version: str | int = "1"
    workdir: str | None = None
    variables: dict[str, Any] = Field(default_factory=dict)
    steps: list[WorkflowStep]
    artifacts: list[ArtifactSpec] = Field(default_factory=list)
    continue_on_error: bool = False

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: str | int) -> str | int:
        if str(value) not in {"1", "1.0"}:
            raise ValueError("Only workflow version 1 is supported.")
        return value


def inspect_runtime() -> dict[str, Any]:
    paths = known_paths()
    return {
        "ansys_root": str(ANSYS_ROOT),
        "version_tag": VERSION_TAG,
        "allowed_roots": [str(path) for path in allowed_roots()],
        "default_workdir": str(default_workdir()),
        "paths": {
            name: {"path": str(path), "exists": path.exists(), "is_file": path.is_file()}
            for name, path in paths.items()
        },
        "commands": inspect_commands(),
        "workflow": {
            "version": "1",
            "step_types": list(STEP_TYPES),
            "pyfluent_operations": list(PYFLUENT_OPERATIONS),
        },
        "pyfluent_core": {
            "path": str(paths["pyfluent_core"]),
            "exists": paths["pyfluent_core"].exists(),
        },
    }


def validate_workflow(
    workflow: dict[str, Any] | None = None,
    workflow_path: str | None = None,
    variables: dict[str, Any] | None = None,
) -> dict[str, Any]:
    started = time.time()
    diagnostics: list[dict[str, Any]] = []
    try:
        model = load_workflow(workflow, workflow_path, variables)
        _validate_model(model)
        return {
            "ok": True,
            "stage": "validation",
            "diagnostics": diagnostics,
            "workflow": _workflow_info(model),
            "elapsed_s": round(time.time() - started, 3),
        }
    except WorkflowProblem as exc:
        diagnostics.append(exc.diagnostic())
    except ValidationError as exc:
        diagnostics.append({"code": ERROR_VALIDATION, "message": str(exc)})
    except Exception as exc:
        diagnostics.append({"code": ERROR_VALIDATION, "message": repr(exc)})
    return {
        "ok": False,
        "stage": "validation",
        "diagnostics": diagnostics,
        "elapsed_s": round(time.time() - started, 3),
    }


def run_workflow(
    workflow: dict[str, Any] | None = None,
    workflow_path: str | None = None,
    variables: dict[str, Any] | None = None,
    timeout_seconds: int = 7200,
) -> dict[str, Any]:
    run_id = _run_id()
    started = time.time()
    diagnostics: list[dict[str, Any]] = []
    step_results: list[dict[str, Any]] = []
    run_dir: Path | None = None

    try:
        model = load_workflow(workflow, workflow_path, variables)
        _validate_model(model)
        cwd = safe_workdir(model.workdir, default_workdir())
        run_dir = cwd / ".ansys_mcp" / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        deadline = time.time() + max(1, int(timeout_seconds))
        for index, step in enumerate(model.steps):
            step_result = _run_step(step, index, cwd, run_dir, max(1, int(deadline - time.time())))
            step_results.append(step_result)
            if not step_result["ok"]:
                diagnostics.extend(step_result.get("diagnostics", []))
                if not _should_continue(model, step):
                    break

        artifacts = _collect_artifacts(model.artifacts, run_dir)
        diagnostics.extend(_artifact_diagnostics(artifacts))
        ok = not diagnostics and all(step.get("ok", False) for step in step_results)
        summary = _summary(run_id, ok, "complete", started, step_results, artifacts, diagnostics, run_dir)
    except WorkflowProblem as exc:
        diagnostics.append(exc.diagnostic())
        summary = _summary(run_id, False, "validation", started, step_results, [], diagnostics, run_dir)
    except ValidationError as exc:
        diagnostics.append({"code": ERROR_VALIDATION, "message": str(exc)})
        summary = _summary(run_id, False, "validation", started, step_results, [], diagnostics, run_dir)
    except Exception as exc:
        diagnostics.append({"code": ERROR_VALIDATION, "message": repr(exc)})
        summary = _summary(run_id, False, "fatal", started, step_results, [], diagnostics, run_dir)

    if run_dir is None:
        run_dir = default_workdir() / ".ansys_mcp" / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        summary["run_dir"] = str(run_dir)
    _write_manifest(run_dir, summary)
    return summary


def load_workflow(
    workflow: dict[str, Any] | None = None,
    workflow_path: str | None = None,
    variables: dict[str, Any] | None = None,
) -> WorkflowModel:
    if workflow is not None and workflow_path is not None:
        raise WorkflowProblem(ERROR_VALIDATION, "Pass either workflow or workflow_path, not both.")
    if workflow_path is not None:
        path = require_allowed_file(workflow_path)
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    elif workflow is not None:
        payload = workflow
    else:
        raise WorkflowProblem(ERROR_VALIDATION, "Pass workflow or workflow_path.")
    if not isinstance(payload, dict):
        raise WorkflowProblem(ERROR_VALIDATION, "Workflow must be a JSON object.")

    merged_variables = dict(payload.get("variables", {}))
    merged_variables.update(variables or {})
    substituted = _substitute(payload, merged_variables)
    substituted["variables"] = merged_variables
    return WorkflowModel.model_validate(substituted)


def legacy_python_script_workflow(
    script_path: str,
    arguments: list[str] | None = None,
    workdir: str | None = None,
) -> dict[str, Any]:
    return {
        "version": "1",
        "workdir": workdir,
        "steps": [
            {
                "id": "ansys_python_script",
                "type": "python_script",
                "script_path": script_path,
                "arguments": arguments or [],
            }
        ],
    }


def legacy_pyfluent_workflow(
    recipe: dict[str, Any] | None = None,
    recipe_path: str | None = None,
    workdir: str | None = None,
) -> dict[str, Any]:
    if recipe_path:
        recipe_file = require_allowed_file(recipe_path)
        payload = json.loads(recipe_file.read_text(encoding="utf-8-sig"))
        workdir = workdir or str(recipe_file.parent)
    else:
        payload = recipe or {}
    return {
        "version": "1",
        "workdir": workdir or payload.get("workdir"),
        "steps": [
            {
                "id": "pyfluent_recipe",
                "type": "pyfluent",
                "launch": payload.get("launch", {}),
                "read": payload.get("read"),
                "operations": payload.get("operations", []),
                "write": payload.get("write", {}),
            }
        ],
    }


def _validate_model(model: WorkflowModel) -> None:
    require_allowed_path(model.workdir or default_workdir(), must_exist=False, expect_file=False)
    for index, step in enumerate(model.steps):
        step_id = _step_id(step, index)
        if step.type == "command":
            _validate_command_step(step, step_id)
        elif step.type == "python_script":
            _validate_python_script_step(step, step_id)
        elif step.type == "pyfluent":
            _validate_pyfluent_step(step, step_id)
        elif step.type == "file":
            _validate_file_step(step, step_id)
        elif step.type == "assert":
            _validate_assert_step(step, step_id)
        elif step.type == "collect":
            for artifact in step.artifacts:
                _validate_path_string(artifact.path, must_exist=False)
    for artifact in model.artifacts:
        _validate_path_string(artifact.path, must_exist=False)


def _validate_command_step(step: WorkflowStep, step_id: str) -> None:
    if not step.command_id:
        raise WorkflowProblem(ERROR_VALIDATION, "command step requires command_id.", step_id)
    specs = command_specs()
    if step.command_id not in specs:
        raise WorkflowProblem(ERROR_VALIDATION, f"Unknown command_id: {step.command_id}", step_id)
    if not specs[step.command_id].executable.exists():
        raise WorkflowProblem(ERROR_EXECUTABLE_MISSING, f"Executable not found: {specs[step.command_id].executable}", step_id)
    _validate_argv(step.args, step_id)


def _validate_python_script_step(step: WorkflowStep, step_id: str) -> None:
    if not step.script_path:
        raise WorkflowProblem(ERROR_VALIDATION, "python_script step requires script_path.", step_id)
    require_allowed_file(step.script_path)
    _validate_argv(step.arguments, step_id)


def _validate_pyfluent_step(step: WorkflowStep, step_id: str) -> None:
    pyfluent_core = known_paths()["pyfluent_core"]
    if not pyfluent_core.exists():
        raise WorkflowProblem(ERROR_PYFLUENT_IMPORT, f"PyFluent core path not found: {pyfluent_core}", step_id)
    if step.read:
        _validate_pyfluent_read(step.read, step_id)
    if step.write:
        _validate_value_paths(step.write, must_exist=False)
    _validate_value_paths(step.launch, must_exist=False)
    for operation in step.operations:
        if operation.get("op") not in PYFLUENT_OPERATIONS and operation.get("op") != "create_force_report":
            raise WorkflowProblem(ERROR_VALIDATION, f"Unsupported PyFluent operation: {operation.get('op')}", step_id)
        _validate_value_paths(operation, must_exist=False)


def _validate_file_step(step: WorkflowStep, step_id: str) -> None:
    action = step.action or "exists"
    if action not in {"exists", "read_text", "write_text", "copy"}:
        raise WorkflowProblem(ERROR_VALIDATION, f"Unsupported file action: {action}", step_id)
    if action in {"exists", "read_text"}:
        if not step.path:
            raise WorkflowProblem(ERROR_VALIDATION, f"file action {action} requires path.", step_id)
        _validate_path_string(step.path, must_exist=action == "read_text")
    elif action == "write_text":
        target = step.target or step.path
        if not target:
            raise WorkflowProblem(ERROR_VALIDATION, "file write_text requires target or path.", step_id)
        _validate_path_string(target, must_exist=False)
    elif action == "copy":
        if not step.source or not step.target:
            raise WorkflowProblem(ERROR_VALIDATION, "file copy requires source and target.", step_id)
        _validate_path_string(step.source, must_exist=True)
        _validate_path_string(step.target, must_exist=False)


def _validate_assert_step(step: WorkflowStep, step_id: str) -> None:
    if not step.path:
        raise WorkflowProblem(ERROR_VALIDATION, "assert step currently requires path.", step_id)
    _validate_path_string(step.path, must_exist=False)


def _validate_pyfluent_read(read: Any, step_id: str) -> None:
    if isinstance(read, str):
        _validate_path_string(read, must_exist=True)
        return
    if not isinstance(read, dict):
        raise WorkflowProblem(ERROR_VALIDATION, "pyfluent read must be a string or object.", step_id)
    case = read.get("case") or read.get("case_file") or read.get("case_data")
    if case:
        _validate_path_string(str(case), must_exist=True)


def _run_step(step: WorkflowStep, index: int, cwd: Path, run_dir: Path, remaining_timeout: int) -> dict[str, Any]:
    step_id = _step_id(step, index)
    started = time.time()
    try:
        if step.type == "command":
            return _run_process_step(step, step_id, cwd, started, remaining_timeout)
        if step.type == "python_script":
            return _run_python_script_step(step, step_id, cwd, started, remaining_timeout)
        if step.type == "pyfluent":
            return _run_pyfluent_step(step, step_id, cwd, run_dir, started, remaining_timeout)
        if step.type == "file":
            return _run_file_step(step, step_id, cwd, started)
        if step.type == "assert":
            return _run_assert_step(step, step_id, cwd, started)
        if step.type == "collect":
            return _ok_step(step_id, step.type, started, {"artifacts": _collect_artifacts(step.artifacts, run_dir)})
        raise WorkflowProblem(ERROR_VALIDATION, f"Unsupported step type: {step.type}", step_id)
    except WorkflowProblem as exc:
        return _error_step(step_id, step.type, started, exc.code, exc.message)
    except subprocess.TimeoutExpired as exc:
        return _error_step(
            step_id,
            step.type,
            started,
            ERROR_PROCESS_TIMEOUT,
            f"Process timed out after {exc.timeout} seconds.",
            stdout_tail=_tail(exc.stdout or ""),
            stderr_tail=_tail(exc.stderr or ""),
        )
    except Exception as exc:
        return _error_step(step_id, step.type, started, ERROR_VALIDATION, repr(exc))


def _run_process_step(
    step: WorkflowStep,
    step_id: str,
    cwd: Path,
    started: float,
    remaining_timeout: int,
) -> dict[str, Any]:
    argv = command_argv(str(step.command_id), step.args)
    if bool(getattr(step, "detach", False)):
        proc = subprocess.Popen(argv, cwd=str(cwd), env=ansys_env())
        return _ok_step(
            step_id,
            step.type,
            started,
            {"pid": proc.pid, "command": argv, "workdir": str(cwd), "detached": True},
        )
    proc = subprocess.run(
        argv,
        cwd=str(cwd),
        env=ansys_env(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=min(step.timeout_seconds or remaining_timeout, remaining_timeout),
    )
    result = _process_step_result(step_id, step.type, started, argv, cwd, proc)
    if proc.returncode != 0:
        result["ok"] = False
        result["diagnostics"] = [
            {"code": ERROR_PROCESS_NONZERO, "message": f"Process exited with return code {proc.returncode}.", "step_id": step_id}
        ]
    return result


def _run_python_script_step(
    step: WorkflowStep,
    step_id: str,
    cwd: Path,
    started: float,
    remaining_timeout: int,
) -> dict[str, Any]:
    if not step.script_path:
        raise WorkflowProblem(ERROR_VALIDATION, "python_script step requires script_path.", step_id)
    argv = command_argv("ansys_python", [step.script_path, *step.arguments])
    proc = subprocess.run(
        argv,
        cwd=str(cwd),
        env=ansys_env(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=min(step.timeout_seconds or remaining_timeout, remaining_timeout),
    )
    result = _process_step_result(step_id, step.type, started, argv, cwd, proc)
    if proc.returncode != 0:
        result["ok"] = False
        result["diagnostics"] = [
            {"code": ERROR_PROCESS_NONZERO, "message": f"Script exited with return code {proc.returncode}.", "step_id": step_id}
        ]
    return result


def _run_pyfluent_step(
    step: WorkflowStep,
    step_id: str,
    cwd: Path,
    run_dir: Path,
    started: float,
    remaining_timeout: int,
) -> dict[str, Any]:
    payload = {
        "launch": step.launch,
        "read": step.read,
        "operations": step.operations,
        "write": step.write,
        "workdir": str(cwd),
    }
    recipe_file = run_dir / f"{step_id}.pyfluent.json"
    recipe_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    runner = Path(__file__).with_name("pyfluent_recipe_runner.py")
    argv = [str(ansys_python_executable()), str(runner), str(recipe_file)]
    proc = subprocess.run(
        argv,
        cwd=str(cwd),
        env=ansys_env(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=min(step.timeout_seconds or remaining_timeout, remaining_timeout),
    )
    result = _process_step_result(step_id, step.type, started, argv, cwd, proc)
    result["recipe_file"] = str(recipe_file)
    if proc.returncode != 0:
        code = ERROR_PYFLUENT_OPERATION
        if "PYFLUENT_IMPORT_ERROR" in proc.stdout or "No module named" in proc.stderr:
            code = ERROR_PYFLUENT_IMPORT
        result["ok"] = False
        result["diagnostics"] = [
            {"code": code, "message": f"PyFluent step exited with return code {proc.returncode}.", "step_id": step_id}
        ]
    return result


def _run_file_step(step: WorkflowStep, step_id: str, cwd: Path, started: float) -> dict[str, Any]:
    action = step.action or "exists"
    if action == "exists":
        path = _resolve_work_path(cwd, step.path)
        return _ok_step(step_id, step.type, started, {"path": str(path), "exists": path.exists()})
    if action == "read_text":
        path = _resolve_work_path(cwd, step.path)
        return _ok_step(step_id, step.type, started, {"path": str(path), "text": path.read_text(encoding="utf-8")})
    if action == "write_text":
        path = _resolve_work_path(cwd, step.target or step.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(step.content or "", encoding="utf-8")
        return _ok_step(step_id, step.type, started, {"path": str(path), "bytes": path.stat().st_size})
    if action == "copy":
        source = _resolve_work_path(cwd, step.source)
        target = _resolve_work_path(cwd, step.target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        return _ok_step(step_id, step.type, started, {"source": str(source), "target": str(target)})
    raise WorkflowProblem(ERROR_VALIDATION, f"Unsupported file action: {action}", step_id)


def _run_assert_step(step: WorkflowStep, step_id: str, cwd: Path, started: float) -> dict[str, Any]:
    path = _resolve_work_path(cwd, step.path)
    expected = True if step.exists is None else bool(step.exists)
    actual = path.exists()
    if actual != expected:
        raise WorkflowProblem(ERROR_VALIDATION, f"Assertion failed for {path}: exists={actual}, expected={expected}.", step_id)
    return _ok_step(step_id, step.type, started, {"path": str(path), "exists": actual})


def _workflow_info(model: WorkflowModel) -> dict[str, Any]:
    return {
        "version": str(model.version),
        "workdir": str(require_allowed_path(model.workdir or default_workdir(), must_exist=False, expect_file=False)),
        "step_count": len(model.steps),
        "steps": [{"id": _step_id(step, index), "type": step.type} for index, step in enumerate(model.steps)],
        "artifact_count": len(model.artifacts),
    }


def _substitute(value: Any, variables: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        return {key: _substitute(child, variables) for key, child in value.items()}
    if isinstance(value, list):
        return [_substitute(child, variables) for child in value]
    if not isinstance(value, str):
        return value

    pattern = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
    full = pattern.fullmatch(value)
    if full:
        name = full.group(1) or full.group(2)
        if name not in variables:
            raise WorkflowProblem(ERROR_VALIDATION, f"Unknown workflow variable: {name}")
        return variables[name]

    def replace(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        if name not in variables:
            raise WorkflowProblem(ERROR_VALIDATION, f"Unknown workflow variable: {name}")
        return str(variables[name])

    return pattern.sub(replace, value)


def _validate_argv(args: list[Any], step_id: str) -> None:
    if not isinstance(args, list):
        raise WorkflowProblem(ERROR_VALIDATION, "Command arguments must be a JSON list.", step_id)
    for arg in args:
        if not isinstance(arg, (str, int, float, bool)):
            raise WorkflowProblem(ERROR_VALIDATION, f"Unsupported argv value: {arg!r}", step_id)
        if isinstance(arg, str) and _contains_shell_operator(arg):
            raise WorkflowProblem(ERROR_VALIDATION, f"Shell control operators are not allowed in argv: {arg!r}", step_id)


def _contains_shell_operator(value: str) -> bool:
    return any(token in value for token in ("&&", "||", "|", ";", "$(", "`", "<", ">"))


def _validate_value_paths(value: Any, must_exist: bool) -> None:
    if isinstance(value, dict):
        for child in value.values():
            _validate_value_paths(child, must_exist)
    elif isinstance(value, list):
        for child in value:
            _validate_value_paths(child, must_exist)
    elif isinstance(value, str) and _looks_like_path(value):
        _validate_path_string(value, must_exist=False if not Path(value).exists() else must_exist)


def _validate_path_string(value: str, must_exist: bool) -> Path:
    try:
        path = Path(value).expanduser()
        if must_exist:
            return require_allowed_path(path, must_exist=True)
        if path.exists():
            return require_allowed_path(path, must_exist=True)
        parent = path.parent if str(path.parent) not in {"", "."} else default_workdir()
        require_allowed_path(parent, must_exist=False, expect_file=False)
        return require_allowed_path(path, must_exist=False)
    except ValueError as exc:
        raise WorkflowProblem(ERROR_PATH_DENIED, str(exc)) from exc


def _looks_like_path(value: str) -> bool:
    text = value.strip()
    if not text:
        return False
    return any(marker in text for marker in (":\\", ":/", "\\", "/")) or text.lower().endswith(PATH_SUFFIXES)


def _resolve_work_path(cwd: Path, value: str | None) -> Path:
    if not value:
        raise WorkflowProblem(ERROR_VALIDATION, "Missing path value.")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = cwd / path
    return require_allowed_path(path, must_exist=False)


def _step_id(step: WorkflowStep, index: int) -> str:
    return step.id or f"step_{index + 1}"


def _should_continue(model: WorkflowModel, step: WorkflowStep) -> bool:
    return model.continue_on_error if step.continue_on_error is None else bool(step.continue_on_error)


def _run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]


def _ok_step(step_id: str, step_type: str, started: float, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    data = {
        "id": step_id,
        "type": step_type,
        "ok": True,
        "elapsed_s": round(time.time() - started, 3),
        "diagnostics": [],
    }
    data.update(extra or {})
    return data


def _error_step(
    step_id: str,
    step_type: str,
    started: float,
    code: str,
    message: str,
    stdout_tail: str = "",
    stderr_tail: str = "",
) -> dict[str, Any]:
    return {
        "id": step_id,
        "type": step_type,
        "ok": False,
        "elapsed_s": round(time.time() - started, 3),
        "stdout_tail": stdout_tail,
        "stderr_tail": stderr_tail,
        "diagnostics": [{"code": code, "message": message, "step_id": step_id}],
    }


def _process_step_result(
    step_id: str,
    step_type: str,
    started: float,
    argv: list[str],
    cwd: Path,
    proc: subprocess.CompletedProcess[str],
) -> dict[str, Any]:
    return {
        "id": step_id,
        "type": step_type,
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "command_id": argv[0],
        "command": argv,
        "workdir": str(cwd),
        "stdout_tail": _tail(proc.stdout),
        "stderr_tail": _tail(proc.stderr),
        "elapsed_s": round(time.time() - started, 3),
        "diagnostics": [],
    }


def _tail(value: str | bytes, limit: int = 20000) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return value[-limit:] if value else ""


def _collect_artifacts(specs: list[ArtifactSpec], run_dir: Path | None) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    for spec in specs:
        path = Path(spec.path).expanduser()
        if not path.is_absolute() and run_dir is not None:
            path = run_dir.parent.parent.parent / path
        exists = path.exists()
        artifacts.append(
            {
                "name": spec.name or path.name,
                "path": str(path),
                "required": spec.required,
                "exists": exists,
                "size": path.stat().st_size if exists and path.is_file() else None,
            }
        )
    return artifacts


def _artifact_diagnostics(artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"code": ERROR_ARTIFACT_MISSING, "message": f"Required artifact missing: {artifact['path']}"}
        for artifact in artifacts
        if artifact.get("required") and not artifact.get("exists")
    ]


def _summary(
    run_id: str,
    ok: bool,
    stage: str,
    started: float,
    steps: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
    diagnostics: list[dict[str, Any]],
    run_dir: Path | None,
) -> dict[str, Any]:
    stdout_tail = _tail("\n".join(step.get("stdout_tail", "") for step in steps))
    stderr_tail = _tail("\n".join(step.get("stderr_tail", "") for step in steps))
    return {
        "ok": ok,
        "run_id": run_id,
        "stage": stage,
        "run_dir": str(run_dir) if run_dir else None,
        "steps": steps,
        "artifacts": artifacts,
        "diagnostics": diagnostics,
        "stdout_tail": stdout_tail,
        "stderr_tail": stderr_tail,
        "elapsed_s": round(time.time() - started, 3),
    }


def _write_manifest(run_dir: Path, summary: dict[str, Any]) -> None:
    manifest = run_dir / "manifest.json"
    manifest.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
