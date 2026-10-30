"""Pure numerical and retained-field checks for Mechanical acceptance packs."""

from __future__ import annotations

import csv
import json
import math
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


class MissingMaterialProperties(ValueError):
    pass


def material_constants(
    source: Path,
    material_name: str,
    expected: dict,
    *,
    allow_zero: tuple[str, ...] = (),
) -> dict[str, float]:
    try:
        document = ET.parse(source).getroot()
    except ET.ParseError as exc:
        raise ValueError("Malformed saved Engineering Data XML.") from exc
    details = {
        node.attrib["id"]: node for node in document.findall(".//ParameterDetails")
    }
    for material in document.findall(".//Material"):
        bulk = material.find("BulkDetails")
        if bulk is None or bulk.findtext("Name") != material_name:
            continue
        values = {}
        for parameter in bulk.findall(".//ParameterValue"):
            definition = details.get(parameter.get("parameter"))
            name = definition.findtext("Name") if definition is not None else None
            if name not in expected:
                continue
            unit = next(
                (
                    node.text or ""
                    for node in parameter.findall("Qualifier")
                    if node.get("name") == "Units"
                ),
                "",
            ).strip()
            if not unit:
                unit = " ".join(
                    (node.findtext("Name") or "")
                    + (
                        "^" + node.get("power")
                        if node.get("power") not in {None, "1"}
                        else ""
                    )
                    for node in definition.findall("Units/Unit")
                )
            if unit not in expected[name][1]:
                raise ValueError(
                    "Material property must use the benchmark SI unit: " + name
                )
            value = float(parameter.findtext("Data", ""))
            if (
                not math.isfinite(value)
                or value < 0
                or (value == 0 and expected[name][0] not in allow_zero)
            ):
                raise ValueError(
                    "Material properties must be finite positive constants."
                )
            values[expected[name][0]] = value
        if len(values) == len(expected):
            return values
    raise MissingMaterialProperties(
        "Saved Engineering Data does not prove the benchmark material constants."
    )


def structural_steel_properties(source: Path) -> dict[str, float]:
    return material_constants(
        source,
        "Structural Steel",
        {
            "Young's Modulus": ("young_pa", {"Pa"}),
            "Density": ("density_kg_m3", {"kg m^-3"}),
            "Thermal Conductivity": (
                "conductivity_w_mk",
                {"W m^-1 C^-1", "W m^-1 K^-1"},
            ),
        },
    )


def find_structural_steel(directory: Path) -> tuple[dict[str, float], Path]:
    for source in [
        *directory.rglob("EngineeringData.xml"),
        *directory.rglob("MatML.xml"),
    ]:
        try:
            return structural_steel_properties(source), source
        except MissingMaterialProperties:
            continue
    raise ValueError("No saved SI Structural Steel material table was found.")


def material_properties(directory: Path) -> dict[str, float]:
    return find_structural_steel(directory)[0]


def nodal_vectors(path: Path) -> dict[int, list[float]]:
    values = {}
    with path.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            if (
                row["unit"] != "m"
                or row["location"] != "Nodal"
                or row["field_index"] != "0"
                or row["entity_row"] != "0"
            ):
                raise ValueError(
                    "Coordinate comparison requires one nodal displacement field in metres."
                )
            key = int(row["entity_id"])
            vector = [float(row["component_" + str(i)]) for i in range(3)]
            if key in values or not all(math.isfinite(v) for v in vector):
                raise ValueError("Duplicate or nonfinite nodal displacement.")
            values[key] = vector
    if not values:
        raise ValueError("Empty nodal displacement field.")
    return values


def validate_coordinates(
    report: dict[str, Any],
    parameters: dict[str, Any],
    global_field: dict[int, list[float]],
    rotated_field: dict[int, list[float]],
) -> dict[str, Any]:
    checks: dict[str, bool] = {"product_complete": report.get("status") == "complete"}
    metrics: dict[str, float] = {}
    try:

        def finite(value):
            if isinstance(value, dict):
                return all(finite(v) for v in value.values())
            if isinstance(value, list):
                return all(finite(v) for v in value)
            return not isinstance(value, float) or math.isfinite(value)

        if not finite([report, parameters, global_field, rotated_field]):
            raise ValueError("Nonfinite benchmark data.")
        angle = math.radians(parameters["angle_deg"])
        c, s = math.cos(angle), math.sin(angle)
        checks["geometry_readback"] = (
            report["geometry"]["body_count"] == 1
            and report["geometry"]["material"] == "Structural Steel"
            and all(
                abs(report["geometry"][key] - parameters[key]) < 1e-9
                for key in ("length_m", "width_m", "height_m")
            )
        )
        basis = report["coordinate"]["basis"]
        expected = [[c, s, 0], [-s, c, 0], [0, 0, 1]]
        checks["nontrivial_rotation"] = 5 <= abs(parameters["angle_deg"]) <= 85
        checks["coordinate_basis"] = (
            len(basis) == 3
            and all(len(row) == 3 for row in basis)
            and max(
                abs(basis[i][j] - expected[i][j]) for i in range(3) for j in range(3)
            )
            < 1e-8
        )
        checks["coordinate_angle"] = (
            abs(report["coordinate"]["angle_deg"] - parameters["angle_deg"]) < 1e-8
        )
        checks["one_transformation"] = report["coordinate"]["transformation_count"] == 1
        checks["coordinate_origin"] = (
            len(report["coordinate"]["origin_m"]) == 3
            and max(
                abs(a - b)
                for a, b in zip(
                    report["coordinate"]["origin_m"], parameters["origin_m"]
                )
            )
            < 1e-10
        )
        selections = report["selections"]
        root, tip = set(selections["root"]), set(selections["tip"])
        checks["named_selections"] = (
            len(root) == len(tip) == 1
            and root.isdisjoint(tip)
            and root == set(selections["expected_root"])
            and tip == set(selections["expected_tip"])
        )
        checks["selection_object_ids"] = (
            selections["root_object_id"] != selections["tip_object_id"]
        )
        for name in ("global", "rotated"):
            case = report["cases"][name]
            inputs = case["inputs"]
            force = inputs["force_n"]
            if len(force) != 3:
                raise ValueError("Readback must contain three force components.")
            transformed = (
                force
                if name == "global"
                else [sum(basis[j][i] * force[j] for j in range(3)) for i in range(3)]
            )
            checks[name + "_load"] = (
                max(abs(a - b) for a, b in zip(transformed, [0, -1, 0])) < 1e-8
            )
            checks[name + "_scoping"] = (
                set(inputs["root_ids"]) == root and set(inputs["tip_ids"]) == tip
            )
            checks[name + "_resources"] = (
                inputs["max_cores"] == 4 and inputs["distributed"] is False
            )
            checks[name + "_solved"] = case["status"] == "solved"
            checks[name + "_reaction"] = (
                len(case["reaction_n"]) == 3
                and max(abs(a - b) for a, b in zip(case["reaction_n"], [0, 1, 0]))
                < 0.01
            )
        checks["rotated_load_uses_frame"] = (
            report["cases"]["rotated"]["inputs"]["force_coordinate_id"]
            == report["coordinate"]["id"]
        )
        checks["distinct_global_frame"] = (
            report["cases"]["global"]["inputs"]["force_coordinate_id"]
            != report["coordinate"]["id"]
        )
        checks["mesh"] = report["mesh"]["elements"] > 0
        checks["node_ids"] = (
            bool(global_field)
            and set(global_field) == set(rotated_field)
            and len(global_field) == report["mesh"]["nodes"]
        )
        if not checks["node_ids"]:
            return {"ok": False, "checks": checks, "metrics": metrics}
        if not all(
            len(v) == 3
            for field in (global_field, rotated_field)
            for v in field.values()
        ):
            raise ValueError("Displacements must contain all three components.")
        peak = max(math.sqrt(sum(x * x for x in v)) for v in global_field.values())
        if peak <= 0:
            raise ValueError("A zero displacement field cannot validate equivalence.")
        metrics["vector_max_error_over_peak"] = (
            max(
                math.sqrt(
                    sum(
                        (a - b) ** 2
                        for a, b in zip(global_field[key], rotated_field[key])
                    )
                )
                for key in global_field
            )
            / peak
        )
        checks["nodal_vector_equivalence"] = (
            metrics["vector_max_error_over_peak"] <= 1e-6
        )
        length, width, height, young = (
            parameters[key] for key in ("length_m", "width_m", "height_m", "young_pa")
        )
        if min(length, width, height, young) <= 0:
            raise ValueError("Positive SI geometry and material properties required.")
        reference = length**3 / (3 * young * (width * height**3 / 12))
        metrics["analytic_tip_m"] = reference
        metrics["global_peak_m"] = peak
        metrics["beam_relative_error"] = abs(peak / reference - 1)
        checks["independent_beam_reference"] = metrics["beam_relative_error"] <= 0.05
        checks["mechanical_scalar_readback"] = all(
            abs(
                report["cases"][name]["maximum_deformation_m"]
                / max(math.sqrt(sum(x * x for x in v)) for v in field.values())
                - 1
            )
            <= 0.001
            for name, field in (("global", global_field), ("rotated", rotated_field))
        )
        return {
            "ok": all(checks.values()),
            "checks": checks,
            "metrics": metrics,
            "scope": "rotated_coordinate_named_selection_static_equivalence_only",
        }
    except (
        KeyError,
        ValueError,
        TypeError,
        IndexError,
        ZeroDivisionError,
        OverflowError,
    ) as exc:
        return {
            "ok": False,
            "checks": {**checks, "valid_data": False},
            "metrics": metrics,
            "error": str(exc),
        }


def validate_basic_receipt(
    receipt: Any, report: dict, parameters: dict, resolved: dict[str, Path], base: Path
) -> dict:
    from .config import require_allowed_file
    from .acceptance_evidence import file_sha256

    def source(role):
        path = require_allowed_file(parameters["sources"][role], base=base)
        if path not in set(resolved.values()):
            raise ValueError("Coordinate source must be hash-bound: " + role)
        return path

    def read(role):
        return json.loads(source(role).read_text(encoding="utf-8"))

    transient = receipt.validator == "transient_sdof"
    if transient:
        from .transient_benchmark import (
            transient_material,
            transient_curve,
            validate_transient,
        )

        properties = transient_material(source("material"), parameters["material_name"])
        checked = validate_transient(
            report,
            {**parameters, **properties},
            transient_curve(source("transient_field"), report),
        )
        names = ("transient",)
    else:
        properties = structural_steel_properties(source("material"))
        checked = validate_coordinates(
            report,
            {**parameters, **properties},
            nodal_vectors(source("global_field")),
            nodal_vectors(source("rotated_field")),
        )
        names = ("global", "rotated")
    checks = checked["checks"]
    checks["material_constants"] = all(
        parameters[key] == value for key, value in properties.items()
    )
    model = read("model_workflow")
    closed = read("closed")
    fields = read("field_workflow")
    checks["completed_model_fields_close"] = all(
        value.get("ok") is True for value in (model, closed, fields)
    )
    checks["original_product_report"] = (
        model["steps"][0]["outputs"]["values"]["benchmark"] == report
    )
    model_session = model["steps"][0]["outputs"]["session"]
    closed_session = closed["steps"][0]["outputs"]["session"]
    checks["same_session_confirmed_closed"] = (
        model_session["id"] == closed_session["id"] and closed_session["closed"] is True
    )
    checks["recorded_installation_at_run"] = (
        read("installation")["installation_fingerprint"]
        == receipt.installation_fingerprint
        and receipt.scope_origin == "run"
    )
    integrity = read("source_integrity")
    checks["source_geometry_preserved"] = (
        integrity["sha256_before"]
        == integrity["sha256_after"]
        == integrity["copy_sha256"]
        == file_sha256(source("geometry_copy"))
    )
    for name in names:
        result = source(name + "_result")
        checks[name + "_native_result"] = (
            result.stat().st_size > 0
            and require_allowed_file(report["cases"][name]["result_file"]) == result
        )
        field = source(name + "_field")
        step = next(value for value in fields["steps"] if value["id"] == name)
        exported = step["outputs"]["values"]["csv"]
        checks[name + "_original_dpf_export"] = (
            step["ok"] is True
            and exported["sha256"] == file_sha256(field)
            and exported["size_bytes"] == field.stat().st_size
            and require_allowed_file(exported["path"]) == field
        )
    checks["script_snapshot_nonempty"] = source("script").stat().st_size > 0
    checked["ok"] = all(checks.values())
    return checked
