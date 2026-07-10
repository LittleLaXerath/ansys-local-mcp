# Ansys Local MCP Server

[Main page](README.md) | [中文说明](README.zh-CN.md)

An experimental Windows-local MCP server for orchestrating Ansys through strict
JSON workflows, structured Workbench recipes, direct product commands, and
generic PyFluent operations.

This project is not affiliated with, endorsed by, or sponsored by Ansys. It
does not ship Ansys software, licenses, solvers, or proprietary project data.
It requires a working local installation. Product licensing and legal
compliance remain the responsibility of the user and Ansys products themselves.
The MCP does not inspect, bypass, modify, or manage licensing.

## Install

```powershell
git clone https://github.com/LittleLaXerath/ansys-local-mcp.git
cd ansys-local-mcp
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

Run the console entry point:

```powershell
ansys-local-mcp
```

The legacy `python .\ansys_mcp_server.py` entry point remains available.

## Client Configuration

Ansys installation discovery checks, in order, `ANSYS_MCP_ROOT`, a matching
`AWP_ROOT<tag>`, other `AWP_ROOT*` variables, the Windows registry, and fixed
drive installation directories. You normally only need to configure the
workspace boundary:

```json
{
  "mcpServers": {
    "ansys-local": {
      "command": "ansys-local-mcp",
      "args": [],
      "env": {
        "ANSYS_MCP_ALLOWED_ROOTS": "C:\\Users\\YOUR-NAME\\ansys-work"
      }
    }
  }
}
```

If `ANSYS_MCP_ROOT` is explicitly set but invalid, execution fails with
`CONFIG_ERROR` instead of silently choosing another installation. If allowed
roots are omitted, only the server startup directory is allowed and a warning
is returned.

## What Schema Means

A schema is only the contract for workflow JSON: required fields, allowed
values, identifier syntax, and type-specific options. It is unrelated to Ansys
licensing or executable signatures.

Workflow v1 rejects unknown fields, empty step lists, duplicate step IDs, path
traversal IDs, non-positive timeouts, and malformed product recipes before any
Ansys application starts.

## MCP Tools

Stable general tools:

- `inspect_runtime`
- `validate_workflow`
- `run_workflow`

Compatibility tools retained unchanged:

- `get_ansys_install_info`
- `check_ansys_security_surface`
- `launch_workbench`
- `run_workbench_journal`
- `run_mapdl_input`
- `run_fluent_journal`
- `run_system_coupling`
- `run_ansys_python_script`
- `run_pyfluent_recipe`

`check_ansys_security_surface` is an opt-in signature report. It is never called
by validation, discovery, or execution, and its result never blocks workflows.

## Workflow v1

Step types:

- `command`: run a whitelisted product command with an argv list.
- `python_script`: run an allowed `.py` file with Ansys Python.
- `pyfluent`: execute strict generic PyFluent operations.
- `workbench`: compile and execute a structured Workbench recipe.
- `file`: inspect, read, write, or copy local files.
- `assert`: assert path existence.
- `collect`: collect required or optional artifacts.

Every step may define:

```json
{
  "id": "solve",
  "timeout_seconds": 3600,
  "retry": {
    "max_attempts": 2,
    "backoff_seconds": 2,
    "retry_on_codes": ["PROCESS_TIMEOUT", "PROCESS_NONZERO"]
  }
}
```

Every step returns an `outputs` object. Later steps can reference it with
`${steps.<id>.outputs.<field>}`. Existing `${variable}` substitution remains
supported.

`validate_workflow(validation_mode="schema")` validates portable structure
without requiring Ansys executables. The default `runtime` mode also verifies
the discovered runtime and executable availability. Neither mode launches
Ansys or writes workflow files.

## Workbench Recipe

The `workbench` step accepts exactly one of `recipe` or `recipe_path`. Recipes
store Workbench handles with `store_as`, then reference them through `target`:

```json
{
  "id": "suite",
  "type": "workbench",
  "mode": "batch",
  "recipe": {
    "actions": [
      {"op": "get_system", "name": "CFX", "store_as": "cfx"},
      {
        "op": "get_container",
        "target": "cfx",
        "component_name": "Setup",
        "store_as": "setup"
      },
      {
        "op": "send_command",
        "target": "setup",
        "script_path": "scripts/setup.ccl"
      }
    ]
  }
}
```

Actions: `get_template`, `create_system`, `get_system`, `get_component`,
`get_container`, `call`, `get_attr`, `set_attr`, `send_command`, and
`run_script`. Nested arguments can reference handles with `{"$ref": "name"}`.

Product scripts must be files under allowed roots. Inline `code` is disabled by
default; set `ANSYS_MCP_ALLOW_INLINE_CODE=1` to opt in. Inline content is first
written into the run directory so it remains auditable.

See [the full-suite example](examples/workbench_recipe.example.json).

## PyFluent

Supported primitives:

- `read`, `write`
- `set_state`, `set_constant`, `get_state`
- `call`, `tui`
- `iterate`, `calculate`
- `create`, `compute_reports`, `inspect_path`

Dotted paths support attributes, integer indexes, quoted collection keys, and
fallback path lists. The old `mesh_check`, `set_zone_type`, and
`create_force_report` names remain compatibility aliases and emit warnings.

## Results and Logs

`run_workflow` returns `ok`, `run_id`, `status`, `steps`, `artifacts`,
`diagnostics`, output tails, and elapsed time. Each step includes `outputs` and
full stdout/stderr log paths.

Runs are stored under:

```text
<workdir>\.ansys_mcp\runs\<run_id>\
```

`manifest.json` is written atomically at run creation, after every step, and at
completion. Timeouts terminate the supervised Windows process tree. Required
missing artifacts return `ARTIFACT_MISSING` and fail the run.

## Security Boundary

Allowed roots constrain paths resolved by the MCP server. They are not an OS
sandbox: a trusted Python, Workbench, Fluent, CFX, Mechanical, or SpaceClaim
script can access resources permitted to that product process. Review scripts
and connect only trusted MCP clients. See [Security Notes](SECURITY.en.md).

## Verification

```powershell
python -m unittest discover tests
python -m compileall ansys_mcp ansys_mcp_server.py
```

Automated tests do not launch Workbench, Fluent, CFX, SpaceClaim, Mechanical,
MAPDL, System Coupling, or a solver.
