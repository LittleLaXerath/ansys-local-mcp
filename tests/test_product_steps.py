from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ansys_mcp.server import mcp
from ansys_mcp.workflow import run_workflow, validate_workflow


class ProductStepIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.environment = patch.dict(os.environ, {"ANSYS_MCP_ALLOWED_ROOTS": str(self.root)})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def workflow(self, step):
        return {"version": "1", "workdir": str(self.root), "steps": [step]}

    def test_mechanical_schema_validation_does_not_launch(self):
        with patch("ansys_mcp.mechanical.run_mechanical_recipe") as runner:
            result = validate_workflow(self.workflow({"type": "mechanical", "recipe": {"operations": [{"op": "inspect"}]}}), validation_mode="schema")
        self.assertTrue(result["ok"], result)
        runner.assert_not_called()

    def test_mechanical_calls_domain_and_returns_typed_output(self):
        packet = {"ok": True, "status": "complete", "outputs": {"values": {"size": {"$type": "quantity", "value": 2, "unit": "mm"}}}, "diagnostics": [], "artifacts": []}
        with patch("ansys_mcp.mechanical.run_mechanical_recipe", return_value=packet) as runner:
            result = run_workflow(self.workflow({"id": "model", "type": "mechanical", "recipe": {"operations": [{"op": "inspect"}]}}))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["steps"][0]["outputs"]["values"]["size"]["unit"], "mm")
        runner.assert_called_once()

    def test_mechanical_mutation_cannot_be_automatically_replayed(self):
        result = validate_workflow(self.workflow({"type": "mechanical", "retry": {"max_attempts": 2}, "recipe": {}}), validation_mode="schema")
        self.assertFalse(result["ok"])
        self.assertIn("replayed", result["diagnostics"][0]["message"])

    def test_dpf_schema_accepts_future_declared_input(self):
        workflow = {"version": "1", "workdir": str(self.root), "steps": [
            {"id": "input", "type": "file", "action": "write_text", "path": "model.rst", "content": "synthetic"},
            {"id": "post", "type": "dpf", "recipe": {"result_files": [{"path": "model.rst"}], "operations": [{"id": "info", "op": "metadata"}]}}
        ]}
        with patch("ansys_mcp.dpf.run_dpf_recipe", return_value={"ok": True, "status": "complete", "outputs": {"metadata": {}}, "diagnostics": [], "artifacts": []}) as runner:
            result = run_workflow(workflow)
        self.assertTrue(result["ok"], result)
        runner.assert_called_once()

    def test_new_tools_and_schemas_are_exposed(self):
        names = {item.name for item in asyncio.run(mcp.list_tools())}
        self.assertTrue({"start_workflow", "get_workflow_status", "cancel_workflow", "query_capabilities", "get_mechanical_request_status"} <= names)
        resources = {str(item.uri) for item in asyncio.run(mcp.list_resources())}
        self.assertTrue({"ansys://schema/mechanical-recipe", "ansys://schema/dpf-recipe"} <= resources)


if __name__ == "__main__":
    unittest.main()
