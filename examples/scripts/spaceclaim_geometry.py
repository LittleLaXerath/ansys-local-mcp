# Python Script, API Version = V261
# Run inside a fresh SpaceClaim document, through either supplied workflow.
import json
import math
import os


workdir = os.environ.get("ANSYS_MCP_WORKDIR", os.getcwd())
output_dir = os.path.join(workdir, ".ansys_mcp", "spaceclaim-example")
with open(os.path.join(output_dir, "parameters.json"), "r") as stream:
    parameters = json.load(stream)

dimensions = [float(parameters[key]) for key in ("length_mm", "width_mm", "height_mm")]
if any(value <= 0 or math.isnan(value) or math.isinf(value) for value in dimensions):
    raise ValueError("Channel dimensions must be finite positive millimetres.")
native_path = os.path.join(output_dir, "channel.scdocx")
step_path = os.path.join(output_dir, "channel.step")
measurement_path = os.path.join(output_dir, "measurements.json")
if any(os.path.exists(path) for path in (native_path, step_path, measurement_path)):
    raise ValueError("Example output already exists. Archive it before running again.")

root = GetRootPart()
if len(root.Bodies) or len(root.Components):
    raise ValueError("This example requires an empty document.")
length, width, height = [MM(value) for value in dimensions]
created = BlockBody.Create(Point.Create(0, 0, 0), Point.Create(length, width, height), ExtrudeType.ForceIndependent)
if len(created.CreatedBodies) != 1:
    raise RuntimeError("Expected exactly one channel fluid body.")
body = created.CreatedBodies[0]
body.Name = "fluid"
selection = Selection.Create(body)
selection.CreateAGroup("fluid")

# Select faces by position, so the example does not depend on recorded face IDs.
groups = {"inlet": [], "outlet": [], "wall": []}
tolerance = MM(0.000001)
for face in selection.ConvertToFaces().Items:
    x = face.EvalMid().Point.X
    group = "inlet" if abs(x) <= tolerance else ("outlet" if abs(x - length) <= tolerance else "wall")
    groups[group].append(face)
if [len(groups[key]) for key in ("inlet", "outlet", "wall")] != [1, 1, 4]:
    raise RuntimeError("Unexpected channel boundary topology.")
for name in ("inlet", "outlet", "wall"):
    Selection.Create(groups[name]).CreateAGroup(name)

volume = float(body.Shape.Volume)
expected_volume = length * width * height
if abs(volume - expected_volume) > max(expected_volume * 1e-8, 1e-15):
    raise RuntimeError("Created volume differs from the requested dimensions.")
DocumentSave.Execute(step_path)
DocumentSave.Execute(native_path)
with open(measurement_path, "w") as stream:
    json.dump({
        "ok": True, "body_count": len(root.Bodies), "dimensions_mm": dimensions,
        "volume_m3": volume, "expected_volume_m3": expected_volume,
        "boundary_face_counts": dict((key, len(value)) for key, value in groups.items()),
        "native_file": native_path, "step_file": step_path,
    }, stream, indent=2)
