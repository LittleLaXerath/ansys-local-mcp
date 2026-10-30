# Examples / 示例说明

[English README](../README.en.md) | [中文说明](../README.zh-CN.md)

## Verification / 验证范围

Workflows are checked in `validation_mode="schema"`. Default tests use fake
product objects and ordinary worker processes. Separate opt-in product tests
use fresh generated projects; measured support is listed in
[the validation report](../docs/validation.md).

默认测试使用模拟产品对象和普通工作进程；真实产品验收单独显式运行，采用新生成的隔离工程。
已测范围见[验收记录](../docs/validation.md)。Schema 通过不等于建模、网格或求解成功。

[Mechanical 坐标系模板](mechanical_coordinates.workflow.example.json) 展示既有 mechanical step；
`coordinate.py` 是显式验收脚手架生成的带参数可信脚本，不应直接运行无参数的产品源脚本。
真实执行及完整 DPF 数值比较请使用[基础验收入口](../docs/mechanical-basics.md)。

[瞬态结构模板](mechanical_transient.workflow.example.json) 使用同一 mechanical step，
通过现有 Engineering Data 操作创建工程内材料。带参数脚本由 `--case transient_sdof`
验收入口生成；完整时间历程导出和数值比较不以该单步模板执行成功代替。

[谐响应模板](mechanical_harmonic.workflow.example.json) 复用同一通道和工程内材料。
`--case harmonic_sdof` 生成参数化脚本，并按原生频率集合导出实部/虚部，独立比较频响幅值和相位；
Mechanical 实虚记录与 DPF 频率编号的映射会显式核对。

[基础分析模板](mechanical_basic_analyses.workflow.example.json) 展示六项后续分析复用的既有通道，
默认是顺序热结构脚本入口。完整验收使用 `tools.run_mechanical_basics --case`，可选
`response_spectrum`、`random_vibration`、`buckling`、`transient_heat`、`acoustic_cavity`、
`thermal_expansion`。各项固定参考、资源上限、单位和结果集选择见[基础验收包](../docs/mechanical-basics.md)。

[Fluent 持久会话示例](fluent_persistent.workflow.example.json) 演示创建、跨 step 复用和显式关闭；
真实执行会启动 Fluent，检查格式时请使用 `validation_mode="schema"`。

Set allowed roots to include the examples and inputs. A workflow file's relative
workdir is resolved from its directory. Other server-managed paths are relative
to that workdir. JSON variables are not substituted inside external scripts.

allowed roots 应包含示例和输入。相对 workdir 以工作流文件所在目录为基准，
其余服务器管理的路径以 workdir 为基准。外部脚本内容不会自动替换 JSON 变量。

## SpaceClaim Geometry / 几何建模

[Workbench route](workbench_recipe.example.json) creates a new Geometry system,
opens SpaceClaim, runs [the geometry script](scripts/spaceclaim_geometry.py),
closes the editor and saves a new project.
[Direct route](spaceclaim_direct.example.json) executes the same script using
SpaceClaim command-line options.

The script creates a rectangular fluid body and named selections `fluid`,
`inlet`, `outlet`, and `wall`. Dimensions default to 100 x 20 x 10 mm;
override `length_mm`, `width_mm`, and `height_mm` using workflow variables.
It checks body count, face counts and measured volume, then exports:
`channel.scdocx`, `channel.step`, and `measurements.json` under
`<workdir>/.ansys_mcp/spaceclaim-example/`. Workbench also saves `model.wbpj`.

The two routes share this output directory. Archive previous outputs before
running again. The script refuses to overwrite them or modify a non-empty
document. Native files preserve named selections; STEP should not be assumed
to preserve them. This example creates geometry, not a mesh or CFD solution.

Workbench 通道创建新系统，直接通道运行同一脚本。默认生成 100 x 20 x 10 mm
矩形流体体积，尺寸可通过变量修改，包含入口、出口和壁面命名选择。
脚本检查体数量、边界面数量和体积，再输出原生模型、STEP 与测量 JSON。
两个通道共用输出目录，再次运行前请归档旧产物；脚本拒绝覆盖旧文件或清空已有几何。
命名选择保存在原生模型中，不保证通过 STEP 传递。此示例不生成网格。

## CFX Setup / 前处理设置

[CFX Workbench example](cfx_workbench.example.json) requires an existing
`.wbpj` and its associated project directory. Override `project` and
`system_name` (default `CFX`).
The [CCL script](scripts/cfx_setup.ccl) expects `Default Domain` under
`Flow Analysis 1`, and changes turbulence to SST with automatic wall treatment.
Adapt these names to the input project. The recipe returns `PhysicsStatus`,
writes `physics-status.txt`, and saves a separate project in
`.ansys_mcp/cfx-example/`. A returned status needs inspection; it does not
guarantee that the physics setup is valid.

CFX 示例需要已有工程和配套目录。通过 project 指定输入，默认系统名为 CFX。
脚本修改 Flow Analysis 1 下 Default Domain 的湍流设置为 SST 与自动壁面处理。
运行前核对这些名称。示例返回物理状态并另存工程，不生成网格、不启动求解器。

For direct execution, use `cfx_pre` with `-batch` and your own trusted
`.pre` session. Declare generated files in `produces` before later steps use
them, such as `cfx_solve` with `-def`. The supplied CFX Workbench example does
not claim to create a complete simulation from raw geometry.

直接通道可执行自己的 CFX-Pre 会话文件，再把声明的产物传给后续步骤。
当前 CFX 示例不代表已经实现从原始几何到完整仿真的流程。

## Other Templates / 其他模板

- [workflow.example.json](workflow.example.json): local file operations.
- [workbench_first_suite.example.json](workbench_first_suite.example.json):
  legacy wrapper around [workbench_noop.wbjn](workbench_noop.wbjn).
- [direct_channels.example.json](direct_channels.example.json): launch/help
  checks, not modeling.
- [fluent_recipe.example.json](fluent_recipe.example.json): legacy PyFluent
  template requiring your case and zone names.
- `scripts/fluent_setup.jou` remains a placeholder requiring a real case.
- `scripts/mechanical_setup.py` reads model/analysis metadata; it does not solve.
- [mechanical.example.json](mechanical.example.json) and
  [mechanical_workbench.example.json](mechanical_workbench.example.json)
  demonstrate structured object/session operations.
- [dpf_recipe.example.json](dpf_recipe.example.json) is a standalone DPF recipe.
- [cfx_recipe.example.json](cfx_recipe.example.json) chains one `cfx` step per
  operation (pre → solve → post) with declared `.def`/`.res`/image products.
  The `.pre` and `.cse` scripts are yours; this step does not parse CCL. Completion
  is verified from fresh outputs and the solver log, not from the exit code.
- `tools/run_product_acceptance.py` explicitly runs isolated physical benchmarks;
  its original native files and logs stay in the ignored run directory.
- `tools/run_contact_parameters.py`, `tools/run_result_exports.py` and
  `tools/run_specialized_acceptance.py` are opt-in source-checkout runners.
- `scripts/engineering_data_acceptance.py` runs in the client Python interpreter
  and only prepares a workflow for a synthetic material round trip. Execute the
  generated workflow separately after reviewing its paths and unique body selection;
  it is not a script to send directly into Mechanical.

其他示例分别用于文件操作、旧入口、启动检查和 PyFluent 模板。
Fluent 脚本仍需真实案例配置；Mechanical 元数据脚本不代表完整仿真设置。
CFX 示例只负责按步骤执行并校验产物，不解析 CCL。
材料往返示例是客户端工作流生成器，生成后另行执行，不应作为 Mechanical 产品内脚本发送。

## Official Interfaces / 官方接口

- [Workbench Geometry](https://ansyshelp.ansys.com/public/Views/Secured/corp/v261/en/wb2_js/ContainerName56.html)
- [SpaceClaim command-line options](https://ansyshelp.ansys.com/public/Views/Secured/corp/v261/en/spaceclaim/Discovery/user_manual/spaceclaim_command_line_options.html)
- [Geometry creation and named selections](https://discuss.ansys.com/discussion/2202/example-of-worflow-automation-create-geometry-model-setup-and-postprocess)
- [CFX Setup](https://ansyshelp.ansys.com/public/Views/Secured/corp/v261/en/wb2_js/ContainerName10.html)
