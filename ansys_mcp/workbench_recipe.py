from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .config import (
    discover_installation,
    inline_code_allowed,
    require_allowed_file,
    require_allowed_path,
)
from .schemas import (
    CallAction,
    CreateSystemAction,
    GetAttrAction,
    GetComponentAction,
    GetContainerAction,
    GetSystemAction,
    GetTemplateAction,
    RunScriptAction,
    SendCommandAction,
    SetAttrAction,
    WorkbenchAction,
    WorkbenchRecipe,
)


_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class WorkbenchRecipeError(ValueError):
    def __init__(self, message: str, action_id: str | None = None):
        super().__init__(message)
        self.message = message
        self.action_id = action_id


@dataclass(frozen=True)
class _PythonExpression:
    value: str


@dataclass(frozen=True)
class PreparedWorkbenchRecipe:
    recipe: WorkbenchRecipe
    journal_path: Path
    result_path: Path
    generated_scripts: tuple[Path, ...]


def load_workbench_recipe(
    *,
    recipe: WorkbenchRecipe | dict[str, Any] | None,
    recipe_path: str | None,
    workdir: Path,
) -> WorkbenchRecipe:
    if recipe_path:
        path = require_allowed_file(recipe_path, base=workdir)
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkbenchRecipeError(f"Cannot read Workbench recipe {path}: {exc}") from exc
        try:
            return WorkbenchRecipe.model_validate(payload)
        except ValidationError as exc:
            raise WorkbenchRecipeError(f"Invalid Workbench recipe {path}: {exc}") from exc
    if isinstance(recipe, WorkbenchRecipe):
        return recipe
    if recipe is not None:
        try:
            return WorkbenchRecipe.model_validate(recipe)
        except ValidationError as exc:
            raise WorkbenchRecipeError(f"Invalid inline Workbench recipe: {exc}") from exc
    raise WorkbenchRecipeError("Workbench step requires recipe or recipe_path.")


def validate_workbench_recipe_paths(recipe: WorkbenchRecipe, workdir: Path) -> None:
    if recipe.project.open_path:
        require_allowed_file(recipe.project.open_path, base=workdir)
    if recipe.project.save_path:
        require_allowed_path(recipe.project.save_path, base=workdir, must_exist=False)
    for index, action in enumerate(recipe.actions):
        action_id = action.id or f"action_{index + 1}"
        script_path = getattr(action, "script_path", None)
        code = getattr(action, "code", None)
        if script_path:
            require_allowed_file(script_path, base=workdir)
        if code is not None and not inline_code_allowed():
            raise WorkbenchRecipeError(
                "Inline Workbench product code is disabled. Set ANSYS_MCP_ALLOW_INLINE_CODE=1 to enable it.",
                action_id,
            )


def compile_workbench_recipe(
    recipe: WorkbenchRecipe,
    *,
    workdir: Path,
    run_dir: Path,
    step_id: str,
) -> PreparedWorkbenchRecipe:
    validate_workbench_recipe_paths(recipe, workdir)
    result_path = run_dir / f"{step_id}.workbench-result.json"
    journal_path = run_dir / f"{step_id}.wbjn"
    generated_scripts: list[Path] = []

    lines = _journal_header(result_path, recipe.script_version or _default_script_version())
    if recipe.project.open_path:
        open_path = require_allowed_file(recipe.project.open_path, base=workdir)
        lines.extend(_wrapped_statement("project_open", "open_project", f"Open(FilePath={_literal(str(open_path))})", False))

    for index, action in enumerate(recipe.actions):
        action_id = action.id or f"action_{index + 1}"
        statement, script = _render_action(action, action_id, workdir, run_dir)
        if script:
            generated_scripts.append(script)
        should_continue = recipe.continue_on_error if action.continue_on_error is None else action.continue_on_error
        lines.extend(_wrapped_statement(action_id, action.op, statement, bool(should_continue)))

    if recipe.project.save_path:
        save_path = require_allowed_path(recipe.project.save_path, base=workdir, must_exist=False)
        statement = (
            f"Save(FilePath={_literal(str(save_path))}, "
            f"Overwrite={_literal(recipe.project.overwrite)})"
        )
        lines.extend(_wrapped_statement("project_save", "save_project", statement, False))

    lines.extend(["_status['ok'] = all(item['ok'] for item in _status['actions'])", "_write_result()", ""])
    journal_path.write_text("\n".join(lines), encoding="utf-8")
    return PreparedWorkbenchRecipe(recipe, journal_path, result_path, tuple(generated_scripts))


def read_workbench_result(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _journal_header(result_path: Path, script_version: str) -> list[str]:
    return [
        "# encoding: utf-8",
        f"SetScriptVersion(Version={_literal(script_version)})",
        "import json",
        "import traceback",
        f"_RESULT_PATH = {_literal(str(result_path))}",
        "_status = {'ok': True, 'actions': []}",
        "def _write_result():",
        "    _stream = open(_RESULT_PATH, 'w')",
        "    try:",
        "        json.dump(_status, _stream, indent=2)",
        "    finally:",
        "        _stream.close()",
        "def _record(_id, _op, _ok, _error=None):",
        "    _status['actions'].append({'id': _id, 'op': _op, 'ok': _ok, 'error': _error})",
        "    if not _ok:",
        "        _status['ok'] = False",
        "    _write_result()",
        "def _read_script(_path):",
        "    _stream = open(_path, 'rb')",
        "    try:",
        "        _data = _stream.read()",
        "    finally:",
        "        _stream.close()",
        "    try:",
        "        return _data.decode('utf-8-sig')",
        "    except Exception:",
        "        return _data",
        "_write_result()",
        "",
    ]


def _wrapped_statement(action_id: str, op: str, statement: str, should_continue: bool) -> list[str]:
    lines = ["try:"]
    lines.extend(f"    {line}" for line in statement.splitlines())
    lines.append(f"    _record({_literal(action_id)}, {_literal(op)}, True)")
    lines.extend(
        [
            "except Exception as _exc:",
            f"    _record({_literal(action_id)}, {_literal(op)}, False, str(_exc))",
        ]
    )
    if not should_continue:
        lines.append("    raise")
    lines.append("")
    return lines


def _render_action(
    action: WorkbenchAction,
    action_id: str,
    workdir: Path,
    run_dir: Path,
) -> tuple[str, Path | None]:
    if isinstance(action, GetTemplateAction):
        kwargs = {"TemplateName": action.template_name}
        if action.solver is not None:
            kwargs["Solver"] = action.solver
        return f"{action.store_as} = GetTemplate({_render_kwargs(kwargs)})", None
    if isinstance(action, CreateSystemAction):
        call = _render_call(action.template, "CreateSystem", action.args, action.kwargs)
        return f"{action.store_as} = {call}", None
    if isinstance(action, GetSystemAction):
        return f"{action.store_as} = GetSystem(Name={_literal(action.name)})", None
    if isinstance(action, GetComponentAction):
        return f"{action.store_as} = {action.target}.GetComponent(Name={_literal(action.name)})", None
    if isinstance(action, GetContainerAction):
        return (
            f"{action.store_as} = {action.target}.GetContainer(ComponentName={_literal(action.component_name)})",
            None,
        )
    if isinstance(action, CallAction):
        call = _render_call(action.target, action.method, action.args, action.kwargs)
        return (f"{action.store_as} = {call}" if action.store_as else call), None
    if isinstance(action, GetAttrAction):
        return f"{action.store_as} = getattr({action.target}, {_literal(action.attribute)})", None
    if isinstance(action, SetAttrAction):
        return f"setattr({action.target}, {_literal(action.attribute)}, {_literal(action.value)})", None
    if isinstance(action, (SendCommandAction, RunScriptAction)):
        script_path = _materialize_script(action, action_id, workdir, run_dir)
        if isinstance(action, SendCommandAction):
            kwargs: dict[str, Any] = {
                "Command": _PythonExpression(f"_read_script({_literal(str(script_path))})")
            }
            if action.language:
                kwargs["Language"] = action.language
            call = f"{action.target}.SendCommand({_render_kwargs(kwargs)})"
        else:
            call = f"{action.target}.RunScript(ScriptFile={_literal(str(script_path))})"
        store_as = getattr(action, "store_as", None)
        return (f"{store_as} = {call}" if store_as else call), script_path if action.code is not None else None
    raise WorkbenchRecipeError(f"Unsupported Workbench action: {action.op}", action_id)


def _materialize_script(
    action: SendCommandAction | RunScriptAction,
    action_id: str,
    workdir: Path,
    run_dir: Path,
) -> Path:
    if action.script_path:
        return require_allowed_file(action.script_path, base=workdir)
    if action.code is None:
        raise WorkbenchRecipeError("Missing Workbench script content.", action_id)
    if not inline_code_allowed():
        raise WorkbenchRecipeError("Inline Workbench product code is disabled.", action_id)
    if isinstance(action, RunScriptAction):
        suffix = action.suffix
    else:
        language = (action.language or "text").casefold()
        suffix = {"python": ".py", "javascript": ".js", "ccl": ".ccl"}.get(language, ".txt")
    path = run_dir / f"{action_id}{suffix}"
    path.write_text(action.code, encoding="utf-8")
    return path


def _render_call(target: str, method: str, args: list[Any], kwargs: dict[str, Any]) -> str:
    if not _IDENTIFIER_RE.fullmatch(method):
        raise WorkbenchRecipeError(f"Invalid Workbench method name: {method}")
    rendered = [_literal(value) for value in args]
    for key, value in kwargs.items():
        if not _IDENTIFIER_RE.fullmatch(key):
            raise WorkbenchRecipeError(f"Invalid Workbench keyword name: {key}")
        rendered.append(f"{key}={_literal(value)}")
    return f"{target}.{method}({', '.join(rendered)})"


def _render_kwargs(kwargs: dict[str, Any]) -> str:
    rendered: list[str] = []
    for key, value in kwargs.items():
        if not _IDENTIFIER_RE.fullmatch(key):
            raise WorkbenchRecipeError(f"Invalid Workbench keyword name: {key}")
        rendered.append(f"{key}={_literal(value)}")
    return ", ".join(rendered)


def _literal(value: Any) -> str:
    if isinstance(value, _PythonExpression):
        return value.value
    if isinstance(value, dict):
        if set(value) == {"$ref"}:
            reference = value["$ref"]
            if not isinstance(reference, str) or not _IDENTIFIER_RE.fullmatch(reference):
                raise WorkbenchRecipeError(f"Invalid Workbench reference: {reference!r}")
            return reference
        return "{" + ", ".join(f"{_literal(str(key))}: {_literal(child)}" for key, child in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(_literal(child) for child in value) + "]"
    if isinstance(value, tuple):
        return "(" + ", ".join(_literal(child) for child in value) + ")"
    if value is None:
        return "None"
    if value is True:
        return "True"
    if value is False:
        return "False"
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise WorkbenchRecipeError("Workbench recipe numbers must be finite.")
        return repr(value)
    if isinstance(value, str):
        return "u" + json.dumps(value, ensure_ascii=True)
    raise WorkbenchRecipeError(f"Unsupported Workbench value: {value!r}")


def _default_script_version() -> str:
    tag = discover_installation().version_tag
    return f"{tag[:-1]}.{tag[-1]}" if len(tag) >= 3 else tag
