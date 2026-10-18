# Runs in Mechanical/IronPython 2.7. A benign two-body compression benchmark.
# Source preparation is not proof that a product supports each operation.
import json
import os
import shutil
import Ansys
from Ansys.ACT.Interfaces.Common import SelectionTypeEnum
from Ansys.Mechanical.DataModel.Enums import (
    AutomaticTimeStepping, ContactBehavior, ContactFormulation, ContactType,
    DataModelObjectCategory, ElementOrder, MethodType,
)

p = globals().get("CONTACT_PARAMETERS")
if not p:
    raise ValueError("Use the contact/parameter acceptance runner.")
directory = p["output_dir"]
report = {"schema_version": 1, "stage": "begin", "time_samples": [], "parameters": {}}


def checkpoint(stage):
    report["stage"] = stage
    with open(os.path.join(directory, "contact.checkpoint.json"), "w") as stream:
        json.dump(report, stream, indent=2)


def quantity(value, unit):
    return float(Quantity(str(value)).ConvertUnit(unit).Value)


def scalar(value):
    return float(value.Value) if hasattr(value, "Value") else float(value)


def selection(ids):
    result = ExtAPI.SelectionManager.CreateSelectionInfo(SelectionTypeEnum.GeometryEntities)
    result.Ids = list(ids)
    return result


model = ExtAPI.DataModel.Project.Model
if len(model.Analyses) > 1 or (len(model.Analyses) == 1 and len(model.Analyses[0].Children) > 3):
    raise ValueError("Contact acceptance refuses an existing populated user model.")
bodies = list(model.Geometry.GetChildren(DataModelObjectCategory.Body, True))
if not bodies:
    importer = model.GeometryImportGroup.AddGeometryImport()
    importer.Import(p["geometry_path"], Ansys.Mechanical.DataModel.Enums.GeometryImportPreference.Format.Automatic,
                    Ansys.ACT.Mechanical.Utilities.GeometryImportPreferences())
    bodies = list(model.Geometry.GetChildren(DataModelObjectCategory.Body, True))
if len(bodies) != 2:
    raise RuntimeError("Contact acceptance requires exactly two independent solids.")
scale = quantity(Quantity("1 [" + str(ExtAPI.DataModel.GeoData.Unit) + "]"), "m")
records = []
for body in bodies:
    body.Material = "Structural Steel"
    if str(body.Material) != "Structural Steel":
        raise RuntimeError("Structural Steel assignment did not read back.")
    geometry = body.GetGeoBody()
    faces = [(face, float(face.Centroid[0]) * scale) for face in geometry.Faces]
    if len(faces) != 6:
        raise RuntimeError("Compression segments must each have six planar faces.")
    records.append((min(value for face, value in faces), max(value for face, value in faces), body, geometry, faces))
records.sort(key=lambda item: item[0])
segment_m = float(p["segment_length_mm"]) / 1000.0
if abs(records[0][1] - records[1][0]) > 1e-9 or abs(records[1][1] - records[0][0] - 2 * segment_m) > 1e-8:
    raise RuntimeError("Compression faces are not touching at the expected geometry scale.")


def end_face(record, x):
    ids = [face.Id for face, value in record[4] if abs(value - x) < 1e-9]
    if len(ids) != 1:
        raise RuntimeError("A compression end face is ambiguous.")
    return ids[0]


root_ids = [end_face(records[0], records[0][0])]
target_ids = [end_face(records[0], records[0][1])]
source_ids = [end_face(records[1], records[1][0])]
tip_ids = [end_face(records[1], records[1][1])]
for name, ids in (("compression_root", root_ids), ("compression_tip", tip_ids)):
    named = model.AddNamedSelection()
    named.Name = name
    named.Location = selection(ids)

# The model is fresh; remove its automatically generated contact groups so the
# one deliberate frictional interface is the only load-transfer path.
for child in list(model.Connections.Children):
    child.Delete()
contact = model.Connections.AddContactRegion()
contact.Name = "Acceptance Frictional Interface"
contact.SourceLocation = selection(source_ids)
contact.TargetLocation = selection(target_ids)
contact.ContactType = ContactType.Frictional
contact.FrictionCoefficient = float(p["friction_coefficient"])
contact.Behavior = ContactBehavior.Asymmetric
contact.ContactFormulation = ContactFormulation.AugmentedLagrange
if str(contact.ContactType) != "Frictional" or abs(float(contact.FrictionCoefficient) - float(p["friction_coefficient"])) > 1e-12:
    raise RuntimeError("Frictional contact properties did not read back.")
report["contact"] = {"type": str(contact.ContactType), "formulation": str(contact.ContactFormulation),
                     "friction_coefficient": float(contact.FrictionCoefficient),
                     "source_ids": list(contact.SourceLocation.Ids), "target_ids": list(contact.TargetLocation.Ids)}
checkpoint("contact_created")

mesh = model.Mesh
mesh.ElementSize = Quantity("2 [mm]")
mesh.ElementOrder = ElementOrder.Quadratic
for record in records:
    method = mesh.AddAutomaticMethod()
    method.Location = selection([record[3].Id])
    method.Method = MethodType.Sweep
mesh.GenerateMesh()
report["mesh"] = {"nodes": int(mesh.Nodes), "elements": int(mesh.Elements)}
if min(report["mesh"].values()) <= 0:
    raise RuntimeError("Compression mesh is empty.")
checkpoint("mesh_complete")

analysis = model.Analyses[0] if len(model.Analyses) else model.AddStaticStructuralAnalysis()
analysis.Name = "Acceptance Contact Compression"
settings = analysis.AnalysisSettings
# Preserve the product's active consistent solver-unit system. The earlier
# acceptance tests explicitly established this contract on the tested release.
settings.NumberOfSteps = 2
settings.LargeDeflection = False
for step in (1, 2):
    settings.CurrentStepNumber = step
    settings.StepEndTime = Quantity(str(step) + " [s]")
    settings.AutomaticTimeStepping = AutomaticTimeStepping.On
    settings.InitialSubsteps = 10
    settings.MinimumSubsteps = 5
    settings.MaximumSubsteps = 100
remote = analysis.AddRemoteDisplacement()
remote.Name = "Acceptance Root Remote Displacement"
remote.Location = selection(root_ids)
for component in (remote.XComponent, remote.YComponent, remote.ZComponent):
    component.Output.DiscreteValues = [Quantity("0 [m]")]
for component in (remote.RotationX, remote.RotationY, remote.RotationZ):
    component.Output.DiscreteValues = [Quantity("0 [rad]")]
pressure = analysis.AddPressure()
pressure.Name = "Acceptance Tabular Pressure"
pressure.Location = selection(tip_ids)
pressure.Magnitude.Inputs[0].DiscreteValues = [Quantity("0 [s]"), Quantity("1 [s]"), Quantity("2 [s]")]
base_pressure = float(p["pressure_pa"])
pressure.Magnitude.Output.DiscreteValues = [Quantity("0 [Pa]"), Quantity(str(base_pressure) + " [Pa]"), Quantity(str(2 * base_pressure) + " [Pa]")]
report["pressure_table"] = {"times_s": [quantity(item, "s") for item in pressure.Magnitude.Inputs[0].DiscreteValues],
                            "values_pa": [quantity(item, "Pa") for item in pressure.Magnitude.Output.DiscreteValues]}
total = analysis.Solution.AddTotalDeformation()
reaction = analysis.Solution.AddForceReaction()
reaction.BoundaryConditionSelection = remote
contact_tool = analysis.Solution.AddContactTool()
status = contact_tool.AddStatus()
contact_pressure = contact_tool.AddPressure()
checkpoint("loads_and_results_complete")

input_parameter = contact.CreateParameter("FrictionCoefficient")
output_parameter = total.CreateParameter("Maximum")
report["parameters"] = {"input": {"id": str(input_parameter.ID), "property_name": str(input_parameter.PropertyName)},
                        "output": {"id": str(output_parameter.ID), "property_name": str(output_parameter.PropertyName)}}
checkpoint("parameters_created")
checkpoint("before_solve")
analysis.Solution.Solve(True)
analysis.Solution.EvaluateAllResults()
checkpoint("after_solve")
for second in (1, 2):
    for result in (total, reaction, status, contact_pressure):
        result.DisplayTime = Quantity(str(second) + " [s]")
    analysis.Solution.EvaluateAllResults()
    report["time_samples"].append({"time_s": second, "maximum_deformation_m": quantity(total.Maximum, "m"),
                                   "reaction_x_n": quantity(reaction.XAxis, "N"),
                                   "contact_status_min": scalar(status.Minimum), "contact_status_max": scalar(status.Maximum),
                                   "average_contact_pressure_pa": quantity(contact_pressure.Average, "Pa")})
    status.ExportToTextFile(os.path.join(directory, "contact_status_" + str(second) + ".txt"))
    total.ExportToTextFile(os.path.join(directory, "deformation_" + str(second) + ".txt"))
source = str(analysis.ResultFileName)
if not os.path.isfile(source):
    raise RuntimeError("Compression solve did not produce its result file.")
shutil.copyfile(source, os.path.join(directory, "contact.rst"))
report["solution_status"] = str(analysis.Solution.Status)
report["solver_units"] = str(settings.SolverUnitSystem)
checkpoint("complete")
mcp_result = report
