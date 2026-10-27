"""Conservative, opt-in retention of logs belonging to finished workflows."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import time
from pathlib import Path
from typing import Any

from .config import default_workdir, require_allowed_path
from .process_supervisor import process_identity_matches


_FINISHED = {"complete", "failed", "timeout", "cancelled"}


def _plain(path: Path, *, directory: bool = False) -> bool:
    info = path.lstat()
    return (not path.is_symlink() and not getattr(info, "st_file_attributes", 0) & 0x400
            and (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)))


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _record(path: Path) -> dict[str, Any]:
    if not _plain(path):
        raise ValueError("Linked or non-regular manifest.")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Invalid retention manifest.")
    return value


def _has_session(value: Any) -> bool:
    if isinstance(value, dict):
        session = value.get("session")
        if isinstance(session, dict) and session.get("id"):
            closed = session.get("closed") is True or session.get("state", session.get("status")) == "closed"
            if not closed or any(process_identity_matches(item) for item in session.get("owned_processes", [])):
                return True
        if value.get("pending_observations"):
            return True
        return any(_has_session(child) for child in value.values())
    return isinstance(value, list) and any(_has_session(child) for child in value)


def _finished(record: dict[str, Any]) -> bool:
    if record.get("status") not in _FINISHED or _has_session(record):
        return False
    identities = [record.get("worker"), *record.get("owned_processes", [])]
    return not any(process_identity_matches(identity) for identity in identities if identity)


def retention_plan(workdir: Path, *, older_than_days: float = 30, now: float | None = None) -> dict[str, Any]:
    if not 0 <= older_than_days < float("inf"):
        raise ValueError("Retention age must be finite and nonnegative.")
    root = require_allowed_path(workdir, must_exist=True, expect_file=False)
    private = root / ".ansys_mcp"
    candidates, retained = [], []
    cutoff = (time.time() if now is None else now) - older_than_days * 86400
    if not private.exists():
        return {"ok": True, "dry_run": True, "workdir": str(root), "older_than_days": older_than_days, "candidates": [], "retained": [], "bytes": 0}
    if not _plain(private, directory=True):
        raise ValueError("Linked retention directory is not supported.")
    # Unsettled or live jobs can reference a completed run while reconciling it.
    jobs = private / "jobs"
    protected_runs: set[Path] = set()
    if jobs.exists():
        if not _plain(jobs, directory=True):
            raise ValueError("Linked job catalog is not supported.")
        for directory in jobs.iterdir():
            if not directory.is_dir():
                continue
            try:
                if not _plain(directory, directory=True):
                    raise ValueError("Linked job.")
                record = _record(directory / "job.json")
                if not _finished(record):
                    result_file = directory / "result.json"
                    if result_file.exists():
                        run_path = _record(result_file).get("run_dir")
                        if run_path:
                            protected_runs.add(require_allowed_path(run_path, base=root))
            except (OSError, ValueError, TypeError):
                # Unreadable ownership cannot safely establish that any run is idle.
                return {"ok": True, "dry_run": True, "workdir": str(root), "older_than_days": older_than_days,
                        "candidates": [], "retained": [{"path": str(directory), "reason": "job_ownership_unavailable"}], "bytes": 0}
    for group, manifest_name in (("runs", "manifest.json"), ("jobs", "job.json")):
        catalog = private / group
        if not catalog.exists():
            continue
        if not _plain(catalog, directory=True):
            raise ValueError("Linked retention catalog is not supported.")
        for directory in sorted(catalog.iterdir()):
            if not directory.is_dir():
                continue
            try:
                if not _plain(directory, directory=True):
                    raise ValueError("Linked run.")
                require_allowed_path(directory, must_exist=True, expect_file=False)
                manifest = directory / manifest_name
                record = _record(manifest)
                if directory.resolve() in protected_runs or not _finished(record):
                    retained.append({"path": str(directory), "reason": "active_or_unsettled"})
                    continue
                if manifest.stat().st_mtime > cutoff:
                    retained.append({"path": str(directory), "reason": "recent_manifest"})
                    continue
                manifest_digest = _digest(manifest)
                for path in sorted(directory.iterdir()):
                    # No recursive deletion. Never select bootstrap credentials,
                    # artifacts, native projects, result files or evidence stores.
                    if not path.name.endswith((".stdout.log", ".stderr.log")) or not _plain(path):
                        continue
                    require_allowed_path(path, must_exist=True, expect_file=True)
                    info = path.stat()
                    if info.st_mtime > cutoff or info.st_nlink != 1:
                        continue
                    candidates.append({"path": str(path), "manifest": str(manifest), "manifest_sha256": manifest_digest,
                                       "sha256": _digest(path), "size": info.st_size, "mtime_ns": info.st_mtime_ns,
                                       "device": info.st_dev, "inode": info.st_ino, "reason": "finished_workflow_log"})
            except (OSError, ValueError, TypeError):
                retained.append({"path": str(directory), "reason": "record_unavailable"})
    return {"ok": True, "dry_run": True, "workdir": str(root), "older_than_days": older_than_days,
            "candidates": candidates, "retained": retained, "bytes": sum(item["size"] for item in candidates)}


def apply_retention(plan: dict[str, Any]) -> dict[str, Any]:
    # Regenerate the allowlist rather than trusting file paths supplied by a caller.
    current = retention_plan(Path(plan["workdir"]), older_than_days=plan["older_than_days"])
    available = {item["path"]: item for item in current["candidates"]}
    deleted, skipped = [], []
    for original in plan.get("candidates", []):
        path = original.get("path")
        if original != available.get(path):
            skipped.append({"path": path, "reason": "candidate_changed"})
            continue
        try:
            target = require_allowed_path(path, must_exist=True, expect_file=True)
            manifest = require_allowed_path(original["manifest"], must_exist=True, expect_file=True)
            if (not _plain(target) or not _finished(_record(manifest))
                    or _digest(manifest) != original["manifest_sha256"] or _digest(target) != original["sha256"]):
                raise ValueError("Candidate changed during revalidation.")
            info = target.stat()
            if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_nlink) != (
                    original["device"], original["inode"], original["size"], original["mtime_ns"], 1):
                raise ValueError("Candidate identity changed.")
            target.unlink()
            deleted.append(path)
        except (OSError, ValueError):
            skipped.append({"path": path, "reason": "revalidation_failed"})
    return {"ok": not skipped, "dry_run": False, "deleted": deleted, "skipped": skipped}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", type=Path, default=default_workdir())
    parser.add_argument("--older-than-days", type=float, default=30)
    parser.add_argument("--apply", action="store_true", help="Delete only freshly revalidated log candidates")
    args = parser.parse_args()
    plan = retention_plan(args.workdir, older_than_days=args.older_than_days)
    print(json.dumps(apply_retention(plan) if args.apply else plan, indent=2))


if __name__ == "__main__":
    main()
