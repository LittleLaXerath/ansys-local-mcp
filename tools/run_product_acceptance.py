"""Opt-in real-product benchmarks. Never run by the default unit-test suite.

Run from the repository with its installed environment. SpaceClaim creates a
fresh 100 x 10 x 10 mm block. One owning Mechanical session then solves three
small, non-impact models; native projects/results stay in the ignored run tree.
An unknown request is observed by its original handle, never replayed.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ansys_mcp.config import discover_installation, require_allowed_file, require_allowed_path
from ansys_mcp.capabilities import record_runtime_evidence
from ansys_mcp.dpf import _load_dpf, _start_server, run_dpf_recipe
from ansys_mcp.dpf_schema import DPFRecipe
from ansys_mcp.elevation import ElevatedRequestTimeout, ElevatedSession
from ansys_mcp.mechanical import (
    mechanical_elevated_request_status, mechanical_request_status, run_mechanical_recipe,
)
from ansys_mcp.workflow import run_workflow


from ansys_mcp.benchmarks import analytic_references
from ansys_mcp.mechanical_benchmarks import material_properties


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=True, default=str, allow_nan=False), encoding="utf-8")


def _wrapper(source: Path, parameters: dict[str, Any], target: Path) -> Path:
    # JSON content is an ASCII Python string literal, not shell interpolation.
    text = "# Python Script, API Version = V" + discover_installation().version_tag + "\n" if source.name.endswith("_geometry.py") else ""
    text += "import json\nACCEPTANCE_PARAMETERS = json.loads(" + repr(json.dumps(parameters, ensure_ascii=True)) + ")\n"
    text += "try:\n    eval(compile(open(" + repr(str(source)) + ", 'rb').read(), " + repr(str(source)) + ", 'exec'), globals(), globals())\n"
    text += "except Exception:\n    import traceback\n    open(" + repr(str(target.with_suffix(".error.txt"))) + ", 'w').write(traceback.format_exc())\n    raise\n"
    target.write_text(text, encoding="utf-8")
    return target


def _observe_mechanical(recipe: dict[str, Any], root: Path, directory: Path, timeout_seconds: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    result = run_mechanical_recipe(recipe, root, directory, min(45, timeout_seconds))
    _write_json(directory / "observation.json", result)
    if result.get("status") != "unknown":
        return result
    output = result.get("outputs", {})
    request_id = output.get("request_id")
    session_id = output.get("session", {}).get("id")
    worker_id = output.get("worker_id")
    worker_request = output.get("worker_request_id")
    while time.monotonic() < deadline:
        if worker_id and worker_request:
            observation = mechanical_elevated_request_status(worker_id, worker_request)
        elif session_id and request_id:
            observation = mechanical_request_status(session_id, request_id)
        else:
            return result
        _write_json(directory / "observation.json", observation)
        if isinstance(observation.get("result"), dict):
            result = observation["result"]
            if result.get("status") == "unknown" and result.get("outputs", {}).get("request_id"):
                # An elevated worker's observation can itself time out while its
                # Mechanical future remains live. Observe that original future
                # inside the same owned worker rather than replaying the recipe.
                payload = result["outputs"]
                request_id = payload["request_id"]
                session_id = payload["session"]["id"]
                worker_id = worker_request = None
                continue
            return result
        if observation.get("status") not in {"running", "pending", "queued", "unknown"}:
            return observation
        if observation.get("status") in {"missing", "failed", "cancelled"}:
            return observation
        time.sleep(2)
    # Leave the same owned task running and its handle recorded for inspection.
    return {**result, "acceptance_observation_deadline_exceeded": True}


def _run_geometry(workflow: dict[str, Any], directory: Path, elevation: str) -> dict[str, Any]:
    if elevation != "require":
        result = run_workflow(workflow=workflow, timeout_seconds=180)
        refused_before_launch = (len(result.get("steps", [])) == 1 and not result.get("steps", [{}])[0].get("outputs") and
            any(item.get("code") == "PROCESS_LAUNCH_ERROR" and "[WinError 740]" in item.get("message", "")
                for item in result.get("diagnostics", [])))
        if elevation != "auto" or not refused_before_launch:
            return result
    proxy = ElevatedSession.launch("ansys_mcp.workflow", state_dir=directory / "geometry_worker", timeout_seconds=30, elevated=True)
    terminal = False
    try:
        try:
            result = proxy.request("run_workflow", {"workflow": workflow, "timeout_seconds": 180}, timeout_seconds=190)
            terminal = True
            return result
        except ElevatedRequestTimeout as error:
            _write_json(directory / "geometry.worker_request.json", {"worker_state_dir": str(proxy.state_dir), "request_id": error.request_id})
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                observation = proxy.query(error.request_id)
                _write_json(directory / "geometry.observation.json", observation)
                if observation.get("status") == "complete":
                    terminal = True
                    return observation["result"]
                if observation.get("status") in {"failed", "cancelled"}:
                    terminal = True
                    return {"ok": False, "status": observation["status"], "observation": observation}
                time.sleep(2)
            return {"ok": False, "status": "unknown", "worker_request_id": error.request_id}
    finally:
        if terminal:
            _write_json(directory / "geometry.worker_closed.json", proxy.close())


def mechanical_nodal_values(path: Path) -> dict[int, float]:
    raw = path.read_bytes()
    text = raw.decode("utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig", errors="strict")
    result = {}
    for line in text.splitlines():
        columns = line.strip().split("\t")
        if len(columns) < 2:
            continue
        try:
            node_id = int(columns[0])
            value = float(columns[-1])
        except ValueError:
            continue
        if node_id in result:
            raise ValueError("Mechanical nodal export contains duplicate IDs.")
        result[node_id] = value
    if not result:
        raise ValueError("Mechanical did not export a tab-separated nodal result table.")
    return result


def dpf_comparison(output_dir: Path, root: Path) -> dict[str, Any]:
    recipe = {"result_files": [{"path": str(output_dir / "static.rst")}], "operations": [
        {"op": "metadata", "id": "metadata"},
        {"op": "result", "id": "u", "name": "displacement", "time_scoping": 1, "requested_location": "Nodal"},
        {"op": "operator", "id": "norm", "name": "norm_fc", "inputs": {"fields_container": {"$ref": "u"}}},
        {"op": "export", "id": "csv", "source": "norm", "path": str(output_dir / "dpf_displacement.csv"), "overwrite": True},
    ]}
    result = run_dpf_recipe(recipe, root, output_dir / "dpf", 120)
    _write_json(output_dir / "dpf.envelope.json", result)
    if not result.get("ok"):
        return {"status": "failed", "diagnostics": result.get("diagnostics", [])}
    values = mechanical_nodal_values(output_dir / "static_deformation.txt")
    with (output_dir / "dpf_displacement.csv").open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    if not rows or any(row["location"] != "Nodal" or row["unit"] != "m" for row in rows):
        raise ValueError("DPF comparison requires the identical nodal location and metre unit.")
    dpf_values = {int(row["entity_id"]): float(row["component_0"]) for row in rows}
    if set(values) != set(dpf_values):
        raise ValueError("Mechanical and DPF do not contain the same node IDs.")
    maximum = max(abs(value) for value in values.values())
    error = max(abs(value - dpf_values[node]) for node, value in values.items()) / maximum
    relative_error = max(abs(value - dpf_values[node]) / max(abs(value), maximum * 1e-9) for node, value in values.items())
    return {"status": "passed" if max(error, relative_error) <= 0.001 else "failed", "nodes_compared": len(values),
            "normalized_maximum_error": error, "maximum_relative_error": relative_error,
            "near_zero_scale_floor": 1e-9, "tolerance": 0.001, "location": "Nodal", "unit": "m"}


def thermal_linearity(output_dir: Path, length_m: float) -> dict[str, Any]:
    dpf = _load_dpf()
    server = _start_server(dpf, DPFRecipe.model_validate({"operations": [{"id": "readers", "op": "discover"}]}), 30)
    try:
        model = dpf.Model(str(output_dir / "thermal.rth"), server=server)
        names = [value.name for value in model.metadata.result_info.available_results]
        name = next((value for value in ("temperature", "structural_temperature") if value in names), None)
        if name is None:
            raise ValueError("Thermal reader does not advertise a temperature result.")
        field = getattr(model.results, name).on_last_time_freq.eval()[0]
        coordinates = model.metadata.meshed_region.nodes.coordinates_field
        if field.location != "Nodal" or field.unit not in {"C", "degC", "K"}:
            raise ValueError("Thermal comparison requires a nodal Celsius or Kelvin field.")
        offset = 273.15 if field.unit == "K" else 0
        errors = []
        for index, node_id in enumerate(field.scoping.ids):
            x = float(coordinates.get_entity_data_by_id(int(node_id))[0][0])
            actual = float(field.get_entity_data(index)[0]) - offset
            errors.append(abs(actual - 100 * x / length_m) / 100)
        maximum = max(errors)
        return {"status": "passed" if maximum <= 0.01 else "failed", "nodes_compared": len(errors),
                "maximum_fraction_of_temperature_range_error": maximum, "tolerance": 0.01}
    finally:
        server.shutdown()


def validate_report(output_dir: Path, report: dict[str, Any], root: Path) -> dict[str, Any]:
    material = material_properties(output_dir)
    references = analytic_references(0.1, 0.01, 0.01, **material)
    checks = {}
    for name, case in report.get("cases", {}).items():
        if case.get("status") != "solved":
            checks[name] = {"status": "failed", "reason": "Product case did not finish solving."}
            continue
        if name == "static":
            error = abs(case["maximum_deformation_m"] / references["static_tip_m"] - 1)
            balance = abs(case["reaction_y_n"] - 1)
            checks[name] = {"status": "passed" if error <= 0.05 and balance <= 0.01 else "failed",
                            "relative_error": error, "tolerance": 0.05, "reaction_balance_error_n": balance}
        elif name == "modal":
            error = abs(min(case["frequencies_hz"]) / references["modal_first_hz"] - 1)
            checks[name] = {"status": "passed" if error <= 0.05 else "failed", "relative_error": error, "tolerance": 0.05}
        elif name == "thermal":
            error = abs(case["average_heat_flux_w_m2"] / references["thermal_flux_w_m2"] - 1)
            extrema = max(abs(case["minimum_temperature_c"]), abs(case["maximum_temperature_c"] - 100)) / 100
            checks[name] = {"status": "passed" if max(error, extrema) <= 0.01 else "failed",
                            "flux_relative_error": error, "temperature_range_relative_error": extrema, "tolerance": 0.01}
    if report.get("cases", {}).get("static", {}).get("status") == "solved":
        checks["dpf_comparison"] = dpf_comparison(output_dir, root)
    if report.get("cases", {}).get("thermal", {}).get("status") == "solved":
        checks["thermal_linearity"] = thermal_linearity(output_dir, 0.1)
    requested = set(report.get("requested_cases", ["static", "modal", "thermal"]))
    required = requested | ({"dpf_comparison"} if "static" in requested else set()) | ({"thermal_linearity"} if "thermal" in requested else set())
    return {"ok": set(checks) == required and all(value["status"] == "passed" for value in checks.values()),
            "requested_cases": sorted(requested), "not_run": sorted({"static", "modal", "thermal"} - requested),
            "material_properties_si": material, "analytic_references": references, "checks": checks}


def record_acceptance_evidence(report: dict[str, Any], validation: dict[str, Any], envelope: dict[str, Any]) -> list[str]:
    """Promote exact executed APIs only after all requested numerical gates pass."""
    if validation.get("ok") is not True or envelope.get("ok") is not True:
        return []
    session = envelope.get("outputs", {}).get("session", {})
    backend, version = session.get("backend"), session.get("version")
    if backend not in {"workbench", "pymechanical"} or not version:
        return []
    cases = {name: case for name, case in report.get("cases", {}).items()
             if case.get("status") == "solved" and validation.get("checks", {}).get(name, {}).get("status") == "passed"}
    if not cases:
        return []
    prefix = "Ansys.ACT.Automation.Mechanical."
    names = [prefix + "Solution.Solve", prefix + "Solution.EvaluateAllResults"]
    if "modal" in cases:
        names.append(prefix + "Model.AddModalAnalysis")
    if "thermal" in cases:
        names.append(prefix + "Model.AddSteadyStateThermalAnalysis")
    # A Workbench Static Structural template can supply the initial analysis.
    # Never infer a factory call from the existence of a solved static case.
    if backend == "pymechanical" and cases.get("static", {}).get("creation_api") == "Model.AddStaticStructuralAnalysis":
        names.append(prefix + "Model.AddStaticStructuralAnalysis")
    for qualified in names:
        record_runtime_evidence({"product": "mechanical", "version": version, "backend": backend,
            "qualified_name": qualified, "runtime_reachable": True, "validation_stage": "result",
            "evidence_source": "core_acceptance_" + backend + "_trusted_script"})
    return names


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=("workbench", "pymechanical"), default="workbench")
    parser.add_argument("--elevation", choices=("auto", "never", "require"), default="auto")
    parser.add_argument("--case", choices=("all", "static", "modal", "thermal"), default="all")
    parser.add_argument("--output-dir")
    parser.add_argument("--geometry-path", help="Reuse an allowed, previously generated benchmark STEP instead of rerunning SpaceClaim.")
    parser.add_argument("--timeout", type=float, default=1200)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if args.output_dir:
        directory = require_allowed_path(args.output_dir, base=root, expect_file=False)
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        directory = require_allowed_path(root / ".ansys_mcp" / "product_acceptance" / stamp, expect_file=False)
    if args.validate_only:
        report = json.loads((directory / "mechanical.checkpoint.json").read_text(encoding="utf-8"))
        result = validate_report(directory, report, root)
        envelope_path = directory / "mechanical.envelope.json"
        if envelope_path.is_file():
            try:
                result["recorded_apis"] = record_acceptance_evidence(report, result, json.loads(envelope_path.read_text(encoding="utf-8")))
            except (OSError, ValueError, TypeError):
                result["evidence_warning"] = "Numerical checks passed but capability evidence registration failed."
        _write_json(directory / "validation.json", result)
        print(json.dumps(result))
        return 0 if result["ok"] else 1
    directory.mkdir(parents=True, exist_ok=False)
    parameters = {"output_dir": str(directory), "geometry_path": str(directory / "beam.step"),
                  "length_mm": 100, "width_mm": 10, "height_mm": 10,
                  "cases": ["static", "modal", "thermal"] if args.case == "all" else [args.case]}
    _write_json(directory / "parameters.json", parameters)
    geometry_script = _wrapper(root / "examples" / "scripts" / "mechanical_acceptance_geometry.py", parameters, directory / "geometry.py")
    geometry_workflow = {"version": "1", "workdir": str(root), "steps": [{"id": "beam_geometry", "type": "command",
        "command_id": "spaceclaim", "args": ["/RunScript=" + str(geometry_script), "/Headless=True", "/ExitAfterScript=True", "/ScriptAsync=False",
            "/ScriptOutput=" + str(directory / "spaceclaim.log")],
        "produces": [{"path": str(directory / name)} for name in ("beam.step", "beam.scdocx", "geometry.json")]}]}
    if args.geometry_path:
        geometry_source = require_allowed_file(args.geometry_path, base=root)
        shutil.copyfile(geometry_source, directory / "beam.step")
        geometry = {"ok": True, "source": "supplied_benchmark_step", "spaceclaim_run_this_invocation": False}
    else:
        geometry = _run_geometry(geometry_workflow, directory, args.elevation)
    _write_json(directory / "geometry.envelope.json", geometry)
    if not geometry.get("ok"):
        print(json.dumps({"ok": False, "phase": "geometry", "output_dir": str(directory)}))
        return 1
    script = _wrapper(root / "examples" / "scripts" / "mechanical_acceptance.py", parameters, directory / "mechanical.py")
    recipe = {"version": "1", "backend": args.backend, "mode": "persistent", "launch": {"elevation": args.elevation},
              "project": {"geometry_path": str(directory / "beam.step"), "save_path": str(directory / ("acceptance.wbpj" if args.backend == "workbench" else "acceptance.mechdat"))},
              "operations": [{"id": "preflight", "op": "project_save", "path": str(directory / ("preflight.wbpj" if args.backend == "workbench" else "preflight.mechdat"))},
                             {"id": "benchmark", "op": "script", "script_path": str(script)}]}
    _write_json(directory / "recipe.json", recipe)
    run_directory = directory / "mechanical_run"
    run_directory.mkdir()
    result = _observe_mechanical(recipe, root, run_directory, args.timeout)
    _write_json(directory / "mechanical.envelope.json", result)
    session_id = result.get("outputs", {}).get("session", {}).get("id")
    try:
        if not result.get("ok"):
            print(json.dumps({"ok": False, "phase": "mechanical", "status": result.get("status"), "output_dir": str(directory),
                              "diagnostics": result.get("diagnostics")}))
            return 1
        report = result["outputs"]["values"]["benchmark"]
        _write_json(directory / "mechanical.report.json", report)
        validation = validate_report(directory, report, root)
        try:
            validation["recorded_apis"] = record_acceptance_evidence(report, validation, result)
        except (OSError, ValueError, TypeError):
            validation["evidence_warning"] = "Numerical checks passed but capability evidence registration failed."
        _write_json(directory / "validation.json", validation)
        print(json.dumps({"output_dir": str(directory), **validation}))
        return 0 if validation["ok"] else 1
    finally:
        if session_id and result.get("status") != "unknown":
            closed = run_mechanical_recipe({"backend": args.backend, "launch": {"elevation": args.elevation},
                "session": {"action": "close", "id": session_id}}, root, directory / "close", 60)
            _write_json(directory / "closed.json", closed)


if __name__ == "__main__":
    raise SystemExit(main())
