from __future__ import annotations

import os
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT = Path(__file__).resolve().parents[1]
os.environ["ANSYS_MCP_ALLOWED_ROOTS"] = str(PROJECT)
os.chdir(PROJECT)

from ansys_mcp.config import STARTUP_CWD, allowed_roots_info, discover_installation
from ansys_mcp.workflow import inspect_runtime, validate_workflow


class DiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = PROJECT / ".ansys_mcp" / "discovery-tests"
        self.scratch.mkdir(parents=True, exist_ok=True)
        self.fake_root = self.scratch / "v999"
        launcher = self.fake_root / "Framework" / "bin" / "Win64" / "RunWB2.exe"
        launcher.parent.mkdir(parents=True, exist_ok=True)
        launcher.write_bytes(b"")

    def tearDown(self) -> None:
        shutil.rmtree(self.scratch, ignore_errors=True)

    def test_explicit_root_has_highest_priority(self) -> None:
        resolution = discover_installation(
            {
                "ANSYS_MCP_ROOT": str(self.fake_root),
                "ANSYS_MCP_VERSION_TAG": "999",
                "AWP_ROOT261": r"C:\missing",
            }
        )
        self.assertEqual(resolution.root, self.fake_root.resolve())
        self.assertEqual(resolution.version_tag, "999")
        self.assertEqual(resolution.source, "ANSYS_MCP_ROOT")

    def test_invalid_explicit_root_does_not_fallback(self) -> None:
        resolution = discover_installation(
            {
                "ANSYS_MCP_ROOT": str(self.scratch / "missing"),
                "AWP_ROOT999": str(self.fake_root),
                "ANSYS_MCP_VERSION_TAG": "999",
            }
        )
        self.assertIsNone(resolution.root)
        self.assertEqual(resolution.source, "ANSYS_MCP_ROOT_INVALID")

    def test_exact_awp_root_is_discovered(self) -> None:
        resolution = discover_installation(
            {"ANSYS_MCP_VERSION_TAG": "999", "AWP_ROOT999": str(self.fake_root)}
        )
        self.assertEqual(resolution.root, self.fake_root.resolve())
        self.assertEqual(resolution.source, "AWP_ROOT999")

    def test_current_machine_awp_root_is_reported_when_available(self) -> None:
        if not any(name.upper().startswith("AWP_ROOT") for name in os.environ):
            self.skipTest("No AWP_ROOT environment variable on this machine")
        runtime = inspect_runtime()
        self.assertIsNotNone(runtime["resolved_root"])
        self.assertTrue(runtime["discovery_source"].upper().startswith("AWP_ROOT"))


class AllowedRootsTests(unittest.TestCase):
    def test_unset_defaults_to_startup_directory_only(self) -> None:
        info = allowed_roots_info({})
        self.assertEqual(info.roots, (STARTUP_CWD,))
        self.assertEqual(info.source, "startup_cwd")
        self.assertTrue(info.warnings)

    def test_any_invalid_configured_root_fails_closed(self) -> None:
        configured = str(PROJECT) + ";" + str(PROJECT / "does-not-exist")
        with patch.dict(os.environ, {"ANSYS_MCP_ALLOWED_ROOTS": configured}, clear=False):
            result = validate_workflow(
                workflow={
                    "version": "1",
                    "workdir": str(PROJECT),
                    "steps": [{"type": "assert", "path": str(PROJECT)}],
                },
                validation_mode="schema",
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["diagnostics"][0]["code"], "CONFIG_ERROR")

    def test_signature_report_is_never_called_by_validation(self) -> None:
        with patch("ansys_mcp.runtime.signature_info") as signature_mock:
            result = validate_workflow(
                workflow={
                    "version": "1",
                    "workdir": str(PROJECT),
                    "steps": [{"type": "assert", "path": str(PROJECT)}],
                },
                validation_mode="runtime",
            )
        self.assertTrue(result["ok"], result)
        signature_mock.assert_not_called()

    def test_invalid_explicit_install_root_is_runtime_config_error(self) -> None:
        with patch.dict(
            os.environ,
            {"ANSYS_MCP_ROOT": str(PROJECT / "missing-install")},
            clear=False,
        ):
            result = validate_workflow(
                workflow={
                    "version": "1",
                    "workdir": str(PROJECT),
                    "steps": [{"id": "check", "type": "assert", "path": str(PROJECT)}],
                },
                validation_mode="runtime",
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["diagnostics"][0]["code"], "CONFIG_ERROR")


if __name__ == "__main__":
    unittest.main()
