"""Read-only validation of a mass-dominated, undamped axial transient model."""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path

from .mechanical_benchmarks import material_constants, MissingMaterialProperties


def transient_material(source: Path, name: str) -> dict:
    return material_constants(
        source,
        name,
        {
            "Young's Modulus": ("young_pa", {"Pa"}),
            "Density": ("density_kg_m3", {"kg m^-3"}),
            "Poisson's Ratio": (
                "poisson_ratio",
                {"", "1", "unitless", "dimensionless"},
            ),
        },
        allow_zero=("poisson_ratio",),
    )


def find_transient_material(directory: Path, name: str) -> tuple[dict, Path]:
    for source in [
        *directory.rglob("EngineeringData.xml"),
        *directory.rglob("MatML.xml"),
    ]:
        try:
            return transient_material(source, name), source
        except MissingMaterialProperties:
            continue
    raise ValueError("The saved transient material is unavailable.")


def transient_curve(path: Path, report: dict) -> list[dict]:
    times = report["cases"]["transient"]["times_s"]
    nodes = set(report["tip_node_ids"])
    if not times or not nodes or len(nodes) != len(report["tip_node_ids"]):
        raise ValueError("A nonempty time axis and unique tip node IDs are required.")
    values = {i: {} for i in range(len(times))}
    with path.open(newline="", encoding="utf-8-sig") as stream:
        for row in csv.DictReader(stream):
            index, node = int(row["field_index"]), int(row["entity_id"])
            label = json.loads(row["label_space"])
            if (
                row["location"] != "Nodal"
                or row["unit"] != "m"
                or row["entity_row"] != "0"
                or index not in values
                or node not in nodes
                or label.get("time") != index + 1
            ):
                raise ValueError(
                    "DPF transient samples must match native time-set/node IDs in SI units."
                )
            vector = [float(row["component_" + str(j)]) for j in range(3)]
            if node in values[index] or not all(math.isfinite(v) for v in vector):
                raise ValueError("Duplicate or nonfinite transient sample.")
            values[index][node] = vector
    samples = []
    for index, time in enumerate(times):
        if set(values[index]) != nodes:
            raise ValueError("A native time set is missing tip-node samples.")
        vectors = list(values[index].values())
        xs = [value[0] for value in vectors]
        samples.append(
            {
                "time_s": time,
                "x_m": sum(xs) / len(xs),
                "tip_spread_m": max(xs) - min(xs),
                "transverse_m": max(
                    math.hypot(value[1], value[2]) for value in vectors
                ),
            }
        )
    return samples


def validate_transient(report: dict, parameters: dict, samples: list[dict]) -> dict:
    checks, metrics = {}, {}
    try:

        def finite(value):
            if isinstance(value, dict):
                return all(finite(v) for v in value.values())
            if isinstance(value, list):
                return all(finite(v) for v in value)
            return not isinstance(value, float) or math.isfinite(value)

        if not finite([report, parameters, samples]):
            raise ValueError("Nonfinite transient data.")
        length, width, height, young, mass, force, dt, end = [
            parameters[key]
            for key in (
                "length_m",
                "width_m",
                "height_m",
                "young_pa",
                "mass_kg",
                "force_n",
                "dt_s",
                "end_time_s",
            )
        ]
        if min(length, width, height, young, mass, force, dt, end) <= 0:
            raise ValueError("Positive SI parameters required.")
        stiffness = young * width * height / length
        omega = math.sqrt(stiffness / mass)
        period = 2 * math.pi / omega
        static = force / stiffness
        checks["complete"] = (
            report["status"] == "complete"
            and report["cases"]["transient"]["status"] == "solved"
        )
        checks["geometry"] = all(
            abs(report["geometry"][key] - parameters[key]) < 1e-9
            for key in ("length_m", "width_m", "height_m")
        )
        checks["material"] = (
            report["geometry"]["material"] == parameters["material_name"]
            and parameters["poisson_ratio"] == 0
        )
        checks["mass"] = (
            math.isclose(report["mass"]["point_kg"], mass, rel_tol=1e-10)
            and "Rigid" in report["mass"]["behavior"]
            and report["mass"]["location_ids"] == report["geometry"]["tip_ids"]
        )
        expected_body_mass = parameters["density_kg_m3"] * length * width * height
        checks["body_mass"] = (
            math.isclose(report["mass"]["body_kg"], expected_body_mass, rel_tol=1e-5)
            and 0 < expected_body_mass / mass <= 1e-3
        )
        checks["mass_location"] = (
            max(
                abs(a - b)
                for a, b in zip(
                    report["mass"]["position_m"], report["mass"]["tip_center_m"]
                )
            )
            < 1e-10
        )
        controls = report["controls"]
        checks["time_controls"] = (
            math.isclose(controls["dt_s"], dt, rel_tol=1e-10)
            and math.isclose(controls["end_time_s"], end, rel_tol=1e-10)
            and controls["automatic_time_stepping"] == "Off"
            and controls["store_results_at"] == "AllTimePoints"
        )
        checks["undamped_linear_inertia"] = (
            controls["time_integration"] is True
            and controls["large_deflection"] is False
            and all(
                controls[key] == 0
                for key in ("mass_damping", "stiffness_damping", "numerical_damping")
            )
        )
        checks["resources"] = (
            controls["max_cores"] == 4 and controls["distributed"] is False
        )
        applied = report["force"]
        checks["step_force"] = (
            applied["times_s"] == [0, end]
            and applied["x_n"] == [force, force]
            and applied["y_n"] == applied["z_n"] == [0, 0]
        )
        checks["resolved_time_window"] = (
            end >= 2 * period and dt <= period / 100 and len(samples) >= 200
        )
        times = [sample["time_s"] for sample in samples]
        native = report["cases"]["transient"]["times_s"]
        checks["native_time_axis"] = (
            times == native
            and 0 <= times[0] <= dt * 1.01
            and abs(times[-1] - end) <= dt * 1e-5
            and all(
                math.isclose(b - a, dt, rel_tol=1e-5, abs_tol=dt * 1e-8)
                for a, b in zip(times, times[1:])
            )
        )
        reference = [static * (1 - math.cos(omega * t)) for t in times]
        errors = [sample["x_m"] - value for sample, value in zip(samples, reference)]
        l2 = math.sqrt(
            sum(error * error for error in errors)
            / sum(value * value for value in reference)
        )
        maximum = max(abs(error) for error in errors) / (2 * static)
        metrics.update(
            stiffness_n_per_m=stiffness,
            period_s=period,
            static_displacement_m=static,
            relative_l2_error=l2,
            max_error_over_analytic_peak=maximum,
            samples=len(samples),
            body_to_point_mass_ratio=expected_body_mass / mass,
        )
        checks["analytic_time_response"] = l2 <= 0.05 and maximum <= 0.05
        peak = max(sample["x_m"] for sample in samples)
        checks["oscillating_not_static"] = (
            1.5 * static < peak < 2.2 * static
            and min(sample["x_m"] for sample in samples) < 0.2 * static
        )
        checks["axial_uniform_motion"] = (
            max(sample["tip_spread_m"] for sample in samples) <= 2 * static * 0.001
            and max(sample["transverse_m"] for sample in samples) <= 2 * static * 0.001
        )
        peaks = [
            times[i]
            for i in range(1, len(samples) - 1)
            if samples[i]["x_m"] > samples[i - 1]["x_m"]
            and samples[i]["x_m"] >= samples[i + 1]["x_m"]
            and samples[i]["x_m"] > 1.5 * static
        ]
        checks["period"] = (
            len(peaks) >= 2 and abs((peaks[1] - peaks[0]) / period - 1) <= 0.02
        )
        if len(peaks) >= 2:
            metrics["measured_period_s"] = peaks[1] - peaks[0]
        checks["mechanical_last_readback"] = (
            abs(
                report["cases"]["transient"]["last_displacement_m"] - samples[-1]["x_m"]
            )
            <= 2 * static * 0.001
        )
        return {
            "ok": all(checks.values()),
            "checks": checks,
            "metrics": metrics,
            "scope": "undamped_mass_dominated_axial_transient_only",
        }
    except (
        KeyError,
        IndexError,
        TypeError,
        ValueError,
        ZeroDivisionError,
        OverflowError,
    ) as exc:
        return {
            "ok": False,
            "checks": {**checks, "valid_data": False},
            "metrics": metrics,
            "error": str(exc),
        }
