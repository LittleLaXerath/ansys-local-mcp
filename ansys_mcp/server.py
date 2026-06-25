from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .fluent_recipe import run_ansys_python_script as run_ansys_python_script_impl
from .fluent_recipe import run_pyfluent_recipe as run_pyfluent_recipe_impl
from .config import known_paths, require_allowed_file
from .runtime import signature_info
from .workflow import inspect_runtime as inspect_runtime_impl
from .workflow import run_workflow as run_workflow_impl
from .workflow import validate_workflow as validate_workflow_impl


mcp = FastMCP("ansys-local")


@mcp.tool()
def get_ansys_install_info() -> dict[str, Any]:
    """Return local Ansys installation paths and availability."""
    return inspect_runtime_impl()


@mcp.tool()
def inspect_runtime() -> dict[str, Any]:
    """Return runtime paths, command registry, allowed roots, and workflow capabilities."""
    return inspect_runtime_impl()


@mcp.tool()
def check_ansys_security_surface() -> dict[str, Any]:
    """Read-only security surface check for Ansys entry points and licensing helpers."""
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
        "note": "Signature mismatches mean the file differs from its embedded signature. This server only reports signature status.",
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
) -> dict[str, Any]:
    """Validate a workflow without launching Ansys or mutating files."""
    return validate_workflow_impl(workflow=workflow, workflow_path=workflow_path, variables=variables)


@mcp.tool()
def run_workflow(
    workflow: dict[str, Any] | None = None,
    workflow_path: str | None = None,
    variables: dict[str, Any] | None = None,
    timeout_seconds: int = 7200,
) -> dict[str, Any]:
    """Run a data-driven Ansys workflow with whitelisted commands and structured output."""
    return run_workflow_impl(
        workflow=workflow,
        workflow_path=workflow_path,
        variables=variables,
        timeout_seconds=timeout_seconds,
    )


def main() -> None:
    """Run the MCP server over stdio."""
    mcp.run()
