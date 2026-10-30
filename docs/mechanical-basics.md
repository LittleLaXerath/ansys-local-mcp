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
| Response spectrum | Passed for the documented flat pseudo-acceleration/SRSS benchmark |
| Random vibration | Passed for the documented one-sided acceleration PSD / one-sigma benchmark |
| Eigenvalue buckling | Passed for the documented cantilever Euler-column benchmark |
| Transient thermal | Passed for the documented one-dimensional conduction history |
| Basic acoustics | Passed for the first three nonzero modes of the documented closed cavity |
| Sequential thermal/structural transfer | Passed for the documented native-temperature import / free-expansion benchmark |

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

The additional six basic cases are documented below; this does not extend the
harmonic benchmark's own numerical scope.

## Additional Basic Analyses

The same opt-in runner accepts `--case response_spectrum`, `random_vibration`,
`buckling`, `transient_heat`, `acoustic_cavity`, or `thermal_expansion`. Each
creates a new project/copy, validates before execution, uses 4 SMP cores and a
1,800 s budget, reads native results with DPF and confirms shutdown. A shared
trusted product script and case registry avoid per-analysis MCP tools.

- **Response spectrum:** a flat pseudo-acceleration spectrum, 1 m/s² between
  20 and 200 Hz, with SRSS combination. The declared 5% damping is the definition
  of the supplied response-spectrum curve, not a claim to set a hidden native
  model-damping control. Native modal participation, mass-normalized DPF shape,
  modal linkage, spectrum units/direction, and peak relative displacement are
  checked against `Sa/(E*A/(L*m))`. Six modes are extracted; the dominant X
  mode is identified from the native X participation table, not assumed to be
  mode one. No extra constraints are imposed on the rigid mass attachment.
- **Random vibration:** a one-sided flat acceleration PSD of
  0.01 (m/s²)²/Hz between 20 and 120 Hz. Constant damping is explicitly Manual,
  modal damping ratio 0.05 and mass/stiffness coefficients zero. Relative
  directional displacement is explicitly one sigma. Independent Simpson
  integration of the SDOF transfer function over **Hz** is refined before
  comparison; RMS is not compared with a peak or three-sigma value. Native
  result tables and DPF displacements must agree.
  The result file contains both a unit-base static solution and the one-sigma
  solution. Native load step/substep/iteration identify the latter explicitly;
  the first DPF field is never assumed to be the RMS result.
- **Eigenvalue buckling:** a fixed-root/free-tip column under a 1 N compressive
  preload, linked to native Eigenvalue Buckling. The positive first multiplier
  times preload is compared with `pi²*E*I/(4*L²)`, with a 5% tolerance. This is
  a linear bifurcation check, not a nonlinear collapse prediction.
- **Transient thermal:** initially 0°C, left end held at 0°C and right end at
  100°C, with insulated lateral walls. Fixed dt=0.25 s through 100 s and all
  400 native time sets are read. The one-dimensional Fourier-series conduction
  reference uses saved conductivity, density and specific heat. The comparison
  window is defined in advance as 10–100 s; maximum nodal error divided by the
  100°C imposed temperature range must be <=1%. Native mesh coordinates are
  converted from the CAD unit and checked against the measured length.
- **Modal acoustics:** a closed, rigid rectangular air cavity with density
  1.2 kg/m³ and sound speed 343 m/s. A new native Modal Acoustic analysis is
  created inside the Workbench-owned model (no unavailable standalone template
  is assumed). Three nonzero longitudinal frequencies are compared with
  `n*c/(2*L)`; nonempty native pressure eigenfields are read, but their normalized
  amplitudes are not interpreted as forced acoustic pressure.
- **Sequential thermal/structural:** an actual native thermal result at 72°C is
  imported by Imported Body Temperature into a fresh structural analysis with
  a 22°C reference. Root axial and minimal vertex constraints remove rigid
  motion without preventing free expansion. Source temperature, mapped target
  structural temperature and displacement are read independently; the axial
  reference is `alpha*(72-22)*L`, with <=1% tolerance. Setting an unrelated
  structural temperature manually cannot replace the transfer.

See [the shared workflow template](../examples/mechanical_basic_analyses.workflow.example.json).
It references a parameterized script generated by the opt-in runner. Run the
runner for complete field exports, material creation where needed, independent
numerical checks, closure and receipts; the single-step template is not itself
a full acceptance sequence.

### Recorded Results

Measured 2026-10-30 on v261, using independent new projects and 4 SMP cores:

| Case | Independent reference and measured result | Error |
| --- | --- | --- |
| Response spectrum | 5e-6 m reference; 5.0002e-6 m native table; X effective-mass ratio 0.999974 | 0.004% |
| Random vibration | 1.6497469678e-5 m integrated RMS; 1.6498e-5 m one-sigma native table, confirmed with DPF | 0.003215% |
| Buckling | 41,123.351671 N Euler reference; 41,004.132919 N critical load | 0.289905% |
| Transient thermal | 400 native time sets; 224,181 nodal/time comparisons in the declared window | 0.341759% of imposed temperature range |
| Modal acoustics | 1715/3430/5145 Hz references; 1715.000724/3430.023083/5145.174173 Hz measured | Maximum 0.003385% |
| Thermal/structural | 72°C source and mapped target fields; 6e-5 m axial expansion reference | Relative error below 1e-10 |

The successful native runs included project save, all input/material/unit
readbacks, source-geometry integrity, independent receipt revalidation and
confirmed shutdown. Two fresh stdio MCP clients from different working
directories returned the expected verified run for all nine basic cases;
existing Fluent/CFX workflow evidence remained verified.

Earlier failed runs are retained. These exposed unsupported body-scoped
displacement, read-only damping properties until the correct native control is
selected, mesh-coordinate unit conversion, and the PSD static/RMS set mapping.
A trial that removed the Workbench-owned initial analysis stalled before solve;
only its authenticated original task was cancelled and then explicitly closed.
The final acoustic recipe leaves that owning analysis intact. A preliminary
acoustic evidence index lacking a conditional-call observation was archived,
not used as final evidence; the fresh run records creation versus reuse of a
physics region and claims only actual API calls.

All nine **basic benchmark gates** are now passed, not every Mechanical API or
extension. Stress-based spectral fatigue, nonlinear collapse, arbitrary
geometry/materials, fluid/structure acoustics, nonuniform transfer and mesh/time
independence remain outside these cases. Full CFX Re100 regression is separate.

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

计划中的九项基础验收均已通过：坐标系/命名选择、瞬态结构、谐响应、响应谱、随机振动、
屈曲、瞬态热、基础声学和顺序热结构传递。均有新工程、独立参考、原生字段读回、关闭确认
及可查询回执；不是 Mechanical 全组件/全模型验收。ACP、DesignLife、优化、增材及第三方
ACT 等专用包仍另列后续。原工程不覆盖，失败和取消记录保留；工程、原生结果和完整报告
仅保留在忽略的私有目录，不进入 GitHub 仓库。
