import ast
import copy
import csv
import json
import math
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ansys_mcp.mechanical_benchmarks import (
    nodal_vectors,
    validate_coordinates,
    structural_steel_properties,
)


PROJECT = Path(__file__).resolve().parents[1]


def coordinate_fixture():
    c, s = math.cos(math.pi / 6), math.sin(math.pi / 6)
    parameters = {
        "angle_deg": 30,
        "origin_m": [0.03, 0.002, -0.001],
        "length_m": 0.1,
        "width_m": 0.01,
        "height_m": 0.01,
        "young_pa": 200e9,
    }
    fields = {1: [0, 0, 0], 7: [0, -1e-6, 0], 15: [0, -2e-6, 0]}
    report = {
        "status": "complete",
        "coordinate": {
            "id": 11,
            "angle_deg": 30,
            "basis": [[c, s, 0], [-s, c, 0], [0, 0, 1]],
            "origin_m": [0.03, 0.002, -0.001],
            "transformation_count": 1,
        },
        "selections": {
            "root": [1],
            "tip": [2],
            "expected_root": [1],
            "expected_tip": [2],
            "root_object_id": 10,
            "tip_object_id": 20,
        },
        "mesh": {"nodes": 3, "elements": 1},
        "cases": {},
        "geometry": {
            "body_count": 1,
            "material": "Structural Steel",
            "length_m": 0.1,
            "width_m": 0.01,
            "height_m": 0.01,
        },
    }
    for name, coord, force in (("global", 0, [0, -1, 0]), ("rotated", 11, [-s, -c, 0])):
        report["cases"][name] = {
            "status": "solved",
            "maximum_deformation_m": 2e-6,
            "reaction_n": [0, 1, 0],
            "inputs": {
                "force_coordinate_id": coord,
                "force_n": force,
                "root_ids": [1],
                "tip_ids": [2],
                "max_cores": 4,
                "distributed": False,
            },
        }
    return report, parameters, fields


MATERIAL_XML = """<MatML_Doc><Metadata>
<ParameterDetails id="e"><Name>Young's Modulus</Name></ParameterDetails>
<ParameterDetails id="r"><Name>Density</Name></ParameterDetails>
<ParameterDetails id="k"><Name>Thermal Conductivity</Name></ParameterDetails>
</Metadata><Material><BulkDetails><Name>Structural Steel</Name>
<PropertyData><ParameterValue parameter="e"><Data>200e9</Data><Qualifier name="Units">Pa</Qualifier></ParameterValue>
<ParameterValue parameter="r"><Data>7850</Data><Qualifier name="Units">kg m^-3</Qualifier></ParameterValue>
<ParameterValue parameter="k"><Data>60.5</Data><Qualifier name="Units">W m^-1 C^-1</Qualifier></ParameterValue>
</PropertyData></BulkDetails></Material></MatML_Doc>"""


class CoordinateBenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.report, self.parameters, self.fields = coordinate_fixture()

    def check(self, report=None, fields=None):
        return validate_coordinates(
            report or self.report,
            self.parameters,
            self.fields,
            fields if fields is not None else self.fields,
        )

    def test_analytic_beam_and_rotated_load_equivalence(self):
        checked = self.check()
        self.assertTrue(checked["ok"], checked)
        self.assertAlmostEqual(checked["metrics"]["analytic_tip_m"], 2e-6)

    def test_unchanged_scalar_cannot_hide_changed_nodal_vector(self):
        wrong = copy.deepcopy(self.fields)
        wrong[15] = [2e-6, 0, 0]
        checked = self.check(fields=wrong)
        self.assertFalse(checked["checks"]["nodal_vector_equivalence"])

    def test_matching_values_with_different_node_ids_fail(self):
        wrong = {key + 1: value for key, value in self.fields.items()}
        self.assertFalse(self.check(fields=wrong)["ok"])

    def test_wrong_rotation_load_scoping_origin_and_core_count_fail(self):
        changes = [
            lambda r: r["coordinate"].update(basis=[[1, 0, 0], [0, 1, 0], [0, 0, 1]]),
            lambda r: r["cases"]["rotated"]["inputs"].update(force_n=[0, -1, 0]),
            lambda r: r["cases"]["rotated"]["inputs"].update(root_ids=[3]),
            lambda r: r["cases"]["rotated"]["inputs"].update(max_cores=16),
            lambda r: r["coordinate"].update(origin_m=[0, 0, 0]),
        ]
        for change in changes:
            report = copy.deepcopy(self.report)
            change(report)
            self.assertFalse(self.check(report=report)["ok"])

    def test_failed_solve_and_bad_reaction_fail(self):
        self.report["cases"]["global"]["status"] = "failed"
        self.assertFalse(self.check()["ok"])
        self.report["cases"]["global"]["status"] = "solved"
        self.report["cases"]["rotated"]["reaction_n"] = [0, -1, 0]
        self.assertFalse(self.check()["ok"])

    def test_zero_nonfinite_and_malformed_data_fail(self):
        for fields in (
            {},
            {1: [0, 0, 0], 7: [0, 0, 0], 15: [0, 0, 0]},
            {**self.fields, 15: [0, math.nan, 0]},
            {**self.fields, 15: [0, 1]},
        ):
            self.assertFalse(self.check(fields=fields)["ok"])

    def test_dpf_csv_requires_si_units_unique_ids_and_full_vectors(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "nodal.csv"
            header = [
                "field_index",
                "location",
                "unit",
                "entity_id",
                "entity_row",
                "component_0",
                "component_1",
                "component_2",
            ]

            def write(rows):
                with path.open("w", newline="") as stream:
                    writer = csv.writer(stream)
                    writer.writerow(header)
                    writer.writerows(rows)

            rows = [
                [0, "Nodal", "m", 15, 0, 0, -2e-6, 0],
                [0, "Nodal", "m", 7, 0, 0, -1e-6, 0],
            ]
            write(rows)
            self.assertEqual(nodal_vectors(path)[15], [0, -2e-6, 0])
            for bad in (
                rows + rows,
                [[0, "Nodal", "mm", 15, 0, 0, -2e-6, 0]],
                [[0, "Nodal", "m", 15, 0, 0, float("nan"), 0]],
                [],
            ):
                write(bad)
                with self.assertRaises(ValueError):
                    nodal_vectors(path)

    def test_runner_requires_explicit_opt_in_without_starting_a_client(self):
        from tools import run_mechanical_basics

        with (
            patch("sys.argv", ["runner", "--geometry-path", "missing.step"]),
            patch.object(run_mechanical_basics, "mcp_client") as client,
        ):
            with self.assertRaises(SystemExit):
                run_mechanical_basics.main()
            client.assert_not_called()

    def test_product_script_and_workflow_tests_never_import_product_python(self):
        source = PROJECT / "examples/scripts/mechanical_coordinate_acceptance.py"
        ast.parse(source.read_text(encoding="utf-8"))
        self.assertIn("mcp_result = report", source.read_text(encoding="utf-8"))

    def test_material_parser_checks_saved_constants_and_units(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "material.xml"
            path.write_text(MATERIAL_XML)
            self.assertEqual(structural_steel_properties(path)["young_pa"], 200e9)
            for bad in (
                "<broken-xml>",
                MATERIAL_XML.replace("Pa</Qualifier>", "MPa</Qualifier>"),
                MATERIAL_XML.replace("200e9", "nan"),
                MATERIAL_XML.replace("7850", "0"),
            ):
                path.write_text(bad)
                with self.assertRaises(ValueError):
                    structural_steel_properties(path)


class CoordinateReceiptTests(unittest.TestCase):
    def setUp(self):
        from ansys_mcp import capabilities
        from ansys_mcp.acceptance_evidence import file_sha256

        self.digest = file_sha256
        temp = tempfile.TemporaryDirectory(dir=PROJECT)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        environment = patch.dict(
            os.environ,
            {
                "ANSYS_MCP_ALLOWED_ROOTS": str(self.root),
                "ANSYS_MCP_EVIDENCE_ROOT": str(self.root / "evidence"),
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        scope = patch.object(
            capabilities,
            "_runtime_scope",
            return_value={
                "installation_fingerprint": "c" * 64,
                "version": "261",
                "current_installation": True,
            },
        )
        scope.start()
        self.addCleanup(scope.stop)
        self.report, parameters, fields = coordinate_fixture()
        self.sources = {}

        def write(role, name, value):
            path = self.root / name
            path.write_text(value if isinstance(value, str) else json.dumps(value))
            self.sources[role] = str(path)
            return path

        write("material", "material.xml", MATERIAL_XML)
        for name in ("global", "rotated"):
            path = self.root / (name + ".csv")
            with path.open("w", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(
                    [
                        "field_index",
                        "location",
                        "unit",
                        "entity_id",
                        "entity_row",
                        "component_0",
                        "component_1",
                        "component_2",
                    ]
                )
                for key, values in fields.items():
                    writer.writerow([0, "Nodal", "m", key, 0, *values])
            self.sources[name + "_field"] = str(path)
            result = write(
                name + "_result",
                name + ".rst",
                "Synthetic fixture, never product evidence.",
            )
            self.report["cases"][name]["result_file"] = str(result)
        session = {"id": "fixture-session", "backend": "workbench", "version": "261"}
        write(
            "model_workflow",
            "model.json",
            {
                "ok": True,
                "steps": [
                    {
                        "outputs": {
                            "session": session,
                            "values": {"benchmark": self.report},
                        }
                    }
                ],
            },
        )
        write(
            "closed",
            "closed.json",
            {
                "ok": True,
                "steps": [{"outputs": {"session": {**session, "closed": True}}}],
            },
        )
        steps = []
        for name in ("global", "rotated"):
            path = Path(self.sources[name + "_field"])
            steps.append(
                {
                    "id": name,
                    "ok": True,
                    "outputs": {
                        "values": {
                            "csv": {
                                "path": str(path),
                                "size_bytes": path.stat().st_size,
                                "sha256": self.digest(path),
                            }
                        }
                    },
                }
            )
        write("field_workflow", "dpf.json", {"ok": True, "steps": steps})
        write(
            "installation", "installation.json", {"installation_fingerprint": "c" * 64}
        )
        geometry = write("geometry_copy", "beam.step", "Synthetic geometry fixture.")
        write(
            "source_integrity",
            "source-integrity.json",
            {
                key: self.digest(geometry)
                for key in ("sha256_before", "sha256_after", "copy_sha256")
            },
        )
        write("script", "product.py", "# Synthetic script fixture only.")
        parameters.update(
            density_kg_m3=7850, conductivity_w_mk=60.5, sources=self.sources
        )
        report = write("report", "report.json", self.report)
        validation = write("validation", "validation.json", {"ok": True})
        envelope = write(
            "envelope", "envelope.json", {"ok": True, "outputs": {"session": session}}
        )
        params = write("parameters", "parameters.json", parameters)

        def bound(path):
            return {"path": str(path), "sha256": self.digest(path)}

        self.receipt = {
            "version": "1",
            "product": "mechanical",
            "product_version": "261",
            "backend": "workbench",
            "validator": "coordinate_equivalence",
            "case_id": "coordinate_equivalence",
            "run_id": "synthetic_coordinate",
            "scope_origin": "run",
            "observed_at": "2026-10-30T00:00:00Z",
            "installation_fingerprint": "c" * 64,
            "report": bound(report),
            "validation": bound(validation),
            "envelope": bound(envelope),
            "parameters": bound(params),
            "artifacts": [
                bound(Path(value))
                for key, value in self.sources.items()
                if key not in {"report", "validation", "envelope", "parameters"}
            ],
        }
        self.path = self.root / "receipt.json"
        self.path.write_text(json.dumps(self.receipt))

    def test_import_and_query_recompute_coordinate_evidence(self):
        from ansys_mcp.acceptance_evidence import import_receipt
        from ansys_mcp.capabilities import query_capabilities

        checked = import_receipt(self.path, apply=True)
        self.assertTrue(checked["checks"]["ok"])
        found = query_capabilities(product="mechanical", version="261", kind="workflow")
        self.assertEqual(found["items"][0]["status"], "verified")

    def test_artifact_loss_revokes_query(self):
        from ansys_mcp.acceptance_evidence import import_receipt
        from ansys_mcp.capabilities import query_capabilities

        import_receipt(self.path, apply=True)
        Path(self.sources["rotated_field"]).unlink()
        found = query_capabilities(product="mechanical", version="261", kind="workflow")
        self.assertEqual(found["items"][0]["status"], "unavailable")

    def test_unhashed_source_is_rejected(self):
        from ansys_mcp.acceptance_evidence import inspect_receipt

        self.receipt["artifacts"] = [
            item
            for item in self.receipt["artifacts"]
            if item["path"] != self.sources["material"]
        ]
        self.path.write_text(json.dumps(self.receipt))
        with self.assertRaisesRegex(ValueError, "hash-bound"):
            inspect_receipt(self.path)

    def test_new_receipt_hash_cannot_hide_changed_original_dpf_export(self):
        from ansys_mcp.acceptance_evidence import inspect_receipt

        path = Path(self.sources["rotated_field"])
        path.write_text(path.read_text().replace("-2e-06", "-1e-06"))
        for item in self.receipt["artifacts"]:
            if item["path"] == str(path):
                item["sha256"] = self.digest(path)
        self.path.write_text(json.dumps(self.receipt))
        with self.assertRaisesRegex(ValueError, "original_dpf_export"):
            inspect_receipt(self.path)


if __name__ == "__main__":
    unittest.main()
