"""Explicit Workbench-first basic-analysis acceptance. Never called by CI."""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ansys_mcp.acceptance_evidence import file_sha256, import_receipt
from ansys_mcp.config import (
    require_allowed_file,
    require_allowed_path,
    discover_installation,
)
from ansys_mcp.cfd_evidence import workflow_scope
from ansys_mcp.jobs import _write_json
from ansys_mcp.mechanical_benchmarks import (
    nodal_vectors,
    validate_coordinates,
    find_structural_steel,
)
from ansys_mcp.transient_benchmark import (
    validate_transient,
    transient_curve,
    find_transient_material,
)
from ansys_mcp.harmonic_benchmark import harmonic_curve, validate_harmonic
from tools._mcp_acceptance import mcp_client, tool_value
from tools.run_product_acceptance import _wrapper


CASE_SPECS = {
    "coordinate_equivalence": {
        "script": "mechanical_coordinate_acceptance.py",
        "template": "Static Structural (ANSYS)",
        "project": "coordinates.wbpj",
        "results": ("global", "rotated"),
        "parameters": {"angle_deg": 30, "origin_m": [0.03, 0.002, -0.001]},
    },
    "transient_sdof": {
        "script": "mechanical_transient_acceptance.py",
        "template": "Transient Structural (ANSYS)",
        "project": "transient.wbpj",
        "results": ("transient",),
        "axis": "times_s",
        "curve_reader": transient_curve,
        "validator": validate_transient,
        "parameters": {
            "mass_kg": 1000.0,
            "force_n": 100.0,
            "dt_s": 0.00005,
            "end_time_s": 0.04,
            "material_name": "MCP SDOF Linear",
        },
    },
    "harmonic_sdof": {
        "script": "mechanical_harmonic_acceptance.py",
        "template": "Harmonic Response (ANSYS)",
        "project": "harmonic.wbpj",
        "results": ("harmonic",),
        "axis": "frequencies_hz",
        "curve_reader": harmonic_curve,
        "validator": validate_harmonic,
        "parameters": {
            "mass_kg": 1000.0,
            "force_n": 100.0,
            "mass_damping_s_inv": 44.721359549995796,
            "frequency_min_hz": 20.0,
            "frequency_max_hz": 120.0,
            "frequency_intervals": 100,
            "material_name": "MCP SDOF Linear",
        },
    },
}


def material_operations(parameters):
    def reference(name):
        return {"$type": "local_ref", "name": name}

    def operation(id, target, member, kwargs, store_as=None):
        return {
            "op": "engineering_data",
            "action": "call",
            "id": id,
            "target": target,
            "member": member,
            "kwargs": kwargs,
            **({"store_as": store_as} if store_as else {}),
        }

    return [
        operation(
            "material",
            "EngineeringData",
            "CreateMaterial",
            {"Name": parameters["material_name"]},
            "sdof_material",
        ),
        operation(
            "density",
            reference("sdof_material"),
            "CreateProperty",
            {"Name": "Density"},
            "sdof_density",
        ),
        operation(
            "set_density",
            reference("sdof_density"),
            "SetData",
            {
                "SheetName": "",
                "Index": 0,
                "Variables": ["Density"],
                "Values": [["7850 [kg m^-3]"]],
            },
        ),
        operation(
            "elastic",
            reference("sdof_material"),
            "CreateProperty",
            {"Name": "Elasticity", "Behavior": "Isotropic"},
            "sdof_elastic",
        ),
        operation(
            "set_elastic",
            reference("sdof_elastic"),
            "SetData",
            {
                "SheetName": "",
                "Index": 0,
                "Variables": ["Young's Modulus", "Poisson's Ratio"],
                "Values": [["200000000000 [Pa]"], ["0"]],
            },
        ),
        operation(
            "elastic_readback",
            reference("sdof_elastic"),
            "GetData",
            {
                "Variables": ["Young's Modulus", "Poisson's Ratio"],
                "AsDictionary": False,
            },
        ),
    ]


async def execute(
    directory: Path,
    geometry: Path,
    register: bool = False,
    case: str = "coordinate_equivalence",
):
    specification = CASE_SPECS[case]
    axis = specification.get("axis")
    started = time.monotonic()
    deadline = started + 1800
    installation = workflow_scope("mechanical", discover_installation().version_tag)
    _write_json(directory / "installation.json", installation)
    source_hash = file_sha256(geometry)
    _write_json(
        directory / "source-geometry.json",
        {"path": str(geometry), "sha256_before": source_hash},
    )
    destination = directory / "beam.step"
    shutil.copyfile(geometry, destination)
    parameters = {
        "output_dir": str(directory),
        "geometry_path": str(destination),
        "length_m": 0.1,
        "width_m": 0.01,
        "height_m": 0.01,
        **specification["parameters"],
    }
    _write_json(directory / "parameters.json", parameters)
    source = (
        Path(__file__).resolve().parents[1]
        / "examples/scripts"
        / specification["script"]
    )
    stem = "coordinate" if case == "coordinate_equivalence" else case
    snapshot = directory / (stem + ".source.py")
    shutil.copyfile(source, snapshot)
    script = _wrapper(snapshot, parameters, directory / (stem + ".py"))
    model_recipe = {
        "backend": "workbench",
        "mode": "persistent",
        "launch": {"elevation": "never"},
        "project": {
            "geometry_path": str(destination),
            "save_path": str(directory / specification["project"]),
            "template_name": specification["template"],
        },
        "operations": [
            {
                "id": "preflight",
                "op": "project_save",
                "path": str(directory / "preflight.wbpj"),
            },
            *(material_operations(parameters) if "material_name" in parameters else []),
            {"id": "benchmark", "op": "script", "script_path": str(script)},
        ],
    }
    session_id = None
    unknown = False
    results = {}
    async with mcp_client(directory) as client:

        async def run(name, steps):
            nonlocal unknown
            remaining = int(deadline - time.monotonic())
            if remaining <= 0:
                raise TimeoutError(
                    "The acceptance budget expired; no new operation was submitted."
                )
            workflow = {"version": "1", "workdir": str(directory), "steps": steps}
            _write_json(directory / (name + ".workflow.json"), workflow)
            validated = tool_value(
                await client.call_tool(
                    "validate_workflow",
                    {"workflow": workflow, "validation_mode": "runtime"},
                )
            )
            _write_json(directory / (name + ".validation.json"), validated)
            if not validated["ok"]:
                raise ValueError("Workflow validation failed: " + name)
            job = tool_value(
                await client.call_tool(
                    "start_workflow",
                    {"workflow": workflow, "timeout_seconds": remaining},
                )
            )
            _write_json(directory / (name + ".job.json"), job)
            if not job.get("ok") or not job.get("run_id"):
                raise RuntimeError("Background workflow was not started: " + name)
            run_id = job["run_id"]
            while (
                job.get("status") in {"queued", "running", "starting"}
                and time.monotonic() < deadline
            ):
                await asyncio.sleep(1)
                job = tool_value(
                    await client.call_tool("get_workflow_status", {"run_id": run_id})
                )
                _write_json(directory / (name + ".observation.json"), job)
            if job.get("status") not in {"complete", "failed", "cancelled", "timeout"}:
                unknown = True
                cancellation = tool_value(
                    await client.call_tool("cancel_workflow", {"run_id": run_id})
                )
                _write_json(directory / (name + ".cancel.json"), cancellation)
                raise TimeoutError(
                    "Original workflow was cancelled after timeout/unknown outcome; observe its run_id, never replay it."
                )
            result = job.get("result", job)
            results[name] = result
            _write_json(directory / (name + ".result.json"), result)
            unknown = any(
                step.get("status") == "unknown" for step in result.get("steps", [])
            )
            if not result["ok"]:
                raise RuntimeError(
                    "Workflow failed or is unknown: "
                    + name
                    + ". Observe the original request; do not replay it."
                )
            return result

        try:
            try:
                model = await run(
                    "01-model",
                    [{"id": "model", "type": "mechanical", "recipe": model_recipe}],
                )
            finally:
                if "01-model" in results:
                    session_id = (
                        results["01-model"]["steps"][0]
                        .get("outputs", {})
                        .get("session", {})
                        .get("id")
                    )
            report = model["steps"][0]["outputs"]["values"]["benchmark"]
            _write_json(directory / "report.json", report)
            steps = []
            for name in specification["results"]:
                steps.append(
                    {
                        "id": name,
                        "type": "dpf",
                        "recipe": {
                            "result_files": [
                                {"path": str(directory / (name + ".rst"))}
                            ],
                            "operations": [
                                {
                                    "op": "result",
                                    "id": "u",
                                    "name": "displacement",
                                    "time_scoping": list(
                                        range(
                                            1,
                                            len(report["cases"][name][axis]) + 1,
                                        )
                                    )
                                    if axis
                                    else 1,
                                    "requested_location": "Nodal",
                                    **(
                                        {"mesh_scoping": report["tip_node_ids"]}
                                        if axis
                                        else {}
                                    ),
                                },
                                {
                                    "op": "export",
                                    "id": "csv",
                                    "source": "u",
                                    "path": str(directory / (name + ".csv")),
                                },
                            ],
                        },
                    }
                )
            await run("02-fields", steps)
            properties, material_file = (
                find_transient_material(directory, parameters["material_name"])
                if "material_name" in parameters
                else find_structural_steel(directory)
            )
            _write_json(directory / "material-properties.json", properties)
            parameters.update(properties)
            parameters["sources"] = {
                "material": str(material_file),
                **{
                    name + "_field": str(directory / (name + ".csv"))
                    for name in specification["results"]
                },
            }
            _write_json(directory / "validation-parameters.json", parameters)
            validation = (
                specification["validator"](
                    report,
                    parameters,
                    specification["curve_reader"](
                        directory / (specification["results"][0] + ".csv"), report
                    ),
                )
                if axis
                else validate_coordinates(
                    report,
                    parameters,
                    nodal_vectors(directory / "global.csv"),
                    nodal_vectors(directory / "rotated.csv"),
                )
            )
            validation["source_geometry_unchanged"] = (
                file_sha256(geometry) == source_hash
            )
            _write_json(
                directory / "source-integrity.json",
                {
                    "path": str(geometry),
                    "sha256_before": source_hash,
                    "sha256_after": file_sha256(geometry),
                    "copy_sha256": file_sha256(destination),
                },
            )
            validation["ok"] = (
                validation["ok"] and validation["source_geometry_unchanged"]
            )
            validation["elapsed_s"] = time.monotonic() - started
            _write_json(directory / "validation.json", validation)
        except BaseException as exc:
            _write_json(
                directory / "failure.json",
                {
                    "ok": False,
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                    "session_id": session_id,
                    "outcome_unknown": unknown,
                },
            )
            raise
        finally:
            if session_id and not unknown:
                await run(
                    "03-close",
                    [
                        {
                            "id": "close",
                            "type": "mechanical",
                            "recipe": {
                                "backend": "workbench",
                                "session": {"action": "close", "id": session_id},
                            },
                        }
                    ],
                )
    if validation["ok"]:
        model_step = results["01-model"]["steps"][0]
        parameters["sources"].update(
            {
                "model_workflow": str(directory / "01-model.result.json"),
                "field_workflow": str(directory / "02-fields.result.json"),
                "closed": str(directory / "03-close.result.json"),
                "installation": str(directory / "installation.json"),
                "source_integrity": str(directory / "source-integrity.json"),
                "geometry_copy": str(destination),
                "script": str(snapshot),
                **{
                    name + "_result": str(directory / (name + ".rst"))
                    for name in specification["results"]
                },
            }
        )
        _write_json(directory / "receipt-parameters.json", parameters)
        _write_json(directory / "mechanical.envelope.json", model_step)

        def bound(path):
            path = require_allowed_file(path)
            return {"path": str(path), "sha256": file_sha256(path)}

        receipt = {
            "version": "1",
            "product": "mechanical",
            "backend": "workbench",
            "product_version": model_step["outputs"]["session"]["version"],
            "validator": case,
            "case_id": case,
            "run_id": results["01-model"]["run_id"],
            "scope_origin": "run",
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "installation_fingerprint": installation["installation_fingerprint"],
            "report": bound(directory / "report.json"),
            "validation": bound(directory / "validation.json"),
            "envelope": bound(directory / "mechanical.envelope.json"),
            "parameters": bound(directory / "receipt-parameters.json"),
            "artifacts": [
                bound(path) for path in dict.fromkeys(parameters["sources"].values())
            ],
        }
        _write_json(directory / "receipt.json", receipt)
        imported = import_receipt(directory / "receipt.json", apply=register)
        _write_json(directory / "receipt-check.json", imported)
    print(json.dumps(validation, indent=2))
    return validation["ok"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--geometry-path",
        type=Path,
        required=True,
        help="Existing benchmark STEP; only a new copy is used",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--case", choices=tuple(CASE_SPECS), default="coordinate_equivalence"
    )
    parser.add_argument(
        "--register-evidence",
        action="store_true",
        help="Import the independently rechecked receipt after confirmed shutdown",
    )
    args = parser.parse_args()
    if not args.execute:
        parser.error(
            "Use --execute to authorize real Workbench/Mechanical/DPF startup."
        )
    source = require_allowed_file(args.geometry_path)
    directory = require_allowed_path(
        args.output or Path.cwd() / ".ansys_mcp/mechanical_basics" / uuid.uuid4().hex
    )
    directory.mkdir(parents=True, exist_ok=False)
    return (
        0
        if asyncio.run(execute(directory, source, args.register_evidence, args.case))
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
