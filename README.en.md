# Ansys Local MCP Server

[Main page](README.md) | [中文说明](README.zh-CN.md)

Experimental local MCP server for orchestrating Ansys workflows through
Workbench-first journals and direct product commands.

This project is not affiliated with, endorsed by, or sponsored by Ansys. It
does not include Ansys software, licenses, solvers, examples, or proprietary
project data. You need a working local Ansys installation and valid licensing.

## What It Does

- Exposes a small MCP interface for local Ansys automation.
- Prefers JSON workflows over adding one-off MCP tools.
- Supports Workbench-first orchestration through `RunWB2`.
- Supports direct command channels for Fluent, CFX, SpaceClaim, Mechanical,
  MAPDL, System Coupling, and Ansys Python.
- Validates workflow schema, allowed paths, command IDs, and variables before
  execution.

## Install

```powershell
git clone https://github.com/LittleLaXerath/ansys-local-mcp.git
cd ansys-local-mcp
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

The console entry point is:

```powershell
ansys-local-mcp
```

Legacy script entry point:

```powershell
python .\ansys_mcp_server.py
```

## MCP Client Config

Example stdio config:

```json
{
  "mcpServers": {
    "ansys-local": {
      "command": "ansys-local-mcp",
      "args": [],
      "env": {
        "ANSYS_MCP_ROOT": "C:\\Program Files\\ANSYS Inc\\v261",
        "ANSYS_MCP_VERSION_TAG": "261",
        "ANSYS_MCP_ALLOWED_ROOTS": "C:\\Users\\YOUR-NAME\\ansys-work"
      }
    }
  }
}
```

Adjust `ANSYS_MCP_ROOT` and `ANSYS_MCP_ALLOWED_ROOTS` for your machine.

## Tools

General tools:

- `inspect_runtime`
- `validate_workflow`
- `run_workflow`

Compatibility tools:

- `get_ansys_install_info`
- `check_ansys_security_surface`
- `launch_workbench`
- `run_workbench_journal`
- `run_mapdl_input`
- `run_fluent_journal`
- `run_system_coupling`
- `run_ansys_python_script`
- `run_pyfluent_recipe`

The compatibility tools build one-step workflows internally and return the same
structured result shape as `run_workflow`.

## Command IDs

External execution is argv-only through a whitelist. Arbitrary shell strings are
rejected.

- `ansys_python`
- `fluent`
- `cfx_pre`
- `cfx_solve`
- `cfx_post`
- `cfd_post`
- `runwb2`
- `runwb2_batch`
- `mapdl`
- `mechanical`
- `spaceclaim`
- `system_coupling`

## Workflow v1

Validate-only example:

```json
{
  "version": "1",
  "workdir": ".",
  "steps": [
    {
      "id": "check_project_dir",
      "type": "assert",
      "path": ".",
      "exists": true
    }
  ]
}
```

Step types:

- `command`: run a whitelisted command ID with argv args.
- `python_script`: run an allowed Python script with Ansys Python.
- `pyfluent`: launch PyFluent and execute generic operations.
- `file`: `exists`, `read_text`, `write_text`, or `copy`.
- `assert`: check path existence.
- `collect`: record artifacts without running Ansys.

PyFluent operations:

- `read`, `write`
- `set_state`, `set_constant`, `get_state`
- `call`, `tui`
- `iterate`, `calculate`
- `create`, `compute_reports`
- `inspect_path`

PyFluent dotted paths support collection indexes and fallback variants:

```json
{
  "op": "set_state",
  "path": [
    "settings.setup.models.viscous.model",
    "settings.setup.models.viscous"
  ],
  "value": "k-epsilon"
}
```

## Workbench-First Channel

Use Workbench journals when the task spans products such as Fluent, CFX,
SpaceClaim, Mechanical, Meshing, and shared project state.

Template:

`examples/workbench_first_suite.example.json`

Replace `examples/workbench_noop.wbjn` with a real recorded or generated
Workbench journal for your project.

## Direct Product Channel

Use direct commands for targeted batch work, diagnostics, or single-product
automation.

Template:

`examples/direct_channels.example.json`

For GUI products such as SpaceClaim and Mechanical, set `"detach": true` so the
workflow returns after launch.

## Results and Diagnostics

`run_workflow` returns:

- `ok`
- `run_id`
- `stage`
- `run_dir`
- `steps`
- `artifacts`
- `diagnostics`
- `stdout_tail`
- `stderr_tail`
- `elapsed_s`

Each run writes:

`<workdir>\.ansys_mcp\runs\<run_id>\manifest.json`

Standard diagnostic codes:

- `VALIDATION_ERROR`
- `PATH_DENIED`
- `EXECUTABLE_MISSING`
- `PROCESS_TIMEOUT`
- `PROCESS_NONZERO`
- `PYFLUENT_IMPORT_ERROR`
- `PYFLUENT_OPERATION_ERROR`
- `ARTIFACT_MISSING`

Use `validate_workflow` before `run_workflow` when building a new workflow.

## Security

This is a local command-execution MCP server. Restrict
`ANSYS_MCP_ALLOWED_ROOTS`, review workflows before running them, and do not
expose this server to untrusted clients. See `SECURITY.md`.

## Verification

```powershell
python -m unittest discover tests
python -m compileall ansys_mcp ansys_mcp_server.py
```

The tests do not launch Fluent, Workbench, CFX, SpaceClaim, Mechanical, MAPDL,
or System Coupling.
