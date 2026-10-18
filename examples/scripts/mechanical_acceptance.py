# Runs inside Mechanical's product Python interpreter (including IronPython).
# It is deliberately opt-in and refuses an existing user model.
import json
import math
import os
import shutil
import traceback
import Ansys
from Ansys.ACT.Interfaces.Common import SelectionTypeEnum
from Ansys.Mechanical.DataModel.Enums import (
    DataModelObjectCategory, ElementOrder, LoadDefineBy, MethodType,
    SolverUnitsControlType, WBUnitSystemType,
)

if "MechanicalUnitSystem" not in globals():
    from Ansys.ACT.Interfaces.Common import MechanicalUnitSystemEnum as MechanicalUnitSystem

parameters = globals().get("ACCEPTANCE_PARAMETERS")
if not parameters:
    raise ValueError("Use the opt-in product acceptance runner to supply parameters.")
output_dir = parameters["output_dir"]
model = ExtAPI.DataModel.Project.Model
if len(model.Analyses) > 1:
    raise ValueError("Acceptance requires a new project, not an existing multi-analysis model.")
if len(model.Analyses) == 1 and len(model.Analyses[0].Children) > 3:
    raise ValueError("Acceptance refuses a model with existing loads or results.")
ExtAPI.Application.ActiveUnitSystem = MechanicalUnitSystem.StandardMKS


def selection(ids):
    result = ExtAPI.SelectionManager.CreateSelectionInfo(SelectionTypeEnum.GeometryEntities)
    result.Ids = list(ids)
    return result


def quantity_value(value, unit):
    converted = Quantity(str(value))
    converted = converted.ConvertUnit(unit)
    return float(converted.Value)


def preserved_result(analysis, name):
    source = str(analysis.ResultFileName)
    if not os.path.isfile(source):
        raise RuntimeError("The solver did not produce a result file.")
    target = os.path.join(output_dir, name)
    if os.path.exists(target):
        raise ValueError("An acceptance result file already exists.")
    shutil.copyfile(source, target)
    return target


def solver_units(analysis):
    settings = analysis.AnalysisSettings
    # Let Mechanical derive its consistent solver system from the explicitly
    # chosen MKS display system. Result quantities are converted on readback.
    settings.SolverUnits = SolverUnitsControlType.ActiveSystem


def save_checkpoint(report):
    path = os.path.join(output_dir, "mechanical.checkpoint.json")
    with open(path, "w") as stream:
        json.dump(report, stream, indent=2)


def checkpoint(case_name, phase, analysis=None):
    report["active_case"] = case_name
    report["active_phase"] = phase
    report.setdefault("progress", []).append({"case": case_name, "phase": phase})
    if analysis is not None:
        report.setdefault("solver_directories", {})[case_name] = str(analysis.WorkingDir)
        report.setdefault("solver_units", {})[case_name] = {"control": str(analysis.AnalysisSettings.SolverUnits),
                                                           "system": str(analysis.AnalysisSettings.SolverUnitSystem)}
    save_checkpoint(report)


report = {"schema_version": 1, "cases": {}, "geometry": {}, "mesh": {},
          "requested_cases": parameters.get("cases", ["static", "modal", "thermal"])}
body_objects = list(model.Geometry.GetChildren(DataModelObjectCategory.Body, True))
if not body_objects:
    importer = model.GeometryImportGroup.AddGeometryImport()
    importer.Import(parameters["geometry_path"], Ansys.Mechanical.DataModel.Enums.GeometryImportPreference.Format.Automatic,
                    Ansys.ACT.Mechanical.Utilities.GeometryImportPreferences())
    body_objects = list(model.Geometry.GetChildren(DataModelObjectCategory.Body, True))
if len(body_objects) != 1:
    raise RuntimeError("Expected exactly one imported acceptance body.")
body = body_objects[0]
body.Material = "Structural Steel"
if str(body.Material) != "Structural Steel":
    raise RuntimeError("Material assignment did not read back.")
geo_body = body.GetGeoBody()
faces = list(geo_body.Faces)
if len(faces) != 6:
    raise RuntimeError("Acceptance body must have six planar faces.")
geometry_unit = str(ExtAPI.DataModel.GeoData.Unit)
geometry_scale = quantity_value(Quantity("1 [" + geometry_unit + "]"), "m")
centroids = [(face, [float(item) * geometry_scale for item in face.Centroid]) for face in faces]
low = min(item[1][0] for item in centroids)
high = max(item[1][0] for item in centroids)
length_m = float(parameters["length_mm"]) / 1000.0
if abs(high - low - length_m) > 1e-8:
    raise RuntimeError("Geometry units or beam orientation differ from the acceptance contract: extent=" + str(high - low) + " m, CAD unit=" + geometry_unit)
root_faces = [face.Id for face, point in centroids if abs(point[0] - low) < 1e-8]
tip_faces = [face.Id for face, point in centroids if abs(point[0] - high) < 1e-8]
if len(root_faces) != 1 or len(tip_faces) != 1:
    raise RuntimeError("Root/tip geometry selection is ambiguous.")
root_selection = selection(root_faces)
tip_selection = selection(tip_faces)
report["geometry"] = {"bodies": 1, "faces": 6, "length_m": high - low, "material": str(body.Material),
                      "root_face_ids": root_faces, "tip_face_ids": tip_faces, "cad_unit": geometry_unit}
for name, location in (("acceptance_root", root_selection), ("acceptance_tip", tip_selection)):
    named = model.AddNamedSelection()
    named.Name = name
    named.Location = location
mesh = model.Mesh
mesh.ElementSize = Quantity("2 [mm]")
mesh.ElementOrder = ElementOrder.Quadratic
method = mesh.AddAutomaticMethod()
method.Location = selection([geo_body.Id])
method.Method = MethodType.Sweep
mesh.GenerateMesh()
report["mesh"] = {"nodes": int(mesh.Nodes), "elements": int(mesh.Elements), "element_size_m": 0.002,
                  "element_order": str(mesh.ElementOrder), "method": str(method.Method)}
if report["mesh"]["nodes"] <= 0 or report["mesh"]["elements"] <= 0:
    raise RuntimeError("Mesh generation produced no nodes or elements.")
save_checkpoint(report)


def static_case():
    checkpoint("static", "before_analysis")
    existing_analysis = len(model.Analyses) > 0
    analysis = model.Analyses[0] if existing_analysis else model.AddStaticStructuralAnalysis()
    analysis.Name = "Acceptance Static"
    checkpoint("static", "before_units", analysis)
    solver_units(analysis)
    checkpoint("static", "before_fixed_support", analysis)
    fixed = analysis.AddFixedSupport()
    fixed.Location = root_selection
    checkpoint("static", "before_force", analysis)
    force = analysis.AddForce()
    force.Location = tip_selection
    force.DefineBy = LoadDefineBy.Components
    force.XComponent.Output.DiscreteValues = [Quantity("0 [N]")]
    force.YComponent.Output.DiscreteValues = [Quantity("-1 [N]")]
    force.ZComponent.Output.DiscreteValues = [Quantity("0 [N]")]
    checkpoint("static", "before_total_deformation", analysis)
    total = analysis.Solution.AddTotalDeformation()
    checkpoint("static", "before_force_reaction", analysis)
    reaction = analysis.Solution.AddForceReaction()
    reaction.BoundaryConditionSelection = fixed
    checkpoint("static", "before_solve", analysis)
    analysis.Solution.Solve(True)
    checkpoint("static", "after_solve", analysis)
    analysis.Solution.EvaluateAllResults()
    checkpoint("static", "after_evaluation", analysis)
    export_path = os.path.join(output_dir, "static_deformation.txt")
    total.ExportToTextFile(export_path)
    checkpoint("static", "after_export", analysis)
    return {"status": "solved", "solution_status": str(analysis.Solution.Status),
            "creation_api": None if existing_analysis else "Model.AddStaticStructuralAnalysis",
            "maximum_deformation_m": quantity_value(total.Maximum, "m"),
            "reaction_y_n": quantity_value(reaction.YAxis, "N"),
            "nodal_export": export_path, "result_file": preserved_result(analysis, "static.rst")}


def modal_case():
    checkpoint("modal", "before_analysis")
    analysis = model.AddModalAnalysis()
    analysis.Name = "Acceptance Modal"
    checkpoint("modal", "before_units", analysis)
    solver_units(analysis)
    analysis.AnalysisSettings.MaximumModesToFind = 6
    fixed = analysis.AddFixedSupport()
    fixed.Location = root_selection
    deformation = analysis.Solution.AddTotalDeformation()
    checkpoint("modal", "before_solve", analysis)
    analysis.Solution.Solve(True)
    checkpoint("modal", "after_solve", analysis)
    analysis.Solution.EvaluateAllResults()
    checkpoint("modal", "after_evaluation", analysis)
    reader = analysis.GetResultsData()
    try:
        frequencies = [float(value) for value in reader.ListTimeFreq]
    finally:
        reader.Dispose()
    if not frequencies or min(frequencies) <= 0:
        raise RuntimeError("Modal solve produced no positive frequencies.")
    return {"status": "solved", "solution_status": str(analysis.Solution.Status),
            "frequencies_hz": frequencies, "result_file": preserved_result(analysis, "modal.rst")}


def thermal_case():
    checkpoint("thermal", "before_analysis")
    analysis = model.AddSteadyStateThermalAnalysis()
    analysis.Name = "Acceptance Thermal"
    checkpoint("thermal", "before_units", analysis)
    solver_units(analysis)
    cold = analysis.AddTemperature()
    cold.Location = root_selection
    cold.Magnitude.Output.DiscreteValues = [Quantity("0 [C]")]
    hot = analysis.AddTemperature()
    hot.Location = tip_selection
    hot.Magnitude.Output.DiscreteValues = [Quantity("100 [C]")]
    checkpoint("thermal", "before_results", analysis)
    temperature = analysis.Solution.AddTemperature()
    flux = analysis.Solution.AddTotalHeatFlux()
    checkpoint("thermal", "before_solve", analysis)
    analysis.Solution.Solve(True)
    checkpoint("thermal", "after_solve", analysis)
    analysis.Solution.EvaluateAllResults()
    checkpoint("thermal", "after_evaluation", analysis)
    temperature.ExportToTextFile(os.path.join(output_dir, "thermal_temperature.txt"))
    flux.ExportToTextFile(os.path.join(output_dir, "thermal_flux.txt"))
    return {"status": "solved", "solution_status": str(analysis.Solution.Status),
            "minimum_temperature_c": quantity_value(temperature.Minimum, "C"),
            "maximum_temperature_c": quantity_value(temperature.Maximum, "C"),
            "average_heat_flux_w_m2": quantity_value(flux.Average, "W m^-2"),
            "result_file": preserved_result(analysis, "thermal.rth")}


for name, case in (("static", static_case), ("modal", modal_case), ("thermal", thermal_case)):
    if name not in report["requested_cases"]:
        continue
    try:
        report["cases"][name] = case()
    except Exception as error:
        report["cases"][name] = {"status": "failed", "error": str(error), "traceback": traceback.format_exc()}
    save_checkpoint(report)
mcp_result = report
