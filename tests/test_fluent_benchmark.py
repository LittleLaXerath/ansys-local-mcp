import copy
import math
import unittest

from ansys_mcp.benchmarks import validate_fluent_channel


class FluentBenchmarkTests(unittest.TestCase):
    def fields(self):
        xys = [(0.2 * (i+0.5) / 20, 0.01 * (j+0.5) / 20) for i in range(20) for j in range(20)]
        return {"centroids": [v for point in xys for v in point],
                "u": [0.6*(y/0.01)*(1-y/0.01) for x, y in xys],
                "p": [12*(0.2-x) for x, y in xys]}

    def test_analytic_poiseuille_reference_passes(self):
        fields = self.fields()
        result = validate_fluent_channel(fields, copy.deepcopy(fields), 0.001, -0.001)
        self.assertTrue(result["passed"], result)
        self.assertAlmostEqual(result["metrics"]["pressure_gradient_pa_per_m"], -12)

    def test_velocity_pressure_and_mass_errors_fail_independently(self):
        fields = self.fields()
        for key in ("u", "p"):
            bad = copy.deepcopy(fields)
            bad[key] = [value*1.2 for value in bad[key]]
            result = validate_fluent_channel(bad, bad, 0.001, -0.001)
            self.assertFalse(result["passed"], result)
        result = validate_fluent_channel(fields, fields, 0.001, -0.0009)
        self.assertFalse(result["checks"]["mass_balance"])

    def test_invalid_shapes_nonfinite_and_missing_fields_fail(self):
        fields = self.fields()
        for bad in ({}, {**fields, "centroids": []}, {**fields, "u": [math.nan]*400}):
            self.assertFalse(validate_fluent_channel(bad, fields, 0.001, -0.001)["passed"])
        self.assertFalse(validate_fluent_channel(fields, fields, math.nan, 0)["passed"])

    def test_reopen_changed_arrays_fail_even_with_analytic_first_result(self):
        fields = self.fields()
        reopened = copy.deepcopy(fields)
        reopened["p"][0] += 1
        result = validate_fluent_channel(fields, reopened, 0.001, -0.001)
        self.assertFalse(result["checks"]["save_reopen_identical"])


if __name__ == "__main__":
    unittest.main()
