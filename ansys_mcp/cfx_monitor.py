"""Incremental parser for CFX solver logs (``*.out``).

The parser only reports what the log actually contains. Missing values stay
unknown; they are never filled with zeros or treated as converged. It supports
reads from a byte offset so a caller can poll a growing log, and it tolerates a
partially written final line, CRLF, and restart rewinds of simulation time.
"""
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

_NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[EeDd][-+]?\d+)?"
_TIME_STEP = re.compile(
    r"^\s*TIME STEP\s*=\s*(\d+)\s+SIMULATION TIME\s*=\s*(" + _NUMBER + r")\s+CPU SECONDS\s*=\s*(" + _NUMBER + r")")
_LOOP = re.compile(r"^\s*COEFFICIENT LOOP ITERATION\s*=\s*(\d+)\s+CPU SECONDS\s*=\s*(" + _NUMBER + r")")
_EQUATION = re.compile(
    r"^\s*\|\s*([A-Za-z][A-Za-z0-9_.+\-]*)\s*\|\s*(" + _NUMBER + r")\s*\|\s*(" + _NUMBER + r")\s*\|\s*("
    + _NUMBER + r")\s*\|\s*(" + _NUMBER + r")[^|]*\|")
_COURANT = re.compile(
    r"^\s*\|\s*(" + _NUMBER + r")\s*\|\s*(" + _NUMBER + r")\s*\|\s*(" + _NUMBER + r")\s*\|\s*$")
_TARGET = re.compile(r"^\s*Residual Target\s*=\s*(" + _NUMBER + r")")
_RESIDUAL_TYPE = re.compile(r"^\s*Residual Type\s*=\s*([A-Za-z]+)")
_TOTAL_TIME = re.compile(r"^\s*Total Time\s*=\s*(" + _NUMBER + r")\s*\[s\]")
_MONITOR_POINT = re.compile(r"^\s*MONITOR POINT:\s*(\S+)")
_RESULT_PATH = re.compile(r"^\s*\|?\s*([A-Za-z]:[\\/][^\r\n|]+?\.res)\s*\|?\s*$")
_FINISHED = "This run of the ANSYS CFX Solver has finished"


def _number(text: str) -> float | None:
    try:
        value = float(text.replace("D", "E").replace("d", "e"))
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def empty_state() -> dict[str, Any]:
    return {"samples": [], "residual_target": None, "residual_type": None, "total_time_s": None,
            "finished": False, "result_path": None, "monitor_points": [], "restarts": 0, "orphan_rows": 0,
            "table_section": None, "result_section": False, "result_path_buffer": ""}


def parse_cfx_log(text: str, state: dict[str, Any] | None = None) -> dict[str, Any]:
    """Parse complete log lines, updating a caller-provided state in place."""
    current = state if state is not None else empty_state()
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        body = line.strip()
        if body.startswith("|") and body.endswith("|"):
            body = body[1:-1].strip()
        if "The results from this run of the ANSYS CFX Solver have been" in body:
            current.update(result_section=True, result_path_buffer="", result_path=None, table_section=None)
            continue
        if current.get("result_section"):
            if re.fullmatch(r"\+-+\+", body):
                current.update(result_section=False, result_path_buffer="")
            elif body and not body.startswith("written to"):
                pending = current.get("result_path_buffer", "")
                if pending:
                    # CFX's verbatim wrapper appends one hyphen when splitting
                    # a word. A real leading hyphen on the next line is kept.
                    pending = pending[:-1] + body if pending.endswith("-") else pending + " " + body
                else:
                    pending = body
                current["result_path_buffer"] = pending
                match = _RESULT_PATH.fullmatch(pending)
                if match:
                    current.update(result_path=match.group(1), result_section=False, result_path_buffer="")
            continue
        if "Equation" in body and "RMS Res" in body and "Max Res" in body:
            current["table_section"] = "equations"
        elif "RMS Courant Number" in body and "Max Courant Number" in body:
            current["table_section"] = "courant"
        elif not body or (line.lstrip().startswith("|") and re.search(r"[A-Za-z]", body)
                          and not _EQUATION.match(line) and not _COURANT.match(line)):
            current["table_section"] = None
        match = _TIME_STEP.match(line)
        if match:
            step, simulation_time, cpu = int(match.group(1)), _number(match.group(2)), _number(match.group(3))
            if simulation_time is None or cpu is None:
                current["orphan_rows"] += 1
                continue
            previous = current["samples"][-1]["simulation_time_s"] if current["samples"] else None
            if previous is not None and simulation_time < previous:
                current["restarts"] += 1
            current["samples"].append({"time_step": step, "simulation_time_s": simulation_time,
                                       "cpu_seconds": cpu, "iteration": None, "equations": {}, "courant": None})
            continue
        match = _LOOP.match(line)
        if match:
            current["table_section"] = "equations"
            sample = current["samples"][-1] if current["samples"] else None
            if sample is not None:
                sample["iteration"] = int(match.group(1))
            continue
        match = _EQUATION.match(line)
        if match and current.get("table_section") == "equations":
            sample = current["samples"][-1] if current["samples"] else None
            rate, rms, maximum = (_number(match.group(2)), _number(match.group(3)), _number(match.group(4)))
            if sample is None or rms is None or maximum is None or rate is None:
                current["orphan_rows"] += 1
                continue
            sample["equations"][match.group(1)] = {"rate": rate, "rms": rms, "max": maximum}
            continue
        match = _COURANT.match(line)
        if match and current.get("table_section") == "courant":
            sample = current["samples"][-1] if current["samples"] else None
            timestep, mean, maximum = (_number(match.group(1)), _number(match.group(2)), _number(match.group(3)))
            if sample is not None and timestep is not None and mean is not None and maximum is not None:
                sample["courant"] = {"timestep_s": timestep, "rms": mean, "max": maximum}
            continue
        match = _TARGET.match(line)
        if match:
            current["residual_target"] = _number(match.group(1))
            continue
        match = _RESIDUAL_TYPE.match(line)
        if match:
            current["residual_type"] = match.group(1)
            continue
        match = _TOTAL_TIME.match(line)
        if match:
            current["total_time_s"] = _number(match.group(1))
            continue
        match = _MONITOR_POINT.match(line)
        if match and match.group(1) not in current["monitor_points"]:
            current["monitor_points"].append(match.group(1))
            continue
        if _FINISHED in line:
            current["finished"] = True
    return current


def read_cfx_log(path: Path | str, *, offset: int = 0, state: dict[str, Any] | None = None) -> dict[str, Any]:
    """Read new complete lines from ``offset``; keep the partial tail for later."""
    target = Path(path)
    if offset < 0:
        raise ValueError("Log offset must be nonnegative.")
    try:
        with target.open("rb") as stream:
            stream.seek(offset)
            data = stream.read()
    except FileNotFoundError:
        return {"ok": False, "reason": "missing", "path": str(target), "offset": offset, "state": state}
    except OSError as exc:
        return {"ok": False, "reason": "unreadable", "error": type(exc).__name__, "path": str(target),
                "offset": offset, "state": state}
    end = data.rfind(b"\n") + 1
    complete, partial = data[:end], data[end:]
    parsed = parse_cfx_log(complete.decode("utf-8", "replace"), state)
    return {"ok": True, "path": str(target), "offset": offset + end, "partial_tail_bytes": len(partial),
            "state": parsed}


def summarize(state: dict[str, Any], *, expected_end_time: float | None = None,
               time_tolerance_s: float = 1e-12) -> dict[str, Any]:
    """Report completion separately from convergence and result availability."""
    samples = state.get("samples", [])
    last = samples[-1] if samples else None
    target = state.get("residual_target")
    equations: dict[str, Any] = {}
    converged: bool | None = None
    convergence_time: float | None = None
    if last is not None and last["equations"]:
        for name, values in last["equations"].items():
            equations[name] = dict(values)
        if target is not None:
            converged = all(values["rms"] <= target for values in last["equations"].values())
            for sample in samples:
                if sample["equations"] and all(v["rms"] <= target for v in sample["equations"].values()):
                    convergence_time = sample["simulation_time_s"]
                    break
    end_time = expected_end_time if expected_end_time is not None else state.get("total_time_s")
    end_reached: bool | None = None
    if end_time is not None and last is not None:
        end_reached = last["simulation_time_s"] >= end_time - time_tolerance_s
    return {
        "ok": True,
        "time_steps": len(samples),
        "restarts": int(state.get("restarts", 0)),
        "last_time_step": last["time_step"] if last else None,
        "last_simulation_time_s": last["simulation_time_s"] if last else None,
        "last_iteration": last["iteration"] if last else None,
        "equations": equations,
        "residual_target": target,
        "residual_type": state.get("residual_type"),
        "converged": converged,
        "convergence_time_s": convergence_time,
        "expected_end_time_s": end_time,
        "end_time_reached": end_reached,
        "finished_marker": bool(state.get("finished")),
        "result_path_from_log": state.get("result_path"),
        "monitor_points": list(state.get("monitor_points", [])),
        "monitor_values": None,  # CFX prints monitor values to .trn, not this log
        "orphan_rows": int(state.get("orphan_rows", 0)),
    }
