from __future__ import annotations

import os
import types
import unittest
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
os.environ["ANSYS_MCP_ALLOWED_ROOTS"] = str(PROJECT)
os.chdir(PROJECT)

from ansys_mcp.pyfluent_operations import PRIMITIVE_OPERATIONS, normalize_operation
from ansys_mcp.pyfluent_recipe_runner import _path_tokens, _run_operation
from ansys_mcp.schemas import WORKFLOW_STEP_ADAPTER


class PyFluentRegistryTests(unittest.TestCase):
    def test_every_public_primitive_has_a_strict_schema(self) -> None:
        samples = {
            "read": {"op": "read", "case": "input.cas.h5"},
            "write": {"op": "write", "case_data": "output.cas.h5"},
            "set_state": {"op": "set_state", "path": "settings.a", "value": 1},
            "set_constant": {"op": "set_constant", "path": "settings.a", "value": 1},
            "get_state": {"op": "get_state", "path": "settings.a"},
            "call": {"op": "call", "path": "settings.a", "args": []},
            "tui": {"op": "tui", "path": "mesh.check"},
            "iterate": {"op": "iterate", "iterations": 1},
            "calculate": {"op": "calculate"},
            "create": {"op": "create", "path": "settings.reports.force", "name": "drag"},
            "compute_reports": {"op": "compute_reports", "names": ["drag"]},
            "inspect_path": {"op": "inspect_path", "path": "settings"},
        }
        self.assertEqual(set(samples), set(PRIMITIVE_OPERATIONS))
        for name, operation in samples.items():
            with self.subTest(operation=name):
                step = WORKFLOW_STEP_ADAPTER.validate_python(
                    {"id": "fluent", "type": "pyfluent", "operations": [operation]}
                )
                self.assertEqual(step.operations[0].op, name)

    def test_unknown_operation_fields_are_rejected(self) -> None:
        with self.assertRaises(Exception):
            WORKFLOW_STEP_ADAPTER.validate_python(
                {
                    "id": "fluent",
                    "type": "pyfluent",
                    "operations": [{"op": "iterate", "iterations": 1, "typo": True}],
                }
            )

        for operation in ({"op": "read"}, {"op": "write"}, {"op": "compute_reports"}):
            with self.subTest(operation=operation["op"]), self.assertRaises(Exception):
                WORKFLOW_STEP_ADAPTER.validate_python(
                    {"id": "fluent", "type": "pyfluent", "operations": [operation]}
                )

    def test_compatibility_aliases_normalize_to_primitives(self) -> None:
        mesh, mesh_warning = normalize_operation({"op": "mesh_check", "store_as": "mesh"})
        self.assertEqual(mesh["op"], "tui")
        self.assertEqual(mesh["path"], "mesh.check")
        self.assertTrue(mesh_warning)

        zone, _ = normalize_operation(
            {"op": "set_zone_type", "zone_list": ["inlet"], "new_type": "velocity-inlet"}
        )
        self.assertEqual(zone["op"], "call")
        self.assertEqual(zone["kwargs"]["zone_list"], ["inlet"])

        force, _ = normalize_operation(
            {"op": "create_force_report", "name": "drag", "zones": ["wall"]}
        )
        self.assertEqual(force["op"], "create")
        self.assertEqual(force["state"]["zones"], ["wall"])

    def test_alias_executor_uses_generic_dispatch(self) -> None:
        called = []

        class Check:
            def __call__(self):
                called.append("mesh")
                return "ok"

        solver = types.SimpleNamespace(tui=types.SimpleNamespace(mesh=types.SimpleNamespace(check=Check())))
        self.assertEqual(_run_operation(solver, {"op": "mesh_check"}), "ok")
        self.assertEqual(called, ["mesh"])

    def test_path_parser_supports_indexes_and_quoted_keys(self) -> None:
        self.assertEqual(
            _path_tokens('settings.materials["air.mix"].density[0]'),
            [
                ("attr", "settings"),
                ("attr", "materials"),
                ("index", "air.mix"),
                ("attr", "density"),
                ("index", 0),
            ],
        )


if __name__ == "__main__":
    unittest.main()
