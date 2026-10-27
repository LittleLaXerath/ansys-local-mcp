from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from .fluent_recipe import run_ansys_python_script as run_ansys_python_script_impl
from .fluent_recipe import run_pyfluent_recipe as run_pyfluent_recipe_impl
from .config import known_paths, require_allowed_file
from .runtime import signature_info
from .schemas import WorkflowModel, WorkbenchRecipe
from .mechanical_schema import MechanicalRecipe
from .dpf_schema import DPFRecipe
from .cfx_schema import CFXRecipe
from .workflow import inspect_runtime as inspect_runtime_impl
from .workflow import run_workflow as run_workflow_impl
from .workflow import validate_workflow as validate_workflow_impl


mcp = FastMCP("ansys-local")


@mcp.resource("ansys://schema/workflow", mime_type="application/schema+json")
def workflow_schema() -> str:
    """Workflow v1 fields, step variants, output declarations, and reference syntax."""
    return json.dumps(WorkflowModel.model_json_schema(), indent=2)


@mcp.resource("ansys://schema/workbench-recipe", mime_type="application/schema+json")
def workbench_recipe_schema() -> str:
    """Strict Workbench recipe schema, including project and action fields."""
    return json.dumps(WorkbenchRecipe.model_json_schema(), indent=2)


@mcp.resource("ansys://schema/mechanical-recipe", mime_type="application/schema+json")
def mechanical_recipe_schema() -> str:
    """Versioned Mechanical operations, session references, and typed values."""
    return json.dumps(MechanicalRecipe.model_json_schema(), indent=2)


@mcp.resource("ansys://schema/dpf-recipe", mime_type="application/schema+json")
def dpf_recipe_schema() -> str:
    """Versioned local DPF operations, scoping, and artifact exports."""
    return json.dumps(DPFRecipe.model_json_schema(), indent=2)


@mcp.resource("ansys://schema/cfx-recipe", mime_type="application/schema+json")
def cfx_recipe_schema() -> str:
    """Versioned CFX pre/solve/post operation with verified completion."""
    return json.dumps(CFXRecipe.model_json_schema(), indent=2)


@mcp.tool()
def get_ansys_install_info() -> dict[str, Any]:
    """Return local Ansys installation paths and availability."""
    return inspect_runtime_impl()


@mcp.tool()
def inspect_runtime(include_schema: bool = False) -> dict[str, Any]:
    """Inspect runtime. Set include_schema=True for complete workflow and Workbench JSON schemas."""
    return inspect_runtime_impl(include_schema=include_schema)


@mcp.tool()
def check_ansys_security_surface() -> dict[str, Any]:
    """Opt-in signature report. Results never gate validation or execution."""
    paths = known_paths()
    signature_targets = [
        paths["runwb2"],
        paths["mechanical"],
        paths["ansys_mapdl"],
        paths["fluent"],
        paths["cfx_pre"],
        paths["cfx_solve"],
        paths["cfx_post"],
        paths["spaceclaim"],
        paths["electronics_desktop"],
    ]
    return {
        "signatures": [signature_info(path) for path in signature_targets],
        "note": (
            "This opt-in report is informational. Missing or mismatched signatures never block workflows, "
            "and the server does not inspect or manage product licensing."
        ),
    }


@mcp.tool()
def launch_workbench(workdir: str | None = None) -> dict[str, Any]:
    """Launch Ansys Workbench GUI and return immediately."""
    workflow = {
        "version": "1",
        "workdir": workdir,
        "steps": [{"id": "launch_workbench", "type": "command", "command_id": "runwb2", "args": [], "detach": True}],
    }
    return run_workflow_impl(workflow=workflow, timeout_seconds=30)


@mcp.tool()
def run_workbench_journal(
    journal_path: str,
    workdir: str | None = None,
    batch: bool = True,
    timeout_seconds: int = 3600,
) -> dict[str, Any]:
    """Run a Workbench journal (.wbjn). Uses runwb2.bat with -R, optionally -B."""
    journal = require_allowed_file(journal_path)
    args = ["-R", str(journal)]
    if batch:
        args.insert(0, "-B")
    workflow = {
        "version": "1",
        "workdir": workdir or str(journal.parent),
        "steps": [{"id": "run_workbench_journal", "type": "command", "command_id": "runwb2_batch", "args": args}],
    }
    return run_workflow_impl(workflow=workflow, timeout_seconds=timeout_seconds)


@mcp.tool()
def run_mapdl_input(
    input_path: str,
    workdir: str | None = None,
    jobname: str | None = None,
    nproc: int = 2,
    timeout_seconds: int = 3600,
) -> dict[str, Any]:
    """Run a MAPDL input file in batch mode."""
    input_file = require_allowed_file(input_path)
    safe_job = jobname or input_file.stem
    output = Path(workdir or input_file.parent) / f"{safe_job}.out"
    args = [
        "-b",
        "-i",
        str(input_file),
        "-o",
        str(output),
        "-j",
        safe_job,
        "-np",
        str(max(1, int(nproc))),
    ]
    workflow = {
        "version": "1",
        "workdir": workdir or str(input_file.parent),
        "steps": [{"id": "run_mapdl_input", "type": "command", "command_id": "mapdl", "args": args}],
        "artifacts": [{"name": "output_file", "path": str(output), "required": False}],
    }
    return run_workflow_impl(workflow=workflow, timeout_seconds=timeout_seconds)


@mcp.tool()
def run_fluent_journal(
    journal_path: str,
    workdir: str | None = None,
    dimension: str = "3d",
    double_precision: bool = True,
    nproc: int = 2,
    gui: bool = False,
    timeout_seconds: int = 3600,
) -> dict[str, Any]:
    """Run a Fluent journal (.jou). Defaults to 3ddp, headless, two processes."""
    journal = require_allowed_file(journal_path)
    if dimension not in {"2d", "3d"}:
        raise ValueError("dimension must be '2d' or '3d'")
    mode = dimension + ("dp" if double_precision else "")
    args = [mode, f"-t{max(1, int(nproc))}", "-i", str(journal)]
    if not gui:
        args.insert(1, "-g")
    workflow = {
        "version": "1",
        "workdir": workdir or str(journal.parent),
        "steps": [{"id": "run_fluent_journal", "type": "command", "command_id": "fluent", "args": args}],
    }
    return run_workflow_impl(workflow=workflow, timeout_seconds=timeout_seconds)


@mcp.tool()
def run_system_coupling(arguments: list[str], workdir: str | None = None, timeout_seconds: int = 3600) -> dict[str, Any]:
    """Run System Coupling with explicit command-line arguments."""
    workflow = {
        "version": "1",
        "workdir": workdir,
        "steps": [{"id": "run_system_coupling", "type": "command", "command_id": "system_coupling", "args": list(arguments)}],
    }
    return run_workflow_impl(workflow=workflow, timeout_seconds=timeout_seconds)


@mcp.tool()
def run_ansys_python_script(
    script_path: str,
    arguments: list[str] | None = None,
    workdir: str | None = None,
    timeout_seconds: int = 3600,
) -> dict[str, Any]:
    """Run an allowed Python script with the Ansys bundled Python runtime."""
    return run_ansys_python_script_impl(script_path, arguments, workdir, timeout_seconds)


@mcp.tool()
def run_pyfluent_recipe(
    recipe: dict[str, Any] | None = None,
    recipe_path: str | None = None,
    workdir: str | None = None,
    timeout_seconds: int = 7200,
) -> dict[str, Any]:
    """Run a reusable PyFluent JSON recipe instead of writing a one-off script."""
    return run_pyfluent_recipe_impl(recipe, recipe_path, workdir, timeout_seconds)


@mcp.tool()
def validate_workflow(
    workflow: dict[str, Any] | None = None,
    workflow_path: str | None = None,
    variables: dict[str, Any] | None = None,
    validation_mode: Literal["runtime", "schema"] = "runtime",
) -> dict[str, Any]:
    """Validate a workflow without launching Ansys or mutating files."""
    return validate_workflow_impl(
        workflow=workflow,
        workflow_path=workflow_path,
        variables=variables,
        validation_mode=validation_mode,
    )


@mcp.tool()
def run_workflow(
    workflow: dict[str, Any] | None = None,
    workflow_path: str | None = None,
    variables: dict[str, Any] | None = None,
    timeout_seconds: int = 7200,
) -> dict[str, Any]:
    """Run an Ansys workflow. Read ansys://schema/workflow or inspect_runtime(include_schema=True) first.

    Use produces to declare generated artifacts; later steps can reference
    ${steps.ID.outputs.FIELD}. Workbench scalar results are in outputs.values.
    """
    return run_workflow_impl(
        workflow=workflow,
        workflow_path=workflow_path,
        variables=variables,
        timeout_seconds=timeout_seconds,
    )


@mcp.tool()
def start_workflow(
    workflow: dict[str, Any] | None = None,
    workflow_path: str | None = None,
    variables: dict[str, Any] | None = None,
    timeout_seconds: int = 7200,
) -> dict[str, Any]:
    """Start an owned background workflow and return its persistent run_id."""
    from .jobs import start_workflow as start
    return start(workflow=workflow, workflow_path=workflow_path, variables=variables, timeout_seconds=timeout_seconds)


@mcp.tool()
def get_workflow_status(run_id: str) -> dict[str, Any]:
    """Reconcile a background workflow against its worker identity and persisted state."""
    from .jobs import get_workflow_status as status
    return status(run_id)


@mcp.tool()
def cancel_workflow(run_id: str) -> dict[str, Any]:
    """Request cancellation of a workflow created by this service; never replay it."""
    from .jobs import cancel_workflow as cancel
    return cancel(run_id)


@mcp.tool()
def query_capabilities(
    product: str = "mechanical",
    version: str | None = None,
    component: str | None = None,
    status: str | None = None,
    offset: int = 0,
    limit: int = 100,
    kind: Literal["api", "workflow"] = "api",
) -> dict[str, Any]:
    """Page the published API catalog and its separate runtime/validation evidence."""
    from .capabilities import query_capabilities as query
    return query(product=product, version=version, component=component, status=status, offset=offset, limit=limit, kind=kind)


@mcp.tool()
def get_mechanical_request_status(
    request_id: str,
    session_id: str | None = None,
    worker_id: str | None = None,
) -> dict[str, Any]:
    """Observe the same Mechanical request after a timeout; never submit it again."""
    from .mechanical import mechanical_request_status, mechanical_elevated_request_status
    if bool(session_id) == bool(worker_id):
        raise ValueError("Specify exactly one session_id or worker_id from the original response.")
    if session_id:
        return mechanical_request_status(session_id, request_id)
    return mechanical_elevated_request_status(worker_id, request_id)


def main() -> None:
    """Run the MCP server over stdio."""
    mcp.run()
