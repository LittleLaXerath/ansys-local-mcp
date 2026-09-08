# Examples / 示例说明

[English README](../README.en.md) | [中文说明](../README.zh-CN.md)

## Verification / 验证范围

Workflows are checked in `validation_mode="schema"`. Tests use fake product
objects, not real Ansys sessions. The new scripts contain actual operations,
but have not yet been verified against a real Ansys installation.

自动测试使用模拟产品对象，不会启动 Ansys。新脚本已有实际操作，但尚未完成真实产品验证；
不能把 Schema 通过理解为建模、网格或求解成功。

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
`channel.scdoc`, `channel.step`, and `measurements.json` under
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
- `scripts/fluent_setup.jou` and `scripts/mechanical_setup.py` are explicitly
  marked placeholders, not completed simulation setups.

其他示例分别用于文件操作、旧入口、启动检查和 PyFluent 模板。
Fluent 与 Mechanical 占位脚本不代表已完成仿真设置。

## Official Interfaces / 官方接口

- [Workbench Geometry](https://ansyshelp.ansys.com/public/Views/Secured/corp/v261/en/wb2_js/ContainerName56.html)
- [SpaceClaim command-line options](https://ansyshelp.ansys.com/public/Views/Secured/corp/v261/en/spaceclaim/Discovery/user_manual/spaceclaim_command_line_options.html)
- [Geometry creation and named selections](https://discuss.ansys.com/discussion/2202/example-of-worflow-automation-create-geometry-model-setup-and-postprocess)
- [CFX Setup](https://ansyshelp.ansys.com/public/Views/Secured/corp/v261/en/wb2_js/ContainerName10.html)
