# Changelog

## Unreleased

- Adds a versioned `cfx` workflow step for one `cfx5pre`/`cfx5solve`/`cfx5post`
  operation per step, with an `ansys://schema/cfx-recipe` resource. The MCP tool
  count stays at 17 and the step count becomes 10.
- Verifies CFX completion instead of trusting the exit code: declared outputs
  must be non-empty and fresh, the solver log must be written during this run,
  solves require a finish marker or a reached end time, and an undeclared result
  keeps freshness unknown instead of counting as verified.
- Adds an opt-in `supervise_handoff` policy to the supervisor: after a launcher
  exits, remaining job processes are still supervised, while timeout and
  cancellation keep terminating the whole job. Other steps keep the default
  policy.
- Adds an incremental CFX solver-log parser (time steps, simulation time,
  residuals, target, restarts, monitor point names, partial-tail handling).
- Adds product-free launcher hand-off regression tests covering child
  completion, timeout, cancellation and stale-log misjudgement, plus
  `tools.diagnose_cfx_launch` for the synthetic matrix and the authorized
  official-vs-MCP comparison template.
- Fixes result-footer extraction for hyphen-wrapped long paths and prevents
  host-memory tables from being interpreted as residual equations.
- Records an opt-in CFX short restart through the real pre/solve/post MCP
  chain with native result readback. Full Re100 shedding regression remains
  pending; the initial failed parser record is retained separately.

## 0.3.1

- Isolates pywin32 caches and exposes an explicit allowed-root evidence store.
- Adds ordinary/elevated worker diagnostics and unique test-outcome reports.
- Records explicit WMI denial as a known, non-replayed launch failure and
  releases COM objects before uninitializing their apartment.
- Adds default-dry-run retention for revalidated, finished-workflow log files.
- Adds hash-bound, independently checked Mechanical acceptance receipts and
  `query_capabilities(kind="workflow")` without adding MCP tools.
- Shares product-free benchmark validators between runners and receipt imports.
- Preserves the medium-token WMI restriction as a measured compatibility boundary.

## 0.3.0

- Adds versioned Mechanical/DPF workflow steps while retaining Workflow v1 and
  the original public tools.
- Adds Workbench-first and independent Mechanical backends, typed values,
  owned sessions, request observation, and explicit local elevation workers.
- Adds durable background jobs, progress, process-identity reconciliation and
  cancellation without replaying uncertain product mutations.
- Inventories the installed official Mechanical stubs with source accounting;
  public declarations and product validation evidence remain separate.
- Adds scoped local DPF operations and CSV/JSON exports in supervised workers.
- Fixes Windows batch quoting and expands real-process lifecycle regressions.
- Normalizes Windows path aliases for allowed roots and session registries;
  rejects invalid command arguments even when Ansys is not installed.
- Includes PyAnsys dependencies in the standard installation and publication
  checks for tracked files, staged content, distributions and commit identity.
- Records opt-in product validation separately from unit and simulated tests.

- 新增 Mechanical/DPF 工作流、双后端、统一值协议及持久任务管理。
- 分离公开 API 目录、运行时可达和真实产品数值验证等级。
- 增加发布脱敏检查，原始工程、配置及运行日志留在本地运行目录。
- 统一 Windows 长短路径；未安装产品时仍严格校验动作参数。

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
