"""Prepare an optional Engineering Data round-trip fixture; never launch Ansys.

Use an isolated single-body STEP file. An optional --body-name requires an exact
Mechanical tree name; otherwise the fixture requires exactly one body.
The generated workflow creates its own synthetic elastic material, writes and
reads its properties, exports/imports MatML, then assigns the imported material.
All product operations use the structured bridge; no raw product script or
solver is required. This fixture has not, by itself, verified a real product.
"""
from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path
from typing import Any


DENSITY_INITIAL = "1000 [kg m^-3]"
DENSITY_MODIFIED = "1100 [kg m^-3]"
YOUNGS_MODULUS = "2000 [MPa]"
POISSONS_RATIO = "0.25"


def build_workflow(
    geometry_path: str, workdir: str, *, body_name: str | None = None,
    material_name: str | None = None, output_dir: str | None = None,
) -> dict[str, Any]:
    if body_name is not None and not body_name.strip():
        raise ValueError("The fixture requires a nonempty unique body name.")
    material = material_name or "MCP synthetic elastic " + uuid.uuid4().hex[:12]
    if not material.strip():
        raise ValueError("The fixture material name must be nonempty.")
    directory = Path(output_dir) if output_dir else Path(".ansys_mcp") / "engineering-data" / uuid.uuid4().hex
    exported = str(directory / "fixture_material.xml")

    def ref(name: str, index: int | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {"$type": "local_ref", "name": name}
        if index is not None:
            result["index"] = index
        return result

    def ed(operation_id: str, member: str, *, target: Any = "EngineeringData", **kwargs: Any) -> dict[str, Any]:
        return {"id": operation_id, "op": "engineering_data", "action": "call", "target": target, "member": member, **kwargs}

    operations = [
        ed("create_material", "CreateMaterial", kwargs={"Name": material}, store_as="created_material"),
        ed("create_density", "CreateProperty", target=ref("created_material"), kwargs={"Name": "Density"}, store_as="density"),
        ed("set_density", "SetData", target=ref("density"), kwargs={"SheetName": "", "Index": 0, "Variables": ["Density"], "Values": [[DENSITY_INITIAL]]}),
        ed("density_initial", "GetData", target=ref("density"), kwargs={"Variables": ["Density"], "AsDictionary": False}),
        ed("create_elasticity", "CreateProperty", target=ref("created_material"), kwargs={"Name": "Elasticity", "Behavior": "Isotropic"}, store_as="elasticity"),
        ed("set_elasticity", "SetData", target=ref("elasticity"), kwargs={"SheetName": "", "Index": 0, "Variables": ["Young's Modulus", "Poisson's Ratio"], "Values": [[YOUNGS_MODULUS], [POISSONS_RATIO]]}),
        ed("elasticity_initial", "GetData", target=ref("elasticity"), kwargs={"Variables": ["Young's Modulus", "Poisson's Ratio"], "AsDictionary": False}),
        ed("export_library", "WriteMaterials", target="EngData", kwargs={"MaterialList": [ref("created_material")], "FilePath": {"$type": "path", "path": exported, "role": "output"}, "Format": "MatML31", "UnitSystem": "MKS_STANDARD", "OverwriteTarget": False}),
        # Delete only the object returned by this fixture's successful create.
        # An existing name causes creation to fail before any deletion occurs.
        ed("remove_created_material", "Delete", target=ref("created_material")),
        ed("import_material", "ImportMaterial", kwargs={"Name": material, "Source": {"$type": "path", "path": exported, "role": "input"}}, invalidate_refs=True, store_as="imported_material"),
        ed("find_imported_density", "GetProperty", target=ref("imported_material"), kwargs={"Name": "Density"}, store_as="imported_density"),
        ed("density_reimported", "GetData", target=ref("imported_density"), kwargs={"Variables": ["Density"], "AsDictionary": False}),
        ed("modify_imported_density", "SetData", target=ref("imported_density"), kwargs={"SheetName": "", "Index": 0, "Variables": ["Density"], "Values": [[DENSITY_MODIFIED]]}),
        ed("density_modified", "GetData", target=ref("imported_density"), kwargs={"Variables": ["Density"], "AsDictionary": False}),
        {"id": "refresh_model_materials", "op": "call", "target": "Model.Materials", "member": "RefreshMaterials", "invalidate_refs": True},
        {"id": "find_unique_body", "op": "find", "category": {"$type": "enum", "type": "DataModelObjectCategory", "name": "Body"},
         **({"name": body_name} if body_name is not None else {}), "limit": 1, "store_as": "bodies"},
        {"id": "body_name", "op": "get", "target": ref("bodies", 0), "member": "Name"},
        {"id": "assign_imported_material", "op": "set", "target": ref("bodies", 0), "member": "Material", "value": material},
        {"id": "assignment", "op": "get", "target": ref("bodies", 0), "member": "Material"},
    ]
    return {
        "version": "1", "workdir": workdir,
        "variables": {"fixture_material_name": material, "fixture_body_name": body_name},
        "steps": [{
            "id": "engineering_data_roundtrip", "type": "mechanical",
            "recipe": {"version": "1", "backend": "workbench", "mode": "batch", "launch": {"elevation": "auto"},
                       "project": {"template_name": "Static Structural (ANSYS)", "geometry_path": geometry_path,
                                   "save_path": str(directory / "engineering-data.wbpj"), "overwrite": False},
                       "operations": operations},
            "produces": [{"name": "material_library", "path": exported},
                         {"name": "project", "path": str(directory / "engineering-data.wbpj")}],
        }],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--geometry", required=True)
    parser.add_argument("--workdir", default=".")
    parser.add_argument("--body-name", help="Optional exact Mechanical tree body name. Omit for a unique single-body model.")
    parser.add_argument("--material-name")
    parser.add_argument("--output", required=True, type=Path, help="New JSON workflow path; existing files are never overwritten.")
    args = parser.parse_args()
    workflow = build_workflow(args.geometry, str(Path(args.workdir).resolve()), body_name=args.body_name, material_name=args.material_name)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(workflow, stream, ensure_ascii=True, indent=2)
    print(json.dumps({"prepared": True, "launched_product": False, "workflow_path": str(args.output),
                      "required_readbacks": ["density_initial", "density_reimported", "density_modified", "elasticity_initial", "body_name", "assignment"]}))


if __name__ == "__main__":
    main()
