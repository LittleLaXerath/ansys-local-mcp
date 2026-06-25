# Ansys 本地 MCP 服务器

[主页面](README.md) | [英文说明](README.en.md)

这是一个实验性的本地 MCP 服务器，用来通过 Workbench 优先流程和直接产品命令编排 Ansys 工作流。

本项目不是 Ansys 官方项目，也未获得 Ansys 背书、赞助或认可。本项目不包含 Ansys 软件、许可证、求解器、官方示例或任何专有工程数据。使用前需要你本机已经安装可用的 Ansys，并具备有效授权。

## 能做什么

- 暴露少量稳定的 MCP 工具，用于本地 Ansys 自动化。
- 优先使用 JSON 工作流，而不是不断新增一次性 MCP 工具。
- 支持通过 `RunWB2` 走 Workbench 优先编排。
- 支持 Fluent、CFX、SpaceClaim、Mechanical、MAPDL、System Coupling 和 Ansys Python 的直接命令通道。
- 在执行前验证工作流结构、允许路径、命令编号和变量替换。

## 安装

```powershell
git clone https://github.com/LittleLaXerath/ansys-local-mcp.git
cd ansys-local-mcp
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

控制台入口：

```powershell
ansys-local-mcp
```

旧入口仍然保留：

```powershell
python .\ansys_mcp_server.py
```

## MCP 客户端配置

标准输入输出模式示例：

```json
{
  "mcpServers": {
    "ansys-local": {
      "command": "ansys-local-mcp",
      "args": [],
      "env": {
        "ANSYS_MCP_ROOT": "C:\\Program Files\\ANSYS Inc\\v261",
        "ANSYS_MCP_VERSION_TAG": "261",
        "ANSYS_MCP_ALLOWED_ROOTS": "C:\\Users\\YOUR-NAME\\ansys-work"
      }
    }
  }
}
```

请根据你的机器调整 `ANSYS_MCP_ROOT` 和 `ANSYS_MCP_ALLOWED_ROOTS`。

## 工具

通用工具：

- `inspect_runtime`
- `validate_workflow`
- `run_workflow`

兼容旧名称的工具：

- `get_ansys_install_info`
- `check_ansys_security_surface`
- `launch_workbench`
- `run_workbench_journal`
- `run_mapdl_input`
- `run_fluent_journal`
- `run_system_coupling`
- `run_ansys_python_script`
- `run_pyfluent_recipe`

这些兼容工具会在内部生成单步工作流，并返回和 `run_workflow` 相同结构的结果。

## 命令白名单

外部进程只能通过白名单命令编号执行，并且只使用参数列表调用，不拼接任意 shell 字符串。

- `ansys_python`
- `fluent`
- `cfx_pre`
- `cfx_solve`
- `cfx_post`
- `cfd_post`
- `runwb2`
- `runwb2_batch`
- `mapdl`
- `mechanical`
- `spaceclaim`
- `system_coupling`

## 工作流版本一

只验证、不启动 Ansys 的例子：

```json
{
  "version": "1",
  "workdir": ".",
  "steps": [
    {
      "id": "check_project_dir",
      "type": "assert",
      "path": ".",
      "exists": true
    }
  ]
}
```

步骤类型：

- `command`：运行白名单命令编号和参数列表。
- `python_script`：使用 Ansys Python 运行允许路径内的脚本。
- `pyfluent`：启动 PyFluent 并执行通用操作。
- `file`：执行 `exists`、`read_text`、`write_text` 或 `copy`。
- `assert`：检查路径是否存在。
- `collect`：记录产物，不启动 Ansys。

PyFluent 操作：

- `read`、`write`
- `set_state`、`set_constant`、`get_state`
- `call`、`tui`
- `iterate`、`calculate`
- `create`、`compute_reports`
- `inspect_path`

PyFluent 点路径支持集合索引和候选路径回退：

```json
{
  "op": "set_state",
  "path": [
    "settings.setup.models.viscous.model",
    "settings.setup.models.viscous"
  ],
  "value": "k-epsilon"
}
```

## Workbench 优先通道

当任务跨越 Fluent、CFX、SpaceClaim、Mechanical、Meshing 或共享工程状态时，优先使用 Workbench journal。

模板：

`examples/workbench_first_suite.example.json`

把 `examples/workbench_noop.wbjn` 替换为你工程中真实录制或生成的 Workbench journal。

## 直接产品通道

直接通道适合单产品批处理、诊断和定向自动化。

模板：

`examples/direct_channels.example.json`

对 SpaceClaim、Mechanical 这类图形界面产品，可以设置 `"detach": true`，让工作流在启动产品后返回。

## 结果和诊断

`run_workflow` 返回：

- `ok`
- `run_id`
- `stage`
- `run_dir`
- `steps`
- `artifacts`
- `diagnostics`
- `stdout_tail`
- `stderr_tail`
- `elapsed_s`

每次运行都会写入：

`<workdir>\.ansys_mcp\runs\<run_id>\manifest.json`

标准诊断代码：

- `VALIDATION_ERROR`
- `PATH_DENIED`
- `EXECUTABLE_MISSING`
- `PROCESS_TIMEOUT`
- `PROCESS_NONZERO`
- `PYFLUENT_IMPORT_ERROR`
- `PYFLUENT_OPERATION_ERROR`
- `ARTIFACT_MISSING`

编写新工作流时，建议先调用 `validate_workflow`，再调用 `run_workflow`。

## 安全提示

这是一个本地命令执行 MCP 服务器。请限制 `ANSYS_MCP_ALLOWED_ROOTS`，运行前审查工作流，不要把服务器暴露给不受信任的客户端。更多内容见 [中文安全提示](SECURITY.zh-CN.md)。

## 验证

```powershell
python -m unittest discover tests
python -m compileall ansys_mcp ansys_mcp_server.py
```

测试不会启动 Fluent、Workbench、CFX、SpaceClaim、Mechanical、MAPDL 或 System Coupling。
