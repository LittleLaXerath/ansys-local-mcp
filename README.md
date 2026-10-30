# Ansys Local MCP / Ansys 本地 MCP

让支持 MCP 的 AI 客户端调用你电脑上的 Ansys。可以通过 Workbench 管理工程，也可以直接运行 Fluent、CFX、Mechanical 和 SpaceClaim 的脚本。

Connect an MCP-compatible AI client to Ansys on your own computer. Use Workbench
to manage projects, or run Fluent, CFX, Mechanical and SpaceClaim scripts directly.

这个项目解决的是本地调用、工作流编排和结果读回，不是把所有工程判断交给 AI。
计算前需要明确模型、单位、边界条件和验收标准；计算后仍要检查结果。

The server handles local execution, workflow steps and result readback. It does
not replace engineering judgment: define the model and acceptance criteria first,
then check what the solver actually produced.

This project is not affiliated with, endorsed by, or sponsored by Ansys.

本项目不是 Ansys 官方项目，也未获得 Ansys 背书、赞助或认可。

## Documents / 文档

| Topic | 中文 | English |
| --- | --- | --- |
| README | [中文说明](README.zh-CN.md) | [English README](README.en.md) |
| Security | [中文安全提示](SECURITY.zh-CN.md) | [English Security Notes](SECURITY.en.md) |
| Examples | [工作流示例](examples/README.md) | [Workflow examples](examples/README.md) |
| Mechanical | [适配、会话与任务](docs/mechanical.zh-CN.md) | [Adapter, sessions and jobs](docs/mechanical.md) |
| Mechanical 基础验收 | [范围与逐项状态](docs/mechanical-basics.md) | [Basic acceptance scope and status](docs/mechanical-basics.md) |
| Fluent | [持久会话、运行时与监控](docs/fluent.md) | [Sessions, runtime and monitoring](docs/fluent.md) |
| Validating | [分级验收记录](docs/validation.md) | [Validation evidence](docs/validation.md) |
| Testing | [测试分组与基线](docs/testing.md) | [Test groups and baseline](docs/testing.md) |
| Known issues | [已知问题](docs/known-issues.md) | [Known issues](docs/known-issues.md) |
| CFX step | [CFX 步骤](docs/cfx.md) | [CFX step](docs/cfx.md) |

## v0.3 Mechanical and DPF / Mechanical 与 DPF

All nine planned Mechanical basic benchmarks have scoped Workbench/DPF evidence;
this is not full API or extension coverage. [Scope and results](docs/mechanical-basics.md).

九项计划内 Mechanical 基础验收已有对应 Workbench/DPF 证据，不等于全 API 或专用扩展覆盖。
[查看范围与结果](docs/mechanical-basics.md)。

- Workbench-first and independent Mechanical backends with typed object operations.
- Persistent sessions, background jobs, cancellation and request observation.
- Complete installed public-stub inventory with separate runtime and validation status.
- Local DPF recipes with scoped result fields and CSV/JSON exports.
- Publication checks for source, distributions and new commit metadata.

- 默认 Workbench 及独立 Mechanical 后端，提供统一对象和值协议。
- 持久会话、后台任务、取消及未知请求状态查询。
- 从安装的官方 stubs 建立全量目录，单独记录运行时及验收状态。
- 本机 DPF 后处理、范围选择及 CSV/JSON 导出。
- 源码、分发包和新提交元数据发布检查。

Catalog completeness does not imply that every analysis is installed, licensed
or numerically validated. See the validation report for measured support.

目录完整不等于所有分析均已安装、获得许可或通过数值验证；实际支持以验收报告为准。

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

需要 Windows、Python 3.11 或更新版本，以及可工作的本机 Ansys 安装。首次使用可在仓库目录运行：

You need Windows, Python 3.11 or newer, and a working local Ansys installation.
For a first-time setup, run:

```powershell
git clone https://github.com/LittleLaXerath/ansys-local-mcp.git
cd ansys-local-mcp
python tools/setup_local.py
.\.venv\Scripts\python.exe -m pip install -e ".[fluent]"
```

第一步创建独立环境和 `workspace` 目录，并生成
`mcp_client_config.json`。把其中的服务器条目加入自己的 MCP 客户端即可；配置使用你本机的完整路径，
不需要照抄维护者的环境。先完成第二步依赖安装（含 PyFluent），再连接客户端。
已有环境或配置不会被覆盖，也不会自动启动 Ansys。

Setup creates a virtual environment and workspace, and writes `mcp_client_config.json`.
The second command installs dependencies including PyFluent. After it succeeds, add the server
entry to your MCP client. The generated paths belong to your machine; existing
environments/configurations are not overwritten, and no solver is started.

已有安装、需要自选目录或遇到权限问题时，见[中文说明](README.zh-CN.md) / [English README](README.en.md)。
首次连接先调用 `inspect_runtime` 检查环境；生成配置不等于产品求解已通过验收。

## Safety / 安全

This server executes trusted local automation. Restrict
`ANSYS_MCP_ALLOWED_ROOTS`, review workflows and product scripts, and do not
expose it to untrusted clients. The server does not inspect, bypass, modify, or
manage Ansys licensing. Signature reports are optional and never gate runs.

该服务器会执行受信任的本地自动化。请限制 `ANSYS_MCP_ALLOWED_ROOTS`，审查工作流与产品脚本，不要暴露给不受信任的客户端。服务器不会检查、绕过、修改或管理 Ansys 授权；签名报告仅供主动诊断，永不拦截运行。

See / 参见：[Security / 安全提示](SECURITY.md)
