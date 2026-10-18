from __future__ import annotations

import copy
import json
import re
import shutil
import time
import uuid
from importlib import metadata
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from .command_registry import (
    CommandPolicyError,
    command_argv,
    command_output_paths,
    command_specs,
    inspect_commands,
    validate_command_args,
)
from .config import (
    ConfigurationError,
    allowed_roots_info,
    ansys_env,
    default_workdir,
    discover_installation,
    inline_code_allowed,
    known_paths,
    require_allowed_file,
    require_allowed_path,
    safe_workdir,
)
from .process_supervisor import ProcessResult, WorkflowCancelled, current_cancellation_token, run_process
from .pyfluent_operations import (
    COMPATIBILITY_ALIASES,
    PRIMITIVE_OPERATIONS,
    normalize_operation,
)
from .runtime import ansys_python_executable
from .schemas import (
    ArtifactSpec,
    AssertStep,
    BaseWorkflowStep,
    CollectStep,
    CommandStep,
    FileStep,
    PyFluentStep,
    PythonScriptStep,
    WORKFLOW_STEP_ADAPTER,
    WorkbenchRecipe,
    WorkbenchStep,
    MechanicalStep,
    DPFStep,
    WorkflowModel,
    effective_step_id,
)
from .workbench_recipe import (
    WorkbenchRecipeError,
    compile_workbench_recipe,
    load_workbench_recipe,
    read_workbench_result,
    validate_workbench_recipe_paths,
    validate_workbench_result,
)
from .workflow_paths import PathChecks, PathDeniedError, STEP_REF_PATTERN


ERROR_VALIDATION = "VALIDATION_ERROR"
ERROR_CONFIG = "CONFIG_ERROR"
ERROR_PATH_DENIED = "PATH_DENIED"
ERROR_COMMAND_POLICY = "COMMAND_POLICY_ERROR"
ERROR_EXECUTABLE_MISSING = "EXECUTABLE_MISSING"
ERROR_PROCESS_LAUNCH = "PROCESS_LAUNCH_ERROR"
ERROR_PROCESS_TIMEOUT = "PROCESS_TIMEOUT"
ERROR_PROCESS_NONZERO = "PROCESS_NONZERO"
ERROR_PYFLUENT_IMPORT = "PYFLUENT_IMPORT_ERROR"
ERROR_PYFLUENT_OPERATION = "PYFLUENT_OPERATION_ERROR"
ERROR_WORKBENCH_RECIPE = "WORKBENCH_RECIPE_ERROR"
ERROR_INLINE_CODE_DISABLED = "INLINE_CODE_DISABLED"
ERROR_ARTIFACT_MISSING = "ARTIFACT_MISSING"
ERROR_WORKFLOW_CANCELLED = "WORKFLOW_CANCELLED"

STEP_TYPES = ("command", "python_script", "pyfluent", "workbench", "mechanical", "dpf", "file", "assert", "collect")
DEFAULT_RETRY_CODES = {ERROR_PROCESS_LAUNCH, ERROR_PROCESS_TIMEOUT, ERROR_PROCESS_NONZERO}
_VARIABLE_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_STEP_REF_PATTERN = STEP_REF_PATTERN


class WorkflowProblem(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        step_id: str | None = None,
        *,
        severity: Literal["error", "warning"] = "error",
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.step_id = step_id
        self.severity = severity

    def diagnostic(self) -> dict[str, Any]:
        return _diagnostic(self.code, self.message, self.step_id, self.severity)


def inspect_runtime(include_schema: bool = False) -> dict[str, Any]:
    installation = discover_installation()
    roots = allowed_roots_info()
    paths = known_paths(installation.root)
    warnings = [*installation.warnings, *roots.warnings]
    if roots.invalid:
        warnings.append("Invalid allowed roots: " + "; ".join(roots.invalid))
    result = {
        "package_version": __import__("ansys_mcp").__version__,
        "ansys_root": str(installation.root) if installation.root else None,
        "resolved_root": str(installation.root) if installation.root else None,
        "version_tag": installation.version_tag,
        "discovery_source": installation.source,
        "invalid_install_candidates": list(installation.invalid_candidates),
        "allowed_roots": [str(path) for path in roots.roots],
        "allowed_roots_source": roots.source,
        "invalid_allowed_roots": list(roots.invalid),
        "default_workdir": str(default_workdir()),
        "warnings": warnings,
        "paths": {
            name: {"path": str(path), "exists": path.exists(), "is_file": path.is_file()}
            for name, path in paths.items()
        },
        "commands": inspect_commands(),
        "workflow": {
            "version": "1",
            "step_types": list(STEP_TYPES),
            "pyfluent_operations": list(PRIMITIVE_OPERATIONS),
            "pyfluent_compatibility_aliases": list(COMPATIBILITY_ALIASES),
            "inline_code_allowed": inline_code_allowed(),
            "schema_resources": {
                "workflow": "ansys://schema/workflow",
                "workbench_recipe": "ansys://schema/workbench-recipe",
                "mechanical_recipe": "ansys://schema/mechanical-recipe",
                "dpf_recipe": "ansys://schema/dpf-recipe",
            },
        },
        "pyfluent_core": {
            "path": str(paths["pyfluent_core"]),
            "exists": paths["pyfluent_core"].exists(),
        },
    }
    result["dependencies"] = {}
    for package in ("ansys-workbench-core", "ansys-mechanical-core", "ansys-mechanical-stubs", "ansys-dpf-core"):
        try:
            result["dependencies"][package] = {"installed": True, "version": metadata.version(package)}
        except metadata.PackageNotFoundError:
            result["dependencies"][package] = {"installed": False, "version": None}
    from .capabilities import capability_catalog_summary
    result["mechanical_catalog"] = capability_catalog_summary(installation.version_tag)
    from .mechanical import session_summary
    result["mechanical_sessions"] = session_summary()
    if include_schema:
        from .mechanical_schema import MechanicalRecipe
        from .dpf_schema import DPFRecipe
        result["schemas"] = {
            "workflow": WorkflowModel.model_json_schema(),
            "workbench_recipe": WorkbenchRecipe.model_json_schema(),
            "mechanical_recipe": MechanicalRecipe.model_json_schema(),
            "dpf_recipe": DPFRecipe.model_json_schema(),
        }
    return result


def validate_workflow(
    workflow: dict[str, Any] | None = None,
    workflow_path: str | None = None,
    variables: dict[str, Any] | None = None,
    validation_mode: Literal["runtime", "schema"] = "runtime",
) -> dict[str, Any]:
    started = time.monotonic()
    diagnostics: list[dict[str, Any]] = []
    try:
        if validation_mode not in {"runtime", "schema"}:
            raise WorkflowProblem(ERROR_VALIDATION, "validation_mode must be 'runtime' or 'schema'.")
        model = load_workflow(workflow, workflow_path, variables)
        diagnostics.extend(_validate_model(model, validation_mode))
        return {
            "ok": not _has_errors(diagnostics),
            "stage": "validation",
            "status": "complete" if not _has_errors(diagnostics) else "failed",
            "diagnostics": diagnostics,
            "workflow": _workflow_info(model),
            "elapsed_s": round(time.monotonic() - started, 3),
        }
    except WorkflowProblem as exc:
        diagnostics.append(exc.diagnostic())
    except ConfigurationError as exc:
        diagnostics.append(_diagnostic(ERROR_CONFIG, str(exc)))
    except PathDeniedError as exc:
        diagnostics.append(_diagnostic(ERROR_PATH_DENIED, str(exc)))
    except ValidationError as exc:
        diagnostics.append(_diagnostic(ERROR_VALIDATION, str(exc)))
    except (OSError, json.JSONDecodeError) as exc:
        diagnostics.append(_diagnostic(ERROR_VALIDATION, str(exc)))
    except Exception as exc:
        diagnostics.append(_diagnostic(ERROR_VALIDATION, repr(exc)))
    return {
        "ok": False,
        "stage": "validation",
        "status": "failed",
        "diagnostics": diagnostics,
        "elapsed_s": round(time.monotonic() - started, 3),
    }


def run_workflow(
    workflow: dict[str, Any] | None = None,
    workflow_path: str | None = None,
    variables: dict[str, Any] | None = None,
    timeout_seconds: int = 7200,
) -> dict[str, Any]:
    run_id = _run_id()
    started = time.monotonic()
    diagnostics: list[dict[str, Any]] = []
    step_results: list[dict[str, Any]] = []
    run_dir: Path | None = None
    cwd: Path | None = None

    try:
        if timeout_seconds <= 0:
            raise WorkflowProblem(ERROR_VALIDATION, "timeout_seconds must be positive.")
        model = load_workflow(workflow, workflow_path, variables)
        diagnostics.extend(_validate_model(model, "runtime"))
        if _has_errors(diagnostics):
            raise WorkflowProblem(ERROR_VALIDATION, "Workflow validation failed.")
        cwd = safe_workdir(model.workdir, default_workdir())
        run_dir = cwd / ".ansys_mcp" / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        _write_manifest_atomic(
            run_dir,
            _summary(run_id, True, "running", started, step_results, [], diagnostics, run_dir),
        )

        deadline = time.monotonic() + float(timeout_seconds)
        result_context: dict[str, dict[str, Any]] = {}
        for index, original_step in enumerate(model.steps):
            _check_cancellation()
            step_id = effective_step_id(original_step, index)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                result = _error_step(
                    step_id,
                    original_step.type,
                    time.monotonic(),
                    ERROR_PROCESS_TIMEOUT,
                    "Global workflow timeout expired before this step started.",
                )
            else:
                resolved_step = original_step
                step_started = time.monotonic()
                try:
                    resolved_step = _resolve_step(original_step, result_context, step_id, cwd)
                    result = _run_step_with_retry(resolved_step, step_id, cwd, run_dir, deadline)
                except WorkflowProblem as exc:
                    result = _error_step(step_id, original_step.type, step_started, exc.code, exc.message)
                except WorkbenchRecipeError as exc:
                    result = _error_step(step_id, original_step.type, step_started, ERROR_WORKBENCH_RECIPE, exc.message)
                except (ValueError, OSError) as exc:
                    code = ERROR_PATH_DENIED if isinstance(exc, PathDeniedError) else ERROR_VALIDATION
                    result = _error_step(step_id, original_step.type, step_started, code, str(exc))
            step_results.append(result)
            result_context[step_id] = result
            diagnostics.extend(result.get("diagnostics", []))
            _write_manifest_atomic(
                run_dir,
                _summary(run_id, not _has_errors(diagnostics), "running", started, step_results, [], diagnostics, run_dir),
            )
            if result.get("status") == "unknown":
                # The product request is still being observed. Even a workflow
                # configured to continue on error must not advance past it.
                break
            if not result.get("ok", False) and not _should_continue(model, resolved_step if remaining > 0 else original_step):
                break

        unknown = any(step.get("status") == "unknown" for step in step_results)
        # Outputs of later, unexecuted steps do not exist yet. Their artifact
        # references must not replace the active product's unknown outcome.
        artifacts = [] if unknown else _resolve_and_collect_artifacts(model.artifacts, cwd, result_context)
        diagnostics.extend(_artifact_diagnostics(artifacts))
        ok = not _has_errors(diagnostics) and all(step.get("ok", False) for step in step_results)
        if unknown:
            status = "unknown"
        elif ok:
            status = "complete"
        elif _has_code(diagnostics, ERROR_WORKFLOW_CANCELLED):
            status = "cancelled"
        elif _has_code(diagnostics, ERROR_PROCESS_TIMEOUT) or any(step.get("status") in {"timeout", "timed_out"} for step in step_results):
            status = "timeout"
        else:
            status = "failed"
        summary = _summary(run_id, ok, status, started, step_results, artifacts, diagnostics, run_dir)
    except WorkflowCancelled:
        diagnostics.append(_diagnostic(ERROR_WORKFLOW_CANCELLED, "Workflow cancellation requested."))
        summary = _summary(run_id, False, "cancelled", started, step_results, [], diagnostics, run_dir)
    except WorkflowProblem as exc:
        diagnostics.append(exc.diagnostic())
        summary = _summary(run_id, False, "failed", started, step_results, [], diagnostics, run_dir)
    except ConfigurationError as exc:
        diagnostics.append(_diagnostic(ERROR_CONFIG, str(exc)))
        summary = _summary(run_id, False, "failed", started, step_results, [], diagnostics, run_dir)
    except PathDeniedError as exc:
        diagnostics.append(_diagnostic(ERROR_PATH_DENIED, str(exc)))
        summary = _summary(run_id, False, "failed", started, step_results, [], diagnostics, run_dir)
    except ValidationError as exc:
        diagnostics.append(_diagnostic(ERROR_VALIDATION, str(exc)))
        summary = _summary(run_id, False, "failed", started, step_results, [], diagnostics, run_dir)
    except Exception as exc:
        diagnostics.append(_diagnostic(ERROR_VALIDATION, repr(exc)))
        summary = _summary(run_id, False, "failed", started, step_results, [], diagnostics, run_dir)

    if any(step.get("status") == "unknown" for step in step_results):
        # Exception and cancellation aggregation must retain the original
        # observation handles, rather than inventing a product terminal state.
        summary.update(ok=False, status="unknown", stage="unknown")
    if run_dir is not None:
        _write_manifest_atomic(run_dir, summary)
    return summary


def load_workflow(
    workflow: dict[str, Any] | None = None,
    workflow_path: str | None = None,
    variables: dict[str, Any] | None = None,
) -> WorkflowModel:
    if workflow is not None and workflow_path is not None:
        raise WorkflowProblem(ERROR_VALIDATION, "Pass either workflow or workflow_path, not both.")
    if workflow_path is not None:
        path = require_allowed_file(workflow_path, base=default_workdir())
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        source_base = path.parent
    elif workflow is not None:
        payload = copy.deepcopy(workflow)
        source_base = default_workdir()
    else:
        raise WorkflowProblem(ERROR_VALIDATION, "Pass workflow or workflow_path.")
    if not isinstance(payload, dict):
        raise WorkflowProblem(ERROR_VALIDATION, "Workflow must be a JSON object.")

    raw_variables = payload.get("variables", {})
    if not isinstance(raw_variables, dict):
        raise WorkflowProblem(ERROR_VALIDATION, "Workflow variables must be a JSON object.")
    merged_variables = dict(raw_variables)
    merged_variables.update(variables or {})
    resolved_variables = _resolve_variables(merged_variables)
    substituted = _substitute(payload, resolved_variables)
    substituted["variables"] = resolved_variables
    workdir = substituted.get("workdir")
    if workdir:
        candidate = Path(str(workdir)).expanduser()
        if not candidate.is_absolute():
            substituted["workdir"] = str((source_base / candidate).resolve(strict=False))
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
        recipe_file = require_allowed_file(recipe_path, base=default_workdir())
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
                "write": payload.get("write"),
            }
        ],
    }


def _validate_model(model: WorkflowModel, validation_mode: Literal["runtime", "schema"]) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    installation = discover_installation()
    if validation_mode == "runtime" and installation.source == "ANSYS_MCP_ROOT_INVALID":
        invalid = "; ".join(installation.invalid_candidates) or "unknown path"
        raise WorkflowProblem(ERROR_CONFIG, f"Explicit ANSYS_MCP_ROOT is invalid: {invalid}")
    roots = allowed_roots_info()
    if not roots.valid:
        details = "; ".join(roots.invalid) if roots.invalid else "no valid directories"
        raise WorkflowProblem(ERROR_CONFIG, f"Invalid ANSYS_MCP_ALLOWED_ROOTS: {details}")
    diagnostics.extend(_diagnostic(ERROR_CONFIG, warning, severity="warning") for warning in roots.warnings)
    cwd = _allowed_path(model.workdir or default_workdir(), base=default_workdir(), must_exist=False, expect_file=False)
    checks = PathChecks(cwd, validation_mode, allow_references=True)
    seen: set[str] = set()
    for index, step in enumerate(model.steps):
        step_id = effective_step_id(step, index)
        _validate_step_references(step.model_dump(mode="python"), seen, step_id)
        if step.id is None:
            diagnostics.append(
                _diagnostic(ERROR_VALIDATION, f"Step uses implicit id {step_id}.", step_id, "warning")
            )
        diagnostics.extend(_validate_step(step, step_id, checks, seen))
        for path in _produced_paths(step):
            checks.declare(path)
        seen.add(step_id)
    for artifact in model.artifacts:
        _validate_step_references(artifact.model_dump(), seen)
        _validate_path_string(artifact.path, cwd, must_exist=False)
    return diagnostics


def _validate_step_references(value: Any, seen: set[str], step_id: str | None = None) -> None:
    if isinstance(value, dict):
        for child in value.values():
            _validate_step_references(child, seen, step_id)
    elif isinstance(value, list):
        for child in value:
            _validate_step_references(child, seen, step_id)
    elif isinstance(value, str):
        for match in _STEP_REF_PATTERN.finditer(value):
            if match.group(1) not in seen:
                raise WorkflowProblem(ERROR_VALIDATION, f"Unknown or forward step reference: {match.group(1)}", step_id)


def _validate_step(
    step: BaseWorkflowStep, step_id: str, checks: PathChecks, seen: set[str] | None = None
) -> list[dict[str, Any]]:
    cwd, mode = checks.workdir, checks.mode
    diagnostics: list[dict[str, Any]] = []
    if isinstance(step, CommandStep):
        _validate_command_step(step, step_id, cwd, mode, checks)
    elif isinstance(step, PythonScriptStep):
        _validate_python_script_step(step, step_id, cwd, mode, checks)
    elif isinstance(step, PyFluentStep):
        diagnostics.extend(_validate_pyfluent_step(step, step_id, cwd, mode, checks))
    elif isinstance(step, WorkbenchStep):
        diagnostics.extend(_validate_workbench_step(step, step_id, cwd, mode, checks, seen))
    elif isinstance(step, MechanicalStep):
        from .mechanical import validate_mechanical_recipe_paths
        validate_mechanical_recipe_paths(step.recipe, cwd, path_checks=checks)
        if step.retry.max_attempts != 1:
            raise WorkflowProblem(ERROR_VALIDATION, "Mechanical recipes must not be automatically replayed; reconcile product state before retrying.", step_id)
    elif isinstance(step, DPFStep):
        from .dpf import validate_dpf_recipe_paths
        validate_dpf_recipe_paths(step.recipe, cwd, path_checks=checks)
        if step.retry.max_attempts != 1:
            raise WorkflowProblem(ERROR_VALIDATION, "DPF recipes may write artifacts and must not be automatically replayed.", step_id)
    elif isinstance(step, FileStep):
        _validate_file_step(step, checks)
    elif isinstance(step, AssertStep):
        checks.resolve(step.path)
    elif isinstance(step, CollectStep):
        for artifact in step.artifacts:
            checks.resolve(artifact.path)
    for artifact in step.produces:
        checks.resolve(artifact.path)
    return diagnostics


def _produced_paths(step: BaseWorkflowStep) -> list[str]:
    paths = [artifact.path for artifact in step.produces]
    if isinstance(step, FileStep) and step.action in {"write_text", "copy"}:
        paths.append(str(step.target or step.path))
    elif isinstance(step, CommandStep):
        paths.extend(command_output_paths(step.command_id, step.args))
    elif isinstance(step, WorkbenchStep) and step.recipe and step.recipe.project.save_path:
        paths.append(step.recipe.project.save_path)
    elif isinstance(step, MechanicalStep):
        from .mechanical import mechanical_produced_paths
        paths.extend(mechanical_produced_paths(step.recipe))
    elif isinstance(step, DPFStep):
        from .dpf import dpf_produced_paths
        paths.extend(dpf_produced_paths(step.recipe))
    elif isinstance(step, PyFluentStep):
        if step.write:
            paths.extend(step.write.model_dump(exclude_none=True).values())
        for operation in step.operations:
            if operation.op == "write":
                paths.extend(value for key, value in operation.model_dump(exclude_none=True).items() if key in {"case", "case_data"})
    return paths


def _validate_command_step(
    step: CommandStep,
    step_id: str,
    cwd: Path,
    validation_mode: Literal["runtime", "schema"],
    checks: PathChecks,
) -> None:
    specs = command_specs()
    if step.command_id not in specs:
        raise WorkflowProblem(ERROR_VALIDATION, f"Unknown command_id: {step.command_id}", step_id)
    spec = specs[step.command_id]
    if step.detach and not spec.supports_detach:
        raise WorkflowProblem(ERROR_COMMAND_POLICY, f"{step.command_id} does not support detach.", step_id)
    try:
        validate_command_args(step.command_id, step.args, workdir=cwd, validation_mode=validation_mode, path_checks=checks)
    except (CommandPolicyError, ValueError, ConfigurationError) as exc:
        code = ERROR_CONFIG if isinstance(exc, ConfigurationError) else ERROR_COMMAND_POLICY
        raise WorkflowProblem(code, str(exc), step_id) from exc
    # Invalid actions must be rejected even on hosts with no Ansys installation.
    # Availability is a runtime check, after the installation-independent policy.
    if validation_mode == "runtime" and not spec.executable.is_file():
        raise WorkflowProblem(ERROR_EXECUTABLE_MISSING, f"Executable not found: {spec.executable}", step_id)


def _validate_python_script_step(
    step: PythonScriptStep,
    step_id: str,
    cwd: Path,
    validation_mode: Literal["runtime", "schema"],
    checks: PathChecks,
) -> None:
    try:
        validate_command_args(
            "ansys_python",
            [step.script_path, *step.arguments],
            workdir=cwd,
            validation_mode=validation_mode,
            path_checks=checks,
        )
    except (CommandPolicyError, ValueError, ConfigurationError) as exc:
        raise WorkflowProblem(ERROR_COMMAND_POLICY, str(exc), step_id) from exc
    if validation_mode == "runtime" and not ansys_python_executable().is_file():
        raise WorkflowProblem(ERROR_EXECUTABLE_MISSING, f"Ansys Python not found: {ansys_python_executable()}", step_id)


def _validate_pyfluent_step(
    step: PyFluentStep,
    step_id: str,
    cwd: Path,
    validation_mode: Literal["runtime", "schema"],
    checks: PathChecks,
) -> list[dict[str, Any]]:
    if validation_mode == "runtime" and not known_paths()["pyfluent_core"].is_dir():
        raise WorkflowProblem(ERROR_PYFLUENT_IMPORT, f"PyFluent core path not found: {known_paths()['pyfluent_core']}", step_id)
    diagnostics: list[dict[str, Any]] = []
    read = step.read
    if isinstance(read, str):
        checks.resolve(read, input_file=True)
    elif read is not None:
        read_data = read.model_dump(exclude_none=True)
        source = read_data.get("case_data") or read_data.get("case") or read_data.get("case_file")
        if source:
            checks.resolve(source, input_file=True)
    if step.write:
        for value in step.write.model_dump(exclude_none=True).values():
            checks.resolve(str(value))
    if "cwd" in step.launch:
        checks.resolve(str(step.launch["cwd"]), expect_file=False)
    for operation in step.operations:
        data = operation.model_dump(exclude_none=True)
        normalized, warning = normalize_operation(data)
        if warning:
            diagnostics.append(_diagnostic(ERROR_VALIDATION, warning, step_id, "warning"))
        if normalized["op"] == "read":
            source = normalized.get("case_data") or normalized.get("case") or normalized.get("case_file")
            if source:
                checks.resolve(str(source), input_file=True)
        elif normalized["op"] == "write":
            for key in ("case", "case_data"):
                if normalized.get(key):
                    checks.resolve(str(normalized[key]))
    return diagnostics


def _validate_workbench_step(
    step: WorkbenchStep,
    step_id: str,
    cwd: Path,
    validation_mode: Literal["runtime", "schema"],
    checks: PathChecks,
    seen: set[str] | None,
) -> list[dict[str, Any]]:
    executable_key = "runwb2_batch" if step.mode == "batch" else "runwb2"
    executable = command_specs()[executable_key].executable
    if validation_mode == "runtime" and not executable.is_file():
        raise WorkflowProblem(ERROR_EXECUTABLE_MISSING, f"Workbench executable not found: {executable}", step_id)
    try:
        if step.recipe_path:
            path = checks.resolve(step.recipe_path, input_file=True, expect_file=True)
            if path is None or path in checks.produced:
                return [_diagnostic(ERROR_VALIDATION, "Generated Workbench recipe will be validated before execution.", step_id, "warning")]
        recipe = load_workbench_recipe(recipe=step.recipe, recipe_path=step.recipe_path, workdir=cwd)
        if seen is not None:
            _validate_step_references(recipe.model_dump(mode="python"), seen, step_id)
        validate_workbench_recipe_paths(recipe, cwd, path_checks=checks)
    except WorkbenchRecipeError as exc:
        code = ERROR_INLINE_CODE_DISABLED if "Inline" in exc.message else ERROR_WORKBENCH_RECIPE
        raise WorkflowProblem(code, exc.message, step_id) from exc
    except (ValueError, ConfigurationError) as exc:
        code = ERROR_CONFIG if isinstance(exc, ConfigurationError) else ERROR_PATH_DENIED
        raise WorkflowProblem(code, str(exc), step_id) from exc
    return []


def _validate_file_step(step: FileStep, checks: PathChecks) -> None:
    if step.action in {"exists", "read_text"}:
        checks.resolve(str(step.path), input_file=step.action == "read_text")
    elif step.action == "write_text":
        checks.resolve(str(step.target or step.path))
    elif step.action == "copy":
        checks.resolve(str(step.source), input_file=True)
        checks.resolve(str(step.target))


def _run_step_with_retry(
    step: BaseWorkflowStep,
    step_id: str,
    cwd: Path,
    run_dir: Path,
    deadline: float,
) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    retry_codes = set(step.retry.retry_on_codes) or DEFAULT_RETRY_CODES
    for attempt in range(1, step.retry.max_attempts + 1):
        _check_cancellation()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            result = _error_step(
                step_id,
                step.type,
                time.monotonic(),
                ERROR_PROCESS_TIMEOUT,
                "Global workflow timeout expired.",
            )
        else:
            step_timeout = min(float(step.timeout_seconds or remaining), remaining)
            result = _run_step_once(step, step_id, cwd, run_dir, step_timeout, attempt)
            if result.get("ok") and step.produces:
                artifacts = _collect_artifacts(step.produces, cwd)
                result["outputs"]["artifacts"] = artifacts
                missing = _artifact_diagnostics(artifacts, step_id)
                if missing:
                    result.update(ok=False, status="failed")
                    result["diagnostics"].extend(missing)
        attempts.append(
            {
                "attempt": attempt,
                "ok": result.get("ok", False),
                "elapsed_s": result.get("elapsed_s", 0),
                "diagnostics": result.get("diagnostics", []),
            }
        )
        if result.get("ok", False):
            break
        code = _first_error_code(result.get("diagnostics", []))
        if attempt >= step.retry.max_attempts or code not in retry_codes:
            break
        delay = min(step.retry.backoff_seconds * (2 ** (attempt - 1)), max(0, deadline - time.monotonic()))
        if delay > 0:
            time.sleep(delay)
    result["attempt_count"] = len(attempts)
    if len(attempts) > 1:
        result["attempts"] = attempts
    return result


def _run_step_once(
    step: BaseWorkflowStep,
    step_id: str,
    cwd: Path,
    run_dir: Path,
    timeout_seconds: float,
    attempt: int,
) -> dict[str, Any]:
    started = time.monotonic()
    try:
        _validate_step(step, step_id, PathChecks(cwd))
        if isinstance(step, CommandStep):
            return _run_command_step(step, step_id, cwd, run_dir, timeout_seconds, attempt, started)
        if isinstance(step, PythonScriptStep):
            return _run_python_script_step(step, step_id, cwd, run_dir, timeout_seconds, attempt, started)
        if isinstance(step, PyFluentStep):
            return _run_pyfluent_step(step, step_id, cwd, run_dir, timeout_seconds, attempt, started)
        if isinstance(step, WorkbenchStep):
            return _run_workbench_step(step, step_id, cwd, run_dir, timeout_seconds, attempt, started)
        if isinstance(step, (MechanicalStep, DPFStep)):
            if isinstance(step, MechanicalStep):
                from .mechanical import run_mechanical_recipe as run_domain_recipe
            else:
                from .dpf import run_dpf_recipe as run_domain_recipe
            domain = run_domain_recipe(step.recipe, workdir=cwd, run_dir=run_dir, timeout_seconds=timeout_seconds)
            result = _ok_step(step_id, step.type, started, {"outputs": domain.get("outputs", {})})
            result.update(domain)
            if result.get("status") == "timed_out":
                result["status"] = "timeout"
            result.update(id=step_id, type=step.type, elapsed_s=round(time.monotonic()-started, 3))
            result.setdefault("diagnostics", [])
            if not result.get("ok", False) and not result["diagnostics"]:
                result["diagnostics"] = [_diagnostic(ERROR_VALIDATION, "Product recipe failed; inspect operation results.", step_id)]
            return result
        if isinstance(step, FileStep):
            return _run_file_step(step, step_id, cwd, started)
        if isinstance(step, AssertStep):
            return _run_assert_step(step, step_id, cwd, started)
        if isinstance(step, CollectStep):
            artifacts = _collect_artifacts(step.artifacts, cwd)
            diagnostics = _artifact_diagnostics(artifacts, step_id)
            result = _ok_step(step_id, step.type, started, {"outputs": {"artifacts": artifacts}, "artifacts": artifacts})
            if diagnostics:
                result["ok"] = False
                result["diagnostics"] = diagnostics
            return result
        raise WorkflowProblem(ERROR_VALIDATION, f"Unsupported step type: {step.type}", step_id)
    except WorkflowCancelled:
        result = _error_step(step_id, step.type, started, ERROR_WORKFLOW_CANCELLED, "Workflow cancellation requested.")
        result["status"] = "cancelled"
        return result
    except WorkflowProblem as exc:
        return _error_step(step_id, step.type, started, exc.code, exc.message)
    except ConfigurationError as exc:
        return _error_step(step_id, step.type, started, ERROR_CONFIG, str(exc))
    except PathDeniedError as exc:
        return _error_step(step_id, step.type, started, ERROR_PATH_DENIED, str(exc))
    except WorkbenchRecipeError as exc:
        return _error_step(step_id, step.type, started, ERROR_WORKBENCH_RECIPE, exc.message)
    except FileNotFoundError as exc:
        return _error_step(step_id, step.type, started, ERROR_PROCESS_LAUNCH, str(exc))
    except OSError as exc:
        return _error_step(step_id, step.type, started, ERROR_PROCESS_LAUNCH, str(exc))
    except Exception as exc:
        return _error_step(step_id, step.type, started, ERROR_VALIDATION, repr(exc))


def _run_command_step(
    step: CommandStep,
    step_id: str,
    cwd: Path,
    run_dir: Path,
    timeout_seconds: float,
    attempt: int,
    started: float,
) -> dict[str, Any]:
    args = validate_command_args(step.command_id, step.args, workdir=cwd, validation_mode="runtime")
    argv = command_argv(step.command_id, args)
    process = run_process(
        argv,
        cwd=cwd,
        env=_run_environment(cwd, run_dir),
        timeout_seconds=timeout_seconds,
        run_dir=run_dir,
        log_name=f"{step_id}.attempt-{attempt}",
        detached=step.detach,
    )
    return _process_step_result(step_id, step.type, step.command_id, argv, cwd, process, started)


def _run_python_script_step(
    step: PythonScriptStep,
    step_id: str,
    cwd: Path,
    run_dir: Path,
    timeout_seconds: float,
    attempt: int,
    started: float,
) -> dict[str, Any]:
    script = require_allowed_file(step.script_path, base=cwd)
    args = validate_command_args(
        "ansys_python", [str(script), *step.arguments], workdir=cwd, validation_mode="runtime"
    )
    argv = command_argv("ansys_python", args)
    process = run_process(
        argv,
        cwd=cwd,
        env=_run_environment(cwd, run_dir),
        timeout_seconds=timeout_seconds,
        run_dir=run_dir,
        log_name=f"{step_id}.attempt-{attempt}",
    )
    return _process_step_result(step_id, step.type, "ansys_python", argv, cwd, process, started)


def _run_pyfluent_step(
    step: PyFluentStep,
    step_id: str,
    cwd: Path,
    run_dir: Path,
    timeout_seconds: float,
    attempt: int,
    started: float,
) -> dict[str, Any]:
    operations: list[dict[str, Any]] = []
    for operation in step.operations:
        normalized, _ = normalize_operation(operation.model_dump(exclude_none=True))
        operations.append(normalized)
    result_file = run_dir / f"{step_id}.attempt-{attempt}.pyfluent-result.json"
    payload = {
        "launch": step.launch,
        "read": step.read if isinstance(step.read, str) else (step.read.model_dump(exclude_none=True) if step.read else None),
        "operations": operations,
        "write": step.write.model_dump(exclude_none=True) if step.write else None,
        "workdir": str(cwd),
        "result_path": str(result_file),
    }
    recipe_file = run_dir / f"{step_id}.attempt-{attempt}.pyfluent.json"
    recipe_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    runner = Path(__file__).with_name("pyfluent_recipe_runner.py")
    argv = [str(ansys_python_executable()), str(runner), str(recipe_file)]
    process = run_process(
        argv,
        cwd=cwd,
        env=_run_environment(cwd, run_dir),
        timeout_seconds=timeout_seconds,
        run_dir=run_dir,
        log_name=f"{step_id}.attempt-{attempt}",
    )
    result = _process_step_result(step_id, step.type, "pyfluent", argv, cwd, process, started)
    runner_result = _read_json_file(result_file)
    result["recipe_file"] = str(recipe_file)
    result["outputs"].update({"recipe_file": str(recipe_file), "runner_result": runner_result})
    if not result["ok"] and not process.timed_out:
        code = ERROR_PYFLUENT_IMPORT if _result_mentions_import_error(runner_result, process) else ERROR_PYFLUENT_OPERATION
        result["diagnostics"] = [_diagnostic(code, "PyFluent operation failed.", step_id)]
    return result


def _run_workbench_step(
    step: WorkbenchStep,
    step_id: str,
    cwd: Path,
    run_dir: Path,
    timeout_seconds: float,
    attempt: int,
    started: float,
) -> dict[str, Any]:
    recipe = load_workbench_recipe(recipe=step.recipe, recipe_path=step.recipe_path, workdir=cwd)
    prepared = compile_workbench_recipe(recipe, workdir=cwd, run_dir=run_dir, step_id=f"{step_id}.attempt-{attempt}")
    command_id = "runwb2_batch" if step.mode == "batch" else "runwb2"
    args = ["-B", "-R", str(prepared.journal_path)] if step.mode == "batch" else ["-I", "-X", "-R", str(prepared.journal_path)]
    validated_args = validate_command_args(command_id, args, workdir=cwd, validation_mode="runtime")
    argv = command_argv(command_id, validated_args)
    process = run_process(
        argv,
        cwd=cwd,
        env=_run_environment(cwd, run_dir),
        timeout_seconds=timeout_seconds,
        run_dir=run_dir,
        log_name=f"{step_id}.attempt-{attempt}",
    )
    result = _process_step_result(step_id, step.type, command_id, argv, cwd, process, started)
    workbench_result = read_workbench_result(prepared.result_path)
    result["outputs"].update(
        {
            "journal_path": str(prepared.journal_path),
            "result_path": str(prepared.result_path),
            "generated_scripts": [str(path) for path in prepared.generated_scripts],
            "workbench_result": workbench_result,
        }
    )
    result["journal_path"] = str(prepared.journal_path)
    if not process.timed_out:
        problem = validate_workbench_result(workbench_result, recipe)
        if problem:
            result.update(ok=False, status="failed")
            result["diagnostics"].append(_diagnostic(ERROR_WORKBENCH_RECIPE, problem, step_id))
        else:
            result["outputs"]["values"] = workbench_result["outputs"]
    return result


def _run_file_step(step: FileStep, step_id: str, cwd: Path, started: float) -> dict[str, Any]:
    if step.action == "exists":
        path = _runtime_path(str(step.path), cwd, must_exist=False)
        outputs = {"path": str(path), "exists": path.exists()}
    elif step.action == "read_text":
        path = _runtime_path(str(step.path), cwd, must_exist=True)
        outputs = {"path": str(path), "text": path.read_text(encoding="utf-8")}
    elif step.action == "write_text":
        path = _runtime_path(str(step.target or step.path), cwd, must_exist=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(step.content or "", encoding="utf-8")
        outputs = {"path": str(path), "bytes": path.stat().st_size}
    elif step.action == "copy":
        source = _runtime_path(str(step.source), cwd, must_exist=True)
        target = _runtime_path(str(step.target), cwd, must_exist=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        outputs = {"source": str(source), "target": str(target), "bytes": target.stat().st_size}
    else:  # pragma: no cover - guarded by schema
        raise WorkflowProblem(ERROR_VALIDATION, f"Unsupported file action: {step.action}", step_id)
    return _ok_step(step_id, step.type, started, {"outputs": outputs, **outputs})


def _run_environment(cwd: Path, run_dir: Path) -> dict[str, str]:
    env = ansys_env()
    env["ANSYS_MCP_WORKDIR"] = str(cwd)
    env["ANSYS_MCP_RUN_DIR"] = str(run_dir)
    return env


def _check_cancellation() -> None:
    token = current_cancellation_token()
    if token is not None:
        token.raise_if_cancelled()


def worker_dispatch(operation: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Authenticated execution-worker entry point for an explicit workflow.

    The private worker supplies the same allowed-root snapshot. There is no
    automatic escalation or replay through this handler.
    """
    if operation != "run_workflow":
        raise ValueError("Unsupported workflow worker operation.")
    if not isinstance(payload, dict) or set(payload) - {"workflow", "variables", "timeout_seconds"}:
        raise ValueError("Invalid workflow worker request.")
    return run_workflow(workflow=payload.get("workflow"), variables=payload.get("variables"), timeout_seconds=payload.get("timeout_seconds", 7200))


def _run_assert_step(step: AssertStep, step_id: str, cwd: Path, started: float) -> dict[str, Any]:
    path = _runtime_path(step.path, cwd, must_exist=False)
    actual = path.exists()
    if actual != step.exists:
        raise WorkflowProblem(
            ERROR_VALIDATION,
            f"Assertion failed for {path}: exists={actual}, expected={step.exists}.",
            step_id,
        )
    outputs = {"path": str(path), "exists": actual}
    return _ok_step(step_id, step.type, started, {"outputs": outputs, **outputs})


def _resolve_step(
    step: BaseWorkflowStep,
    results: dict[str, dict[str, Any]],
    step_id: str,
    cwd: Path,
) -> BaseWorkflowStep:
    payload = _resolve_runtime_references(step.model_dump(mode="python"), results, step_id)
    if isinstance(step, WorkbenchStep) and payload.get("recipe_path"):
        recipe = load_workbench_recipe(recipe=None, recipe_path=payload["recipe_path"], workdir=cwd)
        payload["recipe"] = _resolve_runtime_references(recipe.model_dump(mode="python"), results, step_id)
        payload["recipe_path"] = None
    try:
        return WORKFLOW_STEP_ADAPTER.validate_python(payload)
    except ValidationError as exc:
        raise WorkflowProblem(ERROR_VALIDATION, f"Resolved step is invalid: {exc}", step_id) from exc


def _resolve_runtime_references(value: Any, results: dict[str, dict[str, Any]], step_id: str | None = None) -> Any:
    if isinstance(value, dict):
        return {key: _resolve_runtime_references(child, results, step_id) for key, child in value.items()}
    if isinstance(value, list):
        return [_resolve_runtime_references(child, results, step_id) for child in value]
    if not isinstance(value, str):
        return value
    full = _STEP_REF_PATTERN.fullmatch(value)
    if full:
        return _lookup_step_output(results, full.group(1), full.group(2), step_id)

    def replace(match: re.Match[str]) -> str:
        return str(_lookup_step_output(results, match.group(1), match.group(2), step_id))

    return _STEP_REF_PATTERN.sub(replace, value)


def _lookup_step_output(
    results: dict[str, dict[str, Any]],
    referenced_step: str,
    output_path: str | None,
    current_step: str | None,
) -> Any:
    if referenced_step not in results:
        raise WorkflowProblem(
            ERROR_VALIDATION,
            f"Unknown or not-yet-completed step output: {referenced_step}",
            current_step,
        )
    current: Any = results[referenced_step].get("outputs", {})
    if not output_path:
        return current
    for part in output_path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            raise WorkflowProblem(
                ERROR_VALIDATION,
                f"Step output does not exist: steps.{referenced_step}.outputs.{output_path}",
                current_step,
            )
    return current


def _resolve_variables(variables: dict[str, Any]) -> dict[str, Any]:
    resolved: dict[str, Any] = {}
    resolving: set[str] = set()

    def resolve(name: str) -> Any:
        if name in resolved:
            return resolved[name]
        if name not in variables:
            raise WorkflowProblem(ERROR_VALIDATION, f"Unknown workflow variable: {name}")
        if name in resolving:
            raise WorkflowProblem(ERROR_VALIDATION, f"Cyclic workflow variable reference: {name}")
        resolving.add(name)

        def variable_replace(match: re.Match[str]) -> str:
            child = match.group(1) or match.group(2)
            return str(resolve(child))

        value = variables[name]
        if isinstance(value, str):
            full = _VARIABLE_PATTERN.fullmatch(value)
            if full:
                value = resolve(full.group(1) or full.group(2))
            else:
                value = _VARIABLE_PATTERN.sub(variable_replace, value)
        else:
            value = _substitute(value, {key: resolve(key) for key in _referenced_variables(value)})
        resolving.remove(name)
        resolved[name] = value
        return value

    for variable_name in variables:
        resolve(variable_name)
    return resolved


def _referenced_variables(value: Any) -> set[str]:
    if isinstance(value, dict):
        found: set[str] = set()
        for child in value.values():
            found.update(_referenced_variables(child))
        return found
    if isinstance(value, list):
        found = set()
        for child in value:
            found.update(_referenced_variables(child))
        return found
    if isinstance(value, str):
        return {match.group(1) or match.group(2) for match in _VARIABLE_PATTERN.finditer(value)}
    return set()


def _substitute(value: Any, variables: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        return {key: _substitute(child, variables) for key, child in value.items()}
    if isinstance(value, list):
        return [_substitute(child, variables) for child in value]
    if not isinstance(value, str):
        return value
    full = _VARIABLE_PATTERN.fullmatch(value)
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

    return _VARIABLE_PATTERN.sub(replace, value)


def _validate_path_string(value: str, cwd: Path, must_exist: bool) -> Path | None:
    if _STEP_REF_PATTERN.search(value):
        return None
    return _allowed_path(value, base=cwd, must_exist=must_exist)


def _allowed_path(
    value: str | Path,
    *,
    base: Path,
    must_exist: bool,
    expect_file: bool | None = None,
) -> Path:
    try:
        return require_allowed_path(value, base=base, must_exist=must_exist, expect_file=expect_file)
    except ConfigurationError:
        raise
    except ValueError as exc:
        raise WorkflowProblem(ERROR_PATH_DENIED, str(exc)) from exc


def _runtime_path(value: str, cwd: Path, *, must_exist: bool) -> Path:
    return _allowed_path(value, base=cwd, must_exist=must_exist)


def _workflow_info(model: WorkflowModel) -> dict[str, Any]:
    return {
        "version": str(model.version),
        "workdir": str(_allowed_path(model.workdir or default_workdir(), base=default_workdir(), must_exist=False)),
        "step_count": len(model.steps),
        "steps": [
            {"id": effective_step_id(step, index), "type": step.type}
            for index, step in enumerate(model.steps)
        ],
        "artifact_count": len(model.artifacts),
    }


def _should_continue(model: WorkflowModel, step: BaseWorkflowStep) -> bool:
    return model.continue_on_error if step.continue_on_error is None else bool(step.continue_on_error)


def _run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]


def _ok_step(
    step_id: str,
    step_type: str,
    started: float,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = {
        "id": step_id,
        "type": step_type,
        "ok": True,
        "status": "complete",
        "elapsed_s": round(time.monotonic() - started, 3),
        "outputs": {},
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
        "status": "timeout" if code == ERROR_PROCESS_TIMEOUT else "failed",
        "elapsed_s": round(time.monotonic() - started, 3),
        "outputs": {},
        "stdout_tail": stdout_tail,
        "stderr_tail": stderr_tail,
        "diagnostics": [_diagnostic(code, message, step_id)],
    }


def _process_step_result(
    step_id: str,
    step_type: str,
    command_id: str,
    argv: list[str],
    cwd: Path,
    process: ProcessResult,
    started: float,
) -> dict[str, Any]:
    outputs = {
        "pid": process.pid,
        "returncode": process.returncode,
        "stdout_path": str(process.stdout_path),
        "stderr_path": str(process.stderr_path),
        "detached": process.detached,
    }
    result = {
        "id": step_id,
        "type": step_type,
        "ok": process.detached or (not process.timed_out and not process.cancelled and process.returncode == 0),
        "status": "launched" if process.detached else ("cancelled" if process.cancelled else ("timeout" if process.timed_out else "complete")),
        "returncode": process.returncode,
        "pid": process.pid,
        "command_id": command_id,
        "command": argv,
        "workdir": str(cwd),
        "stdout_path": str(process.stdout_path),
        "stderr_path": str(process.stderr_path),
        "stdout_tail": process.stdout_tail,
        "stderr_tail": process.stderr_tail,
        "elapsed_s": round(time.monotonic() - started, 3),
        "outputs": outputs,
        "diagnostics": [],
    }
    if process.cancelled:
        result["ok"] = False
        result["diagnostics"].append(_diagnostic(ERROR_WORKFLOW_CANCELLED, "Workflow cancellation requested.", step_id))
    elif process.timed_out:
        result["ok"] = False
        result["diagnostics"].append(
            _diagnostic(ERROR_PROCESS_TIMEOUT, "Process exceeded its allowed timeout.", step_id)
        )
    elif not process.detached and process.returncode != 0:
        result["ok"] = False
        result["status"] = "failed"
        result["diagnostics"].append(
            _diagnostic(ERROR_PROCESS_NONZERO, f"Process exited with return code {process.returncode}.", step_id)
        )
    if process.termination_error:
        result["diagnostics"].append(
            _diagnostic(
                ERROR_PROCESS_TIMEOUT,
                f"Process-tree termination reported: {process.termination_error}",
                step_id,
                "warning",
            )
        )
    return result


def _collect_artifacts(specs: list[ArtifactSpec], cwd: Path) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    for spec in specs:
        path = _runtime_path(spec.path, cwd, must_exist=False)
        exists = path.exists()
        artifacts.append(
            {
                "name": spec.name or path.name,
                "path": str(path),
                "required": spec.required,
                "exists": exists,
                "size": path.stat().st_size if exists and path.is_file() else None,
                "modified_s": path.stat().st_mtime if exists else None,
            }
        )
    return artifacts


def _resolve_and_collect_artifacts(
    specs: list[ArtifactSpec],
    cwd: Path,
    results: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    resolved: list[ArtifactSpec] = []
    for spec in specs:
        payload = _resolve_runtime_references(spec.model_dump(mode="python"), results)
        resolved.append(ArtifactSpec.model_validate(payload))
    return _collect_artifacts(resolved, cwd)


def _artifact_diagnostics(
    artifacts: list[dict[str, Any]],
    step_id: str | None = None,
) -> list[dict[str, Any]]:
    return [
        _diagnostic(ERROR_ARTIFACT_MISSING, f"Required artifact missing: {artifact['path']}", step_id)
        for artifact in artifacts
        if artifact.get("required") and not artifact.get("exists")
    ]


def _summary(
    run_id: str,
    ok: bool,
    status: str,
    started: float,
    steps: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
    diagnostics: list[dict[str, Any]],
    run_dir: Path | None,
) -> dict[str, Any]:
    return {
        "ok": ok,
        "run_id": run_id,
        "stage": status,
        "status": status,
        "run_dir": str(run_dir) if run_dir else None,
        "steps": steps,
        "artifacts": artifacts,
        "diagnostics": diagnostics,
        "stdout_tail": _tail("\n".join(step.get("stdout_tail", "") for step in steps)),
        "stderr_tail": _tail("\n".join(step.get("stderr_tail", "") for step in steps)),
        "elapsed_s": round(time.monotonic() - started, 3),
    }


def _write_manifest_atomic(run_dir: Path, summary: dict[str, Any]) -> None:
    manifest = run_dir / "manifest.json"
    temporary = run_dir / "manifest.json.tmp"
    temporary.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    temporary.replace(manifest)
    token = current_cancellation_token()
    observer = getattr(token, "on_progress", None) if token is not None else None
    if observer is not None:
        try:
            observer(summary)
        except Exception:
            # Reporting failure must not turn a completed model mutation into a
            # failed operation that a caller might replay.
            pass


def _diagnostic(
    code: str,
    message: str,
    step_id: str | None = None,
    severity: Literal["error", "warning"] = "error",
) -> dict[str, Any]:
    data: dict[str, Any] = {"code": code, "message": message, "severity": severity}
    if step_id:
        data["step_id"] = step_id
    return data


def _has_errors(diagnostics: list[dict[str, Any]]) -> bool:
    return any(item.get("severity", "error") == "error" for item in diagnostics)


def _has_code(diagnostics: list[dict[str, Any]], code: str) -> bool:
    return any(item.get("code") == code and item.get("severity", "error") == "error" for item in diagnostics)


def _first_error_code(diagnostics: list[dict[str, Any]]) -> str | None:
    for item in diagnostics:
        if item.get("severity", "error") == "error":
            return str(item.get("code"))
    return None


def _tail(value: str, limit: int = 20000) -> str:
    return value[-limit:] if value else ""


def _read_json_file(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _result_mentions_import_error(result: dict[str, Any] | None, process: ProcessResult) -> bool:
    text = json.dumps(result or {}, default=str) + process.stdout_tail + process.stderr_tail
    return "PYFLUENT_IMPORT_ERROR" in text or "No module named" in text
