"""Fixed references and retained-field validation for six basic analysis cases."""

from __future__ import annotations

import csv
import json
import math
import re
from pathlib import Path

from .mechanical_benchmarks import material_constants, MissingMaterialProperties


CASE_FIELDS = {
    "response_spectrum": {
        "modal": ("rst", "displacement"),
        "response": ("rst", "displacement"),
    },
    "random_vibration": {
        "modal": ("rst", "displacement"),
        "random": ("rst", "displacement"),
    },
    "buckling": {
        "prestress": ("rst", "displacement"),
        "buckling": ("rst", "displacement"),
    },
    "transient_heat": {"heat": ("rth", "temperature")},
    "acoustic_cavity": {"acoustic": ("rst", "pressure")},
    "thermal_expansion": {
        "thermal": ("rth", "temperature"),
        "expansion": ("rst", "displacement"),
        "transferred": ("rst", "structural_temperature"),
    },
}
PARAMETERS = {
    "response_spectrum": {
        "mass_kg": 1000.0,
        "damping_ratio": 0.05,
        "spectrum_hz": [20.0, 200.0],
        "spectrum_values": [1.0, 1.0],
    },
    "random_vibration": {
        "mass_kg": 1000.0,
        "damping_ratio": 0.05,
        "spectrum_hz": [20.0, 120.0],
        "spectrum_values": [0.01, 0.01],
    },
    "buckling": {"compression_n": 1.0, "mesh_size_m": 0.002},
    "transient_heat": {
        "hot_c": 100.0,
        "end_time_s": 100.0,
        "dt_s": 0.25,
        "comparison_start_s": 10.0,
    },
    "acoustic_cavity": {"sound_speed_m_s": 343.0, "density_kg_m3": 1.2},
    "thermal_expansion": {"temperature_c": 72.0, "reference_c": 22.0},
}
TEMPLATES = {
    "response_spectrum": "Modal (ANSYS)",
    "random_vibration": "Modal (ANSYS)",
    "buckling": "Static Structural (ANSYS)",
    "transient_heat": "Transient Thermal (ANSYS)",
    "acoustic_cavity": "Modal (ANSYS)",
    "thermal_expansion": "Steady-State Thermal (ANSYS)",
}


def material_expected(case):
    density = {"Density": ("density_kg_m3", {"kg m^-3"})}
    if case == "acoustic_cavity":
        return {**density, "Speed of Sound": ("sound_speed_m_s", {"m s^-1"})}
    result = {
        **density,
        "Young's Modulus": ("young_pa", {"Pa"}),
        "Poisson's Ratio": ("poisson_ratio", {"", "1", "unitless", "dimensionless"}),
    }
    if case in {"transient_heat", "thermal_expansion"}:
        result.update(
            {
                "Thermal Conductivity": (
                    "conductivity_w_mk",
                    {"W m^-1 C^-1", "W m^-1 K^-1"},
                ),
                "Specific Heat": (
                    "specific_heat_j_kgk",
                    {"J kg^-1 C^-1", "J kg^-1 K^-1"},
                ),
                "Coefficient of Thermal Expansion": (
                    "expansion_per_k",
                    {"C^-1", "K^-1"},
                ),
            }
        )
    return result


def read_material(path, parameters):
    return material_constants(
        path,
        parameters["material_name"],
        material_expected(parameters["case"]),
        allow_zero=("poisson_ratio",),
    )


def find_material(directory, parameters):
    for path in [
        *directory.rglob("EngineeringData.xml"),
        *directory.rglob("MatML.xml"),
    ]:
        try:
            return read_material(path, parameters), path
        except MissingMaterialProperties:
            continue
    raise ValueError("The saved benchmark material is missing.")


def read_fields(path: Path, result_name: str):
    fields = {}
    with path.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            labels = json.loads(row["label_space"])
            if (
                set(labels) != {"time"}
                or type(labels["time"]) is not int
                or labels["time"] < 1
            ):
                raise ValueError("Expected a real native result with explicit set IDs.")
            time_id, node = labels["time"], int(row["entity_id"])
            expected = {
                "displacement": {"m"},
                "temperature": {"C", "degC", "K"},
                "pressure": {"Pa"},
                "structural_temperature": {"C", "degC", "K"},
            }[result_name]
            if (
                row["location"] != "Nodal"
                or row["unit"] not in expected
                or row["entity_row"] != "0"
            ):
                raise ValueError("Native field location, unit or entity-row mismatch.")
            count = 3 if result_name == "displacement" else 1
            vector = [float(row["component_" + str(i)]) for i in range(count)]
            if (
                result_name in {"temperature", "structural_temperature"}
                and row["unit"] == "K"
            ):
                vector[0] -= 273.15
            nodes = fields.setdefault(time_id, {})
            if node in nodes or not all(math.isfinite(v) for v in vector):
                raise ValueError("Duplicate or nonfinite native sample.")
            nodes[node] = vector
    if not fields:
        raise ValueError("Empty native field export.")
    return fields


def read_table(path):
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = csv.reader(f, delimiter="\t")
        header = next(rows)
        if len(header) != 2 or "Node" not in header[0] or "(m)" not in header[1]:
            raise ValueError(
                "Expected a native directional-displacement table in metres."
            )
        result = {}
        for row in rows:
            if not row:
                continue
            node, value = int(row[0]), float(row[1])
            if node in result or not math.isfinite(value):
                raise ValueError("Duplicate or nonfinite native table row.")
            result[node] = value
    if not result:
        raise ValueError("Empty native result table.")
    return result


def rms_reference(parameters, subdivisions=20000):
    low, high = parameters["spectrum_hz"]
    s0, s1 = parameters["spectrum_values"]
    if s0 != s1 or min(low, high, s0, parameters["damping_ratio"]) <= 0 or high <= low:
        raise ValueError(
            "This reference requires a positive, one-sided flat acceleration PSD."
        )
    stiffness = (
        parameters["young_pa"]
        * parameters["width_m"]
        * parameters["height_m"]
        / parameters["length_m"]
    )
    omega_n = math.sqrt(stiffness / parameters["mass_kg"])

    def integrand(f):
        w = 2 * math.pi * f
        return s0 / (
            (omega_n * omega_n - w * w) ** 2
            + (2 * parameters["damping_ratio"] * omega_n * w) ** 2
        )

    step = (high - low) / subdivisions
    total = integrand(low) + integrand(high)
    total += sum(
        (4 if i % 2 else 2) * integrand(low + i * step) for i in range(1, subdivisions)
    )
    return math.sqrt(step * total / 3)


def modal_participation(path):
    rows = []
    active = False
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "PARTICIPATION FACTOR CALCULATION" in line:
            if active:
                break
            active = bool(re.search(r"\*\*\*\*\*\s+X\s+DIRECTION", line))
            continue
        values = line.split()
        if active and len(values) == 8 and values[0].isdigit():
            numbers = [float(v.replace("D", "E")) for v in values[1:]]
            if not all(math.isfinite(v) for v in numbers):
                raise ValueError("Nonfinite modal participation.")
            rows.append(
                {
                    "mode": int(values[0]),
                    "frequency_hz": numbers[0],
                    "factor": numbers[2],
                    "effective_mass_kg": numbers[4],
                    "mass_fraction": numbers[6],
                }
            )
    if not rows:
        raise ValueError("The native X participation table is missing.")
    return max(rows, key=lambda row: row["effective_mass_kg"])


def temperature_reference(x, time, parameters):
    length = parameters["length_m"]
    diffusivity = parameters["conductivity_w_mk"] / (
        parameters["density_kg_m3"] * parameters["specific_heat_j_kgk"]
    )
    ratio = x / length
    value = ratio + sum(
        2
        * ((-1) ** n)
        / (n * math.pi)
        * math.sin(n * math.pi * ratio)
        * math.exp(-diffusivity * (n * math.pi / length) ** 2 * time)
        for n in range(1, 101)
    )
    return parameters["hot_c"] * value


def validate_remaining(report, parameters, paths):
    checks, metrics = {}, {}
    try:
        case = parameters["case"]

        def finite(v):
            if isinstance(v, dict):
                return all(finite(x) for x in v.values())
            if isinstance(v, list):
                return all(finite(x) for x in v)
            return not isinstance(v, float) or math.isfinite(v)

        if not finite([report, parameters]):
            raise ValueError("Nonfinite benchmark input.")
        checks["complete"] = (
            report["status"] == "complete"
            and report["case"] == case
            and all(
                report["cases"][name]["status"] == "solved"
                for name in CASE_FIELDS[case]
            )
        )
        checks["geometry_material"] = (
            all(
                abs(report["geometry"][k] - parameters[k]) < 1e-9
                for k in ("length_m", "width_m", "height_m")
            )
            and report["geometry"]["material"] == parameters["material_name"]
        )
        checks["resources"] = bool(report["controls"]) and all(
            v["max_cores"] == 4 and v["distributed"] is False
            for v in report["controls"].values()
        )
        checks["mesh"] = report["mesh"]["elements"] > 0 and report["mesh"]["nodes"] > 0
        fields = {
            name: read_fields(paths[name], spec[1])
            for name, spec in CASE_FIELDS[case].items()
        }
        checks["native_field_nodes"] = all(
            len(nodes) == report["mesh"]["nodes"]
            for sets in fields.values()
            for nodes in sets.values()
        )
        length, width, height = (
            parameters[k] for k in ("length_m", "width_m", "height_m")
        )
        if min(length, width, height) <= 0:
            raise ValueError("Positive SI dimensions required.")
        if case in {"response_spectrum", "random_vibration"}:
            stiffness = parameters["young_pa"] * width * height / length
            w2 = stiffness / parameters["mass_kg"]
            frequencies = report["cases"]["modal"]["native_axis"]
            spectrum = report["spectrum"]
            mass = report["mass"]
            checks["mass"] = (
                mass["point_kg"] == parameters["mass_kg"]
                and mass["behavior"] == "Rigid"
                and mass["location_ids"] == report["geometry"]["tip_ids"]
                and 0 < mass["body_kg"] / mass["point_kg"] < 1e-3
                and math.isclose(
                    mass["body_kg"],
                    parameters["density_kg_m3"] * length * width * height,
                    rel_tol=1e-5,
                )
            )
            checks["axial_support"] = (
                report["fixed_ids"] == report["geometry"]["root_ids"]
                and report["lateral_constraint"]["enabled"] is False
            )
            participation = modal_participation(paths["log_modal"])
            dominant = participation["mode"]
            axial_frequency = frequencies[dominant - 1]
            checks["modal_frequency"] = (
                len(frequencies) >= 2
                and abs(axial_frequency / (math.sqrt(w2) / (2 * math.pi)) - 1) <= 0.02
                and max(frequencies) > 1.5 * max(parameters["spectrum_hz"])
            )
            mode_tip = [
                fields["modal"][dominant][node][0] for node in report["tip_node_ids"]
            ]
            gamma_tip = participation["factor"] * sum(mode_tip) / len(mode_tip)
            checks["native_participation"] = (
                participation["mass_fraction"] >= 0.99 and abs(gamma_tip - 1) <= 0.01
            )
            metrics.update(
                participation=participation, participation_tip_product=gamma_tip
            )
            checks["spectrum_readback"] = (
                spectrum["frequency_hz"] == parameters["spectrum_hz"]
                and spectrum["values"] == parameters["spectrum_values"]
                and spectrum["direction"] == "XAxis"
                and spectrum["boundary_id"] == spectrum["fixed_id"]
                and spectrum["modal_id"] == spectrum["expected_modal_id"]
                and spectrum["damping_ratio"] == parameters["damping_ratio"]
            )
            name = "response" if case == "response_spectrum" else "random"
            table = read_table(paths["table_" + name])
            checks["native_table_scope"] = set(table) == set(report["tip_node_ids"])
            measured = sum(table.values()) / len(table)
            checks["mechanical_readback"] = math.isclose(
                measured, report["cases"][name]["displacement_m"], rel_tol=1e-4
            )
            native_response = fields[name]
            response_id = 1
            if case == "random_vibration":
                record = report["cases"][name]["result_record"]
                checks["native_sigma_record"] = record == {
                    "load_step": 3,
                    "substep": 1,
                    "iteration": 2,
                } and set(native_response) == {1, 2}
                response_id = 2
            else:
                checks["native_response_record"] = set(native_response) == {1}
            checks["dpf_response_readback"] = response_id in native_response and all(
                math.isclose(
                    native_response[response_id][node][0],
                    value,
                    rel_tol=2e-4,
                    abs_tol=1e-14,
                )
                for node, value in table.items()
            )
            if case == "response_spectrum":
                if parameters["spectrum_values"][0] != parameters["spectrum_values"][1]:
                    raise ValueError("Flat pseudo-acceleration spectrum required.")
                reference = parameters["spectrum_values"][0] / w2
                checks["spectrum_definition"] = (
                    spectrum["unit"] == "m s^-2"
                    and spectrum["combination"] == "SRSS"
                    and spectrum["missing_mass"] is False
                    and spectrum["rigid_response"] is False
                )
            else:
                reference = rms_reference(parameters)
                checks["spectrum_definition"] = (
                    spectrum["unit"] == "m^2 s^-3"
                    and spectrum["mode_significance"] == 0
                    and report["cases"][name]["sigma_scale"] == "Sigma1"
                    and spectrum["constant_damping"] == "Manual"
                    and spectrum["mass_damping"] == spectrum["stiffness_damping"] == 0
                )
                metrics["integration_refinement_error"] = abs(
                    rms_reference(parameters, 10000) / reference - 1
                )
                checks["integral_converged"] = (
                    metrics["integration_refinement_error"] < 1e-6
                )
            metrics.update(
                reference_m=reference,
                measured_m=measured,
                relative_error=abs(measured / reference - 1),
                axial_frequency_hz=axial_frequency,
            )
            checks["independent_reference"] = metrics["relative_error"] <= 0.05
        elif case == "buckling":
            reference = (
                math.pi**2
                * parameters["young_pa"]
                * width
                * height**3
                / (12 * 4 * length**2)
            )
            multiplier = report["cases"]["buckling"]["load_multiplier"]
            measured = multiplier * parameters["compression_n"]
            load = report["buckling"]
            checks["prestress_link"] = (
                load["prestress_id"] == load["static_id"]
                and load["fixed_ids"] == report["geometry"]["root_ids"]
                and load["force_ids"] == report["geometry"]["tip_ids"]
                and load["force_n"] == [-parameters["compression_n"], 0, 0]
            )
            metrics.update(
                reference_critical_n=reference,
                critical_n=measured,
                load_multiplier=multiplier,
                relative_error=abs(measured / reference - 1),
            )
            checks["euler_reference"] = (
                multiplier > 0 and metrics["relative_error"] <= 0.05
            )
        elif case == "transient_heat":
            native = report["cases"]["heat"]["native_axis"]
            settings = report["thermal"]
            checks["thermal_controls"] = (
                settings["initial_c"] == settings["cold_c"] == 0
                and settings["hot_c"] == parameters["hot_c"]
                and settings["dt_s"] == parameters["dt_s"]
                and settings["end_time_s"] == parameters["end_time_s"]
                and settings["auto"] == "Off"
                and settings["store"] == "AllTimePoints"
            )
            checks["thermal_boundaries"] = (
                settings["cold_ids"] == report["geometry"]["root_ids"]
                and settings["hot_ids"] == report["geometry"]["tip_ids"]
            )
            checks["native_time_axis"] = (
                len(native) == round(parameters["end_time_s"] / parameters["dt_s"])
                and set(fields["heat"]) == set(range(1, len(native) + 1))
                and all(
                    math.isclose(t, (i + 1) * parameters["dt_s"], rel_tol=1e-6)
                    for i, t in enumerate(native)
                )
            )
            coords = {int(n): x for n, x in report["node_x_m"].items()}
            checks["coordinate_units"] = (
                len(coords) == report["mesh"]["nodes"]
                and abs(min(coords.values())) < 1e-9
                and math.isclose(max(coords.values()), length, rel_tol=1e-8)
            )
            maximum = 0.0
            count = 0
            reference_cache = {}
            for time_id, nodes in fields["heat"].items():
                time = native[time_id - 1]
                if time < parameters["comparison_start_s"]:
                    continue
                for node, value in nodes.items():
                    key = (round(coords[node], 12), time)
                    if key not in reference_cache:
                        reference_cache[key] = temperature_reference(
                            key[0], time, parameters
                        )
                    error = abs(value[0] - reference_cache[key]) / parameters["hot_c"]
                    maximum = max(maximum, error)
                    count += 1
            metrics.update(
                max_temperature_range_error=maximum,
                compared_samples=count,
                time_sets=len(native),
            )
            checks["analytic_conduction"] = count > 100 and maximum <= 0.01
        elif case == "acoustic_cavity":
            frequencies = report["cases"]["acoustic"]["native_axis"]
            reference = [
                parameters["sound_speed_m_s"] * n / (2 * length) for n in (1, 2, 3)
            ]
            region = report["acoustic"]
            checks["explicit_api_evidence"] = type(
                region["region_created_here"]
            ) is bool and (
                (
                    "Ansys.ACT.Automation.Mechanical.Analysis.AddPhysicsRegion"
                    in report.get("executed_apis", [])
                )
                == region["region_created_here"]
            )
            checks["acoustic_region"] = (
                region["acoustics"] is True
                and region["structural"] is False
                and region["location_ids"] == report["geometry"]["body_ids"]
            )
            errors = [abs(a / b - 1) for a, b in zip(frequencies, reference)]
            metrics.update(
                frequencies_hz=frequencies,
                reference_hz=reference,
                max_frequency_relative_error=max(errors),
            )
            checks["cavity_reference"] = len(frequencies) == 3 and max(errors) <= 0.05
            checks["native_pressure_modes"] = len(fields["acoustic"]) == 3 and all(
                any(abs(v[0]) > 0 for v in values.values())
                for values in fields["acoustic"].values()
            )
        else:
            transfer = report["transfer"]
            reference = (
                parameters["expansion_per_k"]
                * (parameters["temperature_c"] - parameters["reference_c"])
                * length
            )
            checks["actual_temperature_transfer"] = (
                Path(transfer["source_file"])
                == Path(report["cases"]["thermal"]["result_file"])
                and transfer["source_min_c"]
                == transfer["source_max_c"]
                == transfer["temperature_c"]
                == parameters["temperature_c"]
                and transfer["reference_c"] == parameters["reference_c"]
                and transfer["target_ids"] == report["geometry"]["body_ids"]
            )
            checks["thermal_field"] = all(
                abs(v[0] - parameters["temperature_c"]) < 1e-6
                for ns in fields["thermal"].values()
                for v in ns.values()
            )
            checks["mapped_target_temperature"] = all(
                abs(v[0] - parameters["temperature_c"]) < 1e-6
                for ns in fields["transferred"].values()
                for v in ns.values()
            )
            values = fields["expansion"][max(fields["expansion"])]
            tips = [values[n][0] for n in report["tip_node_ids"]]
            measured = sum(tips) / len(tips)
            metrics.update(
                reference_expansion_m=reference,
                measured_m=measured,
                relative_error=abs(measured / reference - 1),
            )
            checks["free_expansion_reference"] = (
                metrics["relative_error"] <= 0.01
                and max(tips) - min(tips) <= reference * 1e-4
            )
            checks["mechanical_readback"] = math.isclose(
                measured, report["cases"]["expansion"]["displacement_m"], rel_tol=1e-4
            )
        return {
            "ok": all(checks.values()),
            "checks": checks,
            "metrics": metrics,
            "scope": case + "_documented_linear_benchmark_only",
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


def specifications():
    specs = {}
    for case, fields in CASE_FIELDS.items():
        thermal = case in {"transient_heat", "thermal_expansion"}
        specs[case] = {
            "script": "mechanical_remaining_acceptance.py",
            "template": TEMPLATES[case],
            "project": case + ".wbpj",
            "results": tuple(fields),
            "field_specs": fields,
            "bundle_validator": validate_remaining,
            "material_loader": find_material,
            "parameters": {
                "case": case,
                "mesh_size_m": 0.005,
                "material_name": "Structural Steel"
                if thermal
                else "MCP Air"
                if case == "acoustic_cavity"
                else "MCP SDOF Linear",
                **PARAMETERS[case],
            },
        }
    return specs
