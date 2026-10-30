import ast
import cmath
import copy
import csv
import json
import math
import tempfile
import unittest
from pathlib import Path

from ansys_mcp.harmonic_benchmark import harmonic_curve, validate_harmonic
from tools.run_mechanical_basics import CASE_SPECS, material_operations


def fixture():
    p = {
        "length_m": 0.1,
        "width_m": 0.01,
        "height_m": 0.01,
        "young_pa": 2e11,
        "density_kg_m3": 7850,
        "poisson_ratio": 0,
        **CASE_SPECS["harmonic_sdof"]["parameters"],
    }
    frequencies = list(range(21, 121))
    samples = []
    for f in frequencies:
        omega = 2 * math.pi * f
        value = p["force_n"] / complex(
            2e8 - p["mass_kg"] * omega**2,
            p["mass_damping_s_inv"] * p["mass_kg"] * omega,
        )
        samples.append(
            {
                "frequency_hz": f,
                "real_m": value.real,
                "imag_m": value.imag,
                "tip_spread_m": 0.0,
                "transverse_m": 0.0,
            }
        )
    report = {
        "case": "harmonic_sdof",
        "status": "complete",
        "geometry": {
            "length_m": 0.1,
            "width_m": 0.01,
            "height_m": 0.01,
            "material": p["material_name"],
            "tip_ids": [2],
            "root_ids": [1],
        },
        "mass": {
            "point_kg": 1000,
            "body_kg": 0.0785,
            "behavior": "Rigid",
            "location_ids": [2],
            "position_m": [0.1, 0.005, 0.005],
            "tip_center_m": [0.1, 0.005, 0.005],
        },
        "mesh": {"elements": 80, "nodes": 621},
        "tip_node_ids": [7, 9],
        "fixed_ids": [1],
        "force": {
            "location_ids": [2],
            "x_n": [100],
            "y_n": [0],
            "z_n": [0],
            "x_phase_deg": [0],
        },
        "controls": {
            "solution_method": "Full",
            "frequency_spacing": "Linear",
            "min_hz": 20,
            "max_hz": 120,
            "solution_intervals": 100,
            "max_cores": 4,
            "distributed": False,
            "mass_damping_s_inv": p["mass_damping_s_inv"],
            "stiffness_damping_s": 0,
            "constant_damping_ratio": 0,
        },
        "cases": {
            "harmonic": {
                "status": "solved",
                "frequencies_hz": frequencies,
                "reader_frequencies_hz": [f for f in frequencies for _ in range(2)],
                "last_frequency_hz": 120,
                "last_amplitude_m": abs(
                    complex(samples[-1]["real_m"], samples[-1]["imag_m"])
                ),
            }
        },
    }
    return p, report, samples


def write_fields(path, report, samples):
    with path.open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(
            [
                "field_index",
                "label_space",
                "location",
                "unit",
                "entity_id",
                "entity_row",
                "component_0",
                "component_1",
                "component_2",
            ]
        )
        for i, row in enumerate(samples):
            for part, name in ((0, "real_m"), (1, "imag_m")):
                for node in reversed(report["tip_node_ids"]):
                    writer.writerow(
                        [
                            2 * i + part,
                            json.dumps({"time": i + 1, "complex": part}),
                            "Nodal",
                            "m",
                            node,
                            0,
                            row[name],
                            0,
                            0,
                        ]
                    )


class HarmonicBenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.p, self.report, self.samples = fixture()

    def check(self):
        return validate_harmonic(self.report, self.p, self.samples)

    def test_complex_reference_and_resonance_pass(self):
        result = self.check()
        self.assertTrue(result["ok"], result)
        self.assertAlmostEqual(
            result["metrics"]["natural_frequency_hz"], 71.17625434171771
        )
        self.assertAlmostEqual(result["metrics"]["damping_ratio"], 0.05)
        self.assertEqual(result["metrics"]["frequencies"], 100)

    def test_phase_error_with_identical_amplitude_fails(self):
        for row in self.samples:
            value = complex(row["real_m"], row["imag_m"]) * cmath.exp(0.2j)
            row.update(real_m=value.real, imag_m=value.imag)
        self.assertFalse(self.check()["checks"]["analytic_complex_response"])

    def test_imaginary_conjugation_or_missing_imaginary_fails(self):
        for factor in (-1, 0):
            p, r, s = fixture()
            for row in s:
                row["imag_m"] *= factor
            self.assertFalse(validate_harmonic(r, p, s)["ok"])

    def test_static_or_amplitude_only_cannot_pass(self):
        for row in self.samples:
            row.update(real_m=5e-7, imag_m=0)
        self.assertFalse(self.check()["ok"])

    def test_controls_scoping_units_mass_and_method_are_read_back(self):
        edits = [
            lambda r: r["mass"].update(point_kg=1),
            lambda r: r["controls"].update(solution_method="ModeSuperposition"),
            lambda r: r["controls"].update(mass_damping_s_inv=0),
            lambda r: r["controls"].update(stiffness_damping_s=0.1),
            lambda r: r["force"].update(x_phase_deg=[30]),
            lambda r: r["force"].update(location_ids=[1]),
            lambda r: r["geometry"].update(length_m=100),
            lambda r: r["controls"].update(max_cores=16),
        ]
        for edit in edits:
            report = copy.deepcopy(self.report)
            edit(report)
            self.assertFalse(validate_harmonic(report, self.p, self.samples)["ok"])

    def test_missing_wrong_frequency_or_nonfinite_data_fails(self):
        self.samples.pop()
        self.assertFalse(self.check()["ok"])
        self.p, self.report, self.samples = fixture()
        self.samples[3]["frequency_hz"] += 0.5
        self.assertFalse(self.check()["ok"])
        self.samples[3]["imag_m"] = math.nan
        self.assertFalse(self.check()["ok"])

    def test_mechanical_readback_is_independently_compared(self):
        self.report["cases"]["harmonic"]["last_amplitude_m"] *= 2
        self.assertFalse(self.check()["checks"]["mechanical_amplitude_readback"])

    def test_native_real_imaginary_records_map_to_one_dpf_frequency_set(self):
        self.assertTrue(self.check()["checks"]["native_frequency_axis"])
        self.report["cases"]["harmonic"]["reader_frequencies_hz"].pop()
        self.assertFalse(self.check()["checks"]["native_frequency_axis"])
        self.p, self.report, self.samples = fixture()
        self.report["cases"]["harmonic"]["frequencies_hz"] = self.report["cases"][
            "harmonic"
        ]["reader_frequencies_hz"]
        self.assertFalse(self.check()["ok"])

    def test_csv_uses_complex_labels_not_field_order(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "u.csv"
            write_fields(path, self.report, self.samples)
            parsed = harmonic_curve(path, self.report)
            self.assertEqual(parsed, self.samples)
            with path.open(newline="") as stream:
                rows = list(csv.reader(stream))
            with path.open("w", newline="") as stream:
                csv.writer(stream).writerows([rows[0], *reversed(rows[1:])])
            self.assertEqual(harmonic_curve(path, self.report), self.samples)

    def test_csv_rejects_missing_parts_duplicate_nodes_and_units(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "u.csv"
            write_fields(path, self.report, self.samples)
            with path.open(newline="") as stream:
                rows = list(csv.reader(stream))
            for bad in (
                rows[:-1],
                [*rows, rows[-1]],
                [rows[0], [*rows[1][:3], "mm", *rows[1][4:]], *rows[2:]],
            ):
                with path.open("w", newline="") as stream:
                    csv.writer(stream).writerows(bad)
                with self.assertRaises(ValueError):
                    harmonic_curve(path, self.report)

    def test_case_uses_existing_workbench_primitives_and_parseable_script(self):
        from ansys_mcp.schemas import WORKFLOW_STEP_ADAPTER

        spec = CASE_SPECS["harmonic_sdof"]
        self.assertEqual(spec["template"], "Harmonic Response (ANSYS)")
        WORKFLOW_STEP_ADAPTER.validate_python(
            {
                "type": "mechanical",
                "recipe": {
                    "backend": "workbench",
                    "operations": material_operations(self.p),
                },
            }
        )
        ast.parse(
            (
                Path(__file__).resolve().parents[1]
                / "examples/scripts"
                / spec["script"]
            ).read_text()
        )


if __name__ == "__main__":
    unittest.main()
