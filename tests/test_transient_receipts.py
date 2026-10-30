import csv
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_transient_benchmark import fixture
from test_mechanical_basics import MATERIAL_XML
from ansys_mcp import capabilities
from ansys_mcp.acceptance_evidence import file_sha256, import_receipt, inspect_receipt


PROJECT = Path(__file__).resolve().parents[1]


class TransientReceiptTests(unittest.TestCase):
    def setUp(self):
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
        scoped = patch.object(
            capabilities,
            "_runtime_scope",
            return_value={
                "installation_fingerprint": "d" * 64,
                "version": "261",
                "current_installation": True,
            },
        )
        scoped.start()
        self.addCleanup(scoped.stop)
        p, report, samples = fixture()
        sources = {}

        def write(role, name, value):
            path = self.root / name
            path.write_text(value if isinstance(value, str) else json.dumps(value))
            sources[role] = str(path)
            return path

        material = MATERIAL_XML.replace("Structural Steel", p["material_name"]).replace(
            "</Metadata>",
            "<ParameterDetails id='n'><Name>Poisson's Ratio</Name></ParameterDetails></Metadata>",
        )
        material = material.replace(
            "</PropertyData>",
            "<ParameterValue parameter='n'><Data>0</Data></ParameterValue></PropertyData>",
        )
        write("material", "material.xml", material)
        native = write(
            "transient_result",
            "transient.rst",
            "Synthetic test fixture, not product evidence.",
        )
        report["cases"]["transient"]["result_file"] = str(native)
        field = self.root / "transient.csv"
        with field.open("w", newline="") as stream:
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
            for i, sample in enumerate(samples):
                for node in report["tip_node_ids"]:
                    writer.writerow(
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
        sources["transient_field"] = str(field)
        session = {"id": "synthetic-session", "backend": "workbench", "version": "261"}
        write(
            "model_workflow",
            "model.json",
            {
                "ok": True,
                "steps": [
                    {"outputs": {"session": session, "values": {"benchmark": report}}}
                ],
            },
        )
        write(
            "field_workflow",
            "fields.json",
            {
                "ok": True,
                "steps": [
                    {
                        "id": "transient",
                        "ok": True,
                        "outputs": {
                            "values": {
                                "csv": {
                                    "path": str(field),
                                    "sha256": file_sha256(field),
                                    "size_bytes": field.stat().st_size,
                                }
                            }
                        },
                    }
                ],
            },
        )
        write(
            "closed",
            "close.json",
            {
                "ok": True,
                "steps": [{"outputs": {"session": {**session, "closed": True}}}],
            },
        )
        write(
            "installation", "installation.json", {"installation_fingerprint": "d" * 64}
        )
        geometry = write("geometry_copy", "beam.step", "Synthetic geometry.")
        write(
            "source_integrity",
            "integrity.json",
            {
                key: file_sha256(geometry)
                for key in ("sha256_before", "sha256_after", "copy_sha256")
            },
        )
        write("script", "product.py", "# Synthetic fixture, not an executed script.")
        p["sources"] = dict(sources)
        self.sources = sources
        report_path = write("report", "report.json", report)
        validation = write("validation", "validation.json", {"ok": True})
        envelope = write(
            "envelope", "envelope.json", {"ok": True, "outputs": {"session": session}}
        )
        parameters = write("parameters", "parameters.json", p)

        def bound(path):
            return {"path": str(path), "sha256": file_sha256(path)}

        self.receipt = {
            "product": "mechanical",
            "product_version": "261",
            "backend": "workbench",
            "validator": "transient_sdof",
            "case_id": "transient_sdof",
            "run_id": "synthetic_transient",
            "scope_origin": "run",
            "observed_at": "2026-10-30T00:00:00Z",
            "installation_fingerprint": "d" * 64,
            "report": bound(report_path),
            "validation": bound(validation),
            "envelope": bound(envelope),
            "parameters": bound(parameters),
            "artifacts": [
                bound(Path(value))
                for key, value in sources.items()
                if key not in {"report", "validation", "envelope", "parameters"}
            ],
        }
        self.path = self.root / "receipt.json"
        self.path.write_text(json.dumps(self.receipt))

    def test_import_and_query_recompute_all_native_time_sets(self):
        checked = import_receipt(self.path, apply=True)
        self.assertTrue(checked["checks"]["ok"])
        result = capabilities.query_capabilities(
            product="mechanical",
            version="261",
            kind="workflow",
            component="transient_sdof",
        )
        self.assertEqual(result["items"][0]["status"], "verified")
        self.assertEqual(result["items"][0]["metrics"]["samples"], 800)

    def test_new_receipt_hash_cannot_hide_changed_original_export(self):
        path = Path(self.sources["transient_field"])
        path.write_text(path.read_text() + "\n")
        for item in self.receipt["artifacts"]:
            if item["path"] == str(path):
                item["sha256"] = file_sha256(path)
        self.path.write_text(json.dumps(self.receipt))
        with self.assertRaisesRegex(ValueError, "original_dpf_export"):
            inspect_receipt(self.path)

    def test_missing_artifact_revokes_verified_status(self):
        import_receipt(self.path, apply=True)
        Path(self.sources["transient_result"]).unlink()
        result = capabilities.query_capabilities(
            product="mechanical",
            version="261",
            kind="workflow",
            component="transient_sdof",
        )
        self.assertEqual(result["items"][0]["status"], "unavailable")
