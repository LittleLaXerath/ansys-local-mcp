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

## Workbench-first workflow

`workbench_first_suite.example.json` shows the preferred full-suite pattern:
run a Workbench journal with `runwb2_batch`. Replace `workbench_noop.wbjn` with
a real recorded or generated journal for your project.

Use this path when Fluent, CFX, SpaceClaim, Mechanical, Meshing, and project
data transfer need to stay coordinated through Workbench.

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
