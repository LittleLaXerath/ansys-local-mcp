from __future__ import annotations

from typing import Any


PRIMITIVE_OPERATIONS = (
    "read",
    "write",
    "set_state",
    "set_constant",
    "get_state",
    "call",
    "tui",
    "iterate",
    "calculate",
    "create",
    "compute_reports",
    "inspect_path",
)
COMPATIBILITY_ALIASES = ("mesh_check", "set_zone_type", "create_force_report")


def supported_operations(*, include_aliases: bool = True) -> list[str]:
    values = list(PRIMITIVE_OPERATIONS)
    if include_aliases:
        values.extend(COMPATIBILITY_ALIASES)
    return values


def normalize_operation(operation: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    op = operation.get("op")
    if op == "mesh_check":
        normalized = {key: value for key, value in operation.items() if key != "op"}
        normalized.update({"op": "tui", "path": "mesh.check"})
        return normalized, "PyFluent operation 'mesh_check' is deprecated; use tui path 'mesh.check'."
    if op == "set_zone_type":
        normalized = {
            "op": "call",
            "path": "settings.setup.boundary_conditions.set_zone_type",
            "kwargs": {
                "zone_list": operation.get("zone_list"),
                "new_type": operation.get("new_type"),
            },
        }
        _copy_common(operation, normalized)
        return normalized, "PyFluent operation 'set_zone_type' is deprecated; use call."
    if op == "create_force_report":
        state = {
            key: operation[key]
            for key in ("force_vector", "zones", "per_selection", "report_output_type")
            if key in operation
        }
        normalized = {
            "op": "create",
            "path": "settings.solution.report_definitions.force",
            "name": operation.get("name"),
            "state": state,
        }
        _copy_common(operation, normalized)
        return normalized, "PyFluent operation 'create_force_report' is deprecated; use create."
    return dict(operation), None


def _copy_common(source: dict[str, Any], target: dict[str, Any]) -> None:
    for key in ("store_as", "continue_on_error"):
        if key in source:
            target[key] = source[key]
