# Security Notes

[Main page](README.md) | [中文安全提示](SECURITY.zh-CN.md)

This project exposes local command execution through MCP. Treat it as a local
automation adapter, not as a remote service.

## Safe Use

- Run it only on machines and MCP clients you trust.
- Always restrict `ANSYS_MCP_ALLOWED_ROOTS` to the smallest workspace needed.
- Do not expose the MCP stdio process through a network bridge to untrusted users.
- Review workflows before running them. `validate_workflow` checks schema, paths,
  command IDs, and variables without launching Ansys.
- This project does not bypass, modify, or manage Ansys licensing.

## Paths and Files

- Do not commit license files, commercial models, customer data, solver results,
  or private project files.
- Do not point `ANSYS_MCP_ALLOWED_ROOTS` at an entire disk, user profile, or
  system directory.
- Run logs are written under `.ansys_mcp/runs/<run_id>/`; this directory should
  not be committed.
- Workflow inputs, outputs, and artifacts should stay inside allowed roots.

## Command Execution

- External processes must use whitelisted `command_id` values. Do not attempt
  arbitrary shell command composition.
- For unfamiliar workflows, run `validate_workflow` before `run_workflow`.
- GUI products may open local windows or read local projects. Run them only for
  trusted projects.
- Do not connect this server to clients where prompts or tool calls cannot be
  audited.

## Reporting Issues

For public repositories, report security issues privately when possible. If
private reporting is not enabled, open a minimal issue without including secrets,
license files, proprietary models, customer data, full logs, or sensitive paths.
