# Python Script, API Version = V261
# Opt-in, independent touching solids for a compression/contact acceptance test.
import json
import os

p = globals().get("CONTACT_PARAMETERS")
if not p:
    raise ValueError("Use the contact/parameter acceptance runner.")
part = GetRootPart()
if len(part.Bodies) or len(part.Components):
    raise ValueError("Contact acceptance requires a fresh empty SpaceClaim document.")
directory = p["output_dir"]
step_path = os.path.join(directory, "compression.step")
native_path = os.path.join(directory, "compression.scdocx")
if os.path.exists(step_path) or os.path.exists(native_path):
    raise ValueError("Contact acceptance geometry already exists.")
length = MM(float(p["segment_length_mm"]))
width = MM(float(p["width_mm"]))
height = MM(float(p["height_mm"]))
for index in range(2):
    block = BlockBody.Create(Point.Create(index * length, 0, 0),
                             Point.Create((index + 1) * length, width, height),
                             ExtrudeType.ForceIndependent)
    if len(block.CreatedBodies) != 1:
        raise RuntimeError("Expected one independent solid per segment.")
    block.CreatedBodies[0].Name = "compression_segment_" + str(index + 1)
if len(part.Bodies) != 2:
    raise RuntimeError("Independent contact geometry did not preserve both bodies.")
volumes = [float(body.Shape.Volume) for body in part.Bodies]
expected = length * width * height
if any(abs(volume - expected) > expected * 1e-8 for volume in volumes):
    raise RuntimeError("Contact geometry volume differs from its contract.")
DocumentSave.Execute(step_path)
DocumentSave.Execute(native_path)
with open(os.path.join(directory, "contact.geometry.json"), "w") as stream:
    json.dump({"ok": True, "body_count": 2, "volumes_m3": volumes,
               "segment_length_mm": p["segment_length_mm"],
               "width_mm": p["width_mm"], "height_mm": p["height_mm"]}, stream)
