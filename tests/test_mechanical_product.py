"""Opt-in empty-project product tests, separate from synthetic contract tests.

Run explicitly with ANSYS_MCP_PRODUCT_TESTS=1 and a writable allowed-root path
in ANSYS_MCP_PRODUCT_WORKDIR. No existing project is opened by this test.
"""
import os
import unittest
import uuid
from pathlib import Path

from ansys_mcp.mechanical import run_mechanical_recipe


@unittest.skipUnless(os.environ.get("ANSYS_MCP_PRODUCT_TESTS") == "1", "Product launch is explicitly opt-in.")
class MechanicalEmptyProjectProductTests(unittest.TestCase):
    def test_empty_project_save_reopen_readback(self):
        root_text = os.environ.get("ANSYS_MCP_PRODUCT_WORKDIR")
        self.assertTrue(root_text, "Set ANSYS_MCP_PRODUCT_WORKDIR to an explicit writable allowed root.")
        root = Path(root_text).resolve()
        directory = root / ".ansys_mcp" / "product-tests" / uuid.uuid4().hex
        directory.mkdir(parents=True, exist_ok=False)
        backend = os.environ.get("ANSYS_MCP_PRODUCT_BACKEND", "workbench")
        extension = ".wbpj" if backend == "workbench" else ".mechdb"
        result = run_mechanical_recipe({
            "backend": backend, "mode": "batch", "launch": {"elevation": "auto"},
            "operations": [{"op": "inspect", "target": "Model", "limit": 10},
                           {"op": "project_save", "path": str(directory / ("empty" + extension))},
                           {"op": "project_reopen"}, {"op": "get", "target": "Model", "member": "Name"}],
        }, root, directory, 180)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["outputs"]["session"]["closed"])
        self.assertEqual(result["outputs"]["session"]["generation"], 1)
        self.assertEqual(result["outputs"]["operations"][-1]["value"], "Model")
        self.assertTrue((directory / ("empty" + extension)).is_file())
