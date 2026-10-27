"""Pure, product-free numerical acceptance checks shared by runners and receipts."""
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any


def validate_contact(report: dict[str, Any], parameters: dict[str, Any]) -> dict[str, Any]:
    if any(type(parameters.get(key)) not in (int, float) or not math.isfinite(parameters[key]) or parameters[key] <= 0
           for key in ("width_mm", "height_mm", "pressure_pa")):
        return {"passed": False, "checks": {"positive_finite_inputs": False}}
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


def validate_specialized(report: dict[str, Any]) -> dict[str, Any]:
    """Independently gate reported solve, retained export, and basic motion."""
    case = report.get("case")
    checks: dict[str, bool] = {}
    checks["solved"] = report.get("status") == "solved" and "done" in report.get("solution_status", "").lower()
    phases = report.get("progress", [])
    checks["stages"] = all(phase in phases for phase in ("before_solve", "after_solve", "after_result_evaluation", "after_result_export", "complete"))
    table = Path(report["result_table"]) if report.get("result_table") else None
    checks["export_exists"] = bool(table and table.is_file() and table.stat().st_size > 0)
    result_file = Path(report["result_file"]) if report.get("result_file") else None
    checks["result_file_exists_after_save"] = bool(result_file and result_file.is_file() and result_file.stat().st_size > 0)
    displacement = report.get("maximum_deformation_m")
    checks["finite_displacement"] = type(displacement) in (int, float) and math.isfinite(displacement) and displacement > 0
    if case == "rigid":
        expected = report.get("driver", {}).get("speed_m_s", 0) * report.get("end_time_s", 0)
        checks["driven_joint"] = bool(report.get("joint", {}).get("mobile_ids")) and "translational" in report.get("joint", {}).get("type", "").lower()
        checks["prescribed_motion"] = bool(checks["finite_displacement"] and expected > 0 and abs(displacement / expected - 1) <= 0.01)
    elif case in {"explicit", "lsdyna"}:
        initial = report.get("initial_velocity_m_s", [0, 0, 0])
        speed = -initial[2] if isinstance(initial, list) and len(initial) == 3 else 0
        end_time = report.get("end_time_s", 0)
        gap = report.get("geometry", {}).get("gap_m", 0)
        checks["low_speed_after_contact"] = 0 < speed <= 0.1 and gap > 0 and end_time > gap / speed
        checks["mesh"] = report.get("mesh", {}).get("elements", 0) > 0
        checks["frictionless_contact"] = "frictionless" in report.get("contact", {}).get("type", "").lower()
        checks["no_enabled_exposed_erosion_controls"] = not any(report.get("erosion", {}).values())
        # Free flight without contact must fail this check (not merely complete).
        checks["contact_changed_motion"] = bool(checks["finite_displacement"] and speed > 0 and displacement < 0.8 * speed * end_time)
    else:
        checks["known_case"] = False
    not_claimed = ["energy_convergence", "mesh_convergence", "material_failure"]
    if case in {"explicit", "lsdyna"} and not report.get("erosion"):
        not_claimed.append("unexposed_erosion_controls")
    return {"ok": all(checks.values()), "case": case, "checks": checks,
            "level": "product_solve_and_motion_sanity", "not_claimed": not_claimed}


def analytic_references(length_m: float, width_m: float, height_m: float,
                        young_pa: float, density_kg_m3: float, conductivity_w_mk: float) -> dict[str, float]:
    if min(length_m, width_m, height_m, young_pa, density_kg_m3, conductivity_w_mk) <= 0:
        raise ValueError("Benchmark dimensions and material properties must be positive.")
    inertia = width_m * height_m**3 / 12
    area = width_m * height_m
    return {"static_tip_m": length_m**3 / (3 * young_pa * inertia),
            "modal_first_hz": 1.875104068711961**2 / (2 * math.pi * length_m**2) * math.sqrt(young_pa * inertia / (density_kg_m3 * area)),
            "thermal_flux_w_m2": conductivity_w_mk * 100 / length_m}


def validate_core_scalars(report: dict[str, Any], parameters: dict[str, Any]) -> dict[str, Any]:
    fields = ("length_m", "width_m", "height_m", "young_pa", "density_kg_m3", "conductivity_w_mk")
    if any(type(parameters.get(key)) not in (int, float) or not math.isfinite(parameters[key]) for key in fields):
        raise ValueError("Beam references require finite SI dimensions and material properties.")
    refs = analytic_references(*(parameters[key] for key in fields))
    requested = report.get("requested_cases")
    if not isinstance(requested, list) or not requested or set(requested) - {"static", "modal", "thermal"}:
        raise ValueError("Beam receipt must declare its requested cases.")
    checks = {}
    for name in requested:
        case = report.get("cases", {}).get(name, {})
        if case.get("status") != "solved":
            checks[name] = {"status": "failed"}
            continue
        if name == "static":
            error = abs(case["maximum_deformation_m"] / refs["static_tip_m"] - 1)
            passed = error <= .05 and abs(case["reaction_y_n"] - 1) <= .01
        elif name == "modal":
            error = abs(min(case["frequencies_hz"]) / refs["modal_first_hz"] - 1)
            passed = error <= .05
        else:
            error = max(abs(case["average_heat_flux_w_m2"] / refs["thermal_flux_w_m2"] - 1),
                        abs(case["minimum_temperature_c"]) / 100, abs(case["maximum_temperature_c"] - 100) / 100)
            passed = error <= .01
        checks[name] = {"status": "passed" if passed else "failed", "relative_error": error}
    return {"ok": all(item["status"] == "passed" for item in checks.values()),
            "checks": checks, "analytic_references": refs, "scope": "reported_scalar_results_only"}
