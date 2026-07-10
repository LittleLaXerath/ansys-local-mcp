from __future__ import annotations

import json
import os
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT = Path(__file__).resolve().parents[1]
os.environ["ANSYS_MCP_ALLOWED_ROOTS"] = str(PROJECT)
os.chdir(PROJECT)

from ansys_mcp.workflow import run_workflow, validate_workflow


class StrictSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = PROJECT / ".ansys_mcp" / "schema-tests"
        self.scratch.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.scratch, ignore_errors=True)

    def _assert_invalid(self, workflow: dict) -> dict:
        result = validate_workflow(workflow=workflow, validation_mode="schema")
        self.assertFalse(result["ok"], result)
        return result

    def test_unknown_field_empty_steps_duplicate_and_invalid_ids(self) -> None:
        root = str(PROJECT)
        cases = [
            {
                "version": "1",
                "workdir": root,
                "continue_on_erorr": True,
                "steps": [{"type": "assert", "path": root}],
            },
            {"version": "1", "workdir": root, "steps": []},
            {
                "version": "1",
                "workdir": root,
                "steps": [
                    {"id": "same", "type": "assert", "path": root},
                    {"id": "same", "type": "assert", "path": root},
                ],
            },
            {
                "version": "1",
                "workdir": root,
                "steps": [{"id": "..\\escape", "type": "assert", "path": root}],
            },
        ]
        for workflow in cases:
            with self.subTest(workflow=workflow):
                result = self._assert_invalid(workflow)
                self.assertEqual(result["diagnostics"][0]["code"], "VALIDATION_ERROR")

    def test_positive_timeout_and_retry_bounds(self) -> None:
        workflow = {
            "version": "1",
            "workdir": str(PROJECT),
            "steps": [
                {
                    "id": "bad_retry",
                    "type": "assert",
                    "path": str(PROJECT),
                    "timeout_seconds": 0,
                    "retry": {"max_attempts": 6},
                }
            ],
        }
        self._assert_invalid(workflow)

    def test_variable_recursion_and_cycle_detection(self) -> None:
        valid = {
            "version": "1",
            "workdir": str(PROJECT),
            "variables": {"base": ".ansys_mcp", "target": "${base}/schema-tests"},
            "steps": [{"type": "assert", "path": "${target}", "exists": True}],
        }
        self.assertTrue(validate_workflow(workflow=valid, validation_mode="schema")["ok"])

        cyclic = {
            "version": "1",
            "workdir": str(PROJECT),
            "variables": {"a": "${b}", "b": "${a}"},
            "steps": [{"type": "assert", "path": "${a}"}],
        }
        result = self._assert_invalid(cyclic)
        self.assertIn("Cyclic", result["diagnostics"][0]["message"])

    def test_step_outputs_flow_into_later_steps(self) -> None:
        target = self.scratch / "step-output.txt"
        result = run_workflow(
            workflow={
                "version": "1",
                "workdir": str(PROJECT),
                "steps": [
                    {
                        "id": "write",
                        "type": "file",
                        "action": "write_text",
                        "target": str(target),
                        "content": "done",
                    },
                    {
                        "id": "check",
                        "type": "assert",
                        "path": "${steps.write.outputs.path}",
                        "exists": True,
                    },
                ],
            },
            timeout_seconds=20,
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["steps"][1]["outputs"]["path"], str(target))

    def test_unknown_step_output_is_rejected_before_mutation(self) -> None:
        target = self.scratch / "should-not-exist.txt"
        result = run_workflow(
            workflow={
                "version": "1",
                "workdir": str(PROJECT),
                "steps": [
                    {
                        "id": "write",
                        "type": "file",
                        "action": "write_text",
                        "target": "${steps.missing.outputs.path}",
                        "content": "no",
                    }
                ],
            }
        )
        self.assertFalse(result["ok"])
        self.assertFalse(target.exists())

    def test_required_collect_artifact_fails_step_and_run(self) -> None:
        result = run_workflow(
            workflow={
                "version": "1",
                "workdir": str(PROJECT),
                "steps": [
                    {
                        "id": "collect_missing",
                        "type": "collect",
                        "artifacts": [{"path": str(self.scratch / "missing.out"), "required": True}],
                    }
                ],
            }
        )
        self.assertFalse(result["ok"])
        self.assertFalse(result["steps"][0]["ok"])
        self.assertEqual(result["diagnostics"][-1]["code"], "ARTIFACT_MISSING")

    def test_top_level_artifact_can_reference_a_future_step_output(self) -> None:
        target = self.scratch / "future-artifact.txt"
        result = run_workflow(
            workflow={
                "version": "1",
                "workdir": str(PROJECT),
                "steps": [
                    {
                        "id": "write",
                        "type": "file",
                        "action": "write_text",
                        "target": str(target),
                        "content": "artifact",
                    }
                ],
                "artifacts": [
                    {"name": "future", "path": "${steps.write.outputs.path}", "required": True}
                ],
            }
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["artifacts"][0]["path"], str(target))

    def test_validate_never_invokes_process_supervisor(self) -> None:
        with patch("ansys_mcp.workflow.run_process") as run_process_mock:
            result = validate_workflow(
                workflow={
                    "version": "1",
                    "workdir": str(PROJECT),
                    "steps": [{"type": "assert", "path": str(PROJECT)}],
                },
                validation_mode="runtime",
            )
        self.assertTrue(result["ok"], result)
        run_process_mock.assert_not_called()

    def test_manifest_is_atomic_and_has_final_status(self) -> None:
        result = run_workflow(
            workflow={
                "version": "1",
                "workdir": str(PROJECT),
                "steps": [{"id": "check", "type": "assert", "path": str(PROJECT)}],
            }
        )
        manifest_path = Path(result["run_dir"]) / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "complete")
        self.assertFalse((manifest_path.parent / "manifest.json.tmp").exists())


class CommandPolicyTests(unittest.TestCase):
    def test_ansys_python_is_script_only(self) -> None:
        result = validate_workflow(
            workflow={
                "version": "1",
                "workdir": str(PROJECT),
                "steps": [
                    {"id": "python", "type": "command", "command_id": "ansys_python", "args": ["-c", "print(1)"]}
                ],
            },
            validation_mode="schema",
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["diagnostics"][0]["code"], "COMMAND_POLICY_ERROR")

    def test_command_path_outside_roots_is_rejected(self) -> None:
        result = validate_workflow(
            workflow={
                "version": "1",
                "workdir": str(PROJECT),
                "steps": [
                    {
                        "id": "outside",
                        "type": "command",
                        "command_id": "fluent",
                        "args": ["-i", "C:/Windows/win.ini"],
                    }
                ],
            },
            validation_mode="schema",
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["diagnostics"][0]["code"], "COMMAND_POLICY_ERROR")

    def test_batch_metacharacters_are_rejected(self) -> None:
        result = validate_workflow(
            workflow={
                "version": "1",
                "workdir": str(PROJECT),
                "steps": [
                    {
                        "id": "batch",
                        "type": "command",
                        "command_id": "runwb2_batch",
                        "args": ["-R", "journal.wbjn&calc.exe"],
                    }
                ],
            },
            validation_mode="schema",
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["diagnostics"][0]["code"], "COMMAND_POLICY_ERROR")

    def test_allowed_relative_command_paths_are_normalized(self) -> None:
        from ansys_mcp.command_registry import validate_command_args

        normalized = validate_command_args(
            "fluent",
            ["-i", "examples/workbench_noop.wbjn"],
            workdir=PROJECT,
            validation_mode="schema",
        )
        self.assertEqual(normalized[1], str((PROJECT / "examples" / "workbench_noop.wbjn").resolve()))


if __name__ == "__main__":
    unittest.main()
