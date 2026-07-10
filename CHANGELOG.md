# Changelog

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
