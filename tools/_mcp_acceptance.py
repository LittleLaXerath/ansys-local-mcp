"""Shared bounded stdio client for explicit product acceptance runners."""

from __future__ import annotations

import json
import os
import sys
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from ansys_mcp.config import merge_local_no_proxy


@asynccontextmanager
async def mcp_client(directory: Path, timeout_seconds: float = 1820):
    project = Path(__file__).resolve().parents[1]
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(project / "ansys_mcp_server.py")],
        env=merge_local_no_proxy(os.environ),
    )
    with (directory / "mcp.stderr.log").open("a", encoding="utf-8") as log:
        async with stdio_client(params, errlog=log) as (read, write):
            async with ClientSession(
                read, write, read_timeout_seconds=timedelta(seconds=timeout_seconds)
            ) as session:
                await session.initialize()
                yield session


def tool_value(result):
    if result.isError:
        raise RuntimeError("MCP rejected the request: " + str(result.content))
    if result.structuredContent:
        return result.structuredContent
    return json.loads(
        next(block.text for block in result.content if block.type == "text")
    )
