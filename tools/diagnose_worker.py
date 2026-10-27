"""Opt-in local worker diagnostic; launches only a short-lived Python process."""
from __future__ import annotations

import argparse
import csv
import ctypes
import io
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from ansys_mcp.config import require_allowed_path
from ansys_mcp.jobs import _write_json
from ansys_mcp.process_supervisor import prepare_python_com_cache


def probe() -> dict:
    result = {"observed_at": datetime.now(timezone.utc).isoformat(), "platform": sys.platform,
              "elevated": bool(ctypes.windll.shell32.IsUserAnAdmin()) if os.name == "nt" else False}
    completed = subprocess.run([sys.executable, "-c", "pass"], stdin=subprocess.DEVNULL,
                               capture_output=True, timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    result["ordinary_process"] = {"ok": completed.returncode == 0, "returncode": completed.returncode}
    if os.name != "nt":
        return {**result, "wmi": {"ok": False, "status": "unsupported"}}
    groups = subprocess.run(["whoami.exe", "/groups", "/fo", "csv", "/nh"],
                            capture_output=True, check=True, timeout=10)
    for row in csv.reader(io.StringIO(groups.stdout.decode("utf-8", errors="replace"))):
        if len(row) >= 4 and row[2] == "S-1-5-32-544":
            result["administrators_attributes"] = row[3]
        if len(row) >= 3 and row[2].startswith("S-1-16-"):
            result["integrity_sid"] = row[2]
    phase = "com_import"
    try:
        prepare_python_com_cache()
        import pythoncom
        import win32com.client
        result["com_import"] = {"ok": True}
        pythoncom.CoInitialize()
        service = startup = method = parameters = response = None
        try:
            phase = "wmi_connect"
            service = win32com.client.GetObject(r"winmgmts:{impersonationLevel=impersonate}!\\.\root\cimv2")
            phase = "wmi_query"
            result["wmi_query"] = {"ok": len(list(service.ExecQuery(
                "SELECT ProcessId FROM Win32_Process WHERE ProcessId=" + str(os.getpid())))) == 1}
            phase = "wmi_create"
            startup = service.Get("Win32_ProcessStartup").SpawnInstance_()
            startup.ShowWindow = 0
            method = service.Get("Win32_Process").Methods_("Create")
            parameters = method.InParameters.SpawnInstance_()
            parameters.CommandLine = subprocess.list2cmdline([sys.executable, "-c", "pass"])
            parameters.CurrentDirectory = str(Path.cwd())
            parameters.ProcessStartupInformation = startup
            response = service.ExecMethod("Win32_Process", "Create", parameters)
            code = int(response.Properties_("ReturnValue").Value)
            result["wmi"] = {"ok": code == 0, "phase": phase, "returncode": code}
        except Exception as exc:
            result["wmi"] = {"ok": False, "phase": phase, "error_type": type(exc).__name__, "message": str(exc)}
        finally:
            # Release COM proxies on the apartment thread before uninitializing.
            response = parameters = method = startup = service = None
            pythoncom.CoUninitialize()
    except Exception as exc:
        result["wmi"] = {"ok": False, "phase": phase, "error_type": type(exc).__name__, "message": str(exc)}
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="Private diagnostic JSON inside allowed roots")
    args = parser.parse_args()
    output = require_allowed_path(args.output, expect_file=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    report = probe()
    _write_json(output, report)
    print(json.dumps(report, indent=2, ensure_ascii=True))
    raise SystemExit(0 if report.get("wmi", {}).get("ok") else 1)


if __name__ == "__main__":
    main()
