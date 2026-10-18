# Python Script, API Version = V261
# Executed by SpaceClaim in a fresh document. Parameters are supplied by the
# opt-in acceptance runner; all native geometry remains in its private run folder.
import json
import os

parameters = globals().get("ACCEPTANCE_PARAMETERS")
if not parameters:
    raise ValueError("Use the opt-in product acceptance runner to supply parameters.")
output_dir = parameters["output_dir"]
step_path = os.path.join(output_dir, "beam.step")
native_path = os.path.join(output_dir, "beam.scdocx")
root = GetRootPart()
if len(root.Bodies) or len(root.Components):
    raise ValueError("Acceptance geometry requires a fresh empty document.")
if os.path.exists(step_path) or os.path.exists(native_path):
    raise ValueError("Acceptance geometry output already exists.")
length, width, height = [MM(float(parameters[name])) for name in ("length_mm", "width_mm", "height_mm")]
created = BlockBody.Create(Point.Create(0, 0, 0), Point.Create(length, width, height), ExtrudeType.ForceIndependent)
if len(created.CreatedBodies) != 1:
    raise RuntimeError("Expected one rectangular solid.")
body = created.CreatedBodies[0]
body.Name = "acceptance_beam"
volume = float(body.Shape.Volume)
expected = length * width * height
if abs(volume - expected) > expected * 1e-8:
    raise RuntimeError("Unexpected acceptance geometry volume.")
DocumentSave.Execute(step_path)
DocumentSave.Execute(native_path)
with open(os.path.join(output_dir, "geometry.json"), "w") as stream:
    json.dump({"ok": True, "body_count": 1, "volume_m3": volume,
               "dimensions_mm": [parameters[name] for name in ("length_mm", "width_mm", "height_mm")]}, stream)
