"""Synthetic Engineering Data bridge tests. No product or solver is launched."""
from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ansys_mcp import mechanical as host
from ansys_mcp.mechanical_dispatch import dispatch
from ansys_mcp.workflow import validate_workflow


PROJECT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("engineering_data_fixture", PROJECT / "examples" / "scripts" / "engineering_data_acceptance.py")
fixture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixture)


class Property:
    def __init__(self, name):
        self.Name = name
        self.data = {}

    def SetData(self, *, Variables, Values, SheetName="", Index=0):
        self.data.update(zip(Variables, Values))

    def GetData(self, *, Variables, AsDictionary=False):
        return [self.data[name] for name in Variables]


class Material:
    def __init__(self, owner, name):
        self.owner = owner
        self.DisplayText = name
        self.properties = {}

    def CreateProperty(self, *, Name, Behavior=None):
        self.properties[Name] = Property(Name)
        return self.properties[Name]

    def GetProperty(self, *, Name):
        return self.properties[Name]

    def Delete(self):
        del self.owner.materials[self.DisplayText]


class EngineeringData:
    def __init__(self):
        self.materials = {}

    def CreateMaterial(self, *, Name):
        if Name in self.materials:
            raise ValueError("Material already exists.")
        self.materials[Name] = Material(self, Name)
        return self.materials[Name]

    def WriteMaterials(self, *, MaterialList, FilePath, Format, UnitSystem, OverwriteTarget=False):
        # A mock serialization fixture, deliberately not claimed to be MatML.
        mode = "w" if OverwriteTarget else "x"
        with Path(FilePath).open(mode) as stream:
            json.dump({value.DisplayText: {key: prop.data for key, prop in value.properties.items()} for value in MaterialList}, stream)

    def ImportMaterial(self, *, Name, Source):
        record = json.loads(Path(Source).read_text())[Name]
        material = self.CreateMaterial(Name=Name)
        for name, data in record.items():
            material.CreateProperty(Name=name).data = data
        return material


class Backend(host.ProductBackend):
    instances = []

    def __init__(self):
        super().__init__()
        self.version = "synthetic"
        self.ed = EngineeringData()
        self.body = SimpleNamespace(Name="Acceptance block", Material="Unassigned", DataModelObjectCategory="Body")
        self.refresh_count = 0
        self.namespace = {"EngineeringData": self.ed, "EngData": self.ed, "DataModelObjectCategory": SimpleNamespace(Body="Body"),
                          "DataModel": SimpleNamespace(GetObjectsByName=lambda name: [self.body] if name == self.body.Name else [],
                                                       GetObjectsByType=lambda category: [self.body] if category == "Body" else []),
                          "Model": SimpleNamespace(Materials=SimpleNamespace(RefreshMaterials=self.refresh))}
        self.instances.append(self)

    def refresh(self):
        self.refresh_count += 1

    def start(self, recipe, directory):
        pass

    def execute(self, request, *, engineering_data=False):
        self.test_namespace = "engineering_data" if engineering_data else "mechanical"
        return json.loads(dispatch(request, self.namespace))

    def project_save(self, path, *, overwrite):
        Path(path).write_text("synthetic project", encoding="utf-8")

    def close(self):
        pass


class EngineeringDataRecipeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.geometry = self.root / "fixture.step"
        self.geometry.write_text("synthetic geometry placeholder", encoding="utf-8")
        self.env = patch.dict(os.environ, {"ANSYS_MCP_ALLOWED_ROOTS": str(self.root)})
        self.env.start()
        self.marker = host._WORKER_CONTEXT.set(True)
        Backend.instances = []

    def tearDown(self):
        for session in host._SESSIONS.values():
            session.executor.shutdown(wait=True)
        host._SESSIONS.clear()
        host._WORKER_CONTEXT.reset(self.marker)
        self.env.stop()
        self.temp.cleanup()

    def test_fixture_schema_and_declared_export_paths_without_launch(self):
        workflow = fixture.build_workflow(str(self.geometry), str(self.root), material_name="Synthetic fixture")
        with patch.object(host, "_dependencies", side_effect=AssertionError("No product import allowed")):
            self.assertTrue(validate_workflow(workflow=workflow, validation_mode="schema")["ok"])
        recipe = workflow["steps"][0]["recipe"]
        export = next(operation for operation in recipe["operations"] if operation["id"] == "export_library")
        self.assertEqual((export["target"], export["member"]), ("EngData", "WriteMaterials"))
        self.assertEqual(export["kwargs"]["MaterialList"], [{"$type": "local_ref", "name": "created_material"}])
        selection = next(operation for operation in recipe["operations"] if operation["id"] == "find_unique_body")
        self.assertNotIn("name", selection)
        self.assertEqual(selection["category"]["name"], "Body")
        outputs = host.mechanical_produced_paths(recipe)
        self.assertTrue(any(path.endswith("fixture_material.xml") for path in outputs))
        self.assertFalse(any(operation.get("op") == "script" for operation in recipe["operations"]))

    def test_structured_material_roundtrip_and_assignment_readback_with_synthetic_backend(self):
        workflow = fixture.build_workflow(str(self.geometry), str(self.root), material_name="Synthetic fixture")
        with patch.object(host, "_dependencies", return_value=(None, None)), patch.object(host, "WorkbenchBackend", Backend):
            result = host.run_mechanical_recipe(workflow["steps"][0]["recipe"], self.root, self.root / "runs", 5)
        self.assertTrue(result["ok"], result)
        values = result["outputs"]["values"]
        self.assertEqual(values["density_initial"], [[fixture.DENSITY_INITIAL]])
        self.assertEqual(values["density_reimported"], [[fixture.DENSITY_INITIAL]])
        self.assertEqual(values["density_modified"], [[fixture.DENSITY_MODIFIED]])
        self.assertEqual(values["assignment"], "Synthetic fixture")
        self.assertEqual(values["body_name"], "Acceptance block")
        self.assertEqual(Backend.instances[0].refresh_count, 1)
        self.assertGreaterEqual(result["outputs"]["session"]["generation"], 3)

    def test_duplicate_material_fails_before_deleting_existing_data(self):
        backend = Backend()
        existing = backend.ed.CreateMaterial(Name="Synthetic fixture")
        workflow = fixture.build_workflow(str(self.geometry), str(self.root), material_name="Synthetic fixture")
        with patch.object(host, "_dependencies", return_value=(None, None)), patch.object(host, "WorkbenchBackend", return_value=backend):
            result = host.run_mechanical_recipe(workflow["steps"][0]["recipe"], self.root, self.root / "runs", 5)
        self.assertFalse(result["ok"])
        self.assertIs(backend.ed.materials["Synthetic fixture"], existing)

    def test_ambiguous_body_selection_does_not_assign_a_material(self):
        backend = Backend()
        other = SimpleNamespace(Name="Other body", Material="Protected", DataModelObjectCategory="Body")
        backend.namespace["DataModel"].GetObjectsByType = lambda category: [backend.body, other]
        workflow = fixture.build_workflow(str(self.geometry), str(self.root), material_name="Synthetic fixture")
        with patch.object(host, "_dependencies", return_value=(None, None)), patch.object(host, "WorkbenchBackend", return_value=backend):
            result = host.run_mechanical_recipe(workflow["steps"][0]["recipe"], self.root, self.root / "runs", 5)
        self.assertFalse(result["ok"])
        self.assertEqual(backend.body.Material, "Unassigned")
        self.assertEqual(other.Material, "Protected")
        self.assertEqual(result["diagnostics"][0]["code"], "MECHANICAL_RESULT_LIMIT")


if __name__ == "__main__":
    unittest.main()
