# Trusted product-side script. Run only in a new, isolated Workbench model.
import json
import math
import os
import shutil
import Ansys
from Ansys.ACT.Interfaces.Common import SelectionTypeEnum, MechanicalUnitSystem
from Ansys.Mechanical.DataModel.Enums import (
    CoordinateSystemAlignmentType,
    DataModelObjectCategory,
    ElementOrder,
    LoadDefineBy,
    MethodType,
    SolverUnitsControlType,
    LocationDefinitionMethod,
)

p = globals().get("ACCEPTANCE_PARAMETERS")
if not p:
    raise ValueError("An explicit acceptance runner must supply parameters.")
model = ExtAPI.DataModel.Project.Model
if len(model.Analyses) > 1 or (
    len(model.Analyses) == 1 and len(model.Analyses[0].Children) > 3
):
    raise ValueError("Refusing an existing analysis with loads/results.")
if model.NamedSelections is not None and len(model.NamedSelections.Children) > 0:
    raise ValueError("Refusing a model with existing named selections.")
ExtAPI.Application.ActiveUnitSystem = MechanicalUnitSystem.StandardMKS
report = {
    "case": "coordinate_equivalence",
    "status": "running",
    "cases": {},
    "executed_apis": [],
}


def checkpoint(phase):
    report["phase"] = phase
    with open(
        os.path.join(p["output_dir"], "coordinate.checkpoint.json"), "w"
    ) as stream:
        json.dump(report, stream, indent=2)


def q(value, unit):
    return float(Quantity(str(value)).ConvertUnit(unit).Value)


def selection(ids):
    value = ExtAPI.SelectionManager.CreateSelectionInfo(
        SelectionTypeEnum.GeometryEntities
    )
    value.Ids = list(ids)
    return value


def ids(location):
    if hasattr(location, "Ids"):
        return [int(value) for value in location.Ids]
    return [int(value) for value in location.Location.Ids]


checkpoint("geometry")
bodies = list(model.Geometry.GetChildren(DataModelObjectCategory.Body, True))
if not bodies:
    importer = model.GeometryImportGroup.AddGeometryImport()
    importer.Import(
        p["geometry_path"],
        Ansys.Mechanical.DataModel.Enums.GeometryImportPreference.Format.Automatic,
        Ansys.ACT.Mechanical.Utilities.GeometryImportPreferences(),
    )
    bodies = list(model.Geometry.GetChildren(DataModelObjectCategory.Body, True))
if len(bodies) != 1:
    raise ValueError("One beam body is required.")
body = bodies[0]
body.Material = "Structural Steel"
geo = body.GetGeoBody()
scale = q("1 [" + str(ExtAPI.DataModel.GeoData.Unit) + "]", "m")
faces = [(face, [float(v) * scale for v in face.Centroid]) for face in geo.Faces]
low, high = min(x[1][0] for x in faces), max(x[1][0] for x in faces)
extents = [max(x[1][i] for x in faces) - min(x[1][i] for x in faces) for i in range(3)]
if len(faces) != 6 or any(
    abs(extents[i] - p[key]) > 1e-9
    for i, key in enumerate(("length_m", "width_m", "height_m"))
):
    raise ValueError("Geometry does not match the beam reference.")
root_ids = [f.Id for f, x in faces if abs(x[0] - low) < 1e-9]
tip_ids = [f.Id for f, x in faces if abs(x[0] - high) < 1e-9]
if len(root_ids) != 1 or len(tip_ids) != 1:
    raise ValueError("Unique end-face selections required.")
named = {}
for name, face_ids in (("p4_root", root_ids), ("p4_tip", tip_ids)):
    value = model.AddNamedSelection()
    value.Name = name
    value.Location = selection(face_ids)
    named[name] = value
report["geometry"] = {
    "length_m": extents[0],
    "width_m": extents[1],
    "height_m": extents[2],
    "body_count": 1,
    "material": str(body.Material),
}
report["selections"] = {
    "root": ids(named["p4_root"]),
    "tip": ids(named["p4_tip"]),
    "expected_root": root_ids,
    "expected_tip": tip_ids,
    "root_object_id": int(named["p4_root"].ObjectId),
    "tip_object_id": int(named["p4_tip"].ObjectId),
}
report["executed_apis"].append(
    "Ansys.ACT.Automation.Mechanical.Model.AddNamedSelection"
)

checkpoint("coordinate")
global_cs = next(cs for cs in model.CoordinateSystems.Children if cs.IsGlobal)
frame = model.CoordinateSystems.AddCoordinateSystem()
frame.Name = "P4 Rotated Z"
frame.OriginDefineBy = CoordinateSystemAlignmentType.Fixed
frame.OriginX, frame.OriginY, frame.OriginZ = [
    Quantity(str(v) + " [m]") for v in p["origin_m"]
]
frame.PrimaryAxisDefineBy = CoordinateSystemAlignmentType.GlobalX
frame.RotateZ()
frame.SetTransformationValue(1, float(p["angle_deg"]))
report["coordinate"] = {
    "id": int(frame.CoordinateSystemID),
    "angle_deg": math.degrees(float(frame.GetTransformationValue(1))),
    "transformation_value_rad": float(frame.GetTransformationValue(1)),
    "basis": [
        [float(v) for v in axis] for axis in (frame.XAxis, frame.YAxis, frame.ZAxis)
    ],
    "origin_m": [q(frame.OriginX, "m"), q(frame.OriginY, "m"), q(frame.OriginZ, "m")],
    "transformation_count": int(frame.TransformationCount),
}
report["executed_apis"] += [
    "Ansys.ACT.Automation.Mechanical.CoordinateSystems.AddCoordinateSystem",
    "Ansys.ACT.Automation.Mechanical.CoordinateSystem.RotateZ",
    "Ansys.ACT.Automation.Mechanical.CoordinateSystem.SetTransformationValue",
]
checkpoint("mesh")
mesh = model.Mesh
mesh.ElementSize = Quantity("2 [mm]")
mesh.ElementOrder = ElementOrder.Quadratic
method = mesh.AddAutomaticMethod()
method.Location = selection([geo.Id])
method.Method = MethodType.Sweep
mesh.GenerateMesh()
report["mesh"] = {"nodes": int(mesh.Nodes), "elements": int(mesh.Elements)}

a = math.radians(float(p["angle_deg"]))
for name, coordinate, components in (
    ("global", global_cs, [0.0, -1.0, 0.0]),
    ("rotated", frame, [-math.sin(a), -math.cos(a), 0.0]),
):
    checkpoint(name + "_setup")
    analysis = (
        model.Analyses[0]
        if name == "global" and len(model.Analyses)
        else model.AddStaticStructuralAnalysis()
    )
    analysis.Name = "P4 " + name
    analysis.AnalysisSettings.SolverUnits = SolverUnitsControlType.ActiveSystem
    configuration = analysis.SolveConfiguration.SolveProcessSettings
    configuration.MaxNumberOfCores = 4
    configuration.DistributeSolution = False
    fixed = analysis.AddFixedSupport()
    fixed.Location = named["p4_root"]
    force = analysis.AddForce()
    force.Location = named["p4_tip"]
    force.DefineBy = LoadDefineBy.Components
    force.CoordinateSystem = coordinate
    for component, value in zip(
        (force.XComponent, force.YComponent, force.ZComponent), components
    ):
        component.Output.DiscreteValues = [Quantity(str(value) + " [N]")]
    total = analysis.Solution.AddTotalDeformation()
    reaction = analysis.Solution.AddForceReaction()
    reaction.LocationMethod = LocationDefinitionMethod.BoundaryCondition
    reaction.BoundaryConditionSelection = fixed
    reaction.Orientation = global_cs
    before = {
        "force_coordinate_id": int(force.CoordinateSystem.CoordinateSystemID),
        "force_n": [
            q(component.Output.DiscreteValues[0], "N")
            for component in (force.XComponent, force.YComponent, force.ZComponent)
        ],
        "root_ids": ids(fixed.Location),
        "tip_ids": ids(force.Location),
        "max_cores": int(configuration.MaxNumberOfCores),
        "distributed": bool(configuration.DistributeSolution),
    }
    report["cases"][name] = {"status": "setup", "inputs": before}
    checkpoint(name + "_solve")
    analysis.Solution.Solve(True)
    analysis.Solution.EvaluateAllResults()
    reaction.EvaluateAllResults()
    if not reaction.IsSolved:
        raise RuntimeError("The force-reaction probe has not been evaluated.")
    result = str(analysis.ResultFileName)
    if not os.path.isfile(result):
        raise RuntimeError("Native result is absent.")
    destination = os.path.join(p["output_dir"], name + ".rst")
    if os.path.exists(destination):
        raise ValueError("Refusing to overwrite an acceptance result.")
    shutil.copyfile(result, destination)
    table = os.path.join(p["output_dir"], name + "_deformation.txt")
    total.ExportToTextFile(table)
    report["cases"][name].update(
        status="solved",
        solution_status=str(analysis.Solution.Status),
        maximum_deformation_m=q(total.Maximum, "m"),
        reaction_n=[
            q(v, "N") for v in (reaction.XAxis, reaction.YAxis, reaction.ZAxis)
        ],
        result_file=destination,
        nodal_export=table,
    )
    checkpoint(name + "_solved")
report["status"] = "complete"
checkpoint("complete")
mcp_result = report
