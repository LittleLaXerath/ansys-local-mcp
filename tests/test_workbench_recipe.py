from __future__ import annotations

import json
import os
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError


PROJECT = Path(__file__).resolve().parents[1]
os.environ["ANSYS_MCP_ALLOWED_ROOTS"] = str(PROJECT)
os.chdir(PROJECT)

from ansys_mcp.schemas import WorkbenchRecipe
from ansys_mcp.workbench_recipe import (
    WorkbenchRecipeError,
    compile_workbench_recipe,
    load_workbench_recipe,
    read_workbench_result,
    validate_workbench_recipe_paths,
)


class WorkbenchRecipeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = PROJECT / ".ansys_mcp" / "workbench-tests"
        self.run_dir = self.scratch / "run"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.project_file = self.scratch / "input.wbpj"
        self.project_file.write_text("placeholder", encoding="utf-8")
        self.fluent_script = self.scratch / "fluent.jou"
        self.fluent_script.write_text('/define/models/energy yes\n', encoding="utf-8")
        self.cfx_script = self.scratch / "setup.ccl"
        self.cfx_script.write_text("FLOW: Flow Analysis 1\nEND\n", encoding="utf-8")
        self.mechanical_script = self.scratch / "mechanical.py"
        self.mechanical_script.write_text("Model.Refresh()\n", encoding="utf-8")
        self.spaceclaim_script = self.scratch / "geometry-script.py"
        self.spaceclaim_script.write_text("# geometry script\n", encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.scratch, ignore_errors=True)

    def _suite_recipe(self) -> WorkbenchRecipe:
        return WorkbenchRecipe.model_validate(
            {
                "project": {
                    "open_path": str(self.project_file),
                    "save_path": str(self.scratch / "output.wbpj"),
                    "overwrite": True,
                },
                "actions": [
                    {"id": "fluent_system", "op": "get_system", "name": "FLUENT", "store_as": "fluent"},
                    {
                        "id": "fluent_setup",
                        "op": "get_container",
                        "target": "fluent",
                        "component_name": "Setup",
                        "store_as": "fluent_setup",
                    },
                    {
                        "id": "fluent_tui",
                        "op": "send_command",
                        "target": "fluent_setup",
                        "script_path": str(self.fluent_script),
                    },
                    {"id": "cfx_system", "op": "get_system", "name": "CFX", "store_as": "cfx"},
                    {
                        "id": "cfx_setup",
                        "op": "get_container",
                        "target": "cfx",
                        "component_name": "Setup",
                        "store_as": "cfx_setup",
                    },
                    {
                        "id": "cfx_ccl",
                        "op": "send_command",
                        "target": "cfx_setup",
                        "script_path": str(self.cfx_script),
                    },
                    {"id": "mech_system", "op": "get_system", "name": "MECH", "store_as": "mech"},
                    {
                        "id": "mech_model",
                        "op": "get_container",
                        "target": "mech",
                        "component_name": "Model",
                        "store_as": "model",
                    },
                    {
                        "id": "mech_python",
                        "op": "send_command",
                        "target": "model",
                        "language": "Python",
                        "script_path": str(self.mechanical_script),
                    },
                    {"id": "geo_system", "op": "get_system", "name": "GEO", "store_as": "geo"},
                    {
                        "id": "geo_container",
                        "op": "get_container",
                        "target": "geo",
                        "component_name": "Geometry",
                        "store_as": "geometry",
                    },
                    {
                        "id": "open_spaceclaim",
                        "op": "call",
                        "target": "geometry",
                        "method": "Edit",
                        "kwargs": {"IsSpaceClaimGeometry": True, "Interactive": False},
                    },
                    {
                        "id": "spaceclaim_script",
                        "op": "run_script",
                        "target": "geometry",
                        "script_path": str(self.spaceclaim_script),
                    },
                ],
            }
        )

    def test_compiler_covers_full_suite_without_product_specific_tools(self) -> None:
        prepared = compile_workbench_recipe(
            self._suite_recipe(), workdir=PROJECT, run_dir=self.run_dir, step_id="suite"
        )
        journal = prepared.journal_path.read_text(encoding="utf-8")
        self.assertIn("Open(FilePath=", journal)
        self.assertIn("GetSystem(Name=u\"FLUENT\")", journal)
        self.assertIn(".SendCommand(", journal)
        self.assertIn("Language=u\"Python\"", journal)
        self.assertIn("IsSpaceClaimGeometry=True", journal)
        self.assertIn(".RunScript(ScriptFile=", journal)
        self.assertIn("Save(FilePath=", journal)
        self.assertEqual(prepared.generated_scripts, ())

    def test_generic_reference_values_render_as_handles(self) -> None:
        recipe = WorkbenchRecipe.model_validate(
            {
                "actions": [
                    {"op": "get_template", "template_name": "Fluid Flow", "store_as": "template"},
                    {"op": "get_system", "name": "GEO", "store_as": "geo"},
                    {
                        "op": "create_system",
                        "template": "template",
                        "kwargs": {"RelativeTo": {"$ref": "geo"}, "Position": "Right"},
                        "store_as": "fluent",
                    },
                ]
            }
        )
        prepared = compile_workbench_recipe(recipe, workdir=PROJECT, run_dir=self.run_dir, step_id="refs")
        journal = prepared.journal_path.read_text(encoding="utf-8")
        self.assertIn('RelativeTo=_handles[u"geo"]', journal)

    def test_user_values_cannot_inject_internal_expressions(self) -> None:
        recipe = WorkbenchRecipe.model_validate(
            {
                "actions": [
                    {"op": "get_system", "name": "SYS", "store_as": "system"},
                    {
                        "op": "call",
                        "target": "system",
                        "method": "Update",
                        "kwargs": {"Value": {"$expression": "dangerous()"}},
                    },
                ]
            }
        )
        prepared = compile_workbench_recipe(recipe, workdir=PROJECT, run_dir=self.run_dir, step_id="literal")
        journal = prepared.journal_path.read_text(encoding="utf-8")
        self.assertIn('u"$expression": u"dangerous()"', journal)
        self.assertNotIn("Value=dangerous()", journal)

    def test_unknown_or_duplicate_handles_are_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            WorkbenchRecipe.model_validate(
                {"actions": [{"op": "call", "target": "missing", "method": "Refresh"}]}
            )
        with self.assertRaises(ValidationError):
            WorkbenchRecipe.model_validate(
                {
                    "actions": [
                        {"op": "get_system", "name": "A", "store_as": "sys"},
                        {"op": "get_system", "name": "B", "store_as": "sys"},
                    ]
                }
            )
        with self.assertRaises(ValidationError):
            WorkbenchRecipe.model_validate(
                {"actions": [{"op": "get_system", "name": "A", "store_as": "bad-handle"}]}
            )

    def test_inline_code_is_disabled_by_default_and_audited_when_enabled(self) -> None:
        recipe = WorkbenchRecipe.model_validate(
            {
                "actions": [
                    {"op": "get_system", "name": "MECH", "store_as": "mech"},
                    {
                        "op": "get_container",
                        "target": "mech",
                        "component_name": "Model",
                        "store_as": "model",
                    },
                    {
                        "id": "inline_python",
                        "op": "send_command",
                        "target": "model",
                        "language": "Python",
                        "code": "print('quoted \\\"value\\\"')",
                    },
                ]
            }
        )
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ANSYS_MCP_ALLOW_INLINE_CODE", None)
            with self.assertRaises(WorkbenchRecipeError):
                validate_workbench_recipe_paths(recipe, PROJECT)

        with patch.dict(os.environ, {"ANSYS_MCP_ALLOW_INLINE_CODE": "1"}, clear=False):
            prepared = compile_workbench_recipe(recipe, workdir=PROJECT, run_dir=self.run_dir, step_id="inline")
        self.assertEqual(len(prepared.generated_scripts), 1)
        generated = prepared.generated_scripts[0]
        self.assertIn("quoted", generated.read_text(encoding="utf-8"))
        self.assertIn(str(generated).replace("\\", "\\\\"), prepared.journal_path.read_text(encoding="utf-8"))

    def test_recipe_path_and_result_reader(self) -> None:
        recipe_path = self.scratch / "recipe.json"
        recipe_path.write_text(
            json.dumps({"actions": [{"op": "get_system", "name": "SYS", "store_as": "sys"}]}),
            encoding="utf-8",
        )
        recipe = load_workbench_recipe(recipe=None, recipe_path=str(recipe_path), workdir=PROJECT)
        self.assertEqual(recipe.actions[0].op, "get_system")

        result_path = self.scratch / "result.json"
        result_path.write_text('{"ok": true, "actions": []}', encoding="utf-8")
        self.assertTrue(read_workbench_result(result_path)["ok"])


if __name__ == "__main__":
    unittest.main()
