# Mechanical、会话、任务和能力证据

本适配器从已安装的官方 stubs 建立公开 API 目录，并提供结构化对象操作。目录存在、对象调用成功、求解完成及数值结果通过验证是不同等级，不能互相替代。

## 安装与入口

标准安装包含 PyWorkbench、PyMechanical、Mechanical stubs 和 PyDPF。`inspect_runtime(include_schema=True)` 返回安装发现、依赖版本和 recipe schema，不会启动产品。安装位置和工作目录由本地配置决定，源码中不写入用户电脑路径。启动 stdio 服务前，将 `ANSYS_MCP_ALLOWED_ROOTS` 指向存在的工作目录。

除原有 schema 资源外，新增 `ansys://schema/mechanical-recipe` 和 `ansys://schema/dpf-recipe`。`query_capabilities` 支持产品、版本、组件、状态和分页过滤。省略版本时使用发现到的产品版本；GUI-only、缺失扩展和没有公开接口的功能保留缺口状态。

## Mechanical 工作流

新增 `mechanical` 步骤，recipe 版本为字符串 `"1"`。默认 `backend="workbench"`，保持同一 Workbench 工程及其 Mechanical 会话；`backend="pymechanical"` 使用独立 Mechanical 进程。执行结果不明确时不会自动切换后端。

`mode="persistent"` 返回会话标识，后续通过 `session={"action":"reuse","id":...}` 复用。`mode="batch"` 只关闭本次新建的自有会话，复用会话仍保持打开。显式 `close` 操作负责关闭自有会话。对象引用绑定会话和模型代次；工程替换、可信脚本、已识别的结构修改调用及显式 `invalidate_refs` 会推进代次。其他会替换对象集合的 API 调用应设置 `invalidate_refs=true`，之后重新查询引用。

通用操作包括 `discover/inspect/find/get/set/call/construct`、工程管理和可信脚本文件。Engineering Data 操作单独进入所属 Workbench 会话。统一值协议覆盖单位、枚举、选择、表格和对象引用；公开属性路径不通过表达式求值执行。

`engineering_data` 中，`EngineeringData` 指向工程材料容器，`EngData` 指向公开 Workbench 命令命名空间。材料库导出通过 `EngData.WriteMaterials` 传入材料引用及 typed output path；接口依据见[官方 Engineering Data 文档](https://ansyshelp.ansys.com/public/Views/Secured/corp/v261/en/wb2_js/ContainerName37.html)。

文件输入输出使用相对路径或注明用途的 typed path，并遵守 allowed roots。覆盖输出必须显式指定；大型返回值写入产物。可信脚本保留 SECURITY.md 中已有的执行边界，不构成操作系统沙箱。

`examples/mechanical.example.json` 演示空工程检查、网格尺寸设置和回读，不进行物理求解。重复运行时应选择未使用的工程输出位置。

## 长任务和权限

保留同步 `run_workflow`，新增 `start_workflow/get_workflow_status/cancel_workflow`。后台 worker 独立运行，状态查询核对真实进程及创建身份；只有旧记录而没有终态结果时，任务标为中断或结果未知，不推断成功。

重连时保持相同的 allowed roots 和默认工作区配置，任务及 actor 索引归属于该本地工作区。

Mechanical 和 DPF recipe 禁止自动重试。观察超时后应查询同一会话和请求的实际状态，避免重复建模或求解。取消及强制清理仅作用于服务拥有的任务；没有公开停止接口的产品，不能把进程终止称为优雅取消。

使用 `get_mechanical_request_status(request_id, session_id=...)` 观察已知产品请求；首次响应只有执行 worker 标识时传入 `worker_id`。核对产品终态后，已停止后续步骤的工作流保持 `needs_attention`，不会静默续跑。

持久 actor 有本地未跟踪索引；新 stdio 服务核对创建身份并认证后，连接原 actor。缺失 actor 不会自动重建。Windows 独立 worker 不继承客户端用于连接清理的 Job，关闭连接不会隐式取消已提交任务。

Windows 权限不足时按需请求独立管理员执行会话。普通 MCP 保持普通权限，本机执行 worker 经过身份校验；拒绝授权时给出诊断，不改兼容设置、UAC 或许可证。

## DPF 与验证

新增 `dpf` 步骤，recipe 版本为数字 `1`，在独立受监督 worker 中运行。默认本机进程内模式，gRPC 保留产品的安全默认值。通用算子的文件输入使用显式 `$path`；结果文件和导出位置均须通过路径校验。

支持元数据、命名选择、网格／时间范围、结果字段、通用算子及 CSV/JSON 导出。格式支持以实际读取器为准，不能由 LS-DYNA 读取器推断 AUTODYN 结果已适配。

gRPC 缺少所需证书时返回认证诊断；进程内模式通过不代表 gRPC 已验收，也不会回退到不安全传输。

单元测试、模拟产品测试和真实产品验收分别报告；真实案例使用隔离的新工程。公开证据见 validation.md，原始日志、工程、安装路径和求解结果留在本机未跟踪运行目录。

发布前检查完整跟踪树、暂存区、分发包和新提交元数据。本机指纹文件不得提交。公开示例使用占位符和合成输入；此检查不会匿名化既有 Git 历史。

本地指纹文件为包含字符串数组 `values` 的 JSON；检查报告只返回相对位置和类别，不输出匹配值。扫描器读取实际暂存 blob、压缩包内容及头信息和新提交原始头信息，不能只依赖 `.gitignore`。应从已审阅的暂存快照构建，再通过 `--artifact` 检查每个实际 wheel/sdist；新提交使用 `--commit`、`--expected-name`、`--expected-email` 校验，author 和 committer 均须为核实后的 GitHub 登录名及 noreply 邮箱。
