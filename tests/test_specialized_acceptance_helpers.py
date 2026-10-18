from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import os

from tools.run_specialized_acceptance import prepare, resolve_saved_result_path, validate


class SpecializedAcceptanceGateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.table = Path(self.temporary.name) / "result.txt"
        self.table.write_text("node\tdisplacement\n1\t0.001\n", encoding="utf-8")

    def report(self, case="rigid"):
        return {"case": case, "status": "solved", "solution_status": "Done",
                "progress": ["before_solve", "after_solve", "after_result_evaluation", "after_result_export", "complete"],
                "result_table": str(self.table), "maximum_deformation_m": 0.001, "end_time_s": 0.1,
                "result_file": str(self.table),
                "joint": {"mobile_ids": [1], "type": "Translational"}, "driver": {"speed_m_s": 0.01}}

    def test_rigid_case_requires_the_prescribed_one_millimetre_motion(self):
        report = self.report()
        self.assertTrue(validate(report)["ok"])
        report["maximum_deformation_m"] = 0.000001
        self.assertFalse(validate(report)["ok"])

    def test_preparation_and_an_empty_export_cannot_pass(self):
        report = self.report()
        report["status"] = "prepared"
        self.assertFalse(validate(report)["ok"])
        report["status"] = "solved"
        self.table.write_text("", encoding="utf-8")
        self.assertFalse(validate(report)["ok"])

    def test_drop_case_rejects_an_unchanged_free_flight_trajectory(self):
        report = self.report("explicit")
        report.update({"initial_velocity_m_s": [0, 0, -0.1], "end_time_s": 0.0005,
                       "geometry": {"gap_m": 0.00002}, "mesh": {"elements": 8},
                       "contact": {"type": "Frictionless"}, "erosion": {"on_strain": False},
                       "maximum_deformation_m": 0.00005})
        self.assertFalse(validate(report)["ok"])
        report["maximum_deformation_m"] = 0.00002
        self.assertTrue(validate(report)["ok"])

    def test_preparation_uses_the_native_lsdyna_template_name_and_script_snapshot(self):
        directory = self.table.parent / "prepared"
        directory.mkdir()
        root = Path(__file__).resolve().parents[1]
        _, recipe = prepare(directory, root, case="lsdyna", backend="workbench", elevation="never")
        self.assertEqual(recipe["project"]["template_name"], "LSDYNA")
        wrapper = Path(recipe["operations"][1]["script_path"]).read_text(encoding="utf-8")
        self.assertIn("scripts", wrapper)
        self.assertTrue((directory / "scripts" / "mechanical_specialized_acceptance.py").is_file())

    def test_save_as_result_relocation_retains_the_original_observation(self):
        project = self.table.parent / "saved.wbpj"
        project.write_text("fixture", encoding="utf-8")
        retained = self.table.parent / "saved_files" / "solver" / "d3plot"
        retained.parent.mkdir(parents=True)
        retained.write_bytes(b"native-result-fixture")
        report = {"status": "solved", "result_file": str(self.table.parent / "old_files" / "d3plot")}
        with patch.dict(os.environ, {"ANSYS_MCP_ALLOWED_ROOTS": str(self.table.parent)}):
            resolved = resolve_saved_result_path(report, project)
        self.assertEqual(resolved["result_file"], str(retained))
        self.assertEqual(resolved["result_file_before_project_save"], report["result_file"])
        self.assertNotIn("result_path_relocated_after_save", report)


if __name__ == "__main__":
    unittest.main()
