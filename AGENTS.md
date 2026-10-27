# AGENTS.md — repository conventions

Concise, public conventions for coding agents working in this repository.
Task-specific plans are intentionally kept outside the repository.

## Layout

- `ansys_mcp/` — the MCP server package (tools, workflow engine, adapters).
- `tests/` — unit and ordinary-process tests; never starts an Ansys product.
- `tools/` — explicit, opt-in product acceptance runners.
- `examples/` — schema-checked workflow templates and trusted scripts.
- `docs/` — validation evidence, testing groups, known issues.

## Commands (Windows, repository `.venv`)

```powershell
.\.venv\Scripts\python.exe -m unittest discover tests
.\.venv\Scripts\python.exe -m compileall ansys_mcp ansys_mcp_server.py tools
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m ansys_mcp.publication --root .
```

## Gates

1. Unit tests and CI never start Workbench, Fluent, CFX, Mechanical, MAPDL, a
   solver or UAC. Product acceptance runs only when its opt-in variable is set.
2. Validate every workflow before execution; schema mode needs no product.
3. All server-managed paths stay inside `ANSYS_MCP_ALLOWED_ROOTS`.
4. Never bypass, inspect for, or manage Ansys licensing; never change UAC,
   compatibility flags or security settings.
5. Do not replay a product request whose outcome is unknown; observe the
   original request identity instead.

## Changing public interfaces

A change to a tool, workflow step type, recipe field or MCP resource must update
the Pydantic schema, the runtime registration test, the examples, both READMEs
and the tests in the same change. Counts are asserted from the registered
objects, not from prose.

## Evidence honesty

Schema validation, unit tests, catalog presence and a zero exit code do not
prove a product run or a numerical result. Unrun acceptance stays `pending`.
See `docs/testing.md`, `docs/known-issues.md` and `docs/validation.md`.
