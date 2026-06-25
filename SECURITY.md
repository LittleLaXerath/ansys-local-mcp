# Security / 安全提示

This repository exposes local command execution through MCP.

本仓库通过 MCP 暴露本地命令执行能力。

Read the full safety notes before running workflows:

运行工作流前请先阅读完整安全提示：

| Topic | 中文 | English |
| --- | --- | --- |
| Security notes | [中文安全提示](SECURITY.zh-CN.md) | [English Security Notes](SECURITY.en.md) |

## Minimum Rules / 最小规则

- Restrict `ANSYS_MCP_ALLOWED_ROOTS`.
- Review workflows before running them.
- Do not expose this MCP server to untrusted clients.
- Do not commit license files, proprietary models, customer data, or solver results.

- 限制 `ANSYS_MCP_ALLOWED_ROOTS`。
- 运行前审查工作流。
- 不要把这个 MCP 服务器暴露给不受信任的客户端。
- 不要提交许可证文件、专有模型、客户数据或求解结果。

## Reporting / 报告问题

Use private vulnerability reporting when available. If it is not enabled, open
a minimal issue without secrets or sensitive project data.

优先使用 GitHub 私密漏洞报告功能。若仓库没有开启该功能，可以提交最小化 issue，但不要包含密钥或敏感项目数据。
