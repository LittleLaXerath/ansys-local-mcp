# Ansys 本地 MCP 服务器

后台权限诊断、默认 dry-run 的日志维护和可核查验收回执见[运行维护说明](docs/operations.md)。
`query_capabilities(kind="workflow")` 查询工作流证据，默认 `kind="api"` 仍查询公开 API 目录。
工具数量保持 17 个。

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
.\.venv\Scripts\ansys-local-mcp.exe
```

旧入口 `.\.venv\Scripts\python.exe .\ansys_mcp_server.py` 继续保留。
从未激活虚拟环境的 MCP 客户端启动时，将 `command` 设为该环境中
`ansys-local-mcp.exe` 的完整路径。示例短命令假定客户端 PATH 已包含此入口。

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

客户端可读取 MCP 资源 `ansys://schema/workflow` 和 `ansys://schema/workbench-recipe`，获取完整字段规则；新增 Mechanical、DPF recipe 也有独立 schema 资源。不能读取资源的客户端可调用 `inspect_runtime(include_schema=True)`。Schema 直接由 Pydantic 模型生成；保留原有 12 个工具，并增加 5 个任务、目录与请求状态工具。

## MCP 工具

稳定的通用工具：

- `inspect_runtime`
- `validate_workflow`
- `run_workflow`
- `start_workflow`
- `get_workflow_status`
- `cancel_workflow`
- `query_capabilities`
- `get_mechanical_request_status`

Mechanical 会话、值协议与后端说明见[适配文档](docs/mechanical.zh-CN.md)，产品级证据见[验收记录](docs/validation.md)。

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
- `mechanical`：执行版本化 Mechanical recipe（对象、会话与工程操作）。
- `dpf`：执行版本化本地 DPF recipe 并按范围导出结果。
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

产品脚本生成的文件可通过 `produces` 声明，例如 `"produces": [{"path": "output/model.def", "required": true}]`。验证时允许后续步骤使用这些尚未生成的文件；实际使用前会重新检查，必需产物也会在生成步骤结束后立即检查。文件写入、复制目标和已知命令输出参数会自动识别。没有声明来源的缺失输入、未知步骤或对尚未执行步骤的引用会在任何写入前报错。

产品进程会收到 `ANSYS_MCP_WORKDIR` 和 `ANSYS_MCP_RUN_DIR` 环境变量，脚本可据此定位文件，无需依赖编辑器自身的当前目录。SpaceClaim 的路径参数会正确解析，`/Headless=True` 等开关保持原样。

`validate_workflow(validation_mode="schema")` 只验证可移植结构，不要求本机存在 Ansys executable。默认 `runtime` 模式还会检查发现到的运行环境。两种验证模式都不会启动 Ansys，也不会执行工作流写文件。

## Workbench Recipe

`workbench` step 必须且只能提供 `recipe` 或 `recipe_path` 之一。Recipe 使用 `store_as` 保存 Workbench 对象句柄，再通过 `target` 引用：

```json
{
  "id": "suite",
  "type": "workbench",
  "mode": "batch",
  "recipe": {
    "project": {
      "open_path": "inputs/project.wbpj",
      "save_path": "outputs/project.wbpj"
    },
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

使用 `store_as` 保存的可序列化结果会返回到 `outputs.values`，例如 `${steps.setup.outputs.values.physics_status}`。产品对象句柄仅在当前 recipe 内可用。成功状态必须同时满足最终完成标记和全部预期动作记录，单凭退出码为零不足以确认成功。

示例包括 [SpaceClaim 几何工作流](examples/workbench_recipe.example.json)、[SpaceClaim 直接通道](examples/spaceclaim_direct.example.json)和 [CFX 已有工程设置](examples/cfx_workbench.example.json)。SpaceClaim 脚本创建矩形流体体积、命名入口/出口/壁面、校验体积并导出原生文件、STEP 和测量结果。CFX 示例修改已有域的湍流设置并返回 `PhysicsStatus`，不负责生成网格或求解。运行前提和验证范围见[示例说明](examples/README.md)。

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

默认自动测试不会启动 Workbench、Fluent、CFX、SpaceClaim、Mechanical、MAPDL、System Coupling 或任何求解器。真实产品及 UAC 测试需要显式启用，验收脚本使用隔离的新工程。
