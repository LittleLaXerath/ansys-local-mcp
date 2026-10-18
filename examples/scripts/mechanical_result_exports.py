# Public Graphics API only. Reopen an already solved benchmark; never solve it.
import json
import os
import Ansys
from Ansys.Mechanical.DataModel.Enums import (
    GraphicsAnimationExportFormat, GraphicsBackgroundType, GraphicsImageExportFormat,
    GraphicsResolutionType, ResultAnimationRangeType, ViewOrientationType,
)

parameters = globals().get("ACCEPTANCE_PARAMETERS")
if not parameters or not parameters.get("output_dir"):
    raise ValueError("Use the isolated result-export acceptance runner.")
output_dir = parameters["output_dir"]
model = ExtAPI.DataModel.Project.Model
analyses = [analysis for analysis in model.Analyses if str(analysis.Name) == parameters["analysis_name"]]
if len(analyses) != 1:
    raise RuntimeError("The requested solved analysis is not unique.")
analysis = analyses[0]
status_before = str(analysis.Solution.Status)
if "done" not in status_before.lower():
    raise RuntimeError("Result exports require a previously solved analysis; no solve is attempted.")
results = [child for child in analysis.Solution.Children if str(child.GetType().FullName).endswith(".TotalDeformation")]
if len(results) != 1:
    raise RuntimeError("Expected one existing TotalDeformation result in the selected analysis.")
result = results[0]
report = {"status": "running", "solve_called": False, "analysis_name": str(analysis.Name),
          "analysis_type": str(analysis.AnalysisType),
          "result_type": str(result.GetType().FullName), "solution_status_before": status_before,
          "progress": []}


def checkpoint(phase):
    report["active_phase"] = phase
    report["progress"].append(phase)
    with open(os.path.join(output_dir, "exports.checkpoint.json"), "w") as stream:
        json.dump(report, stream, indent=2)


checkpoint("before_activate")
result.Activate()
checkpoint("before_evaluate_existing_result")
analysis.Solution.EvaluateAllResults()
report["solution_status_after"] = str(analysis.Solution.Status)
if "done" not in report["solution_status_after"].lower():
    raise RuntimeError("Previously solved status changed during result evaluation.")
maximum = Quantity(str(result.Maximum)).ConvertUnit("m")
report["maximum_deformation_m"] = float(maximum.Value)
graphics = ExtAPI.Graphics
report["graphics_type"] = str(graphics.GetType().FullName)
graphics.Camera.SetSpecificViewOrientation(ViewOrientationType.Iso)
graphics.Camera.SetFit()
table_path = os.path.join(output_dir, "deformation.txt")
png_path = os.path.join(output_dir, "deformation.png")
animation_format = parameters.get("animation_format", "GIF")
if animation_format not in ("GIF", "MP4"):
    raise ValueError("Acceptance exports GIF or MP4 only.")
animation_path = os.path.join(output_dir, "deformation." + animation_format.lower())
if any(os.path.exists(path) for path in (table_path, png_path, animation_path)):
    raise ValueError("Result-export acceptance refuses to overwrite existing artifacts.")
checkpoint("before_table")
result.ExportToTextFile(table_path)
report["table_path"] = table_path
report["table_length_unit"] = str(ExtAPI.DataModel.CurrentUnitFromQuantityName("Length"))
report["table_length_scale_m"] = float(Quantity("1 [" + report["table_length_unit"] + "]").ConvertUnit("m").Value)
checkpoint("before_png")
image_settings = Ansys.Mechanical.Graphics.GraphicsImageExportSettings()
image_settings.CurrentGraphicsDisplay = False
image_settings.Width = int(parameters["width"])
image_settings.Height = int(parameters["height"])
image_settings.Background = GraphicsBackgroundType.White
image_settings.Resolution = GraphicsResolutionType.NormalResolution
graphics.ExportImage(png_path, GraphicsImageExportFormat.PNG, image_settings)
report["png_path"] = png_path
checkpoint("before_animation")
report["animation_path"] = animation_path
report["animation_format"] = animation_format
reader = analysis.GetResultsData()
try:
    report["result_sets"] = len(list(reader.ListTimeFreq))
finally:
    reader.Dispose()
if report["result_sets"] < 2:
    raise RuntimeError("This animation fixture requires multiple solved result sets or modes; no solve is attempted.")
if parameters.get("animation_options", "minimal") == "configured":
    checkpoint("before_read_animation_options")
    options = graphics.ResultAnimationOptions
    report["animation_option_readback"] = {"range_type": str(options.RangeType), "frames": int(options.NumberOfFrames),
                                            "duration_s": float(options.Duration.ConvertUnit("s").Value)}
    checkpoint("after_read_animation_options")
    checkpoint("before_range_type_setter")
    options.RangeType = ResultAnimationRangeType.Distributed
    checkpoint("after_range_type_setter")
    options.NumberOfFrames = int(parameters["frames"])
    checkpoint("after_frames_setter")
    options.Duration = Quantity(str(parameters["duration_s"]) + " [s]")
    checkpoint("after_duration_setter")
    animation_settings = Ansys.Mechanical.Graphics.AnimationExportSettings()
    animation_settings.Width = int(parameters["width"])
    checkpoint("after_animation_width_setter")
    animation_settings.Height = int(parameters["height"])
    checkpoint("after_animation_height_setter")
    checkpoint("before_export_animation")
    result.ExportAnimation(animation_path, getattr(GraphicsAnimationExportFormat, animation_format), animation_settings)
else:
    checkpoint("before_export_animation")
    result.ExportAnimation(animation_path, getattr(GraphicsAnimationExportFormat, animation_format))
report["configured_size"] = [int(parameters["width"]), int(parameters["height"])]
report["animation_options"] = parameters.get("animation_options", "minimal")
for path in (table_path, png_path, animation_path):
    if not os.path.isfile(path) or os.path.getsize(path) <= 0:
        raise RuntimeError("A requested native result export is missing or empty.")
report["status"] = "exported"
checkpoint("complete")
mcp_result = report
