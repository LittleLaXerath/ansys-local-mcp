from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from .config import ansys_env, known_paths


def process_result(proc: subprocess.CompletedProcess[str], args: list[str], workdir: Path) -> dict[str, Any]:
    return {
        "returncode": proc.returncode,
        "stdout": proc.stdout[-20000:],
        "stderr": proc.stderr[-20000:],
        "command": args,
        "workdir": str(workdir),
    }


def run(args: list[str], workdir: Path, timeout_seconds: int) -> dict[str, Any]:
    proc = subprocess.run(
        args,
        cwd=str(workdir),
        env=ansys_env(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout_seconds,
    )
    return process_result(proc, args, workdir)


def run_batch(batch_file: Path, extra_args: list[str], workdir: Path, timeout_seconds: int) -> dict[str, Any]:
    return run(["cmd.exe", "/d", "/c", str(batch_file), *extra_args], workdir, timeout_seconds)


def launch(args: list[str], workdir: Path) -> dict[str, Any]:
    proc = subprocess.Popen(args, cwd=str(workdir), env=ansys_env())
    return {"pid": proc.pid, "command": args, "workdir": str(workdir)}


def ansys_python_executable() -> Path:
    exe = known_paths()["ansys_python"]
    return exe if exe.exists() else Path(sys.executable)


def signature_info(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"path": str(path), "exists": False}
    command = (
        "$p = $env:TARGET_PATH; "
        "$sig = Get-AuthenticodeSignature -LiteralPath $p; "
        "$hash = Get-FileHash -LiteralPath $p -Algorithm SHA256; "
        "[PSCustomObject]@{"
        "Path=$p; Status=[string]$sig.Status; StatusMessage=$sig.StatusMessage; "
        "Signer=if($sig.SignerCertificate){$sig.SignerCertificate.Subject}else{''}; "
        "SHA256=$hash.Hash"
        "} | ConvertTo-Json -Compress"
    )
    env = os.environ.copy()
    env["TARGET_PATH"] = str(path)
    proc = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", command],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        timeout=30,
    )
    if proc.returncode != 0:
        return {"path": str(path), "exists": True, "error": proc.stderr.strip()}
    return json.loads(proc.stdout)
