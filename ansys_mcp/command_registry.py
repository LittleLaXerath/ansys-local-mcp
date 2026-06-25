from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import known_paths
from .runtime import ansys_python_executable


@dataclass(frozen=True)
class CommandSpec:
    command_id: str
    executable: Path
    prefix_args: tuple[str, ...] = ()
    description: str = ""
    uses_cmd_wrapper: bool = False


def command_specs() -> dict[str, CommandSpec]:
    paths = known_paths()
    return {
        "ansys_python": CommandSpec(
            command_id="ansys_python",
            executable=ansys_python_executable(),
            description="Ansys bundled Python runtime, or current Python as fallback.",
        ),
        "fluent": CommandSpec(
            command_id="fluent",
            executable=paths["fluent"],
            description="Ansys Fluent executable.",
        ),
        "cfx_pre": CommandSpec(
            command_id="cfx_pre",
            executable=paths["cfx_pre"],
            description="CFX-Pre executable for CFX setup and batch pre-processing.",
        ),
        "cfx_solve": CommandSpec(
            command_id="cfx_solve",
            executable=paths["cfx_solve"],
            description="CFX solver executable.",
        ),
        "cfx_post": CommandSpec(
            command_id="cfx_post",
            executable=paths["cfx_post"],
            description="CFX-Post executable from the CFX product tree.",
        ),
        "cfd_post": CommandSpec(
            command_id="cfd_post",
            executable=paths["cfd_post"],
            description="CFD-Post executable.",
        ),
        "runwb2": CommandSpec(
            command_id="runwb2",
            executable=paths["runwb2"],
            description="Ansys Workbench GUI executable.",
        ),
        "runwb2_batch": CommandSpec(
            command_id="runwb2_batch",
            executable=paths["runwb2_bat"],
            prefix_args=("cmd.exe", "/d", "/c"),
            description="Ansys Workbench batch launcher.",
            uses_cmd_wrapper=True,
        ),
        "mapdl": CommandSpec(
            command_id="mapdl",
            executable=paths["ansys_mapdl"],
            description="MAPDL batch executable.",
        ),
        "mechanical": CommandSpec(
            command_id="mechanical",
            executable=paths["mechanical"],
            description="Ansys Mechanical GUI executable.",
        ),
        "spaceclaim": CommandSpec(
            command_id="spaceclaim",
            executable=paths["spaceclaim"],
            description="Ansys SpaceClaim executable.",
        ),
        "system_coupling": CommandSpec(
            command_id="system_coupling",
            executable=paths["system_coupling"],
            prefix_args=("cmd.exe", "/d", "/c"),
            description="System Coupling batch launcher.",
            uses_cmd_wrapper=True,
        ),
    }


def supported_command_ids() -> list[str]:
    return sorted(command_specs())


def inspect_commands() -> dict[str, dict[str, Any]]:
    return {
        command_id: {
            "executable": str(spec.executable),
            "exists": spec.executable.exists(),
            "description": spec.description,
            "uses_cmd_wrapper": spec.uses_cmd_wrapper,
        }
        for command_id, spec in command_specs().items()
    }


def command_argv(command_id: str, args: list[Any] | None = None) -> list[str]:
    specs = command_specs()
    if command_id not in specs:
        raise KeyError(f"Unknown command_id: {command_id}")

    spec = specs[command_id]
    argv = [str(part) for part in (args or [])]
    if spec.prefix_args:
        return [*spec.prefix_args, str(spec.executable), *argv]
    return [str(spec.executable), *argv]
