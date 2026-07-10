# 安全提示

[主页面](README.md) | [English Security Notes](SECURITY.en.md)

本项目通过 MCP 暴露受信任的本地命令和产品脚本执行能力。请把它当作本机自动化适配器，不要当作远程服务。

## 信任边界

- 只在你信任的机器、工程、提示词和 MCP 客户端上运行。
- 不要通过网络桥接或公开端口暴露 stdio 服务。
- 调用 `run_workflow` 前审查工作流和引用的脚本。
- 检查可移植工作流时优先使用 `validate_workflow(validation_mode="schema")`，执行前再使用 `runtime`。

## 路径边界不是沙箱

`ANSYS_MCP_ALLOWED_ROOTS` 只限制 MCP 服务器解析的路径；配置中只要有无效目录就会失败关闭。它不会把 Python 或 Ansys 产品进程变成沙箱：受信任脚本仍可执行当前进程账户允许的操作。

除非确实需要，不要把整个磁盘、用户主目录、Windows 目录或共享工程树加入 allowed roots。

## 命令与内嵌代码

- 外部进程只能使用已知 `command_id` 和参数列表。
- batch wrapper 会拒绝命令控制字符。
- `ansys_python` 只接受允许目录内的 `.py`，禁用 `-c`、`-m` 和 stdin 模式。
- Workbench `send_command` 与 `run_script` 默认只使用允许目录内的文件。
- `ANSYS_MCP_ALLOW_INLINE_CODE=1` 会显式开启内嵌产品代码；内容会先写进 run 目录，便于检查和审计。

## 安装、签名与授权

运行环境发现只使用路径、环境变量、注册表信息和 executable 是否存在，不检查许可证状态，也不区分安装来源。`check_ansys_security_surface` 只是用户主动调用的信息报告；签名不匹配、缺失或无法验证都不会阻止执行。

服务器不会绕过、修改、提供或管理 Ansys 授权。用户需要自行遵守适用的软件条款和法律。

## 日志与数据

每次运行会在 `.ansys_mcp/runs/<run_id>/` 写入 manifest 和完整 step 日志。不要公开包含敏感路径、客户数据、专有模型、凭据、许可证文件或求解结果的日志。该目录已被 Git 忽略。

## 报告问题

如条件允许，请使用私密漏洞报告。否则只提交最小化 issue，不要包含密钥、专有文件、客户数据或完整日志。
