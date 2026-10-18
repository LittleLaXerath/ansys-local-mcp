from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path

from tools.run_product_acceptance import analytic_references, mechanical_nodal_values


class ProductAcceptanceHelperTests(unittest.TestCase):
    def test_known_cantilever_and_thermal_references(self):
        values = analytic_references(0.1, 0.01, 0.01, 200e9, 7850, 60.5)
        self.assertAlmostEqual(values["static_tip_m"], 2e-6, places=15)
        self.assertAlmostEqual(values["modal_first_hz"], 815.3807054705454, delta=0.1)
        self.assertAlmostEqual(values["thermal_flux_w_m2"], 60500)

    def test_bad_benchmark_properties_fail_before_comparison(self):
        with self.assertRaises(ValueError):
            analytic_references(0.1, 0.01, 0.01, 0, 7850, 60.5)

    def test_nodal_export_joins_by_node_id_instead_of_row_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "result.txt"
            path.write_text("Node Number\tTotal Deformation (m)\n23\t2e-6\n11\t1e-6\n", encoding="utf-8")
            self.assertEqual(mechanical_nodal_values(path), {23: 2e-6, 11: 1e-6})
            path.write_text("Node Number\tTotal Deformation (m)\n23\t2e-6\n11\t1e-6\n", encoding="utf-16")
            self.assertEqual(mechanical_nodal_values(path), {23: 2e-6, 11: 1e-6})
            path.write_text("Node Number\tTotal Deformation (m)\n23\t2e-6\n23\t1e-6\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                mechanical_nodal_values(path)


if __name__ == "__main__":
    unittest.main()
