from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ansys_mcp import capabilities
from ansys_mcp.acceptance_evidence import file_sha256, import_receipt, inspect_receipt
from ansys_mcp.benchmarks import analytic_references


PROJECT = Path(__file__).resolve().parents[1]


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        parent = PROJECT / ".ansys_mcp"
        parent.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict(os.environ, {"ANSYS_MCP_ALLOWED_ROOTS": str(self.root), "ANSYS_MCP_EVIDENCE_ROOT": str(self.root / "evidence")})
        env.start()
        self.addCleanup(env.stop)
        self.scope = {"installation_fingerprint": "f" * 64, "version": "261", "current_installation": True}
        scoped = patch.object(capabilities, "_runtime_scope", return_value=self.scope)
        scoped.start()
        self.addCleanup(scoped.stop)
        capabilities._EVIDENCE.clear()
        capabilities._LOADED_EVIDENCE.clear()
        self.addCleanup(capabilities._EVIDENCE.clear)
        self.addCleanup(capabilities._LOADED_EVIDENCE.clear)
        self.parameters = dict(length_m=.1, width_m=.01, height_m=.01, young_pa=2e11, density_kg_m3=7850, conductivity_w_mk=60.5)
        refs = analytic_references(**self.parameters)
        self.report = {"requested_cases": ["static"], "cases": {"static": {"status": "solved", "reaction_y_n": 1,
                       "maximum_deformation_m": refs["static_tip_m"]}},
                       "executed_apis": ["Ansys.ACT.Automation.Mechanical.Solution.Solve"]}
        self.receipt = {"version": "1", "product": "mechanical", "product_version": "261", "backend": "workbench",
                        "case_id": "beam", "run_id": "run_test_1", "validator": "core_beam_scalars",
                        "observed_at": "2026-01-02T00:00:00Z", "installation_fingerprint": "f" * 64}
        for key, value in {"report": self.report, "validation": {"ok": True}, "parameters": self.parameters,
                           "envelope": {"ok": True, "outputs": {"session": {"backend": "workbench", "version": "261"}}}}.items():
            path = self.root / (key + ".json")
            path.write_text(json.dumps(value))
            self.receipt[key] = {"path": path.name, "sha256": file_sha256(path)}
        artifact = self.root / "native.rst"
        artifact.write_text("fake native test fixture; not product evidence")
        self.receipt["artifacts"] = [{"path": artifact.name, "sha256": file_sha256(artifact)}]
        self.path = self.root / "receipt.json"
        self.save()

    def save(self):
        self.path.write_text(json.dumps(self.receipt))

    def change_report(self, data):
        path = self.root / "report.json"
        path.write_text(json.dumps(data))
        self.receipt["report"]["sha256"] = file_sha256(path)
        self.save()

    def test_dry_run_does_not_write_or_promote_evidence(self):
        result = import_receipt(self.path)
        self.assertTrue(result["dry_run"])
        self.assertFalse(Path(result["index_path"]).exists())
        self.assertEqual(capabilities._EVIDENCE, {})

    def test_import_query_reload_and_artifact_mutation(self):
        result = import_receipt(self.path, apply=True)
        self.assertTrue(Path(result["index_path"]).is_file())
        capabilities._EVIDENCE.clear()
        capabilities._LOADED_EVIDENCE.clear()
        found = capabilities.query_capabilities(kind="workflow", version="261", component="beam")
        self.assertEqual(found["total"], 1)
        self.assertEqual(found["items"][0]["status"], "verified")
        capabilities._load_current_evidence("261", self.scope)
        observed = next(iter(capabilities._EVIDENCE.values()))
        self.assertEqual(observed["case_id"], "beam")
        self.assertEqual(observed["run_id"], "run_test_1")
        self.assertEqual(observed["receipt_sha256"], file_sha256(self.path))
        api = {"product": "mechanical", "version": "261", "qualified_name": self.report["executed_apis"][0]}
        self.assertEqual(capabilities._overlay(api, self.scope)["status"], "verified")
        (self.root / "native.rst").write_text("changed")
        invalid = capabilities.query_capabilities(kind="workflow", version="261")
        self.assertEqual(invalid["items"][0]["status"], "unavailable")
        self.assertEqual(capabilities._overlay(api, self.scope)["status"], "evidence_unavailable")

    def test_failed_or_nonfinite_numerical_check_cannot_be_promoted(self):
        for value in (0, float("nan"), float("inf")):
            with self.subTest(value=value):
                self.report["cases"]["static"]["maximum_deformation_m"] = value
                self.change_report(self.report)
                with self.assertRaises(ValueError): import_receipt(self.path, apply=True)
                self.assertEqual(capabilities._EVIDENCE, {})

    def test_missing_or_modified_file_fails_before_import(self):
        (self.root / "native.rst").unlink()
        with self.assertRaises((OSError, ValueError)): import_receipt(self.path, apply=True)
        self.assertEqual(capabilities._EVIDENCE, {})

    def test_installation_mismatch_is_not_silently_rescoped(self):
        self.receipt["installation_fingerprint"] = "a" * 64
        self.save()
        with self.assertRaisesRegex(ValueError, "installation"): import_receipt(self.path, apply=True)

    def test_empty_executed_api_list_does_not_infer_factory_calls(self):
        self.report.pop("executed_apis")
        self.change_report(self.report)
        result = import_receipt(self.path, apply=True)
        self.assertEqual(result["imported_apis"], [])
        self.assertEqual(capabilities._EVIDENCE, {})
        self.assertEqual(capabilities.query_capabilities(kind="workflow", version="261")["total"], 1)

    def test_failed_product_envelope_is_not_a_valid_receipt(self):
        path = self.root / "envelope.json"
        path.write_text('{"ok":false}')
        self.receipt["envelope"]["sha256"] = file_sha256(path)
        self.save()
        with self.assertRaisesRegex(ValueError, "execution"): inspect_receipt(self.path)

    def test_receipt_paths_must_stay_inside_roots(self):
        self.receipt["artifacts"][0]["path"] = "../../outside.rst"
        self.save()
        with self.assertRaises((ValueError, OSError)): import_receipt(self.path, apply=True)

    def test_reimport_is_idempotent_and_different_receipt_does_not_overwrite(self):
        original = import_receipt(self.path, apply=True)
        target = Path(original["index_path"])
        before = target.read_bytes()
        self.assertTrue(import_receipt(self.path, apply=True)["already_imported"])
        self.receipt["observed_at"] = "2026-01-03T00:00:00Z"
        self.save()
        with self.assertRaisesRegex(ValueError, "already indexed"):
            import_receipt(self.path, apply=True)
        self.assertEqual(target.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
