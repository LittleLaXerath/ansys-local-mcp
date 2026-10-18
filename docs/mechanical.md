# Mechanical, sessions, jobs and capability evidence

The adapter inventories the installed official public API stubs and exposes structured object operations. Catalog coverage, a successful object call, a completed solve and a verified numerical result are distinct observations. A catalog entry alone never proves that a module is installed or licensed.

## Installation and discovery

The standard installation includes PyWorkbench, PyMechanical, Mechanical stubs and PyDPF. `inspect_runtime(include_schema=True)` reports discovered installations, dependency versions and recipe schemas without starting a product. Installation and working directories remain local configuration, not source constants. Set `ANSYS_MCP_ALLOWED_ROOTS` to an existing work directory before launching the stdio service.

Resources:

- `ansys://schema/workflow`
- `ansys://schema/workbench-recipe`
- `ansys://schema/mechanical-recipe`
- `ansys://schema/dpf-recipe`

`query_capabilities(product="mechanical", component="explicit_dynamics", offset=0, limit=100)` returns a page of public declarations and any separate runtime evidence. Omit `version` to use the discovered product release. Components without a public interface, absent extensions and GUI-only operations are reported as gaps, not implemented adapters.

## Mechanical recipes

Use the `mechanical` workflow step with a version `"1"` recipe. `backend` is `workbench` by default, or `pymechanical` for an independent Mechanical process. Workbench keeps the project and its Mechanical system in one session. There is no automatic backend switch after an uncertain operation.

`mode="persistent"` returns a session identifier for subsequent `session={"action":"reuse","id":...}` recipes. `mode="batch"` closes only a newly created owned session. A reused session remains open. Use an explicit session `close` recipe to close an owned session. Objects are scoped to their session and model generation. Project replacement, trusted scripts, recognized structural calls and explicit `invalidate_refs` advance that generation. For other API calls that replace object collections, set `invalidate_refs=true` and query fresh references afterward.

Operations include `discover`, `inspect`, `find`, `get`, `set`, `call`, `construct`, project operations and trusted `script` files. `engineering_data` is dispatched through the owning Workbench session. Public member paths are resolved without evaluating user expressions. Quantity, enum, selection, table, local-reference and object-reference values have explicit `$type` encodings in the schema.

Within `engineering_data`, `EngineeringData` denotes the project's container
and `EngData` denotes the public Workbench command namespace. Material-library
export uses `EngData.WriteMaterials` with material references and a typed output
path, following the [official Engineering Data interface](https://ansyshelp.ansys.com/public/Views/Secured/corp/v261/en/wb2_js/ContainerName37.html).

Inputs and outputs use relative paths or typed paths with an explicit input/output role. Output overwriting is opt-in. Larger responses are written as artifacts. File scripts retain the trusted-script execution boundary described in SECURITY.md and do not constitute an OS sandbox.

The example `examples/mechanical.example.json` starts an empty project, reads the model, sets a mesh quantity and reads it back. It does not solve a physical model. Use a new output path when rerunning an example whose project already exists.

## Jobs and failure semantics

`run_workflow` remains synchronous. `start_workflow` launches an owned background worker and returns `run_id`; `get_workflow_status(run_id)` reconciles the persisted record with the worker's process creation identity. `cancel_workflow(run_id)` requests cancellation. A missing worker with no terminal result is an interrupted, unknown outcome, not a completed job.

Keep the same allowed-root/default-workspace configuration when reconnecting;
job and actor registries belong to that local workspace.

Mechanical and DPF recipes are not automatically retried. Reconcile an observation timeout against the same session/request before deciding what to do next. Cancellation and forced termination apply only to owned work. A product without a public graceful-stop interface cannot be reported as gracefully cancelled merely because its process exited.

Use `get_mechanical_request_status(request_id, session_id=...)` for a known
product session, or `worker_id=...` when the original response only identified
an execution worker. A workflow interrupted by an observation timeout retains
its original handles. Once that product request is reconciled, the workflow is
`needs_attention`, not silently resumed past its unexecuted steps.

Owned actors have a private persistent registry. A new stdio server attaches to
the same authenticated actor after checking its process creation identity; a
missing actor is never recreated automatically. Windows workers are launched
independently of the stdio client's cleanup Job so closing one connection does
not implicitly cancel a submitted job.

On Windows, elevation is requested only when necessary or explicitly required. The normal MCP process remains unelevated. A separately authenticated local execution worker owns elevated sessions; refusal returns a diagnostic. The service does not change compatibility flags, UAC settings or licensing.

## DPF

The `dpf` step accepts a version `1` recipe. It runs in an owned supervised worker. Local in-process execution is the default; local gRPC retains the product's transport-security defaults. Result files, export paths and generic operator path pins must pass the same allowed-root checks. Generic operator file inputs use explicit `$path` values.

Metadata, named selections, mesh/time scoping, result fields, generic operators and CSV/JSON exports are supported. The installed readers determine usable formats. In particular, LS-DYNA reader availability does not prove AUTODYN file support.

A gRPC deployment without the required certificates returns an authentication
diagnostic. In-process success does not claim gRPC transport acceptance, and the
adapter does not fall back to insecure transport.

## Validation and publication

Unit tests and fake-product tests exercise contracts, serialization and failure handling. Real product tests are opt-in and run against isolated generated projects. See validation.md for the current public evidence summary. Original logs, installation paths, project files and solver outputs stay in the private run directory.

Before publication, run `ansys-mcp-check-publication` against the tracked tree, staged changes, distributions and new commit metadata. A private fingerprint file may add machine-specific patterns; never commit that file. Published examples and reports use placeholders and synthetic inputs. Existing Git history is not anonymized by this check.

The fingerprint file is local JSON with a `values` array of strings. Reports
emit relative locations and categories, never the matched values. The checker
reads staged Git blobs, archive contents/headers and raw new-commit headers;
`.gitignore` alone is not a publication check. Build from the reviewed index
snapshot, then pass each actual wheel/sdist with `--artifact`. Verify a new
commit with `--commit`, `--expected-name` and `--expected-email`; both author
and committer must use the verified public login and GitHub noreply address.
