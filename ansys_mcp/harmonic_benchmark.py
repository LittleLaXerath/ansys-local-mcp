"""Read-only complex frequency-response checks for a mass-dominated axial model."""

from __future__ import annotations

import cmath
import csv
import json
import math
from pathlib import Path


def harmonic_curve(path: Path, report: dict) -> list[dict]:
    frequencies = report["cases"]["harmonic"]["frequencies_hz"]
    nodes = set(report["tip_node_ids"])
    if not frequencies or not nodes or len(nodes) != len(report["tip_node_ids"]):
        raise ValueError(
            "A native frequency axis and unique tip node IDs are required."
        )
    fields = {}
    indexes = {}
    with path.open(newline="", encoding="utf-8-sig") as stream:
        for row in csv.DictReader(stream):
            label = json.loads(row["label_space"])
            time_id, part = label.get("time"), label.get("complex")
            node, index = int(row["entity_id"]), int(row["field_index"])
            if (
                set(label) != {"time", "complex"}
                or type(time_id) is not int
                or type(part) is not int
                or not 1 <= time_id <= len(frequencies)
                or part not in (0, 1)
                or node not in nodes
                or row["unit"] != "m"
                or row["location"] != "Nodal"
                or row["entity_row"] != "0"
                or index < 0
            ):
                raise ValueError(
                    "Complex displacement must match native frequency/node IDs in metres."
                )
            key = (time_id, part)
            if index in indexes and indexes[index] != key:
                raise ValueError(
                    "A field index cannot identify two frequency/complex parts."
                )
            indexes[index] = key
            values = fields.setdefault(key, {})
            vector = [float(row["component_" + str(i)]) for i in range(3)]
            if node in values or not all(math.isfinite(v) for v in vector):
                raise ValueError("Duplicate or nonfinite complex displacement.")
            values[node] = vector
    if set(fields) != {(i + 1, c) for i in range(len(frequencies)) for c in (0, 1)}:
        raise ValueError("Every native frequency needs both real and imaginary fields.")
    if len(indexes) != len(fields):
        raise ValueError("Each native complex part must have one field index.")
    samples = []
    for i, frequency in enumerate(frequencies):
        real, imag = fields[i + 1, 0], fields[i + 1, 1]
        if set(real) != nodes or set(imag) != nodes:
            raise ValueError("Incomplete tip-node coverage at a frequency.")
        vectors = [[complex(real[n][j], imag[n][j]) for j in range(3)] for n in nodes]
        mean = sum(v[0] for v in vectors) / len(vectors)
        samples.append(
            {
                "frequency_hz": frequency,
                "real_m": mean.real,
                "imag_m": mean.imag,
                "tip_spread_m": max(abs(v[0] - mean) for v in vectors),
                "transverse_m": max(
                    math.sqrt(abs(v[1]) ** 2 + abs(v[2]) ** 2) for v in vectors
                ),
            }
        )
    return samples


def validate_harmonic(report: dict, parameters: dict, samples: list[dict]) -> dict:
    checks, metrics = {}, {}
    try:

        def finite(value):
            if isinstance(value, dict):
                return all(finite(v) for v in value.values())
            if isinstance(value, list):
                return all(finite(v) for v in value)
            return not isinstance(value, float) or math.isfinite(value)

        if not finite([report, parameters, samples]):
            raise ValueError("Nonfinite harmonic data.")
        length, width, height, young, mass, force, alpha = [
            parameters[key]
            for key in (
                "length_m",
                "width_m",
                "height_m",
                "young_pa",
                "mass_kg",
                "force_n",
                "mass_damping_s_inv",
            )
        ]
        if min(length, width, height, young, mass, force, alpha) <= 0:
            raise ValueError(
                "Positive SI geometry, material, mass, force and damping required."
            )
        stiffness = young * width * height / length
        omega_n = math.sqrt(stiffness / mass)
        natural_hz = omega_n / (2 * math.pi)
        zeta = alpha / (2 * omega_n)
        if not 0.02 <= zeta <= 0.1:
            raise ValueError(
                "This fixed benchmark requires a lightly damped, resolved resonance."
            )
        case = report["cases"]["harmonic"]
        controls = report["controls"]
        checks["complete"] = (
            report["status"] == "complete" and case["status"] == "solved"
        )
        checks["geometry_material"] = (
            all(
                abs(report["geometry"][key] - parameters[key]) < 1e-9
                for key in ("length_m", "width_m", "height_m")
            )
            and report["geometry"]["material"] == parameters["material_name"]
            and parameters["poisson_ratio"] == 0
        )
        body_mass = parameters["density_kg_m3"] * length * width * height
        point = report["mass"]
        checks["mass_and_location"] = (
            math.isclose(point["point_kg"], mass, rel_tol=1e-10)
            and math.isclose(point["body_kg"], body_mass, rel_tol=1e-5)
            and 0 < body_mass / mass <= 1e-3
            and point["behavior"] == "Rigid"
            and point["location_ids"] == report["geometry"]["tip_ids"]
            and len(point["position_m"]) == len(point["tip_center_m"]) == 3
            and max(
                abs(a - b) for a, b in zip(point["position_m"], point["tip_center_m"])
            )
            < 1e-10
        )
        checks["full_method_damping"] = (
            controls["solution_method"] == "Full"
            and math.isclose(controls["mass_damping_s_inv"], alpha, rel_tol=1e-10)
            and controls["stiffness_damping_s"]
            == controls["constant_damping_ratio"]
            == 0
        )
        checks["resources_mesh"] = (
            controls["max_cores"] == 4
            and controls["distributed"] is False
            and report["mesh"]["elements"] > 0
            and report["mesh"]["nodes"] > 0
        )
        applied = report["force"]
        checks["force_and_support"] = (
            applied["location_ids"] == report["geometry"]["tip_ids"]
            and report["fixed_ids"] == report["geometry"]["root_ids"]
            and set(report["fixed_ids"]).isdisjoint(applied["location_ids"])
            and applied["x_n"] == [force]
            and applied["y_n"] == applied["z_n"] == [0]
            and applied["x_phase_deg"] == [0]
        )
        minimum, maximum, intervals = (
            parameters[k]
            for k in ("frequency_min_hz", "frequency_max_hz", "frequency_intervals")
        )
        step = (maximum - minimum) / intervals
        frequencies = [row["frequency_hz"] for row in samples]
        checks["native_frequency_axis"] = (
            frequencies == case["frequencies_hz"]
            and case["reader_frequencies_hz"]
            in (frequencies, [f for f in frequencies for _ in range(2)])
            and len(frequencies) == intervals
            and intervals >= 50
            and minimum <= frequencies[0] <= minimum + step * 1.001
            and math.isclose(frequencies[-1], maximum, rel_tol=1e-8)
            and all(
                math.isclose(b - a, step, rel_tol=1e-6)
                for a, b in zip(frequencies, frequencies[1:])
            )
            and controls["frequency_spacing"] == "Linear"
            and controls["solution_intervals"] == intervals
            and math.isclose(controls["min_hz"], minimum, rel_tol=1e-10)
            and math.isclose(controls["max_hz"], maximum, rel_tol=1e-10)
        )
        checks["resolved_resonance"] = (
            frequencies[0] < natural_hz * 0.5
            and frequencies[-1] > natural_hz * 1.5
            and step <= zeta * natural_hz / 2
        )
        references = [
            force
            / complex(
                stiffness - mass * (2 * math.pi * f) ** 2,
                alpha * mass * 2 * math.pi * f,
            )
            for f in frequencies
        ]
        values = [complex(row["real_m"], row["imag_m"]) for row in samples]
        complex_error = max(abs(v - r) / abs(r) for v, r in zip(values, references))
        amplitude_error = max(
            abs(abs(v) / abs(r) - 1) for v, r in zip(values, references)
        )
        phase_error = max(
            abs(math.degrees(cmath.phase(v / r))) for v, r in zip(values, references)
        )
        metrics.update(
            stiffness_n_per_m=stiffness,
            natural_frequency_hz=natural_hz,
            damping_ratio=zeta,
            frequencies=len(frequencies),
            max_complex_relative_error=complex_error,
            max_amplitude_relative_error=amplitude_error,
            max_phase_error_deg=phase_error,
        )
        checks["analytic_complex_response"] = (
            complex_error <= 0.05 and amplitude_error <= 0.05 and phase_error <= 3
        )
        checks["axial_uniform_motion"] = all(
            row["tip_spread_m"] <= abs(ref) * 0.001
            and row["transverse_m"] <= abs(ref) * 0.001
            for row, ref in zip(samples, references)
        )
        peak_index = max(range(len(values)), key=lambda i: abs(values[i]))
        peak_hz = frequencies[peak_index]
        metrics["measured_peak_frequency_hz"] = peak_hz
        metrics["peak_amplitude_m"] = abs(values[peak_index])
        checks["resonance_and_phase_change"] = (
            abs(peak_hz / (natural_hz * math.sqrt(1 - 2 * zeta * zeta)) - 1) <= 0.02
            and values[0].real > 0
            and values[-1].real < 0
            and all(v.imag < 0 for v in values)
            and abs(values[peak_index]) > 5 * force / stiffness
        )
        checks["mechanical_amplitude_readback"] = (
            math.isclose(case["last_frequency_hz"], frequencies[-1], rel_tol=1e-8)
            and abs(case["last_amplitude_m"] / abs(values[-1]) - 1) <= 0.001
        )
        return {
            "ok": all(checks.values()),
            "checks": checks,
            "metrics": metrics,
            "scope": "full_method_mass_damped_axial_harmonic_only",
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
