from __future__ import annotations

import asyncio
import json
import os
import shutil
import types
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
os.environ["ANSYS_MCP_ALLOWED_ROOTS"] = str(PROJECT)

import sys

sys.path.insert(0, str(PROJECT))
os.chdir(PROJECT)

from ansys_mcp.command_registry import command_argv, supported_command_ids
from ansys_mcp.fluent_recipe import legacy_pyfluent_workflow, legacy_python_script_workflow
from ansys_mcp.pyfluent_recipe_runner import _resolve_any, _run_operation
from ansys_mcp.workflow import inspect_runtime, run_workflow, validate_workflow


class WorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = PROJECT / ".ansys_mcp" / "tests"
        self.scratch.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.scratch, ignore_errors=True)

    def test_validate_workflow_success(self) -> None:
        result = validate_workflow(
            workflow={
                "version": "1",
                "workdir": str(PROJECT),
                "steps": [{"id": "check_project", "type": "assert", "path": str(PROJECT), "exists": True}],
            }
        )
        self.assertTrue(result["ok"], result)

    def test_validate_workflow_schema_failure(self) -> None:
        result = validate_workflow(workflow={"version": "1", "workdir": str(PROJECT)})
        self.assertFalse(result["ok"])
        self.assertEqual(result["diagnostics"][0]["code"], "VALIDATION_ERROR")

    def test_path_denied(self) -> None:
        result = validate_workflow(
            workflow={
                "version": "1",
                "workdir": str(PROJECT),
                "steps": [{"type": "assert", "path": r"C:\Windows\notepad.exe", "exists": True}],
            }
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["diagnostics"][0]["code"], "PATH_DENIED")

    def test_unknown_command_rejected(self) -> None:
        result = validate_workflow(
            workflow={
                "version": "1",
                "workdir": str(PROJECT),
                "steps": [{"type": "command", "command_id": "not_a_command", "args": []}],
            }
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["diagnostics"][0]["code"], "VALIDATION_ERROR")

    def test_shell_string_rejected(self) -> None:
        result = validate_workflow(
            workflow={
                "version": "1",
                "workdir": str(PROJECT),
                "steps": [{"type": "command", "command_id": "ansys_python", "args": ["-c", "print(1) && echo bad"]}],
            }
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["diagnostics"][0]["code"], "COMMAND_POLICY_ERROR")

    def test_validate_does_not_mutate(self) -> None:
        target = self.scratch / "validate_only.txt"
        result = validate_workflow(
            workflow={
                "version": "1",
                "workdir": str(PROJECT),
                "steps": [{"type": "file", "action": "write_text", "target": str(target), "content": "no write"}],
            }
        )
        self.assertTrue(result["ok"], result)
        self.assertFalse(target.exists())

    def test_run_workflow_writes_manifest(self) -> None:
        result = run_workflow(
            workflow={
                "version": "1",
                "workdir": str(PROJECT),
                "steps": [{"type": "assert", "path": str(PROJECT), "exists": True}],
            },
            timeout_seconds=30,
        )
        self.assertTrue(result["ok"], result)
        self.assertTrue((Path(result["run_dir"]) / "manifest.json").exists())

    def test_legacy_workflow_builders(self) -> None:
        py = legacy_python_script_workflow(str(PROJECT / "ansys_mcp_server.py"), ["--x"], str(PROJECT))
        self.assertEqual(py["steps"][0]["type"], "python_script")
        self.assertEqual(py["steps"][0]["arguments"], ["--x"])

        fluent = legacy_pyfluent_workflow(recipe={"launch": {"mode": "solver"}, "operations": []}, workdir=str(PROJECT))
        self.assertEqual(fluent["steps"][0]["type"], "pyfluent")
        self.assertEqual(fluent["steps"][0]["launch"]["mode"], "solver")

    def test_command_registry_argv(self) -> None:
        argv = command_argv("ansys_python", ["-V"])
        self.assertTrue(argv)
        with self.assertRaises(KeyError):
            command_argv("unknown")

    def test_suite_command_ids_registered(self) -> None:
        ids = set(supported_command_ids())
        self.assertTrue(
            {
                "fluent",
                "cfx_pre",
                "cfx_solve",
                "cfx_post",
                "cfd_post",
                "spaceclaim",
                "mechanical",
                "runwb2",
                "runwb2_batch",
            }.issubset(ids)
        )
        runtime = inspect_runtime()
        self.assertIn("spaceclaim", runtime["paths"])
        self.assertIn("cfx_pre", runtime["paths"])

    def test_example_workflow_validates(self) -> None:
        example = PROJECT / "examples" / "workflow.example.json"
        result = validate_workflow(workflow_path=str(example), validation_mode="schema")
        self.assertTrue(result["ok"], result)

    def test_command_examples_parse_without_ansys(self) -> None:
        for name in [
            "workbench_first_suite.example.json",
            "workbench_recipe.example.json",
            "direct_channels.example.json",
        ]:
            example = PROJECT / "examples" / name
            result = validate_workflow(workflow_path=str(example), validation_mode="schema")
            self.assertTrue(result["ok"], result)

    def test_fastmcp_tool_registration(self) -> None:
        from ansys_mcp.server import mcp

        tools = asyncio.run(mcp.list_tools())
        names = {tool.name for tool in tools}
        self.assertTrue(
            {
                "get_ansys_install_info",
                "check_ansys_security_surface",
                "launch_workbench",
                "run_workbench_journal",
                "run_mapdl_input",
                "run_fluent_journal",
                "run_system_coupling",
                "run_ansys_python_script",
                "run_pyfluent_recipe",
                "inspect_runtime",
                "validate_workflow",
                "run_workflow",
            }.issubset(names)
        )


class FakePyFluentTests(unittest.TestCase):
    def test_resolve_collection_index_and_fallback(self) -> None:
        leaf = FakeNode()
        solver = types.SimpleNamespace(
            settings=types.SimpleNamespace(
                setup=types.SimpleNamespace(
                    materials=types.SimpleNamespace(fluid={"air": types.SimpleNamespace(density=leaf)})
                )
            )
        )
        resolved = _resolve_any(
            solver,
            ["settings.setup.missing", "settings.setup.materials.fluid[air].density"],
        )
        self.assertIs(resolved, leaf)

        quoted = _resolve_any(solver, 'settings.setup.materials.fluid["air"].density')
        self.assertIs(quoted, leaf)

    def test_set_state_operation(self) -> None:
        leaf = FakeNode()
        solver = types.SimpleNamespace(
            settings=types.SimpleNamespace(
                setup=types.SimpleNamespace(
                    materials=types.SimpleNamespace(fluid={"air": types.SimpleNamespace(density=leaf)})
                )
            )
        )
        _run_operation(
            solver,
            {"op": "set_state", "path": "settings.setup.materials.fluid[air].density", "value": 1.23},
        )
        self.assertEqual(leaf.state, 1.23)

    def test_operation_error(self) -> None:
        with self.assertRaises(ValueError):
            _run_operation(types.SimpleNamespace(), {"op": "not_supported"})


class FakeNode:
    def __init__(self) -> None:
        self.state = None

    def set_state(self, value):
        self.state = value

    def get_state(self):
        return self.state


if __name__ == "__main__":
    unittest.main()
