# Fluent Sessions / Fluent 会话

Use the existing `pyfluent` workflow step. The 17 MCP tools are unchanged;
`ansys://schema/pyfluent-recipe` is the sixth schema resource.

## Session Contract / 会话约定

- `mode=batch` is the compatible default: launch, execute, exit each step.
- `mode=persistent` uses the existing authenticated, same-user local worker.
  `session.action` is `new`, `reuse`, `summary`, or `close`. Only the server
  creates IDs. Reuse authenticates the recorded worker and creation identity;
  it never attaches to arbitrary Fluent ports or relaunches a missing solver.
- `${steps.start.outputs.session.id}` passes a session to later steps. The ID
  also survives stdio MCP shutdown and can be used by a new client connection.
  Reconnecting clients must use the same allowed-root configuration.
- `session.expected_generation` rejects stale state. Reading a case, setting
  state, creating objects, TUI and generic calls conservatively advance the
  generation, including calls that partly fail. No live Python object handles
  are exported. `call.select`/`tui.select` selects JSON-compatible return data,
  for example `["fluid"]` from solution-variable data.
- An explicit `call` with `path="switch_to_solver"` replaces a meshing root
  and refreshes monitoring callbacks. Other operations resolve from the current
  root on every call, not a cached object from an earlier generation.
- Automatic workflow retries are rejected for Fluent, including batch mode.
  A timed-out persistent request returns `PYFLUENT_OUTCOME_UNKNOWN`, the session
  ID and original `request_id`. Use `session.action=summary` with that request
  ID to observe it. Do not repeat a solve or mutation. Ordinary summary checks
  session health; `inspect_runtime` only reads cached registry observations.
- Explicit close only shuts down the launched session and processes whose
  creation identities the worker recorded. Failed or unconfirmed shutdown is
  not reported as closed. Cancellation uses the existing authenticated worker
  and its owned-process cancellation mechanism.

中文：默认仍为每步起停。持久模式显式创建、复用、观察和关闭；超时不等于失败，
必须查询原请求，不能重放。普通 Windows medium 会话的 WMI 创建权限限制仍然存在，
服务器不会自动提权或更改 Windows 安全设置。

## Runtime / 运行时

`runtime=bundled` explicitly selects the PyFluent distribution shipped with the
discovered Ansys installation (initial compatibility baseline: 0.37.1).
Batch runs use Ansys Python, preserving the legacy path. Persistent actors run
in the MCP Python environment and load the bundled PyFluent package; therefore
its dependencies must be compatible with that interpreter. Child/watchdog
imports receive the same explicit package path. Import failures are returned,
never retried in another environment. The session reports both client/product
versions and the actual Python executable/version.

`runtime=client` requires `ansys-fluent-core` installed in the MCP environment;
it does not inject the bundled distribution. No new mandatory dependency is
introduced. A fresh actor is required to choose a different runtime.

Launch accepts only the schema-listed options: solver/meshing/pure-meshing,
2D/3D, precision, processor count, startup timeout, UI mode, cwd and transcript.
Arbitrary CLI strings, environment overrides, remote endpoints and executable
overrides are not accepted. Explicit file fields and known file keyword
arguments are checked against allowed roots again in the worker. This is not an
OS sandbox: trusted TUI/call operations can still act with product permissions.

## Monitoring / 监控

`monitoring=true` records native monitor samples to `monitors.jsonl`, including
their x-axis index, series names, observation time and state generation. Native
iteration/time-step events are written to `events.jsonl`; transcript output
goes to `transcript.log`. The files stay in the original private run directory
across reconnects. Monitor errors are retained separately, never converted into
an invented residual or convergence claim. Missing streams remain unavailable.

These integrations use the public [monitor interface](https://fluent.docs.pyansys.com/version/stable/user_guide/monitors.html)
and [event interface](https://fluent.docs.pyansys.com/version/stable/user_guide/events.html).

## Discovery And Acceptance / 发现与验收

`query_capabilities(product="fluent")` parses installed release-specific
settings, solver TUI and meshing TUI declarations without executing them.
`component` selects `settings`, `tui`, or `meshing`. Declaration identifiers are
not executable dotted paths. Catalog presence never grants runtime reachability,
activation, writability or numerical evidence; use `inspect_path` on the current
session for active state. CFX catalog entries are limited to pre/solve/post.

`python -m tools.run_fluent_acceptance --execute --case channel` creates a new
isolated planar channel, 4 processes, and a 30-minute total deadline. It validates
every workflow, reconnects through a second stdio MCP server, saves results and
reads them back. Numerical evaluation is recorded separately from process/API
completion. CI never invokes this command. Failed runs are preserved, and an
unknown result is observed rather than automatically repeated.

`--runtime client` selects the optional installed environment explicitly.
`python -m tools.check_fluent_channel <run-directory>` independently evaluates
the saved native fields against Poiseuille flow, verifies physics readback,
mass balance, session continuity, shutdown and exact save/reopen equality.

Measured local acceptance (2026-10-29): Fluent 2026 R1 / PyFluent 0.37.1,
2D double precision, 2,400 cells, 4 processes, 46.953 seconds for the complete
workflow sequence. Velocity relative L2 error was 0.103454%, maximum profile
error/peak 0.111813%, pressure-gradient error 0.220579%, and relative mass
imbalance 0.000130344%. Saved/reopened velocity, pressure and centroid arrays
were identical. Native monitor, iteration-event and transcript files were
nonempty. This is one steady solver-channel benchmark, not validation of every
Fluent model, meshing workflow or transient operation. Ordinary-user WMI limits
and cross-product evidence receipt integration remain separate follow-up work.

完整 P3 产品 gate 还要求速度剖面误差 <=2%、压降误差 <=3%、质量不平衡 <=0.01%，
以及保存重开后一致。未达到这些条件时不得把接口或流程通过写成数值验收通过。
