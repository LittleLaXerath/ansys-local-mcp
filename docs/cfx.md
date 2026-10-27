# CFX pre/solve/post step

The `cfx` workflow step runs exactly one CFX product process per step:
`cfx5pre -batch`, `cfx5solve` or `cfx5post -batch`. The recipe is versioned and
strict; unknown fields are rejected before any process starts.

## Why completion is verified, not assumed

A CFX launcher may hand work to another process and exit. The supervisor waits
for the direct child only, so it can return with a zero exit code while the real
solver is still starting; closing the kill-on-close job then terminates the
hand-off process. A zero exit code is therefore never evidence of a finished
solve.

The step decides completion from files and log content:

- Declared outputs must exist **and** have been written during this run
  (modification time not older than the process start). An older `.def` or
  `.res` is reported as stale, never accepted as a new result.
- A solve also reads the solver log (`<run_name>_<n>.out`, a declared
  `out_path`, or `<run_name>.out`). It reports separately: process status,
  finish marker, end-time reached, residual history and result freshness.
- A launcher that exits without a result or finish marker is `incomplete`
  (`CFX_SOLVER_INCOMPLETE`), not `complete`.
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

## Current limits

- No CCL parser: the `.pre`/`.ccl`/`.cse` files are trusted inputs whose meaning
  is not validated here.
- Product acceptance for a real small CFX case and the Re100 regression is
  explicit opt-in and has not been run for this step yet. Interface and
  completion tests are product-free.
- The Workbench CCL channel remains available and unchanged for projects,
  geometry and meshing.
