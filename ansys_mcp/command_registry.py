from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .runtime import ansys_python_executable
from .config import known_paths
from .workflow_paths import PathChecks


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
    ".gtm",
    ".ccl",
    ".step",
    ".stp",
)
_BATCH_META_RE = re.compile(r"[&|<>^%!()\r\n\x00-\x1f]")
_CONTROL_RE = re.compile(r"[\r\n\x00-\x08\x0b\x0c\x0e-\x1f]")
_OPTION_ASSIGNMENT_RE = re.compile(r"^([/-][A-Za-z][A-Za-z0-9_-]*)=(.*)$")

# Only path-bearing options need product knowledge; ordinary switches remain argv.
_PATH_OPTIONS = {
    "fluent": {"-i": "input"},
    "cfx_pre": {"-cfx": "input", "-def": "input", "-batch": "input", "-session": "input", "-s": "input"},
    "cfx_solve": {
        "-def": "input", "-definition": "input", "-ccl": "input", "-ini": "input",
        "-initial": "input", "-initial-file": "input", "-continue-from-file": "input", "-res": "input",
    },
    "cfx_post": {"-res": "input", "-batch": "input", "-session": "input"},
    "cfd_post": {"-res": "input", "-batch": "input", "-session": "input"},
    "runwb2": {"-r": "input", "-f": "input"},
    "runwb2_batch": {"-r": "input", "-f": "input"},
    "mapdl": {"-i": "input", "-o": "output", "-dir": "directory"},
    "mechanical": {"-script": "input", "-file": "input"},
    "spaceclaim": {
        "/runscript": "input", "/scriptoutput": "output",
        "/defaultopendirectory": "directory", "/defaultsavedirectory": "directory",
        "/defaultusersettingsfile": "input",
    },
    "system_coupling": {"-r": "input"},
}


class CommandPolicyError(ValueError):
    pass


class WindowsBatchCommand(list[str]):
    """Argv for diagnostics, with cmd.exe's distinct quoting preserved for Popen.

    Passing a batch command as an ordinary argv list asks subprocess to apply
    MS C-runtime quoting. cmd.exe does not understand the resulting backslash
    escapes, so the exact /s /c command line must be passed as a string instead.
    """

    def __init__(self, executable: Path, arguments: list[str]):
        for value in arguments:
            if '"' in value or _BATCH_META_RE.search(value):
                raise CommandPolicyError("Batch arguments contain unsupported shell characters.")
        if '"' in str(executable) or re.search(r"[%!\r\n\x00]", str(executable)):
            raise CommandPolicyError("Batch executable contains unsupported shell characters.")
        # The executable is parsed by cmd; arguments are ultimately passed to
        # the batch file's child executable and retain Windows argv quoting.
        inner = '"' + str(executable) + '"'
        if arguments:
            inner += " " + subprocess.list2cmdline(arguments)
        # Keep the existing logical argv layout for diagnostics and callers
        # which inspect product flags. Only the supervisor uses the wire form.
        super().__init__(["cmd.exe", "/d", "/s", "/c", str(executable), *arguments])
        self.windows_commandline = 'cmd.exe /d /v:off /s /c "' + inner + '"'


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
            "path_options": _PATH_OPTIONS.get(command_id, {}),
        }
        for command_id, spec in command_specs().items()
    }


def validate_command_args(
    command_id: str,
    args: list[Any] | None,
    *,
    workdir: Path,
    validation_mode: Literal["runtime", "schema"] = "runtime",
    path_checks: PathChecks | None = None,
) -> list[str]:
    specs = command_specs()
    if command_id not in specs:
        raise CommandPolicyError(f"Unknown command_id: {command_id}")
    if not isinstance(args or [], list):
        raise CommandPolicyError("Command arguments must be a JSON list.")

    spec = specs[command_id]
    checks = path_checks or PathChecks(workdir, validation_mode)
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
        return _validate_python_argv(argv, checks)
    for index, prefix, value, role in _path_arguments(command_id, argv):
        resolved = checks.resolve(
            value, input_file=role == "input", expect_file=False if role == "directory" else True
        )
        if resolved is None:
            continue
        normalized = prefix + str(resolved)
        if spec.wrapper == "batch" and _BATCH_META_RE.search(normalized):
            raise CommandPolicyError(f"Batch command path contains unsupported metacharacters: {normalized!r}")
        argv[index] = normalized
    return argv


def command_output_paths(command_id: str, args: list[Any]) -> list[str]:
    return [value for _, _, value, role in _path_arguments(command_id, [str(arg) for arg in args]) if role == "output"]


def _path_arguments(command_id: str, argv: list[str]):
    options = _PATH_OPTIONS.get(command_id, {})
    pending: str | None = None
    for index, value in enumerate(argv):
        if pending:
            if value.startswith("-") and not re.match(r"^-\d", value):
                raise CommandPolicyError("Missing path argument before " + value)
            yield index, "", value.strip('"'), pending
            pending = None
            continue
        assignment = _OPTION_ASSIGNMENT_RE.fullmatch(value)
        if assignment:
            flag, argument = assignment.groups()
            if command_id == "spaceclaim" and flag.casefold() == "/scriptargs":
                continue
            role = options.get(flag.casefold())
            if role or _looks_like_path(argument):
                if not argument:
                    raise CommandPolicyError(f"Missing path for {flag}")
                yield index, flag + "=", argument.strip('"'), role or "input"
            continue
        if value.casefold() in options:
            pending = options[value.casefold()]
        elif command_id == "spaceclaim" and re.fullmatch(r"/[A-Za-z][A-Za-z0-9_-]*", value):
            continue
        elif _looks_like_path(value):
            role = "output" if Path(value).suffix.lower() in {".out", ".res"} else "input"
            yield index, "", value.strip('"'), role
    if pending:
        raise CommandPolicyError("Missing final path argument.")


def command_argv(command_id: str, args: list[Any] | None = None) -> list[str]:
    specs = command_specs()
    if command_id not in specs:
        raise KeyError(f"Unknown command_id: {command_id}")
    spec = specs[command_id]
    argv = [str(part) for part in (args or [])]
    if spec.wrapper == "batch":
        return WindowsBatchCommand(spec.executable, argv)
    return [str(spec.executable), *argv]


def _validate_python_argv(argv: list[str], checks: PathChecks) -> list[str]:
    if not argv:
        raise CommandPolicyError("ansys_python requires an allowed .py script path.")
    if argv[0] in {"-c", "-m", "-"} or any(value in {"-c", "-m"} for value in argv):
        raise CommandPolicyError("ansys_python only accepts script files; -c, -m, and stdin are disabled.")
    script = checks.resolve(argv[0], input_file=True, expect_file=True)
    if script is not None and script.suffix.lower() != ".py":
        raise CommandPolicyError(f"ansys_python script must use the .py extension: {script}")
    if script is not None:
        argv[0] = str(script)
    for index, value in enumerate(argv[1:], start=1):
        if _looks_like_path(value):
            resolved = checks.resolve(value)
            if resolved is not None:
                argv[index] = str(resolved)
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
