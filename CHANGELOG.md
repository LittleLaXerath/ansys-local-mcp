# Changelog

## Unreleased

- Adds a first-run setup command that creates a separate environment, prints
  the dependency installation command and writes local MCP configuration without starting
  a product or overwriting an existing installation.
- Supports `python -m ansys_mcp` and rewrites the bilingual getting-started pages
  around the actual setup and workflow rather than internal implementation notes.

- Completes the six remaining planned basic benchmark workflows through the
  existing Workbench/Mechanical/DPF channels: response spectrum, one-sigma PSD,
  Euler buckling, transient conduction, closed-cavity acoustics and native
  thermal/structural transfer. All nine basic cases have scoped numerical
  evidence, not full Mechanical or specialized-extension coverage.
- Adds shared case/material/result registration, original modal-participation
  checks, explicit PSD static/RMS set identification and mesh-coordinate units.
  No new MCP tools, step types, schema resources or runtime dependencies.
- Keeps failed/cancelled trials and records only observed conditional API calls.

- Adds a Workbench-first Full harmonic benchmark with a native PointMass,
  explicit mass-proportional damping and independent complex frequency-response
  checks. Both complex parts, all native frequency sets and final Mechanical
  amplitude are verified.
- Preserves and validates Mechanical real/imaginary frequency-record mapping
  instead of treating both records as separate DPF frequency-set IDs.

- Adds a Workbench-first transient structural benchmark with project-local
  Engineering Data, a native tip PointMass, all-time DPF readback and an
  independent undamped SDOF step-response reference.
- Selects Mechanical basic cases through the existing acceptance runner's case
  registry and shares receipt integrity checks; no new MCP tools or dependencies.
- Canonicalizes surrounding XML-unit whitespace while keeping physical SI-unit
  requirements and dimensionless-material validation strict.
- Adds the first Mechanical basic-analysis acceptance: coordinate-system and
  named-selection load equivalence through Workbench, sequential 4-core static
  solves, native DPF nodal-vector readback and an independent beam reference.
- Adds run-scoped coordinate receipts with original export hashes, material XML,
  geometry integrity, frozen scripts and confirmed shutdown. Existing 17 tools,
  10 workflow steps and 6 schema resources are unchanged; other P4 cases remain pending.
- Shares bounded acceptance-client and SI material parsing helpers without new
  runtime dependencies. Ordinary tests never start Ansys products.
- Fixes Windows CI evidence-path comparison for 8.3 TEMP aliases without
  weakening path validation or skipping tests.
- Adds product-scoped Fluent/CFX workflow receipts, independent revalidation of
  native exports/fields/logs, explicit historical provenance and read-only
  queries. Existing Mechanical/DPF evidence identities remain unchanged; CFD
  API inventory entries are not promoted by these benchmark receipts.
- Rejects mismatched receipt/index metadata instead of borrowing another
  valid receipt's verification status.
- Adds managed Fluent sessions through the existing authenticated worker,
  strict launch/runtime contracts, generation checks and original-request
  observation without automatic replay. Adds native monitor/event/transcript
  logs, JSON/protobuf/array result conversion and generic return selection.
- Adds the sixth schema resource (`pyfluent-recipe`) and offline Fluent/CFX
  capability inventories. Keeps 17 tools and 10 workflow step types.
- Adds an optional `[fluent]` client environment and an explicitly opt-in
  isolated channel/session acceptance runner; no CI product launches.
- Records a real 4-process Fluent channel benchmark with independent Poiseuille
  comparison, native fields/physics readback, cross-client reuse, save/reopen
  equality and confirmed shutdown. Earlier failed probes are retained.
- Corrects passed-test accounting when one test has multiple failure records.
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
