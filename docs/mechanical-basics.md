# Mechanical Basic Acceptance / Mechanical 基础验收

This pack validates concrete workflows through the existing Workbench-first
Mechanical adapter and DPF, without adding a dedicated MCP tool per analysis.
It does **not** claim coverage of every Mechanical analysis or extension.

## Coverage

| Planned case | Product/numerical gate |
| --- | --- |
| Coordinate systems and named selections | Passed on the documented v261 benchmark |
| Transient structural | Passed for the documented undamped mass-dominated axial benchmark |
| Harmonic response | Passed for the documented Full-method, mass-damped axial benchmark |
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

## Transient Structural Benchmark

```powershell
python -m tools.run_mechanical_basics --case transient_sdof --execute --geometry-path path/to/benchmark-beam.step --output path/to/new-transient-run --register-evidence
```

The same runner now selects cases from a registry; the default remains the
coordinate benchmark. No new MCP tool, workflow step or runtime dependency is
added. The transient workflow uses the existing Engineering Data bridge to
create a project-local linear material: E=200 GPa, density 7,850 kg/m³ and
Poisson's ratio 0. That ratio removes lateral-constraint stiffening from the
independent axial-spring reference; no global material library is modified.

A native Mechanical PointMass of 1,000 kg is rigidly attached at the beam tip.
The 100 x 10 x 10 mm beam supplies axial stiffness k=EA/L=2e8 N/m. Its own
0.0785 kg mass is only 7.85e-5 of the point mass, so the comparison is explicitly
a **mass-dominated FE approximation** to an SDOF oscillator, not an assertion
that the entire finite-element model has only one degree of freedom.

The fresh Transient Structural model has nominally zero initial displacement
and velocity and receives a constant axial 100 N step. For the independent
undamped reference, u(t)=F/k*[1-cos(sqrt(k/m)*t)]. The run uses 4 SMP cores,
no large deflection, zero Rayleigh/numerical damping, a fixed 5e-5 s time step,
and 0.04 s end time. The runner reads back all these controls, point/body mass,
tip position, force table, geometry and saved material XML. Native controls
follow the public [transient-analysis interface](https://ansyshelp.ansys.com/public/Views/Secured/corp/v251/en/act_script/act_script_demo_trans_struct.html).

All 800 native result sets are exported through the existing DPF step, scoped
to the actual tip node IDs. Time-set labels, native result times, complete node
coverage and metre units must agree. Validation checks the complete signed
time response, oscillation/period, axial uniformity, small transverse motion
and the final Mechanical displacement. A static result, matching peak with
wrong phase, missing time sets, changed damping or modified original export
cannot grant a verified receipt. The material parser trims whitespace around
XML units but still rejects non-SI physical units; an empty unit is allowed
only for the dimensionless Poisson ratio.

Measured 2026-10-30: time-response relative L2 error **0.544548%**, maximum
error/analytical peak **0.503675%** (both <=5%); measured period 0.014050 s versus
0.014049629462 s analytical (<=2% tolerance). All 800 samples, geometry hashes,
project save, independent revalidation and confirmed shutdown passed. The
earlier material-format failure remains unchanged in its own run directory.

```text
query_capabilities(product="mechanical", version="261", kind="workflow", component="transient_sdof")
```

This does not validate damping, nonlinear contact, arbitrary load histories,
initial-velocity workflows, mesh/time-step independence or every transient
element type. Other analysis categories need their own acceptance.

## Harmonic Response Benchmark

```powershell
python -m tools.run_mechanical_basics --case harmonic_sdof --execute --geometry-path path/to/benchmark-beam.step --output path/to/new-harmonic-run --register-evidence
```

This case reuses the project-local zero-Poisson material and 1,000 kg native
tip PointMass. It selects Mechanical's Full harmonic method, 4 SMP cores and
100 linear solution intervals between 20 and 120 Hz. A real 100 N axial force
has zero phase. Mass-proportional damping alpha=44.72135955 per second gives
an SDOF reference damping ratio of 0.05; stiffness-proportional and constant
damping are zero. Parameters, face scoping, mass, damping, solver settings and
saved material data are read back, rather than inferred from script success.

The independent reference is the complex displacement
`U(w)=F/(k-m*w*w+i*alpha*m*w)`, with `k=E*A/L`. No stiffness, mass or damping
is fitted to solver output. This is a mass-dominated FE approximation, not a
claim that the complete solid model has one degree of freedom.

Mechanical's native reader can list the real and imaginary records at the
same frequency separately. The report preserves that raw axis and checks its
mapping to DPF's frequency-set IDs. DPF exports both complex parts and all tip
nodes; labels, not field order, join the data. Missing parts/nodes, duplicate
rows, wrong units or mismatched frequencies fail. See the official
[complex-result example](https://post.docs.pyansys.com/version/stable/examples/00-Different-analysis-types/04-harmonic-complex-results.html)
and [harmonic methods](https://ansyshelp.ansys.com/public/Views/Secured/corp/v261/en/wb_sim/ds_harmonic_analysis_type.html).

Every frequency must meet <=5% complex and amplitude relative error and
<=3 degrees phase error. Fixed checks also require a resolved resonance,
the expected sign/phase change across it, axial uniformity and agreement with
Mechanical's independently evaluated final-frequency amplitude. The validator
does not accept amplitude-only or static data as a harmonic solution.

This case does not validate mode superposition, arbitrary damping models,
nonlinear vibration, base excitation or mesh/frequency independence.

Measured 2026-10-30 on v261: 100 frequencies (21 through 120 Hz), 80 elements,
621 nodes and 21 tip nodes. Maximum complex relative error was **0.0262003%**,
maximum amplitude error **0.0150184%**, maximum phase error **0.0149926 degrees**.
The peak was at 71 Hz; independent undamped natural frequency was 71.1762543 Hz.
Geometry hashes, native result readback, project save, independent receipt
revalidation and shutdown passed. The first attempt solved successfully but
failed DPF scoping because it counted both native complex records as frequency
sets. That failed run remains unchanged; a fresh corrected run passed.

```text
query_capabilities(product="mechanical", version="261", kind="workflow", component="harmonic_sdof")
```

Six other planned basic analysis cases remain pending.

## Receipt Integrity

Successful runs prepare an existing-format private acceptance receipt;
`--register-evidence` explicitly imports it after independent checks. It binds
the raw product report, original DPF export hashes, native results, material XML,
script snapshot, source-geometry integrity, pre-run installation fingerprint,
and model/field/close workflow results. Query revalidation detects changed
artifacts or missing sources instead of retaining a false verified status.

```text
query_capabilities(product="mechanical", version="261", kind="workflow", component="coordinate_equivalence")
```

Only API names explicitly recorded by each product script receive receipt-derived
evidence; no other analysis API is inferred from these benchmarks.

## 中文说明

目前通过“坐标系与命名选择”“集中质量主导的轴向瞬态响应”和“全法质量比例阻尼轴向谐响应”
三项，均有新工程、原生结果比较、单位/几何/材料读回、关闭确认与回执。不是 P4 全部完成。
其余六项仍待真实验收，不能以已有 API 目录或这三项通过替代。工程、原生结果、失败记录和完整报告仅保留
在忽略的私有目录，不进入 GitHub 仓库。
