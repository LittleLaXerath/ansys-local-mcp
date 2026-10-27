"""Opt-in ED/LS-DYNA low-speed contact and RBD driven-slider acceptance.

Run one case at a time in an isolated new project. --prepare-only validates and
writes the private input files without starting SpaceClaim or Mechanical. A
prepared fixture is never advertised as a verified analysis or solver result.
"""
from __future__ import annotations

import argparse
import json
import math
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ansys_mcp.config import require_allowed_file, require_allowed_path
from ansys_mcp.mechanical import run_mechanical_recipe
from ansys_mcp.mechanical_schema import MechanicalRecipe
from ansys_mcp.schemas import WorkflowModel
from tools.run_product_acceptance import _observe_mechanical, _run_geometry, _wrapper, _write_json


def prepare(directory: Path, root: Path, *, case: str, backend: str, elevation: str,
            template: str | None = None, solver: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Prepare only. No API here launches a product or executes a workflow."""
    parameters = {"output_dir": str(directory), "geometry_path": str(directory / "drop.step"),
                  "fresh_project": True, "case": case, "block_side_mm": 10.0,
                  "plate_side_mm": 40.0, "plate_height_mm": 6.0, "gap_mm": 0.02,
                  "drop_speed_m_s": 0.1, "drop_end_time_s": 0.0005,
                  "rigid_speed_m_s": 0.01, "rigid_end_time_s": 0.1}
    _write_json(directory / "parameters.json", parameters)
    snapshots = directory / "scripts"
    snapshots.mkdir()
    for filename in ("mechanical_drop_geometry.py", "mechanical_specialized_acceptance.py"):
        shutil.copyfile(root / "examples" / "scripts" / filename, snapshots / filename)
    geometry_script = _wrapper(snapshots / "mechanical_drop_geometry.py", parameters, directory / "drop_geometry.py")
    product_script = _wrapper(snapshots / "mechanical_specialized_acceptance.py", parameters, directory / "specialized.py")
    geometry = {"version": "1", "workdir": str(root), "steps": [{"id": "drop_geometry", "type": "command",
        "command_id": "spaceclaim", "args": ["/RunScript=" + str(geometry_script), "/Headless=True", "/ExitAfterScript=True",
            "/ScriptAsync=False", "/ScriptOutput=" + str(directory / "spaceclaim.log")],
        "produces": [{"path": str(directory / name)} for name in ("drop.step", "drop.scdocx", "geometry.json")]}]}
    # These are Workbench template names, checked by actual GetTemplate at run
    # time. The recorded source remains unverified until product execution.
    defaults = {"explicit": ("Explicit Dynamics", "AUTODYN"), "lsdyna": ("LSDYNA", None), "rigid": ("Rigid Dynamics", None)}
    default_template, default_solver = defaults[case]
    extension = ".wbpj" if backend == "workbench" else ".mechdat"
    project = {"geometry_path": str(directory / "drop.step"), "save_path": str(directory / ("specialized" + extension)),
               "template_name": template or default_template}
    if solver or (not template and default_solver):
        project["solver"] = solver or default_solver
    recipe = {"version": "1", "backend": backend, "mode": "persistent", "launch": {"elevation": elevation}, "project": project,
              "operations": [{"id": "preflight", "op": "project_save", "path": str(directory / ("preflight" + extension))},
                             {"id": "benchmark", "op": "script", "script_path": str(product_script)}]}
    WorkflowModel.model_validate(geometry)
    MechanicalRecipe.model_validate(recipe)
    _write_json(directory / "geometry.workflow.json", geometry)
    _write_json(directory / "recipe.json", recipe)
    _write_json(directory / "preparation.json", {"status": "prepared", "product_launched": False,
                                                 "verification": "not_verified", "case": case, "backend": backend})
    return geometry, recipe


from ansys_mcp.benchmarks import validate_specialized as validate


def resolve_saved_result_path(report: dict[str, Any], project_path: Path) -> dict[str, Any]:
    """SaveAs relocates native solver folders; retain the original observation."""
    if report.get("status") != "solved" or not report.get("result_file"):
        return report
    old_path = Path(report["result_file"])
    if old_path.is_file():
        return report
    suffix = "_files" if project_path.suffix.casefold() == ".wbpj" else "_Mech_Files"
    sidecar = require_allowed_path(project_path.with_name(project_path.stem + suffix), must_exist=True, expect_file=False)
    candidates = [path for path in sidecar.rglob(old_path.name) if path.is_file()]
    if len(candidates) != 1:
        raise ValueError("Saved project did not retain a unique native result file.")
    retained = require_allowed_file(candidates[0])
    return {**report, "result_file_before_project_save": str(old_path), "result_file": str(retained),
            "result_file_exists": True, "result_path_relocated_after_save": True,
            "solver_directory_before_project_save": report.get("solver_directory"), "solver_directory": str(retained.parent)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", required=True, choices=("explicit", "lsdyna", "rigid"))
    parser.add_argument("--backend", choices=("workbench", "pymechanical"), default="workbench")
    parser.add_argument("--elevation", choices=("auto", "never", "require"), default="auto")
    parser.add_argument("--output-dir")
    parser.add_argument("--geometry-path", help="Reuse an existing two-body acceptance STEP instead of launching SpaceClaim.")
    parser.add_argument("--template", help="Explicit Workbench template override, recorded in the private recipe.")
    parser.add_argument("--solver", help="Explicit Workbench solver override.")
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    directory = require_allowed_path(args.output_dir or root / ".ansys_mcp" / "specialized_acceptance" / (stamp + "_" + args.case), base=root, expect_file=False)
    if args.validate_only:
        report = json.loads((directory / "specialized.checkpoint.json").read_text(encoding="utf-8"))
        recipe = json.loads((directory / "recipe.json").read_text(encoding="utf-8"))
        report = resolve_saved_result_path(report, Path(recipe["project"]["save_path"]))
        _write_json(directory / "specialized.report.json", report)
        result = validate(report)
        _write_json(directory / "validation.json", result)
        print(json.dumps(result))
        return 0 if result["ok"] else 1
    directory.mkdir(parents=True, exist_ok=False)
    geometry_workflow, recipe = prepare(directory, root, case=args.case, backend=args.backend, elevation=args.elevation,
                                        template=args.template, solver=args.solver)
    if args.prepare_only:
        print(json.dumps({"ok": True, "status": "prepared", "verification": "not_verified", "product_launched": False, "output_dir": str(directory)}))
        return 0
    if args.geometry_path:
        source = require_allowed_file(args.geometry_path, base=root)
        shutil.copyfile(source, directory / "drop.step")
        geometry = {"ok": True, "source": "supplied_fixture_step", "product_launched": False}
    else:
        geometry = _run_geometry(geometry_workflow, directory, args.elevation)
    _write_json(directory / "geometry.envelope.json", geometry)
    if not geometry.get("ok"):
        print(json.dumps({"ok": False, "phase": "geometry", "output_dir": str(directory)}))
        return 1
    run_dir = directory / "mechanical_run"
    run_dir.mkdir()
    result = _observe_mechanical(recipe, root, run_dir, args.timeout)
    _write_json(directory / "mechanical.envelope.json", result)
    session_id = result.get("outputs", {}).get("session", {}).get("id")
    try:
        if not result.get("ok"):
            print(json.dumps({"ok": False, "phase": "mechanical", "status": result.get("status"), "output_dir": str(directory),
                              "diagnostics": result.get("diagnostics")}))
            return 1
        report = result["outputs"]["values"]["benchmark"]
        report = resolve_saved_result_path(report, Path(recipe["project"]["save_path"]))
        _write_json(directory / "specialized.report.json", report)
        validation = validate(report)
        _write_json(directory / "validation.json", validation)
        print(json.dumps({"output_dir": str(directory), **validation}))
        return 0 if validation["ok"] else 1
    finally:
        # Never close an unknown request merely because an observation expired.
        if session_id and result.get("status") != "unknown":
            closed = run_mechanical_recipe({"backend": args.backend, "launch": {"elevation": args.elevation},
                "session": {"action": "close", "id": session_id}}, root, directory / "close", 60)
            _write_json(directory / "closed.json", closed)


if __name__ == "__main__":
    raise SystemExit(main())
