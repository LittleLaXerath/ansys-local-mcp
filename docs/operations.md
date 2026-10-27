# Worker diagnostics, retention, and acceptance receipts

These commands are source-checkout utilities unless a package module is shown.
All paths must be inside `ANSYS_MCP_ALLOWED_ROOTS`. They do not start Ansys.

## Windows worker diagnostics

```powershell
python -m tools.diagnose_worker --output .ansys_mcp/diagnostics/worker.json
python -m tools.run_checks --output .ansys_mcp/baseline/checks.json
```

The diagnostic launches only a short-lived Python process, queries WMI, and
tries the same process creation through WMI. It records elevation and token
group/integrity information without changing Windows security settings.
Use different output filenames when comparing ordinary and elevated sessions.
`run_checks` disables both product and UAC test opt-ins and records unique
affected test IDs: one test can generate an error in both its body and cleanup.

On the measured Windows installation, the medium token was denied at
`Win32_Process.Create`, while the normally elevated token succeeded. This
does not mean all WMI calls require administrator privileges. The independent
worker uses the caller's existing permissions and never silently requests
elevation or substitutes a lifetime-bound worker.

An explicit WMI denial is reported as `WINDOWS_WORKER_ACCESS_DENIED` with its
phase. A denied background job remains `failed` with `outcome=not_started`
when queried later; it is not mislabeled as a worker crash with unknown outcome.

## Conservative log retention

```powershell
python -m ansys_mcp.maintenance --workdir path/to/workspace --older-than-days 30
```

This is a dry-run by default. `--apply` explicitly enables deletion after a
fresh scan and identity/hash checks. Version 1 selects only direct-child
`*.stdout.log` and `*.stderr.log` files in finished workflow/job directories.
Both the manifest and the log must meet the age limit.

Running, interrupted, unknown, needs-attention and live-session records are
retained. Unreadable job ownership prevents deletion. Linked directories,
symlinks and hard-linked logs are not selected. Native models, result files,
input files, receipts, evidence, credentials and manifests are never selected;
there is no recursive directory deletion. General cache deletion is not enabled.

## Private acceptance receipts

```powershell
python -c "import json; from ansys_mcp.acceptance_evidence import AcceptanceReceipt; print(json.dumps(AcceptanceReceipt.model_json_schema(), indent=2))"
python -m ansys_mcp.acceptance_evidence path/to/receipt.json
python -m ansys_mcp.acceptance_evidence path/to/receipt.json --apply
```

The first import command only checks. `--apply` records the independently
rechecked workflow in the private evidence store. A receipt contains:

- Product/version, backend, original observation time, installation fingerprint,
  run ID and case ID; client versions when actually recorded (otherwise empty).
- Paths and SHA-256 digests for the original report, validation, product envelope,
  parameters and at least one retained product artifact.
- A fixed validator: `contact`, `parameters`, `specialized`, or
  `core_beam_scalars`. Their formulas and tolerances are shared with acceptance
  runners and are packaged with the server; no product process is launched.

The current receipt importers cover the existing Mechanical fixtures. They
do not establish new DPF reader support. Core beam receipts check reported
scalar results only; they do not repeat a nodal DPF comparison. Hash checks
bind the reports to retained artifacts; importing is not a new solver run.

Wrong installation/backend/version, failed checks, nonfinite values, missing
files or changed hashes reject the receipt. Existing indexed run/case identities
are retained, with idempotent reimport of the same receipt. Do not fabricate
historical SDK versions or reassign old fingerprints to the current install.

Only names explicitly present in the original report's `executed_apis` list
receive API-level observations. No calls are inferred from a case name or a
Markdown table. Older reports without this list gain workflow evidence only.

Read the results through the existing MCP tool:

```text
query_capabilities(product="mechanical", kind="workflow", component="contact-compression")
```

The default `kind="api"` retains its previous behavior. Workflow queries check
receipt/artifact integrity again. Imported API evidence also loses its verified
status if its receipt/artifacts disappear or change, without rewriting history.

## 中文说明

诊断入口只启动普通 Python，不启动 Ansys。2026-10-28 的对照证明，本机普通令牌下
WMI 创建进程被拒，正常提权后的会话可以创建，并通过后台生命周期测试。
这是一项已测得的执行权限边界，不表示所有 WMI 操作都需要管理员权限。

日志维护默认 dry-run，只选择已结束且没有活动会话的旧 stdout/stderr 日志。
应用时重新检查状态、边界、文件身份和哈希，不递归删除运行目录，也不删除工程、
结果、证据、认证材料或任何一般缓存。

证据导入默认只检查。正式导入需要保留原报告、产品执行结果、参数、验证文件和原生
产物，并核对 SHA-256、安装范围和固定数值条件。它不重新求解，也不冒充对全部原生
场数据进行了独立读取。旧报告没有实际 API 调用清单时，只登记工作流级证据。
可通过 `query_capabilities(kind="workflow")` 查询；产物变更或丢失后不继续显示为已验证。
