import json
import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.setup_local import client_configuration, setup


class SetupTests(unittest.TestCase):
    def test_module_entry_point_starts_from_a_different_working_directory(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from datetime import timedelta

        async def check(root):
            configuration = client_configuration(Path(sys.executable), root)
            entry = configuration["mcpServers"]["ansys-local"]
            params = StdioServerParameters(
                command=entry["command"],
                args=entry["args"],
                cwd=str(root),
                env={**os.environ, **entry["env"]},
            )
            with (root / "stderr.log").open("w") as log:
                async with stdio_client(params, errlog=log) as (read, write):
                    async with ClientSession(
                        read, write, read_timeout_seconds=timedelta(seconds=30)
                    ) as client:
                        await client.initialize()
                        result = await client.list_tools()
                        self.assertEqual(len(result.tools), 17)

        with tempfile.TemporaryDirectory() as temp:
            asyncio.run(check(Path(temp).resolve()))

    def test_generated_config_has_absolute_interpreter_and_scoped_storage(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            result = client_configuration(
                root / "env/Scripts/python.exe", root / "work"
            )
            server = result["mcpServers"]["ansys-local"]
            self.assertEqual(server["args"], ["-m", "ansys_mcp"])
            self.assertEqual(
                server["env"]["ANSYS_MCP_ALLOWED_ROOTS"], str(root / "work")
            )
            self.assertNotIn("ANSYS_MCP_ROOT", server["env"])

    def test_setup_creates_only_local_configuration_and_never_launches_products(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "pyproject.toml").write_text("[project]\n")
            with (
                patch("tools.setup_local.venv.EnvBuilder") as builder,
                patch("tools.setup_local.subprocess.run") as run,
            ):
                result = setup(root, root / "env", root / "work", root / "client.json")
                builder.return_value.create.assert_called_once()
                run.assert_not_called()
                self.assertIn("pip", result["install_command"])
                config = json.loads(Path(result["config"]).read_text())
                self.assertEqual(
                    config["mcpServers"]["ansys-local"]["args"], ["-m", "ansys_mcp"]
                )

    def test_existing_environment_or_config_is_not_modified(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "pyproject.toml").write_text("[project]\n")
            (root / "env").mkdir()
            with patch("tools.setup_local.venv.EnvBuilder") as builder:
                with self.assertRaises(ValueError):
                    setup(root, root / "env", root / "work", root / "client.json")
                builder.assert_not_called()
            (root / "client.json").write_text("preserved")
            with self.assertRaises(ValueError):
                setup(root, root / "fresh", root / "work", root / "client.json")
            self.assertEqual((root / "client.json").read_text(), "preserved")

    def test_workspace_inside_environment_is_rejected_before_creation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "pyproject.toml").write_text("[project]\n")
            with patch("tools.setup_local.venv.EnvBuilder") as builder:
                with self.assertRaises(ValueError):
                    setup(root, root / "env", root / "env/work", root / "client.json")
                builder.assert_not_called()

    def test_explicit_existing_environment_only_generates_configuration(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "pyproject.toml").write_text("[project]\n")
            python = (
                root
                / "env"
                / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            )
            python.parent.mkdir(parents=True)
            python.write_text("synthetic interpreter fixture")
            with (
                patch("tools.setup_local.venv.EnvBuilder") as builder,
                patch("tools.setup_local.subprocess.run") as run,
            ):
                setup(
                    root,
                    root / "env",
                    root / "work",
                    root / "client.json",
                    use_existing=True,
                )
                builder.assert_not_called()
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
