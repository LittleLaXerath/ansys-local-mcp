"""Read-only declarations from installed public PyFluent metadata, not live APIs."""
from __future__ import annotations

import ast
import hashlib
from functools import lru_cache
from pathlib import Path
from typing import Any


def _declarations(tree: ast.AST, prefix: str = "") -> list[dict[str, Any]]:
    entries = []
    for node in getattr(tree, "body", []):
        if isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            name = prefix + node.name
            entries.append({"qualified_name": name, "declaration_kind": "class", "source_line": node.lineno,
                            "description": (ast.get_docstring(node) or "").split("\n")[0]})
            entries.extend(_declarations(node, name + "."))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
            entries.append({"qualified_name": prefix + node.name, "declaration_kind": "method", "source_line": node.lineno})
    return entries


@lru_cache(maxsize=12)
def _source_catalog(path: str, size: int, modified_ns: int) -> tuple[list[dict[str, Any]], str]:
    source = Path(path).read_bytes()
    return _declarations(ast.parse(source)), hashlib.sha256(source).hexdigest()


def query_cfd_catalog(product: str, version: str, component: str | None, status: str | None,
                      offset: int, limit: int) -> dict[str, Any]:
    from .config import known_paths
    records = []
    manifest = []
    if product == "cfx":
        records = [{"qualified_name": "cfx." + op, "component": op,
                    "declaration_kind": "recipe_operation"} for op in ("pre", "solve", "post")]
    else:
        generated = known_paths()["pyfluent_core"] / "ansys" / "fluent" / "core" / "generated"
        sources = [("settings", generated / "solver" / ("settings_" + version + ".py")),
                   ("tui", generated / "solver" / ("tui_" + version + ".py")),
                   ("meshing", generated / "meshing" / ("tui_" + version + ".py"))]
        for category, path in sources:
            if not path.is_file():
                manifest.append({"component": category, "available": False})
                continue
            stat = path.stat()
            entries, digest = _source_catalog(str(path), stat.st_size, stat.st_mtime_ns)
            manifest.append({"component": category, "available": True, "sha256": digest, "declarations": len(entries)})
            records.extend({**entry, "qualified_name": category + "." + entry["qualified_name"], "component": category} for entry in entries)
    for record in records:
        record.update(product=product, version=version, status="catalogued", validation_stage="catalogued",
                      runtime_reachable=None, active=None, writable=None,
                      executable_path=None)
    if component:
        records = [record for record in records if record["component"] == component]
    if status:
        records = [record for record in records if status == "catalogued"]
    return {"ok": bool(records) or product == "cfx" or any(item.get("available") for item in manifest),
            "product": product, "version": version, "total": len(records), "offset": offset, "limit": limit,
            "next_offset": offset + limit if offset + limit < len(records) else None,
            "items": records[offset:offset + limit], "manifest": manifest,
            "scope": "Offline declarations only, not runtime dotted paths or product verification. Use inspect_path on a managed session for live state."}
