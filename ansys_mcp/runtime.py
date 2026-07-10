from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from .config import known_paths


def ansys_python_executable() -> Path:
    """Return the discovered Ansys Python path without silently falling back."""
    return known_paths()["ansys_python"]


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
