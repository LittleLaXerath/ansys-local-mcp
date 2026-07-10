from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .config import require_allowed_path
from .runtime import ansys_python_executable
from .config import known_paths


PATH_SUFFIXES = (
    ".cas",
    ".cas.h5",
    ".dat",
    ".dat.h5",
    ".def",
    ".res",
    ".cfx",
    ".pre",
    ".cse",
    ".jou",
    ".msh",
    ".msh.gz",
    ".wbjn",
    ".wbpj",
    ".mechdb",
    ".scdoc",
    ".scdocx",
    ".scscript",
    ".py",
    ".txt",
    ".out",
    ".json",
)
_BATCH_META_RE = re.compile(r"[&|<>^%!()\r\n\x00-\x1f]")
_CONTROL_RE = re.compile(r"[\r\n\x00-\x08\x0b\x0c\x0e-\x1f]")


class CommandPolicyError(ValueError):
    pass


@dataclass(frozen=True)
class CommandSpec:
    command_id: str
    executable: Path
    description: str = ""
    wrapper: Literal["direct", "batch"] = "direct"
    supports_detach: bool = False
    python_script_only: bool = False

    @property
    def uses_cmd_wrapper(self) -> bool:
        return self.wrapper == "batch"


def command_specs() -> dict[str, CommandSpec]:
    paths = known_paths()
    return {
        "ansys_python": CommandSpec(
            "ansys_python",
            ansys_python_executable(),
            "Ansys bundled Python runtime (script files only).",
            python_script_only=True,
        ),
        "fluent": CommandSpec("fluent", paths["fluent"], "Ansys Fluent executable."),
        "cfx_pre": CommandSpec("cfx_pre", paths["cfx_pre"], "CFX-Pre batch executable."),
        "cfx_solve": CommandSpec("cfx_solve", paths["cfx_solve"], "CFX solver executable."),
        "cfx_post": CommandSpec("cfx_post", paths["cfx_post"], "CFX-Post executable."),
        "cfd_post": CommandSpec("cfd_post", paths["cfd_post"], "CFD-Post executable."),
        "runwb2": CommandSpec(
            "runwb2", paths["runwb2"], "Ansys Workbench executable.", supports_detach=True
        ),
        "runwb2_batch": CommandSpec(
            "runwb2_batch", paths["runwb2_bat"], "Ansys Workbench batch launcher.", wrapper="batch"
        ),
        "mapdl": CommandSpec("mapdl", paths["ansys_mapdl"], "MAPDL batch executable."),
        "mechanical": CommandSpec(
            "mechanical", paths["mechanical"], "Ansys Mechanical executable.", supports_detach=True
        ),
        "spaceclaim": CommandSpec(
            "spaceclaim", paths["spaceclaim"], "Ansys SpaceClaim executable.", supports_detach=True
        ),
        "system_coupling": CommandSpec(
            "system_coupling", paths["system_coupling"], "System Coupling batch launcher.", wrapper="batch"
        ),
    }


def supported_command_ids() -> list[str]:
    return sorted(command_specs())


def inspect_commands() -> dict[str, dict[str, Any]]:
    return {
        command_id: {
            "executable": str(spec.executable),
            "exists": spec.executable.is_file(),
            "description": spec.description,
            "wrapper": spec.wrapper,
            "uses_cmd_wrapper": spec.uses_cmd_wrapper,
            "supports_detach": spec.supports_detach,
            "python_script_only": spec.python_script_only,
        }
        for command_id, spec in command_specs().items()
    }


def validate_command_args(
    command_id: str,
    args: list[Any] | None,
    *,
    workdir: Path,
    validation_mode: Literal["runtime", "schema"] = "runtime",
) -> list[str]:
    specs = command_specs()
    if command_id not in specs:
        raise CommandPolicyError(f"Unknown command_id: {command_id}")
    if not isinstance(args or [], list):
        raise CommandPolicyError("Command arguments must be a JSON list.")

    spec = specs[command_id]
    argv: list[str] = []
    for raw in args or []:
        if not isinstance(raw, (str, int, float, bool)):
            raise CommandPolicyError(f"Unsupported argv value: {raw!r}")
        value = str(raw)
        if _CONTROL_RE.search(value):
            raise CommandPolicyError(f"Control characters are not allowed in argv: {value!r}")
        if spec.wrapper == "batch" and _BATCH_META_RE.search(value):
            raise CommandPolicyError(f"Batch command metacharacters are not allowed in argv: {value!r}")
        argv.append(value)

    if spec.python_script_only:
        return _validate_python_argv(argv, workdir, validation_mode)
    else:
        for index, value in enumerate(argv):
            if _looks_like_path(value):
                resolved = require_allowed_path(
                    value,
                    base=workdir,
                    must_exist=validation_mode == "runtime" and Path(value).suffix.lower() not in {".out", ".res"},
                )
                normalized = str(resolved)
                if spec.wrapper == "batch" and _BATCH_META_RE.search(normalized):
                    raise CommandPolicyError(
                        f"Batch command path contains unsupported metacharacters: {normalized!r}"
                    )
                argv[index] = normalized
    return argv


def command_argv(command_id: str, args: list[Any] | None = None) -> list[str]:
    specs = command_specs()
    if command_id not in specs:
        raise KeyError(f"Unknown command_id: {command_id}")
    spec = specs[command_id]
    argv = [str(part) for part in (args or [])]
    if spec.wrapper == "batch":
        return ["cmd.exe", "/d", "/s", "/c", str(spec.executable), *argv]
    return [str(spec.executable), *argv]


def _validate_python_argv(argv: list[str], workdir: Path, validation_mode: str) -> list[str]:
    if not argv:
        raise CommandPolicyError("ansys_python requires an allowed .py script path.")
    if argv[0] in {"-c", "-m", "-"} or any(value in {"-c", "-m"} for value in argv):
        raise CommandPolicyError("ansys_python only accepts script files; -c, -m, and stdin are disabled.")
    script = require_allowed_path(
        argv[0],
        base=workdir,
        must_exist=validation_mode == "runtime",
        expect_file=True,
    )
    if script.suffix.lower() != ".py":
        raise CommandPolicyError(f"ansys_python script must use the .py extension: {script}")
    argv[0] = str(script)
    for index, value in enumerate(argv[1:], start=1):
        if _looks_like_path(value):
            argv[index] = str(require_allowed_path(value, base=workdir, must_exist=False))
    return argv


def _looks_like_path(value: str) -> bool:
    text = value.strip().strip('"')
    if not text or text.startswith("-"):
        return False
    lower = text.lower()
    return (
        re.match(r"^[A-Za-z]:[\\/]", text) is not None
        or text.startswith(("./", ".\\", "../", "..\\"))
        or "/" in text
        or "\\" in text
        or lower.endswith(PATH_SUFFIXES)
    )
