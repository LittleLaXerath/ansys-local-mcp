from __future__ import annotations

import os
from pathlib import Path


ANSYS_ROOT = Path(os.environ.get("ANSYS_MCP_ROOT", r"C:\Program Files\ANSYS Inc\v261"))
VERSION_TAG = os.environ.get("ANSYS_MCP_VERSION_TAG", "261")


def existing_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.exists():
        raise ValueError(f"Path does not exist: {path}")
    return path.resolve()


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def allowed_roots() -> list[Path]:
    roots: list[Path] = []
    configured = os.environ.get("ANSYS_MCP_ALLOWED_ROOTS")
    if configured:
        roots.extend(Path(p).expanduser() for p in configured.split(";") if p.strip())
    else:
        roots.extend(
            [
                Path.cwd(),
                Path.home() / "Documents",
                Path.home() / "Desktop",
                Path.home() / "Downloads",
            ]
        )
    return [p.resolve() for p in roots if p.exists()]


def require_allowed_path(
    path_value: str | Path,
    *,
    must_exist: bool = False,
    expect_file: bool | None = None,
) -> Path:
    path = Path(path_value).expanduser()
    if must_exist and not path.exists():
        raise ValueError(f"Path does not exist: {path}")

    resolved = path.resolve(strict=must_exist)
    roots = allowed_roots()
    if roots and not any(is_relative_to(resolved, root) or resolved == root for root in roots):
        allowed = "; ".join(str(root) for root in roots)
        raise ValueError(f"Path is outside ANSYS_MCP_ALLOWED_ROOTS: {resolved}. Allowed: {allowed}")

    if expect_file is True and resolved.exists() and not resolved.is_file():
        raise ValueError(f"Expected a file: {resolved}")
    if expect_file is False and resolved.exists() and not resolved.is_dir():
        raise ValueError(f"Expected a directory: {resolved}")
    return resolved


def require_allowed_file(path_value: str | Path) -> Path:
    return require_allowed_path(path_value, must_exist=True, expect_file=True)


def safe_workdir(workdir: str | None, fallback: Path) -> Path:
    path = require_allowed_path(workdir or fallback, must_exist=False, expect_file=False)
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_workdir() -> Path:
    roots = allowed_roots()
    return roots[0] if roots else Path.cwd()


def known_paths() -> dict[str, Path]:
    root = ANSYS_ROOT
    return {
        "ansys_root": root,
        "runwb2": root / "Framework" / "bin" / "Win64" / "RunWB2.exe",
        "runwb2_bat": root / "Framework" / "bin" / "Win64" / "runwb2.bat",
        "mechanical": root / "aisol" / "bin" / "winx64" / "AnsysWBU.exe",
        "ansys_mapdl": root / "ansys" / "bin" / "winx64" / "ANSYS.exe",
        "mapdl_launcher": root / "ansys" / "bin" / "winx64" / "MAPDL.exe",
        "fluent": root / "fluent" / "ntbin" / "win64" / "fluent.exe",
        "cfx_pre": root / "CFX" / "bin" / "cfx5pre.exe",
        "cfx_solve": root / "CFX" / "bin" / "cfx5solve.exe",
        "cfx_post": root / "CFX" / "bin" / "cfx5post.exe",
        "cfd_post": root / "CFD-Post" / "bin" / "cfx5post.exe",
        "spaceclaim": root / "scdm" / "SpaceClaim.exe",
        "system_coupling": root / "SystemCoupling" / "bin" / "systemcoupling.bat",
        "ansys_python": root
        / "commonfiles"
        / "CPython"
        / "3_10"
        / "winx64"
        / "Release"
        / "python"
        / "python.exe",
        "pyfluent_core": root
        / "commonfiles"
        / "CPython"
        / "3_10"
        / "winx64"
        / "Release"
        / "Ansys"
        / "PyFluentCore",
        "electronics_desktop": root / "AnsysEM" / "ansysedt.exe",
    }


def ansys_env() -> dict[str, str]:
    env = os.environ.copy()
    root = str(ANSYS_ROOT)
    env[f"AWP_ROOT{VERSION_TAG}"] = root
    env[f"ANSYS{VERSION_TAG}_DIR"] = str(ANSYS_ROOT / "ANSYS")
    env[f"ICEMCFD_ROOT{VERSION_TAG}"] = str(ANSYS_ROOT / "icemcfd")
    env[f"CADOE_LIBDIR{VERSION_TAG}"] = str(ANSYS_ROOT / "CommonFiles" / "Language" / "en-us")
    env["ANSYS_SYSDIR"] = "winx64"
    env["ANSYS_MCP_ROOT"] = str(ANSYS_ROOT)
    env["ANSYS_MCP_VERSION_TAG"] = VERSION_TAG
    env["ANSYS_MCP_ALLOWED_ROOTS"] = ";".join(str(path) for path in allowed_roots())

    path_parts = [
        ANSYS_ROOT / "Framework" / "bin" / "Win64",
        ANSYS_ROOT / "ansys" / "bin" / "winx64",
        ANSYS_ROOT / "fluent" / "ntbin" / "win64",
        ANSYS_ROOT / "CFX" / "bin",
        ANSYS_ROOT / "CFD-Post" / "bin",
        ANSYS_ROOT / "scdm",
        ANSYS_ROOT / "licensingclient" / "winx64",
    ]
    env["PATH"] = ";".join(str(p) for p in path_parts if p.exists()) + ";" + env.get("PATH", "")
    return env
