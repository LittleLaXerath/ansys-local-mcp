# Mechanical Basic Acceptance / Mechanical 基础验收

This pack validates concrete workflows through the existing Workbench-first
Mechanical adapter and DPF, without adding a dedicated MCP tool per analysis.
It does **not** claim coverage of every Mechanical analysis or extension.

## Coverage

| Planned case | Product/numerical gate |
| --- | --- |
| Coordinate systems and named selections | Passed on the documented v261 benchmark |
| Transient structural | Pending |
| Harmonic response | Pending |
| Response spectrum | Pending |
| Random vibration | Pending |
| Eigenvalue buckling | Pending |
| Transient thermal | Pending |
| Basic acoustics | Pending |
| Sequential thermal/structural transfer | Pending |

ACP, DesignLife, optimization, additive and third-party ACT remain later,
separately scoped acceptance packages. Catalog entries are not passed tests.

## Coordinate And Selection Benchmark

From a source checkout with working local products and allowed paths:

```powershell
python -m tools.run_mechanical_basics --execute --geometry-path path/to/benchmark-beam.step --output path/to/new-run --register-evidence
```

The input is a previously generated 100 x 10 x 10 mm beam, copied into a fresh
directory. The runner refuses existing output directories. It never opens or
overwrites an existing user project, changes licensing/security settings or
silently relaunches an unknown request. `--execute` is mandatory. The current
worker environment must support authenticated durable-worker creation; existing
ordinary-medium-token WMI limitations still apply.

The runner starts a new Workbench project and runs two static analyses
sequentially, each limited to 4 SMP cores. The complete acceptance has an 1,800 s
execution budget and uses the existing MCP background-job/status/cancellation
interfaces. Every workflow is validated first. Unknown outcomes retain their
original run/request identities; the runner requests cancellation, not replay.

The trusted product script creates root/tip named selections, a Cartesian
coordinate system at (0.03, 0.002, -0.001) m, and a 30-degree Z rotation. It
reads back the basis, origin, selection IDs, solver configuration, load
components and reaction forces. A global force (0,-1,0) N is compared with
the equivalent local components (-sin(30°),-cos(30°),0) N in the rotated frame.
All results are evaluated before being read.

The script uses public [CoordinateSystem APIs](https://scripting.mechanical.docs.pyansys.com/version/stable/api/ansys/mechanical/stubs/v261/Ansys/ACT/Automation/Mechanical/CoordinateSystem.html).
Under the tested MKS configuration, the transformation getter returns radians;
the report records that native value separately and converts it to degrees.
Reaction orientation and reaction location are different properties: the
probe is explicitly scoped to the fixed support and oriented globally.

DPF opens each newly written RST and exports all three nodal displacement
components in metres. Node IDs, not row ordering, join the fields. Fixed checks
require equivalent transformed forces, the same support/load faces, global
reaction balance, maximum vector discrepancy/peak <=1e-6, and <=5% error
against the independent Euler-Bernoulli tip-displacement reference FL³/(3EI).
The saved Workbench Engineering Data XML supplies SI material constants and is
hash-bound to the evidence; unknown units and nonpositive/nonfinite constants
fail validation.

## Recorded Result

On 2026-10-30, Workbench/Mechanical 2026 R1 and local DPF completed the isolated
benchmark with 1,250 elements and 6,696 nodes. Global peak displacement was
2.0061802802864e-6 m versus 2e-6 m analytical; relative error 0.309014%.
Maximum nodal-vector discrepancy/peak was 7.36148e-12. The source STEP's
before/after/copy SHA-256 values matched. The accepted sequence included native
result readback, project save and confirmed product shutdown. Earlier failed
and incomplete validation attempts remain unchanged in separate directories.

This tests coordinate/load equivalence on one linear isotropic static model,
not mesh independence, nonlinear behavior, all coordinate types or other
analysis categories. Runtime and numerical gates for pending cases stay pending.

## Receipts

Successful runs prepare an existing-format private acceptance receipt;
`--register-evidence` explicitly imports it after independent checks. It binds
the raw product report, original DPF export hashes, native results, material XML,
script snapshot, source-geometry integrity, pre-run installation fingerprint,
and model/field/close workflow results. Query revalidation detects changed
artifacts or missing sources instead of retaining a false verified status.

```text
query_capabilities(product="mechanical", version="261", kind="workflow", component="coordinate_equivalence")
```

Only the four explicitly recorded coordinate/selection API names receive
receipt-derived evidence; no other analysis API is inferred from this model.

## 中文说明

本轮通过的是“坐标系与命名选择”这一项：新建工程、先后两次静力求解、原生结果逐节点
比较、反力/单位/几何/材料读回和关闭确认。不是 P4 全部完成。上表其余八项仍待真实
验收，不能以已有 API 目录或本项通过替代。工程、原生结果、失败记录和完整报告仅保留
在忽略的私有目录，不进入 GitHub 仓库。
