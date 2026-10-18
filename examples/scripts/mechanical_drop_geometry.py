# Python Script, API Version = V261
# Opt-in SpaceClaim fixture. All dimensions describe a benign laboratory drop.
import json
import os

parameters = globals().get("ACCEPTANCE_PARAMETERS")
if not parameters or parameters.get("fresh_project") is not True:
    raise ValueError("Use the opt-in specialized acceptance runner in a fresh project.")
output_dir = parameters["output_dir"]
root = GetRootPart()
if len(root.Bodies) or len(root.Components):
    raise ValueError("Acceptance geometry requires a fresh empty document.")
step_path = os.path.join(output_dir, "drop.step")
native_path = os.path.join(output_dir, "drop.scdocx")
if os.path.exists(step_path) or os.path.exists(native_path):
    raise ValueError("Acceptance geometry output already exists.")
side = MM(float(parameters.get("block_side_mm", 10.0)))
plate_side = MM(float(parameters.get("plate_side_mm", 40.0)))
plate_height = MM(float(parameters.get("plate_height_mm", 6.0)))
gap = MM(float(parameters.get("gap_mm", 0.02)))
if min(side, plate_side, plate_height, gap) <= 0 or plate_side <= side:
    raise ValueError("Drop dimensions must be positive and the plate wider than the block.")
created = []
for name, first, second in (
    ("acceptance_plate", Point.Create(-plate_side / 2, -plate_side / 2, 0),
     Point.Create(plate_side / 2, plate_side / 2, plate_height)),
    ("acceptance_block", Point.Create(-side / 2, -side / 2, plate_height + gap),
     Point.Create(side / 2, side / 2, plate_height + gap + side)),
):
    result = BlockBody.Create(first, second, ExtrudeType.ForceIndependent)
    if len(result.CreatedBodies) != 1:
        raise RuntimeError("Expected exactly one independent solid per fixture part.")
    body = result.CreatedBodies[0]
    body.Name = name
    created.append({"name": name, "volume_m3": float(body.Shape.Volume)})
expected_volumes = [plate_side * plate_side * plate_height, side ** 3]
if any(abs(item["volume_m3"] / expected - 1) > 1e-8 for item, expected in zip(created, expected_volumes)):
    raise RuntimeError("Drop geometry volume verification failed.")
DocumentSave.Execute(step_path)
DocumentSave.Execute(native_path)
with open(os.path.join(output_dir, "geometry.json"), "w") as stream:
    json.dump({"ok": True, "body_count": 2, "bodies": created, "gap_m": gap}, stream, indent=2)
