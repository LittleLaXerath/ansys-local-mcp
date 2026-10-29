"""Complete public-source API inventory, kept distinct from product evidence.

The official stubs are parsed, never executed.  This inventories every public
declaration and re-export in every source file for the requested release.  A
manifest accounts for each declaration (including duplicate stub definitions)
and hashes each source, making coverage measurable rather than a short hand-made
list.  Inherited members remain represented by their declaring type and bases.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import importlib.metadata
import json
import os
import re
import stat
import threading
from collections import Counter
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any


STUB_PACKAGE = "ansys-mechanical-stubs"
MECHANICAL_SOURCE = "https://scripting.mechanical.docs.pyansys.com/version/stable/api/ansys/mechanical/stubs"
VALIDATION_STAGES = ("catalogued", "reachable", "object", "workflow", "solve", "result")
COMPONENT_RULES: dict[str, tuple[str, ...]] = {
    "engineering_data": ("material", "engineeringdata"),
    "geometry": ("geometry", "geobody", "geoface", "geoedge", "part"),
    "selection": ("selection", "scoping", "path"),
    "coordinate_system": ("coordinatesystem", "coordinate_system"),
    "mesh": ("mesh", "node", "element", "sweep", "multizone", "inflation"),
    "connection": ("connection", "contact", "joint", "spring", "bearing", "remote"),
    "load": ("boundarycondition", "pressure", "force", "support", "displacement", "convection", "radiation", "heatflux", "pretension"),
    "solve": ("solve", "solver", "solutioninformation", "analysissettings", "timestep", "convergence"),
    "result": ("result", "stress", "strain", "probe", "deformation", "fatigue", "fracture", "chart", "figure", "image", "animation"),
    "parameter": ("parameter", "designpoint", "design_point"),
    "static_structural": ("staticstructural", "static_structural"),
    "transient_structural": ("transientstructural", "transient_structural"),
    "modal": ("modal",),
    "harmonic": ("harmonic",),
    "response_spectrum": ("responsespectrum",),
    "random_vibration": ("randomvibration",),
    "buckling": ("buckling",),
    "thermal": ("thermal", "temperature", "heat"),
    "acoustic": ("acoustic", "sound"),
    "coupled_field": ("coupledfield", "coupled_field", "systemcoupling"),
    "explicit_dynamics": ("explicit", "autodyn", "exdanalysissettings"),
    "ls_dyna": ("lsdyna", "ls_dyna", "ls-dyna"),
    "rigid_body_dynamics": ("rigiddynamic", "rigidbodydynamic", "rigid_body", "mbdanalysissettings", "rbdanalysissettings", "analysistype.mbd"),
    "composites_acp": ("composite", "acp", "layered", "ply"),
    "designlife": ("designlife", "ncode"),
    "optimization": ("optim", "topology", "shapeconstraint", "densityconstraint"),
    "additive": ("additive", "amprocess", "ambuild", "amoverhang", "amsupport"),
    "act": (".act.", "extension"),
}
_COMPONENT_ALIASES = {"ed": "explicit_dynamics", "explicit": "explicit_dynamics", "ls-dyna": "ls_dyna",
                      "lsdyna": "ls_dyna", "rbd": "rigid_body_dynamics", "acp": "composites_acp",
                      "materials": "engineering_data", "topology": "optimization"}
_EVIDENCE: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
_LOADED_EVIDENCE: dict[tuple[str, str], tuple[int, int]] = {}
_LOCK = threading.RLock()


def _version(value: str | None) -> str:
    if value is None:
        from .config import discover_installation
        value = discover_installation().version_tag
    result = str(value).lstrip("vV")
    if not re.fullmatch(r"\d{3,4}", result):
        raise ValueError("Version must be a Mechanical release tag such as 261.")
    return result


@lru_cache(maxsize=16)
def _binary_digest(path: str, size: int, modified_ns: int) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _runtime_scope(version: str) -> dict[str, Any]:
    from .config import discover_installation, known_paths
    installation = discover_installation()
    if installation.root is None or installation.version_tag != version:
        return {"installation_fingerprint": "unresolved", "version": version, "current_installation": False}
    digest = hashlib.sha256((str(installation.root.resolve()).casefold() + "\0" + version).encode("utf-8"))
    binaries = [known_paths(installation.root)["mechanical"],
                installation.root / "aisol" / "bin" / "winx64" / "AnsysWBU.exe",
                installation.root / "dpf" / "bin" / "winx64" / "Ans.Dpf.Grpc.exe",
                installation.root / "aisol" / "bin" / "winx64" / "Ans.Dpf.Grpc.exe"]
    for binary in dict.fromkeys(binaries):
        if binary.is_file():
            stat = binary.stat()
            digest.update(_binary_digest(str(binary), stat.st_size, stat.st_mtime_ns).encode("ascii"))
    return {"installation_fingerprint": digest.hexdigest(), "version": version, "current_installation": True}


def _evidence_file(version: str, fingerprint: str) -> Path | None:
    from .config import evidence_root, require_allowed_path
    try:
        return require_allowed_path(evidence_root() / ("v" + version) / (fingerprint + ".jsonl"), expect_file=True)
    except (OSError, ValueError):
        return None


def capability_evidence_status(version: str) -> dict[str, Any]:
    """Report the resolved evidence store without granting validation stages."""
    from .config import ConfigurationError, evidence_root
    configured = any(str(name).casefold() == "ansys_mcp_evidence_root" for name in os.environ)
    try:
        root = evidence_root()
    except ConfigurationError as exc:
        return {"ok": False, "status": "configuration_error", "error": str(exc),
                "configured_by_environment": configured}
    scope = _runtime_scope(version)
    directory = root / ("v" + version)
    target = directory / (scope["installation_fingerprint"] + ".jsonl")
    records: dict[str, Any] = {"total": 0, "by_product": {}, "by_stage": {}, "unparsed_lines": 0}
    if target.is_file():
        try:
            with target.open("rb") as stream:
                for line in stream:
                    try:
                        observation = json.loads(line)
                    except (ValueError, TypeError):
                        records["unparsed_lines"] += 1
                        continue
                    if not isinstance(observation, dict):
                        records["unparsed_lines"] += 1
                        continue
                    records["total"] += 1
                    product = str(observation.get("product"))
                    stage = str(observation.get("validation_stage"))
                    records["by_product"][product] = records["by_product"].get(product, 0) + 1
                    records["by_stage"][stage] = records["by_stage"].get(stage, 0) + 1
        except OSError as exc:
            return {"ok": False, "status": "unreadable", "error": str(exc), "root": str(root),
                    "configured_by_environment": configured}
    try:
        other_files = sorted(path.name for path in directory.glob("*.jsonl") if path != target) if directory.is_dir() else []
    except OSError:
        other_files = []
    diagnostics = []
    if not scope["current_installation"]:
        diagnostics.append({"code": "EVIDENCE_INSTALLATION_MISMATCH",
                            "message": "Evidence scope does not match the discovered installation; historical records are not applied."})
    if not target.is_file() and other_files:
        diagnostics.append({"code": "EVIDENCE_FINGERPRINT_MISSING",
                            "message": "Evidence exists only for another installation fingerprint."})
    return {"ok": True, "status": "ready", "root": str(root), "directory": str(directory),
            "file": str(target), "exists": target.is_file(), "records": records,
            "other_fingerprint_files": other_files,
            "installation_fingerprint": scope["installation_fingerprint"],
            "current_installation": scope["current_installation"],
            "configured_by_environment": configured, "diagnostics": diagnostics}


def _load_current_evidence(version: str, scope: dict[str, Any]) -> None:
    fingerprint = scope["installation_fingerprint"]
    target = _evidence_file(version, fingerprint)
    if target is None:
        return
    load_key = str(target), fingerprint
    with _LOCK:
        try:
            file_stat = target.stat()
        except OSError:
            # A reader can run before the first actor writes its evidence.
            # Absence and transient access failures must not become permanent.
            _LOADED_EVIDENCE.pop(load_key, None)
            return
        signature = file_stat.st_mtime_ns, file_stat.st_size
        if _LOADED_EVIDENCE.get(load_key) == signature:
            return
        if not stat.S_ISREG(file_stat.st_mode):
            _LOADED_EVIDENCE.pop(load_key, None)
            return
        # Private evidence can be truncated by a killed process. A malformed
        # final line is ignored; it never grants a validation stage.
        try:
            with target.open("rb") as stream:
                for line in stream:
                    try:
                        observation = json.loads(line)
                        if not isinstance(observation, dict):
                            continue
                        if (observation.get("version") != version or observation.get("installation_fingerprint") != fingerprint
                                or observation.get("validation_stage") not in VALIDATION_STAGES
                                or type(observation.get("runtime_reachable")) is not bool
                                or observation.get("product") not in {"mechanical", "dpf"}
                                or not isinstance(observation.get("qualified_name"), str)
                                or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.]*", observation["qualified_name"])
                                or not isinstance(observation.get("observed_at"), str)):
                            continue
                        observed_at = datetime.fromisoformat(observation["observed_at"].replace("Z", "+00:00"))
                        if observed_at.tzinfo is None:
                            observed_at = observed_at.replace(tzinfo=timezone.utc)
                        observation["observed_at"] = observed_at.astimezone(timezone.utc).isoformat()
                        if not observation["runtime_reachable"] and observation["validation_stage"] != "catalogued":
                            continue
                        observation["status"] = "unavailable" if not observation["runtime_reachable"] else ("verified" if observation["validation_stage"] in {"workflow", "solve", "result"} else "reachable")
                        key = observation["product"], version, observation.get("backend", "unspecified"), fingerprint, observation["qualified_name"]
                        previous = _EVIDENCE.get(key)
                        if previous is None or observation["observed_at"] > previous["observed_at"]:
                            _EVIDENCE[key] = observation
                    except (ValueError, KeyError, TypeError):
                        continue
            # Cache the pre-read signature. If another actor appended during
            # this read, the next query notices its newer signature and reloads.
            _LOADED_EVIDENCE[load_key] = signature
        except OSError:
            _LOADED_EVIDENCE.pop(load_key, None)


def _package_root(version: str) -> tuple[Path, str]:
    distribution = importlib.metadata.distribution(STUB_PACKAGE)
    # Distribution metadata avoids importing .NET-looking namespace stubs.
    relative = Path("ansys") / "mechanical" / "stubs" / ("v" + version)
    root = Path(distribution.locate_file(relative))
    if not root.is_dir():
        raise FileNotFoundError("The installed official stub package does not contain v" + version + ".")
    return root, distribution.version


def _annotation(node: ast.AST | None) -> str | None:
    return ast.unparse(node) if node is not None else None


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    prefix = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
    return prefix + node.name + "(" + ast.unparse(node.args) + ")" + (" -> " + ast.unparse(node.returns) if node.returns else "")


def _public(name: str, *, class_member: bool = False) -> bool:
    return not name.startswith("_") or (class_member and name == "__init__")


def _targets(node: ast.AST):
    if isinstance(node, ast.Name):
        yield node.id
    elif isinstance(node, (ast.Tuple, ast.List)):
        for child in node.elts:
            yield from _targets(child)


def _components(qualified_name: str) -> list[str]:
    lowered = qualified_name.casefold()
    tags = [name for name, tokens in COMPONENT_RULES.items() if any(token in lowered for token in tokens)]
    return tags or ["model"]


def _doc_url(version: str, module: str, owner: str, name: str, kind: str) -> str:
    stem = module.replace(".", "/")
    if owner:
        stem += "/" + owner.split(".")[0] + ".html"
        return MECHANICAL_SOURCE + "/v" + version + "/" + stem + "#" + owner + "." + name
    if kind == "type":
        stem += "/" + name + ".html"
    else:
        stem += "/index.html"
    return MECHANICAL_SOURCE + "/v" + version + "/" + stem


def _declarations(statements: list[ast.stmt], module: str, owner: str = ""):
    for node in statements:
        if isinstance(node, ast.ClassDef):
            if not _public(node.name):
                continue
            yield {"name": node.name, "kind": "type", "node": node, "owner": owner,
                   "bases": [_annotation(base) for base in node.bases],
                   "signature": "class " + node.name + "(" + ", ".join(ast.unparse(base) for base in node.bases) + ")"}
            yield from _declarations(node.body, module, ".".join(filter(None, (owner, node.name))))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not _public(node.name, class_member=bool(owner)):
                continue
            decorators = [ast.unparse(decorator) for decorator in node.decorator_list]
            kind = "constructor" if node.name == "__init__" else ("property" if "property" in decorators or any(value.endswith(".setter") for value in decorators) else ("method" if owner else "function"))
            yield {"name": node.name, "kind": kind, "node": node, "owner": owner,
                   "signature": _signature(node), "annotation": _annotation(node.returns), "decorators": decorators}
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                for name in _targets(target):
                    if _public(name):
                        yield {"name": name, "kind": "attribute" if owner else "constant", "node": node, "owner": owner,
                               "signature": name + (": " + ast.unparse(node.annotation) if isinstance(node, ast.AnnAssign) else ""),
                               "annotation": _annotation(node.annotation) if isinstance(node, ast.AnnAssign) else None}
        elif not owner and isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                target = alias.name if isinstance(node, ast.Import) else ("." * node.level + (node.module or "") + "." + alias.name)
                if "ansys.mechanical.stubs." not in target and not target.startswith("Ansys"):
                    continue
                name = alias.asname or alias.name.split(".")[0]
                if _public(name) and name != "*":
                    yield {"name": name, "kind": "reexport", "node": node, "owner": owner, "signature": target, "target": target}
        elif isinstance(node, (ast.If, ast.Try, ast.With)):
            # Some supported releases guard public bindings with TYPE_CHECKING.
            yield from _declarations(node.body, module, owner)
            if hasattr(node, "orelse"):
                yield from _declarations(node.orelse, module, owner)
            if isinstance(node, ast.Try):
                for handler in node.handlers:
                    yield from _declarations(handler.body, module, owner)
                yield from _declarations(node.finalbody, module, owner)


def _build(root: Path, version: str, distribution_version: str) -> dict[str, Any]:
    paths = sorted(path for path in root.rglob("*") if path.is_file() and path.suffix in {".py", ".pyi"})
    if not paths:
        raise ValueError("Official stub source contains no Python API files.")
    entries: dict[tuple[str, str], dict[str, Any]] = {}
    files = []
    source_digest = hashlib.sha256()
    declaration_total = 0
    for path in paths:
        relative = path.relative_to(root).as_posix()
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        source_digest.update((relative + "\0" + digest + "\n").encode("utf-8"))
        tree = ast.parse(raw.decode("utf-8-sig"), filename=relative)
        parts = list(path.relative_to(root).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        module = ".".join(parts)
        module_qname = module or ("v" + version)
        module_key = (module_qname, "module")
        if module_key not in entries:
            entries[module_key] = {"id": "mechanical:v" + version + ":module:" + module_qname,
                "product": "mechanical", "version": version, "qualified_name": module_qname, "name": parts[-1] if parts else "v" + version,
                "kind": "module", "owner": None, "components": _components(module_qname), "signatures": [],
                "public_source": MECHANICAL_SOURCE + "/v" + version + "/" + module.replace(".", "/") + "/index.html",
                "source": {"distribution": STUB_PACKAGE, "distribution_version": distribution_version, "file": relative, "line": 1, "sha256": digest},
                "declarations": [], "runtime_reachable": None, "validation_stage": "catalogued", "status": "catalogued",
                "unavailable_reason": "Runtime has not been probed."}
        count = 0
        for declaration in _declarations(tree.body, module):
            count += 1
            name, kind, owner = declaration["name"], declaration["kind"], declaration["owner"]
            qualified = ".".join(filter(None, (module, owner, name)))
            key = (qualified, kind)
            if key not in entries:
                node = declaration["node"]
                doc = ast.get_docstring(node) if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) else None
                entries[key] = {"id": "mechanical:v" + version + ":" + kind + ":" + qualified,
                    "product": "mechanical", "version": version, "qualified_name": qualified, "name": name,
                    "kind": kind, "owner": ".".join(filter(None, (module, owner))) or None,
                    "components": _components(qualified), "signatures": [], "bases": declaration.get("bases", []),
                    "description": " ".join((doc or "").split())[:400],
                    "public_source": _doc_url(version, module, owner, name, kind),
                    "source": {"distribution": STUB_PACKAGE, "distribution_version": distribution_version,
                               "file": relative, "line": node.lineno, "sha256": digest},
                    "declarations": [], "runtime_reachable": None, "validation_stage": "catalogued", "status": "catalogued",
                    "unavailable_reason": "Runtime has not been probed."}
                if "target" in declaration:
                    entries[key]["target"] = declaration["target"]
            entry = entries[key]
            if declaration["signature"] not in entry["signatures"]:
                entry["signatures"].append(declaration["signature"])
            entry["declarations"].append({"file": relative, "line": declaration["node"].lineno})
        declaration_total += count
        files.append({"file": relative, "sha256": digest, "public_declarations": count})
    ordered = sorted(entries.values(), key=lambda entry: (entry["qualified_name"], entry["kind"]))
    accounted = sum(len(entry["declarations"]) for entry in ordered)
    if accounted != declaration_total:
        raise AssertionError("Public declaration accounting mismatch.")
    return {"product": "mechanical", "version": version, "distribution": STUB_PACKAGE,
            "distribution_version": distribution_version, "manifest": {"source_files": len(paths), "parsed_files": len(files),
            "public_declarations": declaration_total, "accounted_declarations": accounted, "unique_entries": len(ordered),
            "kind_counts": dict(sorted(Counter(entry["kind"] for entry in ordered).items())),
            "source_tree_sha256": source_digest.hexdigest(), "complete_for_installed_public_stubs": True,
            "coverage_boundary": "All public source declarations and Ansys reexports; inherited members are represented by declaring types and bases. GUI-only and undocumented APIs are outside the source inventory.",
            "files": files}, "entries": ordered}


@lru_cache(maxsize=4)
def _installed_catalog(version: str, package_version: str) -> dict[str, Any]:
    root, actual_version = _package_root(version)
    return _build(root, version, actual_version)


def build_catalog(version: str | None = None, *, stubs_root: Path | None = None) -> dict[str, Any]:
    """Build the full inventory. Explicit roots are useful for offline audits/tests."""
    tag = _version(version)
    if stubs_root is not None:
        return _build(Path(stubs_root), tag, "explicit-source")
    package_version = importlib.metadata.version(STUB_PACKAGE)
    return _installed_catalog(tag, package_version)


def record_runtime_evidence(evidence: dict[str, Any], *, evidence_path: Path | None = None) -> dict[str, Any]:
    """Record observation facts, never infer successful solve/results from stubs.

    The caller must make the product observation. Only a compact label (not a
    path, stdout, hostname or URL) is accepted as the evidence source.
    """
    product = str(evidence.get("product", "mechanical")).casefold()
    if product not in {"mechanical", "dpf"}:
        raise ValueError("Runtime evidence product must be mechanical or dpf.")
    version = _version(evidence.get("version"))
    qualified = str(evidence.get("qualified_name", ""))
    prefix = "ansys.mechanical.stubs.v" + version + "."
    if qualified.startswith(prefix):
        qualified = qualified[len(prefix):]
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.]*", qualified):
        raise ValueError("Runtime evidence requires a public qualified API name.")
    reachable = evidence.get("runtime_reachable")
    if not isinstance(reachable, bool):
        raise ValueError("Runtime evidence requires an observed boolean runtime_reachable.")
    stage = str(evidence.get("validation_stage", "reachable"))
    if stage not in VALIDATION_STAGES:
        raise ValueError("Unknown validation stage.")
    if not reachable and stage != "catalogued":
        raise ValueError("An unavailable API cannot have a successful validation stage.")
    source = str(evidence.get("evidence_source", "runtime_probe"))
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", source):
        raise ValueError("Evidence source must be a non-sensitive label, not a path or log.")
    backend = str(evidence.get("backend", "unspecified"))
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", backend):
        raise ValueError("Backend must be a compact identifier.")
    scope = _runtime_scope(version)
    _load_current_evidence(version, scope)
    reason = evidence.get("unavailable_reason")
    # Keep a category here; original exception and product logs belong to the private run.
    if reason is not None and not re.fullmatch(r"[A-Za-z0-9 _.,:()-]{1,256}", str(reason)):
        reason = "Product operation unavailable; inspect private runtime diagnostics."
    observed_at = str(evidence.get("observed_at") or datetime.now(timezone.utc).isoformat())
    # Timestamp strings must not become a route for private log content.
    timestamp = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    observed_at = timestamp.astimezone(timezone.utc).isoformat()
    observation = {"product": product, "version": version, "qualified_name": qualified,
                   "runtime_reachable": reachable, "validation_stage": stage,
                   "status": "unavailable" if not reachable else ("verified" if stage in {"workflow", "solve", "result"} else "reachable"),
                   "unavailable_reason": reason if not reachable else None, "evidence_source": source,
                   "observed_at": observed_at, "backend": backend, **scope}
    for key in ("run_id", "case_id"):
        if evidence.get(key) is not None:
            if not isinstance(evidence[key], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", evidence[key]):
                raise ValueError("Invalid evidence provenance identifier.")
            observation[key] = evidence[key]
    if evidence.get("receipt_sha256") is not None:
        if not isinstance(evidence["receipt_sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", evidence["receipt_sha256"]):
            raise ValueError("Invalid evidence receipt digest.")
        observation["receipt_sha256"] = evidence["receipt_sha256"]
    verified_format = evidence.get("verified_format")
    if verified_format is not None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", str(verified_format)):
            raise ValueError("A verified format must be a compact format identifier.")
        observation["verified_formats"] = [str(verified_format)]
    with _LOCK:
        key = product, version, backend, scope["installation_fingerprint"], qualified
        old = _EVIDENCE.get(key)
        if old and old.get("verified_formats"):
            observation["verified_formats"] = sorted(set(old["verified_formats"]) | set(observation.get("verified_formats", [])))
        if old and reachable and old["runtime_reachable"] and VALIDATION_STAGES.index(old["validation_stage"]) > VALIDATION_STAGES.index(stage):
            observation["highest_validated_stage"] = old.get("highest_validated_stage", old["validation_stage"])
            observation["last_observation_stage"] = stage
            observation["validation_stage"] = observation["highest_validated_stage"]
            observation["status"] = "verified" if observation["validation_stage"] in {"workflow", "solve", "result"} else "reachable"
        else:
            observation["highest_validated_stage"] = stage
        _EVIDENCE[key] = observation
        target = _evidence_file(version, scope["installation_fingerprint"])
        if target is not None:
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(observation, ensure_ascii=True) + "\n")
                observation["persistence"] = "local_private_evidence"
            except OSError:
                observation["persistence"] = "process_memory_only"
        else:
            observation["persistence"] = "process_memory_only"
    if evidence_path is not None:
        from .config import require_allowed_path
        target = require_allowed_path(evidence_path, expect_file=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(observation, indent=2, ensure_ascii=True), encoding="utf-8")
    return copy.deepcopy(observation)


def _overlay(entry: dict[str, Any], scope: dict[str, Any] | None = None) -> dict[str, Any]:
    result = entry.copy()
    if scope is None:
        scope = _runtime_scope(entry["version"])
        _load_current_evidence(entry["version"], scope)
    with _LOCK:
        observations = [value for key, value in _EVIDENCE.items()
                        if key[0] == entry["product"] and key[1] == entry["version"]
                        and key[3] == scope["installation_fingerprint"] and key[4] == entry["qualified_name"]]
        observations = copy.deepcopy(observations)
        for observation in observations:
            if observation.get("receipt_sha256"):
                from .acceptance_evidence import receipt_provenance_available
                if not receipt_provenance_available(observation):
                    observation.update(validation_stage="catalogued", status="evidence_unavailable",
                                       unavailable_reason="Imported receipt artifacts are unavailable or changed.")
        if observations:
            # Preserve per-backend failures; a failure in one channel must not
            # erase actual evidence from a different channel.
            reachable = [value for value in observations if value["runtime_reachable"]]
            selected = max(reachable, key=lambda value: (VALIDATION_STAGES.index(value["validation_stage"]), value["observed_at"])) if reachable else max(observations, key=lambda value: value["observed_at"])
            result.update(selected)
            result["runtime_observations"] = copy.deepcopy(observations)
    return result


def _catalog_entries_with_runtime(catalog: dict[str, Any], scope: dict[str, Any]) -> list[dict[str, Any]]:
    entries = list(catalog["entries"])
    known = {entry["qualified_name"] for entry in entries}
    with _LOCK:
        extras = sorted({key[4] for key in _EVIDENCE if key[0] == "mechanical" and key[1] == catalog["version"]
                         and key[3] == scope["installation_fingerprint"] and key[4] not in known})
    for qualified in extras:
        owner, _, name = qualified.rpartition(".")
        entries.append({"id": "mechanical:v" + catalog["version"] + ":runtime_member:" + qualified,
            "product": "mechanical", "version": catalog["version"], "qualified_name": qualified, "name": name,
            "kind": "runtime_member", "owner": owner or None, "components": _components(qualified),
            "signatures": [], "bases": [], "description": "Public product API observed at runtime; not declared by the installed stub source.",
            "public_source": "runtime_public_api_observation", "source": {"kind": "runtime", "stub_declaration": False},
            "declarations": [], "runtime_reachable": None, "validation_stage": "catalogued", "status": "catalogued",
            "unavailable_reason": "Runtime observation must be matched to the current installation."})
    return sorted(entries, key=lambda entry: (entry["qualified_name"], entry["kind"]))


def capability_catalog_summary(version: str | None = None) -> dict[str, Any]:
    try:
        catalog = build_catalog(version)
    except (importlib.metadata.PackageNotFoundError, FileNotFoundError) as exc:
        return {"ok": False, "product": "mechanical", "version": _version(version), "status": "dependency_unavailable",
                "diagnostics": [{"code": "STUB_CATALOG_UNAVAILABLE", "message": str(exc)}]}
    scope = _runtime_scope(catalog["version"])
    _load_current_evidence(catalog["version"], scope)
    entries = _catalog_entries_with_runtime(catalog, scope)
    observations = [_overlay(entry, scope) for entry in entries]
    components = []
    for component in ["model", *COMPONENT_RULES]:
        matching = [entry for entry in entries if component in entry["components"]]
        observed = [entry for entry in observations if component in entry["components"]]
        components.append({"component": component, "public_entries": len(matching),
                           "runtime_reachable_entries": sum(entry["runtime_reachable"] is True for entry in observed),
                           "validation_stage_counts": dict(Counter(entry["validation_stage"] for entry in observed)),
                           "unavailable_reason": None if matching else "No public declarations for this component were found in the installed stubs; external extension or GUI support must be probed separately."})
    manifest = catalog["manifest"].copy()
    manifest.pop("files", None)
    manifest["runtime_only_entries"] = len(entries) - len(catalog["entries"])
    return {"ok": True, "product": "mechanical", "version": catalog["version"],
            "distribution_version": catalog["distribution_version"], "manifest": manifest, "components": components,
            "evidence_scope": {**scope, "storage": "private .ansys_mcp/capability_evidence",
                               "backend_specific": True, "live_session_liveness": "Session summaries, not historical evidence, determine current process liveness."},
            "gui_only": {"status": "gap", "runtime_reachable": None,
                         "unavailable_reason": "GUI-only features require a separate adapter and are not claimed by the API catalog."}}


def query_capabilities(product: str = "mechanical", version: str | None = None, component: str | None = None,
                       status: str | None = None, offset: int = 0, limit: int = 100, kind: str = "api") -> dict[str, Any]:
    if offset < 0 or not 1 <= limit <= 1000:
        raise ValueError("offset must be nonnegative and limit must be between 1 and 1000.")
    product = product.casefold()
    if kind not in {"api", "workflow"}:
        raise ValueError("Capability kind must be api or workflow.")
    if product not in {"mechanical", "dpf", "fluent", "cfx"}:
        raise ValueError("Product must be mechanical, dpf, fluent or cfx.")
    if kind == "workflow":
        from .acceptance_evidence import query_workflow_evidence
        return query_workflow_evidence(product, _version(version), component, status, offset, limit)
    if product in {"fluent", "cfx"}:
        from .cfd_catalog import query_cfd_catalog
        return query_cfd_catalog(product, _version(version), component, status, offset, limit)
    if product == "dpf":
        from .dpf import dpf_runtime_info
        records = dpf_runtime_info()["formats"]
        for record in records:
            record.update({"product": "dpf", "version": _version(version), "status": "catalogued", "qualified_name": "readers." + record["component"]})
            record.update(_overlay(record))
            record["file_read_verified"] = record["validation_stage"] in {"object", "workflow", "solve", "result"}
            record.setdefault("verified_formats", [])
            record["verification_scope"] = "Only listed verified_formats have been opened; other documented formats remain unverified."
        if component:
            records = [record for record in records if record["component"] == component.casefold()]
        if status:
            records = [record for record in records if status in {record["status"], record["validation_stage"]}]
        return {"ok": True, "product": product, "version": _version(version), "total": len(records),
                "offset": offset, "limit": limit, "items": records[offset:offset + limit]}
    if product != "mechanical":
        raise ValueError("Product must be mechanical or dpf.")
    try:
        catalog = build_catalog(version)
    except (importlib.metadata.PackageNotFoundError, FileNotFoundError) as exc:
        return {"ok": False, "product": product, "version": _version(version), "total": 0,
                "offset": offset, "limit": limit, "items": [], "status": "dependency_unavailable",
                "diagnostics": [{"code": "STUB_CATALOG_UNAVAILABLE", "message": str(exc)}]}
    selected = _COMPONENT_ALIASES.get((component or "").casefold(), (component or "").casefold())
    scope = _runtime_scope(catalog["version"])
    _load_current_evidence(catalog["version"], scope)
    matches = []
    entries = _catalog_entries_with_runtime(catalog, scope)
    for entry in entries:
        if selected and selected not in entry["components"]:
            continue
        overlaid = _overlay(entry, scope)
        if status and status not in {overlaid["status"], overlaid["validation_stage"]}:
            continue
        matches.append(overlaid)
    manifest = catalog["manifest"].copy()
    manifest.pop("files", None)
    manifest["runtime_only_entries"] = len(entries) - len(catalog["entries"])
    return {"ok": True, "product": product, "version": catalog["version"], "total": len(matches),
            "offset": offset, "limit": limit, "next_offset": offset + limit if offset + limit < len(matches) else None,
            "items": copy.deepcopy(matches[offset:offset + limit]), "manifest": manifest,
            "evidence_scope": scope}
