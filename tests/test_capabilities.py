from __future__ import annotations

import copy
import importlib.metadata
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ansys_mcp import capabilities


class CapabilityCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {"ANSYS_MCP_ALLOWED_ROOTS": str(self.root)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        module = self.root / "Ansys" / "ACT" / "Automation" / "Mechanical"
        module.mkdir(parents=True)
        (self.root / "__init__.py").write_text('"""Release module."""', encoding="utf-8")
        (module / "__init__.py").write_text('''
import typing
import ansys.mechanical.stubs.v261.Ansys.ACT.Automation.Mechanical.MeshControls as MeshControls
class Model(object):
    """Public model."""
    def AddExplicitDynamicsAnalysis(self) -> Analysis: ...
    @property
    def Name(self) -> str: ...
    @Name.setter
    def Name(self, value: str) -> None: ...
    def _implementation(self): ...
class AnalysisType:
    ExplicitDynamics = 10
    LSDyna = 12
_private = 1
''', encoding="utf-8")
        self.catalog = capabilities.build_catalog("261", stubs_root=self.root)
        with capabilities._LOCK:
            self.saved_evidence = copy.deepcopy(capabilities._EVIDENCE)
            self.saved_loaded_evidence = dict(capabilities._LOADED_EVIDENCE)
            capabilities._EVIDENCE.clear()
            capabilities._LOADED_EVIDENCE.clear()
        self.addCleanup(self.restore_evidence)

    def restore_evidence(self):
        with capabilities._LOCK:
            capabilities._EVIDENCE.clear()
            capabilities._EVIDENCE.update(self.saved_evidence)
            capabilities._LOADED_EVIDENCE.clear()
            capabilities._LOADED_EVIDENCE.update(self.saved_loaded_evidence)

    def test_manifest_accounts_duplicate_properties_without_losing_signatures(self):
        manifest = self.catalog["manifest"]
        self.assertEqual(manifest["source_files"], 2)
        self.assertEqual(manifest["parsed_files"], 2)
        self.assertEqual(manifest["public_declarations"], 8)
        self.assertEqual(manifest["accounted_declarations"], 8)
        names = [entry["qualified_name"] for entry in self.catalog["entries"]]
        self.assertNotIn("Ansys.ACT.Automation.Mechanical.Model._implementation", names)
        name = next(entry for entry in self.catalog["entries"] if entry["name"] == "Name")
        self.assertEqual(len(name["signatures"]), 2)
        self.assertEqual(len(name["declarations"]), 2)
        self.assertTrue(all(entry["runtime_reachable"] is None for entry in self.catalog["entries"]))
        self.assertTrue(all(entry["status"] == "catalogued" for entry in self.catalog["entries"]))
        self.assertNotIn(str(self.root), json.dumps(self.catalog))

    def test_pagination_and_component_filter_preserve_catalog_evidence_boundary(self):
        with patch.object(capabilities, "build_catalog", return_value=self.catalog):
            first = capabilities.query_capabilities(component="ed", limit=1)
            second = capabilities.query_capabilities(component="ed", offset=1, limit=1)
            self.assertEqual(first["total"], 2)
            self.assertEqual(first["next_offset"], 1)
            self.assertNotEqual(first["items"][0]["id"], second["items"][0]["id"])
            self.assertEqual(capabilities.query_capabilities(status="verified")["total"], 0)

    def test_explicit_observation_only_upgrades_exact_member(self):
        qualified = "Ansys.ACT.Automation.Mechanical.Model.AddExplicitDynamicsAnalysis"
        capabilities.record_runtime_evidence({"version": "261", "qualified_name": qualified,
            "runtime_reachable": True, "validation_stage": "object", "evidence_source": "test_product_probe"})
        with patch.object(capabilities, "build_catalog", return_value=self.catalog):
            result = capabilities.query_capabilities(version="261", component="ed")
        observed = next(item for item in result["items"] if item["qualified_name"] == qualified)
        self.assertEqual(observed["validation_stage"], "object")
        self.assertEqual(observed["status"], "reachable")
        enum = next(item for item in result["items"] if item["kind"] == "attribute")
        self.assertIsNone(enum["runtime_reachable"])
        self.assertTrue(all(entry["runtime_reachable"] is None for entry in self.catalog["entries"]))

    def test_family_filters_include_product_abbreviated_settings_types(self):
        settings = self.root / "Ansys" / "ACT" / "Automation" / "Mechanical" / "AnalysisSettings"
        settings.mkdir()
        (settings / "__init__.py").write_text(
            "class EXDAnalysisSettings:\n    def SetStepEndTime(self, step, time): ...\n"
            "class RBDAnalysisSettings:\n    def SetStepEndTime(self, step, time): ...\n", encoding="utf-8")
        catalog = capabilities.build_catalog("261", stubs_root=self.root)
        with patch.object(capabilities, "build_catalog", return_value=catalog):
            explicit = capabilities.query_capabilities(version="261", component="ed")["items"]
            rigid = capabilities.query_capabilities(version="261", component="rbd")["items"]
        self.assertTrue(any(item["qualified_name"].endswith("EXDAnalysisSettings.SetStepEndTime") for item in explicit))
        self.assertTrue(any(item["qualified_name"].endswith("RBDAnalysisSettings.SetStepEndTime") for item in rigid))

    def test_rejected_evidence_cannot_claim_a_failed_solve(self):
        with self.assertRaises(ValueError):
            capabilities.record_runtime_evidence({"qualified_name": "Ansys.Model.Solve", "runtime_reachable": False,
                                                 "validation_stage": "solve"})
        with self.assertRaises(ValueError):
            capabilities.record_runtime_evidence({"qualified_name": "Ansys.Model", "runtime_reachable": True,
                                                 "evidence_source": "C:/private/log.txt"})

    def test_runtime_only_inherited_members_are_visible_without_changing_source_counts(self):
        capabilities.record_runtime_evidence({"version": "261", "qualified_name": "Ansys.ACT.Automation.Mechanical.Model.ObjectId",
                                              "runtime_reachable": True, "validation_stage": "object"})
        with patch.object(capabilities, "build_catalog", return_value=self.catalog):
            result = capabilities.query_capabilities(version="261", status="reachable")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["kind"], "runtime_member")
        self.assertEqual(result["manifest"]["public_declarations"], 8)
        self.assertEqual(result["manifest"]["runtime_only_entries"], 1)

    def test_a_shallow_success_does_not_erase_a_previously_validated_stage(self):
        for stage in ("result", "reachable"):
            capabilities.record_runtime_evidence({"version": "261", "qualified_name": "Ansys.ACT.Automation.Mechanical.Model.Name",
                                                  "runtime_reachable": True, "validation_stage": stage})
        with patch.object(capabilities, "build_catalog", return_value=self.catalog):
            result = capabilities.query_capabilities(version="261", status="verified")
        self.assertEqual(result["items"][0]["validation_stage"], "result")
        self.assertEqual(result["items"][0]["last_observation_stage"], "reachable")

    def test_bad_source_aborts_instead_of_reporting_complete(self):
        (self.root / "broken.py").write_text("class Broken(\n", encoding="utf-8")
        with self.assertRaises(SyntaxError):
            capabilities.build_catalog("261", stubs_root=self.root)

    def test_source_fingerprint_changes_with_upstream_content(self):
        (self.root / "__init__.py").write_text('"""Changed release module."""', encoding="utf-8")
        changed = capabilities.build_catalog("261", stubs_root=self.root)
        self.assertNotEqual(self.catalog["manifest"]["source_tree_sha256"], changed["manifest"]["source_tree_sha256"])

    def test_private_evidence_survives_reload_and_is_scoped_to_installation_and_backend(self):
        scope = {"installation_fingerprint": "a" * 64, "version": "261", "current_installation": True}
        qualified = "Ansys.ACT.Automation.Mechanical.Model.AddExplicitDynamicsAnalysis"
        with patch.object(capabilities, "_runtime_scope", return_value=scope), patch.object(capabilities, "build_catalog", return_value=self.catalog):
            for backend, reachable, stage in (("workbench", True, "object"), ("pymechanical", False, "catalogued")):
                capabilities.record_runtime_evidence({"version": "261", "qualified_name": qualified, "backend": backend,
                    "runtime_reachable": reachable, "validation_stage": stage})
            capabilities._EVIDENCE.clear()
            capabilities._LOADED_EVIDENCE.clear()
            reloaded = capabilities.query_capabilities(version="261", status="reachable")
            self.assertEqual(reloaded["total"], 1)
            self.assertEqual(len(reloaded["items"][0]["runtime_observations"]), 2)
            self.assertEqual(reloaded["items"][0]["backend"], "workbench")
        changed = {**scope, "installation_fingerprint": "b" * 64}
        with patch.object(capabilities, "_runtime_scope", return_value=changed), patch.object(capabilities, "build_catalog", return_value=self.catalog):
            self.assertEqual(capabilities.query_capabilities(version="261", status="reachable")["total"], 0)

    def external_observation(self, scope, *, backend="workbench", reachable=True, stage="object", observed_at="2026-01-02T00:00:00+00:00"):
        return {"product": "mechanical", "version": "261", "qualified_name": "Ansys.ACT.Automation.Mechanical.Model.AddExplicitDynamicsAnalysis",
                "runtime_reachable": reachable, "validation_stage": stage, "evidence_source": "external_actor_fixture",
                "observed_at": observed_at, "backend": backend, **scope}

    def test_evidence_appearing_after_first_query_is_loaded_without_server_restart(self):
        scope = {"installation_fingerprint": "c" * 64, "version": "261", "current_installation": True}
        with patch.object(capabilities, "_runtime_scope", return_value=scope), patch.object(capabilities, "build_catalog", return_value=self.catalog):
            self.assertEqual(capabilities.query_capabilities(version="261", status="reachable")["total"], 0)
            target = capabilities._evidence_file("261", scope["installation_fingerprint"])
            target.parent.mkdir(parents=True)
            target.write_text(json.dumps(self.external_observation(scope)) + "\n", encoding="utf-8")
            result = capabilities.query_capabilities(version="261", status="reachable")
            self.assertEqual(result["total"], 1)
            self.assertEqual(result["items"][0]["backend"], "workbench")

    def test_external_append_refreshes_same_process_and_preserves_scope_backend_and_newest_timestamp(self):
        scope = {"installation_fingerprint": "d" * 64, "version": "261", "current_installation": True}
        target = capabilities._evidence_file("261", scope["installation_fingerprint"])
        target.parent.mkdir(parents=True)
        target.write_text(json.dumps(self.external_observation(scope)) + "\n", encoding="utf-8")
        with patch.object(capabilities, "_runtime_scope", return_value=scope), patch.object(capabilities, "build_catalog", return_value=self.catalog):
            self.assertEqual(capabilities.query_capabilities(version="261", status="verified")["total"], 0)
            observations = [
                self.external_observation(scope, stage="result", observed_at="2026-01-03T00:00:00Z"),
                self.external_observation(scope, reachable=False, stage="catalogued", observed_at="2026-01-01T00:00:00Z"),
                self.external_observation(scope, reachable=False, stage="catalogued", observed_at="2026-01-03T01:00:00+02:00"),
                self.external_observation(scope, backend="pymechanical", reachable=False, stage="catalogued"),
                self.external_observation({**scope, "installation_fingerprint": "e" * 64}, stage="result"),
                self.external_observation({**scope, "version": "252"}, stage="result"),
            ]
            with target.open("ab") as stream:
                for observation in observations:
                    stream.write((json.dumps(observation) + "\n").encode("utf-8"))
                stream.write(b'[]\n\xfftruncated-tail')
            result = capabilities.query_capabilities(version="261", status="verified")
            self.assertEqual(result["total"], 1)
            item = result["items"][0]
            self.assertEqual(item["validation_stage"], "result")
            self.assertEqual(item["backend"], "workbench")
            self.assertEqual(len(item["runtime_observations"]), 2)
            self.assertEqual({value["backend"] for value in item["runtime_observations"]}, {"workbench", "pymechanical"})

    def test_transient_evidence_read_failure_can_retry_the_same_file_signature(self):
        scope = {"installation_fingerprint": "f" * 64, "version": "261", "current_installation": True}
        target = capabilities._evidence_file("261", scope["installation_fingerprint"])
        target.parent.mkdir(parents=True)
        target.write_text(json.dumps(self.external_observation(scope)) + "\n", encoding="utf-8")
        original_open = Path.open
        def temporarily_denied(path, *args, **kwargs):
            if path == target:
                raise PermissionError("Synthetic sharing violation")
            return original_open(path, *args, **kwargs)
        with patch.object(capabilities, "_runtime_scope", return_value=scope), patch.object(capabilities, "build_catalog", return_value=self.catalog):
            with patch.object(Path, "open", temporarily_denied):
                self.assertEqual(capabilities.query_capabilities(version="261", status="reachable")["total"], 0)
            self.assertEqual(capabilities.query_capabilities(version="261", status="reachable")["total"], 1)

    def test_evidence_root_environment_override_is_reported(self):
        scope = {"installation_fingerprint": "a" * 64, "version": "261", "current_installation": True}
        custom = self.root / "custom-evidence"
        with patch.dict(os.environ, {"ANSYS_MCP_EVIDENCE_ROOT": str(custom)}):
            with patch.object(capabilities, "_runtime_scope", return_value=scope):
                target = capabilities._evidence_file("261", scope["installation_fingerprint"])
                # Windows runner TEMP may use an 8.3 alias; the API canonicalizes it.
                expected = (custom / "v261" / (scope["installation_fingerprint"] + ".jsonl")).resolve()
                self.assertEqual(target, expected)
                status = capabilities.capability_evidence_status("261")
        self.assertTrue(status["ok"], status)
        self.assertTrue(status["configured_by_environment"])
        self.assertEqual(Path(status["root"]), custom.resolve())
        self.assertEqual(status["installation_fingerprint"], scope["installation_fingerprint"])

    def test_evidence_root_outside_allowed_roots_is_a_configuration_error(self):
        outside = self.root.parent / "outside-evidence-scope"
        with patch.dict(os.environ, {"ANSYS_MCP_EVIDENCE_ROOT": str(outside)}):
            status = capabilities.capability_evidence_status("261")
            self.assertFalse(status["ok"])
            self.assertEqual(status["status"], "configuration_error")
            self.assertIn("ANSYS_MCP_EVIDENCE_ROOT", status["error"])
            self.assertIsNone(capabilities._evidence_file("261", "b" * 64))

    def test_evidence_status_counts_records_and_reports_corrupt_tail(self):
        scope = {"installation_fingerprint": "c" * 64, "version": "261", "current_installation": True}
        with patch.object(capabilities, "_runtime_scope", return_value=scope):
            target = capabilities._evidence_file("261", scope["installation_fingerprint"])
            target.parent.mkdir(parents=True)
            target.write_text(
                json.dumps(self.external_observation(scope)) + "\n"
                + json.dumps(self.external_observation(scope, stage="result")) + "\n"
                + '{"product": "mechanical"\n',
                encoding="utf-8",
            )
            status = capabilities.capability_evidence_status("261")
        self.assertTrue(status["ok"], status)
        self.assertTrue(status["exists"])
        self.assertEqual(status["records"]["total"], 2)
        self.assertEqual(status["records"]["by_product"], {"mechanical": 2})
        self.assertEqual(status["records"]["by_stage"], {"object": 1, "result": 1})
        self.assertEqual(status["records"]["unparsed_lines"], 1)
        self.assertEqual(status["diagnostics"], [])

    def test_installed_v261_inventory_covers_all_source_files(self):
        try:
            root, _ = capabilities._package_root("261")
        except (importlib.metadata.PackageNotFoundError, FileNotFoundError):
            self.skipTest("Official v261 stubs are not installed.")
        catalog = capabilities.build_catalog("261")
        sources = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.suffix in {".py", ".pyi"} and path.is_file()}
        manifest = catalog["manifest"]
        self.assertEqual(sources, {entry["file"] for entry in manifest["files"]})
        self.assertEqual(manifest["public_declarations"], sum(entry["public_declarations"] for entry in manifest["files"]))
        self.assertEqual(manifest["public_declarations"], sum(len(entry["declarations"]) for entry in catalog["entries"]))
        self.assertGreater(manifest["unique_entries"], 10000)
        self.assertTrue(any(entry["qualified_name"].endswith("Model.AddExplicitDynamicsAnalysis") for entry in catalog["entries"]))
        self.assertTrue(any(entry["qualified_name"].endswith("Model.AddDesignLifeAnalysis") for entry in catalog["entries"]))


if __name__ == "__main__":
    unittest.main()
