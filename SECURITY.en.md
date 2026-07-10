# Security Notes

[Main page](README.md) | [中文安全提示](SECURITY.zh-CN.md)

This project exposes trusted local command and product-script execution through
MCP. Treat it as a local automation adapter, not a remote service.

## Trust Boundary

- Run it only with machines, projects, prompts, and MCP clients you trust.
- Do not expose stdio through a network bridge or public endpoint.
- Review workflows and referenced scripts before calling `run_workflow`.
- Prefer `validate_workflow(validation_mode="schema")` when inspecting portable
  workflows and `runtime` before execution.

## Paths Are Not a Sandbox

`ANSYS_MCP_ALLOWED_ROOTS` constrains paths resolved by the MCP server. Invalid
configured roots fail closed. This boundary does not sandbox Python or Ansys
product processes: a trusted script can perform any action permitted to its
process account.

Do not allow an entire drive, user profile, Windows directory, or shared project
tree unless that access is genuinely required.

## Commands and Inline Code

- External processes use known `command_id` values and argv lists.
- Batch-wrapper metacharacters are rejected.
- `ansys_python` accepts allowed `.py` files only; `-c`, `-m`, and stdin modes
  are disabled.
- Workbench `send_command` and `run_script` use allowed files by default.
- `ANSYS_MCP_ALLOW_INLINE_CODE=1` explicitly enables inline product code. It is
  materialized in the run directory for review and auditing.

## Installation, Signatures, and Licensing

Runtime discovery uses paths, environment variables, registry metadata, and
executable existence. It does not inspect license state or classify installation
source. `check_ansys_security_surface` is a user-invoked informational signature
report only; mismatched, missing, or unverifiable signatures do not block runs.

The server does not bypass, modify, supply, or manage Ansys licensing. Users are
responsible for applicable software terms and laws.

## Logs and Data

Runs write manifests and full step logs under `.ansys_mcp/runs/<run_id>/`.
Do not publish logs containing sensitive paths, customer data, proprietary
models, credentials, license files, or solver output. This directory is ignored
by Git.

## Reporting

Use private vulnerability reporting when available. Otherwise open a minimal
issue without secrets, proprietary files, customer data, or complete logs.
