from __future__ import annotations

from typing import Any

from .workflow import legacy_pyfluent_workflow, legacy_python_script_workflow, run_workflow


def run_ansys_python_script(
    script_path: str,
    arguments: list[str] | None = None,
    workdir: str | None = None,
    timeout_seconds: int = 3600,
) -> dict[str, Any]:
    workflow = legacy_python_script_workflow(script_path, arguments, workdir)
    return run_workflow(workflow=workflow, timeout_seconds=timeout_seconds)


def run_pyfluent_recipe(
    recipe: dict[str, Any] | None = None,
    recipe_path: str | None = None,
    workdir: str | None = None,
    timeout_seconds: int = 7200,
) -> dict[str, Any]:
    workflow = legacy_pyfluent_workflow(recipe=recipe, recipe_path=recipe_path, workdir=workdir)
    return run_workflow(workflow=workflow, timeout_seconds=timeout_seconds)
