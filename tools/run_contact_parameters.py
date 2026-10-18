"""Opt-in contact and Workbench design-point product acceptance.

Creates a new ignored project; never opens or overwrites a user project. This
runner is separate from the default unit tests and observes unknown requests.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ansys_mcp.config import discover_installation, require_allowed_path
from ansys_mcp.mechanical import cancel_mechanical_request, mechanical_elevated_request_status, mechanical_request_status, run_mechanical_recipe
from tools.run_product_acceptance import _observe_mechanical, _run_geometry, _write_json


def validate_contact(report: dict[str, Any], parameters: dict[str, Any]) -> dict[str, Any]:
    samples = report.get("time_samples", [])
    checks: dict[str, bool] = {"complete": report.get("stage") == "complete", "two_times": len(samples) == 2}
    area = float(parameters["width_mm"]) * float(parameters["height_mm"]) / 1e6
    if len(samples) == 2:
        for index, sample in enumerate(samples, 1):
            expected = index * float(parameters["pressure_pa"]) * area
            reaction = float(sample["reaction_x_n"])
            checks["reaction_balance_" + str(index)] = math.isfinite(reaction) and abs(reaction - expected) <= 0.02 * expected
            checks["contact_closed_" + str(index)] = 2 <= float(sample["contact_status_min"]) <= float(sample["contact_status_max"]) <= 3
            contact_pressure = float(sample["average_contact_pressure_pa"])
            checks["positive_contact_pressure_" + str(index)] = math.isfinite(contact_pressure) and contact_pressure > 0
        first = float(samples[0]["maximum_deformation_m"])
        second = float(samples[1]["maximum_deformation_m"])
        ratio = second / first if first > 0 else float("nan")
        checks["double_pressure_response"] = math.isfinite(ratio) and abs(ratio - 2) <= 0.1
    else:
        ratio = None
    expected_table = [0.0, float(parameters["pressure_pa"]), 2 * float(parameters["pressure_pa"])]
    checks["table_readback"] = report.get("pressure_table", {}).get("values_pa") == expected_table and report.get("pressure_table", {}).get("times_s") == [0.0, 1.0, 2.0]
    checks["parameter_creation"] = all(report.get("parameters", {}).get(kind, {}).get("id") for kind in ("input", "output"))
    return {"passed": all(checks.values()), "checks": checks, "deformation_ratio": ratio, "reaction_tolerance_relative": 0.02}


def wrapper(source: Path, parameters: dict[str, Any], target: Path, *, geometry: bool = False) -> Path:
    prefix = "# Python Script, API Version = V" + discover_installation().version_tag + "\n" if geometry else ""
    text = prefix + "import json\nCONTACT_PARAMETERS=json.loads(" + repr(json.dumps(parameters, ensure_ascii=True)) + ")\n"
    text += "eval(compile(open(" + repr(str(source)) + ", 'rb').read(), " + repr(str(source)) + ", 'exec'), globals(), globals())\n"
    target.write_text(text, encoding="utf-8")
    return target


def parameter_journal(project: Path, report: dict[str, Any], output: Path) -> str:
    """Build an explicit official Workbench parameter/design-point journal."""
    def literal(value: str) -> str:
        return "u" + json.dumps(value, ensure_ascii=True)
    candidates = {kind: [report["parameters"][kind]["id"]] for kind in ("input", "output")}
    source = """import json
Open(FilePath=PROJECT_PATH)
_candidates=json.loads(CANDIDATES_JSON)
_all=list(Parameters.GetAllParameters())
def _select(kind):
    found=[parameter for parameter in _all if str(parameter.Name) in _candidates[kind]]
    if len(found)!=1:
        raise Exception('A unique Workbench parameter could not be matched to the Mechanical parameter key/name.')
    return found[0]
_input=_select('input')
_output=_select('output')
_base=Parameters.GetDesignPoint(Name='0')
_points=[]
for _expression in ('0.25','0.3'):
    _point=Parameters.CreateDesignPoint(Retained=True)
    _base.CopyParameterExpressions(ToDesignPoint=_point)
    _point.SetParameterExpression(Parameter=_input,Expression=_expression)
    _points.append(_point)
UpdateAllDesignPoints(DesignPoints=_points,ErrorBehavior='Stop',CannotCompleteBehavior='Stop')
_rows=[]
def _kind(value):
    return str(value.GetType().FullName) if hasattr(value,'GetType') else str(type(value).__name__)
for _point in _points:
    _input_value=_point.GetParameterValue(Parameter=_input)
    _output_value=_point.GetParameterValue(Parameter=_output)
    _rows.append({'name':str(_point.Name),'input':str(_input_value),'output':str(_output_value),
                  'input_type':_kind(_input_value),'output_type':_kind(_output_value),
                  'output_quantity_name':str(_output.ValueQuantityName),
                  'up_to_date':bool(_point.IsUpToDate),'retained_valid':bool(_point.HasValidRetainedData),
                  'input_up_to_date':bool(Parameters.IsParameterInDesignPointUpToDate(DesignPoint=_point,Parameter=_input)),
                  'output_up_to_date':bool(Parameters.IsParameterInDesignPointUpToDate(DesignPoint=_point,Parameter=_output))})
Save(FilePath=PROJECT_PATH,Overwrite=True)
_stream=open(OUTPUT_PATH,'w')
try:
    json.dump({'ok':True,'input_name':str(_input.Name),'output_name':str(_output.Name),'points':_rows},_stream,indent=2)
finally:
    _stream.close()
"""
    return source.replace("PROJECT_PATH", literal(str(project))).replace("CANDIDATES_JSON", literal(json.dumps(candidates))).replace("OUTPUT_PATH", literal(str(output)))


def validate_parameters(report: dict[str, Any]) -> dict[str, Any]:
    points = report.get("points", [])
    checks = {"two_updated_points": len(points) == 2, "unique_design_points": len({point.get("name") for point in points}) == 2}
    for index, expected in enumerate((0.25, 0.3)):
        if index >= len(points):
            continue
        try:
            number = r"^\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?)"
            value = float(re.match(number, points[index]["input"]).group(1))
            output = float(re.match(number, points[index]["output"]).group(1))
            checks["readback_" + str(index)] = abs(value - expected) < 1e-12
            checks["output_" + str(index)] = math.isfinite(output) and output > 0
            checks["updated_" + str(index)] = all(points[index].get(field) is True for field in ("up_to_date", "retained_valid", "input_up_to_date", "output_up_to_date"))
        except (AttributeError, KeyError, TypeError, ValueError):
            checks["readback_" + str(index)] = False
    return {"passed": report.get("ok") is True and all(checks.values()), "checks": checks}


def cancel_original(result: dict[str, Any], root: Path, directory: Path) -> None:
    """Bound a failed observation without submitting the product recipe again."""
    payload = result.get("outputs", {})
    session_id = payload.get("session", {}).get("id")
    request_id = payload.get("request_id")
    worker_id, worker_request = payload.get("worker_id"), payload.get("worker_request_id")
    cancellation = cancel_mechanical_request(session_id=session_id, request_id=request_id,
                                             worker_id=worker_id, worker_request_id=worker_request, grace_seconds=5)
    _write_json(directory / "model.cancel_requested.json", cancellation)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if session_id and request_id:
            observed = mechanical_request_status(session_id, request_id)
        elif worker_id and worker_request:
            observed = mechanical_elevated_request_status(worker_id, worker_request)
        else:
            break
        _write_json(directory / "model.cancel_observation.json", observed)
        nested = observed.get("result", observed)
        output = nested.get("outputs", {})
        if output.get("session", {}).get("id"):
            session_id = output["session"]["id"]
            request_id = output.get("request_id", request_id)
        if output.get("session", {}).get("active_request") is None and output.get("session"):
            break
        if observed.get("status") in {"cancelled", "failed"}:
            break
        time.sleep(1)
    if session_id:
        closed = run_mechanical_recipe({"session": {"action": "close", "id": session_id}}, root, directory, 30)
        _write_json(directory / "model.cancel_closed.json", closed)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Explicitly launch fresh product sessions.")
    parser.add_argument("--workdir", type=Path, default=Path.cwd())
    parser.add_argument("--elevation", choices=("auto", "never", "require"), default="auto")
    parser.add_argument("--timeout", type=float, default=600)
    args = parser.parse_args()
    if not args.execute:
        parser.error("Use --execute to opt into real-product acceptance.")
    root = require_allowed_path(args.workdir, must_exist=True, expect_file=False)
    directory = root / ".ansys_mcp" / "contact_parameters" / uuid.uuid4().hex
    directory.mkdir(parents=True, exist_ok=False)
    sources = Path(__file__).resolve().parents[1] / "examples" / "scripts"
    parameters = {"output_dir": str(directory), "geometry_path": str(directory / "compression.step"),
                  "segment_length_mm": 10, "width_mm": 10, "height_mm": 10,
                  "pressure_pa": 1e6, "friction_coefficient": 0.2}
    geometry = wrapper(sources / "mechanical_contact_parameters_geometry.py", parameters, directory / "geometry.py", geometry=True)
    geometry_workflow = {"version": "1", "workdir": str(root), "steps": [{"id": "geometry", "type": "command", "command_id": "spaceclaim",
        "args": ["/RunScript=" + str(geometry), "/Headless=True", "/ExitAfterScript=True", "/ScriptAPI=V" + discover_installation().version_tag],
        "produces": [{"path": parameters["geometry_path"]}, {"path": str(directory / "contact.geometry.json")}]}]}
    geometry_result = _run_geometry(geometry_workflow, directory, args.elevation)
    _write_json(directory / "geometry.workflow.json", geometry_result)
    if not geometry_result.get("ok"):
        print(json.dumps({"ok": False, "stage": "geometry", "directory": str(directory)}))
        return 1
    script = wrapper(sources / "mechanical_contact_parameters.py", parameters, directory / "model.py")
    recipe = {"backend": "workbench", "mode": "batch", "launch": {"elevation": args.elevation},
              "project": {"geometry_path": parameters["geometry_path"], "save_path": str(directory / "contact.wbpj")},
              "operations": [{"op": "script", "script_path": str(script)}]}
    result = _observe_mechanical(recipe, root, directory, args.timeout)
    _write_json(directory / "model.workflow.json", result)
    if not result.get("ok"):
        if result.get("status") == "unknown":
            cancel_original(result, root, directory)
        print(json.dumps({"ok": False, "stage": "model", "directory": str(directory), "status": result.get("status")}))
        return 1
    report = json.loads((directory / "contact.checkpoint.json").read_text(encoding="utf-8"))
    contact_validation = validate_contact(report, parameters)
    _write_json(directory / "contact.validation.json", contact_validation)
    if not contact_validation["passed"]:
        print(json.dumps({"ok": False, "stage": "contact_validation", "directory": str(directory)}))
        return 1
    journal = directory / "parameters.wbjn"
    journal.write_text(parameter_journal(directory / "contact.wbpj", report, directory / "design_points.json"), encoding="utf-8")
    parameter_workflow = {"version": "1", "workdir": str(root), "steps": [{"id": "design_points", "type": "command", "command_id": "runwb2_batch",
        "args": ["-B", "-R", str(journal)], "produces": [{"path": str(directory / "design_points.json")}]}]}
    # A separate fresh Workbench opens the saved owned project only after the
    # Mechanical actor has closed it; never update two copies concurrently.
    session = result.get("outputs", {}).get("session", {})
    if session.get("id"):
        closed = run_mechanical_recipe({"session": {"action": "close", "id": session["id"]}}, root, directory, 30)
        _write_json(directory / "model.closed.json", closed)
    parameter_stage = directory / "parameter_execution"
    parameter_stage.mkdir(exist_ok=False)
    parameter_result = _run_geometry(parameter_workflow, parameter_stage, args.elevation)
    _write_json(directory / "parameters.workflow.json", parameter_result)
    validation = validate_parameters(json.loads((directory / "design_points.json").read_text(encoding="utf-8"))) if parameter_result.get("ok") else {"passed": False}
    _write_json(directory / "parameters.validation.json", validation)
    print(json.dumps({"ok": contact_validation["passed"] and validation["passed"], "directory": str(directory), "contact": contact_validation, "parameters": validation}))
    return 0 if contact_validation["passed"] and validation["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
