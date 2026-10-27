# Test groups, opt-in gates and baseline record

This page classifies the test suite and records measured baselines. A passing
schema test never proves a product run, and a skipped test is never a success.
The product evidence itself lives in [validation.md](validation.md).

## Test groups

| Group | What it exercises | Files | Needs Ansys product? |
|---|---|---|---|
| 1. Pure logic | Schemas, path policy, command-argument policy, workflow resolution, capability catalog parsing, evidence store, publication checks | `test_capabilities.py`, `test_config_and_discovery.py`, `test_dpf.py` (recipe only), `test_engineering_data_recipe.py`, `test_modeling_regressions.py`, `test_publication.py`, `test_pyfluent_registry.py`, `test_schema_and_safety.py`, `test_workflow.py`, `test_workbench_recipe.py`, `test_acceptance_evidence.py`, `test_product_acceptance_helpers.py`, `test_specialized_acceptance_helpers.py` | No |
| 2. Ordinary Windows processes | Process tree, job objects, worker launch/IPC, cancellation, stdio transport, COM cache selection | `test_execution_runtime.py`, `test_process_supervisor.py`, `test_cancellation_outcomes.py`, `test_mechanical_recovery.py`, `test_mechanical_dispatch.py`, `test_product_steps.py`, `test_result_exports.py` | No (ordinary Python and cmd.exe only; some cases use the bundled interpreter but never a solver) |
| 3. Explicit product acceptance | Real Workbench/Mechanical/DPF/CFX/Fluent runs, numerical checks | `tests/test_mechanical_product.py` (opt-in) and the runners under `tools/` | Yes |

Group 2 uses the bundled interpreter when `ansys_python_executable()` exists and
otherwise skips; that case still launches no product or solver.

## Opt-in gates

| Gate | Environment variable | Effect |
|---|---|---|
| Mechanical product test | `ANSYS_MCP_PRODUCT_TESTS=1` plus `ANSYS_MCP_PRODUCT_WORKDIR` | Opens a fresh isolated project; never an existing one |
| UAC elevation lifecycle | `ANSYS_MCP_RUN_ELEVATION_TESTS=1` | Requests elevation; requires an interactive administrator session |
| Private evidence root | `ANSYS_MCP_EVIDENCE_ROOT` | Must be inside `ANSYS_MCP_ALLOWED_ROOTS`; invalid values raise a configuration error |

Ordinary CI runs group 1 and 2 only, with the opt-in variables unset.

## Baseline record (2026-10-27, local Windows 11, Python 3.11.9)

- `python -m unittest discover tests` considered **224 tests: 208 passed,
  3 failed, 11 error records, 2 skipped** (the two skips are the explicit opt-in
  product and UAC tests).
- Correction: there are 13 unique affected tests, not 14 independent cases;
  one test also errors during cleanup. The earlier manually calculated 208
  passing count should not be interpreted as a unique-test count.
- All affected tests are in `test_execution_runtime.py` and share one
  environment blocker: the session ran with a **medium-integrity, deny-only
  administrators token**, so `Win32_Process.Create` over WMI returns
  `SWbemServicesEx Access denied`. The worker launcher intentionally has no
  lifetime-bound fallback, so durable jobs and elevation sessions cannot start
  in that token. Rerun the same tests from an ordinary elevated-interactive
  session (or GitHub's `windows-latest`) to clear the blocker; no test was
  weakened or converted to an unconditional skip.
- A second, independent failure mode was identified and fixed in this round:
  pywin32 resolved its generated-type cache to a shared `%TEMP%\gen_py\3.11`
  owned by another identity, and `gencache.Rebuild()` failed on `__init__.py`
  before WMI was reached. `process_supervisor.prepare_python_com_cache()` now
  pins the cache to a private writable directory for the server and each
  worker; a unit test asserts the imported `win32com.__gen_path__` stays inside
  that directory. The blocked cases above fail later, at WMI creation, with a
  different diagnostic.
- Skipped runs are recorded as skips. The product and numerical evidence in
  [validation.md](validation.md) comes from separate real-product runs, not
  from this suite.

## P1 comparison and completion (2026-10-28)

- Ordinary Python subprocess creation, COM import and WMI queries passed in
  the medium session; WMI `Win32_Process.Create` returned Access denied.
- The same diagnostic in a normally elevated session passed WMI creation.
  It had high integrity and no Windows security settings were changed.
- Baseline recheck: 224 considered, 222 passed, two intentional skips, zero
  failures/errors. After the new retention, receipt and diagnostics tests:
  **242 considered, 240 passed, two intentional skips, zero failures/errors**.
- Both runs forced product and UAC test opt-ins off. They establish ordinary
  worker lifecycle behavior in that permitted environment, not new Ansys solves.
- `tools.run_checks` records unique affected test IDs and UTF-8 logs. See
  [Operations](operations.md) for commands, receipt and maintenance boundaries.

## Evidence store

- Default: `<default workdir>/.ansys_mcp/capability_evidence/v<version>/<fingerprint>.jsonl`.
- Override: `ANSYS_MCP_EVIDENCE_ROOT` (absolute or relative to the startup
  directory), validated against `ANSYS_MCP_ALLOWED_ROOTS`.
- `inspect_runtime` returns the resolved root, record counts by product and
  validation stage, unparsed-line count, and installation-scope diagnostics
  (`EVIDENCE_INSTALLATION_MISMATCH`, `EVIDENCE_FINGERPRINT_MISSING`).
- Evidence written for one installation fingerprint is not applied to another;
  it is never promoted to `result` from documentation alone.
