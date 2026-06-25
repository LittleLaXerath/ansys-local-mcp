# Ansys Local MCP / Ansys 本地 MCP

Experimental local MCP adapter for Ansys workflows.

用于 Ansys 本地工作流编排的实验性 MCP 适配器。

This project is not affiliated with, endorsed by, or sponsored by Ansys.

本项目不是 Ansys 官方项目，也未获得 Ansys 背书、赞助或认可。

## Documents / 文档

| Topic | 中文 | English |
| --- | --- | --- |
| README | [中文说明](README.zh-CN.md) | [English README](README.en.md) |
| Security | [中文安全提示](SECURITY.zh-CN.md) | [English Security Notes](SECURITY.en.md) |

## Short Overview / 简要说明

This repository provides a small local MCP server with a workflow engine,
command whitelist, Workbench-first orchestration, and direct product channels
for Fluent, CFX, SpaceClaim, Mechanical, MAPDL, System Coupling, and Ansys
Python.

这个仓库提供一个小型本地 MCP 服务器，包含工作流引擎、命令白名单、Workbench 优先编排，以及面向 Fluent、CFX、SpaceClaim、Mechanical、MAPDL、System Coupling 和 Ansys Python 的直接产品通道。

## Install / 安装

```powershell
git clone https://github.com/LittleLaXerath/ansys-local-mcp.git
cd ansys-local-mcp
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

Run / 运行：

```powershell
ansys-local-mcp
```

## Safety First / 安全优先

This is a local command-execution MCP server. Review workflows before running
them, restrict `ANSYS_MCP_ALLOWED_ROOTS`, and do not expose the server to
untrusted clients.

这是一个本地命令执行 MCP 服务器。运行前请审查工作流，限制 `ANSYS_MCP_ALLOWED_ROOTS`，不要把服务器暴露给不受信任的客户端。

See / 参见：[Security / 安全提示](SECURITY.md)
