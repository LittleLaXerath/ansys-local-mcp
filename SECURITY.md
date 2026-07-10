# Security / 安全提示

This repository exposes trusted local automation through MCP.

本仓库通过 MCP 暴露受信任的本地自动化能力。

| Topic | 中文 | English |
| --- | --- | --- |
| Full security notes | [中文安全提示](SECURITY.zh-CN.md) | [English Security Notes](SECURITY.en.md) |

## Minimum Rules / 最低安全规则

- Restrict `ANSYS_MCP_ALLOWED_ROOTS`. / 限制 `ANSYS_MCP_ALLOWED_ROOTS`。
- Review workflows and product scripts. / 审查工作流和产品脚本。
- Keep inline code disabled unless required. / 非必要时不要开启内嵌代码。
- Do not expose the server to untrusted clients. / 不要暴露给不受信任的客户端。
- Allowed roots are not an OS sandbox. / Allowed roots 不是操作系统沙箱。
- Signature or installation source never gates execution. / 签名或安装来源永不作为执行门槛。
- The server does not bypass or manage Ansys licensing. / 服务器不绕过或管理 Ansys 授权。
