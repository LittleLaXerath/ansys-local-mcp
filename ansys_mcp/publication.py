"""Read-only checks for files, distribution archives and new commit metadata.

Findings intentionally contain locations and categories, never matching values.
Machine-specific fingerprints are supplied from a private, untracked file.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import tarfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Iterable


_PRIVATE_PARTS = {".git", ".venv", ".ansys_mcp", "__pycache__", ".pytest_cache", "logs", ".private"}
_PRIVATE_COMPOUND_SUFFIXES = (".mechdb.bak", ".mechdat.bak", ".wbpj.bak", ".cas.h5", ".dat.h5")
_PRIVATE_SUFFIXES = {".pyc", ".pyo", ".log", ".pem", ".key", ".pfx", ".p12", ".wbpj", ".wbpz", ".mechdb", ".mechdat", ".scdoc", ".scdocx", ".step", ".stp", ".rst", ".rth", ".d3plot", ".binout", ".ad", ".adres", ".agdb", ".db", ".cdb", ".mode", ".msh", ".cas", ".dat", ".def", ".gtm", ".res", ".out", ".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".pdf", ".exe", ".dll", ".pyd"}
_PLACEHOLDER_USERS = {"your-name", "your_name", "username", "user", "example", "public", "default", "<user>", "<username>", "<your-name>"}
_PROFILE = re.compile(r"(?i)(?:[a-z]:/users/|(?<![:/\w])/home/|(?<![:/\w])/users/)([a-z0-9_.@-]+|<[^>]+>)")
_PRIVATE_URL = re.compile(r"https?://(?:10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+|172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+)(?::\d+)?", re.I)
_SECRET = re.compile(r'''(?ix)\b(?:api[_-]?key|auth[_-]?key|access[_-]?token|client[_-]?secret|shared[_-]?secret|password)["']?\s*[:=]\s*["']([^"'\n]{8,})["']''')
_TOKEN = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|AKIA[A-Z0-9]{16})\b")


def _normal(value: str) -> str:
    return re.sub(r"\\+", "/", value).casefold()


def _safe_location(name: str, fingerprints: Iterable[str]) -> str:
    result = name
    for value in fingerprints:
        if value:
            for variant in {value, value.replace("\\", "/"), value.replace("\\", "\\\\")}:
                result = re.sub(re.escape(variant), "<redacted>", result, flags=re.I)
    result = _TOKEN.sub("<redacted>", result)
    return "<private-path>" if _PROFILE.search(_normal(result)) else result


def _private_name(name: str) -> bool:
    path = PurePosixPath(name.replace("\\", "/"))
    return (any(part.casefold() in _PRIVATE_PARTS for part in path.parts)
            or path.name == ".env" or path.name.startswith(".env.")
            or path.suffix.casefold() in _PRIVATE_SUFFIXES
            or path.name.casefold().endswith(_PRIVATE_COMPOUND_SUFFIXES)
            or path.name.casefold() in {"mcp_client_config.json", "private_fingerprints.json"})


def scan_bytes(name: str, data: bytes, fingerprints: Iterable[str] = (), *, metadata: bool = False) -> list[dict]:
    fingerprints = tuple(fingerprints)
    name = _safe_location(name, fingerprints)
    findings = []
    if _private_name(name):
        findings.append({"path": name, "category": "private_artifact"})
    # Fingerprints are also checked against binary metadata using common encodings.
    for fingerprint in fingerprints:
        if not fingerprint:
            continue
        variants = {fingerprint, fingerprint.replace("\\", "/"), fingerprint.replace("\\", "\\\\")}
        if any(value.encode(encoding, errors="ignore") in data for value in variants for encoding in ("utf-8", "utf-16-le")):
            findings.append({"path": name, "category": "machine_fingerprint"})
            break
    try:
        content = data.decode("utf-8-sig")
    except UnicodeError:
        if not metadata:
            findings.append({"path": name, "category": "binary_artifact"})
        return findings
    if b"\x00" in data and not metadata:
        findings.append({"path": name, "category": "binary_artifact"})
    for number, line in enumerate(content.splitlines(), 1):
        normalized = _normal(line)
        categories = set()
        if any(_normal(value) in normalized for value in fingerprints if value):
            categories.add("machine_fingerprint")
        if any(match.group(1).casefold() not in _PLACEHOLDER_USERS for match in _PROFILE.finditer(normalized)):
            categories.add("personal_profile_path")
        if _PRIVATE_URL.search(line):
            categories.add("private_service_url")
        if _TOKEN.search(line):
            categories.add("credential_pattern")
        for match in _SECRET.finditer(line):
            value = match.group(1).casefold()
            if not any(marker in value for marker in ("example", "placeholder", "your-", "your_", "<", "${", "dummy", "test-", "fake-")):
                categories.add("credential_literal")
        if re.fullmatch(r"-{5}BEGIN (?:[A-Z ]+ )?PRIVATE KEY-{5}", line.strip()):
            categories.add("private_key_material")
        findings.extend({"path": name, "line": number, "category": category} for category in sorted(categories))
    return findings


def tracked_files(root: Path, staged: bool = False) -> list[str]:
    args = ["git", "-c", "safe.directory=" + str(root), "-C", str(root)]
    args += ["diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"] if staged else ["ls-files", "-z"]
    return subprocess.check_output(args).decode("utf-8").split("\0")[:-1]


def scan_tree(root: Path, fingerprints: Iterable[str] = (), staged: bool = False) -> dict:
    root = root.resolve()
    fingerprints = tuple(fingerprints)
    findings = []
    files = tracked_files(root, staged)
    for name in files:
        path = root / name
        location = _safe_location(name, fingerprints)
        filename_findings = scan_bytes(location, name.encode(), fingerprints, metadata=True)
        findings.extend(filename_findings)
        if staged:
            # The index, not an optional/changed worktree file, is the publish
            # candidate. Git blob IDs also avoid interpreting pathspec syntax.
            record = subprocess.check_output(["git", "-c", "safe.directory=" + str(root), "-C", str(root), "ls-files", "--stage", "-z", "--", ":(literal)" + name])
            mode, oid, stage = record.split(b"\t", 1)[0].split()
            if mode in {b"120000", b"160000"}:
                findings.append({"path": location, "category": "publication_link"})
                continue
            if stage != b"0":
                findings.append({"path": location, "category": "unmerged_index_entry"})
                continue
            data = subprocess.check_output(["git", "-c", "safe.directory=" + str(root), "-C", str(root), "cat-file", "blob", oid.decode("ascii")])
            findings.extend(scan_bytes(name, data, fingerprints))
        elif path.is_symlink():
            findings.append({"path": location, "category": "publication_symlink"})
        elif path.is_file():
            findings.extend(scan_bytes(name, path.read_bytes(), fingerprints))
    return {"ok": not findings, "files_checked": len(files), "findings": findings}


def _gzip_metadata(path: Path) -> bytes:
    with path.open("rb") as stream:
        header = stream.read(1024 * 1024)
    if len(header) < 10 or header[:2] != b"\x1f\x8b":
        return b""
    flags, position, values = header[3], 10, []
    if flags & 4:
        length = int.from_bytes(header[position:position+2], "little")
        values.append(header[position+2:position+2+length])
        position += 2+length
    for mask in (8, 16):
        if flags & mask:
            end = header.find(b"\0", position)
            if end < 0:
                raise ValueError("Unterminated distribution gzip metadata.")
            values.append(header[position:end])
            position = end+1
    return b"\n".join(values)


def scan_archive(path: Path, fingerprints: Iterable[str] = ()) -> dict:
    findings, count = [], 0
    fingerprints = tuple(fingerprints)
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            findings.extend(scan_bytes("archive/comment", archive.comment, fingerprints, metadata=True))
            for member in archive.infolist():
                if PurePosixPath(member.filename).is_absolute() or re.match(r"^[A-Za-z]:", member.filename) or ".." in PurePosixPath(member.filename).parts:
                    findings.append({"path": member.filename, "category": "unsafe_archive_member"})
                findings.extend(scan_bytes("archive/member-name", member.filename.encode(), fingerprints))
                findings.extend(scan_bytes("archive/member-metadata", member.comment + member.extra, fingerprints, metadata=True))
                if (member.external_attr >> 16) & 0o170000 == 0o120000:
                    findings.append({"path": member.filename, "category": "archive_link"})
                if not member.is_dir():
                    count += 1
                    findings.extend(scan_bytes(member.filename, archive.read(member), fingerprints))
    elif tarfile.is_tarfile(path):
        findings.extend(scan_bytes("archive/gzip-header", _gzip_metadata(path), fingerprints, metadata=True))
        with tarfile.open(path) as archive:
            findings.extend(scan_bytes("archive/global-pax", json.dumps(archive.pax_headers).encode(), fingerprints, metadata=True))
            for member in archive:
                if PurePosixPath(member.name).is_absolute() or re.match(r"^[A-Za-z]:", member.name) or ".." in PurePosixPath(member.name).parts:
                    findings.append({"path": member.name, "category": "unsafe_archive_member"})
                findings.extend(scan_bytes("archive/member-name", member.name.encode(), fingerprints))
                findings.extend(scan_bytes("archive/member-metadata", json.dumps(member.pax_headers).encode(), fingerprints, metadata=True))
                if member.uname not in {"", "root"} or member.gname not in {"", "root"}:
                    findings.append({"path": _safe_location(member.name, fingerprints), "category": "personal_archive_owner"})
                if member.isfile():
                    count += 1
                    stream = archive.extractfile(member)
                    if stream:
                        findings.extend(scan_bytes(member.name, stream.read(), fingerprints))
                elif member.issym() or member.islnk():
                    findings.append({"path": member.name, "category": "archive_link"})
    else:
        findings.append({"path": path.name, "category": "unsupported_distribution_archive"})
    for item in findings:
        item["path"] = _safe_location(item["path"], fingerprints)
    return {"ok": not findings, "archive": _safe_location(path.name, fingerprints), "files_checked": count, "findings": findings}


def scan_commit(root: Path, commit: str, expected_email: str, fingerprints: Iterable[str] = (), expected_name: str | None = None) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_./@{}~^+-]+", commit) or commit.startswith("-"):
        raise ValueError("Invalid commit reference")
    fields = subprocess.check_output(["git", "-c", "safe.directory=" + str(root), "-C", str(root), "show", "-s", "--format=%an%x00%ae%x00%cn%x00%ce%x00%B", commit]).decode("utf-8").split("\0", 4)
    findings = []
    for role, name, email in (("author",fields[0],fields[1]),("committer",fields[2],fields[3])):
        if email.strip() != expected_email or not expected_email.endswith("@users.noreply.github.com"):
            findings.append({"path": "commit/" + role, "category": "non_noreply_identity"})
        if expected_name is not None and name.strip() != expected_name:
            findings.append({"path": "commit/" + role, "category": "unexpected_public_identity"})
    findings.extend(scan_bytes("commit/message", fields[4].encode(), fingerprints))
    raw_commit = subprocess.check_output(["git", "-c", "safe.directory=" + str(root), "-C", str(root), "cat-file", "commit", commit])
    findings.extend(scan_bytes("commit/headers", raw_commit.partition(b"\n\n")[0], fingerprints))
    for line in fields[4].splitlines():
        if re.match(r"(?i)co-authored-by:", line) and "@users.noreply.github.com>" not in line:
            findings.append({"path": "commit/message", "category": "personal_coauthor_identity"})
    return {"ok": not findings, "findings": findings}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--fingerprints-file", type=Path)
    parser.add_argument("--staged", action="store_true")
    parser.add_argument("--artifact", type=Path, action="append", default=[])
    parser.add_argument("--commit")
    parser.add_argument("--expected-email")
    parser.add_argument("--expected-name")
    args = parser.parse_args()
    fingerprints = []
    if args.fingerprints_file:
        payload = json.loads(args.fingerprints_file.read_text(encoding="utf-8-sig"))
        fingerprints = payload["values"] if isinstance(payload, dict) else payload
        if not isinstance(fingerprints, list) or not all(isinstance(value, str) and value for value in fingerprints):
            parser.error("Fingerprint file must contain non-empty string values")
    results = {"tree": scan_tree(args.root, fingerprints, args.staged), "archives": [scan_archive(path, fingerprints) for path in args.artifact]}
    if args.commit:
        if not args.expected_email:
            parser.error("--commit requires --expected-email")
        results["commit"] = scan_commit(args.root, args.commit, args.expected_email, fingerprints, args.expected_name)
    results["ok"] = results["tree"]["ok"] and all(item["ok"] for item in results["archives"]) and results.get("commit", {"ok": True})["ok"]
    print(json.dumps(results, indent=2, ensure_ascii=True))
    raise SystemExit(0 if results["ok"] else 1)


if __name__ == "__main__":
    main()
