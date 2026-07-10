# Ansys 本地 MCP 服务器

[主页面](README.md) | [English README](README.en.md)

这是一个实验性的 Windows 本地 MCP 服务器，通过严格 JSON 工作流、结构化 Workbench recipe、直接产品命令和通用 PyFluent 操作编排 Ansys。

本项目不是 Ansys 官方项目，也未获得 Ansys 背书、赞助或认可。仓库不包含 Ansys 软件、许可证、求解器或专有工程数据。使用时需要本机存在可工作的 Ansys 安装；产品授权和合规由用户与 Ansys 产品自身负责。本 MCP 不检查、绕过、修改或管理授权。

## 安装

```powershell
git clone https://github.com/LittleLaXerath/ansys-local-mcp.git
cd ansys-local-mcp
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

运行控制台入口：

```powershell
ansys-local-mcp
```

旧入口 `python .\ansys_mcp_server.py` 继续保留。

## MCP 客户端配置

服务器按以下顺序发现安装：`ANSYS_MCP_ROOT`、匹配版本的 `AWP_ROOT<tag>`、其他 `AWP_ROOT*`、Windows 注册表、固定磁盘安装目录。通常只需要配置工作目录边界：

```json
{
  "mcpServers": {
    "ansys-local": {
      "command": "ansys-local-mcp",
      "args": [],
      "env": {
        "ANSYS_MCP_ALLOWED_ROOTS": "C:\\Users\\YOUR-NAME\\ansys-work"
      }
    }
  }
}
```

如果显式设置的 `ANSYS_MCP_ROOT` 无效，服务器返回 `CONFIG_ERROR`，不会静默改用其他安装。若未设置 allowed roots，则仅允许服务器启动目录，并返回 warning。

## Schema 是什么

Schema 只是工作流 JSON 的格式契约：哪些字段必填、允许哪些值、ID 能使用哪些字符、不同 step 有哪些选项。它与 Ansys 授权或程序数字签名无关。

Workflow v1 会在启动任何 Ansys 程序之前拒绝未知字段、空 steps、重复 step ID、路径穿越 ID、非正数超时和格式错误的产品 recipe。

## MCP 工具

稳定的通用工具：

- `inspect_runtime`
- `validate_workflow`
- `run_workflow`

保持兼容的旧工具：

- `get_ansys_install_info`
- `check_ansys_security_surface`
- `launch_workbench`
- `run_workbench_journal`
- `run_mapdl_input`
- `run_fluent_journal`
- `run_system_coupling`
- `run_ansys_python_script`
- `run_pyfluent_recipe`

`check_ansys_security_surface` 仅在用户主动调用时报告签名。安装发现、验证和执行不会调用它，其结果永远不会阻止工作流。

## Workflow v1

Step 类型：

- `command`：通过参数列表运行白名单产品命令。
- `python_script`：使用 Ansys Python 运行允许目录内的 `.py`。
- `pyfluent`：执行严格定义的通用 PyFluent 操作。
- `workbench`：编译并执行结构化 Workbench recipe。
- `file`：检查、读取、写入或复制文件。
- `assert`：断言路径是否存在。
- `collect`：收集必需或可选产物。

每个 step 都可以定义重试：

```json
{
  "id": "solve",
  "timeout_seconds": 3600,
  "retry": {
    "max_attempts": 2,
    "backoff_seconds": 2,
    "retry_on_codes": ["PROCESS_TIMEOUT", "PROCESS_NONZERO"]
  }
}
```

每个 step 都返回 `outputs`。后续步骤可使用 `${steps.<id>.outputs.<field>}`；原有 `${variable}` 变量替换继续支持。

`validate_workflow(validation_mode="schema")` 只验证可移植结构，不要求本机存在 Ansys executable。默认 `runtime` 模式还会检查发现到的运行环境。两种验证模式都不会启动 Ansys，也不会执行工作流写文件。

## Workbench Recipe

`workbench` step 必须且只能提供 `recipe` 或 `recipe_path` 之一。Recipe 使用 `store_as` 保存 Workbench 对象句柄，再通过 `target` 引用：

```json
{
  "id": "suite",
  "type": "workbench",
  "mode": "batch",
  "recipe": {
    "actions": [
      {"op": "get_system", "name": "CFX", "store_as": "cfx"},
      {
        "op": "get_container",
        "target": "cfx",
        "component_name": "Setup",
        "store_as": "setup"
      },
      {
        "op": "send_command",
        "target": "setup",
        "script_path": "scripts/setup.ccl"
      }
    ]
  }
}
```

Actions：`get_template`、`create_system`、`get_system`、`get_component`、`get_container`、`call`、`get_attr`、`set_attr`、`send_command`、`run_script`。嵌套参数可以使用 `{"$ref": "name"}` 引用句柄。

产品脚本默认必须是 allowed roots 内的文件。只有显式设置 `ANSYS_MCP_ALLOW_INLINE_CODE=1` 后才允许内嵌 `code`；内嵌内容会先写进本次 run 目录，便于审计。

完整示例见 [Workbench 全套 recipe](examples/workbench_recipe.example.json)。

## PyFluent

支持的通用 primitives：

- `read`、`write`
- `set_state`、`set_constant`、`get_state`
- `call`、`tui`
- `iterate`、`calculate`
- `create`、`compute_reports`、`inspect_path`

点路径支持属性、整数索引、带引号的集合键和候选路径回退。旧的 `mesh_check`、`set_zone_type`、`create_force_report` 继续作为兼容别名使用，但会返回弃用 warning。

## 结果与日志

`run_workflow` 返回 `ok`、`run_id`、`status`、`steps`、`artifacts`、`diagnostics`、输出尾部和耗时。每个 step 包含 `outputs` 与完整 stdout/stderr 日志路径。

每次运行写入：

```text
<workdir>\.ansys_mcp\runs\<run_id>\
```

`manifest.json` 在 run 创建时、每个 step 完成后和最终结束时原子更新。超时会终止受监督的 Windows 进程树。required artifact 缺失会返回 `ARTIFACT_MISSING` 并使 run 失败。

## 安全边界

Allowed roots 限制 MCP 服务器解析和使用的路径，但它不是操作系统沙箱。受信任的 Python、Workbench、Fluent、CFX、Mechanical 或 SpaceClaim 脚本仍拥有对应产品进程的系统权限。请审查脚本，只连接可信 MCP 客户端。详见 [中文安全提示](SECURITY.zh-CN.md)。

## 验证

```powershell
python -m unittest discover tests
python -m compileall ansys_mcp ansys_mcp_server.py
```

自动测试不会启动 Workbench、Fluent、CFX、SpaceClaim、Mechanical、MAPDL、System Coupling 或任何求解器。
