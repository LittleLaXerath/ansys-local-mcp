import copy
import csv
import json
import math
import tempfile
import unittest
from pathlib import Path

from ansys_mcp.remaining_benchmarks import (
    CASE_FIELDS,
    PARAMETERS,
    rms_reference,
    temperature_reference,
    validate_remaining,
    read_fields,
    modal_participation,
    specifications,
)


def fixture(directory, case):
    p = {
        "case": case,
        "length_m": 0.1,
        "width_m": 0.01,
        "height_m": 0.01,
        "density_kg_m3": 7850.0,
        "young_pa": 2e11,
        "poisson_ratio": 0,
        "conductivity_w_mk": 60.5,
        "specific_heat_j_kgk": 434.0,
        "expansion_per_k": 1.2e-5,
        "material_name": "Fixture",
        "mesh_size_m": 0.005,
        **PARAMETERS[case],
    }
    ids = [1, 2, 3]
    xs = {"1": 0.0, "2": 0.05, "3": 0.1}
    r = {
        "case": case,
        "status": "complete",
        "geometry": {
            "length_m": 0.1,
            "width_m": 0.01,
            "height_m": 0.01,
            "material": "Fixture",
            "root_ids": [10],
            "tip_ids": [20],
            "body_ids": [30],
        },
        "mesh": {"nodes": 3, "elements": 2},
        "tip_node_ids": [3],
        "root_node_ids": [1],
        "node_x_m": xs,
        "controls": {"1": {"max_cores": 4, "distributed": False}},
        "cases": {},
        "native_tables": {},
    }
    paths = {}

    def field(name, sets):
        path = directory / (name + ".csv")
        result = CASE_FIELDS[case][name][1]
        unit = {
            "displacement": "m",
            "temperature": "C",
            "pressure": "Pa",
            "structural_temperature": "C",
        }[result]
        with path.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(
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
            for i, values in enumerate(sets):
                for node, v in zip(ids, values):
                    w.writerow(
                        [
                            i,
                            json.dumps({"time": i + 1}),
                            "Nodal",
                            unit,
                            node,
                            0,
                            v,
                            0,
                            0,
                        ]
                    )
        paths[name] = path
        r["cases"][name] = {
            "status": "solved",
            "result_file": str(directory / (name + ".rst")),
            "native_axis": [float(i + 1) for i in range(len(sets))],
        }

    if case in {"response_spectrum", "random_vibration"}:
        omega = math.sqrt(2e5)
        value = 1 / 2e5 if case == "response_spectrum" else rms_reference(p)
        field("modal", [[0, 0.5 / math.sqrt(1000), 1 / math.sqrt(1000)], [0, 1, 0]])
        r["cases"]["modal"]["native_axis"] = [omega / (2 * math.pi), 25000]
        name = "response" if case == "response_spectrum" else "random"
        field(
            name,
            [[0, value / 2, value]]
            if case == "response_spectrum"
            else [[1, 1, 1], [0, value / 2, value]],
        )
        r["cases"][name]["displacement_m"] = value
        if case == "random_vibration":
            r["cases"][name]["sigma_scale"] = "Sigma1"
            r["cases"][name]["result_record"] = {
                "load_step": 3,
                "substep": 1,
                "iteration": 2,
            }
        table = directory / (name + ".txt")
        table.write_text(
            "Node Number\tDirectional Deformation (m)\n3\t" + str(value) + "\n"
        )
        paths["table_" + name] = table
        r["native_tables"][name] = str(table)
        r.update(
            mass={
                "point_kg": 1000,
                "body_kg": 0.0785,
                "behavior": "Rigid",
                "location_ids": [20],
            },
            fixed_ids=[10],
            lateral_constraint={"enabled": False},
        )
        log = directory / "modal.solve.txt"
        log.write_text(
            "***** PARTICIPATION FACTOR CALCULATION ***** X DIRECTION\n1 71.176254 0.01405 "
            + str(math.sqrt(1000))
            + " 1 1000 1 0.999974\n2 25000 0.00004 0 0 0 1 0\n***** PARTICIPATION FACTOR CALCULATION ***** Y DIRECTION\n"
        )
        r["solver_logs"] = {"modal": str(log)}
        paths["log_modal"] = log
        r["spectrum"] = {
            "frequency_hz": p["spectrum_hz"],
            "values": p["spectrum_values"],
            "direction": "XAxis",
            "boundary_id": 9,
            "fixed_id": 9,
            "damping_ratio": 0.05,
            "modal_id": 1,
            "expected_modal_id": 1,
            "unit": "m s^-2" if case == "response_spectrum" else "m^2 s^-3",
            "combination": "SRSS",
            "missing_mass": False,
            "rigid_response": False,
            "mode_significance": 0,
            "constant_damping": "Manual",
            "mass_damping": 0,
            "stiffness_damping": 0,
        }
    elif case == "buckling":
        field("prestress", [[0, -1e-9, -2e-9]])
        field("buckling", [[0, 0.2, 1], [0, 1, 0.2]])
        r["cases"]["buckling"]["load_multiplier"] = (
            math.pi**2 * 2e11 * 0.01 * 0.01**3 / (12 * 4 * 0.1**2)
        )
        r["buckling"] = {
            "prestress_id": 1,
            "static_id": 1,
            "fixed_ids": [10],
            "force_ids": [20],
            "force_n": [-1, 0, 0],
        }
    elif case == "transient_heat":
        times = [(i + 1) * p["dt_s"] for i in range(400)]
        field(
            "heat",
            [[temperature_reference(x, t, p) for x in xs.values()] for t in times],
        )
        r["cases"]["heat"]["native_axis"] = times
        r["thermal"] = {
            "initial_c": 0,
            "cold_c": 0,
            "hot_c": 100,
            "dt_s": 0.25,
            "end_time_s": 100,
            "auto": "Off",
            "store": "AllTimePoints",
            "cold_ids": [10],
            "hot_ids": [20],
        }
    elif case == "acoustic_cavity":
        field("acoustic", [[1, 0, -1], [1, -1, 1], [1, 0, -1]])
        r["cases"]["acoustic"]["native_axis"] = [
            p["sound_speed_m_s"] * n / 0.2 for n in (1, 2, 3)
        ]
        r["acoustic"] = {
            "acoustics": True,
            "structural": False,
            "location_ids": [30],
            "region_created_here": False,
        }
    else:
        value = p["expansion_per_k"] * 50 * 0.1
        field("thermal", [[72, 72, 72]])
        field("expansion", [[0, value / 2, value]])
        field("transferred", [[72, 72, 72]])
        r["cases"]["expansion"]["displacement_m"] = value
        r["transfer"] = {
            "source_file": r["cases"]["thermal"]["result_file"],
            "source_min_c": 72,
            "source_max_c": 72,
            "temperature_c": 72,
            "reference_c": 22,
            "target_ids": [30],
        }
    return p, r, paths


class RemainingBenchmarkTests(unittest.TestCase):
    def test_all_six_independent_references_pass_synthetic_fixtures(self):
        with tempfile.TemporaryDirectory() as temp:
            for case in CASE_FIELDS:
                with self.subTest(case=case):
                    p, r, paths = fixture(Path(temp), case)
                    result = validate_remaining(r, p, paths)
                    self.assertTrue(result["ok"], result)

    def test_wrong_spectrum_direction_damping_link_or_units_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            p, r, paths = fixture(Path(temp), "response_spectrum")
            for key, bad in (
                ("direction", "YAxis"),
                ("damping_ratio", 0.1),
                ("modal_id", 2),
                ("unit", "g"),
            ):
                edited = copy.deepcopy(r)
                edited["spectrum"][key] = bad
                self.assertFalse(validate_remaining(edited, p, paths)["ok"])

    def test_random_rms_is_not_peak_or_three_sigma(self):
        with tempfile.TemporaryDirectory() as temp:
            p, r, paths = fixture(Path(temp), "random_vibration")
            path = paths["table_random"]
            value = r["cases"]["random"]["displacement_m"] * 3
            path.write_text(
                "Node Number\tDirectional Deformation (m)\n3\t" + str(value) + "\n"
            )
            r["cases"]["random"]["displacement_m"] = value
            self.assertFalse(
                validate_remaining(r, p, paths)["checks"]["independent_reference"]
            )

    def test_native_participation_requires_the_x_table_and_mass_normalized_shape(self):
        with tempfile.TemporaryDirectory() as temp:
            p, r, paths = fixture(Path(temp), "response_spectrum")
            log = paths["log_modal"]
            log.write_text(log.read_text().replace("X DIRECTION", "Z DIRECTION"))
            with self.assertRaises(ValueError):
                modal_participation(log)
            self.assertFalse(validate_remaining(r, p, paths)["ok"])

    def test_native_table_and_dpf_disagreement_cannot_pass(self):
        with tempfile.TemporaryDirectory() as temp:
            p, r, paths = fixture(Path(temp), "response_spectrum")
            field = paths["response"]
            with field.open(newline="") as stream:
                rows = list(csv.reader(stream))
            for row in rows[1:]:
                row[6] = str(float(row[6]) * 2)
            with field.open("w", newline="") as stream:
                csv.writer(stream).writerows(rows)
            checked = validate_remaining(r, p, paths)
            self.assertTrue(checked["checks"]["independent_reference"])
            self.assertFalse(checked["checks"]["dpf_response_readback"])

    def test_static_temperature_field_is_not_a_transient_solution(self):
        with tempfile.TemporaryDirectory() as temp:
            p, r, paths = fixture(Path(temp), "transient_heat")
            field = paths["heat"]
            with field.open(newline="") as stream:
                rows = list(csv.reader(stream))
            for row in rows[1:]:
                row[6] = str((int(row[4]) - 1) * 50)
            with field.open("w", newline="") as stream:
                csv.writer(stream).writerows(rows)
            self.assertFalse(
                validate_remaining(r, p, paths)["checks"]["analytic_conduction"]
            )

    def test_target_structural_temperature_is_read_back_not_only_the_source(self):
        with tempfile.TemporaryDirectory() as temp:
            p, r, paths = fixture(Path(temp), "thermal_expansion")
            field = paths["transferred"]
            field.write_text(field.read_text().replace(",72,", ",22,"))
            self.assertFalse(
                validate_remaining(r, p, paths)["checks"]["mapped_target_temperature"]
            )

    def test_psd_integral_converges_and_is_frequency_bounded(self):
        p = {
            **PARAMETERS["random_vibration"],
            "young_pa": 2e11,
            "width_m": 0.01,
            "height_m": 0.01,
            "length_m": 0.1,
        }
        a, b = rms_reference(p, 10000), rms_reference(p, 20000)
        self.assertLess(abs(a / b - 1), 1e-8)
        p["spectrum_values"] = [0.01, 0.1]
        with self.assertRaises(ValueError):
            rms_reference(p)

    def test_psd_readback_requires_the_native_sigma_record_not_the_base_solution(self):
        with tempfile.TemporaryDirectory() as temp:
            p, r, paths = fixture(Path(temp), "random_vibration")
            r["cases"]["random"]["result_record"]["iteration"] = 1
            checked = validate_remaining(r, p, paths)
            self.assertFalse(checked["checks"]["native_sigma_record"])

    def test_acoustic_region_creation_is_not_inferred_from_a_namespace(self):
        with tempfile.TemporaryDirectory() as temp:
            p, r, paths = fixture(Path(temp), "acoustic_cavity")
            r["executed_apis"] = [
                "Ansys.ACT.Automation.Mechanical.Analysis.AddPhysicsRegion"
            ]
            self.assertFalse(
                validate_remaining(r, p, paths)["checks"]["explicit_api_evidence"]
            )
            r["acoustic"]["region_created_here"] = True
            self.assertTrue(validate_remaining(r, p, paths)["ok"])

    def test_euler_factor_uses_actual_compression_and_prestress_link(self):
        with tempfile.TemporaryDirectory() as temp:
            p, r, paths = fixture(Path(temp), "buckling")
            r["cases"]["buckling"]["load_multiplier"] *= 4
            self.assertFalse(
                validate_remaining(r, p, paths)["checks"]["euler_reference"]
            )
            r["buckling"]["prestress_id"] = 8
            self.assertFalse(
                validate_remaining(r, p, paths)["checks"]["prestress_link"]
            )

    def test_thermal_rejects_static_field_missing_times_and_wrong_coordinates(self):
        with tempfile.TemporaryDirectory() as temp:
            p, r, paths = fixture(Path(temp), "transient_heat")
            r["cases"]["heat"]["native_axis"].pop()
            self.assertFalse(validate_remaining(r, p, paths)["ok"])
            p, r, paths = fixture(Path(temp), "transient_heat")
            r["node_x_m"]["3"] = 100
            self.assertFalse(validate_remaining(r, p, paths)["ok"])

    def test_acoustic_needs_three_nonzero_modes_and_acoustic_region(self):
        with tempfile.TemporaryDirectory() as temp:
            p, r, paths = fixture(Path(temp), "acoustic_cavity")
            r["cases"]["acoustic"]["native_axis"][0] = 0
            self.assertFalse(
                validate_remaining(r, p, paths)["checks"]["cavity_reference"]
            )
            r["acoustic"]["structural"] = True
            self.assertFalse(
                validate_remaining(r, p, paths)["checks"]["acoustic_region"]
            )

    def test_expansion_needs_actual_source_file_temperature_and_reference(self):
        with tempfile.TemporaryDirectory() as temp:
            p, r, paths = fixture(Path(temp), "thermal_expansion")
            for key, bad in (
                ("source_file", "other.rth"),
                ("source_min_c", 22),
                ("reference_c", 0),
            ):
                edited = copy.deepcopy(r)
                edited["transfer"][key] = bad
                self.assertFalse(validate_remaining(edited, p, paths)["ok"])

    def test_field_parser_rejects_wrong_units_duplicates_and_nan(self):
        with tempfile.TemporaryDirectory() as temp:
            p, r, paths = fixture(Path(temp), "buckling")
            path = paths["prestress"]
            original = path.read_text()
            for bad in (
                original.replace(",m,", ",mm,"),
                original + original.splitlines()[-1] + "\n",
                original.replace("-1e-09", "nan"),
            ):
                path.write_text(bad)
                with self.assertRaises(ValueError):
                    read_fields(path, "displacement")

    def test_registry_uses_the_existing_script_step_and_no_new_tools(self):
        import ast
        from tools.run_mechanical_basics import CASE_SPECS, material_operations
        from ansys_mcp.schemas import WORKFLOW_STEP_ADAPTER

        self.assertEqual(set(specifications()), set(CASE_FIELDS))
        ast.parse(
            (
                Path(__file__).resolve().parents[1]
                / "examples/scripts/mechanical_remaining_acceptance.py"
            ).read_text()
        )
        for case in CASE_FIELDS:
            params = CASE_SPECS[case]["parameters"]
            WORKFLOW_STEP_ADAPTER.validate_python(
                {
                    "type": "mechanical",
                    "recipe": {
                        "backend": "workbench",
                        "operations": material_operations(params)
                        or [{"id": "inspect", "op": "inspect", "target": "Model"}],
                    },
                }
            )


if __name__ == "__main__":
    unittest.main()
