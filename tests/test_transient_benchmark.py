import ast
import copy
import csv
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ansys_mcp.transient_benchmark import (
    transient_curve,
    validate_transient,
    transient_material,
)


PROJECT = Path(__file__).resolve().parents[1]


def fixture():
    p = {
        "length_m": 0.1,
        "width_m": 0.01,
        "height_m": 0.01,
        "young_pa": 200e9,
        "density_kg_m3": 7850,
        "poisson_ratio": 0,
        "mass_kg": 1000.0,
        "force_n": 100.0,
        "dt_s": 0.00005,
        "end_time_s": 0.04,
        "material_name": "MCP SDOF Linear",
    }
    times = [i * p["dt_s"] for i in range(1, 801)]
    k = p["young_pa"] * p["width_m"] * p["height_m"] / p["length_m"]
    samples = [
        {
            "time_s": t,
            "x_m": p["force_n"] / k * (1 - math.cos(math.sqrt(k / p["mass_kg"]) * t)),
            "tip_spread_m": 0,
            "transverse_m": 0,
        }
        for t in times
    ]
    report = {
        "status": "complete",
        "tip_node_ids": [7, 9],
        "geometry": {
            "length_m": 0.1,
            "width_m": 0.01,
            "height_m": 0.01,
            "material": p["material_name"],
            "tip_ids": [2],
        },
        "mass": {
            "point_kg": 1000,
            "body_kg": 0.0785,
            "behavior": "Rigid",
            "location_ids": [2],
            "position_m": [0.1, 0.005, 0.005],
            "tip_center_m": [0.1, 0.005, 0.005],
        },
        "controls": {
            "dt_s": p["dt_s"],
            "end_time_s": p["end_time_s"],
            "automatic_time_stepping": "Off",
            "store_results_at": "AllTimePoints",
            "time_integration": True,
            "large_deflection": False,
            "mass_damping": 0,
            "stiffness_damping": 0,
            "numerical_damping": 0,
            "max_cores": 4,
            "distributed": False,
        },
        "force": {
            "times_s": [0, p["end_time_s"]],
            "x_n": [100, 100],
            "y_n": [0, 0],
            "z_n": [0, 0],
        },
        "cases": {
            "transient": {
                "status": "solved",
                "times_s": times,
                "last_displacement_m": samples[-1]["x_m"],
            }
        },
    }
    return p, report, samples


class TransientBenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.p, self.report, self.samples = fixture()

    def check(self):
        return validate_transient(self.report, self.p, self.samples)

    def test_undamped_analytical_step_response_passes(self):
        checked = self.check()
        self.assertTrue(checked["ok"], checked)
        self.assertAlmostEqual(checked["metrics"]["stiffness_n_per_m"], 2e8)
        self.assertLess(checked["metrics"]["relative_l2_error"], 1e-12)

    def test_static_result_cannot_pass_a_transient_benchmark(self):
        for row in self.samples:
            row["x_m"] = 5e-7
        self.assertFalse(self.check()["ok"])

    def test_phase_error_is_not_hidden_by_correct_peak(self):
        for row in self.samples:
            row["x_m"] = 5e-7 * (1 - math.cos(math.sqrt(2e5) * row["time_s"] + 0.3))
        self.assertFalse(self.check()["checks"]["analytic_time_response"])

    def test_mass_damping_timeintegration_units_and_geometry_are_read_back(self):
        edits = [
            lambda r: r["mass"].update(point_kg=1),
            lambda r: r["controls"].update(time_integration=False),
            lambda r: r["controls"].update(mass_damping=0.1),
            lambda r: r["controls"].update(max_cores=16),
            lambda r: r["geometry"].update(length_m=100),
            lambda r: r["force"].update(x_n=[0, 100]),
        ]
        for edit in edits:
            original = copy.deepcopy(self.report)
            edit(self.report)
            self.assertFalse(self.check()["ok"])
            self.report = original

    def test_nonzero_poisson_or_non_dominant_point_mass_fail(self):
        self.p["poisson_ratio"] = 0.3
        self.assertFalse(self.check()["ok"])
        self.p["poisson_ratio"] = 0
        self.report["mass"]["body_kg"] = 1e6
        self.assertFalse(self.check()["ok"])

    def test_missing_or_nonfinite_time_history_fails(self):
        self.samples.pop()
        self.assertFalse(self.check()["ok"])
        self.p, self.report, self.samples = fixture()
        self.samples[4]["x_m"] = math.nan
        self.assertFalse(self.check()["ok"])

    def test_csv_joins_all_tip_nodes_and_time_set_ids(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "fields.csv"
            header = [
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
            rows = []
            for i, sample in enumerate(self.samples):
                for node in (9, 7):
                    rows.append(
                        [
                            i,
                            json.dumps({"time": i + 1}),
                            "Nodal",
                            "m",
                            node,
                            0,
                            sample["x_m"],
                            0,
                            0,
                        ]
                    )

            def write(values):
                with path.open("w", newline="") as stream:
                    writer = csv.writer(stream)
                    writer.writerow(header)
                    writer.writerows(values)

            write(rows)
            parsed = transient_curve(path, self.report)
            self.assertEqual(parsed, self.samples)
            for bad in (
                rows[:-1],
                rows + [rows[-1]],
                [[*rows[0][:3], "mm", *rows[0][4:]], *rows[1:]],
            ):
                write(bad)
                with self.assertRaises(ValueError):
                    transient_curve(path, self.report)

    def test_material_zero_poisson_is_allowed_but_bad_units_are_not(self):
        from test_mechanical_basics import MATERIAL_XML

        text = MATERIAL_XML.replace(
            "Structural Steel", self.p["material_name"]
        ).replace(
            "</Metadata>",
            "<ParameterDetails id='n'><Name>Poisson's Ratio</Name></ParameterDetails></Metadata>",
        )
        text = text.replace(
            "</PropertyData>",
            "<ParameterValue parameter='n'><Data>0</Data><Qualifier name='Units'>  \n </Qualifier></ParameterValue></PropertyData>",
        )
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "material.xml"
            path.write_text(text)
            self.assertEqual(
                transient_material(path, self.p["material_name"])["poisson_ratio"], 0
            )
            path.write_text(text.replace("Pa</Qualifier>", "MPa</Qualifier>"))
            with self.assertRaises(ValueError):
                transient_material(path, self.p["material_name"])

    def test_case_registry_uses_existing_workbench_engineering_data_operations(self):
        from tools.run_mechanical_basics import CASE_SPECS, material_operations
        from ansys_mcp.schemas import WORKFLOW_STEP_ADAPTER

        spec = CASE_SPECS["transient_sdof"]
        self.assertEqual(spec["results"], ("transient",))
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
                PROJECT / "examples/scripts/mechanical_transient_acceptance.py"
            ).read_text()
        )


if __name__ == "__main__":
    unittest.main()
