# Runs inside Mechanical (including IronPython); launched only by an opt-in runner.
# A linear elastic, low-speed two-body contact test and a driven rigid slider.
# Script preparation or analysis creation is never reported as a solved case.
# Public API references (interfaces only; these do not prove runtime support):
# https://scripting.mechanical.docs.pyansys.com/version/stable/api/ansys/mechanical/stubs/v261/Ansys/ACT/Automation/Mechanical/AnalysisSettings/EXDAnalysisSettings.html
# https://scripting.mechanical.docs.pyansys.com/version/stable/api/ansys/mechanical/stubs/v261/Ansys/ACT/Automation/Mechanical/Connections/Joint.html
# https://scripting.mechanical.docs.pyansys.com/version/stable/api/ansys/mechanical/stubs/v261/Ansys/ACT/Automation/Mechanical/BoundaryConditions/JointLoad.html
import json
import math
import os
import traceback
import Ansys
from Ansys.ACT.Interfaces.Common import SelectionTypeEnum
from Ansys.Mechanical.DataModel.Enums import (
    ContactType, DataModelObjectCategory, ElementOrder, JointConditionType,
    JointScopingType, JointType, KinematicDOF, LoadDefineBy, MethodType,
    SetDriverStyle, SolverUnitsControlType, StiffnessBehavior,
)

if "MechanicalUnitSystem" not in globals():
    from Ansys.ACT.Interfaces.Common import MechanicalUnitSystemEnum as MechanicalUnitSystem

parameters = globals().get("ACCEPTANCE_PARAMETERS")
if not parameters or parameters.get("fresh_project") is not True:
    raise ValueError("Use the opt-in specialized acceptance runner in a fresh project.")
output_dir = parameters["output_dir"]
case_name = parameters["case"]
if case_name not in ("explicit", "lsdyna", "rigid"):
    raise ValueError("Unknown specialized acceptance case.")
checkpoint_path = os.path.join(output_dir, "specialized.checkpoint.json")
if os.path.exists(checkpoint_path):
    raise ValueError("Acceptance refuses to overwrite an existing report.")
model = ExtAPI.DataModel.Project.Model
if len(model.Analyses) > 1 or (len(model.Analyses) == 1 and len(model.Analyses[0].Children) > 3):
    raise ValueError("Acceptance requires a new project with no existing loads or results.")
ExtAPI.Application.ActiveUnitSystem = MechanicalUnitSystem.StandardMKS
report = {"schema_version": 1, "case": case_name, "status": "running", "progress": [],
          "verification": "not_verified", "fixture": "low_speed_elastic_drop" if case_name != "rigid" else "driven_rigid_slider"}


def checkpoint(phase, analysis=None):
    report["active_phase"] = phase
    report["progress"].append(phase)
    if analysis is not None:
        report["analysis_type"] = str(analysis.AnalysisType)
        report["solver_name"] = str(getattr(analysis, "SolverName", "not_exposed"))
        report["solution_status"] = str(analysis.Solution.Status)
        report["solver_directory"] = str(analysis.Solution.WorkingDir)
    with open(checkpoint_path, "w") as stream:
        json.dump(report, stream, indent=2, allow_nan=False)


def selection(ids):
    result = ExtAPI.SelectionManager.CreateSelectionInfo(SelectionTypeEnum.GeometryEntities)
    result.Ids = list(ids)
    return result


def scalar(value, unit):
    converted = Quantity(str(value)).ConvertUnit(unit)
    number = float(converted.Value)
    if math.isnan(number) or math.isinf(number):
        raise RuntimeError("Product returned a nonfinite result.")
    return number


def analysis_for_case():
    methods = {"explicit": "AddExplicitDynamicsAnalysis", "lsdyna": "AddLSDynaAnalysis", "rigid": "AddRigidDynamicsAnalysis"}
    # Actual v261 readback reports RBD as Transient with SolverName MBD.
    # Both explicit solvers use ExplicitDynamics; SolverName disambiguates them.
    expected = {"explicit": "explicitdynamics", "lsdyna": "explicitdynamics", "rigid": "transient"}[case_name]

    def matches(analysis):
        actual = str(analysis.AnalysisType).lower().replace("-", "").replace(" ", "")
        solver = str(getattr(analysis, "SolverName", "not_exposed")).lower().replace("-", "").replace(" ", "").replace("_", "")
        report["analysis_type_readback"] = str(analysis.AnalysisType)
        report["solver_name_readback"] = str(getattr(analysis, "SolverName", "not_exposed"))
        expected_solver = "mbd" if case_name == "rigid" else ("autodyn" if case_name == "explicit" else "lsdyna")
        return actual == expected and expected_solver in solver

    if len(model.Analyses):
        analysis = model.Analyses[0]
        actual = str(analysis.AnalysisType).lower().replace("-", "").replace(" ", "")
        if matches(analysis):
            return analysis
        # Only a new, untouched placeholder may be replaced (never a user model).
        if "static" not in actual:
            raise ValueError("New project has a different analysis type: " + actual)
        checkpoint("before_delete_empty_static_analysis", analysis)
        analysis.Delete()
    analysis = getattr(model, methods[case_name])()
    if not matches(analysis):
        raise RuntimeError("Created analysis type or solver did not match the requested specialized case.")
    return analysis


def configure_time(analysis, end_time):
    if case_name == "lsdyna":
        # The LS-DYNA ACT solver exposes a solver property rather than the
        # generic AnalysisSettings wrapper (which is None in the tested v261).
        # This public path is used by the official PyMechanical LS-DYNA example.
        solver = analysis.Solver
        prop = solver.Properties["Step Controls/Endtime"]
        time_unit = str(ExtAPI.DataModel.CurrentUnitFromQuantityName("Time"))
        time_scale_s = scalar(Quantity("1 [" + time_unit + "]"), "s")
        prop.Value = end_time / time_scale_s
        actual = float(prop.Value) * time_scale_s
        if abs(actual - end_time) > end_time * 1e-8:
            raise RuntimeError("LS-DYNA end time property did not read back.")
        report["solver_settings_channel"] = "Solver.Properties.Step Controls/Endtime"
        report["solver_time_unit"] = time_unit
        report["end_time_s"] = actual
        return
    settings = analysis.AnalysisSettings
    # Explicit and rigid settings do not advertise this common property in v261.
    # When present, derive solver units from the explicitly chosen display system.
    if hasattr(settings, "SolverUnits"):
        settings.SolverUnits = SolverUnitsControlType.ActiveSystem
        report["solver_units_control"] = str(settings.SolverUnits)
    else:
        report["solver_units_control"] = "not_exposed_by_this_settings_type"
    settings.SetStepEndTime(1, Quantity(str(end_time) + " [s]"))
    actual = scalar(settings.GetStepEndTime(1), "s")
    if abs(actual - end_time) > end_time * 1e-8:
        raise RuntimeError("End time assignment did not read back.")
    report["end_time_s"] = actual


def prepare_geometry():
    checkpoint("before_geometry")
    bodies = list(model.Geometry.GetChildren(DataModelObjectCategory.Body, True))
    if not bodies:
        importer = model.GeometryImportGroup.AddGeometryImport()
        importer.Import(parameters["geometry_path"], Ansys.Mechanical.DataModel.Enums.GeometryImportPreference.Format.Automatic,
                        Ansys.ACT.Mechanical.Utilities.GeometryImportPreferences())
        bodies = list(model.Geometry.GetChildren(DataModelObjectCategory.Body, True))
    if len(bodies) != 2:
        raise RuntimeError("Expected exactly the two acceptance solids.")
    unit = str(ExtAPI.DataModel.GeoData.Unit)
    scale = scalar(Quantity("1 [" + unit + "]"), "m")
    parts = []
    for body in bodies:
        body.Material = "Structural Steel"
        if str(body.Material) != "Structural Steel":
            raise RuntimeError("Structural Steel assignment did not read back.")
        geo = body.GetGeoBody()
        faces = list(geo.Faces)
        if len(faces) != 6:
            raise RuntimeError("Expected rectangular solids with six faces.")
        centroids = [(face, [float(value) * scale for value in face.Centroid]) for face in faces]
        z_min = min(point[2] for face, point in centroids)
        z_max = max(point[2] for face, point in centroids)
        parts.append((z_min, body, geo, centroids, z_max))
    parts.sort(key=lambda item: item[0])
    plate, block = parts
    plate_side = float(parameters.get("plate_side_mm", 40.0)) / 1000.0
    plate_height = float(parameters.get("plate_height_mm", 6.0)) / 1000.0
    block_side = float(parameters.get("block_side_mm", 10.0)) / 1000.0
    dimensions = []
    for part, expected in ((plate, [plate_side, plate_side, plate_height]), (block, [block_side] * 3)):
        extents = [max(point[axis] for face, point in part[3]) - min(point[axis] for face, point in part[3]) for axis in range(3)]
        if any(abs(actual - reference) > 1e-8 for actual, reference in zip(extents, expected)):
            raise RuntimeError("Acceptance body dimensions or orientation differ from the metre contract.")
        dimensions.append(extents)
    expected_gap = float(parameters.get("gap_mm", 0.02)) / 1000.0
    gap = block[0] - plate[4]
    if abs(gap - expected_gap) > 1e-8:
        raise RuntimeError("Geometry gap differs from the metre contract: " + str(gap))
    bottom = [face.Id for face, point in plate[3] if abs(point[2] - plate[0]) < 1e-8]
    if len(bottom) != 1:
        raise RuntimeError("Plate bottom selection is ambiguous.")
    report["geometry"] = {"bodies": 2, "faces": 12, "cad_unit": unit, "gap_m": gap,
                           "dimensions_m": dimensions,
                           "plate_body_id": int(plate[2].Id), "block_body_id": int(block[2].Id),
                           "material": "Structural Steel"}
    checkpoint("after_geometry")
    return plate, block, bottom


def solve_and_export(analysis, block, end_time):
    checkpoint("before_result_creation", analysis)
    deformation = analysis.Solution.AddTotalDeformation()
    deformation.Location = selection([block[2].Id])
    # The acceptance evaluates the requested final time explicitly.
    deformation.By = SetDriverStyle.Time
    deformation.DisplayTime = Quantity(str(end_time) + " [s]")
    checkpoint("before_solve", analysis)
    analysis.Solution.Solve(True)
    checkpoint("after_solve", analysis)
    if "done" not in str(analysis.Solution.Status).lower():
        raise RuntimeError("Solver did not report a completed solution: " + str(analysis.Solution.Status))
    analysis.Solution.EvaluateAllResults()
    checkpoint("after_result_evaluation", analysis)
    displacement = scalar(deformation.Maximum, "m")
    export_path = os.path.join(output_dir, case_name + "_deformation.txt")
    deformation.ExportToTextFile(export_path)
    if not os.path.isfile(export_path) or os.path.getsize(export_path) == 0:
        raise RuntimeError("Result table export is missing or empty.")
    report["maximum_deformation_m"] = displacement
    report["result_table"] = export_path
    result_file = str(analysis.ResultFileName)
    report["result_file"] = result_file
    report["result_file_exists"] = os.path.isfile(result_file)
    checkpoint("after_result_export", analysis)


try:
    checkpoint("before_analysis")
    analysis = analysis_for_case()
    analysis.Name = "Acceptance " + case_name
    checkpoint("after_analysis", analysis)
    plate, block, bottom = prepare_geometry()
    checkpoint("before_contact_cleanup", analysis)
    # Import can create automatic bonded pairs. This benign drop uses only the
    # explicitly constructed frictionless body interaction.
    for contact in model.Connections.GetChildren(DataModelObjectCategory.ContactRegion, True):
        contact.Suppressed = True
    if case_name == "rigid":
        plate[1].Suppressed = True
        block[1].StiffnessBehavior = StiffnessBehavior.Rigid
        end_time = float(parameters.get("rigid_end_time_s", 0.1))
        speed = float(parameters.get("rigid_speed_m_s", 0.01))
        if not 0 < speed <= 0.1 or not 0 < end_time <= 1.0:
            raise ValueError("Rigid fixture requires a small positive prescribed motion.")
        checkpoint("before_joint", analysis)
        joint = model.Connections.AddJoint()
        joint.Name = "Acceptance Ground Slider"
        joint.ConnectionType = JointScopingType.BodyToGround
        joint.Type = JointType.Translational
        joint.MobileLocation = selection([face.Id for face, point in block[3]])
        report["joint"] = {"type": str(joint.Type), "connection_type": str(joint.ConnectionType),
                           "mobile_ids": [int(value) for value in joint.MobileLocation.Ids]}
        checkpoint("after_joint", analysis)
        driver = analysis.AddJointLoad()
        driver.Joint = joint
        # Mechanical's Translational joint has its free translation on local X
        # (UX), whereas its Revolute and Cylindrical joints use the Z axis.
        # https://ansyshelp.ansys.com/public/Views/Secured/corp/v242/en/wb_sim/ds_load_joint_condition.html
        # v261 derives this single DOF from the joint and makes it read-only.
        report["driver_dof_readback"] = str(driver.DOF)
        checkpoint("after_driver_joint_assignment", analysis)
        if driver.DOF != KinematicDOF.XDisplacement:
            raise RuntimeError("The translational driver did not expose its expected UX degree of freedom.")
        driver.JointConditionType = JointConditionType.Velocity
        driver.Magnitude.Output.DiscreteValues = [Quantity(str(speed) + " [m s^-1]")]
        report["driver"] = {"type": str(driver.JointConditionType), "dof": str(driver.DOF), "speed_m_s": speed}
        checkpoint("after_driver", analysis)
    else:
        speed = float(parameters.get("drop_speed_m_s", 0.1))
        end_time = float(parameters.get("drop_end_time_s", 0.0005))
        if not 0 < speed <= 0.1 or not 0 < end_time <= 0.005:
            raise ValueError("Drop fixture is restricted to low speed and a short observation.")
        if end_time <= report["geometry"]["gap_m"] / speed:
            raise ValueError("Drop end time must extend beyond first contact.")
        fixed = analysis.AddFixedSupport()
        fixed.Location = selection(bottom)
        checkpoint("after_fixed_support", analysis)
        interaction = model.Connections.AddBodyInteraction()
        interaction.Location = selection([plate[2].Id, block[2].Id])
        interaction.ContactType = ContactType.Frictionless
        report["contact"] = {"type": str(interaction.ContactType), "ids": [int(value) for value in interaction.Location.Ids]}
        checkpoint("after_body_interaction", analysis)
        velocity = analysis.AddInitialVelocity()
        velocity.Location = selection([block[2].Id])
        velocity.DefineBy = LoadDefineBy.Components
        velocity.XComponent = Quantity("0 [m s^-1]")
        velocity.YComponent = Quantity("0 [m s^-1]")
        velocity.ZComponent = Quantity(str(-speed) + " [m s^-1]")
        report["initial_velocity_m_s"] = [scalar(velocity.XComponent, "m s^-1"), scalar(velocity.YComponent, "m s^-1"), scalar(velocity.ZComponent, "m s^-1")]
        if abs(report["initial_velocity_m_s"][2] + speed) > 1e-10:
            raise RuntimeError("Initial velocity did not read back.")
        settings = analysis.AnalysisSettings
        report["erosion"] = {}
        for name in ("ErosionOnGeomStrainType", "ErosionOnMaterialFailureType", "ErosionOnMinElemTimestepType"):
            if hasattr(settings, name):
                setattr(settings, name, False)
                report["erosion"][name] = bool(getattr(settings, name))
                if report["erosion"][name]:
                    raise RuntimeError("Erosion could not be disabled.")
        checkpoint("after_initial_velocity", analysis)
        mesh = model.Mesh
        mesh.ElementSize = Quantity("2 [mm]")
        mesh.ElementOrder = ElementOrder.Linear
        method = mesh.AddAutomaticMethod()
        method.Location = selection([plate[2].Id, block[2].Id])
        method.Method = MethodType.Sweep
        checkpoint("before_mesh", analysis)
        mesh.GenerateMesh()
        report["mesh"] = {"nodes": int(mesh.Nodes), "elements": int(mesh.Elements), "order": str(mesh.ElementOrder), "method": str(method.Method)}
        if report["mesh"]["nodes"] <= 0 or report["mesh"]["elements"] <= 0:
            raise RuntimeError("Drop mesh is empty.")
        checkpoint("after_mesh", analysis)
    checkpoint("before_time_settings", analysis)
    configure_time(analysis, end_time)
    checkpoint("after_time_settings", analysis)
    solve_and_export(analysis, block, end_time)
    if case_name == "rigid":
        expected = speed * end_time
        relative_error = abs(report["maximum_deformation_m"] / expected - 1.0)
        report["validation"] = {"expected_displacement_m": expected, "relative_error": relative_error, "tolerance": 0.01,
                                 "passed": relative_error <= 0.01}
    else:
        # A minimal contact sanity gate: free flight would traverse speed*t.
        # A solved elastic contact should keep deformation finite, nonzero and
        # below that travel; the raw table is retained for later energy checks.
        free_flight = speed * end_time
        passed = 0 < report["maximum_deformation_m"] < free_flight * 0.8
        report["validation"] = {"free_flight_travel_m": free_flight, "first_contact_time_s": report["geometry"]["gap_m"] / speed,
                                 "passed": passed, "level": "contact_sanity_only"}
    if not report["validation"]["passed"]:
        raise RuntimeError("Specialized result sanity check failed.")
    report["status"] = "solved"
    report["verification"] = "product_solve_and_result_sanity"
    checkpoint("complete", analysis)
except Exception as error:
    report["status"] = "failed"
    report["error"] = str(error)
    report["traceback"] = traceback.format_exc()
    # Keep the actual product exception, including any license or API failure.
    checkpoint("failed")
mcp_result = report
