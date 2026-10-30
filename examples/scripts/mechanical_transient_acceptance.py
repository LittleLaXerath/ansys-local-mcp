# Product-side transient benchmark. Only for a fresh, explicitly opted-in model.
import json
import os
import shutil
import Ansys
from Ansys.ACT.Interfaces.Common import SelectionTypeEnum, MechanicalUnitSystem
from Ansys.Mechanical.DataModel.Enums import (
    DataModelObjectCategory,
    ElementOrder,
    LoadDefineBy,
    MethodType,
    SolverUnitsControlType,
    AutomaticTimeStepping,
    TimeStepDefineByType,
    TimePointsOptions,
    TransientDampingType,
    LoadBehavior,
    NormalOrientationType,
)

p = globals().get("ACCEPTANCE_PARAMETERS")
if not p:
    raise ValueError("Use the explicit transient acceptance runner.")
model = ExtAPI.DataModel.Project.Model
if len(model.Analyses) != 1 or len(model.Analyses[0].Children) > 3:
    raise ValueError("Refusing an existing/nonempty transient analysis.")
analysis = model.Analyses[0]
if "Transient" not in str(analysis.AnalysisType):
    raise ValueError("A Transient Structural Workbench system is required.")
ExtAPI.Application.ActiveUnitSystem = MechanicalUnitSystem.StandardMKS
report = {
    "case": "transient_sdof",
    "status": "running",
    "cases": {},
    "executed_apis": [],
}


def checkpoint(phase):
    report["phase"] = phase
    with open(
        os.path.join(p["output_dir"], "transient.checkpoint.json"), "w"
    ) as stream:
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
faces = [(face, [float(v) * scale for v in face.Centroid]) for face in geo.Faces]
extents = [
    max(row[1][i] for row in faces) - min(row[1][i] for row in faces) for i in range(3)
]
if len(faces) != 6 or any(
    abs(extents[i] - p[key]) > 1e-9
    for i, key in enumerate(("length_m", "width_m", "height_m"))
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
mass.Name = "P4 SDOF end mass"
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
tip_nodes = [int(value) for value in mesh_data.MeshRegionById(tip_face.Id).NodeIds]
if not tip_nodes:
    raise ValueError("Tip mesh selection is empty.")
report["tip_node_ids"] = tip_nodes
report["mesh"] = {"nodes": int(mesh.Nodes), "elements": int(mesh.Elements)}
report["mass"] = {
    "point_kg": q(mass.Mass, "kg"),
    "body_kg": q(body.Mass, "kg"),
    "location_ids": [int(value) for value in mass.Location.Ids],
    "behavior": str(mass.Behavior),
    "position_m": [
        q(getattr(mass, member), "m")
        for member in ("XCoordinate", "YCoordinate", "ZCoordinate")
    ],
    "tip_center_m": tip_center,
}
checkpoint("controls")
settings = analysis.AnalysisSettings
settings.SolverUnits = SolverUnitsControlType.ActiveSystem
settings.NumberOfSteps = 1
settings.StepEndTime = Quantity(str(p["end_time_s"]) + " [s]")
settings.AutomaticTimeStepping = AutomaticTimeStepping.Off
settings.DefineBy = TimeStepDefineByType.Time
settings.TimeStep = Quantity(str(p["dt_s"]) + " [s]")
settings.StoreResultsAt = TimePointsOptions.AllTimePoints
settings.LargeDeflection = False
settings.TimeIntegration = True
settings.MassCoefficient = 0.0
settings.StiffnessCoefficient = 0.0
settings.NumericalDamping = TransientDampingType.NumericalDampingManual
settings.NumericalDampingValue = 0.0
process = analysis.SolveConfiguration.SolveProcessSettings
process.MaxNumberOfCores = 4
process.DistributeSolution = False
fixed = analysis.AddFixedSupport()
fixed.Location = root
force = analysis.AddForce()
force.Location = tip
force.DefineBy = LoadDefineBy.Components
for component, magnitude in zip(
    (force.XComponent, force.YComponent, force.ZComponent), (p["force_n"], 0, 0)
):
    component.Inputs[0].DiscreteValues = [
        Quantity("0 [s]"),
        Quantity(str(p["end_time_s"]) + " [s]"),
    ]
    component.Output.DiscreteValues = [
        Quantity(str(magnitude) + " [N]"),
        Quantity(str(magnitude) + " [N]"),
    ]
report["controls"] = {
    "dt_s": q(settings.TimeStep, "s"),
    "end_time_s": q(settings.StepEndTime, "s"),
    "time_integration": bool(settings.TimeIntegration),
    "large_deflection": bool(settings.LargeDeflection),
    "automatic_time_stepping": str(settings.AutomaticTimeStepping),
    "store_results_at": str(settings.StoreResultsAt),
    "mass_damping": float(settings.MassCoefficient),
    "stiffness_damping": float(settings.StiffnessCoefficient),
    "numerical_damping": float(settings.NumericalDampingValue),
    "max_cores": int(process.MaxNumberOfCores),
    "distributed": bool(process.DistributeSolution),
}
report["force"] = {
    "times_s": [q(v, "s") for v in force.XComponent.Inputs[0].DiscreteValues],
    "x_n": [q(v, "N") for v in force.XComponent.Output.DiscreteValues],
    "y_n": [q(v, "N") for v in force.YComponent.Output.DiscreteValues],
    "z_n": [q(v, "N") for v in force.ZComponent.Output.DiscreteValues],
}
directional = analysis.Solution.AddDirectionalDeformation()
directional.Location = tip
directional.NormalOrientation = NormalOrientationType.XAxis
directional.DisplayTime = Quantity(str(p["end_time_s"]) + " [s]")
checkpoint("solve")
analysis.Solution.Solve(True)
analysis.Solution.EvaluateAllResults()
reader = analysis.GetResultsData()
try:
    times = [float(value) for value in reader.ListTimeFreq]
finally:
    reader.Dispose()
destination = os.path.join(p["output_dir"], "transient.rst")
if os.path.exists(destination):
    raise ValueError("Refusing to overwrite a native result.")
shutil.copyfile(str(analysis.ResultFileName), destination)
report["cases"]["transient"] = {
    "status": "solved",
    "solution_status": str(analysis.Solution.Status),
    "times_s": times,
    "last_displacement_m": q(directional.Average, "m"),
    "result_file": destination,
}
report["executed_apis"] = [
    "Ansys.ACT.Automation.Mechanical.Geometry.AddPointMass",
    "Ansys.ACT.Automation.Mechanical.Analysis.AddForce",
    "Ansys.ACT.Automation.Mechanical.Solution.Solve",
]
report["status"] = "complete"
checkpoint("complete")
mcp_result = report
