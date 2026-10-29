"""Evaluate saved native Fluent readback; never starts a product."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from ansys_mcp.benchmarks import validate_fluent_channel
from ansys_mcp.config import require_allowed_path
from ansys_mcp.jobs import _write_json


def evaluate(directory: Path) -> dict:
    directory = require_allowed_path(directory, must_exist=True, expect_file=False)
    def read(name):
        return json.loads(require_allowed_path(directory / name, must_exist=True).read_text(encoding="utf-8"))
    flow = read("03-solve.result.json")
    fields = read("04-readback.result.json")
    reopened = read("05-reopen.result.json")
    solve_outputs = flow["steps"][0]["outputs"]
    values = solve_outputs["results"]
    flows = {key: value for item in values["mass_flows"] for key, value in item.items()}
    physics = values["physics"]
    result = validate_fluent_channel(fields["steps"][0]["outputs"]["results"], reopened["steps"][0]["outputs"]["results"],
                                    flows["mass_in"][0], flows["mass_out"][0])
    material = physics["materials"]["fluid"]["air"]
    inlet = physics["boundary_conditions"]["velocity_inlet"]["inlet"]["momentum"]["velocity_magnitude"]
    checks = result["checks"]
    checks["material_readback"] = material["density"]["value"] == 1.0 and material["viscosity"]["value"] == 0.001
    checks["inlet_readback"] = inlet["value"] == 0.1
    checks["laminar_readback"] = physics["models"]["viscous"]["model"] == "laminar"
    checks["workflows_complete"] = all(read(name)["ok"] for name in
        ("01-new.result.json", "02-reconnect.result.json", "03-solve.result.json", "04-readback.result.json", "05-reopen.result.json", "06-close.result.json"))
    session_id = solve_outputs["session"]["id"]
    checks["same_managed_session"] = all(read(name)["steps"][0]["outputs"]["session"]["id"] == session_id
        for name in ("01-new.result.json", "02-reconnect.result.json", "04-readback.result.json", "05-reopen.result.json", "06-close.result.json"))
    closed = read("06-close.result.json")["steps"][0]["outputs"]["session"]
    checks["confirmed_shutdown"] = closed["state"] == "closed" and not closed["live_owned_processes"]
    actor = require_allowed_path(solve_outputs["session"]["directory"], must_exist=True, expect_file=False)
    for name in ("transcript.log", "monitors.jsonl", "events.jsonl"):
        file = require_allowed_path(actor / name)
        checks[name + "_nonempty"] = file.is_file() and file.stat().st_size > 0
    artifacts = {}
    for name in ("channel.msh", "channel.cas.h5", "channel.dat.h5", "03-solve.result.json", "04-readback.result.json", "05-reopen.result.json"):
        file = require_allowed_path(directory / name, must_exist=True)
        artifacts[name] = {"size": file.stat().st_size, "sha256": hashlib.sha256(file.read_bytes()).hexdigest()}
    result.update(passed=all(checks.values()), artifacts=artifacts, session=solve_outputs["session"],
                  numerical_gate="passed" if all(checks.values()) else "failed")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--output", type=Path, help="Fresh private evaluation path; never overwrites a previous evaluation")
    args = parser.parse_args()
    result = evaluate(args.directory)
    target = require_allowed_path(args.output or args.directory / "numerical-acceptance.json")
    if target.exists():
        raise FileExistsError("Keep previous evaluations; select a fresh output instead of overwriting evidence.")
    _write_json(target, result)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
