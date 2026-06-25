from __future__ import annotations

import contextlib
import json
import sys
import time
from pathlib import Path
from typing import Any


PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from ansys_mcp.config import known_paths, safe_workdir  # noqa: E402


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: pyfluent_recipe_runner.py RECIPE.json", file=sys.stderr)
        return 2

    recipe_path = Path(sys.argv[1]).resolve()
    recipe = json.loads(recipe_path.read_text(encoding="utf-8"))
    if not isinstance(recipe, dict):
        raise ValueError("Recipe must be a JSON object.")

    pyfluent_core = known_paths()["pyfluent_core"]
    if str(pyfluent_core) not in sys.path:
        sys.path.insert(0, str(pyfluent_core))

    try:
        import ansys.fluent.core as pyfluent
    except Exception as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "errors": [{"stage": "import", "code": "PYFLUENT_IMPORT_ERROR", "error": repr(exc)}],
                },
                indent=2,
            ),
            flush=True,
        )
        return 1

    started = time.time()
    results: dict[str, Any] = {}
    errors: list[dict[str, Any]] = []
    solver = None

    try:
        launch_kwargs = _launch_kwargs(recipe)
        solver = pyfluent.launch_fluent(**launch_kwargs)
        _read_case(solver, recipe)

        for index, operation in enumerate(recipe.get("operations", [])):
            try:
                value = _run_operation(solver, operation)
                store_as = operation.get("store_as")
                if store_as:
                    results[store_as] = value
            except Exception as exc:
                error = {"index": index, "operation": operation, "error": repr(exc)}
                errors.append(error)
                if not operation.get("continue_on_error", False):
                    raise

        _write_outputs(solver, recipe)

        summary = {
            "ok": not errors,
            "recipe": str(recipe_path),
            "results": results,
            "errors": errors,
            "elapsed_s": round(time.time() - started, 3),
        }
        print(json.dumps(summary, indent=2, default=str), flush=True)
        return 0 if not errors else 2
    except Exception as exc:
        summary = {
            "ok": False,
            "recipe": str(recipe_path),
            "results": results,
            "errors": [*errors, {"stage": "fatal", "error": repr(exc)}],
            "elapsed_s": round(time.time() - started, 3),
        }
        print(json.dumps(summary, indent=2, default=str), flush=True)
        return 1
    finally:
        if solver is not None:
            with contextlib.suppress(Exception):
                solver.exit()


def _launch_kwargs(recipe: dict[str, Any]) -> dict[str, Any]:
    launch = dict(recipe.get("launch", {}))
    cwd = launch.get("cwd") or recipe.get("workdir")
    if cwd:
        launch["cwd"] = str(safe_workdir(cwd, Path.cwd()))
    launch.setdefault("mode", "solver")
    launch.setdefault("precision", "double")
    launch.setdefault("processor_count", 2)
    launch.setdefault("start_transcript", True)
    return launch


def _read_case(solver: Any, recipe: dict[str, Any]) -> None:
    read = recipe.get("read")
    if read is None and any(key in recipe for key in ("case", "case_file", "case_data")):
        read = recipe
    if not read:
        return
    if isinstance(read, str):
        read = {"case": read}
    method = read.get("method", "auto")
    case = read.get("case") or read.get("case_file")
    case_data = read.get("case_data")

    file_api = _file_api(solver)
    if case_data or method == "case_data":
        file_api.read_case_data(file_name=str(case_data or case))
    elif case or method == "case":
        file_api.read_case(file_name=str(case))


def _write_outputs(solver: Any, recipe: dict[str, Any]) -> None:
    write = recipe.get("write")
    if write is None and any(key in recipe for key in ("case", "case_data")):
        write = recipe
    if not write:
        return
    file_api = _file_api(solver)
    if write.get("case"):
        file_api.write_case(file_name=str(write["case"]))
    if write.get("case_data"):
        file_api.write_case_data(file_name=str(write["case_data"]))


def _file_api(solver: Any) -> Any:
    with contextlib.suppress(Exception):
        return solver.settings.file
    return solver.file


def _run_operation(solver: Any, operation: dict[str, Any]) -> Any:
    op = operation.get("op")
    if not op:
        raise ValueError(f"Operation is missing op: {operation}")

    if op == "read":
        return _read_case(solver, operation)
    if op == "write":
        return _write_outputs(solver, operation)
    if op == "set_state":
        target = _resolve_any(solver, operation["path"])
        return target.set_state(operation.get("value"))
    if op == "set_constant":
        target = _resolve_any(solver, operation["path"])
        return _set_constant(target, operation.get("value"))
    if op == "call":
        target = _resolve_any(solver, operation["path"])
        return target(*(operation.get("args") or []), **(operation.get("kwargs") or {}))
    if op == "tui":
        target = _resolve_any(solver.tui, operation["path"])
        return target(*(operation.get("args") or []), **(operation.get("kwargs") or {}))
    if op == "iterate":
        return solver.settings.solution.run_calculation.iterate(iter_count=int(operation["iterations"]))
    if op == "calculate":
        return solver.settings.solution.run_calculation.calculate()
    if op == "mesh_check":
        return solver.tui.mesh.check()
    if op == "create":
        target = _resolve_any(solver, operation["path"])
        created = target.create(operation["name"])
        state = operation.get("state")
        if state:
            created.set_state(state)
        return operation["name"]
    if op == "set_zone_type":
        return solver.settings.setup.boundary_conditions.set_zone_type(
            zone_list=operation["zone_list"],
            new_type=operation["new_type"],
        )
    if op == "create_force_report":
        return _create_force_report(solver, operation)
    if op == "compute_reports":
        names = operation.get("names") or operation.get("report_defs")
        rd = solver.settings.solution.report_definitions
        try:
            return rd.compute(report_defs=names)
        except TypeError:
            return rd.compute(names)
    if op == "get_state":
        return _resolve_any(solver, operation["path"]).get_state()
    if op == "inspect_path":
        target = _resolve_any(solver, operation["path"])
        data: dict[str, Any] = {
            "path": operation["path"],
            "type": type(target).__name__,
            "members": [name for name in dir(target) if not name.startswith("_")][:200],
        }
        with contextlib.suppress(Exception):
            data["state"] = target.get_state()
        with contextlib.suppress(Exception):
            data["active_commands"] = target.get_active_command_names()
        return data

    raise ValueError(f"Unsupported operation: {op}")


def _resolve_any(root: Any, paths: str | list[str]) -> Any:
    candidates = paths if isinstance(paths, list) else [paths]
    errors: list[str] = []
    for path in candidates:
        try:
            return _resolve(root, path)
        except Exception as exc:
            errors.append(f"{path}: {type(exc).__name__}: {exc}")
    raise AttributeError("; ".join(errors))


def _resolve(root: Any, dotted_path: str) -> Any:
    current = root
    for part in dotted_path.split("."):
        if "[" in part and part.endswith("]"):
            attr, key = part[:-1].split("[", 1)
            if attr:
                current = getattr(current, attr)
            current = current[key]
        else:
            current = getattr(current, part)
    return current


def _set_constant(target: Any, value: Any) -> Any:
    try:
        target.constant = value
        return value
    except Exception:
        pass
    try:
        return target.value.set_state(value)
    except Exception:
        return target.set_state(value)


def _create_force_report(solver: Any, operation: dict[str, Any]) -> Any:
    rd = solver.settings.solution.report_definitions
    name = operation["name"]
    report = rd.force.create(name)
    state = {
        key: operation[key]
        for key in ("force_vector", "zones", "per_selection", "report_output_type")
        if key in operation
    }
    if state:
        report.set_state(state)
    return name


if __name__ == "__main__":
    raise SystemExit(main())
