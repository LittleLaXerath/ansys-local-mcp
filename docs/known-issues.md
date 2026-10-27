# Known issues and pending investigations

Status values: `pending` (not started), `investigating`, `fixed-in-tree`
(change exists, not yet released), `blocked` (external condition), `closed`.
Only measured evidence moves an item forward; a plan entry is not a result.

## K-01 · CFX launcher stall inside the MCP process wrapper — `pending`

- **Observed**: the retained Re100 case report records that the CFX launcher
  appeared to stall while wrapped by the MCP process layer and the run was
  continued through the official batch entry point instead
  ([report how-to](../Karman_Vortex_Cylinder_Re100/RESULTS_REPORT.zh-CN.md)).
- **Not yet established**: which stage stalls (argument/environment, suspended
  creation and resume, launcher hand-off, process ownership, or a product-side
  wait). Inspection of the retained manifests did not locate a matching
  `cfx_solve` hang event.
- **Missing evidence**: a diagnostic bundle with the exact command line,
  working directory, environment diff, creation identity, Job Object
  relationship, and log growth over time from both the official and the MCP
  launch path.
- **Do not**: declare a root cause, add fixed sleeps, disable supervision
  globally, or kill processes by image name.
- **Next**: reproduce with a fake launcher first, then compare one real launch
  pair when product acceptance is explicitly enabled. Keep the workaround
  (official batch entry) documented; do not present it as an MCP success.

## K-02 · Durable workers cannot start in restricted Windows sessions — `blocked`

2026-10-28 comparison: the same ordinary Python/WMI diagnostic succeeded in a
normally elevated session (high integrity) and was denied in the measured medium
session. The completed P1 suite considered 242 tests: 240 passed, two intentional
opt-in tests skipped, no failures or errors. The medium-session limitation remains;
no automatic elevation, permission-policy change or lifetime-bound fallback was added.

- **Observed**: in a session whose token is medium-integrity with the
  administrators group deny-only, `Win32_Process.Create`
  (`SWbemServicesEx Access denied`) fails, so `start_workflow` and elevation
  sessions report `JOB_LAUNCH_ERROR`. The historical log contains 14 failure/error
  records for 13 unique tests, including one cleanup-stage duplicate.
- **Why it is correct to fail closed**: the launcher deliberately has no
  lifetime-bound fallback, because a worker tied to the MCP client's Job would
  vanish when the client closes.
- **Not a product defect**: verify from an ordinary interactive session and
  record the result there. Product gates stay `pending`/`blocked` until then.

## K-03 · Shared pywin32 generated-type cache — `fixed-in-tree`

- **Root cause**: `import win32com.client` resolved `gen_py` to a shared
  `%TEMP%` directory created under another identity; `gencache.Rebuild()`
  failed writing `__init__.py` (and reading `dicts.dat`), before any WMI call.
- **Fix**: `prepare_python_com_cache()` imports pywin32 once with a private
  writable `TEMP`, for the server and for each job/elevation worker. Child
  product processes keep the inherited environment.
- **Residual**: the fix is exercised by unit tests only; the full durable-job
  path now also passes in the measured elevated non-product test session.
  The medium-session WMI creation boundary is retained (see K-02).

## K-04 · Evidence location depends on the startup directory — `fixed-in-tree`

- **Observed**: evidence recorded under the repository's `.ansys_mcp` was not
  visible when the server started with a different default workdir, so
  `query_capabilities` looked empty although records existed.
- **Fix**: `ANSYS_MCP_EVIDENCE_ROOT` makes the store explicit; `inspect_runtime`
  reports the resolved root, counts and fingerprint diagnostics.
- **Residual**: historical records keep their original fingerprint and are not
  re-scoped automatically.

## K-05 · Fluent has no persistent session or product validation — `pending` (P3)

- Each `pyfluent` step launches a solver and exits; there is no reuse,
  generation invalidation, or monitoring (transcript is started but not read).
- No Fluent product acceptance exists in this repository's measured evidence.
- Planned in the handoff plan: persistent sessions, strict launch schema, TUI
  discovery, and a small laminar-channel benchmark.

## K-06 · CFX has no structured recipe — `pending` (P2)

- CFX is reachable through `command` steps and the Workbench CCL channel only;
  there is no `cfx` step, no incremental solver-log monitoring and no
  post-processing recipe.
- RMS targets belong to preprocessing physics settings, not to solver CLI flags.

## K-07 · Mechanical catalog coverage far exceeds measured evidence — `pending` (P4)

- The v261 inventory contains 56,936 unique entries; the local evidence store
  holds tens of observations. Many analysis types (harmonic, response spectrum,
  random vibration, buckling, acoustic, coupled field, composites, optimization,
  additive, ACT extensions) are catalogued but have no dedicated workflow
  acceptance.
- The component matrix in [validation.md](validation.md) remains the source of
  truth for what is measured.

## K-08 · No automated product regression — `pending`

- CI runs unit tests, compilation, publication checks and builds; it never
  starts a product. Product acceptance stays opt-in and manual by design.
