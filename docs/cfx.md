# CFX pre/solve/post step

The `cfx` workflow step runs exactly one CFX product process per step:
`cfx5pre -batch`, `cfx5solve` or `cfx5post -batch`. The recipe is versioned and
strict; unknown fields are rejected before any process starts.

## Launcher hand-off supervision

A CFX launcher may hand work to another process and exit. The generic supervisor
waits only for the direct child, so it can return with a zero exit code while the
real solver is still starting; closing the kill-on-close job then terminates the
hand-off process. A zero exit code is therefore never evidence of a finished
solve.

The `cfx` step opts into `supervise_handoff=True`: after the direct child exits,
the supervisor keeps waiting for the remaining processes in the same job (job
accounting), so the hand-off child can finish. The total timeout and
cancellation still apply and terminate the whole job; job cleanup is not
disabled globally and other steps keep the default policy.

The `cfx` step decides completion from files and log content:

- Declared outputs must exist, be non-empty **and** have been written during this
  run (modification time not older than the process start). An older `.def` or
  `.res` is reported as stale, never accepted as a new result.
- The solver log must also have been written during this run. An old completion
  log cannot prove completion of this run (`CFX_LOG_STALE`), even if a fresh,
  partial result file was planted next to it.
- A solve also reads the solver log (`<run_name>_<n>.out`, a declared
  `out_path`, or `<run_name>.out`). It reports separately: process status,
  finish marker, end-time reached, residual history and result freshness.
- Without a declared `res_path`, result freshness is **unknown**
  (`CFX_RESULT_UNDECLARED`) and the step stays `incomplete`, even when a fresh
  log shows the finish marker.
- A launcher that exits without a result or finish marker is `incomplete`
  (`CFX_SOLVER_INCOMPLETE`), not `complete`.
- A log that names a different result path than the declared one is
  `incomplete` (`CFX_RESULT_PATH_MISMATCH`).
- Missing monitor values stay unknown; they are never filled with zero and never
  treated as converged.

## Recipe shape

```json
{
  "operation": {
    "op": "solve",
    "def_path": "model.def",
    "res_path": "model_001.res",
    "run_name": "model",
    "precision": "double",
    "partitions": 4,
    "start_method": "Intel MPI Local Parallel"
  }
}
```

Operations:

- `pre`: `script` (trusted `.pre`/`.ccl` for `cfx5pre -batch`), optional
  `def_path` expected output.
- `solve`: `def_path` input; optional `res_path`, `out_path`, `run_name`,
  `precision`, `partitions`, `start_method`, `continue_from`, `initial_file`,
  `ccl_path`. `continue_from` and `initial_file` are mutually exclusive.
- `post`: `script` (trusted `.cse` for `cfx5post -batch`), optional
  `result_file` input, and declared `outputs` (name + path) that must be fresh.

RMS targets and other convergence criteria belong to the CFX-Pre physics
settings inside the `.def`. They are not accepted as solver command-line flags,
because `cfx5solve` has no such parameters.

## Paths and policy

- Every declared path is checked against `ANSYS_MCP_ALLOWED_ROOTS`; inputs and
  outputs have explicit roles and are rechecked before execution.
- Intermediate products can be declared with workflow `produces` and referenced
  as `${steps.<id>.outputs.def_path}` / `${...outputs.res_path}`.
- CFX steps refuse automatic retry (`retry.max_attempts` must stay 1): an
  unknown solver state must be observed, not replayed.
- The step uses the official executable, the shared supervisor (process-tree
  timeout, cancellation, kill-on-close job) and the same run directory layout as
  other steps.

## Monitoring fields

`solve` outputs `monitor` with: `time_steps`, `last_time_step`,
`last_simulation_time_s`, `last_iteration`, per-equation `rms`/`max`/`rate`,
`residual_target`, `residual_type`, `converged` (only when a target is present),
`convergence_time_s`, `expected_end_time_s`, `end_time_reached`,
`finished_marker`, `result_path_from_log`, `monitor_points`, `restarts`.
CFX writes monitor *values* to `.trn` files, not to the solver log, so
`monitor_values` stays `null` here.

`cfx_monitor.read_cfx_log(path, offset=...)` reads new complete lines from a
byte offset and keeps a partially written last line for the next read, so a
caller can poll a growing log without duplicating samples.

Result paths are read only from the final result-output footer, including CFX's
hyphen-wrapped long paths. Restart/initial-value input paths are not outputs.
Residual and Courant rows are scoped to their own tables, so parallel memory
statistics cannot contaminate the reported convergence state.

## Recorded product acceptance (2026-10-28)

A copied, retained Re100 case completed `pre -> solve -> post` through a fresh
stdio MCP server: 5.00 to 5.02 s, four 0.005 s steps, double precision, four
local partitions. The workflow took about 52 s; all three supervised Jobs
finished with zero remaining processes. Native CFD-Post opened the new result
and exported final-time metrics. The source project/input/result hashes stayed
unchanged.

The relative mass imbalance was 9.90e-8; final x/y force differences from the
retained result at the same time were 0.01162% and 0.01730%. The minimum finite
volume was positive and final equation RMS values were below 1e-5.
Original files, command lines, manifests and reports remain in the private
acceptance directory, not in this repository.

The first attempt exposed two parser defects (a restart path selected instead
of the wrapped output footer, and a memory-table row misread as a residual).
Its failed MCP record was retained. A new isolated run passed after fixes and
product-free regression tests; the failed record was not relabeled as success.

## Current limits

- No CCL parser: the `.pre`/`.ccl`/`.cse` files are trusted inputs whose meaning
  is not validated here.
- The recorded short restart verifies this product chain and instantaneous
  checks only. A complete Re100 frequency/mean-force regression remains pending;
  no grid/time-step independence or historical-stall root-cause fix is claimed.
- The Workbench CCL channel remains available and unchanged for projects,
  geometry and meshing.

## Diagnostics

`python -m tools.diagnose_cfx_launch --output .ansys_mcp/diagnostics/cfx-launch.json`
runs synthetic launchers through the shared supervisor (default vs supervised
policy, timeout, cancellation) and records whether the hand-off child completes
or is cleaned up. It never starts a CFX executable. The report also contains the
command template for a later, explicitly authorized official-vs-MCP comparison.

