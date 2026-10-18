"""Dispatcher contract tests using synthetic objects; these are not product tests."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from ansys_mcp.mechanical_dispatch import dispatch
from ansys_mcp.mechanical_schema import MechanicalRecipe
from pydantic import ValidationError


class Node:
    def __init__(self, name="model"):
        self.Name = name
        self.Value = 1
        self.Children = []

    def AddChild(self, name):
        child = Node(name)
        self.Children.append(child)
        return child

    def Echo(self, value):
        return value


class Quantity:
    def __init__(self, text):
        number, unit = text.split(" [", 1)
        self.Value = float(number)
        self.Unit = unit.rstrip("]")


class DispatcherTests(unittest.TestCase):
    def setUp(self):
        self.node = Node()
        self.namespace = {
            "Model": self.node, "Quantity": Quantity,
            "DataModel": SimpleNamespace(GetObjectsByName=lambda name: [self.node] if name == self.node.Name else []),
            "Ansys": SimpleNamespace(ExampleEnum=SimpleNamespace(Enabled="enabled")),
        }

    def request(self, operation, **overrides):
        packet = {"session_id": "test-session", "generation": 0, "operation": operation}
        packet.update(overrides)
        return json.loads(dispatch(packet, self.namespace))

    def test_create_get_set_named_and_wire_references(self):
        added = self.request({"op": "call", "target": "Model", "member": "AddChild", "args": ["child"], "store_as": "child"})
        reference = added["value"]
        self.assertEqual(reference["namespace"], "mechanical")
        changed = self.request({"op": "set", "target": reference, "member": "Value", "value": 7})
        self.assertEqual(changed["value"], 7)
        named = self.request({"op": "get", "target": {"$type": "local_ref", "name": "child"}, "member": "Value"})
        self.assertEqual(named["value"], 7)

    def test_cross_session_generation_and_namespace_refs_rejected(self):
        reference = self.request({"op": "call", "target": "Model", "member": "AddChild", "args": ["child"]})["value"]
        for change in ({"session_id": "another"}, {"generation": 1}, {"namespace": "engineering_data"}):
            invalid = dict(reference, **change)
            result = self.request({"op": "get", "target": invalid, "member": "Name"})
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], "MECHANICAL_STALE_REFERENCE")

    def test_named_reference_checks_deletion_and_object_id_reuse(self):
        child = self.node.AddChild("child")
        child.ObjectId = 42
        self.namespace["DataModel"].GetObjectById = lambda identity: child
        self.request({"op": "get", "member": "Children", "store_as": "children"})
        target = {"$type": "local_ref", "name": "children", "index": 0}
        self.assertTrue(self.request({"op": "get", "target": target, "member": "Name"})["ok"])
        self.namespace["DataModel"].GetObjectById = lambda identity: None
        self.assertEqual(self.request({"op": "get", "target": target, "member": "Name"})["error"]["code"], "MECHANICAL_STALE_REFERENCE")
        replacement = Node("reused-id")
        replacement.ObjectId = 42
        self.namespace["DataModel"].GetObjectById = lambda identity: replacement
        self.assertEqual(self.request({"op": "get", "target": target, "member": "Name"})["error"]["code"], "MECHANICAL_STALE_REFERENCE")

    def test_engineering_data_state_does_not_discard_mechanical_handles(self):
        reference = self.request({"op": "call", "member": "AddChild", "args": ["retained"]})["value"]
        self.namespace["EngineeringData"] = Node("materials")
        self.assertTrue(self.request({"op": "inspect", "target": "EngineeringData"}, namespace="engineering_data")["ok"])
        returned = self.request({"op": "get", "target": reference, "member": "Name"})
        self.assertEqual(returned["value"], "retained")

    def test_public_engineering_data_namespace_accepts_material_references(self):
        calls = []
        self.namespace["EngData"] = SimpleNamespace(WriteMaterials=lambda **kwargs: calls.append(kwargs))
        self.request({"op": "get", "target": "Model", "member": "Children", "store_as": "materials"}, namespace="engineering_data")
        result = self.request({"op": "call", "target": "EngData", "member": "WriteMaterials",
                               "kwargs": {"MaterialList": {"$type": "local_ref", "name": "materials"},
                                          "FilePath": {"$type": "path", "path": "synthetic.xml", "role": "output"}}},
                              namespace="engineering_data")
        self.assertTrue(result["ok"], result)
        self.assertEqual(calls, [{"MaterialList": [], "FilePath": "synthetic.xml"}])

    def test_model_alias_refreshed_after_project_replacement(self):
        data_model = self.namespace["DataModel"]
        data_model.Project = SimpleNamespace(Model=self.node)
        self.namespace["ExtAPI"] = SimpleNamespace(DataModel=data_model)
        self.assertEqual(self.request({"op": "get", "member": "Name"})["value"], "model")
        data_model.Project.Model = Node("replacement")
        self.assertEqual(self.request({"op": "get", "member": "Name"}, generation=1)["value"], "replacement")

    def test_typed_quantity_enum_and_collection_index(self):
        quantity = self.request({"op": "call", "target": "Model", "member": "Echo", "args": [{"$type": "quantity", "value": 2, "unit": "mm"}]})
        self.assertEqual(quantity["value"], {"$type": "quantity", "value": 2.0, "unit": "mm"})
        enum = self.request({"op": "call", "target": "Model", "member": "Echo", "args": [{"$type": "enum", "type": "Ansys.ExampleEnum", "name": "Enabled"}]})
        self.assertEqual(enum["value"], "enabled")
        self.node.Children.append(Node("first"))
        self.request({"op": "get", "target": "Model", "member": "Children", "store_as": "children"})
        name = self.request({"op": "get", "target": {"$type": "local_ref", "name": "children", "index": 0}, "member": "Name"})
        self.assertEqual(name["value"], "first")

    def test_inspection_does_not_execute_property_getters(self):
        class Dangerous:
            @property
            def Getter(self):
                raise AssertionError("getter must not run during inspect")
        self.namespace["Model"] = Dangerous()
        result = self.request({"op": "inspect", "target": "Model", "limit": 20})
        self.assertTrue(result["ok"])
        self.assertIn("Getter", result["value"]["members"])

    def test_find_limit_is_not_silent_truncation(self):
        self.namespace["DataModel"].GetObjectsByName = lambda name: [Node(), Node()]
        result = self.request({"op": "find", "name": "model", "limit": 1})
        self.assertEqual(result["error"]["code"], "MECHANICAL_RESULT_LIMIT")

    def test_explicit_constructor_and_overload(self):
        class Overloaded:
            def __init__(self):
                self.Overloads = {(int,): lambda number: number + 10}
        self.namespace["Ansys"].Node = Node
        self.namespace["Ansys"].Integer = int
        self.node.Select = Overloaded()
        created = self.request({"op": "construct", "target": "Ansys.Node", "args": ["constructed"]})
        self.assertEqual(created["value"]["$type"], "ref")
        overloaded = self.request({"op": "call", "target": "Model", "member": "Select", "args": [5], "overload": ["Ansys.Integer"]})
        self.assertEqual(overloaded["value"], 15)

    def test_large_output_written_to_json_artifact(self):
        self.node.Text = "x" * 5000
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            result = self.request({"op": "get", "target": "Model", "member": "Text"}, max_inline_bytes=1000, artifact_path=str(path))
            self.assertNotIn("value", result)
            self.assertEqual(result["artifact"]["path"], str(path))
            self.assertEqual(json.loads(path.read_text())["value"], self.node.Text)

    def test_trusted_script_invalidates_old_handles(self):
        reference = self.request({"op": "call", "target": "Model", "member": "AddChild", "args": ["before"]})["value"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "script.py"
            path.write_text("mcp_result = Model.Name\n", encoding="utf-8")
            result = self.request({"op": "script", "script_path": str(path)})
        self.assertEqual(result["generation"], 1)
        stale = self.request({"op": "get", "target": reference, "member": "Name"}, generation=1)
        self.assertEqual(stale["error"]["code"], "MECHANICAL_STALE_REFERENCE")

    def test_python_os_root_is_not_a_public_api_root(self):
        result = self.request({"op": "call", "target": "os", "member": "getcwd"})
        self.assertEqual(result["error"]["code"], "MECHANICAL_UNKNOWN_ROOT")

    def test_dotnet_os_and_reflection_roots_require_explicit_trusted_script(self):
        for target in ("System.IO.File", "System.Diagnostics.Process", "System.Reflection.Assembly", "System.Type", "System"):
            result = self.request({"op": "inspect", "target": target})
            self.assertEqual(result["error"]["code"], "MECHANICAL_TRUSTED_SCRIPT_REQUIRED")

    def test_script_execution_methods_require_trusted_file(self):
        for member in ("SendCommand", "ExecuteCommand", "RunScript", "ScriptByName"):
            result = self.request({"op": "call", "member": member, "args": ["synthetic code"]})
            self.assertEqual(result["error"]["code"], "MECHANICAL_TRUSTED_SCRIPT_REQUIRED")


class RecipeContractTests(unittest.TestCase):
    def test_invalid_operations_and_values_rejected(self):
        for operation in [
            {"op": "unknown"}, {"op": "get"}, {"op": "script"},
            {"op": "get", "target": "Model.__class__", "member": "Name"},
            {"op": "set", "member": "Value", "value": float("nan")},
            {"op": "set", "member": "Value", "value": {"$type": "table", "columns": [{"name": "a"}], "rows": [[1, 2]]}},
        ]:
            with self.subTest(operation=operation), self.assertRaises(ValidationError):
                MechanicalRecipe.model_validate({"operations": [operation]})

    def test_engineering_data_is_workbench_only(self):
        with self.assertRaises(ValidationError):
            MechanicalRecipe.model_validate({"backend": "pymechanical", "operations": [{"op": "engineering_data", "action": "inspect"}]})


if __name__ == "__main__":
    unittest.main()
