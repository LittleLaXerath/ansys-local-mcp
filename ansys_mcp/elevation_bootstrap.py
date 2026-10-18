"""Independent ordinary-user UAC launcher; no credentials enter its argv."""
from __future__ import annotations

import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ansys_mcp.elevation import _launch_uac
from ansys_mcp.jobs import _read_json, _write_json


def main() -> None:
    bootstrap_path = Path(sys.argv[1]).resolve(strict=True)
    directory = bootstrap_path.parent
    sys.stdout = (directory / "launcher.stdout.log").open("a", encoding="utf-8", buffering=1)
    sys.stderr = (directory / "launcher.stderr.log").open("a", encoding="utf-8", buffering=1)
    try:
        bootstrap = _read_json(bootstrap_path)
        if bootstrap.get("version") != "1" or bootstrap.get("elevated") is not True:
            raise ValueError("The independent UAC launcher requires an elevated execution bootstrap.")
        worker = Path(__file__).with_name("elevation_worker.py")
        _launch_uac([str(worker), str(bootstrap_path)], worker.parent.parent)
        _write_json(directory / "uac-launched.json", {"requested": True})
    except BaseException as exc:
        _write_json(directory / "startup-error.json", {"message": str(exc)})


if __name__ == "__main__":
    main()
