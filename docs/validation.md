# Validation evidence / 分级验收

This report separates public API inventory, simulated tests, product calls,
solver completion and numerical checks. Unverified entries are not supported
claims. Original machine configuration, logs, native projects and result files
are retained only in the local ignored run directory.

## Baseline

P1 follow-up (2026-10-28): the medium-session WMI creation denial was compared
with the same minimal command in a normally elevated session, where it passed.
The 242-test non-product suite then passed 240 tests with two intentional opt-in
skips and no failures/errors. This does not add product/numerical coverage.
Two retained contact/design-point cases were rechecked from their original
files and imported as hash-bound workflow receipts. No unobserved API calls
or new solver runs are claimed. See [Operations](operations.md).

- Product baseline: Ansys 2026 R1 on Windows; Python 3.11 client.
- Standard clients: PyWorkbench 0.14.0, PyMechanical 0.12.12,
  Mechanical stubs 0.1.12, PyDPF 0.16.3.
- The installed v261 public inventory accounts for all 107 source files and
  58,389 public declarations, producing 56,936 unique entries and 1,802 types.
  Aliases and inherited declarations retain their source accounting.
- Catalog presence alone does not establish installed extensions, licensing,
  runtime reachability or engineering correctness.

## Recorded product checks

| Component | Observed evidence | Level |
| --- | --- | --- |
| CFX MCP short restart | Copied Re100 pre/solve/post through fresh stdio MCP; 5.00–5.02 s, four steps/four partitions; native CFD-Post readback, mass imbalance 9.90e-8, x/y force differences 0.01162%/0.01730%; original input/project/result hashes unchanged | Short workflow/result; full shedding regression pending |
| Windows process supervision | Real command quoting, cancellation, owned-child cleanup and client-independent jobs | Runtime |
| Mechanical direct backend | Empty model inspection, save, reopen, generation update and close through an authenticated administrator worker | Object lifecycle |
| DPF local worker | Real server discovery, official bundled result metadata, nodal displacement and CSV/JSON exports | Result access |
| Workbench-linked Mechanical | Model/Engineering Data inspection, save, reopen and close on the same owning project | Object lifecycle |
| Engineering Data material round trip | Structured creation/property readback, MatML export/import, density 1,000 → 1,100 kg/m³, E = 2 GPa and Poisson ratio 0.25, refresh/assignment/readback; exported and saved XML independently checked | Object/workflow |
| Real MCP reconnect | A second stdio server reattached to the original Workbench/Mechanical session and read the original object; save/reopen invalidated the previous generation, then owned processes closed | Object lifecycle |
| Typed object bridge | Real quantity/enum/selection values, public construction, an explicit method overload, table copying and a large response artifact | Object lifecycle |
| Native-request cancellation | An idle animation request was cancelled through its original authenticated owner; its Workbench/Mechanical creation identities exited and the session closed | Runtime |
| Native result exports | Existing six-mode result: 800 × 600 PNG, 800 × 600 GIF with 38 frames over 1.9 s and changing content, full decode and 6,696-row meter-unit CSV; source project/sidecar hashes unchanged | Result export |
| Static cantilever | Workbench and direct backends; tip displacement relative error 0.30902%, reaction error below 0.000001 N | Numerical result |
| Modal | Workbench; first frequency 813.0245 Hz versus 815.3807 Hz reference, 0.28897% relative error | Numerical result |
| Thermal | Workbench; 0–100 C and 60,500 W/m² mean flux match the one-dimensional reference | Numerical result |
| Mechanical/DPF cross-check | All 6,696 nodal displacements, same meter unit; maximum scaled relative difference 0.00465% | Numerical result |
| Thermal field linearity | All 6,696 nodes; maximum temperature-range fraction error below 0.000000001% | Numerical result |
| Contact, remote support and table load | Two-body frictional compression; 1/2 MPa gave 100/200 N reactions, closed sticking contact and a deformation ratio of 2.0 | Numerical result |
| Parameter creation | Workbench-linked friction-coefficient input and deformation-maximum output were created and read back | Object lifecycle |
| Design points | Two retained Workbench points updated friction coefficient to 0.25/0.30; point and input/output freshness flags all true, deformation 0.0000002110687052 m at both points | Workflow/result |
| Explicit Dynamics/AUTODYN | Generated two-body elastic contact at 0.1 m/s, 0.5 ms solve, erosion disabled, native result/table export and motion sanity checks passed | Solve/motion sanity |
| LS-DYNA | Same generated low-speed elastic contact, actual LS-DYNA solver, 0.5 ms solve, native result/table export and motion sanity checks passed | Solve/motion sanity |
| LS-DYNA/DPF reader | Saved `d3plot`: 22 time sets, 1,980 nodes, three-component nodal displacement; empty unit metadata retained as unknown | Result access |
| Rigid dynamics | Ground translational joint, 0.01 m/s velocity for 0.1 s: 1.000 mm displacement versus 1.000 mm reference, below 1% error limit | Solve/prescribed motion |

The DPF check used an official bundled example, without downloading a customer
model. The server exposed 2,350 operators; the checked displacement field had
3,751 nodal entities and retained its meter unit. LS-DYNA reader discovery does
not establish support for AUTODYN result formats.

The exported modal animation uses normalized mode shapes and display scaling;
its amplitudes are not a forced-response displacement prediction.

The physical beam checks used a generated 100 × 10 × 10 mm solid with quadratic
swept elements. Saved material data proved the reference constants: Young's
modulus 200 GPa, density 7,850 kg/m³ and thermal conductivity 60.5 W/(m K).
The static/modal error limit was 5%, thermal 1%, and the same-node DPF limit
0.1%. Near-zero displacements use a documented scale floor rather than an
unstable division by zero.

## Observed limitations

- For this product release and fixture, explicit Manual + StandardMKS solver
  units caused a native application exit before solver-input generation.
  ActiveSystem units, read back as ConsistentMKS, completed the independent
  direct static and Workbench three-case controls. This is a measured
  configuration boundary, not evidence of a licensing failure or a blanket
  claim that all manual-unit configurations are unsupported.
- Local DPF in-process transport passed. An explicit mTLS gRPC trial without
  configured certificates returned an authentication-required diagnostic;
  insecure fallback was not used and gRPC success is not claimed.
- A single-set static animation trial using global animation options did not
  complete and was cancelled. A new clone with six already-solved modes and
  the minimal native `ExportAnimation` call passed without another solve.
  Global animation-option configuration remains outside the measured success.
- One historical worker disappeared before product ownership was recorded.
  Its inactive registry entry remains unavailable, rather than falsely closed
  or automatically relaunched. Processes without proven ownership were retained.

## Public API and component matrix / 公开目录矩阵

Counts below are overlapping qualified-name tags from the v261 inventory, not
counts of usable operations in an analysis. Shared/inherited members remain
accounted for under their declaring types. Runtime evidence attaches to exact
members and the current installation/backend, not every entry with the same tag.

| Component | Tagged entries | Current measured scope |
| --- | ---: | --- |
| Model/project | 2,609 | Both backend lifecycles; Workbench MCP reconnect |
| Engineering Data/materials | 404 | Structured create/modify/export/import, assignment and saved-property checks |
| Geometry | 1,197 | Generated CAD import and geometric scoping |
| Selection | 2,808 | Named selections and typed geometry selections |
| Coordinate systems | 1,580 | Catalogued; dedicated workflow not verified |
| Mesh | 4,626 | Quadratic swept solid meshes |
| Connections | 7,399 | Frictional contact and remote displacement |
| Loads/constraints | 9,783 | Force, temperature and tabular pressure in measured cases |
| Solve | 2,237 | Static, modal and steady thermal measured cases |
| Results | 36,566 | Nodal fields, reaction/flux/contact results, CSV and native PNG/GIF exports |
| Parameters | 2,347 | Input/output creation, readback and two retained design-point updates |
| Static structural | 2 | Cantilever and contact compression |
| Transient structural | 1 | Catalogued; dedicated solve not verified |
| Modal | 88 | First cantilever frequency |
| Harmonic | 104 | Catalogued; dedicated solve not verified |
| Response spectrum | 2 | Catalogued; dedicated solve not verified |
| Random vibration | 1 | Catalogued; dedicated solve not verified |
| Buckling | 2 | Catalogued; dedicated solve not verified |
| Thermal | 3,010 | Steady one-dimensional conduction |
| Acoustic | 3,112 | Catalogued; dedicated solve not verified |
| Coupled field/data transfer | 6 | Catalogued; dedicated workflow not verified |
| Explicit Dynamics | 183 | Low-speed elastic contact solve and motion sanity; no failure/convergence claim |
| LS-DYNA | 86 | Low-speed elastic contact solve and motion sanity; extension-specific solver properties |
| Rigid body dynamics | 69 | Ground-slider mechanism and prescribed 1 mm motion |
| Composites/ACP | 710 | Catalogued; external extension not verified |
| DesignLife | 84 | Catalogued; external extension not verified |
| Optimization | 524 | Catalogued; dedicated workflow not verified |
| Additive | 492 | Catalogued; dedicated workflow not verified |
| ACT | 50,895 | Namespace inventory; individual external extensions not verified |

GUI-only actions remain explicit gaps. Missing runtime or license evidence is
reported as unknown; the stub inventory is never used to infer availability.

## Running checks

`python -m unittest discover tests` runs contract, simulated-product and ordinary
process tests. Product acceptance is opt-in and must run against new isolated
projects. A test skip is recorded as a skip, never as a successful solve.
Test groups, opt-in variables and the measured baseline are recorded in
[testing.md](testing.md); open defects and pending investigations are in
[known-issues.md](known-issues.md).

Measured 2026-10-27 on Windows 11 with Python 3.11.9: 224 tests were considered,
208 passed, three failed, 11 errored and two explicit opt-in tests were skipped.
All 14 failing/erroring cases are durable-worker/elevation lifecycle tests that
cannot create a WMI process from a medium-integrity, deny-only-administrators
token; they fail closed before any worker starts. The run also confirmed that
the shared pywin32 cache defect is fixed (no `gen_py` errors remained). The
product and numerical evidence above comes from separate real-product runs, not
from these tests.

Before publishing, run the publication checker on the full staged tree and the
actual wheel/sdist contents. Only sanitized validation summaries are published.
Existing Git history remains intact.

From a source checkout with a writable allowed root, the opt-in runners are:

```powershell
python -m tools.run_product_acceptance --backend workbench --case all
python -m tools.run_contact_parameters --execute
python -m tools.run_result_exports --help
python -m tools.run_specialized_acceptance --case explicit
python -m tools.run_specialized_acceptance --case lsdyna
python -m tools.run_specialized_acceptance --case rigid
```

Each runner creates a fresh ignored directory. The export runner requires an
existing isolated benchmark project and works on a separate clone. Direct
Mechanical `.mechdb` round trip and `.mechdat` save were checked separately.
UAC and product lifecycle tests require explicit opt-in; ordinary CI never
launches Ansys or requests elevation.

中文：目录、对象调用、真实求解和数值验证分别记级。上表未验证项尚不能承诺完整可用。
完整本机配置、工程和日志不公开；历史 Git 提交按原样保留。
