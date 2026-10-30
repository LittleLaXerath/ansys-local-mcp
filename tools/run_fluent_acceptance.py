"""Explicit real Fluent session/reconnect acceptance; never invoked by CI."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
import traceback
from pathlib import Path

from ansys_mcp.config import require_allowed_path
from ansys_mcp.jobs import _write_json
from tools._mcp_acceptance import mcp_client as client, tool_value as data


def write_channel_mesh(path: Path, nx: int = 80, ny: int = 30, length: float = 0.2, height: float = 0.01) -> None:
    def node(i, j):
        return j * (nx + 1) + i + 1
    def cell(i, j):
        return j * nx + i + 1
    zones = [(3, 2, "interior", []), (4, 10, "inlet", []), (5, 5, "outlet", []),
             (6, 3, "bottom", []), (7, 3, "top", [])]
    interior, inlet, outlet, bottom, top = [zone[3] for zone in zones]
    for j in range(ny):
        for i in range(1, nx):
            interior.append((node(i, j), node(i, j + 1), cell(i - 1, j), cell(i, j)))
    for j in range(1, ny):
        for i in range(nx):
            interior.append((node(i, j), node(i + 1, j), cell(i, j), cell(i, j - 1)))
    for j in range(ny):
        inlet.append((node(0, j + 1), node(0, j), cell(0, j), 0))
        outlet.append((node(nx, j), node(nx, j + 1), cell(nx - 1, j), 0))
    for i in range(nx):
        bottom.append((node(i, 0), node(i + 1, 0), cell(i, 0), 0))
        top.append((node(i + 1, ny), node(i, ny), cell(i, ny - 1), 0))
    nodes, cells = (nx + 1) * (ny + 1), nx * ny
    faces = sum(len(zone[3]) for zone in zones)
    lines = ['(0 "Independent planar laminar channel benchmark")', '(2 2)', f'(10 (0 1 {nodes:x} 0 2))',
             f'(10 (1 1 {nodes:x} 1 2)(']
    lines.extend(f'{length*i/nx:.16g} {height*j/ny:.16g}' for j in range(ny + 1) for i in range(nx + 1))
    lines.extend(['))', f'(12 (0 1 {cells:x} 0))', f'(12 (2 1 {cells:x} 1 3))', f'(13 (0 1 {faces:x} 0))', '(45 (2 fluid fluid)())'])
    first = 1
    for zone, bc, name, data in zones:
        last = first + len(data) - 1
        lines.append(f'(13 ({zone:x} {first:x} {last:x} {bc:x} 2)(')
        lines.extend(' '.join(f'{value:x}' for value in face) for face in data)
        lines.append('))')
        kind = {2: 'interior', 10: 'velocity-inlet', 5: 'pressure-outlet', 3: 'wall'}[bc]
        lines.append(f'(45 ({zone:x} {kind} {name})())')
        first = last + 1
    path.write_text('\n'.join(lines) + '\n', encoding='ascii')


async def execute(directory: Path, case: str, runtime: str = "bundled"):
    started = time.monotonic()
    deadline = started + 1800
    session_id = None
    outcomes = {}
    async def run(session, name, steps):
        workflow = {"version": "1", "workdir": str(directory), "steps": steps}
        _write_json(directory / (name + ".workflow.json"), workflow)
        check = data(await session.call_tool("validate_workflow", {"workflow": workflow, "validation_mode": "runtime"}))
        _write_json(directory / (name + ".validation.json"), check)
        if not check["ok"]:
            raise RuntimeError(f"{name} validation failed: {check}")
        remaining = int(deadline - time.monotonic())
        if remaining <= 0:
            raise TimeoutError("Acceptance total deadline exceeded.")
        result = data(await session.call_tool("run_workflow", {"workflow": workflow, "timeout_seconds": remaining}))
        _write_json(directory / (name + ".result.json"), result)
        outcomes[name] = result
        if not result["ok"]:
            raise RuntimeError(f"{name} failed; inspect its original result. No request was replayed.")
        return result
    def step(action, operations=None, **kwargs):
        spec = {"action": action}
        if action != "new":
            spec["id"] = session_id
        return {"id": "fluent", "type": "pyfluent", "mode": "persistent", "runtime": runtime,
                "session": spec, "operations": operations or [], **kwargs}
    try:
        async with client(directory) as first:
            initial = await run(first, "01-new", [step("new", [{"op": "inspect_path", "path": "settings", "store_as": "root"}],
                                                  launch={"dimension": 2, "processor_count": 4})])
            session_id = initial["steps"][0]["outputs"]["session"]["id"]
            _write_json(directory / "session.json", {"id": session_id})
        # A second stdio server must recover the original authenticated actor.
        async with client(directory) as second:
            await run(second, "02-reconnect", [step("summary")])
            if case == "channel":
                mesh = directory / "channel.msh"
                write_channel_mesh(mesh)
                ops = [
                    {"op": "call", "path": "settings.file.read_mesh", "kwargs": {"file_name": str(mesh)}},
                    {"op": "tui", "path": "mesh.check"},
                    {"op": "set_state", "path": "settings.setup.models.viscous.model", "value": "laminar"},
                    {"op": "set_state", "path": 'settings.setup.materials.fluid["air"].density', "value": {"option": "constant", "value": 1.0}},
                    {"op": "set_state", "path": 'settings.setup.materials.fluid["air"].viscosity', "value": {"option": "constant", "value": 0.001}},
                    {"op": "set_constant", "path": 'settings.setup.boundary_conditions.velocity_inlet["inlet"].momentum.velocity_magnitude', "value": 0.1},
                    *[{"op": "create", "path": "settings.solution.report_definitions.flux", "name": name,
                       "state": {"report_type": "flux-massflow", "boundaries": [boundary]}}
                      for name, boundary in (("mass_in", "inlet"), ("mass_out", "outlet"))],
                    {"op": "call", "path": "settings.solution.initialization.hybrid_initialize"},
                    {"op": "iterate", "iterations": 400},
                    {"op": "compute_reports", "names": ["mass_in", "mass_out"], "store_as": "mass_flows"},
                    {"op": "get_state", "path": "settings.setup", "store_as": "physics"},
                    {"op": "write", "case_data": str(directory / "channel.cas.h5")},
                ]
                await run(second, "03-solve", [step("reuse", ops)])
                exports = [{"op": "call", "path": "fields.solution_variable_data.get_data",
                            "kwargs": {"variable_name": variable, "zone_names": ["fluid"]},
                            "select": '["fluid"]', "store_as": name}
                           for name, variable in (("u", "SV_U"), ("p", "SV_P"), ("centroids", "SV_CENTROID"))]
                await run(second, "04-readback", [step("reuse", exports)])
                await run(second, "05-reopen", [step("reuse", exports, read={"case_data": str(directory / "channel.cas.h5")})])
            await run(second, "06-close", [step("close")])
            session_id = None
        _write_json(directory / "acceptance.json", {"ok": True, "case": case, "elapsed_s": time.monotonic()-started,
                    "scope": "Session/reconnect/save/readback only; numerical gate requires independent evaluation."})
    except Exception as exc:
        _write_json(directory / "acceptance.json", {"ok": False, "error": str(exc), "session_id": session_id,
                    "elapsed_s": time.monotonic()-started, "traceback": traceback.format_exc()})
        # Unknown outcomes are left for observation; never submit a second mutation.
        last = next(reversed(outcomes.values()), None) if outcomes else None
        if session_id and last and not last["ok"] and all(item.get("status") != "unknown" for item in last.get("steps", [])):
            try:
                async with client(directory) as cleanup:
                    await run(cleanup, "cleanup-close", [step("close")])
            except Exception:
                (directory / "cleanup-error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--case", choices=["session", "channel"], default="session")
    parser.add_argument("--runtime", choices=["bundled", "client"], default="bundled")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.execute:
        parser.error("Real products require --execute.")
    directory = require_allowed_path(args.output or Path.cwd() / ".ansys_mcp" / "fluent_acceptance" / uuid.uuid4().hex)
    directory.mkdir(parents=True, exist_ok=False)
    print(str(directory), flush=True)
    asyncio.run(execute(directory, args.case, args.runtime))


if __name__ == "__main__":
    main()
