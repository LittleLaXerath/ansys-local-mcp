"""Opt-in export verification for an already solved Mechanical benchmark.

The input project and its sidecar are copied to an isolated run folder before
opening. No solve, source overwrite, GUI automation, or external upload occurs.
ffprobe and ffmpeg are discovered on PATH only for independent media validation.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import shutil
import struct
import subprocess
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ansys_mcp.config import require_allowed_file, require_allowed_path
from ansys_mcp.capabilities import record_runtime_evidence
from ansys_mcp.mechanical import run_mechanical_recipe
from ansys_mcp.mechanical_schema import MechanicalRecipe
from tools.run_product_acceptance import _observe_mechanical, _wrapper, _write_json, mechanical_nodal_values


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def tree_manifest(directory: Path) -> dict[str, str]:
    directory = require_allowed_path(directory, must_exist=True, expect_file=False)
    result = {}
    for path in sorted(directory.rglob("*")):
        require_allowed_path(path, must_exist=True)
        if path.is_symlink():
            raise ValueError("Acceptance does not follow project sidecar symlinks.")
        if path.is_file():
            result[path.relative_to(directory).as_posix()] = digest_file(path)
    return result


def png_metadata(path: Path) -> dict[str, Any]:
    """Validate PNG chunk structure and CRCs without an image-library dependency."""
    raw = path.read_bytes()
    if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("PNG signature is invalid.")
    position = 8
    dimensions = None
    image_data = False
    ended = False
    while position < len(raw):
        if position + 12 > len(raw):
            raise ValueError("PNG chunk is truncated.")
        length = struct.unpack_from(">I", raw, position)[0]
        kind = raw[position + 4:position + 8]
        end = position + 12 + length
        if end > len(raw):
            raise ValueError("PNG chunk data is truncated.")
        payload = raw[position + 8:position + 8 + length]
        actual_crc = struct.unpack_from(">I", raw, position + 8 + length)[0]
        if zlib.crc32(kind + payload) & 0xFFFFFFFF != actual_crc:
            raise ValueError("PNG chunk CRC is invalid.")
        if kind == b"IHDR":
            if dimensions is not None or position != 8 or len(payload) != 13:
                raise ValueError("PNG header is invalid.")
            width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", payload)
            if compression != 0 or filtering != 0 or interlace not in {0, 1} or color not in {0, 2, 3, 4, 6}:
                raise ValueError("PNG image encoding fields are invalid.")
            dimensions = {"width": width, "height": height, "bit_depth": depth, "color_type": color}
        elif kind == b"IDAT":
            image_data = True
        elif kind == b"IEND":
            if length or end != len(raw):
                raise ValueError("PNG end marker is invalid.")
            ended = True
            break
        position = end
    if dimensions is None or not image_data or not ended or min(dimensions["width"], dimensions["height"]) <= 0:
        raise ValueError("PNG does not contain a complete image.")
    return {"format": "PNG", **dimensions, "size_bytes": len(raw), "sha256": digest_file(path)}


def probe_media(path: Path) -> dict[str, Any]:
    ffprobe = shutil.which("ffprobe")
    ffmpeg = shutil.which("ffmpeg")
    if not ffprobe or not ffmpeg:
        raise RuntimeError("Independent animation validation requires ffprobe and ffmpeg on PATH.")
    result = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-count_frames",
        "-show_entries", "stream=codec_name,width,height,nb_read_frames,nb_frames,duration,r_frame_rate:format=format_name,duration",
        "-of", "json", str(path)], capture_output=True, text=True, timeout=60, check=True)
    data = json.loads(result.stdout)
    if len(data.get("streams", [])) != 1:
        raise ValueError("Expected one decodable video stream.")
    stream = data["streams"][0]
    decoded = subprocess.run([ffmpeg, "-v", "error", "-i", str(path), "-f", "null", "-"],
                             capture_output=True, text=True, timeout=60)
    if decoded.returncode != 0 or decoded.stderr.strip():
        raise ValueError("Animation did not decode completely without errors.")
    duration = float(stream.get("duration") or data.get("format", {}).get("duration") or 0)
    frames = int(stream.get("nb_read_frames") or stream.get("nb_frames") or 0)
    return {"codec": stream.get("codec_name"), "container": data.get("format", {}).get("format_name"),
            "width": int(stream["width"]), "height": int(stream["height"]), "frames": frames,
            "duration_s": duration, "decoded_all_frames": True, "size_bytes": path.stat().st_size,
            "sha256": digest_file(path)}


def validate(directory: Path, report: dict[str, Any], parameters: dict[str, Any]) -> dict[str, Any]:
    paths = {name: require_allowed_file(report[name]) for name in ("png_path", "table_path")}
    requested_animation = report.get("animation_path") or directory / ("deformation." + parameters.get("animation_format", "GIF").lower())
    paths["animation_path"] = require_allowed_path(requested_animation, expect_file=True)
    if any(path.parent != directory.resolve() for path in paths.values()):
        raise ValueError("A native export was not created in the isolated run directory.")
    image = png_metadata(paths["png_path"])
    nodes = mechanical_nodal_values(paths["table_path"])
    scale = float(report["table_length_scale_m"])
    if not math.isfinite(scale) or scale <= 0:
        raise ValueError("Result table unit conversion is not positive and finite.")
    nodes = {node: value * scale for node, value in nodes.items()}
    csv_path = directory / "deformation.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["node_id", "total_deformation_m"])
        writer.writerows(sorted(nodes.items()))
    configured_size = (parameters["width"], parameters["height"])
    checks = {"previously_solved": "done" in report.get("solution_status_before", "").lower(),
              "no_solve": report.get("solve_called") is False,
              "native_exports_completed": report.get("status") == "exported" and report.get("active_phase") == "complete",
              "png_dimensions": (image["width"], image["height"]) == configured_size,
              "csv_nodes": len(nodes) > 0,
              "table_unit_readback": abs(max(nodes.values()) / report["maximum_deformation_m"] - 1) <= 0.001,
              "source_project_unchanged": digest_file(Path(parameters["source_project"])) == parameters["source_sha256"]}
    try:
        animation = probe_media(require_allowed_file(paths["animation_path"]))
        configured = parameters.get("animation_options", "configured") == "configured"
        options = report.get("animation_option_readback", {})
        checks.update({"animation_dimensions": (animation["width"], animation["height"]) == configured_size if configured else min(animation["width"], animation["height"]) >= 64,
                       "animation_frames": 2 <= animation["frames"] <= max(1000, int(options.get("frames", parameters["frames"])) * max(1, report.get("result_sets", 1)) * 2),
                       "animation_duration": 0 < animation["duration_s"] <= max(30, float(options.get("duration_s", parameters["duration_s"])) * max(1, report.get("result_sets", 1)) * 2),
                       "animation_decoded": animation["decoded_all_frames"],
                       "animation_format": animation["codec"] == "gif" if report["animation_format"] == "GIF" else "mp4" in str(animation["container"])})
    except Exception as error:
        animation = {"status": "not_verified", "error": str(error), "exception_type": type(error).__name__}
        checks["animation_decoded"] = False
    if "source_sidecar_manifest" in parameters:
        checks["source_sidecar_unchanged"] = tree_manifest(Path(parameters["source_sidecar"])) == parameters["source_sidecar_manifest"]
    return {"ok": all(checks.values()), "checks": checks, "png": image, "animation": animation,
            "table": {"rows": len(nodes), "unit": "m", "csv_size_bytes": csv_path.stat().st_size, "csv_sha256": digest_file(csv_path)}}


def prepare(project: Path, directory: Path, root: Path, *, system_name: str, animation_format: str, elevation: str,
            analysis_name: str = "Acceptance Modal", animation_options: str = "minimal") -> tuple[dict[str, Any], dict[str, Any]]:
    source = require_allowed_file(project, base=root)
    if source.suffix.casefold() != ".wbpj":
        raise ValueError("This fixture expects a saved Workbench .wbpj project.")
    sidecar = require_allowed_path(source.with_name(source.stem + "_files"), must_exist=True, expect_file=False)
    manifest = tree_manifest(sidecar)
    copy_root = directory / "project"
    copy_root.mkdir()
    clone = copy_root / source.name
    shutil.copyfile(source, clone)
    shutil.copytree(sidecar, clone.with_name(clone.stem + "_files"))
    parameters = {"output_dir": str(directory), "source_project": str(source), "source_sha256": digest_file(source),
                  "source_sidecar": str(sidecar), "source_sidecar_manifest": manifest,
                  "analysis_name": analysis_name, "animation_format": animation_format, "animation_options": animation_options,
                  "width": 800, "height": 600, "frames": 24, "duration_s": 2}
    _write_json(directory / "parameters.json", parameters)
    snapshot = directory / "exports.source.py"
    shutil.copyfile(root / "examples" / "scripts" / "mechanical_result_exports.py", snapshot)
    script = _wrapper(snapshot, parameters, directory / "exports.py")
    recipe = {"version": "1", "backend": "workbench", "mode": "persistent", "launch": {"elevation": elevation},
              "project": {"open_path": str(clone), "system_name": system_name},
              "operations": [{"id": "exports", "op": "script", "script_path": str(script)}]}
    MechanicalRecipe.model_validate(recipe)
    _write_json(directory / "recipe.json", recipe)
    return recipe, parameters


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", help="An allowed, already solved acceptance .wbpj project.")
    parser.add_argument("--system-name", default="SYS")
    parser.add_argument("--analysis-name", default="Acceptance Modal")
    parser.add_argument("--animation-format", choices=("GIF", "MP4"), default="GIF")
    parser.add_argument("--animation-options", choices=("minimal", "configured"), default="minimal")
    parser.add_argument("--elevation", choices=("auto", "never", "require"), default="auto")
    parser.add_argument("--output-dir")
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    directory = require_allowed_path(args.output_dir or root / ".ansys_mcp" / "result_exports" / stamp, base=root, expect_file=False)
    if args.validate_only:
        report = json.loads((directory / "exports.checkpoint.json").read_text(encoding="utf-8"))
        parameters = json.loads((directory / "parameters.json").read_text(encoding="utf-8"))
        result = validate(directory, report, parameters)
        _write_json(directory / "validation.json", result)
        print(json.dumps(result))
        return 0 if result["ok"] else 1
    if not args.project:
        parser.error("--project is required unless --validate-only is selected.")
    directory.mkdir(parents=True, exist_ok=False)
    recipe, parameters = prepare(Path(args.project), directory, root, system_name=args.system_name,
                                 animation_format=args.animation_format, elevation=args.elevation,
                                 analysis_name=args.analysis_name, animation_options=args.animation_options)
    if args.prepare_only:
        print(json.dumps({"ok": True, "status": "prepared", "product_launched": False, "output_dir": str(directory)}))
        return 0
    run_dir = directory / "mechanical_run"
    run_dir.mkdir()
    result = _observe_mechanical(recipe, root, run_dir, args.timeout)
    _write_json(directory / "mechanical.envelope.json", result)
    session_id = result.get("outputs", {}).get("session", {}).get("id")
    try:
        if not result.get("ok"):
            print(json.dumps({"ok": False, "phase": "mechanical", "status": result.get("status"), "output_dir": str(directory), "diagnostics": result.get("diagnostics")}))
            return 1
        report = result["outputs"]["values"]["exports"]
        _write_json(directory / "exports.report.json", report)
        try:
            validation = validate(directory, report, parameters)
        except Exception as error:
            validation = {"ok": False, "status": "validation_failed", "diagnostics": [{"code": "RESULT_EXPORT_VALIDATION_FAILED", "message": str(error), "exception_type": type(error).__name__}]}
        _write_json(directory / "validation.json", validation)
        if validation["ok"]:
            version = result["outputs"]["session"]["version"]
            for qualified in (report["graphics_type"] + ".ExportImage", report["result_type"] + ".ExportAnimation",
                              report["result_type"] + ".ExportToTextFile"):
                try:
                    record_runtime_evidence({"product": "mechanical", "version": version, "backend": "workbench",
                        "qualified_name": qualified, "runtime_reachable": True, "validation_stage": "result",
                        "evidence_source": "product_result_export_trusted_script"})
                except (OSError, ValueError, TypeError):
                    validation.setdefault("warnings", []).append("An export was verified, but its capability evidence was not persisted.")
        print(json.dumps({"output_dir": str(directory), **validation}))
        return 0 if validation["ok"] else 1
    finally:
        if session_id and result.get("status") != "unknown":
            closed = run_mechanical_recipe({"backend": "workbench", "launch": {"elevation": args.elevation},
                "session": {"action": "close", "id": session_id}}, root, directory / "close", 60)
            _write_json(directory / "closed.json", closed)


if __name__ == "__main__":
    raise SystemExit(main())
