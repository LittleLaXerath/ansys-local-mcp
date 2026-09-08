# Changelog

## 0.2.1

- Fixes SpaceClaim slash options and distinguishes command input/output paths.
- Supports earlier-step generated files with `produces` declarations, dependency
  checks, and path revalidation before execution.
- Returns serializable Workbench values and verifies all expected actions and
  completion status. Isolates object handles from journal helper names.
- Exposes generated schemas through MCP resources and `inspect_runtime`,
  retaining all 12 public tools.
- Adds a SpaceClaim channel geometry script with Workbench/direct workflows
  and an existing-project CFX setup example.
- Adds mock product regression tests. The new modeling examples have not yet
  been verified with real Ansys products; automated tests never launch them.

## 0.2.0

- Adds strict Workflow v1 schemas, retry policy, step outputs, and runtime references.
- Adds automatic Ansys installation discovery without license or signature gating.
- Adds a generic Workbench recipe step for Fluent, CFX, Mechanical, and SpaceClaim orchestration.
- Adds shared PyFluent operation schemas and compatibility aliases.
- Adds fail-closed allowed roots, command argument policies, process-tree timeouts,
  complete step logs, and incremental atomic manifests.
- Expands Windows unit coverage without launching Ansys products.

## 0.1.0

- Initial experimental release.
- Adds Workbench-first and direct-product workflow execution.
- Adds whitelisted command registry for Fluent, CFX, SpaceClaim, Mechanical,
  MAPDL, System Coupling, and Ansys Python.
- Adds `inspect_runtime`, `validate_workflow`, and `run_workflow`.
- Keeps legacy MCP tool names as compatibility wrappers.
