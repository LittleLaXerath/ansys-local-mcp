"""Product-scoped fingerprints and read-only revalidation of CFD evidence.

These checks inspect retained product outputs, not solver processes. They grant
workflow evidence only, never every API/model in the offline declaration tree.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Callable

from .config import discover_installation, known_paths, require_allowed_file


def workflow_scope(product: str, version: str) -> dict[str, Any]:
    from .capabilities import _runtime_scope, _binary_digest
    if product in {"mechanical", "dpf"}:
        return _runtime_scope(version)  # Preserve every existing Mechanical/DPF receipt identity.
    if product not in {"fluent", "cfx"}:
        raise ValueError("Unsupported workflow evidence product.")
    installation = discover_installation()
    keys = ("fluent",) if product == "fluent" else ("cfx_pre", "cfx_solve", "cfx_post")
    unavailable = {"installation_fingerprint": "unresolved", "version": version,
                   "product": product, "current_installation": False}
    if installation.root is None or installation.version_tag != version:
        return unavailable
    paths = known_paths(installation.root)
    if any(not paths[key].is_file() for key in keys):
        return unavailable
    digest = hashlib.sha256((product + "\0" + str(installation.root.resolve()).casefold() + "\0" + version).encode())
    for key in keys:
        path = paths[key]
        stat = path.stat()
        digest.update(key.encode())
        digest.update(_binary_digest(str(path), stat.st_size, stat.st_mtime_ns).encode("ascii"))
    return {"installation_fingerprint": digest.hexdigest(), "version": version,
            "product": product, "current_installation": True}


def version_tag(text: str) -> str:
    match = re.search(r"20(\d{2})\s+R([12])\b", text)
    if not match:
        raise ValueError("Native product output has no recognized release tag.")
    return match.group(1) + match.group(2)


def _result(value: dict[str, Any], *, kind: str) -> dict[str, Any]:
    if value.get("ok") is not True or not value.get("steps"):
        raise ValueError("Failed or empty CFD workflow cannot grant evidence.")
    if any(step.get("type") != kind or step.get("ok") is not True or step.get("status") not in {"complete", "closed", "ready"}
           for step in value["steps"]):
        raise ValueError("Incomplete CFD workflow step cannot grant evidence.")
    return value["steps"][0]["outputs"]


def validate_fluent(report: dict, envelope: dict, sources: Callable[[str], Path], version: str,
                    clients: dict[str, str]) -> dict:
    from .benchmarks import validate_fluent_channel
    def read(name):
        return json.loads(sources(name).read_text(encoding="utf-8"))
    solve = _result(envelope, kind="pyfluent")
    fields = _result(report, kind="pyfluent")
    reopened = _result(read("reopen"), kind="pyfluent")
    observations = [_result(read(name), kind="pyfluent") for name in ("new", "reconnect", "close")]
    session = solve["session"]
    values = solve["results"]
    mass = {key: value for item in values["mass_flows"] for key, value in item.items()}
    checked = validate_fluent_channel(fields["results"], reopened["results"], mass["mass_in"][0], mass["mass_out"][0])
    checks = checked["checks"]
    physics = values["physics"]
    fluid = physics["materials"]["fluid"]["air"]
    inlet = physics["boundary_conditions"]["velocity_inlet"]["inlet"]["momentum"]["velocity_magnitude"]
    checks.update(material_readback=fluid["density"]["value"] == 1 and fluid["viscosity"]["value"] == 0.001,
                  inlet_readback=inlet["value"] == 0.1,
                  laminar_readback=physics["models"]["viscous"]["model"] == "laminar",
                  mass_units=mass["mass_in"][1] == mass["mass_out"][1] == "kg/s",
                  product_version=version_tag(session["product_version"]) == version,
                  client_version=clients.get("ansys-fluent-core") == session["client_version"],
                  same_session=all(item["session"]["id"] == session["id"] for item in [fields, reopened, *observations]),
                  closed=observations[-1]["session"]["state"] == "closed" and not observations[-1]["session"]["live_owned_processes"])
    case = sources("case")
    data = sources("data")
    checks["native_outputs"] = (case.name.endswith(".cas.h5") and data == case.with_name(case.name[:-7]+".dat.h5")
                                and case.stat().st_size > 0 and data.stat().st_size > 0)
    # Bind the native files to the successful write and read-back requests.
    def operation_file(workflow, op):
        return [operation.get("case_data") for step in workflow["steps"] for operation in step.get("operations", []) if operation.get("op") == op]
    solve_workflow = read("solve_workflow")
    reopen_workflow = read("reopen_workflow")
    checks["native_file_binding"] = str(case) in operation_file(solve_workflow, "write") and any(
        (step.get("read") or {}).get("case_data") == str(case) for step in reopen_workflow["steps"])
    for role in ("mesh", "transcript", "monitors", "events"):
        checks[role + "_nonempty"] = sources(role).stat().st_size > 0
    return {**checked, "ok": all(checks.values()), "passed": all(checks.values()),
            "scope": "steady_planar_channel_only", "executed_apis": []}


def validate_cfx(report: dict, envelope: dict, sources: Callable[[str], Path], version: str,
                 clients: dict[str, str]) -> dict:
    from .cfx_monitor import parse_cfx_log, summarize
    _result(envelope, kind="cfx")
    steps = envelope["steps"]
    spec = json.loads(sources("specification").read_text(encoding="utf-8"))
    geometry = json.loads(sources("case_specification").read_text(encoding="utf-8"))
    text = sources("solver_log").read_text(encoding="utf-8", errors="replace")
    log = summarize(parse_cfx_log(text), expected_end_time=spec["end_time_s"])
    with sources("metrics").open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    if len(rows) != 1:
        raise ValueError("CFX native export must contain one final-result row.")
    measured = {name: float(value) for name, value in rows[0].items()}
    with sources("reference_forces").open(newline="", encoding="utf-8-sig") as stream:
        reference = min(csv.DictReader(stream), key=lambda row: abs(float(row["TIME"]) - spec["end_time_s"]))
    g, fluid = geometry["geometry"], geometry["fluid"]
    volume = ((g["x_max_m"]-g["x_min_m"])*(g["y_max_m"]-g["y_min_m"]) - math.pi*(g["diameter_m"]/2)**2)*(g["z_max_m"]-g["z_min_m"])
    mass_in = fluid["density_kg_m3"]*fluid["inlet_velocity_m_s"][0]*(g["y_max_m"]-g["y_min_m"])*(g["z_max_m"]-g["z_min_m"])
    checks = {"finite_native_metrics": all(math.isfinite(v) for v in measured.values()),
              "three_operations": len(steps) == 3 and [step["outputs"]["operation"] for step in steps] == ["pre", "solve", "post"],
              "process_completion": all(step["process"]["returncode"] == 0 and step["process"]["handoff_supervised"] is True
                                        and step["process"]["job_processes_at_exit"] == 0 for step in steps),
              "product_version": version_tag(text.splitlines()[0]) == version,
              "finish_marker": log["finished_marker"] is True, "end_reached": log["end_time_reached"] is True,
              "residuals": log["converged"] is True and 0 < log["residual_target"] <= 1e-5,
              "step_count": log["time_steps"] == spec["expected_steps"] and spec["expected_steps"] > 0,
              "short_time_window": 0 < spec["end_time_s"]-spec["source_time_s"] <= 0.1,
              "time_increment": math.isclose((spec["end_time_s"]-spec["source_time_s"])/spec["dt_s"], spec["expected_steps"], abs_tol=1e-6),
              "physical_time": abs(measured["time_s"]-spec["end_time_s"]) <= 1e-6,
              "positive_volume": measured["min_volume_m3"] > 0 and abs(measured["volume_m3"]/volume-1) <= 1e-5,
              "mass_flow": abs(abs(measured["mass_in_kg_s"])/mass_in-1) <= 1e-4,
              "reference_time": abs(float(reference["TIME"])-spec["end_time_s"]) <= 1e-6}
    imbalance = abs(measured["mass_in_kg_s"]+measured["mass_out_kg_s"])/abs(measured["mass_in_kg_s"])
    checks["mass_balance"] = imbalance <= 1e-4
    measured["relative_mass_imbalance"] = imbalance
    for axis in ("x", "y"):
        target = float(reference["USER POINT,Cylinder_F" + axis])
        error = abs(measured["force_"+axis+"_N"]-target)/max(abs(target), 1e-10)
        measured["force_"+axis+"_relative_error"] = error
        checks["force_"+axis+"_regression"] = error <= 0.02
    result = sources("result")
    checks["native_result_binding"] = log["result_path_from_log"] is not None and require_allowed_file(log["result_path_from_log"]) == result
    solve = steps[1]["outputs"]
    checks["recorded_result_binding"] = require_allowed_file(solve["res_path"]) == result and require_allowed_file(solve["log_path"]) == sources("solver_log")
    recorded = [(steps[0]["outputs"].get("artifacts", []), "def", sources("def")),
                (steps[1]["outputs"].get("artifacts", []), "res", result)]
    checks["original_artifact_metadata"] = all(any(
        item.get("role") == role and require_allowed_file(item["path"]) == path
        and item.get("fresh") is True and item.get("size") == path.stat().st_size
        and item.get("mtime_ns") == path.stat().st_mtime_ns for item in items if item.get("role") == role)
        for items, role, path in recorded)
    checks["nonempty_native_inputs_outputs"] = all(sources(role).stat().st_size > 0 for role in ("pre_script", "post_script", "def", "result", "restart"))
    post = sources("post_script").read_text(encoding="utf-8")
    checks["post_reads_result"] = any(line.strip() == ">load filename=" + str(result).replace("\\", "/") for line in post.splitlines())
    expected_paths = known_paths()
    checks["official_entries"] = all(Path(step["command"]["executable"]).resolve() == expected_paths[key].resolve()
                                     for step, key in zip(steps, ("cfx_pre", "cfx_solve", "cfx_post")))
    checks["saved_report_matches_export"] = all(key in report["measured"] and math.isclose(float(report["measured"][key]), value, rel_tol=1e-10, abs_tol=1e-14)
                                              for key, value in measured.items())
    return {"ok": all(checks.values()), "passed": all(checks.values()), "checks": checks, "metrics": measured,
            "scope": "short_restart_only_not_shedding_frequency", "executed_apis": []}


VALIDATORS = {"fluent_channel": ("fluent", "pyfluent", validate_fluent),
              "cfx_short_restart": ("cfx", "official_cfx", validate_cfx)}


def validate_receipt(receipt: Any, report: dict, envelope: dict, parameters: dict,
                     resolved: dict[str, Path], base: Path, validation: dict) -> dict:
    product, backend, validator = VALIDATORS[receipt.validator]
    if receipt.product != product or receipt.backend != backend:
        raise ValueError("CFD validator does not match receipt product/backend.")
    allowed = set(resolved.values())
    def source(role):
        path = require_allowed_file(parameters["sources"][role], base=base)
        if path not in allowed:
            raise ValueError("CFD source must be explicitly hash-bound: " + role)
        return path
    if report.get("executed_apis"):
        raise ValueError("CFD receipts currently grant workflow evidence only, not API promotion.")
    checked = validator(report, envelope, source, receipt.product_version, receipt.client_versions)
    if product == "fluent":
        from .acceptance_evidence import file_sha256
        paths = [source(role) for role in ("case", "data", "mesh", "reopen")]
        paths += [resolved[receipt.report.path], resolved[receipt.envelope.path]]
        manifest = validation.get("artifacts", {})
        checked["checks"]["original_artifact_manifest"] = all(
            manifest.get(path.name, {}).get("size") == path.stat().st_size
            and manifest.get(path.name, {}).get("sha256") == file_sha256(path) for path in paths)
        checked["ok"] = checked["passed"] = all(checked["checks"].values())
    return checked
