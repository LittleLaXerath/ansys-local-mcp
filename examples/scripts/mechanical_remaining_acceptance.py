# Trusted product-side basic analyses. Only the opt-in runner supplies parameters.
import json
import math
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
    NormalOrientationType,
    ModesCombinationType,
    AutomaticTimeStepping,
    TimeStepDefineByType,
    TimePointsOptions,
    InitialTemperatureType,
    ScaleFactorType,
    ConstantDampingType,
)

p = globals().get("ACCEPTANCE_PARAMETERS")
if not p:
    raise ValueError("Use the explicit basic-analysis acceptance runner.")
model = ExtAPI.DataModel.Project.Model
if len(model.Analyses) != 1 or len(model.Analyses[0].Children) > 3:
    raise ValueError("Refusing an existing/nonempty analysis.")
ExtAPI.Application.ActiveUnitSystem = MechanicalUnitSystem.StandardMKS
case = p["case"]
report = {
    "case": case,
    "status": "running",
    "cases": {},
    "controls": {},
    "executed_apis": [],
    "native_tables": {},
    "solver_logs": {},
}


def checkpoint(phase):
    report["phase"] = phase
    with open(os.path.join(p["output_dir"], "remaining.checkpoint.json"), "w") as f:
        json.dump(report, f, indent=2)


def q(value, unit):
    return float(Quantity(str(value)).ConvertUnit(unit).Value)


def selection(ids):
    result = ExtAPI.SelectionManager.CreateSelectionInfo(
        SelectionTypeEnum.GeometryEntities
    )
    result.Ids = list(ids)
    return result


def configure(analysis):
    analysis.AnalysisSettings.SolverUnits = SolverUnitsControlType.ActiveSystem
    process = analysis.SolveConfiguration.SolveProcessSettings
    process.MaxNumberOfCores = 4
    process.DistributeSolution = False
    report["controls"][str(analysis.ObjectId)] = {
        "max_cores": int(process.MaxNumberOfCores),
        "distributed": bool(process.DistributeSolution),
        "type": str(analysis.AnalysisType),
        "solver_units": str(analysis.AnalysisSettings.SolverUnitSystem),
    }


def preserve(source, name):
    target = os.path.join(p["output_dir"], name)
    if os.path.exists(target) or not os.path.isfile(source):
        raise ValueError("Missing source or existing acceptance artifact: " + name)
    shutil.copyfile(source, target)
    return target


def solve(analysis, name, extension="rst"):
    checkpoint(name + "_solve")
    analysis.Solution.Solve(True)
    analysis.Solution.EvaluateAllResults()
    report.setdefault("solve_diagnostics", {})[name] = {
        "status": str(analysis.Solution.Status),
        "working_dir": str(analysis.WorkingDir),
        "children": [
            {"name": str(obj.Name), "state": str(obj.ObjectState)}
            for obj in analysis.Children
        ],
        "messages": [
            {
                "text": str(getattr(message, "DisplayString", message)),
                "severity": str(getattr(message, "Severity", "unknown")),
            }
            for message in ExtAPI.Application.Messages
        ],
    }
    checkpoint(name + "_readback")
    reader = analysis.GetResultsData()
    if reader is None:
        raise RuntimeError(
            "Mechanical did not expose a result reader; inspect the retained solve diagnostics."
        )
    try:
        axis = [float(v) for v in reader.ListTimeFreq]
    finally:
        reader.Dispose()
    native = preserve(str(analysis.ResultFileName), name + "." + extension)
    log = os.path.join(str(analysis.WorkingDir), "solve.out")
    if os.path.isfile(log):
        report["solver_logs"][name] = preserve(log, name + ".solve.txt")
    report["cases"][name] = {
        "status": "solved",
        "solution_status": str(analysis.Solution.Status),
        "native_axis": axis,
        "result_file": native,
        "analysis_id": int(analysis.ObjectId),
    }
    return report["cases"][name]


def directional(analysis, name, location):
    result = analysis.Solution.AddDirectionalDeformation()
    result.Name = name
    result.Location = location
    result.NormalOrientation = NormalOrientationType.XAxis
    return result


def export_table(result, name):
    path = os.path.join(p["output_dir"], name + ".txt")
    if os.path.exists(path):
        raise ValueError("Refusing an existing result table.")
    result.ExportToTextFile(path)
    report["native_tables"][name] = path
    return path


checkpoint("geometry")
model.Materials.RefreshMaterials()
bodies = list(model.Geometry.GetChildren(DataModelObjectCategory.Body, True))
if len(bodies) != 1:
    raise ValueError("Exactly one benchmark body is required.")
body = bodies[0]
body.Material = p["material_name"]
geo = body.GetGeoBody()
scale = q("1 [" + str(ExtAPI.DataModel.GeoData.Unit) + "]", "m")
faces = [(f, [float(v) * scale for v in f.Centroid]) for f in geo.Faces]
extents = [max(r[1][i] for r in faces) - min(r[1][i] for r in faces) for i in range(3)]
if len(faces) != 6 or any(
    abs(extents[i] - p[k]) > 1e-9
    for i, k in enumerate(("length_m", "width_m", "height_m"))
):
    raise ValueError("Unexpected benchmark dimensions/units.")
root_face, root_center = min(faces, key=lambda r: r[1][0])
tip_face, tip_center = max(faces, key=lambda r: r[1][0])
root, tip, whole = (
    selection([root_face.Id]),
    selection([tip_face.Id]),
    selection([geo.Id]),
)
report["geometry"] = {
    "length_m": extents[0],
    "width_m": extents[1],
    "height_m": extents[2],
    "material": str(body.Material),
    "root_ids": [root_face.Id],
    "tip_ids": [tip_face.Id],
    "body_ids": [geo.Id],
}
mesh = model.Mesh
mesh.ElementSize = Quantity(str(p["mesh_size_m"]) + " [m]")
mesh.ElementOrder = ElementOrder.Quadratic
method = mesh.AddAutomaticMethod()
method.Location = whole
method.Method = MethodType.Sweep
mesh.GenerateMesh()
data = ExtAPI.DataModel.MeshDataByName("Global")
report["mesh"] = {"nodes": int(mesh.Nodes), "elements": int(mesh.Elements)}
report["tip_node_ids"] = [int(v) for v in data.MeshRegionById(tip_face.Id).NodeIds]
report["root_node_ids"] = [int(v) for v in data.MeshRegionById(root_face.Id).NodeIds]
report["mesh_coordinate_source_unit"] = str(ExtAPI.DataModel.GeoData.Unit)
report["node_x_m"] = {str(v): float(data.NodeById(v).X) * scale for v in data.NodeIds}
if abs(max(report["node_x_m"].values()) - p["length_m"]) > 1e-9:
    raise ValueError("Mesh coordinate units do not match the CAD length readback.")
checkpoint("analysis")
initial = model.Analyses[0]

if case in ("response_spectrum", "random_vibration"):
    modal = initial
    configure(modal)
    modal.AnalysisSettings.MaximumModesToFind = 6
    mass = model.Geometry.AddPointMass()
    mass.Location = tip
    mass.Behavior = LoadBehavior.Rigid
    mass.Mass = Quantity(str(p["mass_kg"]) + " [kg]")
    for key, value in zip(("XCoordinate", "YCoordinate", "ZCoordinate"), tip_center):
        setattr(mass, key, Quantity(str(value) + " [m]"))
    fixed = modal.AddFixedSupport()
    fixed.Location = root
    mode = directional(modal, "Axial mode", tip)
    report["mass"] = {
        "point_kg": q(mass.Mass, "kg"),
        "body_kg": q(body.Mass, "kg"),
        "location_ids": [int(v) for v in mass.Location.Ids],
        "behavior": str(mass.Behavior),
    }
    report["fixed_ids"] = [int(v) for v in fixed.Location.Ids]
    report["lateral_constraint"] = {"enabled": False}
    solve(modal, "modal")
    report["modal_tip_shape"] = q(mode.Average, "m")
    analysis = (
        model.AddResponseSpectrumAnalysis()
        if case == "response_spectrum"
        else model.AddRandomVibrationAnalysis()
    )
    configure(analysis)
    analysis.InitialConditions[0].ModalICEnvironment = modal
    settings = analysis.AnalysisSettings
    if case == "response_spectrum":
        settings.ModesCombinationType = ModesCombinationType.SRSS
        load = analysis.AddRSAcceleration()
        load.MissingMassEffect = False
        load.RigidResponseEffect = False
        load.ScaleFactor = 1.0
        frequencies, values, unit = p["spectrum_hz"], p["spectrum_values"], "m s^-2"
        name = "response"
    else:
        settings.ConstantDamping = ConstantDampingType.Manual
        report["damping_properties"] = {
            name: {
                "value": str(getattr(settings, name)),
                "editable": bool(settings.IsPropertyEditable(name)),
            }
            for name in (
                "DampingRatio",
                "ConstantDampingRatio",
                "MassCoefficient",
                "StiffnessCoefficient",
            )
        }
        checkpoint("random_damping")
        settings.ConstantDampingRatio = p["damping_ratio"]
        settings.MassCoefficient = 0.0
        settings.StiffnessCoefficient = 0.0
        settings.ModeSignificanceLevel = 0.0
        load = analysis.AddPSDAcceleration()
        frequencies, values, unit = p["spectrum_hz"], p["spectrum_values"], "m^2 s^-3"
        name = "random"
    load.BoundaryCondition = fixed
    load.Direction = NormalOrientationType.XAxis
    load.LoadData.Inputs[0].DiscreteValues = [
        Quantity(str(v) + " [Hz]") for v in frequencies
    ]
    load.LoadData.Output.DiscreteValues = [
        Quantity(str(v) + " [" + unit + "]") for v in values
    ]
    result = directional(analysis, "Axial response", tip)
    if case == "random_vibration":
        result.ScaleFactor = ScaleFactorType.Sigma1
    report["spectrum"] = {
        "frequency_hz": [q(v, "Hz") for v in load.LoadData.Inputs[0].DiscreteValues],
        "values": [q(v, unit) for v in load.LoadData.Output.DiscreteValues],
        "unit": unit,
        "direction": str(load.Direction),
        "boundary_id": int(load.BoundaryCondition.ObjectId),
        "fixed_id": int(fixed.ObjectId),
        "damping_ratio": p["damping_ratio"]
        if case == "response_spectrum"
        else float(settings.ConstantDampingRatio),
        "damping_role": "input_pseudo_acceleration_spectrum_label"
        if case == "response_spectrum"
        else "native_constant_modal_damping",
        "modal_id": int(analysis.InitialConditions[0].ModalICEnvironment.ObjectId),
        "expected_modal_id": int(modal.ObjectId),
    }
    if case == "response_spectrum":
        report["spectrum"].update(
            combination=str(settings.ModesCombinationType),
            missing_mass=bool(load.MissingMassEffect),
            rigid_response=bool(load.RigidResponseEffect),
        )
    else:
        report["spectrum"]["mode_significance"] = float(settings.ModeSignificanceLevel)
        report["spectrum"].update(
            constant_damping=str(settings.ConstantDamping),
            mass_damping=float(settings.MassCoefficient),
            stiffness_damping=float(settings.StiffnessCoefficient),
        )
    solved = solve(analysis, name)
    solved["displacement_m"] = q(result.Average, "m")
    if case == "random_vibration":
        solved["sigma_scale"] = str(result.ScaleFactor)
        solved["result_record"] = {
            "load_step": int(result.LoadStep),
            "substep": int(result.Substep),
            "iteration": int(result.IterationNumber),
        }
    solved["table"] = export_table(result, name)
    report["executed_apis"] = [
        "Ansys.ACT.Automation.Mechanical.Model.Add"
        + (
            "ResponseSpectrumAnalysis"
            if case == "response_spectrum"
            else "RandomVibrationAnalysis"
        )
    ]

elif case == "buckling":
    static = initial
    configure(static)
    fixed = static.AddFixedSupport()
    fixed.Location = root
    force = static.AddForce()
    force.Location = tip
    force.DefineBy = LoadDefineBy.Components
    for field, value in zip(
        (force.XComponent, force.YComponent, force.ZComponent),
        (-p["compression_n"], 0, 0),
    ):
        field.Output.DiscreteValues = [Quantity(str(value) + " [N]")]
    static.Solution.AddTotalDeformation()
    solve(static, "prestress")
    analysis = model.AddEigenvalueBucklingAnalysis()
    configure(analysis)
    analysis.InitialConditions[0].PreStressICEnvironment = static
    analysis.AnalysisSettings.MaximumModesToFind = 2
    mode = analysis.Solution.AddTotalDeformation()
    result = solve(analysis, "buckling")
    result["load_multiplier"] = float(mode.LoadMultiplier)
    report["buckling"] = {
        "fixed_ids": [int(v) for v in fixed.Location.Ids],
        "force_ids": [int(v) for v in force.Location.Ids],
        "force_n": [
            q(field.Output.DiscreteValues[0], "N")
            for field in (force.XComponent, force.YComponent, force.ZComponent)
        ],
        "prestress_id": int(
            analysis.InitialConditions[0].PreStressICEnvironment.ObjectId
        ),
        "static_id": int(static.ObjectId),
    }
    export_table(mode, "buckling")
    report["executed_apis"] = [
        "Ansys.ACT.Automation.Mechanical.Model.AddEigenvalueBucklingAnalysis"
    ]

elif case == "transient_heat":
    analysis = initial
    configure(analysis)
    settings = analysis.AnalysisSettings
    settings.StepEndTime = Quantity(str(p["end_time_s"]) + " [s]")
    settings.AutomaticTimeStepping = AutomaticTimeStepping.Off
    settings.DefineBy = TimeStepDefineByType.Time
    settings.TimeStep = Quantity(str(p["dt_s"]) + " [s]")
    settings.StoreResultsAt = TimePointsOptions.AllTimePoints
    analysis.InitialConditions[0].InitialTemperature = InitialTemperatureType.Uniform
    analysis.InitialConditions[0].InitialTemperatureValue = Quantity("0 [C]")
    for location, value in ((root, 0), (tip, p["hot_c"])):
        temperature = analysis.AddTemperature()
        temperature.Location = location
        temperature.Magnitude.Output.DiscreteValues = [Quantity(str(value) + " [C]")]
    temp = analysis.Solution.AddTemperature()
    temp.DisplayTime = Quantity(str(p["end_time_s"]) + " [s]")
    report["thermal"] = {
        "initial_c": q(analysis.InitialConditions[0].InitialTemperatureValue, "C"),
        "end_time_s": q(settings.StepEndTime, "s"),
        "dt_s": q(settings.TimeStep, "s"),
        "auto": str(settings.AutomaticTimeStepping),
        "store": str(settings.StoreResultsAt),
        "cold_ids": [root_face.Id],
        "hot_ids": [tip_face.Id],
        "cold_c": 0,
        "hot_c": q(temperature.Magnitude.Output.DiscreteValues[0], "C"),
    }
    result = solve(analysis, "heat", "rth")
    result["minimum_c"], result["maximum_c"] = (
        q(temp.Minimum, "C"),
        q(temp.Maximum, "C"),
    )
    export_table(temp, "heat")
    report["executed_apis"] = [
        "Ansys.ACT.Automation.Mechanical.Analysis.AddTemperature"
    ]

elif case == "acoustic_cavity":
    analysis = model.AddModalAcousticAnalysis()
    configure(analysis)
    regions = list(analysis.GetChildren(DataModelObjectCategory.PhysicsRegion, False))
    if len(regions) > 1:
        raise ValueError("Unexpected pre-existing acoustic regions.")
    region = regions[0] if regions else analysis.AddPhysicsRegion()
    region.Location = whole
    region.Structural = False
    region.Acoustics = True
    settings = analysis.AnalysisSettings
    settings.MaximumModesToFind = 3
    settings.LimitSearchToRange = True
    settings.SearchRangeMinimum = Quantity("100 [Hz]")
    settings.SearchRangeMaximum = Quantity("10000 [Hz]")
    pressure = analysis.Solution.AddAcousticPressureResult()
    result = solve(analysis, "acoustic")
    report["acoustic"] = {
        "region_created_here": not bool(regions),
        "acoustics": bool(region.Acoustics),
        "structural": bool(region.Structural),
        "location_ids": [int(v) for v in region.Location.Ids],
        "search_min_hz": q(settings.SearchRangeMinimum, "Hz"),
        "search_max_hz": q(settings.SearchRangeMaximum, "Hz"),
    }
    export_table(pressure, "acoustic")
    report["executed_apis"] = [
        "Ansys.ACT.Automation.Mechanical.Model.AddModalAcousticAnalysis",
        "Ansys.ACT.Automation.Mechanical.Solution.AddAcousticPressureResult",
    ] + (
        ["Ansys.ACT.Automation.Mechanical.Analysis.AddPhysicsRegion"]
        if not regions
        else []
    )

elif case == "thermal_expansion":
    thermal = initial
    configure(thermal)
    temperature = thermal.AddTemperature()
    temperature.Location = whole
    temperature.Magnitude.Output.DiscreteValues = [
        Quantity(str(p["temperature_c"]) + " [C]")
    ]
    temp = thermal.Solution.AddTemperature()
    source = solve(thermal, "thermal", "rth")
    export_table(temp, "thermal")
    analysis = model.AddStaticStructuralAnalysis()
    configure(analysis)
    analysis.EnvironmentTemperature = Quantity(str(p["reference_c"]) + " [C]")
    fixed = analysis.AddDisplacement()
    fixed.Location = root
    fixed.DefineBy = LoadDefineBy.Components
    fixed.XComponent.Output.DiscreteValues = [Quantity("0 [m]")]
    vertices = [
        (v, [float(v.X) * scale, float(v.Y) * scale, float(v.Z) * scale])
        for v in geo.Vertices
    ]
    root_vertices = [row for row in vertices if abs(row[1][0] - root_center[0]) < 1e-9]
    corner, corner_pos = min(root_vertices, key=lambda row: row[1][1] + row[1][2])
    second = max(
        [row for row in root_vertices if abs(row[1][2] - corner_pos[2]) < 1e-9],
        key=lambda row: row[1][1],
    )[0]
    anchor = analysis.AddDisplacement()
    anchor.Location = selection([corner.Id])
    anchor.DefineBy = LoadDefineBy.Components
    anchor.YComponent.Output.DiscreteValues = [Quantity("0 [m]")]
    anchor.ZComponent.Output.DiscreteValues = [Quantity("0 [m]")]
    roll = analysis.AddDisplacement()
    roll.Location = selection([second.Id])
    roll.DefineBy = LoadDefineBy.Components
    roll.ZComponent.Output.DiscreteValues = [Quantity("0 [m]")]
    imported = analysis.AddImportedLoadMAPDLResultsFile()
    imported.ResultFile = source["result_file"]
    body_temperature = imported.AddImportedBodyTemperature()
    body_temperature.Location = whole
    imported.ImportLoad()
    result = directional(analysis, "Free axial expansion", tip)
    report["transfer"] = {
        "source_file": str(imported.ResultFile),
        "source_min_c": q(body_temperature.SourceMinimum, "C"),
        "source_max_c": q(body_temperature.SourceMaximum, "C"),
        "temperature_c": q(temperature.Magnitude.Output.DiscreteValues[0], "C"),
        "reference_c": q(analysis.EnvironmentTemperature, "C"),
        "target_ids": [int(v) for v in body_temperature.Location.Ids],
        "root_x_ids": [int(v) for v in fixed.Location.Ids],
        "anchor_ids": [corner.Id],
        "roll_ids": [second.Id],
    }
    target = solve(analysis, "expansion")
    target["displacement_m"] = q(result.Average, "m")
    export_table(result, "expansion")
    report["cases"]["transferred"] = dict(target)
    report["cases"]["transferred"]["result_file"] = preserve(
        target["result_file"], "transferred.rst"
    )
    report["executed_apis"] = [
        "Ansys.ACT.Automation.Mechanical.Analysis.AddImportedLoadMAPDLResultsFile",
        "Ansys.ACT.Automation.Mechanical.ImportedLoads.ImportedLoadGroup.AddImportedBodyTemperature",
    ]
else:
    raise ValueError("Unknown acceptance case.")
report["status"] = "complete"
checkpoint("complete")
mcp_result = report
