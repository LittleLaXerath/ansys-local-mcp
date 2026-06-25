# Security Policy

This project exposes local command execution through MCP. Treat it as a local
automation adapter, not as a remote service.

## Safe Use

- Run it only on machines and MCP clients you trust.
- Always restrict `ANSYS_MCP_ALLOWED_ROOTS` to the smallest workspace needed.
- Do not expose the MCP stdio process through a network bridge to untrusted users.
- Review workflows before running them. `validate_workflow` checks schema, paths,
  command IDs, and variables without launching Ansys.
- This project does not bypass, modify, or manage Ansys licensing.

## Reporting Issues

For public repositories, report security issues privately when possible. If
private reporting is not enabled, open a minimal issue without including secrets,
license files, proprietary models, or sensitive project data.
