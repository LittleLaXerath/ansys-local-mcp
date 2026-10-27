"""Check private product receipts before importing workflow/API evidence.

Receipt verification never launches a product. Original files remain in place;
the private evidence index stores their paths and hashes, not copies of models.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .config import evidence_root, require_allowed_file, require_allowed_path
from .jobs import _write_json


class EvidenceFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class AcceptanceReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal["1"] = "1"
    product: Literal["mechanical"] = "mechanical"
    product_version: str = Field(pattern=r"^[0-9]{3,4}$")
    backend: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    client_versions: dict[str, str] = Field(default_factory=dict)
    run_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
    case_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    observed_at: datetime
    installation_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    validator: Literal["core_beam_scalars", "contact", "parameters", "specialized"]
    report: EvidenceFile
    validation: EvidenceFile
    envelope: EvidenceFile
    parameters: EvidenceFile
    artifacts: list[EvidenceFile] = Field(min_length=1)

    @model_validator(mode="after")
    def dated(self) -> "AcceptanceReceipt":
        if self.observed_at.tzinfo is None:
            raise ValueError("Evidence timestamp must include its timezone.")
        import re
        if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", name)
               or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}", version)
               for name, version in self.client_versions.items()):
            raise ValueError("Client versions must be distribution/version labels, not paths.")
        return self


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("An evidence report must be a JSON object.")
    return value


def _core_scalars(report: dict, parameters: dict, directory: Path) -> dict:
    from .benchmarks import validate_core_scalars
    return validate_core_scalars(report, parameters)


def _validate(validator: str, report: dict, parameters: dict, directory: Path) -> dict:
    if validator == "contact":
        from .benchmarks import validate_contact
        checked = validate_contact(report, parameters)
        return {**checked, "ok": checked["passed"]}
    if validator == "parameters":
        from .benchmarks import validate_parameters
        checked = validate_parameters(report)
        return {**checked, "ok": checked["passed"]}
    if validator == "specialized":
        from .benchmarks import validate_specialized
        return validate_specialized(report)
    return _core_scalars(report, parameters, directory)


def inspect_receipt(path: Path) -> dict[str, Any]:
    from .capabilities import _runtime_scope
    source = require_allowed_file(path)
    receipt = AcceptanceReceipt.model_validate(_read(source))
    scope = _runtime_scope(receipt.product_version)
    if not scope.get("current_installation") or scope["installation_fingerprint"] != receipt.installation_fingerprint:
        raise ValueError("Receipt belongs to a different or unavailable product installation.")
    files = [receipt.report, receipt.validation, receipt.envelope, receipt.parameters, *receipt.artifacts]
    resolved: dict[str, Path] = {}
    for item in files:
        target = require_allowed_file(item.path, base=source.parent)
        if file_sha256(target) != item.sha256:
            raise ValueError("Evidence artifact changed: " + target.name)
        resolved[item.path] = target
    report = _read(resolved[receipt.report.path])
    validation = _read(resolved[receipt.validation.path])
    envelope = _read(resolved[receipt.envelope.path])
    parameters = _read(resolved[receipt.parameters.path])
    if receipt.validator == "specialized":
        for key in ("result_table", "result_file"):
            target = require_allowed_file(report[key], base=resolved[receipt.report.path].parent)
            if target not in [resolved[item.path] for item in receipt.artifacts]:
                raise ValueError("Native result/table must be a hash-checked artifact.")
    saved_passed = validation.get("ok", validation.get("passed"))
    if saved_passed is not True or envelope.get("ok") is not True:
        raise ValueError("Failed product execution or numerical validation cannot be imported.")
    session = envelope.get("outputs", {}).get("session", {})
    if session.get("backend") != receipt.backend or str(session.get("version")) != receipt.product_version:
        raise ValueError("Receipt backend/version does not match the product envelope.")
    # Validators use fixed reference formulas/tolerances; a saved 'ok' alone is
    # insufficient. Serialized NaN/Inf never passes a numeric gate.
    def finite(value):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("Non-finite values in evidence report.")
        if isinstance(value, dict):
            for child in value.values(): finite(child)
        elif isinstance(value, list):
            for child in value: finite(child)
    finite(report)
    finite(parameters)
    checked = _validate(receipt.validator, report, parameters, resolved[receipt.report.path].parent)
    if checked.get("ok") is not True:
        raise ValueError("Independent numerical revalidation did not pass.")
    names = report.get("executed_apis", [])
    if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
        raise ValueError("executed_apis must be a list of observed API names.")
    # Absolute artifact paths are retained only in this private receipt/index.
    payload = receipt.model_dump(mode="json")
    for name in ("report", "validation", "envelope", "parameters"):
        payload[name]["path"] = str(resolved[payload[name]["path"]])
    for item in payload["artifacts"]:
        item["path"] = str(resolved[item["path"]])
    return {"ok": True, "receipt": payload, "checks": checked,
            "executed_apis": names, "source": str(source), "source_sha256": file_sha256(source)}


def import_receipt(path: Path, *, apply: bool = False) -> dict[str, Any]:
    from .capabilities import record_runtime_evidence
    checked = inspect_receipt(path)
    receipt = checked["receipt"]
    # Validate all API names before the first write. No API calls are inferred
    # from a case name, an analysis type, or a Markdown acceptance table.
    import re
    if any(not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.]*", name) for name in checked["executed_apis"]):
        raise ValueError("Invalid executed API name.")
    target = require_allowed_path(evidence_root() / "workflows" / ("v" + receipt["product_version"])
        / receipt["installation_fingerprint"] / receipt["product"] / receipt["run_id"] / (receipt["case_id"] + ".json"))
    if apply:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            stored = _read(require_allowed_file(target))
            normalized = AcceptanceReceipt.model_validate(stored.get("receipt")).model_dump(mode="json")
            if normalized != checked["receipt"] or stored.get("source_sha256") != checked["source_sha256"]:
                raise ValueError("Receipt identity is already indexed; the original index was retained.")
            return {**stored, "dry_run": False, "index_path": str(target), "already_imported": True}
        checked["imported_apis"] = []
        for qualified in checked["executed_apis"]:
            record_runtime_evidence({"product": receipt["product"], "version": receipt["product_version"],
                "backend": receipt["backend"], "qualified_name": qualified, "runtime_reachable": True,
                "validation_stage": "result", "evidence_source": "verified_receipt_import",
                "observed_at": receipt["observed_at"], "run_id": receipt["run_id"], "case_id": receipt["case_id"],
                "receipt_sha256": checked["source_sha256"]})
            checked["imported_apis"].append(qualified)
        _write_json(target, checked)
    return {**checked, "dry_run": not apply, "index_path": str(target)}


def query_workflow_evidence(product: str, version: str, component: str | None, status: str | None,
                            offset: int, limit: int) -> dict[str, Any]:
    from .capabilities import _runtime_scope
    scope = _runtime_scope(version)
    root = require_allowed_path(evidence_root() / "workflows" / ("v" + version) / scope["installation_fingerprint"] / product)
    records = []
    for path in sorted(root.glob("*/*.json")) if root.is_dir() else []:
        try:
            stored = _read(require_allowed_file(path))
            receipt = stored["receipt"]
            if receipt["product"] != product or receipt["product_version"] != version:
                continue
            if component and component != receipt["case_id"]:
                continue
            item = {key: receipt[key] for key in ("product", "product_version", "backend", "run_id", "case_id", "observed_at")}
            item["client_versions"] = receipt.get("client_versions", {})
            try:
                original = require_allowed_file(stored["source"])
                if file_sha256(original) != stored["source_sha256"]:
                    raise ValueError("Receipt source changed.")
                inspect_receipt(original)
                item.update(status="verified", validation_stage="result", artifact_integrity="checked")
            except (OSError, ValueError, KeyError, TypeError):
                item.update(status="unavailable", validation_stage="catalogued", artifact_integrity="unavailable_or_changed")
            if status is None or status in {item["status"], item["validation_stage"]}:
                records.append(item)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return {"ok": True, "product": product, "kind": "workflow", "version": version, "total": len(records),
            "offset": offset, "limit": limit, "items": records[offset:offset+limit],
            "next_offset": offset+limit if offset+limit < len(records) else None, "evidence_scope": scope}


def receipt_provenance_available(observation: dict[str, Any]) -> bool:
    """Check imported API provenance without following an arbitrary index path."""
    import re
    try:
        if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", observation[key]) for key in ("run_id", "case_id")):
            return False
        target = require_allowed_file(evidence_root() / "workflows" / ("v" + observation["version"])
            / observation["installation_fingerprint"] / observation["product"] / observation["run_id"] / (observation["case_id"] + ".json"))
        stored = _read(target)
        if stored["source_sha256"] != observation["receipt_sha256"]:
            return False
        source = require_allowed_file(stored["source"])
        if file_sha256(source) != observation["receipt_sha256"]:
            return False
        receipt = AcceptanceReceipt.model_validate(_read(source))
        for item in (receipt.report, receipt.validation, receipt.envelope, receipt.parameters, *receipt.artifacts):
            if file_sha256(require_allowed_file(item.path, base=source.parent)) != item.sha256:
                return False
        return True
    except (ValueError, KeyError, TypeError, OSError):
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipt", type=Path)
    parser.add_argument("--apply", action="store_true", help="Import after independent checks; default is dry-run")
    args = parser.parse_args()
    print(json.dumps(import_receipt(args.receipt, apply=args.apply), indent=2))


if __name__ == "__main__":
    main()
