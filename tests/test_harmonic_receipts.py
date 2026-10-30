import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_harmonic_benchmark import fixture, write_fields
from test_mechanical_basics import MATERIAL_XML
from ansys_mcp import capabilities
from ansys_mcp.acceptance_evidence import file_sha256, import_receipt, inspect_receipt


class HarmonicReceiptTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        env = patch.dict(
            os.environ,
            {
                "ANSYS_MCP_ALLOWED_ROOTS": str(self.root),
                "ANSYS_MCP_EVIDENCE_ROOT": str(self.root / "evidence"),
            },
        )
        env.start()
        self.addCleanup(env.stop)
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
            path.write_text(
                value if isinstance(value, str) else json.dumps(value), encoding="utf-8"
            )
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
            "harmonic_result",
            "harmonic.rst",
            "Synthetic test fixture, not product evidence.",
        )
        report["cases"]["harmonic"]["result_file"] = str(native)
        field = self.root / "harmonic.csv"
        write_fields(field, report, samples)
        sources["harmonic_field"] = str(field)
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
                        "id": "harmonic",
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
            "validator": "harmonic_sdof",
            "case_id": "harmonic_sdof",
            "run_id": "synthetic_harmonic",
            "scope_origin": "run",
            "observed_at": "2026-10-30T00:00:00Z",
            "installation_fingerprint": "d" * 64,
            "report": bound(report_path),
            "validation": bound(validation),
            "envelope": bound(envelope),
            "parameters": bound(parameters),
            "artifacts": [
                bound(Path(v))
                for k, v in sources.items()
                if k not in {"report", "validation", "envelope", "parameters"}
            ],
        }
        self.path = self.root / "receipt.json"
        self.path.write_text(json.dumps(self.receipt))

    def test_import_and_query_recompute_complex_frequency_response(self):
        checked = import_receipt(self.path, apply=True)
        self.assertTrue(checked["checks"]["ok"])
        result = capabilities.query_capabilities(
            product="mechanical",
            version="261",
            kind="workflow",
            component="harmonic_sdof",
        )
        self.assertEqual(result["items"][0]["status"], "verified")
        self.assertEqual(result["items"][0]["metrics"]["frequencies"], 100)

    def test_current_hash_cannot_hide_modified_original_export(self):
        path = Path(self.sources["harmonic_field"])
        path.write_text(path.read_text() + "\n")
        for item in self.receipt["artifacts"]:
            if item["path"] == str(path):
                item["sha256"] = file_sha256(path)
        self.path.write_text(json.dumps(self.receipt))
        with self.assertRaises(ValueError):
            inspect_receipt(self.path)

    def test_receipt_requires_matching_case_and_backend(self):
        for edit in ({"case_id": "transient_sdof"}, {"backend": "mechanical"}):
            self.path.write_text(json.dumps({**self.receipt, **edit}))
            with self.assertRaises(ValueError):
                inspect_receipt(self.path)

    def test_changed_native_artifact_invalidates_query(self):
        import_receipt(self.path, apply=True)
        Path(self.sources["harmonic_result"]).write_text("Changed synthetic fixture.")
        result = capabilities.query_capabilities(
            product="mechanical",
            version="261",
            kind="workflow",
            component="harmonic_sdof",
        )
        self.assertEqual(result["items"][0]["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
