from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ansys_mcp.command_registry import command_specs, validate_command_args
from ansys_mcp.process_supervisor import ProcessResult
from ansys_mcp.schemas import WorkbenchRecipe
from ansys_mcp.workbench_recipe import compile_workbench_recipe, read_workbench_result, validate_workbench_result
from ansys_mcp.workflow import run_workflow, validate_workflow


PROJECT = Path(__file__).resolve().parents[1]


class ModelingRegressionTests(unittest.TestCase):
    def setUp(self):
        scratch = PROJECT / ".ansys_mcp"
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch, prefix="modeling-tests-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"ANSYS_MCP_ALLOWED_ROOTS": str(self.root)})
        self.env.start()
        self.addCleanup(self.env.stop)
        specs = {key: replace(spec, executable=Path(sys.executable)) for key, spec in command_specs().items()}
        for target in ("ansys_mcp.workflow.command_specs", "ansys_mcp.command_registry.command_specs"):
            patched = patch(target, return_value=specs)
            patched.start()
            self.addCleanup(patched.stop)

    def workflow(self, steps):
        return {"version": "1", "workdir": str(self.root), "steps": steps}

    def validate(self, steps, mode="runtime"):
        return validate_workflow(workflow=self.workflow(steps), validation_mode=mode)

    def fake_process(self, argv, **kwargs):
        self.assertIn("-R", argv)
        journal = Path(argv[argv.index("-R") + 1])
        system = SimpleNamespace(Name="verified-system")
        system.GetContainer = lambda **kw: SimpleNamespace(
            RunScript=lambda **kw: {"script_text": Path(kw["ScriptFile"]).read_text(encoding="utf-8")}
        )
        namespace = {"GetSystem": lambda **kw: system, "SetScriptVersion": lambda **kw: None}
        exec(journal.read_text(encoding="utf-8"), namespace)
        return ProcessResult(0, 1, False, False, self.root / "stdout.log", self.root / "stderr.log", "", "", 0)

    def test_spaceclaim_switches_and_script_paths(self):
        script = self.root / 'geometry \u51e0\u4f55.py'
        script.write_text("# trusted script", encoding="utf-8")
        args = [f'/RunScript="{script}"', "/Headless=True", "/ExitAfterScript=True",
                "/ScriptAPI=V261", "/ScriptArgs=10,20,relative/data.json", "/ScriptOutput=result.txt"]
        result = validate_command_args("spaceclaim", args, workdir=self.root)
        self.assertEqual(result[0], "/RunScript=" + str(script))
        self.assertEqual(result[1:5], args[1:5])
        self.assertEqual(result[5], "/ScriptOutput=" + str(self.root / "result.txt"))

    def test_option_paths_still_enforce_boundaries_and_input_existence(self):
        for command, args in [
            ("spaceclaim", ["/RunScript=C:/Windows/notepad.exe"]),
            ("spaceclaim", ["/ScriptOutput=../outside.txt"]),
            ("cfx_pre", ["-batch=../outside.pre"]),
            ("cfx_solve", ["-def", "missing.def"]),
            ("cfx_solve", ["-res", "missing.res"]),
        ]:
            with self.subTest(command=command, args=args), self.assertRaises((ValueError, OSError)):
                validate_command_args(command, args, workdir=self.root)

    def test_generated_script_fixed_path_and_output_reference(self):
        for reference in ("generated.py", "${steps.write.outputs.path}"):
            with self.subTest(reference=reference):
                steps = [
                    {"id": "write", "type": "file", "action": "write_text", "path": "generated.py", "content": "# generated"},
                    {"id": "wb", "type": "workbench", "recipe": {"actions": [
                        {"op": "get_system", "name": "SYS", "store_as": "system"},
                        {"op": "get_container", "target": "system", "component_name": "Geometry", "store_as": "geometry"},
                        {"op": "run_script", "target": "geometry", "script_path": reference, "store_as": "script_result"},
                        {"op": "get_attr", "target": "system", "attribute": "Name", "store_as": "system_name"},
                    ]}},
                    {"id": "readback", "type": "file", "action": "write_text", "path": "name.txt", "content": "${steps.wb.outputs.values.system_name}"},
                ]
                (self.root / "generated.py").unlink(missing_ok=True)
                with patch("ansys_mcp.workflow.run_process", side_effect=self.fake_process) as process:
                    self.assertTrue(self.validate(steps)["ok"])
                    process.assert_not_called()
                    result = run_workflow(workflow=self.workflow(steps))
                self.assertTrue(result["ok"], result)
                self.assertEqual((self.root / "name.txt").read_text(), "verified-system")
                self.assertEqual(result["steps"][1]["outputs"]["values"]["script_result"], {"script_text": "# generated"})

    def test_generated_recipe_file_is_checked_when_available(self):
        recipe = {"actions": [{"op": "get_system", "name": "SYS", "store_as": "system"}]}
        steps = [
            {"id": "write", "type": "file", "action": "write_text", "path": "recipe.json", "content": json.dumps(recipe)},
            {"id": "wb", "type": "workbench", "recipe_path": "${steps.write.outputs.path}"},
        ]
        with patch("ansys_mcp.workflow.run_process", side_effect=self.fake_process):
            result = run_workflow(workflow=self.workflow(steps))
        self.assertTrue(result["ok"], result)
        steps[0]["content"] = '{"actions": [{"op": "misspelled"}]}'
        with patch("ansys_mcp.workflow.run_process") as process:
            result = run_workflow(workflow=self.workflow(steps))
        self.assertFalse(result["ok"])
        self.assertEqual(result["steps"][-1]["id"], "wb")
        self.assertEqual(result["steps"][-1]["diagnostics"][0]["code"], "WORKBENCH_RECIPE_ERROR")
        process.assert_not_called()

    def test_external_recipe_can_reference_an_earlier_step(self):
        recipe_path = self.root / "external.json"
        recipe_path.write_text(json.dumps({"actions": [
            {"op": "get_system", "name": "${steps.source.outputs.text}", "store_as": "system"}
        ]}), encoding="utf-8")
        (self.root / "system.txt").write_text("SYS", encoding="utf-8")
        steps = [
            {"id": "source", "type": "file", "action": "read_text", "path": "system.txt"},
            {"id": "wb", "type": "workbench", "recipe_path": str(recipe_path)},
        ]
        with patch("ansys_mcp.workflow.run_process", side_effect=self.fake_process):
            result = run_workflow(workflow=self.workflow(steps))
        self.assertTrue(result["ok"], result)
        journal = Path(result["steps"][1]["outputs"]["journal_path"]).read_text()
        self.assertIn('GetSystem(Name=u"SYS")', journal)

    def test_cfx_generated_session_and_declared_output(self):
        steps = [
            {"id": "write", "type": "file", "action": "write_text", "path": "setup.pre", "content": "# session"},
            {"id": "pre", "type": "command", "command_id": "cfx_pre", "args": ["-batch", "setup.pre"], "produces": [{"path": "model.def"}]},
            {"id": "solve", "type": "command", "command_id": "cfx_solve", "args": ["-def", "model.def"]},
        ]
        calls = []

        def fake(argv, **kwargs):
            calls.append(argv)
            if "-batch" in argv:
                self.assertEqual(Path(argv[-1]).read_text(), "# session")
                (self.root / "model.def").write_text("fake solver input")
            else:
                self.assertTrue(Path(argv[-1]).is_file())
            return ProcessResult(0, 1, False, False, self.root / "out", self.root / "err", "", "", 0)

        with patch("ansys_mcp.workflow.run_process", side_effect=fake):
            result = run_workflow(workflow=self.workflow(steps))
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(calls), 2)

    def test_missing_declared_artifact_fails_before_downstream_launch(self):
        steps = [
            {"id": "pre", "type": "command", "command_id": "cfx_pre", "args": ["-help"], "produces": [{"path": "missing.def"}]},
            {"id": "solve", "type": "command", "command_id": "cfx_solve", "args": ["-def", "missing.def"]},
        ]
        fake = ProcessResult(0, 1, False, False, self.root / "out", self.root / "err", "", "", 0)
        with patch("ansys_mcp.workflow.run_process", return_value=fake) as process:
            result = run_workflow(workflow=self.workflow(steps))
        self.assertFalse(result["ok"])
        self.assertEqual(result["steps"][0]["status"], "failed")
        self.assertEqual(result["steps"][0]["diagnostics"][0]["code"], "ARTIFACT_MISSING")
        self.assertEqual(process.call_count, 1)

    def test_forward_reference_rejected_before_any_mutation(self):
        steps = [
            {"id": "write", "type": "file", "action": "write_text", "path": "must-not-exist.txt", "content": "no"},
            {"id": "pre", "type": "command", "command_id": "cfx_pre", "args": ["-batch", "${steps.missing.outputs.path}"]},
        ]
        result = run_workflow(workflow=self.workflow(steps))
        self.assertFalse(result["ok"])
        self.assertFalse((self.root / "must-not-exist.txt").exists())

    def test_generated_path_is_rechecked_after_reference_resolution(self):
        (self.root / "path.txt").write_text(str(self.root.parent / "outside.py"))
        steps = [
            {"id": "source", "type": "file", "action": "read_text", "path": "path.txt"},
            {"id": "wb", "type": "workbench", "recipe": {"actions": [
                {"op": "get_system", "name": "SYS", "store_as": "system"},
                {"op": "run_script", "target": "system", "script_path": "${steps.source.outputs.text}"},
            ]}},
        ]
        with patch("ansys_mcp.workflow.run_process") as process:
            result = run_workflow(workflow=self.workflow(steps))
        self.assertFalse(result["ok"])
        self.assertIn("outside", result["diagnostics"][-1]["message"])
        process.assert_not_called()

    def test_compiled_journal_returns_values_without_overwriting_helpers(self):
        recipe = WorkbenchRecipe.model_validate({"actions": [
            {"op": "get_system", "name": "SYS", "store_as": "json"},
            {"op": "get_attr", "target": "json", "attribute": "Name", "store_as": "class"},
        ]})
        prepared = compile_workbench_recipe(recipe, workdir=self.root, run_dir=self.root, step_id="values")
        self.fake_process(["fake", "-R", str(prepared.journal_path)])
        result = read_workbench_result(prepared.result_path)
        self.assertIsNone(validate_workbench_result(result, recipe))
        self.assertEqual(result["outputs"], {"class": "verified-system"})
        self.assertFalse(result["actions"][0]["value_available"])

    def test_incomplete_and_malformed_workbench_results_are_not_success(self):
        recipe = WorkbenchRecipe.model_validate({"actions": [{"op": "get_system", "name": "SYS", "store_as": "system"}]})
        for payload in [
            None, {"ok": True, "actions": []},
            {"ok": True, "completed": True, "outputs": {}, "actions": []},
            {"ok": True, "completed": True, "outputs": {}, "actions": [None]},
            {"ok": True, "completed": True, "outputs": {}, "actions": [{"id": "wrong", "op": "get_system", "ok": True}]},
            {"ok": True, "completed": True, "outputs": {}, "actions": [{"id": "action_1", "op": "get_system", "ok": 1}]},
        ]:
            with self.subTest(payload=payload):
                self.assertIsNotNone(validate_workbench_result(payload, recipe))

    def test_action_failure_retains_action_identity_and_error(self):
        recipe = WorkbenchRecipe.model_validate({"actions": [{"id": "geometry", "op": "get_system", "name": "SYS", "store_as": "system"}]})
        prepared = compile_workbench_recipe(recipe, workdir=self.root, run_dir=self.root, step_id="failure")

        def fail(**kwargs):
            raise RuntimeError("missing geometry system")

        with self.assertRaises(RuntimeError):
            exec(prepared.journal_path.read_text(), {"SetScriptVersion": lambda **kw: None, "GetSystem": fail})
        result = read_workbench_result(prepared.result_path)
        self.assertFalse(result["completed"])
        self.assertIn("Action geometry (get_system): missing geometry system", validate_workbench_result(result, recipe))


class SchemaDiscoveryTests(unittest.TestCase):
    def test_client_can_read_schemas_without_new_tools(self):
        from ansys_mcp.server import mcp, inspect_runtime

        self.assertEqual(len(asyncio.run(mcp.list_tools())), 12)
        resources = asyncio.run(mcp.list_resources())
        self.assertIn("ansys://schema/workflow", {str(resource.uri) for resource in resources})
        result = inspect_runtime(include_schema=True)
        schema = result["schemas"]["workflow"]
        self.assertIn("produces", schema["$defs"]["WorkbenchStep"]["properties"])
        self.assertEqual(schema["$defs"]["WorkbenchRecipe"]["additionalProperties"], False)
        self.assertIn("discriminator", schema["properties"]["steps"]["items"])


class ModelingExampleTests(unittest.TestCase):
    def setUp(self):
        scratch = PROJECT / ".ansys_mcp"
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch, prefix="example-tests-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / ".ansys_mcp" / "spaceclaim-example"
        self.output.mkdir(parents=True)
        env = patch.dict(os.environ, {"ANSYS_MCP_WORKDIR": str(self.root)})
        env.start()
        self.addCleanup(env.stop)

    def run_geometry_script(self, *, dimensions=(100, 20, 10), volume_scale=1, existing_body=False):
        (self.output / "parameters.json").write_text(json.dumps(dict(zip(
            ("length_mm", "width_mm", "height_mm"), dimensions
        ))), encoding="utf-8")
        model = SimpleNamespace(Bodies=[object()] if existing_body else [], Components=[])
        groups = {}

        def create_block(start, end, mode):
            body = SimpleNamespace(Shape=SimpleNamespace(Volume=end.X * end.Y * end.Z * volume_scale))
            body.SetName = lambda name: setattr(body, "Name", name)
            body.faces = [SimpleNamespace(EvalMid=lambda x=x: SimpleNamespace(Point=SimpleNamespace(X=x)))
                          for x in (0, end.X, end.X / 2, end.X / 2, end.X / 2, end.X / 2)]
            model.Bodies.append(body)
            return SimpleNamespace(CreatedBodies=[body])

        def select(objects):
            return SimpleNamespace(
                CreateAGroup=lambda name: groups.update({name: objects}),
                ConvertToFaces=lambda: SimpleNamespace(Items=objects.faces),
            )

        api = {
            "GetRootPart": lambda: model,
            "MM": lambda value: value / 1000.0,
            "Point": SimpleNamespace(Create=lambda x, y, z: SimpleNamespace(X=x, Y=y, Z=z)),
            "BlockBody": SimpleNamespace(Create=create_block),
            "ExtrudeType": SimpleNamespace(ForceIndependent="independent"),
            "Selection": SimpleNamespace(Create=select),
            "DocumentSave": SimpleNamespace(Execute=lambda path: Path(path).write_text("fake exported model")),
        }
        source = (PROJECT / "examples" / "scripts" / "spaceclaim_geometry.py").read_text(encoding="utf-8")
        exec(compile(source, "spaceclaim_geometry.py", "exec"), api)
        return model, groups

    def test_geometry_script_units_boundaries_and_exports(self):
        model, groups = self.run_geometry_script()
        result = json.loads((self.output / "measurements.json").read_text())
        self.assertAlmostEqual(result["volume_m3"], 0.00002)
        self.assertEqual(result["boundary_face_counts"], {"inlet": 1, "outlet": 1, "wall": 4})
        self.assertEqual(set(groups), {"fluid", "inlet", "outlet", "wall"})
        self.assertEqual(len(model.Bodies), 1)
        self.assertTrue((self.output / "channel.scdoc").is_file())
        self.assertTrue((self.output / "channel.step").is_file())

    def test_geometry_script_rejects_wrong_volume_without_success_artifacts(self):
        with self.assertRaisesRegex(RuntimeError, "volume"):
            self.run_geometry_script(volume_scale=2)
        self.assertFalse((self.output / "measurements.json").exists())
        self.assertFalse((self.output / "channel.scdoc").exists())

    def test_geometry_script_preserves_existing_models_and_files(self):
        with self.assertRaisesRegex(ValueError, "empty document"):
            self.run_geometry_script(existing_body=True)
        native = self.output / "channel.scdoc"
        native.write_text("keep")
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.run_geometry_script()
        self.assertEqual(native.read_text(), "keep")

    def test_geometry_script_rejects_invalid_dimensions(self):
        for dimensions in [(-1, 20, 10), (100, 0, 10), (float("inf"), 20, 10)]:
            with self.subTest(dimensions=dimensions), self.assertRaises(ValueError):
                self.run_geometry_script(dimensions=dimensions)

    def test_all_workflow_examples_validate_without_installation_or_launch(self):
        with patch.dict(os.environ, {"ANSYS_MCP_ALLOWED_ROOTS": str(PROJECT)}), patch("ansys_mcp.workflow.run_process") as process:
            for path in (PROJECT / "examples").glob("*.example.json"):
                if path.name == "fluent_recipe.example.json":
                    continue  # This file is a legacy recipe, not a workflow.
                with self.subTest(example=path.name):
                    result = validate_workflow(workflow_path=str(path), validation_mode="schema")
                    self.assertTrue(result["ok"], result)
            process.assert_not_called()


if __name__ == "__main__":
    unittest.main()
