from __future__ import annotations

import unittest
from unittest.mock import patch

from tools.run_product_acceptance import record_acceptance_evidence


class AcceptanceEvidenceTests(unittest.TestCase):
    def test_workbench_solved_template_does_not_claim_an_unexecuted_static_factory(self):
        report = {"cases": {name: {"status": "solved"} for name in ("static", "modal", "thermal")}}
        validation = {"ok": True, "checks": {name: {"status": "passed"} for name in report["cases"]}}
        envelope = {"ok": True, "outputs": {"session": {"backend": "workbench", "version": "261"}}}
        with patch("tools.run_product_acceptance.record_runtime_evidence") as record:
            names = record_acceptance_evidence(report, validation, envelope)
        self.assertEqual(record.call_count, 4)
        self.assertTrue(any(name.endswith("Solution.Solve") for name in names))
        self.assertTrue(any(name.endswith("Model.AddModalAnalysis") for name in names))
        self.assertTrue(any(name.endswith("Model.AddSteadyStateThermalAnalysis") for name in names))
        self.assertFalse(any(name.endswith("Model.AddStaticStructuralAnalysis") for name in names))

    def test_failed_numerical_gate_or_failed_case_records_nothing(self):
        envelope = {"ok": True, "outputs": {"session": {"backend": "workbench", "version": "261"}}}
        with patch("tools.run_product_acceptance.record_runtime_evidence") as record:
            self.assertEqual(record_acceptance_evidence({"cases": {"modal": {"status": "solved"}}}, {"ok": False}, envelope), [])
            self.assertEqual(record_acceptance_evidence({"cases": {"modal": {"status": "failed"}}}, {"ok": True}, envelope), [])
        record.assert_not_called()

    def test_direct_static_factory_requires_an_explicit_execution_readback(self):
        report = {"cases": {"static": {"status": "solved"}}}
        validation = {"ok": True, "checks": {"static": {"status": "passed"}}}
        envelope = {"ok": True, "outputs": {"session": {"backend": "pymechanical", "version": "261"}}}
        with patch("tools.run_product_acceptance.record_runtime_evidence"):
            names = record_acceptance_evidence(report, validation, envelope)
            self.assertFalse(any(name.endswith("AddStaticStructuralAnalysis") for name in names))
            report["cases"]["static"]["creation_api"] = "Model.AddStaticStructuralAnalysis"
            names = record_acceptance_evidence(report, validation, envelope)
            self.assertTrue(any(name.endswith("AddStaticStructuralAnalysis") for name in names))


if __name__ == "__main__":
    unittest.main()
