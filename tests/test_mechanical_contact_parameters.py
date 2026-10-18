"""Evidence-gate tests only; these do not run or emulate an Ansys solve."""
import copy
import unittest

from tools.run_contact_parameters import validate_contact, validate_parameters


class ContactEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.parameters = {"width_mm": 10, "height_mm": 10, "pressure_pa": 1e6}
        self.report = {"stage": "complete", "parameters": {"input": {"id": "P1"}, "output": {"id": "P2"}},
                       "pressure_table": {"times_s": [0, 1, 2], "values_pa": [0, 1e6, 2e6]},
                       "time_samples": [{"reaction_x_n": 100, "maximum_deformation_m": 1e-7, "contact_status_min": 2, "contact_status_max": 3, "average_contact_pressure_pa": 1e6},
                                        {"reaction_x_n": 200, "maximum_deformation_m": 2e-7, "contact_status_min": 2, "contact_status_max": 3, "average_contact_pressure_pa": 2e6}]}

    def test_pressure_times_area_reaction_and_closed_contact_required(self):
        self.assertTrue(validate_contact(self.report, self.parameters)["passed"])
        for field, invalid in (("reaction_x_n", 50), ("contact_status_min", 1), ("average_contact_pressure_pa", 0)):
            changed = copy.deepcopy(self.report)
            changed["time_samples"][0][field] = invalid
            self.assertFalse(validate_contact(changed, self.parameters)["passed"])

    def test_positive_displacement_alone_does_not_prove_double_load_response(self):
        self.report["time_samples"][1]["maximum_deformation_m"] = 4e-7
        self.assertFalse(validate_contact(self.report, self.parameters)["passed"])

    def test_inherited_positive_output_without_updated_design_point_rejected(self):
        report = {"ok": True, "points": [{"name": "1", "input": "0.25", "output": "1e-7 [m]", "up_to_date": True, "retained_valid": True, "input_up_to_date": True, "output_up_to_date": True},
                                           {"name": "2", "input": "0.3", "output": "1e-7[m]", "up_to_date": True, "retained_valid": True, "input_up_to_date": True, "output_up_to_date": True}]}
        self.assertTrue(validate_parameters(report)["passed"])
        report["points"][1]["output_up_to_date"] = False
        self.assertFalse(validate_parameters(report)["passed"])


if __name__ == "__main__":
    unittest.main()
