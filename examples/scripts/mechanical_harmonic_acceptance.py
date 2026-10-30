# Product-side harmonic benchmark; run only in a fresh opted-in Workbench model.
import json
import os
import shutil
from Ansys.ACT.Interfaces.Common import SelectionTypeEnum, MechanicalUnitSystem
from Ansys.Mechanical.DataModel.Enums import (
    DataModelObjectCategory,
    ElementOrder,
    LoadDefineBy,
    MethodType,
    SolverUnitsControlType,
    LoadBehavior,
    HarmonicMethod,
    FrequencySpacingType,
    NormalOrientationType,
)

p = globals().get("ACCEPTANCE_PARAMETERS")
if not p:
    raise ValueError("Use the explicit harmonic acceptance runner.")
model = ExtAPI.DataModel.Project.Model
if len(model.Analyses) != 1 or len(model.Analyses[0].Children) > 3:
    raise ValueError("Refusing an existing/nonempty harmonic analysis.")
analysis = model.Analyses[0]
if "Harmonic" not in str(analysis.AnalysisType):
    raise ValueError("A Harmonic Response Workbench system is required.")
ExtAPI.Application.ActiveUnitSystem = MechanicalUnitSystem.StandardMKS
report = {
    "case": "harmonic_sdof",
    "status": "running",
    "cases": {},
    "executed_apis": [],
}


def checkpoint(phase):
    report["phase"] = phase
    with open(os.path.join(p["output_dir"], "harmonic.checkpoint.json"), "w") as stream:
        json.dump(report, stream, indent=2)


def q(value, unit):
    return float(Quantity(str(value)).ConvertUnit(unit).Value)


def selection(ids):
    result = ExtAPI.SelectionManager.CreateSelectionInfo(
        SelectionTypeEnum.GeometryEntities
    )
    result.Ids = list(ids)
    return result


checkpoint("geometry")
model.Materials.RefreshMaterials()
bodies = list(model.Geometry.GetChildren(DataModelObjectCategory.Body, True))
if len(bodies) != 1:
    raise ValueError("Exactly one beam body is required.")
body = bodies[0]
body.Material = p["material_name"]
scale = q("1 [" + str(ExtAPI.DataModel.GeoData.Unit) + "]", "m")
geo = body.GetGeoBody()
faces = [(f, [float(v) * scale for v in f.Centroid]) for f in geo.Faces]
extents = [
    max(row[1][i] for row in faces) - min(row[1][i] for row in faces) for i in range(3)
]
if len(faces) != 6 or any(
    abs(extents[i] - p[k]) > 1e-9
    for i, k in enumerate(("length_m", "width_m", "height_m"))
):
    raise ValueError("Unexpected beam geometry or units.")
root_face = min(faces, key=lambda row: row[1][0])[0]
tip_face, tip_center = max(faces, key=lambda row: row[1][0])
root, tip = selection([root_face.Id]), selection([tip_face.Id])
report["geometry"] = {
    "length_m": extents[0],
    "width_m": extents[1],
    "height_m": extents[2],
    "material": str(body.Material),
    "root_ids": [root_face.Id],
    "tip_ids": [tip_face.Id],
}
checkpoint("mass_mesh")
mass = model.Geometry.AddPointMass()
mass.Name = "P4 harmonic end mass"
mass.Location = tip
mass.Behavior = LoadBehavior.Rigid
for member, value in zip(("XCoordinate", "YCoordinate", "ZCoordinate"), tip_center):
    setattr(mass, member, Quantity(str(value) + " [m]"))
mass.Mass = Quantity(str(p["mass_kg"]) + " [kg]")
mesh = model.Mesh
mesh.ElementSize = Quantity("5 [mm]")
mesh.ElementOrder = ElementOrder.Quadratic
method = mesh.AddAutomaticMethod()
method.Location = selection([geo.Id])
method.Method = MethodType.Sweep
mesh.GenerateMesh()
mesh_data = ExtAPI.DataModel.MeshDataByName("Global")
report["tip_node_ids"] = [int(v) for v in mesh_data.MeshRegionById(tip_face.Id).NodeIds]
if not report["tip_node_ids"]:
    raise ValueError("Empty tip mesh selection.")
report["mesh"] = {"nodes": int(mesh.Nodes), "elements": int(mesh.Elements)}
report["mass"] = {
    "point_kg": q(mass.Mass, "kg"),
    "body_kg": q(body.Mass, "kg"),
    "location_ids": [int(v) for v in mass.Location.Ids],
    "behavior": str(mass.Behavior),
    "position_m": [
        q(getattr(mass, k), "m") for k in ("XCoordinate", "YCoordinate", "ZCoordinate")
    ],
    "tip_center_m": tip_center,
}
checkpoint("controls")
settings = analysis.AnalysisSettings
settings.SolverUnits = SolverUnitsControlType.ActiveSystem
settings.SolutionMethod = HarmonicMethod.Full
settings.FrequencySpacing = FrequencySpacingType.Linear
settings.RangeMinimum = Quantity(str(p["frequency_min_hz"]) + " [Hz]")
settings.RangeMaximum = Quantity(str(p["frequency_max_hz"]) + " [Hz]")
settings.SolutionIntervals = p["frequency_intervals"]
settings.MassCoefficient = p["mass_damping_s_inv"]
settings.StiffnessCoefficient = 0.0
settings.ConstantDampingRatio = 0.0
process = analysis.SolveConfiguration.SolveProcessSettings
process.MaxNumberOfCores = 4
process.DistributeSolution = False
fixed = analysis.AddFixedSupport()
fixed.Location = root
force = analysis.AddForce()
force.Location = tip
force.DefineBy = LoadDefineBy.Components
for component, value in zip(
    (force.XComponent, force.YComponent, force.ZComponent), (p["force_n"], 0, 0)
):
    component.Output.DiscreteValues = [Quantity(str(value) + " [N]")]
force.XPhaseAngle.Output.DiscreteValues = [Quantity("0 [deg]")]
report["controls"] = {
    "solution_method": str(settings.SolutionMethod),
    "frequency_spacing": str(settings.FrequencySpacing),
    "min_hz": q(settings.RangeMinimum, "Hz"),
    "max_hz": q(settings.RangeMaximum, "Hz"),
    "solution_intervals": int(settings.SolutionIntervals),
    "mass_damping_s_inv": float(settings.MassCoefficient),
    "stiffness_damping_s": float(settings.StiffnessCoefficient),
    "constant_damping_ratio": float(settings.ConstantDampingRatio),
    "max_cores": int(process.MaxNumberOfCores),
    "distributed": bool(process.DistributeSolution),
}
report["fixed_ids"] = [int(v) for v in fixed.Location.Ids]
report["force"] = {
    "location_ids": [int(v) for v in force.Location.Ids],
    "x_n": [q(v, "N") for v in force.XComponent.Output.DiscreteValues],
    "y_n": [q(v, "N") for v in force.YComponent.Output.DiscreteValues],
    "z_n": [q(v, "N") for v in force.ZComponent.Output.DiscreteValues],
    "x_phase_deg": [q(v, "deg") for v in force.XPhaseAngle.Output.DiscreteValues],
}
directional = analysis.Solution.AddDirectionalDeformation()
directional.Location = tip
directional.NormalOrientation = NormalOrientationType.XAxis
directional.Frequency = Quantity(str(p["frequency_max_hz"]) + " [Hz]")
directional.Amplitude = True
checkpoint("solve")
analysis.Solution.Solve(True)
analysis.Solution.EvaluateAllResults()
reader = analysis.GetResultsData()
try:
    reader_frequencies = [float(v) for v in reader.ListTimeFreq]
finally:
    reader.Dispose()
# Mechanical lists real/imaginary result records separately; DPF set IDs do not.
frequencies = []
for value in reader_frequencies:
    if not frequencies or value != frequencies[-1]:
        frequencies.append(value)
if reader_frequencies not in (frequencies, [f for f in frequencies for _ in range(2)]):
    raise ValueError("Unexpected native complex frequency-record layout.")
destination = os.path.join(p["output_dir"], "harmonic.rst")
if os.path.exists(destination):
    raise ValueError("Refusing to overwrite a native result.")
shutil.copyfile(str(analysis.ResultFileName), destination)
report["cases"]["harmonic"] = {
    "status": "solved",
    "solution_status": str(analysis.Solution.Status),
    "frequencies_hz": frequencies,
    "reader_frequencies_hz": reader_frequencies,
    "result_file": destination,
    "last_frequency_hz": q(directional.Frequency, "Hz"),
    "last_amplitude_m": q(directional.Average, "m"),
}
report["executed_apis"] = [
    "Ansys.ACT.Automation.Mechanical.Geometry.AddPointMass",
    "Ansys.ACT.Automation.Mechanical.Analysis.AddForce",
    "Ansys.ACT.Automation.Mechanical.Solution.Solve",
]
report["status"] = "complete"
checkpoint("complete")
mcp_result = report
