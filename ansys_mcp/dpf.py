"""Local, isolated DPF reader/operator adapter with bounded result summaries.

Every recipe owns one server.  No existing global server is shut down and no
insecure gRPC option is supplied.  Native DPF work runs in a supervised worker so
the workflow deadline also covers a stuck reader or a native-library call.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.metadata
import json
import math
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from .config import allowed_roots, discover_installation, require_allowed_file, require_allowed_path
from .dpf_schema import DPFRecipe
from .process_supervisor import WorkflowCancelled, run_process
from .workflow_paths import PathChecks


DPF_FORMATS = (
    {"id": "mapdl", "formats": ["rst", "rth", "mode"], "reader_tokens": ["rst", "mapdl"]},
    {"id": "ls_dyna", "formats": ["d3plot", "binout"], "reader_tokens": ["lsdyna", "ls_dyna", "d3plot", "binout"]},
    {"id": "fluent", "formats": ["cas", "dat", "cas.h5", "dat.h5"], "reader_tokens": ["fluent"]},
    {"id": "cfx", "formats": ["res"], "reader_tokens": ["cfx"]},
    {"id": "autodyn", "formats": [], "reader_tokens": []},
)
FORMAT_SOURCE = "https://dpf.docs.pyansys.com/version/stable/introduction.html"
_SAFE_STRING_PINS = {"requested_location", "location", "unit", "unit_name", "named_selection_name", "named_selection"}


def dpf_runtime_info() -> dict[str, Any]:
    """Inspect the dependency/installation without loading or starting DPF."""
    try:
        version = importlib.metadata.version("ansys-dpf-core")
    except importlib.metadata.PackageNotFoundError:
        version = None
    installation = discover_installation()
    return {"dependency": "ansys-dpf-core", "dependency_version": version,
            "installed": version is not None, "installation_discovered": installation.root is not None,
            "product_version": installation.version_tag, "default_transport": "in_process",
            "grpc_authentication": "mtls", "readers_probed": False,
            "formats": format_capabilities()}


def format_capabilities(operator_names: list[str] | None = None) -> list[dict[str, Any]]:
    """Documented formats and actual reader candidates are separate evidence.

    Seeing an operator is not proof that a file can be read.  AUTODYN is never
    inferred from the presence of LS-DYNA or a generic result operator.
    """
    names = operator_names if operator_names is not None else []
    records = []
    for spec in DPF_FORMATS:
        matches = [name for name in names if any(token in name.casefold() for token in spec["reader_tokens"])]
        records.append({"component": spec["id"], "documented_formats": spec["formats"],
                        "public_source": FORMAT_SOURCE, "runtime_reader_candidates": matches[:20],
                        "runtime_reader_candidate_count": len(matches), "reader_candidates_truncated": len(matches) > 20,
                        "runtime_reachable": None if operator_names is None else bool(matches),
                        "validation_stage": "catalogued" if not matches else "reachable",
                        "file_read_verified": False,
                        "unavailable_reason": "No AUTODYN reader has been verified by this adapter."
                        if spec["id"] == "autodyn" else
                        ("Reader discovery has not run." if operator_names is None else
                         (None if matches else "No matching reader operator was reported by this server."))})
    return records


def _path_markers(value: Any):
    if isinstance(value, dict):
        if "$path" in value:
            if set(value) - {"$path", "mode", "overwrite"} or value.get("mode", "read") not in {"read", "write"}:
                raise ValueError("DPF $path requires only $path, read/write mode, and optional overwrite.")
            if not isinstance(value["$path"], str):
                raise ValueError("DPF $path must be a string.")
            if "overwrite" in value and type(value["overwrite"]) is not bool:
                raise ValueError("DPF path overwrite must be a boolean.")
            yield value
        else:
            for child in value.values():
                yield from _path_markers(child)
    elif isinstance(value, list):
        for child in value:
            yield from _path_markers(child)


def validate_dpf_recipe_paths(recipe: DPFRecipe, workdir: Path, *, path_checks: PathChecks | None = None) -> None:
    checks = path_checks or PathChecks(workdir)
    for data_file in recipe.result_files:
        checks.resolve(data_file.path, input_file=True, expect_file=True)
    for operation in recipe.operations:
        if operation.op == "export":
            checks.resolve(operation.path, expect_file=True)
        if operation.op == "operator":
            for marker in _path_markers(operation.inputs):
                checks.resolve(marker["$path"], input_file=marker.get("mode", "read") == "read", expect_file=True)


def dpf_produced_paths(recipe: DPFRecipe) -> list[str]:
    paths = []
    for operation in recipe.operations:
        if operation.op == "export":
            paths.append(operation.path)
        elif operation.op == "operator":
            paths.extend(marker["$path"] for marker in _path_markers(operation.inputs) if marker.get("mode", "read") == "write")
    return list(dict.fromkeys(paths))


def _materialize_paths(recipe: DPFRecipe, workdir: Path) -> dict[str, Any]:
    data = recipe.model_dump(mode="json")
    for item in data["result_files"]:
        item["path"] = str(require_allowed_file(item["path"], base=workdir))
    for operation in data["operations"]:
        if operation["op"] == "export":
            operation["path"] = str(require_allowed_path(operation["path"], base=workdir, expect_file=True))
        elif operation["op"] == "operator":
            for marker in _path_markers(operation["inputs"]):
                marker["$path"] = str(require_allowed_path(marker["$path"], base=workdir,
                    must_exist=marker.get("mode", "read") == "read", expect_file=True))
    return data


def run_dpf_recipe(recipe: DPFRecipe | dict[str, Any], workdir: Path, run_dir: Path,
                   timeout_seconds: float) -> dict[str, Any]:
    parsed = recipe if isinstance(recipe, DPFRecipe) else DPFRecipe.model_validate(recipe)
    validate_dpf_recipe_paths(parsed, workdir)
    run_dir = require_allowed_path(run_dir, base=workdir, expect_file=False)
    run_dir.mkdir(parents=True, exist_ok=True)
    request = run_dir / "dpf.request.json"
    result_path = run_dir / "dpf.result.json"
    request.write_text(json.dumps({"recipe": _materialize_paths(parsed, workdir), "workdir": str(workdir),
                                   "timeout_seconds": timeout_seconds}, ensure_ascii=True), encoding="utf-8")
    result_path.unlink(missing_ok=True)
    environment = os.environ.copy()
    # A child starts in the recipe's workdir. Preserve the server's actual root
    # boundary instead of accidentally narrowing its implicit STARTUP_CWD.
    environment["ANSYS_MCP_ALLOWED_ROOTS"] = ";".join(str(root) for root in allowed_roots())
    source_root = str(Path(__file__).resolve().parents[1])
    environment["PYTHONPATH"] = source_root + os.pathsep + environment.get("PYTHONPATH", "")
    try:
        process = run_process([sys.executable, "-m", "ansys_mcp.dpf", "--request", str(request), "--result", str(result_path)],
                              cwd=workdir, env=environment, timeout_seconds=timeout_seconds, run_dir=run_dir, log_name="dpf")
    except WorkflowCancelled:
        return {"ok": False, "status": "cancelled", "outputs": {}, "artifacts": [],
                "diagnostics": [{"code": "WORKFLOW_CANCELLED", "message": "The DPF task was cancelled before launch."}]}
    if process.cancelled:
        return {"ok": False, "status": "cancelled", "outputs": {}, "artifacts": [],
                "diagnostics": [{"code": "WORKFLOW_CANCELLED", "message": "The DPF task was cancelled; its owned process tree was terminated."}],
                "process": {"pid": process.pid, "returncode": process.returncode, "termination_error": process.termination_error}}
    if process.timed_out:
        return {"ok": False, "status": "timed_out", "outputs": {}, "artifacts": [],
                "diagnostics": [{"code": "DPF_TIMEOUT", "message": "The DPF worker exceeded its deadline; its owned process tree was terminated."}],
                "process": {"pid": process.pid, "returncode": process.returncode, "termination_error": process.termination_error}}
    if not result_path.is_file():
        return {"ok": False, "status": "failed", "outputs": {}, "artifacts": [],
                "diagnostics": [{"code": "DPF_WORKER_FAILED", "message": "The DPF worker did not produce a result envelope."}],
                "process": {"pid": process.pid, "returncode": process.returncode}}
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
        if not isinstance(result, dict) or not {"ok", "status", "outputs", "diagnostics", "artifacts"} <= result.keys():
            raise ValueError("Invalid DPF result envelope.")
    except (ValueError, OSError) as exc:
        return {"ok": False, "status": "failed", "outputs": {}, "artifacts": [],
                "diagnostics": [{"code": "DPF_INVALID_RESULT", "message": str(exc)}]}
    if process.returncode != 0:
        result["ok"] = False
        result["status"] = "failed"
        result["diagnostics"].append({"code": "DPF_WORKER_EXIT", "message": "DPF worker exited abnormally."})
    result["process"] = {"pid": process.pid, "returncode": process.returncode, "elapsed_seconds": process.elapsed_s}
    try:
        _record_result_evidence(parsed, result)
    except (OSError, ValueError, TypeError):
        result["diagnostics"].append({"code": "DPF_EVIDENCE_NOT_PERSISTED", "severity": "warning",
                                      "message": "DPF completed, but capability bookkeeping could not be persisted."})
    return result


def _record_result_evidence(recipe: DPFRecipe, result: dict[str, Any]) -> None:
    from .capabilities import record_runtime_evidence
    values = result.get("outputs", {}).get("values", {})
    version = discover_installation().version_tag
    for value in values.values():
        if not isinstance(value, dict):
            continue
        for record in value.get("formats", []):
            if record["component"] == "autodyn":
                continue
            reachable = record.get("runtime_reachable") is True
            record_runtime_evidence({"product": "dpf", "version": version, "qualified_name": "readers." + record["component"],
                "runtime_reachable": reachable, "validation_stage": "reachable" if reachable else "catalogued",
                "unavailable_reason": None if reachable else "No matching reader operator reported.", "evidence_source": "dpf_reader_discovery",
                "backend": recipe.server.transport})
    if recipe.result_files and any(operation.id in values and operation.op in {"metadata", "result"} for operation in recipe.operations):
        primary = recipe.result_files[0].path.casefold()
        reader = next((spec["id"] for spec in DPF_FORMATS if any(primary.endswith("." + suffix) or Path(primary).name == suffix for suffix in spec["formats"])), None)
        if reader:
            verified_format = next(suffix for spec in DPF_FORMATS if spec["id"] == reader for suffix in spec["formats"]
                                   if primary.endswith("." + suffix) or Path(primary).name == suffix)
            evaluated = any(operation.op == "result" and operation.id in values for operation in recipe.operations)
            record_runtime_evidence({"product": "dpf", "version": version, "qualified_name": "readers." + reader,
                "runtime_reachable": True, "validation_stage": "result" if evaluated else "object", "evidence_source": "dpf_file_read",
                "backend": recipe.server.transport, "verified_format": verified_format})


def _load_dpf():
    from ansys.dpf import core as dpf
    return dpf


def _start_server(dpf: Any, recipe: DPFRecipe, timeout_seconds: float):
    installation = discover_installation()
    protocol = dpf.server_factory.CommunicationProtocols.InProcess if recipe.server.transport == "in_process" else dpf.server_factory.CommunicationProtocols.gRPC
    config = dpf.ServerConfig(protocol=protocol, legacy=False, grpc_mode=dpf.server_factory.GrpcMode.mTLS)
    options = {"as_global": False, "config": config, "timeout": min(recipe.server.startup_timeout_seconds, timeout_seconds),
               "ip": "127.0.0.1", "port": 0, "use_docker_by_default": False, "use_pypim_by_default": False}
    if installation.root is not None:
        options["ansys_path"] = str(installation.root)
    return dpf.start_local_server(**options)


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "tolist"):
        return _jsonable(value.tolist())
    return str(value)


def _is_field(value: Any) -> bool:
    return hasattr(value, "scoping") and hasattr(value, "get_entity_data") and hasattr(value, "location")


def _fields(value: Any):
    if _is_field(value):
        yield 0, {}, value
    elif hasattr(value, "get_label_space") and hasattr(value, "__len__"):
        for index in range(len(value)):
            field = value[index]
            if not _is_field(field):
                raise ValueError("Export requires a Field or FieldsContainer.")
            yield index, _jsonable(value.get_label_space(index)), field
    else:
        raise ValueError("Export requires a Field or FieldsContainer.")


def _entity_rows(data: Any) -> list[list[Any]]:
    rows = _jsonable(data)
    if not isinstance(rows, list):
        return [[rows]]
    if rows and isinstance(rows[0], list):
        return rows
    return [rows]


def _summary(value: Any, sample_entities: int = 0) -> dict[str, Any]:
    if _is_field(value) or hasattr(value, "get_label_space"):
        summaries = []
        for index, labels, field in _fields(value):
            scoping = field.scoping
            entity_count = int(scoping.size) if hasattr(scoping, "size") else len(scoping.ids)
            item = {"index": index, "labels": labels, "location": str(field.location), "unit": str(field.unit),
                    "entities": entity_count, "components": int(field.component_count)}
            if sample_entities:
                count = min(sample_entities, entity_count)
                item["sample"] = [{"entity_id": int(scoping.id(i) if hasattr(scoping, "id") else scoping.ids[i]),
                                   "data": _jsonable(field.get_entity_data(i))} for i in range(count)]
            summaries.append(item)
            if len(summaries) >= 100:
                break
        return {"type": type(value).__name__, "field_count": 1 if _is_field(value) else len(value),
                "fields": summaries, "truncated": not _is_field(value) and len(value) > 100}
    if isinstance(value, str):
        return {"type": "str", "value": value[:8192], "characters": len(value), "truncated": len(value) > 8192}
    if isinstance(value, (bool, int, float)) or value is None:
        return {"type": type(value).__name__, "value": _jsonable(value)}
    if isinstance(value, dict):
        return {"type": "dict", "size": len(value), "value": _bounded_json(value), "truncated": len(value) > 100}
    if isinstance(value, (list, tuple)):
        return {"type": type(value).__name__, "size": len(value), "sample": _bounded_json(value), "truncated": len(value) > 100}
    if hasattr(value, "tolist"):
        return {"type": type(value).__name__, "shape": list(value.shape), "sample": _jsonable(value.ravel()[:100])}
    return {"type": type(value).__name__}


def _bounded_json(value: Any, depth: int = 0) -> Any:
    if depth >= 5:
        return {"type": type(value).__name__, "summary_only": True}
    if isinstance(value, dict):
        return {str(key): _bounded_json(value[key], depth + 1) for key in list(value)[:100]}
    if isinstance(value, (tuple, list)):
        return [_bounded_json(item, depth + 1) for item in value[:100]]
    if isinstance(value, str):
        return value[:8192]
    return _jsonable(value)


def _metadata(model: Any) -> dict[str, Any]:
    info = model.metadata.result_info
    results = []
    for available in info.available_results:
        item = {}
        for name in ("name", "operator_name", "unit", "native_location", "dimensionality", "n_components", "homogeneity"):
            item[name] = _jsonable(getattr(available, name, None))
        results.append(item)
    metadata = {"analysis_type": str(info.analysis_type), "physics_type": str(info.physics_type),
                "unit_system": str(info.unit_system_name), "solver_version": str(info.solver_version),
                "available_results": results, "file_read_verified": True}
    time_support = model.metadata.time_freq_support
    metadata["time_frequency_sets"] = int(time_support.n_sets)
    # Do not expose result-file user_name, job_name, main_title, or file paths.
    return metadata


def _resolve_input(value: Any, handles: dict[str, Any], workdir: Path, pin_name: str) -> Any:
    if isinstance(value, dict):
        if set(value) == {"$ref"}:
            return handles[value["$ref"]]
        if "$path" in value:
            resolved = require_allowed_path(value["$path"], base=workdir, must_exist=value.get("mode", "read") == "read", expect_file=True)
            if value.get("mode", "read") == "write":
                if resolved.exists() and not value.get("overwrite", False):
                    raise FileExistsError("DPF operator output exists; path overwrite must be explicit.")
                resolved.parent.mkdir(parents=True, exist_ok=True)
            return str(resolved)
        raise ValueError("DPF operator dictionaries must be $ref or $path inputs.")
    if isinstance(value, list):
        return [_resolve_input(item, handles, workdir, pin_name) for item in value]
    if isinstance(value, str) and pin_name not in _SAFE_STRING_PINS:
        raise ValueError("String DPF inputs require a path marker unless the public pin is a location, unit, or named selection.")
    return value


def _operator(dpf: Any, server: Any, operation: Any, handles: dict[str, Any], workdir: Path):
    operator = dpf.Operator(operation.name, server=server)
    specs = operator.specification.inputs
    for requested, value in operation.inputs.items():
        if requested.isdigit():
            number = int(requested)
            spec = specs[number]
        else:
            found = [(number, spec) for number, spec in specs.items() if spec.name == requested]
            if len(found) != 1:
                raise ValueError("DPF input pin does not resolve uniquely: " + requested)
            number, spec = found[0]
        operator.connect(number, _resolve_input(value, handles, workdir, spec.name))
    return operator.eval(pin=operation.output_pin)


def _export(value: Any, target: Path, format_name: str, overwrite: bool) -> dict[str, Any]:
    if target.exists() and not overwrite:
        raise FileExistsError("DPF export target exists; set overwrite explicitly.")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=".dpf-", suffix=".partial", dir=target.parent, delete=False) as allocated:
        temporary = Path(allocated.name)
    count = 0
    try:
        if format_name == "csv":
            fields = list(_fields(value))
            components = max((int(field.component_count) for _, _, field in fields), default=0)
            with temporary.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(["field_index", "label_space", "location", "unit", "entity_id", "entity_row"] +
                                ["component_" + str(i) for i in range(components)])
                for index, labels, field in fields:
                    for entity_index, entity_id in enumerate(field.scoping.ids):
                        for row_index, row in enumerate(_entity_rows(field.get_entity_data(entity_index))):
                            writer.writerow([index, json.dumps(labels, sort_keys=True), field.location, field.unit,
                                             int(entity_id), row_index] + row + [""] * (components - len(row)))
                            count += 1
        else:
            with temporary.open("w", encoding="utf-8") as stream:
                stream.write('{"schema_version":1,"fields":[')
                first = True
                for index, labels, field in _fields(value):
                    if not first:
                        stream.write(",")
                    first = False
                    metadata = {"index": index, "labels": labels, "location": str(field.location), "unit": str(field.unit),
                                "components": int(field.component_count)}
                    stream.write(json.dumps(metadata, ensure_ascii=True)[:-1] + ',"entities":[')
                    for entity_index, entity_id in enumerate(field.scoping.ids):
                        if entity_index:
                            stream.write(",")
                        json.dump({"id": int(entity_id), "data": _jsonable(field.get_entity_data(entity_index))}, stream, allow_nan=False)
                        count += 1
                    stream.write("]}")
                stream.write("]}")
        # Same-directory publication keeps incomplete data out of the contract.
        # A hard-link creates the non-overwrite destination atomically, even when
        # another process creates it while the export is being generated.
        if overwrite:
            temporary.replace(target)
        else:
            os.link(temporary, target)
            temporary.unlink()
    finally:
        temporary.unlink(missing_ok=True)
    return {**_file_artifact(target), "format": format_name, "rows": count}


def _file_artifact(target: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    with target.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return {"path": str(target), "size_bytes": target.stat().st_size, "sha256": digest.hexdigest()}


def _execute_recipe(recipe: DPFRecipe, workdir: Path, timeout_seconds: float) -> dict[str, Any]:
    outputs: dict[str, Any] = {}
    artifacts: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    server = None
    current_operation = None
    try:
        dpf = _load_dpf()
        server = _start_server(dpf, recipe, timeout_seconds)
        handles: dict[str, Any] = {}
        model = None
        if recipe.result_files:
            primary, *additional = recipe.result_files
            sources = dpf.DataSources(str(require_allowed_file(primary.path, base=workdir)), key=primary.key or "", server=server)
            for data_file in additional:
                sources.add_file_path(str(require_allowed_file(data_file.path, base=workdir)), key=data_file.key or "")
            handles["data_sources"] = sources
            model = dpf.Model(sources, server=server)
        for operation in recipe.operations:
            current_operation = operation.id
            if operation.op == "discover":
                names = sorted(str(name) for name in dpf.available_operator_names(server=server))
                matching = [name for name in names if operation.name_filter is None or operation.name_filter.casefold() in name.casefold()]
                value = {"operator_total": len(names), "matching_total": len(matching),
                         "operators": matching[operation.offset:operation.offset + operation.limit],
                         "offset": operation.offset, "limit": operation.limit,
                         "formats": format_capabilities(names), "server_version": str(server.version)}
                handles[operation.id] = value
                outputs[operation.id] = value
            elif operation.op == "metadata":
                value = _metadata(model)
                handles[operation.id] = value
                outputs[operation.id] = value
            elif operation.op == "result":
                available_results = {item.name: item for item in model.metadata.result_info.available_results}
                available_names = set(available_results)
                if operation.name not in available_names:
                    raise ValueError("The opened result file does not advertise result: " + operation.name)
                result = getattr(model.results, operation.name)
                if operation.time_scoping is not None:
                    result = result.on_time_scoping(operation.time_scoping)
                if operation.mesh_scoping is not None:
                    result = result.on_mesh_scoping(dpf.Scoping(ids=operation.mesh_scoping, location=operation.mesh_location, server=server))
                if operation.named_selection is not None:
                    result = result.on_named_selection(operation.named_selection)
                # Nodal-only readers such as U have no requested_location input.
                # Do not ask their Result wrapper to connect a nonexistent pin.
                native_location = str(available_results[operation.name].native_location)
                if operation.requested_location is not None and operation.requested_location != native_location:
                    result = result.on_location(operation.requested_location)
                value = result.eval()
                handles[operation.id] = value
                outputs[operation.id] = _summary(value)
            elif operation.op == "operator":
                value = _operator(dpf, server, operation, handles, workdir)
                handles[operation.id] = value
                outputs[operation.id] = _summary(value)
                for marker in _path_markers(operation.inputs):
                    if marker.get("mode", "read") == "write":
                        target = require_allowed_file(marker["$path"], base=workdir)
                        artifacts.append(_file_artifact(target))
            elif operation.op == "inspect":
                value = _summary(handles[operation.source], operation.sample_entities)
                handles[operation.id] = value
                outputs[operation.id] = value
            elif operation.op == "export":
                value = _export(handles[operation.source], require_allowed_path(operation.path, base=workdir, expect_file=True), operation.format, operation.overwrite)
                artifacts.append(value)
                handles[operation.id] = value
                outputs[operation.id] = value
        return {"ok": True, "status": "completed", "outputs": {"values": outputs}, "artifacts": artifacts, "diagnostics": diagnostics}
    except (ImportError, ModuleNotFoundError) as exc:
        return {"ok": False, "status": "dependency_unavailable", "outputs": {"values": outputs}, "artifacts": artifacts,
                "diagnostics": [{"code": "DPF_DEPENDENCY_UNAVAILABLE", "message": str(exc), "operation_id": current_operation}]}
    except Exception as exc:
        text = str(exc)
        certificate_error = "mtls" in text.casefold() and "certificat" in text.casefold()
        return {"ok": False, "status": "authentication_required" if certificate_error else "failed", "outputs": {"values": outputs}, "artifacts": artifacts,
                "diagnostics": [{"code": "DPF_MTLS_CERTIFICATES_REQUIRED" if certificate_error else "DPF_OPERATION_FAILED",
                    "message": text, "operation_id": current_operation, "exception_type": type(exc).__name__}]}
    finally:
        if server is not None:
            try:
                server.shutdown()
            except Exception:
                pass


def _main() -> int:
    parser = argparse.ArgumentParser(description="Private supervised DPF recipe worker")
    parser.add_argument("--request", required=True)
    parser.add_argument("--result", required=True)
    args = parser.parse_args()
    result_path = require_allowed_path(args.result, expect_file=True)
    try:
        request = json.loads(require_allowed_file(args.request).read_text(encoding="utf-8"))
        recipe = DPFRecipe.model_validate(request["recipe"])
        workdir = require_allowed_path(request["workdir"], expect_file=False)
        validate_dpf_recipe_paths(recipe, workdir)
        result = _execute_recipe(recipe, workdir, request["timeout_seconds"])
    except Exception as exc:
        result = {"ok": False, "status": "failed", "outputs": {}, "artifacts": [],
                  "diagnostics": [{"code": "DPF_REQUEST_FAILED", "message": str(exc)}]}
    result_path.write_text(json.dumps(result, ensure_ascii=True, allow_nan=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
