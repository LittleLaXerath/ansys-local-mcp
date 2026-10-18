"""Host/session tests with a synthetic backend. No Ansys product is launched."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ansys_mcp import mechanical as host
from ansys_mcp.mechanical_dispatch import dispatch
from ansys_mcp.mechanical_schema import MechanicalRecipe
from ansys_mcp.workflow_paths import PathChecks


class FakeBackend(host.ProductBackend):
    created = []
    gate = None

    def __init__(self):
        super().__init__()
        self.version = "test"
        self.closed = False
        self.calls = []
        self.node = SimpleNamespace(Name="synthetic", Value=0)
        self.namespace = {"Model": self.node}
        self.created.append(self)

    def start(self, recipe, directory):
        self.calls.append("start")

    def execute(self, request, *, engineering_data=False):
        self.calls.append(request["operation"]["op"])
        if self.gate is not None:
            self.gate.wait(timeout=5)
        return json.loads(dispatch(request, self.namespace))

    def project_save(self, path, *, overwrite):
        Path(path).write_text("synthetic project", encoding="utf-8")
        self.project_path = path
        self.calls.append("save")

    def project_open(self, path, *, reopen=False):
        self.project_path = path
        self.calls.append("reopen" if reopen else "open")

    def close(self):
        self.closed = True
        self.calls.append("close")


class MechanicalHostTests(unittest.TestCase):
    def setUp(self):
        self.worker_marker = host._WORKER_CONTEXT.set(True)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.run_dir = self.root / "runs"
        self.env = patch.dict(os.environ, {"ANSYS_MCP_ALLOWED_ROOTS": str(self.root)})
        self.env.start()
        self.dependencies = patch.object(host, "_dependencies", return_value=(None, None))
        self.dependencies.start()
        self.backend = patch.object(host, "WorkbenchBackend", FakeBackend)
        self.backend.start()
        FakeBackend.created = []
        FakeBackend.gate = None

    def tearDown(self):
        if FakeBackend.gate:
            FakeBackend.gate.set()
        for session in list(host._SESSIONS.values()):
            session.executor.shutdown(wait=True)
        host._SESSIONS.clear()
        self.backend.stop()
        self.dependencies.stop()
        self.env.stop()
        host._WORKER_CONTEXT.reset(self.worker_marker)
        self.temp.cleanup()

    def run_recipe(self, recipe, timeout=2):
        return host.run_mechanical_recipe(recipe, self.root, self.run_dir, timeout)

    def test_persistent_session_reused_and_batch_reuse_never_closes_it(self):
        first = self.run_recipe({"operations": [{"op": "get", "member": "Name"}]})
        self.assertTrue(first["ok"], first)
        session_id = first["outputs"]["session"]["id"]
        second = self.run_recipe({"mode": "batch", "session": {"action": "reuse", "id": session_id},
                                  "operations": [{"op": "set", "member": "Value", "value": 42}]})
        self.assertTrue(second["ok"], second)
        self.assertEqual(len(FakeBackend.created), 1)
        self.assertFalse(FakeBackend.created[0].closed)
        self.assertEqual(FakeBackend.created[0].node.Value, 42)
        closed = self.run_recipe({"session": {"action": "close", "id": session_id}})
        self.assertEqual(closed["status"], "closed")
        self.assertTrue(FakeBackend.created[0].closed)

    def test_new_batch_closes_only_its_own_session(self):
        result = self.run_recipe({"mode": "batch", "operations": [{"op": "get", "member": "Name"}]})
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["outputs"]["session"]["closed"])

    def test_timeout_keeps_original_request_and_rejects_replay_while_busy(self):
        FakeBackend.gate = threading.Event()
        result = self.run_recipe({"operations": [{"op": "set", "member": "Value", "value": 9}]}, timeout=0.02)
        self.assertEqual(result["status"], "unknown")
        session_id = result["outputs"]["session"]["id"]
        request_id = result["outputs"]["request_id"]
        replay = self.run_recipe({"session": {"action": "reuse", "id": session_id}, "operations": [{"op": "set", "member": "Value", "value": 10}]})
        self.assertEqual(replay["diagnostics"][0]["code"], "MECHANICAL_SESSION_BUSY")
        self.assertEqual(host.mechanical_request_status(session_id, request_id)["status"], "running")
        FakeBackend.gate.set()
        for _ in range(100):
            observed = host.mechanical_request_status(session_id, request_id)
            if observed["status"] != "running":
                break
            time.sleep(0.005)
        self.assertTrue(observed["ok"], observed)
        self.assertEqual(FakeBackend.created[0].calls.count("set"), 1)
        self.assertEqual(FakeBackend.created[0].node.Value, 9)

    def test_project_save_reopen_invalidates_refs_and_verifies_artifact(self):
        result = self.run_recipe({"operations": [
            {"op": "project_save", "path": "project.wbpj"}, {"op": "project_reopen"},
            {"op": "get", "member": "Name"},
        ]})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["outputs"]["session"]["generation"], 1)
        self.assertTrue((self.root / "project.wbpj").is_file())
        self.assertEqual(len(result["artifacts"]), 1)

    def test_missing_saved_artifact_is_failure(self):
        with patch.object(FakeBackend, "project_save", return_value=None):
            result = self.run_recipe({"project": {"save_path": "missing.wbpj"}})
        self.assertFalse(result["ok"])
        self.assertEqual(result["diagnostics"][0]["code"], "MECHANICAL_ARTIFACT_MISSING")

    def test_disconnect_is_unknown_and_never_switches_backend(self):
        with patch.object(FakeBackend, "execute", side_effect=ConnectionError("synthetic transport loss")):
            result = self.run_recipe({"operations": [{"op": "set", "member": "Value", "value": 2}]})
        self.assertEqual(result["status"], "unknown")
        session_id = result["outputs"]["session"]["id"]
        retry = self.run_recipe({"session": {"action": "reuse", "id": session_id}, "operations": [{"op": "get", "member": "Value"}]})
        self.assertEqual(retry["diagnostics"][0]["code"], "MECHANICAL_SESSION_DISCONNECTED")
        self.assertEqual(len(FakeBackend.created), 1)

    def test_rpc_error_code_method_never_enters_json_diagnostic(self):
        class RpcError(Exception):
            def code(self):
                return "UNAVAILABLE"
        with patch.object(FakeBackend, "execute", side_effect=RpcError("synthetic transport loss")):
            result = self.run_recipe({"operations": [{"op": "get", "member": "Name"}]})
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["diagnostics"][0]["code"], "MECHANICAL_EXECUTION_UNKNOWN")
        self.assertTrue(result["outputs"]["request_id"])
        json.dumps(result)

    def test_evidence_permission_error_preserves_completed_mutation(self):
        original_start = FakeBackend.start
        def start_for_evidence_test(backend, recipe, directory):
            original_start(backend, recipe, directory)
            backend.verified_transport = True
        # The provider remains synthetic, and the recorder is replaced before it
        # can create evidence. Exercise only the completed-mutation boundary.
        with patch.object(FakeBackend, "start", start_for_evidence_test), patch(
            "ansys_mcp.capabilities.record_runtime_evidence", side_effect=PermissionError("synthetic evidence write denied")
        ) as record:
            result = self.run_recipe({"operations": [
                {"op": "set", "member": "Value", "value": 42},
                {"op": "get", "member": "Value"},
            ]})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["outputs"]["operations"][-1]["value"], 42)
        self.assertEqual(FakeBackend.created[0].node.Value, 42)
        self.assertEqual(FakeBackend.created[0].calls.count("set"), 1)
        self.assertEqual(FakeBackend.created[0].calls.count("get"), 1)
        self.assertEqual(record.call_count, 2)

    def test_schema_validation_handles_declared_future_files_without_launch(self):
        checks = PathChecks(self.root, "schema", allow_references=True)
        host.validate_mechanical_recipe_paths({"project": {"geometry_path": "future.step"},
                                              "operations": [{"op": "script", "script_path": "${steps.write.outputs.path}"}]}, self.root, checks)
        self.assertEqual(FakeBackend.created, [])
        self.assertEqual(host.mechanical_produced_paths({"project": {"save_path": "a.wbpj"}, "operations": [{"op": "project_save", "path": "b.wbpj"}]}), ["a.wbpj", "b.wbpj"])

    def test_outside_root_script_rejected_before_backend_launch(self):
        outside = self.root.parent / "outside-script.py"
        result = self.run_recipe({"operations": [{"op": "script", "script_path": str(outside)}]})
        self.assertFalse(result["ok"])
        self.assertEqual(FakeBackend.created, [])

    def test_raw_absolute_and_untyped_relative_file_api_paths_rejected(self):
        for operation in [
            {"op": "call", "member": "Echo", "args": [str(self.root.parent / "private.step")]},
            {"op": "call", "member": "Open", "args": ["relative.mechdb"]},
            {"op": "set", "member": "FilePath", "value": "relative.step"},
            {"op": "call", "member": "Echo", "kwargs": {"FilePath": "relative.step"}},
        ]:
            with self.subTest(operation=operation):
                result = self.run_recipe({"operations": [operation]})
                self.assertFalse(result["ok"])
        self.assertEqual(FakeBackend.created, [])

    def test_typed_file_and_quantity_units_are_not_confused(self):
        recipe = {"operations": [{"op": "call", "member": "ExportToTextFile", "args": [
            {"$type": "path", "path": "output/result.txt", "role": "output"},
        ]}, {"op": "set", "member": "Value", "value": {"$type": "quantity", "value": 2, "unit": "N/mm"}}]}
        self.assertEqual(host.validate_mechanical_recipe_paths(recipe, self.root, PathChecks(self.root, "schema")), [])
        self.assertEqual(host.mechanical_produced_paths(recipe), ["output/result.txt"])

    def test_engineering_data_file_kwargs_and_api_preferences(self):
        unsafe = {"operations": [{"op": "engineering_data", "action": "call", "member": "ImportMaterial", "kwargs": {"Source": "sub/../../outside.xml", "Name": "Example"}}]}
        with self.assertRaises(ValueError):
            host.validate_mechanical_recipe_paths(unsafe, self.root, PathChecks(self.root, "schema"))
        safe = {"operations": [{"op": "engineering_data", "action": "call", "member": "ImportMaterial", "kwargs": {"Source": {"$type": "path", "path": "material.xml", "role": "input"}, "Name": "Example", "Format": "MatML"}},
                               {"op": "engineering_data", "action": "call", "member": "ImportMaterial", "args": ["Example", {"$type": "path", "path": "material.xml", "role": "input"}]},
                               {"op": "call", "member": "Import", "args": [{"$type": "path", "path": "part.step", "role": "input"}, {"$type": "enum", "type": "Ansys.Example", "name": "Automatic"}, {"$type": "local_ref", "name": "preferences"}]}]}
        self.assertEqual(host.validate_mechanical_recipe_paths(safe, self.root, PathChecks(self.root, "schema")), [])
        unsafe_position = {"operations": [{"op": "engineering_data", "action": "call", "member": "ImportMaterial", "args": ["Example", "relative.xml"]}]}
        with self.assertRaises(ValueError):
            host.validate_mechanical_recipe_paths(unsafe_position, self.root, PathChecks(self.root, "schema"))

    def test_readonly_session_summary_does_not_import_dependencies(self):
        with patch.object(host, "_dependencies", side_effect=AssertionError("unexpected provider import")):
            self.assertIsInstance(host.session_summary(), list)

    def test_engineering_data_write_materials_guards_file_not_material_list(self):
        materials = [{"$type": "local_ref", "name": "created_material"}]
        safe = {"operations": [{"op": "engineering_data", "action": "call", "target": "EngData", "member": "WriteMaterials",
                                "args": [materials, {"$type": "path", "path": "material.xml", "role": "output"}]}]}
        self.assertEqual(host.validate_mechanical_recipe_paths(safe, self.root, PathChecks(self.root, "schema")), [])
        for unsafe_path in ("material.xml", "sub/../../outside.xml"):
            unsafe = {"operations": [{"op": "engineering_data", "action": "call", "target": "EngData", "member": "WriteMaterials",
                                      "args": [materials, unsafe_path]}]}
            with self.subTest(path=unsafe_path), self.assertRaises(ValueError):
                host.validate_mechanical_recipe_paths(unsafe, self.root, PathChecks(self.root, "schema"))
        traversal = {"operations": [{"op": "call", "member": "SomePublicMethod", "args": ["sub/../../outside.xml"]}]}
        with self.assertRaises(ValueError):
            host.validate_mechanical_recipe_paths(traversal, self.root, PathChecks(self.root, "schema"))
        delimiter = {"operations": [{"op": "call", "member": "Echo", "args": [".", "Step Controls/Endtime"]}]}
        self.assertEqual(host.validate_mechanical_recipe_paths(delimiter, self.root, PathChecks(self.root, "schema")), [])


if __name__ == "__main__":
    unittest.main()
