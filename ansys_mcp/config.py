from __future__ import annotations

import ctypes
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Mapping


STARTUP_CWD = Path.cwd().resolve()
DEFAULT_VERSION_TAG = "261"
_VERSION_RE = re.compile(r"^v?(\d{3,4})$", re.IGNORECASE)
_AWP_ROOT_RE = re.compile(r"^AWP_ROOT(\d{3,4})$", re.IGNORECASE)


class ConfigurationError(ValueError):
    """Raised when an explicit MCP safety or installation setting is invalid."""


@dataclass(frozen=True)
class InstallationResolution:
    root: Path | None
    version_tag: str
    source: str
    invalid_candidates: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class AllowedRootsResolution:
    roots: tuple[Path, ...]
    source: str
    invalid: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return bool(self.roots) and not self.invalid


def configured_version_tag(environ: Mapping[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    return str(env.get("ANSYS_MCP_VERSION_TAG") or DEFAULT_VERSION_TAG).lstrip("vV")


def infer_version_tag(path: Path, fallback: str | None = None) -> str:
    match = _VERSION_RE.match(path.name)
    return match.group(1) if match else (fallback or DEFAULT_VERSION_TAG)


def discover_installation(environ: Mapping[str, str] | None = None) -> InstallationResolution:
    if environ is None:
        snapshot = tuple(
            sorted(
                (str(key), str(value))
                for key, value in os.environ.items()
                if str(key).upper().startswith("AWP_ROOT")
                or str(key).upper() in {"ANSYS_MCP_ROOT", "ANSYS_MCP_VERSION_TAG"}
            )
        )
        return _discover_installation_cached(snapshot)
    return _discover_installation(environ)


@lru_cache(maxsize=8)
def _discover_installation_cached(snapshot: tuple[tuple[str, str], ...]) -> InstallationResolution:
    return _discover_installation(dict(snapshot))


def _discover_installation(env: Mapping[str, str]) -> InstallationResolution:
    requested_tag = configured_version_tag(env)
    invalid: list[str] = []

    explicit = env.get("ANSYS_MCP_ROOT")
    if explicit:
        root = Path(explicit).expanduser().resolve(strict=False)
        if _is_install_root(root):
            return InstallationResolution(root, infer_version_tag(root, requested_tag), "ANSYS_MCP_ROOT")
        return InstallationResolution(
            None,
            requested_tag,
            "ANSYS_MCP_ROOT_INVALID",
            (str(root),),
            ("Explicit ANSYS_MCP_ROOT is invalid; automatic fallback is disabled.",),
        )

    exact_key = f"AWP_ROOT{requested_tag}"
    exact = _env_get_case_insensitive(env, exact_key)
    if exact:
        root = Path(exact).expanduser().resolve(strict=False)
        if _is_install_root(root):
            return InstallationResolution(root, requested_tag, exact_key)
        invalid.append(str(root))

    env_candidates: list[tuple[str, Path, str]] = []
    for key, value in env.items():
        match = _AWP_ROOT_RE.match(str(key))
        if not match or not value:
            continue
        tag = match.group(1)
        root = Path(str(value)).expanduser().resolve(strict=False)
        if _is_install_root(root):
            env_candidates.append((tag, root, str(key)))
        else:
            invalid.append(str(root))
    selected = _select_candidate(env_candidates, requested_tag)
    if selected:
        tag, root, source = selected
        return InstallationResolution(root, tag, source, tuple(dict.fromkeys(invalid)))

    registry_candidates = list(_registry_install_candidates())
    selected = _select_candidate(registry_candidates, requested_tag)
    if selected:
        tag, root, source = selected
        return InstallationResolution(root, tag, source, tuple(dict.fromkeys(invalid)))

    disk_candidates = list(_disk_install_candidates())
    selected = _select_candidate(disk_candidates, requested_tag)
    if selected:
        tag, root, source = selected
        return InstallationResolution(root, tag, source, tuple(dict.fromkeys(invalid)))

    return InstallationResolution(
        None,
        requested_tag,
        "not_found",
        tuple(dict.fromkeys(invalid)),
        ("No usable Ansys installation was discovered.",),
    )


def allowed_roots_info(environ: Mapping[str, str] | None = None) -> AllowedRootsResolution:
    env = os.environ if environ is None else environ
    configured = env.get("ANSYS_MCP_ALLOWED_ROOTS")
    if not configured:
        return AllowedRootsResolution(
            roots=(STARTUP_CWD,),
            source="startup_cwd",
            warnings=("ANSYS_MCP_ALLOWED_ROOTS is unset; only the server startup directory is allowed.",),
        )

    roots: list[Path] = []
    invalid: list[str] = []
    for raw in configured.split(";"):
        value = raw.strip()
        if not value:
            continue
        path = Path(value).expanduser().resolve(strict=False)
        if path.exists() and path.is_dir():
            roots.append(path)
        else:
            invalid.append(str(path))
    return AllowedRootsResolution(
        roots=tuple(dict.fromkeys(roots)),
        source="ANSYS_MCP_ALLOWED_ROOTS",
        invalid=tuple(dict.fromkeys(invalid)),
    )


def allowed_roots() -> list[Path]:
    info = allowed_roots_info()
    if not info.valid:
        details = "; ".join(info.invalid) if info.invalid else "no valid directories"
        raise ConfigurationError(f"Invalid ANSYS_MCP_ALLOWED_ROOTS: {details}")
    return list(info.roots)


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def resolve_path(path_value: str | Path, base: Path | None = None, *, strict: bool = False) -> Path:
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        path = (base or STARTUP_CWD) / path
    return path.resolve(strict=strict)


def require_allowed_path(
    path_value: str | Path,
    *,
    base: Path | None = None,
    must_exist: bool = False,
    expect_file: bool | None = None,
) -> Path:
    roots = allowed_roots()
    path = resolve_path(path_value, base, strict=must_exist)
    if not any(path == root or is_relative_to(path, root) for root in roots):
        allowed = "; ".join(str(root) for root in roots)
        raise ValueError(f"Path is outside ANSYS_MCP_ALLOWED_ROOTS: {path}. Allowed: {allowed}")
    if must_exist and not path.exists():
        raise ValueError(f"Path does not exist: {path}")
    if expect_file is True and path.exists() and not path.is_file():
        raise ValueError(f"Expected a file: {path}")
    if expect_file is False and path.exists() and not path.is_dir():
        raise ValueError(f"Expected a directory: {path}")
    return path


def require_allowed_file(path_value: str | Path, *, base: Path | None = None) -> Path:
    return require_allowed_path(path_value, base=base, must_exist=True, expect_file=True)


def safe_workdir(workdir: str | Path | None, fallback: Path) -> Path:
    path = require_allowed_path(workdir or fallback, base=fallback, must_exist=False, expect_file=False)
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_workdir() -> Path:
    info = allowed_roots_info()
    if info.valid:
        return info.roots[0]
    return STARTUP_CWD


def known_paths(root: Path | None = None) -> dict[str, Path]:
    resolution = discover_installation()
    selected_root = root or resolution.root or Path(r"C:\Program Files\ANSYS Inc") / f"v{resolution.version_tag}"
    python_base = selected_root / "commonfiles" / "CPython"
    python_version = _discover_bundled_python_version(python_base)
    python_release = python_base / python_version / "winx64" / "Release"
    return {
        "ansys_root": selected_root,
        "runwb2": selected_root / "Framework" / "bin" / "Win64" / "RunWB2.exe",
        "runwb2_bat": selected_root / "Framework" / "bin" / "Win64" / "runwb2.bat",
        "mechanical": selected_root / "aisol" / "bin" / "winx64" / "AnsysWBU.exe",
        "ansys_mapdl": selected_root / "ansys" / "bin" / "winx64" / "ANSYS.exe",
        "mapdl_launcher": selected_root / "ansys" / "bin" / "winx64" / "MAPDL.exe",
        "fluent": selected_root / "fluent" / "ntbin" / "win64" / "fluent.exe",
        "cfx_pre": selected_root / "CFX" / "bin" / "cfx5pre.exe",
        "cfx_solve": selected_root / "CFX" / "bin" / "cfx5solve.exe",
        "cfx_post": selected_root / "CFX" / "bin" / "cfx5post.exe",
        "cfd_post": selected_root / "CFD-Post" / "bin" / "cfx5post.exe",
        "spaceclaim": selected_root / "scdm" / "SpaceClaim.exe",
        "system_coupling": selected_root / "SystemCoupling" / "bin" / "systemcoupling.bat",
        "ansys_python": python_release / "python" / "python.exe",
        "pyfluent_core": python_release / "Ansys" / "PyFluentCore",
        "electronics_desktop": selected_root / "AnsysEM" / "ansysedt.exe",
    }


def ansys_env() -> dict[str, str]:
    env = os.environ.copy()
    resolution = discover_installation()
    if resolution.root is None:
        return env
    root = resolution.root
    tag = resolution.version_tag
    env[f"AWP_ROOT{tag}"] = str(root)
    env[f"ANSYS{tag}_DIR"] = str(root / "ANSYS")
    env[f"ICEMCFD_ROOT{tag}"] = str(root / "icemcfd")
    env[f"CADOE_LIBDIR{tag}"] = str(root / "CommonFiles" / "Language" / "en-us")
    env["ANSYS_SYSDIR"] = "winx64"
    env["ANSYS_MCP_ROOT"] = str(root)
    env["ANSYS_MCP_VERSION_TAG"] = tag
    roots_info = allowed_roots_info()
    if roots_info.valid:
        env["ANSYS_MCP_ALLOWED_ROOTS"] = ";".join(str(path) for path in roots_info.roots)
    path_parts = [
        root / "Framework" / "bin" / "Win64",
        root / "ansys" / "bin" / "winx64",
        root / "fluent" / "ntbin" / "win64",
        root / "CFX" / "bin",
        root / "CFD-Post" / "bin",
        root / "scdm",
        root / "licensingclient" / "winx64",
    ]
    env["PATH"] = ";".join(str(path) for path in path_parts if path.exists()) + ";" + env.get("PATH", "")
    return env


def inline_code_allowed(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return str(env.get("ANSYS_MCP_ALLOW_INLINE_CODE", "")).strip().lower() in {"1", "true", "yes", "on"}


def _env_get_case_insensitive(environ: Mapping[str, str], key: str) -> str | None:
    wanted = key.casefold()
    for name, value in environ.items():
        if str(name).casefold() == wanted:
            return str(value)
    return None


def _is_install_root(path: Path) -> bool:
    if not path.exists() or not path.is_dir():
        return False
    sentinels = (
        path / "Framework" / "bin" / "Win64" / "RunWB2.exe",
        path / "fluent" / "ntbin" / "win64" / "fluent.exe",
        path / "CFX" / "bin" / "cfx5pre.exe",
        path / "aisol" / "bin" / "winx64" / "AnsysWBU.exe",
        path / "scdm" / "SpaceClaim.exe",
    )
    return any(candidate.is_file() for candidate in sentinels)


def _select_candidate(
    candidates: Iterable[tuple[str, Path, str]], requested_tag: str | None
) -> tuple[str, Path, str] | None:
    unique: dict[tuple[str, str], tuple[str, Path, str]] = {}
    for tag, root, source in candidates:
        if _is_install_root(root):
            unique[(tag, str(root).casefold())] = (tag, root, source)
    values = list(unique.values())
    if requested_tag:
        exact = [candidate for candidate in values if candidate[0] == requested_tag]
        if exact:
            return sorted(exact, key=lambda item: str(item[1]).casefold())[0]
    if not values:
        return None
    return max(values, key=lambda item: (int(item[0]), str(item[1]).casefold()))


def _registry_install_candidates() -> Iterable[tuple[str, Path, str]]:
    if os.name != "nt":
        return ()
    try:
        import winreg
    except ImportError:
        return ()

    candidates: list[tuple[str, Path, str]] = []
    roots = ((winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\ANSYS, Inc."),)
    for hive, key_path in roots:
        try:
            with winreg.OpenKey(hive, key_path) as root_key:
                index = 0
                while True:
                    try:
                        child = winreg.EnumKey(root_key, index)
                    except OSError:
                        break
                    index += 1
                    match = _VERSION_RE.match(child)
                    if not match:
                        continue
                    tag = match.group(1)
                    child_path = key_path + "\\" + child
                    for base_path in _registry_base_paths(winreg, hive, child_path, depth=3):
                        install_root = _root_from_product_path(base_path, tag)
                        if install_root:
                            candidates.append((tag, install_root, f"registry:{child_path}"))
        except OSError:
            continue
    return tuple(candidates)


def _registry_base_paths(winreg: object, hive: object, key_path: str, depth: int) -> Iterable[Path]:
    if depth < 0:
        return ()
    values: list[Path] = []
    try:
        with winreg.OpenKey(hive, key_path) as key:  # type: ignore[attr-defined]
            value_index = 0
            while True:
                try:
                    name, value, _ = winreg.EnumValue(key, value_index)  # type: ignore[attr-defined]
                except OSError:
                    break
                value_index += 1
                if name.casefold() in {"basepath", "installdir", "installpath"} and isinstance(value, str):
                    values.append(Path(value).expanduser().resolve(strict=False))
            child_index = 0
            while depth > 0:
                try:
                    child = winreg.EnumKey(key, child_index)  # type: ignore[attr-defined]
                except OSError:
                    break
                child_index += 1
                values.extend(_registry_base_paths(winreg, hive, key_path + "\\" + child, depth - 1))
    except OSError:
        return tuple(values)
    return tuple(values)


def _root_from_product_path(path: Path, tag: str) -> Path | None:
    wanted = f"v{tag}".casefold()
    current = path
    while current != current.parent:
        if current.name.casefold() == wanted:
            return current
        current = current.parent
    return None


def _disk_install_candidates() -> Iterable[tuple[str, Path, str]]:
    candidates: list[tuple[str, Path, str]] = []
    for drive in _fixed_drive_roots():
        program_files = drive / "Program Files"
        if not program_files.is_dir():
            continue
        try:
            vendors = [path for path in program_files.glob("ANSYS Inc*") if path.is_dir()]
        except OSError:
            continue
        for vendor in vendors:
            try:
                versions = [path for path in vendor.glob("v*") if path.is_dir()]
            except OSError:
                continue
            for version in versions:
                match = _VERSION_RE.match(version.name)
                if match:
                    candidates.append((match.group(1), version.resolve(strict=False), "fixed_drive_scan"))
    return tuple(candidates)


def _fixed_drive_roots() -> Iterable[Path]:
    if os.name != "nt":
        return (Path("/"),)
    try:
        mask = ctypes.windll.kernel32.GetLogicalDrives()
    except Exception:
        return (Path("C:\\"),)
    roots: list[Path] = []
    for index in range(26):
        if mask & (1 << index):
            root = Path(f"{chr(65 + index)}:\\")
            try:
                drive_type = ctypes.windll.kernel32.GetDriveTypeW(str(root))
            except Exception:
                drive_type = 3
            if drive_type == 3:
                roots.append(root)
    return tuple(roots)


def _discover_bundled_python_version(base: Path) -> str:
    if base.is_dir():
        versions = [path.name for path in base.iterdir() if path.is_dir() and re.fullmatch(r"\d+_\d+", path.name)]
        if versions:
            return max(versions, key=lambda value: tuple(int(part) for part in value.split("_")))
    return "3_10"


# Compatibility exports. Runtime code uses discover_installation() dynamically.
_INITIAL_INSTALLATION = discover_installation()
ANSYS_ROOT = _INITIAL_INSTALLATION.root or Path(r"C:\Program Files\ANSYS Inc") / f"v{_INITIAL_INSTALLATION.version_tag}"
VERSION_TAG = _INITIAL_INSTALLATION.version_tag
