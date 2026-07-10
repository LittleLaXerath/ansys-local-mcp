# Ansys Local MCP / Ansys 本地 MCP

Experimental local MCP adapter for reliable, data-driven Ansys automation.

用于可靠、数据驱动 Ansys 自动化的实验性本地 MCP 适配器。

This project is not affiliated with, endorsed by, or sponsored by Ansys.

本项目不是 Ansys 官方项目，也未获得 Ansys 背书、赞助或认可。

## Documents / 文档

| Topic | 中文 | English |
| --- | --- | --- |
| README | [中文说明](README.zh-CN.md) | [English README](README.en.md) |
| Security | [中文安全提示](SECURITY.zh-CN.md) | [English Security Notes](SECURITY.en.md) |
| Examples | [工作流示例](examples/README.md) | [Workflow examples](examples/README.md) |

## v0.2 Overview / v0.2 概览

- Strict Workflow v1 schemas and fail-closed path boundaries.
- Automatic local Ansys discovery without license or signature gating.
- Structured Workbench recipes for Fluent, CFX, Mechanical, and SpaceClaim.
- Direct product commands and generic PyFluent primitives.
- Retry policy, step outputs, complete logs, process-tree timeouts, and atomic manifests.

- 严格的 Workflow v1 Schema 和失败即关闭的路径边界。
- 自动发现本机 Ansys，不使用许可证或签名状态拦截执行。
- 通过结构化 Workbench recipe 驱动 Fluent、CFX、Mechanical 和 SpaceClaim。
- 保留直接产品命令与通用 PyFluent primitives。
- 支持重试、步骤输出、完整日志、进程树超时和原子 manifest。

## Install / 安装

```powershell
git clone https://github.com/LittleLaXerath/ansys-local-mcp.git
cd ansys-local-mcp
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
ansys-local-mcp
```

## Safety / 安全

This server executes trusted local automation. Restrict
`ANSYS_MCP_ALLOWED_ROOTS`, review workflows and product scripts, and do not
expose it to untrusted clients. The server does not inspect, bypass, modify, or
manage Ansys licensing. Signature reports are optional and never gate runs.

该服务器会执行受信任的本地自动化。请限制 `ANSYS_MCP_ALLOWED_ROOTS`，审查工作流与产品脚本，不要暴露给不受信任的客户端。服务器不会检查、绕过、修改或管理 Ansys 授权；签名报告仅供主动诊断，永不拦截运行。

See / 参见：[Security / 安全提示](SECURITY.md)
