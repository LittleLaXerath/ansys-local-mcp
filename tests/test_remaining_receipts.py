import json
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from test_remaining_benchmarks import fixture
from ansys_mcp import capabilities
from ansys_mcp.acceptance_evidence import file_sha256, import_receipt, inspect_receipt
from ansys_mcp.remaining_benchmarks import CASE_FIELDS, material_expected


class RemainingReceiptTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        env = patch.dict(
            os.environ,
            {
                "ANSYS_MCP_ALLOWED_ROOTS": str(self.root),
                "ANSYS_MCP_EVIDENCE_ROOT": str(self.root / "evidence"),
            },
        )
        env.start()
        self.addCleanup(env.stop)
        scope = patch.object(
            capabilities,
            "_runtime_scope",
            return_value={
                "installation_fingerprint": "d" * 64,
                "version": "261",
                "current_installation": True,
            },
        )
        scope.start()
        self.addCleanup(scope.stop)

    def receipt(self, case):
        directory = self.root / case
        directory.mkdir()
        p, r, fields = fixture(directory, case)
        sources = {}

        def write(role, name, value):
            path = directory / name
            path.write_text(
                value if isinstance(value, str) else json.dumps(value), encoding="utf-8"
            )
            sources[role] = str(path)
            return path

        document = ET.Element("MatML_Doc")
        metadata = ET.SubElement(document, "Metadata")
        bulk = ET.SubElement(ET.SubElement(document, "Material"), "BulkDetails")
        ET.SubElement(bulk, "Name").text = p["material_name"]
        data = ET.SubElement(bulk, "PropertyData")
        for i, (name, (key, units)) in enumerate(material_expected(case).items()):
            ident = "p" + str(i)
            parameter = ET.SubElement(metadata, "ParameterDetails", id=ident)
            ET.SubElement(parameter, "Name").text = name
            value = ET.SubElement(data, "ParameterValue", parameter=ident)
            ET.SubElement(value, "Data").text = str(p[key])
            ET.SubElement(value, "Qualifier", name="Units").text = sorted(units)[0]
        write("material", "material.xml", ET.tostring(document, encoding="unicode"))
        for name in CASE_FIELDS[case]:
            native = write(
                name + "_result",
                name + ".rst",
                "Synthetic fixture, not product evidence.",
            )
            r["cases"][name]["result_file"] = str(native)
            sources[name + "_field"] = str(fields[name])
        if case == "thermal_expansion":
            r["transfer"]["source_file"] = r["cases"]["thermal"]["result_file"]
        for name in r["native_tables"]:
            sources["table_" + name] = str(fields["table_" + name])
        for name in r.get("solver_logs", {}):
            sources["log_" + name] = str(fields["log_" + name])
        session = {"id": "synthetic-session", "backend": "workbench", "version": "261"}
        write(
            "model_workflow",
            "model.json",
            {
                "ok": True,
                "steps": [
                    {"outputs": {"session": session, "values": {"benchmark": r}}}
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
                        "id": name,
                        "ok": True,
                        "outputs": {
                            "values": {
                                "csv": {
                                    "path": str(fields[name]),
                                    "sha256": file_sha256(fields[name]),
                                    "size_bytes": fields[name].stat().st_size,
                                }
                            }
                        },
                    }
                    for name in CASE_FIELDS[case]
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
        write("script", "script.py", "# Synthetic fixture, never executed.")
        p["sources"] = dict(sources)
        report = write("report", "report.json", r)
        validation = write("validation", "validation.json", {"ok": True})
        parameters = write("parameters", "parameters.json", p)
        envelope = write(
            "envelope", "envelope.json", {"ok": True, "outputs": {"session": session}}
        )

        def bound(path):
            return {"path": str(path), "sha256": file_sha256(path)}

        receipt = {
            "product": "mechanical",
            "product_version": "261",
            "backend": "workbench",
            "validator": case,
            "case_id": case,
            "run_id": "synthetic_" + case,
            "scope_origin": "run",
            "observed_at": "2026-10-30T00:00:00Z",
            "installation_fingerprint": "d" * 64,
            "report": bound(report),
            "validation": bound(validation),
            "envelope": bound(envelope),
            "parameters": bound(parameters),
            "artifacts": [
                bound(Path(v))
                for k, v in sources.items()
                if k not in {"report", "validation", "parameters", "envelope"}
            ],
        }
        path = directory / "receipt.json"
        path.write_text(json.dumps(receipt))
        return path, receipt, sources

    def test_all_six_receipts_recompute_and_are_queryable(self):
        for case in CASE_FIELDS:
            with self.subTest(case=case):
                path, _, _ = self.receipt(case)
                checked = import_receipt(path, apply=True)
                self.assertTrue(checked["checks"]["ok"])
                result = capabilities.query_capabilities(
                    product="mechanical", kind="workflow", component=case, version="261"
                )
                self.assertEqual(result["items"][0]["status"], "verified")

    def test_modified_native_result_revokes_verification(self):
        path, _, sources = self.receipt("buckling")
        import_receipt(path, apply=True)
        Path(sources["buckling_result"]).write_text("Changed fixture")
        result = capabilities.query_capabilities(
            product="mechanical", kind="workflow", component="buckling", version="261"
        )
        self.assertEqual(result["items"][0]["status"], "unavailable")

    def test_new_hash_cannot_hide_changes_to_original_field_export(self):
        path, receipt, sources = self.receipt("thermal_expansion")
        field = Path(sources["expansion_field"])
        field.write_text(field.read_text() + "\n")
        for artifact in receipt["artifacts"]:
            if artifact["path"] == str(field):
                artifact["sha256"] = file_sha256(field)
        path.write_text(json.dumps(receipt))
        with self.assertRaises(ValueError):
            inspect_receipt(path)

    def test_wrong_case_or_backend_is_rejected(self):
        path, receipt, _ = self.receipt("random_vibration")
        for edit in ({"case_id": "response_spectrum"}, {"backend": "pymechanical"}):
            path.write_text(json.dumps({**receipt, **edit}))
            with self.assertRaises(ValueError):
                inspect_receipt(path)


if __name__ == "__main__":
    unittest.main()
