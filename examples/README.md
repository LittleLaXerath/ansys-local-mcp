# Examples

These examples are templates. Edit paths, zones, journals, and workdirs before
running them against your own Ansys projects.

## Validate-only workflow

`workflow.example.json` uses only local repository files. It is safe to validate
without an Ansys installation:

```powershell
ansys-local-mcp
```

Then call the MCP tool `validate_workflow` with `workflow_path` set to the
example file.

## Workbench recipe workflow

`workbench_recipe.example.json` demonstrates the v0.2 structured Workbench
step. It obtains Workbench systems and containers, then routes file-backed
commands to Fluent, CFX, Mechanical, and SpaceClaim. Adjust system names and
replace the placeholder files under `examples/scripts/` before execution.

Product scripts must be inside `ANSYS_MCP_ALLOWED_ROOTS`. Inline code is
disabled unless `ANSYS_MCP_ALLOW_INLINE_CODE=1` is explicitly set.

## Legacy Workbench journal workflow

`workbench_first_suite.example.json` shows the preferred full-suite pattern:
run a Workbench journal with `runwb2_batch`. Replace `workbench_noop.wbjn` with
a real recorded or generated journal for your project.

The journal wrapper remains supported for existing recorded `.wbjn` files.

## Direct product workflow

`direct_channels.example.json` shows direct command IDs for SpaceClaim,
Mechanical, CFX-Pre, CFX Solver, and CFD-Post. GUI products use
`"detach": true` so the workflow returns after launch.

Use this path for targeted batch work, diagnostics, or single-product
automation.

## PyFluent recipe

`fluent_recipe.example.json` is a compatibility example for
`run_pyfluent_recipe`. Replace `path/to/input.cas.h5`, `path/to/output.cas.h5`,
and zone names before running.

`mesh_check` and `create_force_report` remain compatibility aliases but emit
warnings; new recipes should use `tui` and `create` primitives.
