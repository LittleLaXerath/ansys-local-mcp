"""First-run setup from a source checkout; never starts an Ansys product."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import venv
from pathlib import Path


def client_configuration(python: Path, workspace: Path) -> dict:
    return {
        "mcpServers": {
            "ansys-local": {
                "command": str(python),
                "args": ["-m", "ansys_mcp"],
                "env": {
                    "ANSYS_MCP_ALLOWED_ROOTS": str(workspace),
                    "ANSYS_MCP_EVIDENCE_ROOT": str(
                        workspace / ".ansys_mcp/capability_evidence"
                    ),
                },
            }
        }
    }


def setup(
    root: Path, environment: Path, workspace: Path, output: Path, *, use_existing=False
) -> dict:
    root, environment, workspace, output = [
        p.expanduser().resolve() for p in (root, environment, workspace, output)
    ]
    if not (root / "pyproject.toml").is_file():
        raise ValueError("Run setup from a source checkout.")
    if environment.exists() and not use_existing:
        raise ValueError(
            "Environment already exists. Choose a new --venv-path; an active installation will not be replaced."
        )
    if output.exists():
        raise ValueError(
            "Client configuration already exists. Choose a new --config-path."
        )
    if output.is_relative_to(environment) or workspace.is_relative_to(environment):
        raise ValueError(
            "Keep the workspace and client configuration outside the virtual environment."
        )
    if environment == workspace or environment == root or output == workspace:
        raise ValueError("Environment, source and workspace must be separate paths.")
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if use_existing:
        if not python.is_file():
            raise ValueError("The selected environment has no Python interpreter.")
    else:
        venv.EnvBuilder(with_pip=True).create(environment)
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / ".ansys_mcp/capability_evidence").mkdir(parents=True, exist_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    configuration = client_configuration(python, workspace)
    # Exclusive creation protects an existing user's client configuration.
    with output.open("x", encoding="utf-8") as stream:
        json.dump(configuration, stream, indent=2, ensure_ascii=True)
        stream.write("\n")
    return {
        "config": str(output),
        "workspace": str(workspace),
        "python": str(python),
        "install_command": [
            str(python),
            "-m",
            "pip",
            "install",
            "-e",
            str(root) + "[fluent]",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parents[1]
    parser.add_argument("--venv-path", type=Path, default=root / ".venv")
    parser.add_argument("--workspace", type=Path, default=root / "workspace")
    parser.add_argument(
        "--use-existing-env",
        action="store_true",
        help="Generate config only; never changes packages in the selected environment",
    )
    parser.add_argument(
        "--config-path", type=Path, default=root / "mcp_client_config.json"
    )
    args = parser.parse_args()
    if os.name != "nt" or sys.version_info < (3, 11):
        parser.error("Setup requires Windows and Python 3.11 or newer.")
    try:
        result = setup(
            root,
            args.venv_path,
            args.workspace,
            args.config_path,
            use_existing=args.use_existing_env,
        )
    except (OSError, ValueError) as exc:
        parser.exit(1, "Setup failed: " + str(exc) + "\n")
    print(json.dumps(result, indent=2))
    print(
        "Environment/configuration ready. Install dependencies with the command below before connecting your MCP client."
    )
    print("& " + subprocess.list2cmdline(result["install_command"]))


if __name__ == "__main__":
    main()
