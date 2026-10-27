"""Supervised CFX pre/solve/post execution with verified completion.

CFX launchers can hand off to another process and exit early: the supervisor
then sees a zero exit code while the real solver has not produced a result.
This module therefore never treats the exit code alone as success. It verifies
declared outputs were written during this run (freshness), and for solves it
reads the solver log for the completion marker, residual history and end time.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from .cfx_monitor import read_cfx_log, summarize
from .cfx_schema import CFXPostOperation, CFXPreOperation, CFXRecipe, CFXSolveOperation
from .command_registry import command_specs
from .config import ansys_env, require_allowed_path
from .process_supervisor import ProcessResult, run_process
from .workflow_paths import PathChecks

Runner = Callable[..., ProcessResult]

ERROR_OUTPUT_MISSING = "CFX_OUTPUT_MISSING"
ERROR_OUTPUT_STALE = "CFX_OUTPUT_STALE"
ERROR_RESULT_MISSING = "CFX_RESULT_MISSING"
ERROR_RESULT_STALE = "CFX_RESULT_STALE"
ERROR_LOG_MISSING = "CFX_LOG_MISSING"
ERROR_SOLVER_INCOMPLETE = "CFX_SOLVER_INCOMPLETE"
ERROR_OUTCOME_UNVERIFIED = "CFX_OUTCOME_UNVERIFIED"
ERROR_PROCESS_NONZERO = "CFX_PROCESS_NONZERO"


def _paths(recipe: CFXRecipe, workdir: Path) -> dict[str, Any]:
    def resolve(value: str | None, *, input_file: bool) -> Path | None:
        if value is None:
            return None
        return require_allowed_path(value, base=workdir, must_exist=input_file, expect_file=True)

    op = recipe.operation
    if isinstance(op, CFXPreOperation):
        return {"script": resolve(op.script, input_file=True), "def_path": resolve(op.def_path, input_file=False)}
    if isinstance(op, CFXSolveOperation):
        return {"def_path": resolve(op.def_path, input_file=True),
                "res_path": resolve(op.res_path, input_file=False),
                "out_path": resolve(op.out_path, input_file=False),
                "continue_from": resolve(op.continue_from, input_file=True),
                "initial_file": resolve(op.initial_file, input_file=True),
                "ccl_path": resolve(op.ccl_path, input_file=True)}
    return {"script": resolve(op.script, input_file=True),
            "result_file": resolve(op.result_file, input_file=True),
            "outputs": [(item.name, resolve(item.path, input_file=False)) for item in op.outputs]}


def validate_cfx_recipe_paths(recipe: CFXRecipe, workdir: Path, *, path_checks: PathChecks | None = None) -> None:
    """Check every declared path against allowed roots and its input/output role."""
    checks = path_checks or PathChecks(workdir)
    op = recipe.operation
    if isinstance(op, CFXPreOperation):
        checks.resolve(op.script, input_file=True, expect_file=True)
        if op.def_path:
            checks.resolve(op.def_path, expect_file=True)
        return
    if isinstance(op, CFXSolveOperation):
        checks.resolve(op.def_path, input_file=True, expect_file=True)
        for value in (op.res_path, op.out_path):
            if value:
                checks.resolve(value, expect_file=True)
        for value in (op.continue_from, op.initial_file, op.ccl_path):
            if value:
                checks.resolve(value, input_file=True, expect_file=True)
        return
    checks.resolve(op.script, input_file=True, expect_file=True)
    if op.result_file:
        checks.resolve(op.result_file, input_file=True, expect_file=True)
    for item in op.outputs:
        checks.resolve(item.path)


def cfx_produced_paths(recipe: CFXRecipe) -> list[str]:
    op = recipe.operation
    if isinstance(op, CFXPreOperation):
        return [op.def_path] if op.def_path else []
    if isinstance(op, CFXSolveOperation):
        return [op.res_path] if op.res_path else []
    return [item.path for item in op.outputs]


def _argv(recipe: CFXRecipe, paths: dict[str, Any]) -> tuple[str, Path, list[str]]:
    specs = command_specs()
    op = recipe.operation
    if isinstance(op, CFXPreOperation):
        spec, argv = specs["cfx_pre"], ["-batch", str(paths["script"])]
    elif isinstance(op, CFXSolveOperation):
        spec = specs["cfx_solve"]
        argv = ["-def", str(paths["def_path"]), "-double" if op.precision == "double" else "-single"]
        if op.partitions > 1:
            argv.extend(["-part", str(op.partitions), "-par-local"])
        if op.start_method:
            argv.extend(["-start-method", op.start_method])
        if op.run_name:
            argv.extend(["-name", op.run_name])
        if op.continue_from:
            argv.extend(["-continue-from-file", str(paths["continue_from"])])
        if op.initial_file:
            argv.extend(["-initial", str(paths["initial_file"])])
        if op.ccl_path:
            argv.extend(["-ccl", str(paths["ccl_path"])])
    else:
        spec, argv = specs["cfx_post"], ["-batch", str(paths["script"])]
    return spec.command_id, spec.executable, [str(spec.executable), *argv]


def _artifact(path: Path | None, started_ns: int) -> dict[str, Any]:
    if path is None:
        return {"path": None, "declared": False}
    try:
        info = path.stat()
    except OSError:
        return {"path": str(path), "declared": True, "exists": False, "fresh": False}
    return {"path": str(path), "declared": True, "exists": True, "size": info.st_size,
            "mtime_ns": info.st_mtime_ns, "fresh": info.st_mtime_ns >= started_ns}


def _solve_log_path(op: CFXSolveOperation, paths: dict[str, Any], workdir: Path) -> Path | None:
    if paths.get("out_path"):
        return paths["out_path"]
    run_name = op.run_name or paths["def_path"].stem
    candidates = sorted(workdir.glob(run_name + "_*.out"), key=lambda item: item.stat().st_mtime_ns if item.exists() else 0)
    if candidates:
        return candidates[-1]
    fallback = workdir / (run_name + ".out")
    return fallback if fallback.exists() else None


def _diagnostic(code: str, message: str, severity: str = "error") -> dict[str, Any]:
    return {"code": code, "severity": severity, "message": message}


def run_cfx_recipe(recipe: CFXRecipe, *, workdir: Path, run_dir: Path, timeout_seconds: float,
                   env: dict[str, str] | None = None, runner: Runner | None = None,
                   log_name: str = "cfx") -> dict[str, Any]:
    """Run one CFX operation and report process, completion and result separately."""
    paths = _paths(recipe, workdir)
    command_id, executable, argv = _argv(recipe, paths)
    op = recipe.operation
    started_ns = time.time_ns()
    process = (runner or run_process)(
        argv, cwd=workdir, env=env or ansys_env(), timeout_seconds=timeout_seconds,
        run_dir=run_dir, log_name=log_name,
    )
    diagnostics: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    completion: dict[str, Any] = {"process_completed": process.returncode == 0 and not process.timed_out and not process.cancelled}
    outputs: dict[str, Any] = {"operation": op.op}

    if process.cancelled:
        status = "cancelled"
        diagnostics.append(_diagnostic("WORKFLOW_CANCELLED", "CFX operation was cancelled."))
    elif process.timed_out:
        status = "timeout"
        diagnostics.append(_diagnostic("PROCESS_TIMEOUT", "CFX operation timed out."))
    elif process.returncode != 0:
        status = "failed"
        diagnostics.append(_diagnostic(ERROR_PROCESS_NONZERO, "CFX process exited with " + str(process.returncode) + "."))
    elif isinstance(op, CFXPreOperation):
        artifact = _artifact(paths["def_path"], started_ns)
        artifacts.append({**artifact, "role": "def"})
        if paths["def_path"] is not None and not artifact.get("exists"):
            status = "failed"
            diagnostics.append(_diagnostic(ERROR_OUTPUT_MISSING, "Expected .def was not written by CFX-Pre."))
        elif paths["def_path"] is not None and not artifact.get("fresh"):
            status = "failed"
            diagnostics.append(_diagnostic(ERROR_OUTPUT_STALE, "Existing .def predates this run and is not a new output."))
        else:
            status = "complete"
            if paths["def_path"] is None:
                diagnostics.append(_diagnostic("CFX_OUTPUT_UNDECLARED", "No .def was declared; output was not verified.", "warning"))
    elif isinstance(op, CFXSolveOperation):
        result = _artifact(paths["res_path"], started_ns)
        artifacts.append({**result, "role": "res"})
        log_path = _solve_log_path(op, paths, workdir)
        monitor: dict[str, Any] | None = None
        if log_path is not None:
            read = read_cfx_log(log_path)
            if read.get("ok"):
                monitor = summarize(read["state"])
                completion.update(finished_marker=monitor["finished_marker"],
                                  end_time_reached=monitor["end_time_reached"],
                                  end_time_source="log")
        else:
            completion.update(finished_marker=None, end_time_reached=None)
        fresh_result = paths["res_path"] is None or result.get("fresh")
        if paths["res_path"] is not None and not result.get("exists"):
            status = "failed"
            diagnostics.append(_diagnostic(ERROR_RESULT_MISSING, "Declared result file was not produced by this run."))
        elif paths["res_path"] is not None and not result.get("fresh"):
            status = "failed"
            diagnostics.append(_diagnostic(ERROR_RESULT_STALE, "Existing result file predates this run and is not a new result."))
        elif monitor is None:
            status = "failed" if paths["res_path"] is None else "incomplete"
            diagnostics.append(_diagnostic(ERROR_LOG_MISSING, "Solver log was not found; completion could not be verified.", "warning" if paths["res_path"] else "error"))
        elif monitor["finished_marker"]:
            status = "complete"
        elif monitor["end_time_reached"] and fresh_result:
            status = "complete"
            diagnostics.append(_diagnostic("CFX_FINISH_MARKER_MISSING", "End time was reached but the finish marker is absent.", "warning"))
        else:
            status = "incomplete"
            diagnostics.append(_diagnostic(
                ERROR_SOLVER_INCOMPLETE,
                "The launcher exited before the solver reported completion. A CFX launcher may hand off to another "
                "process that is killed when the supervisor's job closes; verify the solver separately."))
        if log_path is not None and monitor is not None:
            outputs["monitor"] = monitor
            outputs["log_path"] = str(log_path)
        completion.update(result_declared=paths["res_path"] is not None, result_fresh=fresh_result,
                          monitor_available=monitor is not None)
    else:  # CFXPostOperation
        status = "complete"
        for name, path in paths["outputs"]:
            artifact = _artifact(path, started_ns)
            artifacts.append({**artifact, "role": "output", "name": name})
            if not artifact.get("exists"):
                status = "failed"
                diagnostics.append(_diagnostic(ERROR_OUTPUT_MISSING, "Declared post-processing output is missing: " + name))
            elif not artifact.get("fresh"):
                status = "failed"
                diagnostics.append(_diagnostic(ERROR_OUTPUT_STALE, "Post-processing output predates this run: " + name))
        if not paths["outputs"]:
            diagnostics.append(_diagnostic("CFX_OUTPUT_UNDECLARED", "No post-processing outputs were declared; outputs were not verified.", "warning"))

    artifacts.extend([
        {"role": "stdout", "path": str(process.stdout_path)},
        {"role": "stderr", "path": str(process.stderr_path)},
    ])
    outputs["artifacts"] = artifacts
    outputs["completion"] = completion
    result = {
        "ok": status == "complete",
        "status": status,
        "operation": op.op,
        "command": {"id": command_id, "executable": str(executable), "argv": argv},
        "process": {"returncode": process.returncode, "timed_out": process.timed_out,
                    "cancelled": process.cancelled, "elapsed_s": process.elapsed_s,
                    "stdout": str(process.stdout_path), "stderr": str(process.stderr_path),
                    "stdout_tail": process.stdout_tail, "stderr_tail": process.stderr_tail},
        "outputs": outputs,
        "diagnostics": diagnostics,
    }
    if isinstance(op, CFXPreOperation) and paths["def_path"] is not None:
        result["outputs"]["def_path"] = str(paths["def_path"])
    if isinstance(op, CFXSolveOperation):
        if paths["res_path"] is not None:
            result["outputs"]["res_path"] = str(paths["res_path"])
    return result
