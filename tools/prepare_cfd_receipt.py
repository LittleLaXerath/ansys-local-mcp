"""Prepare a new private receipt from retained CFD acceptance, without a solver.

Preparation writes new receipt files only. Import remains dry-run unless --apply
is supplied. Never overwrites the original acceptance or a previous receipt.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from ansys_mcp.acceptance_evidence import file_sha256, import_receipt
from ansys_mcp.cfd_evidence import workflow_scope
from ansys_mcp.config import require_allowed_file, require_allowed_path
from ansys_mcp.jobs import _write_json


def prepare(product: str, directory: Path, output: Path, *, version: str = "261",
            reference_specification: Path | None = None, reference_forces: Path | None = None) -> Path:
    directory = require_allowed_path(directory, must_exist=True, expect_file=False)
    output = require_allowed_path(output, expect_file=False)
    scope = workflow_scope(product, version)
    if not scope["current_installation"]:
        raise ValueError("Current product installation is unavailable for fingerprinting.")
    def read(name):
        return json.loads(require_allowed_file(directory / name).read_text(encoding="utf-8"))
    if product == "fluent":
        envelope_name, report_name = "03-solve.result.json", "04-readback.result.json"
        validation_name = "numerical-acceptance-v2.json"
        actor = Path(read(envelope_name)["steps"][0]["outputs"]["session"]["directory"])
        sources = {"new": directory / "01-new.result.json", "reconnect": directory / "02-reconnect.result.json",
                   "reopen": directory / "05-reopen.result.json", "close": directory / "06-close.result.json",
                   "case": directory / "channel.cas.h5", "data": directory / "channel.dat.h5", "mesh": directory / "channel.msh",
                   "solve_workflow": directory / "03-solve.workflow.json", "reopen_workflow": directory / "05-reopen.workflow.json",
                   **{role: actor / name for role, name in (("transcript", "transcript.log"), ("monitors", "monitors.jsonl"), ("events", "events.jsonl"))}}
        client_version = read(envelope_name)["steps"][0]["outputs"]["session"]["client_version"]
        clients = {"ansys-fluent-core": client_version}
        backend, validator = "pyfluent", "fluent_channel"
    elif product == "cfx":
        if reference_specification is None or reference_forces is None:
            raise ValueError("CFX requires the retained reference specification and force history.")
        envelope_name = "workflow-result.json"
        report_name = validation_name = "acceptance.json"
        sources = {"specification": directory / "specification.json", "case_specification": reference_specification,
                   "reference_forces": reference_forces, "metrics": directory / "data/metrics.csv",
                   "solver_log": directory / "data/smoke_001.out", "result": directory / "data/smoke_001.res",
                   "def": directory / "data/smoke.def", "restart": directory / "data/restart_001.res",
                   "pre_script": directory / "data/prepare.pre", "post_script": directory / "data/export.cse"}
        clients = {}
        backend, validator = "official_cfx", "cfx_short_restart"
    else:
        raise ValueError("Product must be fluent or cfx.")
    sources = {role: require_allowed_file(path) for role, path in sources.items()}
    envelope = read(envelope_name)
    if envelope.get("ok") is not True:
        raise ValueError("Failed workflow is not eligible for receipt preparation.")
    output.mkdir(parents=True, exist_ok=False)
    parameters = output / "parameters.json"
    _write_json(parameters, {"sources": {key: str(path) for key, path in sources.items()},
                             "scope_origin": "historical_revalidation", "product": product,
                             "fingerprint_note": "Captured at import; no original binary hash was recorded at run time."})
    def bound(path):
        path = require_allowed_file(path)
        return {"path": str(path), "sha256": file_sha256(path)}
    receipt = {"version": "1", "product": product, "product_version": version, "backend": backend,
               "run_id": envelope["run_id"], "case_id": validator, "validator": validator,
               "scope_origin": "historical_revalidation", "observed_at": datetime.now(timezone.utc).isoformat(),
               "client_versions": {**clients, "receipt-python": sys.version.split()[0]},
               "installation_fingerprint": scope["installation_fingerprint"],
               "report": bound(directory / report_name), "validation": bound(directory / validation_name),
               "envelope": bound(directory / envelope_name), "parameters": bound(parameters),
               "artifacts": [bound(path) for path in dict.fromkeys(sources.values())]}
    path = output / "receipt.json"
    _write_json(path, receipt)
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("product", choices=["fluent", "cfx"])
    parser.add_argument("directory", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", default="261")
    parser.add_argument("--reference-specification", type=Path)
    parser.add_argument("--reference-forces", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    path = prepare(args.product, args.directory, args.output, version=args.version,
                   reference_specification=args.reference_specification, reference_forces=args.reference_forces)
    checked = import_receipt(path, apply=args.apply)
    print(json.dumps({"ok": checked["ok"], "dry_run": checked["dry_run"], "receipt": str(path),
                      "index_path": checked["index_path"], "checks": checked["checks"]}, indent=2))


if __name__ == "__main__":
    main()
